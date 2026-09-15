"""Save a successfully executed SQL statement as a permission-gated AUTO- BAQ.

The SQL runtime executes first, then calls this writer with ``sql_executed``.
The writer also checks ``SaveRight`` so direct callers cannot bypass permission.
Only ``AUTO-`` identifiers can be created, replaced or deleted.

REPLACEMENT ORDER
-----------------
Epicor has no atomic definition replacement. The writer first probes existence,
parses the proposed definition, and rejects unresolved fields or a sanitization
collision. Only then may it delete an existing definition and update the new
one. A designer-tableset snapshot supports restoration if the update fails.
``replaced_existing`` reflects the existence probe, not whether deletion threw.

Sanitization can map different input names to one identifier. A collision is
refused when the input name changed; deliberately reusing an unchanged name
preserves the overwrite contract. A failed pre-delete is recorded and the
update is still attempted because deletion visibility can lag across nodes.

VERIFICATION AND ORDERING
-------------------------
The saved BAQ executes through ``BaqSvc/{id}/Data``, a different runtime from the
ad-hoc ``DynamicQuerySvc/Execute`` path, so a one-row verification run is still
required. Saving and verification must both succeed before success is reported.
Rows from the earlier ad-hoc execution survive any save failure.

ORDER BY is never stripped to make verification succeed. With TOP, removing
an aggregate sort changes which rows are selected. If the saved runtime rejects
the order, keep the BAQ, report ``verified=false`` with Epicor's explanation, and
state the supported qualified-column ordering constraint. The regression suite
is ``tests/test_baq_save.py``.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Awaitable, Callable, Mapping

from epicor_mcp.baq_ops.gate import SaveRight
from epicor_mcp.tools._baq_helpers import (
    _BAQ_NAME_RE,
    _PARSE_DS_TEMPLATE,
    _VERSION_SUFFIX_RE,
    _normalize_sql,
    find_unresolved_parsed_fields,
)

logger = logging.getLogger(__name__)

__all__ = [
    "sql_for_save",
    "sanitize_baq_name",
    "save_query_as_baq",
    "delete_saved_baq",
    "AUTO_PREFIX",
    "MAX_BAQ_NAME",
]

AUTO_PREFIX = "AUTO-"
MAX_BAQ_NAME = 25

GETBYID_PATH = "Ice.BO.DynamicQuerySvc/GetByID"
#: The DESIGNER's own read. `GETBYID_PATH` above is the RUNTIME service and
#: returns a `DynamicQuery` tableset; `UPDATE_PATH` takes a `DynamicQueryDesigner`
#: one. Posting the runtime shape to the designer's Update — which is what the
#: restore did — is a call that cannot succeed, so the recovery for the single
#: worst failure mode (previous definition deleted, new one rejected) was inert.
DESIGNER_GETBYID_PATH = "Ice.BO.BAQDesignerSvc/GetByID"
PARSE_PATH = "Ice.BO.BAQDesignerSvc/ParseFromSQL"
DELETE_PATH = "Ice.BO.BAQDesignerSvc/DeleteByID"
UPDATE_PATH = "Ice.BO.BAQDesignerSvc/Update"

#: Any character outside the set Epicor accepts in a query id. The writer
#: replaces it and ANNOUNCES the replacement rather than failing, because a
#: cosmetic name is rarely worth a failed turn — see :func:`sanitize_baq_name`.
_ILLEGAL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


# --------------------------------------------------------------------------- #
# what gets persisted
# --------------------------------------------------------------------------- #


def sql_for_save(run_result: Mapping[str, Any], raw_sql: str) -> tuple[str, dict[str, Any]]:
    """Return ``(text to persist, row-bound disclosure)``.

    **The TRANSPILED text is what is saved, not the caller's raw string.** The
    pipe runs ``sql_to_run = result.sql or sql`` and that is the only text
    ``ParseFromSQL`` and ``Execute`` ever saw; it is also what the success dict
    reports as ``sql_executed``. Persisting the raw string would persist text
    Epicor has never parsed — the shape that saves and then 400s on every run.

    THE HONEST WRINKLE, disclosed rather than hidden: with no caller ``top`` the
    transpiler injects one sized to ``page_size``, so the same statement saved at
    ``page_size=200`` and at ``page_size=1000`` persists ``top 200`` vs
    ``top 1000``. The saved cap becomes an artefact of a paging argument, and the
    caller has to be told — in the note here AND in the response summary, because
    a nested ``note`` is the field most likely to go unread.
    """
    text = _normalize_sql(str(run_result.get("sql_executed") or raw_sql or ""))
    bound = ((run_result.get("assumptions") or {}).get("row_bound")) or {}
    value = bound.get("value")
    source = str(bound.get("source") or "")
    if not value:
        # `page_size_only` (a bare `select distinct`, a set-operation branch
        # bound): there is no `top` in the persisted text to disclose.
        return text, {}
    if source == "caller":
        return text, {"top": int(value), "source": "caller"}
    # `clamped` is NOT `injected`: the caller DID write a `top`, it was just
    # larger than the governor's ceiling. Reporting it as "you did not write a
    # `top`" is a false statement about the caller's own SQL, and it is made
    # twice — here and in the summary that lifts this note. The two cases need
    # different remedies too: an injected cap is removed by writing any `top`,
    # a clamped one only by writing a SMALLER one.
    if source == "clamped":
        return text, {
            "top": int(value),
            "source": "clamped",
            "note": (
                f"Your `top` was above this server's ceiling, so it was reduced to "
                f"{int(value)}. The SAVED BAQ carries the reduced cap permanently. "
                f"To save a different cap, re-send with a `top` at or below "
                f"{int(value)} and the same save_as."
            ),
        }
    return text, {
        "top": int(value),
        "source": "injected_by_server",
        "note": (
            f"You did not write a `top`, so the server added one sized to this page. "
            f"The SAVED BAQ carries it permanently and returns at most {int(value)} "
            f"rows. To save a different cap, re-send with an explicit "
            f"`select top <N>` and the same save_as."
        ),
    }


def sanitize_baq_name(name: str) -> tuple[str, dict[str, Any]]:
    """``"Open POs / Example Site_v2"`` -> ``("Open-POs---Example Site", {...})``.

    A HARD ERROR for a name that is too long or carries an illegal character
    would cost a turn. This writer sanitises and announces
    instead: the name is cosmetic, the SQL is not, and spending a turn on
    punctuation is the round trip this surface exists to remove. What hard
    errors would really protect against — two different names collapsing onto
    one id and silently destroying the wrong BAQ — is handled by the existence
    probe in :func:`save_query_as_baq`, which is a better fit: it only costs a
    turn when a collision ACTUALLY exists.

    Returns ``(name, changed)`` where ``changed`` is ``{}`` when the name
    survived untouched.

    ``changed["lossy"]`` is the flag the collision guard keys on, and the split
    is load-bearing. Two of these transforms are pure CONVENTION — stripping a
    leading ``AUTO-`` the server itself prepends, and stripping the ``_v2`` a
    caller bumped on a retry — and they map a name onto the id the caller
    already meant. The rest (illegal characters collapsed to ``-``, truncation
    at ``MAX_BAQ_NAME``, the ``"query"`` fallback) are LOSSY: two different
    names can land on one id, which is the case worth a turn.

    Keying the guard on "changed at all" made ``save_as="AUTO-FOO"`` — the exact
    id this server hands back in ``saved.baq_id`` and in ``run_it_with`` —
    refuse with a fabricated ``name_collision`` saying that id is "ALREADY a
    different saved BAQ". It was the caller's OWN BAQ, and re-saving under the
    returned id is the advertised replace-in-place path.
    """
    submitted = str(name or "")
    out = submitted.strip()
    # A caller that already knows the convention should not get `AUTO-AUTO-x`.
    if out.upper().startswith(AUTO_PREFIX):
        out = out[len(AUTO_PREFIX):]
    # Callers reflexively bump `_v2` on a retry, and re-using the base
    # name is what makes replace-in-place work instead of piling up orphans.
    out = _VERSION_SUFFIX_RE.sub("", out)
    # Everything above is convention. Everything below can collapse two distinct
    # names onto one id, so it is measured separately.
    conventional = out
    out = _ILLEGAL_NAME_CHARS.sub("-", out)
    out = out[:MAX_BAQ_NAME]
    if not out or not _BAQ_NAME_RE.match(out):
        out = "query"
    changed: dict[str, Any] = {}
    if out != submitted:
        changed = {"submitted": submitted, "saved_as": out, "lossy": out != conventional}
    return out, changed


# --------------------------------------------------------------------------- #
# the writer
# --------------------------------------------------------------------------- #


def _saved_block(**kw: Any) -> dict[str, Any]:
    """The ``saved`` channel. Always carries ``attempted`` and ``saved``."""
    out: dict[str, Any] = {"attempted": True, "saved": False}
    out.update(kw)
    return out


def _ud_remediation(unresolved: list[dict[str, Any]]) -> str:
    """Explain a failed user-defined column without a BAQ-schema index.
    
    An OData projection can expose ``_c`` fields through a base table while BAQ
    SQL requires the physical ``<Table>_UD`` extension. If the save parse reports
    such a field unresolved, identify the affected tables and explain the mirror
    join explicitly. Do not assume that an OData field is SQL-selectable.
    """
    tables = sorted({str(u.get("table") or "") for u in unresolved if u.get("table")})
    hint = ", ".join(tables) or "<Table>"
    return (
        "A field ending in _c is a user-defined column. In BAQ SQL it cannot be "
        f"selected off the base table ({hint}) — it lives in that table's _UD "
        "extension table. Add `LEFT OUTER JOIN <Table>_UD as [<T>_UD] ON "
        "<T>.SysRowID = <T>_UD.ForeignSysRowID` (LEFT OUTER so rows without a _UD "
        "record survive), then select `[<T>_UD].[<field>_c]`. This is BAQ-only: "
        "the same statement reads _c off the base table just fine when you run it "
        "without save_as."
    )


def _fix_hint(query_id: str, save_as: str) -> str:
    """The legacy ``_retry_hint`` in substance, minus the tool this server does not have.

    This server registers no BAQ delete, so pointing at ``epicor_baq_delete`` would name a
    tool the model cannot call — a guaranteed dead turn, and against the server
    instructions' own rule.
    """
    return (
        f"BAQ '{query_id}' was saved but does not run. Fix the SQL and re-send with "
        f"the SAME save_as ('{save_as}') — it overwrites in place, so you will not "
        "pile up duplicates. Do NOT invent a new name like "
        f"'{save_as}_v2'. If you are abandoning it, it can be removed in Epicor's "
        "BAQ Designer."
    )


def _cleanup_hint(query_id: str) -> str:
    """The after-the-fact half of *"only save when the user asked"*.

    The parameter default and its description are what stop an unrequested save;
    this is what makes an unrequested save VISIBLE once it has happened. It names
    no tool this server does not register — there is no delete on the public surface,
    so it points at Epicor's BAQ Designer.
    """
    return (
        f"This query now exists in LIVE Epicor as BAQ '{query_id}'. Tell the user that "
        "id — it is how they re-run it, here or in Epicor. Re-saving under the same "
        "save_as OVERWRITES it in place, so iterate on the name rather than inventing a "
        "new one. Save only what the user asked to keep: if this was a one-off you "
        "should not have saved it, say so and tell them it can be removed in Epicor's "
        "BAQ Designer."
    )


def _parsed_ds(response: Mapping[str, Any]) -> dict[str, Any] | None:
    for candidate in (
        (response.get("parameters") or {}).get("ds"),
        response.get("ds"),
        response.get("returnObj"),
    ):
        if isinstance(candidate, Mapping) and "DynamicQueryDesigner" in candidate:
            return dict(candidate)
    return None


def _detail(exc: Exception) -> dict[str, Any]:
    return {
        "status": getattr(exc, "status_code", None),
        "message": str(getattr(exc, "message", exc))[:1200],
    }


async def _probe_existing(
    client: Any, api_key: str, base_url: str, query_id: str
) -> dict[str, Any] | None:
    """The existence probe. Returns the previous definition, or ``None``.

    A failure of ANY kind reads as *"not there"*, which is the safe direction:
    the only things this answer gates are (a) whether a ``DeleteByID`` is issued
    at all and (b) whether ``replaced_existing`` is claimed. Guessing "absent"
    when something is present costs a delete that Epicor would have accepted
    anyway; guessing "present" when nothing is fabricates a claim about
    destroying the caller's data, which is the defect being fixed.
    """
    try:
        response = await client.post(
            f"{base_url}/{GETBYID_PATH}", api_key, json_body={"queryID": query_id}
        )
    except Exception as exc:  # noqa: BLE001 - 404 is the expected answer here
        logger.debug("existence probe: %s is absent (%s)", query_id, exc)
        return None
    obj = response.get("returnObj") if isinstance(response, Mapping) else None
    if not isinstance(obj, Mapping):
        return None
    rows = obj.get("DynamicQuery") or obj.get("DynamicQueryDesigner") or []
    if not rows:
        return None
    return dict(obj)


async def _probe_designer_definition(
    client: Any, api_key: str, base_url: str, query_id: str
) -> dict[str, Any] | None:
    """The previous definition in the shape ``BAQDesignerSvc/Update`` accepts.

    Fetched only when an overwrite is about to happen, and only to have
    something to put back if the ``Update`` is rejected after the delete.
    ``None`` means no restore is possible — which is REPORTED, never presented
    as a restore that silently did nothing.
    """
    try:
        response = await client.post(
            f"{base_url}/{DESIGNER_GETBYID_PATH}", api_key,
            json_body={"queryID": query_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("designer probe: %s unreadable (%s)", query_id, exc)
        return None
    obj = response.get("returnObj") if isinstance(response, Mapping) else None
    if not isinstance(obj, Mapping) or not obj.get("DynamicQueryDesigner"):
        return None
    return dict(obj)


async def save_query_as_baq(
    *,
    client: Any,
    api_key: str,
    base_url: str,
    right: SaveRight,
    query_id: str,
    description: str,
    sql: str,
    name_changed: bool = False,
    name_sanitized: dict[str, Any] | None = None,
    row_bound: dict[str, Any] | None = None,
    verify: Callable[[str], Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Persist *sql* as BAQ *query_id*. Returns the ``saved`` response channel.

    *query_id* is the FULL id including the ``AUTO-`` prefix — the caller has
    already run :func:`sanitize_baq_name` and needs the sanitisation outcome for
    the collision rule, which is why *name_changed* / *name_sanitized* come in
    rather than being recomputed here.

    *verify* is injected rather than imported so the verification run charges the
    caller's governor and session budget (this module holds neither). It is
    called with the saved id and must return a result envelope; a
    ``query_budget_exhausted`` refusal is reported as *not attempted*, never as
    doubt about the BAQ.

    **Never raises.** Every failure returns a ``saved`` block naming what
    happened and what to do about it, so the caller can attach it to a result
    whose rows have already been paid for.
    """
    # 0. DEFENCE IN DEPTH. The caller gates before the statement runs; the legacy tools gate
    #    inside the writer (`_baq_helpers.py`, `baq_delete.py`) so that
    #    every write path is self-gating regardless of who calls it. Both.
    if not right.allowed:
        return _saved_block(
            reason="not_authorized",
            message=(
                "This account does not have BAQ write access, so nothing was written. "
                "See the baq_save_not_authorized envelope for the right and how to get it."
            ),
            detail={
                "user": right.user_id,
                "access_level": right.access_level,
                "can_write_baqs": right.can_write_baqs,
                "right_source": right.right_source,
            },
        )

    author = right.epicor_username or "unknown"

    # 1. EXISTENCE PROBE — three jobs in one call: evidence for
    #    `replaced_existing`, the previous tableset for the restore, and the
    #    sanitisation-collision check.
    previous = await _probe_existing(client, api_key, base_url, query_id)
    exists = previous is not None

    if exists and name_changed:
        existing_desc = ""
        rows = (previous or {}).get("DynamicQuery") or (previous or {}).get(
            "DynamicQueryDesigner"
        ) or []
        if rows and isinstance(rows[0], Mapping):
            existing_desc = str(rows[0].get("Description") or "")
        submitted = (name_sanitized or {}).get("submitted", "")
        return _saved_block(
            reason="name_collision",
            message=(
                f"The name you passed ({submitted!r}) had to be changed to fit Epicor's "
                f"BAQ id rules, and the result — '{query_id}' — is ALREADY a different "
                f"saved BAQ{f' ({existing_desc})' if existing_desc else ''}. Nothing was "
                "deleted and nothing was saved. Re-send with an exact id: letters, "
                "digits, '-' and '_' only, at most "
                f"{MAX_BAQ_NAME} characters — either the existing id if you meant to "
                "overwrite it, or a distinct one if you did not."
            ),
            collided_with=query_id,
            name_sanitized=dict(name_sanitized or {}),
        )

    # 2. ParseFromSQL. Nothing is persisted by this call; it compiles the
    #    DisplayPhrase into a designer tableset in memory.
    ds = copy.deepcopy(_PARSE_DS_TEMPLATE)
    ds["DynamicQueryDesigner"][0]["DisplayPhrase"] = sql
    ds["DynamicQueryDesigner"][0]["AuthorID"] = author
    try:
        parse_response = await client.post(
            f"{base_url}/{PARSE_PATH}", api_key, json_body={"ds": ds}
        )
    except Exception as exc:  # noqa: BLE001
        return _saved_block(
            reason="parse_failed",
            message=(
                "Epicor's BAQ parser rejected the statement, so nothing was saved and any "
                "existing BAQ of this name is untouched. The statement itself RAN — the "
                "rows above are real; it is the SAVED form Epicor will not compile."
            ),
            detail=_detail(exc),
        )
    parsed = _parsed_ds(parse_response)
    if parsed is None:
        return _saved_block(
            reason="parse_unreadable",
            message=(
                "ParseFromSQL returned a tableset this server does not recognise "
                "(no DynamicQueryDesigner), so nothing was saved and any existing BAQ of "
                "this name is untouched."
            ),
            detail={"response_keys": sorted(parse_response.keys())[:20]},
        )

    # 3. THE UNRESOLVED-FIELD CHECK — mandatory, and it must sit here, ahead of
    #    the delete. Epicor binds every real column to a DataType; a blank one on
    #    a DB table means ParseFromSQL could not resolve the field, and saving
    #    that definition guarantees a blank `400 BAQ execution failed with error:`
    #    the moment anyone runs it. This is also the ONLY check that catches the
    #    BAQ-SQL-only `_c` trap, which E14 structurally cannot see.
    unresolved = find_unresolved_parsed_fields(parsed)
    if unresolved:
        block = _saved_block(
            reason="unresolved_fields",
            message=(
                "Epicor parsed the SQL but could not bind these fields to real columns, so "
                "the BAQ would save and then fail at run time with a blank '400 BAQ "
                "execution failed with error:'. NOTHING was saved and any existing BAQ of "
                "this name is untouched. The rows above are still a real answer — this is "
                "about the SAVED form only."
            ),
            unresolved_fields=unresolved,
        )
        if any(str(u.get("field") or "").endswith("_c") for u in unresolved):
            block["fix"] = _ud_remediation(unresolved)
        return block

    # 4. DeleteByID — ONLY when the probe found something. There is no atomic
    #    replace, so this is the point of no return and everything that could
    #    refuse has now refused.
    delete_failed: dict[str, Any] | None = None
    restore_ds: dict[str, Any] | None = None
    if exists:
        # Snapshot in the DESIGNER's shape before destroying anything — this is
        # the only payload `UPDATE_PATH` can actually accept as a restore.
        restore_ds = await _probe_designer_definition(
            client, api_key, base_url, query_id
        )
        try:
            await client.post(
                f"{base_url}/{DELETE_PATH}", api_key, json_body={"queryID": query_id}
            )
        except Exception as exc:  # noqa: BLE001
            # Definitions may propagate asynchronously across SaaS nodes,
            # so a DeleteByID issued moments after a create can
            # land on a node that has not seen it and answers 404. Treating
            # that transient as fatal means the caller cannot overwrite their
            # OWN BAQ, which is the whole of replace-in-place. `Update` is an
            # upsert and handles the row already being gone; if the row really
            # is still there and undeletable, `Update` fails and the handler
            # below reports THAT with the previous definition intact.
            #
            # This is not swallowed: logging at debug and telling the caller
            # nothing would make a partial replace look identical to a clean
            # one. It rides back on the saved block.
            delete_failed = _detail(exc)
            logger.info(
                "pre-delete of %s failed (%s); proceeding to Update anyway",
                query_id, delete_failed.get("status"),
            )

    # 5. Stamp the id onto the header AND onto every row of every array — a child
    #    row carrying the parse-time id makes the saved definition inconsistent.
    header = parsed["DynamicQueryDesigner"][0]
    header["QueryID"] = query_id
    header["Description"] = description
    header["AuthorID"] = author
    # DisplayPhrase is stamped EXPLICITLY rather than relied on to come back in
    # the parse echo. Relying on the echo means that if a future Epicor build stops
    # echoing it, a definition is saved with no SQL text in it and the failure is
    # invisible until someone opens the BAQ in the Designer. The text here is the
    # exact string ParseFromSQL just accepted, so this can only ever agree with
    # the tableset below it.
    header["DisplayPhrase"] = sql
    for array in parsed.values():
        if isinstance(array, list):
            for row in array:
                if isinstance(row, dict) and "QueryID" in row:
                    row["QueryID"] = query_id

    try:
        update_response = await client.post(
            f"{base_url}/{UPDATE_PATH}", api_key, json_body={"ds": parsed}
        )
    except Exception as exc:  # noqa: BLE001
        # Without a handler here, an Update failure after the delete would
        # propagate as an exception with the previous definition already gone
        # and no word to the caller about it. Try to put it back, and report
        # BOTH facts either way.
        restored = False
        if exists and restore_ds is not None:
            try:
                await client.post(
                    f"{base_url}/{UPDATE_PATH}", api_key, json_body={"ds": restore_ds}
                )
                restored = True
            except Exception:  # noqa: BLE001 - the restore is itself an Epicor call
                logger.warning(
                    "could not restore the previous definition of %s", query_id,
                    exc_info=True,
                )
        # Three outcomes, not two. "No snapshot to restore from" is its own
        # answer and must not read as "the restore was tried and failed".
        if not exists:
            lost = "No previous definition existed, so nothing was lost."
        elif restored:
            lost = "A previous definition of that id was removed first and has been restored."
        elif restore_ds is None:
            lost = (
                "A previous definition of that id was removed first and could NOT be "
                "read back beforehand, so there was nothing to restore it from. It is gone."
            )
        else:
            lost = (
                "A previous definition of that id was removed first and COULD NOT BE "
                "RESTORED."
            )
        return _saved_block(
            reason="update_failed",
            message=(
                f"Epicor rejected the write of BAQ '{query_id}'. {lost} "
                "The rows above are still a real answer."
            ),
            previous_definition_destroyed=bool(exists),
            previous_definition_restored=restored,
            previous_definition_snapshot=bool(restore_ds) if exists else None,
            detail=_detail(exc),
        )

    saved_ds = _parsed_ds(update_response) or parsed
    block: dict[str, Any] = {
        "attempted": True,
        "saved": True,
        "baq_id": query_id,
        "description": description,
        "author": author,
        "replaced_existing": exists,
        "tables_used": len(saved_ds.get("QueryTableDesigner") or []),
        "fields_selected": len(saved_ds.get("QueryFieldDesigner") or []),
        "run_it_with": {"tool": "epicor_query", "saved_baq": query_id},
        "cleanup_hint": _cleanup_hint(query_id),
    }
    if row_bound:
        block["row_bound"] = dict(row_bound)
    if name_sanitized:
        block["name_sanitized"] = dict(name_sanitized)
    if delete_failed is not None:
        # The save SUCCEEDED via Update's upsert, but the caller is told the
        # replace was not clean — a silently-tolerated delete failure would
        # make a partial replace indistinguishable from a clean one.
        block["pre_delete_failed"] = delete_failed
        block["note"] = (
            f"The existing definition of '{query_id}' could not be deleted first "
            "(Epicor's BAQ definitions propagate across nodes asynchronously, so this "
            "is usually transient); the new definition was written over it instead. "
            "The rows this BAQ returns are the new definition's. If the BAQ looks "
            "stale in the Designer, re-open it."
        )

    # 6. VERIFICATION. Parse-accepts is not run-succeeds, and `BaqSvc/{id}/Data`
    #    is a different runtime from the `DynamicQuerySvc/Execute` that produced
    #    the rows above. The BAQ is KEPT on a failure: fix-in-place under the same
    #    name is the supported recovery, and deleting it would take that away.
    if verify is None:
        block["verified"] = None
        block["verification"] = "not_attempted_no_verifier"
        return block
    try:
        checked = await verify(query_id)
    except Exception as exc:  # noqa: BLE001 - verification must never lose the save
        block["verified"] = None
        block["verification"] = "not_attempted_verifier_error"
        block["detail"] = _detail(exc)
        return block
    if checked.get("success"):
        block["verified"] = True
        return block
    if str(checked.get("error") or "") == "query_budget_exhausted":
        # "Not verified because the budget ran out" must not read as doubt about
        # the BAQ. It is a fact about this session, not about the definition.
        block["verified"] = None
        block["verification"] = "not_attempted_budget_exhausted"
        return block
    block["verified"] = False
    # Carry EPICOR'S OWN message, not the verification envelope's composed prose.
    # `run_saved_baq` is written for the ordinary case of running SOMEBODY ELSE'S
    # saved BAQ, so its failure text ends "This server did not write this BAQ —
    # the definition lives in Epicor". True there; FALSE here, three keys away
    # from a `cleanup_hint` saying this query exists because we just wrote it. A
    # response that both claims and disclaims authorship of the same id makes the
    # model pick, and the wrong pick is "tell the user it is not ours to fix".
    # The raw message is already separate in `detail.message` (saved_run.py).
    checked_detail = checked.get("detail")
    if not isinstance(checked_detail, Mapping):
        checked_detail = {}
    raw_message = str(checked_detail.get("message") or checked.get("message") or "")
    block["detail"] = {
        "error": checked.get("error"),
        "message": raw_message[:1200],
    }
    if checked_detail.get("status") is not None:
        block["detail"]["status"] = checked_detail["status"]
    save_as = query_id[len(AUTO_PREFIX):] if query_id.startswith(AUTO_PREFIX) else query_id
    block["fix"] = _fix_hint(query_id, save_as)
    if exists:
        block["previous_definition_replaced_by_a_baq_that_does_not_run"] = True
    return block


