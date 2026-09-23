"""Menu-derived table authorization for AD-HOC SQL.

WHAT THIS IS
------------
The injected table-scope half of authorization. The built-in denylist (:mod:`epicor_mcp.sql.
denylist`) is absolute — payroll/PII is denied to everyone, SecurityMgr
included. THIS gate is relative: a caller may read only the tables their own
Epicor menu access reaches (e-mail -> Epicor username -> launchable menus ->
BOs -> tables, computed in ``discovery/authz.py``; default deny, nothing is
unioned in). The scope object arrives already computed; nothing here performs a
lookup, an HTTP call, or an import of the discovery package — this module is
pure local CPU, which is what lets it live under ``sql/`` at all
(``test_query_no_write_methods.py`` pins the Epicor endpoint set over every
file here, and the scope check adds none).

FOUR RULES, EACH LOAD-BEARING
-----------------------------
1. **Read Epicor's OWN resolution, never the SQL text**. The tables come from
   :func:`denylist.db_tables_read` over the
   parsed ``QueryTable`` rows — ``TableType == 'DB'`` only, ``SQ``/``TT``
   (CTE / derived / ``Calculated``) skipped, the SAME single extraction the
   deny-list runs. Regexing FROM/JOIN is the classic bypass and stays banned.
2. **After the deny-list, before the lint** (``adhoc.run_sql`` step 3b). Deny
   beats everything: a payroll table INSIDE a caller's scope must still come
   back ``table_access_denied``, never ``table_not_authorized`` — the first is
   final policy, the second invites a request for wider access.
3. **Three states, and a failure NEVER reads as unlimited.** UNLIMITED
   (SecurityMgr / mode=off) passes untouched; SCOPED filters; UNAVAILABLE
   fails CLOSED with a RETRYABLE envelope (``terminal: false`` — the scope is
   never cached on failure, so the next call recomputes it). The scope object
   is duck-typed (``is_unlimited`` / ``is_unavailable`` / ``allows`` /
   ``email`` / ``reason``) so this package never imports ``discovery``.
4. **Ad-hoc SQL only.** The saved-BAQ path is deliberately NOT scope-gated
   (BAQ/dashboard access mirrors Epicor's own
   model, where a shared BAQ is its own grant). ``WedgeRuntime.run`` resolves
   the scope only on the ``sql`` branch, and a test pins that the saved-BAQ
   branch never asks the authorizer anything.

The membership test itself is :meth:`AuthzScope.allows` — the ONE normaliser
(design point 7). This module never strips a schema or lowercases a name;
handing ``allows()`` the qualified ``Erp.JobHead`` spelling is the contract.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from epicor_mcp.sql import denylist
from epicor_mcp.sql.envelope import error_envelope

logger = logging.getLogger(__name__)

__all__ = [
    "AUTHZ_UNAVAILABLE_GUIDANCE",
    "check_table_scope",
    "table_not_authorized_envelope",
    "unavailable_envelope",
]

#: The retry guidance EVERY ``authorization_unavailable`` envelope carries —
#: one copy, imported by ``discovery/tools.py`` too, so the claim can never
#: fork. It deliberately does not say "usually transient — retry the same
#: call", which for a cold SCOPED user is false for minutes — the FIRST
#: authorization for a user crawls Epicor menu security, which can be slow.
#: The background claim below is guaranteed by the implementation: the
#: crawl is single-flighted and primed at connect, so it keeps building after
#: this refusal returns.
AUTHZ_UNAVAILABLE_GUIDANCE = (
    "The first authorization for a user reads Epicor menu security and can "
    "take a minute or two; the snapshot continues building in the background, "
    "so retry shortly — a failed lookup is never cached. If this persists, an "
    "administrator can refresh or inspect the snapshot (admin authz endpoints)."
)


def unavailable_envelope(reason: str, *, sql: str = "") -> dict[str, Any]:
    """Authorization could not be COMPUTED, so nothing may run — fail closed.

    ``terminal: false`` on purpose: an UNAVAILABLE scope is never cached
    (discovery/authz.py, design point 5), so the very same call can succeed as
    soon as the snapshot recovers. A terminal refusal here would teach the
    model that the question itself is unanswerable, which is the one thing this
    envelope must not say.
    """
    why = (reason or "").strip() or "the authorization source did not answer"
    return error_envelope(
        "authorization_unavailable",
        f"Your table authorization could not be computed ({why}), so this query was "
        "NOT run and nothing was read — authorization fails closed, never open. "
        + AUTHZ_UNAVAILABLE_GUIDANCE,
        retry_with={"sql": sql} if sql else None,
        detail={"stage": "authz", "reason": why},
        terminal=False,
    )


def table_not_authorized_envelope(
    unauthorized: list[str],
    authorized: list[str],
    *,
    email: str = "",
    sql: str = "",
) -> dict[str, Any]:
    """The SCOPED refusal. Names the tables the caller lacks — they wrote those
    names themselves, so echoing them leaks nothing — and says where the
    decision comes from, because "access denied" with no provenance reads as a
    server bug and gets retried verbatim.

    Deliberately NOT named here: any discovery tool. This package must keep
    working when ``epicor_tables``/``epicor_fields`` are not registered, so the
    pointer is added conditionally at the
    one funnel that knows the tool list — ``tool.py``.
    """
    missing = sorted(set(str(t) for t in unauthorized))
    clean = sorted(set(str(t) for t in authorized))
    who = f" for {email}" if email else ""
    valid: dict[str, Any] = {}
    if clean:
        valid["authorized_tables_in_this_query"] = clean
    valid["policy"] = "Only tables explicitly granted by the configured authorization policy may be read. Built-in denials always take precedence."
    return error_envelope(
        "table_not_authorized",
        f"Table(s) {', '.join(missing)} are outside the configured table authorization{who}. "
        "The query was not executed. Ask the administrator to review the table whitelist or user grants.",
        valid=valid,
        detail={
            "stage": "authz",
            "unauthorized_tables": missing,
            "authorized_tables_in_this_query": clean,
            "enforced_on": "Epicor's own ParseFromSQL QueryTable rows "
            "(TableType='DB') — never the SQL text",
        },
        terminal=True,
    )


def check_table_scope(
    scope: Any, ds: Mapping[str, Any], *, sql: str = ""
) -> dict[str, Any] | None:
    """``None`` = the statement may run; an INV-1 envelope = it may not.

    *scope* is an ``AuthzScope``-shaped object (or ``None`` = ungated — the
    standalone wedge server and every pre-gate test). *ds* is the RUNTIME
    tableset the deny-list just evaluated, so by the time this runs, every
    extraction anomaly (invalid TableType, blank DBTableName, a crashed
    evaluator) has ALREADY refused the statement one gate earlier — but none of
    that is assumed: extraction failures here still fail closed on their own.
    """
    if scope is None:
        return None
    if getattr(scope, "is_unlimited", False):
        return None
    if getattr(scope, "is_unavailable", False):
        # Defence in depth: WedgeRuntime refuses an UNAVAILABLE scope before
        # the pipe ever runs (zero Epicor calls). If a future caller passes one
        # through anyway, it must still refuse rather than fall through to the
        # SCOPED branch, where `allows()` returning False for everything would
        # produce a misleading "not authorized for <every table>" message.
        return unavailable_envelope(
            getattr(scope, "reason", "") or "authorization unavailable", sql=sql
        )

    try:
        tables = denylist.db_tables_read(ds)
    except Exception as exc:  # noqa: BLE001 - an authz gate must fail CLOSED
        logger.warning("scope gate could not extract tables: %s", exc)
        return unavailable_envelope(
            f"table extraction raised {type(exc).__name__}", sql=sql
        )
    if not tables:
        # A parsed statement with ZERO resolvable DB tables is the shape-guard
        # precedent (an all-empty Denial is falsy): a gate
        # that cannot see what a statement reads must refuse it, not wave it
        # through because nothing failed the membership test.
        return unavailable_envelope(
            "Epicor's parsed tableset contains no resolvable DB tables, so the "
            "table gate cannot evaluate what this statement reads",
            sql=sql,
        )

    # `allows()` is handed the QUALIFIED name and owns the normalisation
    # (design point 7) — nothing here re-implements strip-schema-and-lowercase.
    missing = [t for t in tables if not scope.allows(t)]
    if missing:
        return table_not_authorized_envelope(
            missing,
            [t for t in tables if t not in missing],
            email=getattr(scope, "email", "") or "",
            sql=sql,
        )
    return None
