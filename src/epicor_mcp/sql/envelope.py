"""INV-1 envelopes for the SQL surface (error-envelope contract).

One shape for every model-fixable failure, so the caller never has to parse
prose:

```json
{"error": "sql_run_error",
 "message": "...",                     # what broke, in the caller's terms
 "evidence": "...",                    # the measurement the rule rests on
 "valid": {...},                       # the CORRECT names / forms
 "retry_with": {...},                  # a runnable next call, not a template
 "detail": {...},                      # status, Epicor's own message, GUIDs
 "terminal": false}
```

Two rules keep a failure actionable:

* **Name the side that broke.** An ``unknown_columns`` error can involve both
  ``where`` and ``fields``. Identify the failing argument so the caller can
  preserve a valid filter while repairing invalid projection columns.
* **``retry_with`` is runnable, not a template.** A recovery the caller has to
  fill in is a second failed hop.
"""

from __future__ import annotations

from typing import Any

__all__ = ["error_envelope", "is_envelope"]


def error_envelope(
    error: str,
    message: str,
    *,
    evidence: str = "",
    valid: dict[str, Any] | None = None,
    retry_with: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    terminal: bool = False,
) -> dict[str, Any]:
    """Build the INV-1 envelope. ``error`` is a stable machine-readable slug."""
    env: dict[str, Any] = {
        "success": False,
        "error": error,
        "message": message,
        "terminal": terminal,
    }
    if evidence:
        env["evidence"] = evidence
    if valid:
        env["valid"] = valid
    if retry_with:
        env["retry_with"] = retry_with
    if detail:
        env["detail"] = detail
    return env


def is_envelope(obj: Any) -> bool:
    """True when *obj* is one of our error envelopes."""
    return isinstance(obj, dict) and obj.get("success") is False and "error" in obj