async def delete_saved_baq(
    client: Any, api_key: str, base_url: str, right: SaveRight, query_id: str
) -> dict[str, Any]:
    """Delete an ``AUTO-`` BAQ. **Never registered as a tool.**

    This exists for the live test's teardown, and it carries BOTH legacy
    protections rather than only one: the same
    write gate as the create, AND the ``AUTO-`` prefix check, so it can never be
    repointed at a hand-authored BAQ.

    There is no delete on the public tool surface. An unwanted ``AUTO-`` BAQ is
    removed by a human in Epicor's BAQ Designer, which is what ``cleanup_hint``
    says. If accumulation ever becomes a problem, registering this is a small,
    separate decision — not something to reach for here.
    """
    if not right.allowed:
        return {
            "success": False,
            "error": "baq_delete_not_authorized",
            "message": "Deleting a BAQ needs the same BAQ write access as creating one.",
        }
    query_id = str(query_id or "").strip()
    if not query_id.startswith(AUTO_PREFIX):
        return {
            "success": False,
            "error": "baq_delete_refused",
            "message": (
                f"Only BAQs with the {AUTO_PREFIX} prefix may be deleted here; "
                f"'{query_id}' does not have it. A hand-authored BAQ is removed in "
                "Epicor's BAQ Designer."
            ),
        }
    try:
        await client.post(
            f"{base_url}/{DELETE_PATH}", api_key, json_body={"queryID": query_id}
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": "baq_delete_failed",
            "message": f"Epicor refused the delete of '{query_id}'.",
            "detail": _detail(exc),
        }
    logger.info("deleted BAQ %s (user=%s)", query_id, right.user_id)
    return {"success": True, "deleted": query_id}
