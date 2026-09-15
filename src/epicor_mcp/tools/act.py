"""Tool: epicor_act — the one write/method tool (legacy engine).

Absorbs the earlier ``run_method`` + ``workflow`` tools and adds batch semantics (INV-3):
a whole SET (``where`` + ``changes``) or an explicit ``records`` list is
committed in ONE logical call instead of an N+1 per-row loop.

Design invariants honoured here:

* **INV-1** — resolution / field-validation failures return the shared
  ``error_envelope`` (valid columns, candidate targets), never a bare code.
* **INV-3** — set updates/deletes commit as a single multi-row Epicor
  ``Update`` (one GetRows + one Update); ``records`` batches are looped inside
  one tool call.
* **Write rule** — writes default to **pilot**. ``environment='live'``
  is honoured only when passed explicitly; otherwise a pilot-scoped client is
  built lazily so a live-reads deployment never writes to live by accident.

The heavy multi-step ``ds`` mechanics are reused from ``DatasetHandler``; the
SQL/OData translation from ``_engine``; the resolver + envelope from
``_resolve``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from epicor_mcp.context import get_current_session
from epicor_mcp.epicor_client.dataset_handler import DatasetHandler
from epicor_mcp.tools._engine import odata_to_sql, sql_to_odata
from epicor_mcp.tools._resolve import error_envelope, resolve_fields, resolve_target

if TYPE_CHECKING:
    from mcp.server.lowlevel.server import Server

    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.baq_schema_index import BAQSchemaIndex
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

_WRITE_ACTIONS = {"create", "update", "delete"}

_DESCRIPTION = (
    "Write to Epicor or run a BO method in ONE call. `action` is "
    "'update' | 'create' | 'delete' | '<MethodName>'. `target` is a "
    "service or 'Service/Entity' (same fuzzy resolver as epicor_read). "
    "Act on a SET (`where` selects rows, `changes` applies field=value) or a "
    "batch (`records` list) — no per-row loop. `params` carries named-method "
    "args. Writes default to PILOT; pass environment='live' only when "
    "explicitly told. Returns a per-record {ok, failed} summary; bad "
    "target/field comes back as a correctable envelope."
)


# ---------------------------------------------------------------------------
# Pilot writer (rule: writes go to pilot unless environment=='live')
# ---------------------------------------------------------------------------
# Built lazily and cached module-level so a live-reads deployment does not
# route writes to the live company. On environment=='live' the caller-supplied
# (live) client + dataset_handler are used instead.
_pilot_cache: dict[str, Any] = {}


def _pilot_writer(index: "ServiceIndex") -> tuple["EpicorClient", DatasetHandler]:
    """Return a cached pilot-scoped ``(client, DatasetHandler)`` pair."""
    if "dh" not in _pilot_cache:
        from epicor_mcp.auth.credentials import CredentialManager
        from epicor_mcp.config import Settings
        from epicor_mcp.epicor_client.http_client import EpicorClient

        settings = Settings()
        cm = CredentialManager(settings)
        cm.load()
        client = EpicorClient(
            username=cm.service_username,
            password=cm.service_password,
            company_id=settings.epicor_company_id,
            base_url=cm.get_base_url("pilot"),
        )
        _pilot_cache["client"] = client
        _pilot_cache["dh"] = DatasetHandler(client, index)
    return _pilot_cache["client"], _pilot_cache["dh"]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_read_method(name: str) -> bool:
    """Epicor read accessor: ``Get*`` excluding ``GetNew*``."""
    return name.startswith("Get") and not name.startswith("GetNew")


def _key_of(rec: Any) -> Any:
    """Compact identifier for a record, for the ok/failed summary."""
    if not isinstance(rec, dict):
        return str(rec)[:80]
    src = rec
    if isinstance(rec.get("record_id"), dict):
        src = rec["record_id"]
    ident = {
        k: v
        for k, v in src.items()
        if k.lower().endswith(("num", "id", "code", "seq")) or k == "Company"
    }
    if ident:
        return ident
    return {k: src[k] for k in list(src)[:3]}


def _check_fields(
    index: "ServiceIndex",
    service: str,
    entity: str,
    field_names: list[str],
) -> dict | None:
    """Return an INV-1 envelope if any *field_names* are not real columns."""
    names = [f for f in field_names if f and not f.startswith("_")]
    if not names:
        return None
    res = resolve_fields(index, service, entity, ",".join(names))
    if res["unknown"]:
        return error_envelope(
            "unknown_columns",
            (
                f"Unknown field(s) {res['unknown']} on {service}/{entity}. "
                "Use a real column name from valid.columns."
            ),
            valid={"columns": res["valid_columns"]},
            retry_with={"suggestions": res["suggestions"]} if res["suggestions"] else None,
        )
    return None


async def _set_write(
    client: "EpicorClient",
    service: str,
    entity: str,
    where: str,
    changes: dict,
    api_key: str,
    mode: str,
) -> dict:
    """Update/delete a whole SET as a single multi-row Epicor Update (INV-3).

    Steps: one ``GetRows`` (with a translated whereClause) → mark every returned
    row ``RowMod = mode`` (and apply *changes* for updates) → one ``Update``.
    ``mode`` is ``"U"`` (update) or ``"D"`` (delete). Never touches direct
    OData, so heavy services route safely.
    """
    where_sql = odata_to_sql(sql_to_odata(where)) if where else ""
    getrows_body: dict[str, Any] = {
        f"whereClause{entity}": where_sql,
        "pageSize": 0,          # 0 => all matching rows (Epicor GetRows)
        "absolutePage": 1,
    }
    resp = await client.post(f"{service}/GetRows", api_key, json_body=getrows_body)
    ds = DatasetHandler.extract_ds(resp)
    rows = ds.get(entity, []) if isinstance(ds, dict) else []
    if not rows:
        return {"ok": [], "failed": [], "matched": 0}

    for row in rows:
        row["RowMod"] = mode
        if mode == "U":
            for field, value in changes.items():
                row[field] = value

    try:
        await client.post(f"{service}/Update", api_key, json_body={"ds": ds})
    except Exception as exc:  # whole batch failed together
        return {
            "ok": [],
            "failed": [{"key": _key_of(r), "error": str(exc)} for r in rows],
            "matched": len(rows),
        }
    return {"ok": [{"key": _key_of(r)} for r in rows], "failed": [], "matched": len(rows)}


def register(
    server: "Server",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    client: "EpicorClient",
    baq_index: "BAQSchemaIndex | None" = None,
    dataset_handler: "DatasetHandler | None" = None,
) -> None:
    """Bind the ``epicor_act`` tool to *server*."""

    @server.tool(structured_output=False, description=_DESCRIPTION)
    async def epicor_act(
        action: str,
        target: str,
        where: str = "",
        changes: dict | list | None = None,
        records: list | dict | None = None,
        params: dict | None = None,
        environment: str = "",
    ) -> str:
        """Write/act on Epicor in one call. See tool description."""
        try:
            # A model batching a single record as a bare dict (or wrapping a
            # single change set in a list) is the mirror image of the
            # list-for-string landmine — accept both shapes.
            records = [records] if isinstance(records, dict) else (records or [])
            if isinstance(changes, list) and len(changes) > 1:
                # Keeping changes[0] silently DISCARDED the rest — on a WRITE
                # path, where a partial write is the worst possible outcome.
                # A multi-element list is `records` (INV-3 set semantics), so
                # hand back the corrected call instead of guessing.
                return json.dumps(error_envelope(
                    "changes_is_a_batch",
                    f"`changes` holds {len(changes)} record(s), but it applies "
                    "ONE change set to the set selected by `where`. A batch of "
                    "distinct records goes in `records`. Re-call with "
                    "retry_with — nothing was written.",
                    valid={"arguments": {
                        "changes": "one dict of field=value",
                        "records": "list of dicts, one per record",
                    }},
                    retry_with={"action": action, "target": target,
                                "records": changes},
                ))
            changes = (changes[0] if isinstance(changes, list) and changes
                       else (changes if isinstance(changes, dict) else {}))
            params = params or {}
            action_lc = action.strip().lower()
            is_write = action_lc in _WRITE_ACTIONS
            is_named_method = not is_write

            # --- Resolve the target (same resolver as epicor_read) ----------
            res = resolve_target(index, target)
            if res["candidates"]:
                return json.dumps(
                    error_envelope(
                        "ambiguous_target",
                        "Target is ambiguous. Re-call with one candidate 'target'.",
                        candidates=res["candidates"],
                    )
                )
            service = res["service"]
            entity = res["entity_set"]
            if not service:
                return json.dumps(
                    error_envelope(
                        "unresolved_target",
                        (
                            f"Could not resolve target {target!r} to an Epicor "
                            "service. Pass 'Service/Entity' or a known business term."
                        ),
                    )
                )

            # --- RBAC: service + write access -------------------------------
            session = get_current_session()
            allowed, msg = rbac.check_access(session.user_id, service)
            if not allowed:
                return json.dumps(error_envelope("access_denied", msg))

            method_is_write = is_write or not _is_read_method(action)
            if method_is_write:
                w_allowed, w_msg = rbac.check_write_access(session.user_id, service)
                if not w_allowed:
                    return json.dumps(error_envelope("write_denied", w_msg))
                api_key = (
                    rbac._user_map.get_write_key()
                    or rbac.check_service_access(session.user_id, service).api_key
                    or ""
                )
            else:
                api_key = rbac.check_service_access(session.user_id, service).api_key or ""

            # --- Pick the writer environment (default pilot) ----------------
            env = environment.strip().lower()
            if method_is_write:
                if env == "live":
                    wclient, wdh = client, (dataset_handler or DatasetHandler(client, index))
                else:
                    try:
                        wclient, wdh = _pilot_writer(index)
                    except Exception:
                        logger.exception("epicor_act: pilot writer init failed")
                        return json.dumps(
                            error_envelope(
                                "pilot_unavailable",
                                (
                                    "Could not initialise the pilot Epicor client; "
                                    "write aborted to avoid touching live. Retry, or "
                                    "pass environment='live' only if truly intended."
                                ),
                            )
                        )
            else:
                wclient, wdh = client, (dataset_handler or DatasetHandler(client, index))

            # ================================================================
            # NAMED BO METHOD (e.g. "SubmitForApproval", "ChangeVendorID")
            # ================================================================
            if is_named_method:
                method = action.strip()
                known = index.get_methods(service) or []
                if known:
                    valid_names = {m.get("method_name", "") for m in known}
                    if method not in valid_names:
                        return json.dumps(
                            error_envelope(
                                "unknown_method",
                                (
                                    f"Method {method!r} not found on {service}. "
                                    "Use a real method from valid.methods."
                                ),
                                valid={"methods": sorted(n for n in valid_names if n)[:60]},
                            )
                        )
                ok: list = []
                failed: list = []
                # One call, or one call per record (still a single tool call).
                calls = records if records else [None]
                for rec in calls:
                    body = dict(params)
                    if isinstance(rec, dict):
                        body.update(rec)
                    try:
                        await wclient.post(f"{service}/{method}", api_key, json_body=body)
                        ok.append({"key": _key_of(rec) if rec is not None else "call"})
                    except Exception as exc:
                        failed.append(
                            {"key": _key_of(rec) if rec is not None else "call", "error": str(exc)}
                        )
                return json.dumps(
                    {"ok": ok, "failed": failed, "action": method, "service": service}
                )

            # ================================================================
            # CREATE
            # ================================================================
            if action_lc == "create":
                recs = records if records else ([changes] if changes else [])
                if not recs:
                    return json.dumps(
                        error_envelope(
                            "no_records",
                            "Provide 'changes' (one record) or a 'records' list to create.",
                        )
                    )
                allf: set[str] = set()
                for r in recs:
                    if isinstance(r, dict):
                        allf.update(r.keys())
                env_err = _check_fields(index, service, entity, list(allf))
                if env_err:
                    return json.dumps(env_err)

                ok, failed = [], []
                for r in recs:
                    try:
                        result = await wdh.create_record(
                            base_url="", service=service, entity=entity,
                            api_key=api_key, changes=r,
                        )
                        ok.append(
                            {
                                "key": _key_of(r),
                                "result": wdh.trim_dataset(result, entity)
                                if isinstance(result, dict)
                                else result,
                            }
                        )
                    except Exception as exc:
                        failed.append({"key": _key_of(r), "error": str(exc)})
                return json.dumps(
                    {"ok": ok, "failed": failed, "action": "create",
                     "service": service, "entity": entity}
                )

            # ================================================================
            # UPDATE
            # ================================================================
            if action_lc == "update":
                if records:
                    # Normalise each record into (record_id, per-record changes).
                    norm: list[tuple[dict, dict]] = []
                    for r in records:
                        if isinstance(r, dict) and ("record_id" in r or "changes" in r):
                            rid = r.get("record_id") or {}
                            ch = r.get("changes") or changes
                        else:
                            rid = r if isinstance(r, dict) else {}
                            ch = changes
                        norm.append((rid, ch))
                    allf = set()
                    for _rid, ch in norm:
                        allf.update(ch.keys())
                    env_err = _check_fields(index, service, entity, list(allf))
                    if env_err:
                        return json.dumps(env_err)

                    ok, failed = [], []
                    for rid, ch in norm:
                        if not rid:
                            failed.append(
                                {"key": _key_of(ch),
                                 "error": "missing 'record_id' (primary key) for update"}
                            )
                            continue
                        try:
                            await wdh.update_record(
                                base_url="", service=service, api_key=api_key,
                                record_id=rid, entity=entity, changes=ch,
                            )
                            ok.append({"key": _key_of(rid)})
                        except Exception as exc:
                            failed.append({"key": _key_of(rid), "error": str(exc)})
                    return json.dumps(
                        {"ok": ok, "failed": failed, "action": "update",
                         "service": service, "entity": entity}
                    )

                # Set update via where + changes (single multi-row Update).
                if not where:
                    return json.dumps(
                        error_envelope(
                            "no_selection",
                            "Provide 'where' to select a set, or a 'records' list of "
                            "{record_id, changes}.",
                        )
                    )
                if not changes:
                    return json.dumps(
                        error_envelope("no_changes", "Provide 'changes' for a set update.")
                    )
                env_err = _check_fields(index, service, entity, list(changes.keys()))
                if env_err:
                    return json.dumps(env_err)
                summary = await _set_write(
                    wclient, service, entity, where, changes, api_key, "U"
                )
                summary.update({"action": "update", "service": service, "entity": entity})
                return json.dumps(summary)

            # ================================================================
            # DELETE
            # ================================================================
            if action_lc == "delete":
                if records:
                    ok, failed = [], []
                    for r in records:
                        rid = r.get("record_id") if isinstance(r, dict) and "record_id" in r else r
                        if not isinstance(rid, dict) or not rid:
                            failed.append(
                                {"key": _key_of(r),
                                 "error": "each delete record must be a primary-key dict"}
                            )
                            continue
                        try:
                            await wdh.delete_record(
                                base_url="", service=service, api_key=api_key,
                                record_id=rid, entity=entity,
                            )
                            ok.append({"key": _key_of(rid)})
                        except Exception as exc:
                            failed.append({"key": _key_of(rid), "error": str(exc)})
                    return json.dumps(
                        {"ok": ok, "failed": failed, "action": "delete",
                         "service": service, "entity": entity}
                    )

                if not where:
                    return json.dumps(
                        error_envelope(
                            "no_selection",
                            "Provide 'where' to select a set, or a 'records' list of "
                            "primary-key dicts.",
                        )
                    )
                summary = await _set_write(
                    wclient, service, entity, where, {}, api_key, "D"
                )
                summary.update({"action": "delete", "service": service, "entity": entity})
                return json.dumps(summary)

            # Unreachable: action_lc is either a write action or a named method.
            return json.dumps(
                error_envelope(
                    "unknown_action",
                    f"Unsupported action {action!r}.",
                    valid={"actions": ["create", "update", "delete", "<MethodName>"]},
                )
            )

        except Exception:
            logger.exception("epicor_act failed")
            return json.dumps(
                error_envelope(
                    "act_failed",
                    f"epicor_act({action!r}, {target!r}) failed. "
                    "Verify target, fields, and record ids.",
                )
            )
