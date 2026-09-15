'``next_step`` — the one in-band sentence that says what to DO with a response.'

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["next_step_for", "annotate_next_step"]

# The sanctioned de-duplication form, quoted here because the grain note says
# "add `distinct`" and the transpiler REFUSES `distinct` + `top` in one select.
# Observed behavior: the server's own repairs came back `select top N distinct …` and
# were refused as `sql_distinct_with_top` — the note told the model to
# de-duplicate and our own gate forbade the obvious way to do it. Naming the
# form that works is the difference between an instruction and a dead end.
_DEDUP = (
    "To de-duplicate, wrap it: `select top N [t].[C] as [C] from (select distinct "
    "[T].[C] as [C] from Erp.T as [T]) as [t]` — `distinct` and `top` in the SAME "
    "select is refused."
)

_DUP_RULES = {"duplicate_projection", "duplicate_rows_returned"}


def _is_warn(note: Mapping[str, Any]) -> bool:
    return str(note.get("severity") or "").lower() == "warn"


def next_step_for(result: Mapping[str, Any]) -> str:
    """The instruction for *result*, or ``""`` when the answer is ready to report."""
    if result.get("success") is False:
        # A refusal already IS an instruction: `message` says what broke and
        # `retry_with` carries the recovery. Restating it here would be a second
        # owner for the same claim, and the measured failure class is the
        # response that LOOKS like an answer — not the one that says "refused".
        return ""

    diagnosis = result.get("diagnosis")
    if isinstance(diagnosis, Mapping):
        if diagnosis.get("likely_mistake"):
            retry = diagnosis.get("retry_with")
            if isinstance(retry, Mapping) and retry.get("sql"):
                out = (
                    "RE-RUN, DO NOT REPORT. `diagnosis.likely_mistake` is true: the SQL is "
                    "probably wrong, not the data empty. `diagnosis.retry_with.sql` is a "
                    "RUNNABLE statement — call epicor_query with it verbatim as your "
                    "next action."
                )
                # `_retry_with` repairs one finding only. Other dead or untested
                # predicates and joins can still leave the query empty, so label
                # partial repairs honestly instead of promising a complete fix.
                killers = diagnosis.get("killing_predicates") or []
                untested = diagnosis.get("untested_predicates") or []
                if len(killers) > 1 or untested or retry.get("scope"):
                    why = []
                    if len(killers) > 1:
                        why.append(
                            f"{len(killers)} predicates match nothing and this patches "
                            "the ONE named in `changed`"
                        )
                    if untested:
                        why.append(f"{len(untested)} predicate(s) were never tested")
                    if retry.get("scope"):
                        why.append("the JOIN conditions were never tested")
                    out += (
                        " It is a PARTIAL fix, not a recovery: "
                        + "; ".join(why)
                        + ". If it still returns 0 rows, fix the rest from "
                        "`diagnosis.message` — do not report 0 rows."
                    )
                else:
                    out += (
                        " It corrects the ONE predicate that was killing the result."
                    )
                return out
            return (
                "RE-RUN, DO NOT REPORT. `diagnosis.likely_mistake` is true: the SQL is "
                "probably wrong, not the data empty. Correct the predicate named in "
                "`diagnosis.message` and call epicor_query again."
            )
        return (
            "REPORT THIS AS THE ANSWER. 0 rows, and `diagnosis` found no SQL mistake — "
            "a value that does not exist is a real result. Do not re-run the same "
            "question a different way."
        )

    warns = [n for n in (result.get("notes") or []) if isinstance(n, Mapping) and _is_warn(n)]
    grain = [n for n in warns if n.get("source") in ("grain", "lint")]
    if grain:
        out = (
            "DO NOT REPORT THESE ROWS OR TOTALS YET. A grain warning in `notes` means the "
            "result is not the one the question asks for — rows repeated, or a sum "
            "several times too large, with no error. Fix the SQL as that note says and "
            "call epicor_query again."
        )
        if any(n.get("rule") in _DUP_RULES for n in grain):
            out += " " + _DEDUP
        if result.get("grain_checks"):
            out += " `grain_checks[0].sql` is runnable and measures the damage."
        return out
    return ""


def annotate_next_step(result: dict[str, Any]) -> dict[str, Any]:
    """Fill (or drop) ``result['next_step']`` in place. Never raises."""
    try:
        text = next_step_for(result)
    except Exception:  # noqa: BLE001 - a routing hint must never break a response
        text = ""
    if text:
        result["next_step"] = text
    else:
        result.pop("next_step", None)
    return result
