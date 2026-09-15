"""``baq_ops`` — everything in this server that PERSISTS, deliberately outside ``sql/``.

``sql/``'s first invariant is that **nothing in it persists**: the pipe calls
``ParseFromSQL`` (compiles in memory) and ``Execute`` (takes the tableset in
memory), and nothing else. That claim is not documentation — it is a *test*:
``tests/test_query_no_write_methods.py`` greps every ``sql/*.py`` plus
``wedge_server.py`` with docstrings and comments stripped, and asserts the
reachable endpoint set is EXACTLY ``ParseFromSQL`` / ``Execute`` / ``Analyze``.

Saving a query as a BAQ, running a saved BAQ and resolving a dashboard all need
endpoints that check would (correctly) fail on. So they live HERE instead. The
package boundary is what keeps the invariant checkable rather than merely
asserted: a future edit that adds a write call to ``sql/`` still fails the grep,
and this package's own writes are gated, ordered and tested on their own terms
(see ``CLAUDE.md`` in this directory).

Nothing is re-exported on purpose — importers name the module they need
(``baq_ops.gate``, ``baq_ops.save``, ``baq_ops.saved_run``, ``baq_ops.dashboards``)
so a new module can be added without editing this file, and so importing one
never drags the others' dependencies in.
"""

from __future__ import annotations

__all__: list[str] = []
