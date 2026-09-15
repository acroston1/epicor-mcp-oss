"""``epicor_dashboards`` — resolution, list mode, paging honesty, misses.

Drives the REGISTERED tool through ``register_dashboards_tool`` against a fake
``DashBoardSvc``. The fake implements Epicor's ``whereClause`` semantics well
enough that the resolution ladder is exercised end to end rather than mocked
rung by rung — which is the only way the two properties that matter can be
asserted at all:

* **the tool never executes a BAQ.** The fake raises on any ``get``, so
  resolve-only is a STRUCTURAL fact here, not a comment in the source.
* **a full server page is never reported as the answer.** A full page of 50
  attached BAQs or 500 definitions is not proof that nothing else exists.
"""

from __future__ import annotations

import re

import pytest

from epicor_mcp.baq_ops.dashboards import (
    _group_score,
    register_dashboards_tool,
)

BASE = "https://epicor.test/api/v2/odata/DEMO"
KEY = "test-key"


# --------------------------------------------------------------------------- #
# a DashBoardSvc that behaves like the real one
# --------------------------------------------------------------------------- #
def _like_to_regex(pattern: str) -> re.Pattern:
    """T-SQL ``LIKE`` semantics, including ``[%]`` / ``[_]`` bracket escapes.

    Modelling the escapes is the point: without them a test cannot tell an
    escaped wildcard from a live one, which is exactly the bug being pinned.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            close = pattern.find("]", i)
            if close != -1:
                out.append(re.escape(pattern[i + 1:close]))
                i = close + 1
                continue
            out.append(re.escape(ch))
        elif ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out), re.I | re.S)


_PRED = re.compile(r"(\w+)\s+(=|like)\s+'((?:[^']|'')*)'", re.I)


def _matches(row: dict, where: str) -> bool:
    """Evaluate the ``Field = 'x'`` / ``Field like 'x'`` [or ...] clauses we send."""
    if not where.strip():
        return True
    for field, op, raw in _PRED.findall(where):
        value = str(row.get(field) or "")
        literal = raw.replace("''", "'")
        if op.lower() == "=":
            if value.lower() == literal.lower():
                return True
        elif _like_to_regex(literal).fullmatch(value):
            return True
    return False


class FakeDashboardService:
    """Records every call; serves a corpus. Raises on anything unexpected."""

    def __init__(
        self,
        definitions: list[dict] | None = None,
        baqs: dict[str, list[str]] | None = None,
        *,
        error: Exception | None = None,
        corpus_cap: int | None = None,
    ) -> None:
        self.definitions = list(definitions or [])
        self.baqs = dict(baqs or {})
        self.error = error
        #: Truncate every GetList page to this many rows, simulating a server
        #: page smaller than the corpus.
        self.corpus_cap = corpus_cap
        self.calls: list[tuple[str, dict]] = []

    # -- the surface baq_ops.dashboards actually uses ----------------------
    async def call_method(self, base_url, service, method, api_key, params=None):
        key = f"{service}/{method}"
        self.calls.append((key, dict(params or {})))
        assert base_url == BASE, "base_url must be passed explicitly"
        assert api_key == KEY, "api_key must be passed explicitly"
        if self.error is not None:
            raise self.error
        if key == "Ice.BO.DashBoardSvc/GetList":
            where = str(params.get("whereClause") or "")
            page = int(params.get("pageSize") or 10)
            rows = [r for r in self.definitions if _matches(r, where)]
            if self.corpus_cap is not None:
                rows = rows[: self.corpus_cap]
            return {"returnObj": {"DashBdDefList": rows[:page]}}
        if key == "Ice.BO.DashBoardSvc/GetRows":
            where = str(params.get("whereClauseDashBdDef") or "")
            match = _PRED.search(where)
            defn = match.group(3).replace("''", "'") if match else ""
            ids = self.baqs.get(defn, [])
            page = int(params.get("pageSize") or 50)
            return {"returnObj": {"DashBdBAQ": [{"QueryID": q} for q in ids][:page]}}
        raise AssertionError(f"unexpected call_method {key}")

    async def get(self, url, api_key, params=None):  # pragma: no cover - must not run
        raise AssertionError(
            f"epicor_dashboards executed a BAQ ({url}) — it must only RESOLVE"
        )

    def count(self, fragment: str) -> int:
        return sum(1 for k, _ in self.calls if fragment in k)

    def wheres(self) -> list[str]:
        out = []
        for _key, params in self.calls:
            for name, value in params.items():
                if "whereClause" in name and value:
                    out.append(str(value))
        return out


class _MCP:
    def __init__(self) -> None:
        self.tools: dict = {}
        self.descriptions: dict = {}

    def tool(self, *, name, description):
        def deco(fn):
            self.tools[name] = fn
            self.descriptions[name] = description
            return fn
        return deco


def _tool(client, *, query_tool_available=True, max_list=60, max_bytes=700_000):
    mcp = _MCP()
    register_dashboards_tool(
        mcp, client=client, api_key=KEY, base_url=BASE,
        query_tool_available=query_tool_available,
        max_list=max_list, max_bytes=max_bytes,
    )
    return mcp.tools["epicor_dashboards"], mcp.descriptions["epicor_dashboards"]


def _defs(*pairs) -> list[dict]:
    return [{"DefinitionID": i, "Description": d} for i, d in pairs]


# --------------------------------------------------------------------------- #
# 39-40 — the happy path and the GetRows body
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_exact_id_resolves_in_one_call_and_returns_the_baq_ids():
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog")),
        {"OpenBacklog": ["zCustomerAR01", "zCustomerAR02"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="OpenBacklog")

    assert out["mode"] == "dashboard"
    assert out["dashboard"] == "OpenBacklog"
    assert out["description"] == "Open Backlog"
    assert out["baq_ids"] == ["zCustomerAR01", "zCustomerAR02"]
    assert out["baq_ids_complete"] is True
    assert svc.count("GetList") == 1, "an exact id must not walk the ladder"
    assert svc.count("GetRows") == 1


@pytest.mark.asyncio
async def test_the_getrows_body_carries_every_dataset_table():
    """Omit one and Epicor 400s *"Parameter whereClauseX is not found in the
    input object"*; the siblings must be `""`, not `1=0` and not absent."""
    svc = FakeDashboardService(_defs(("OpenBacklog", "Open Backlog")), {"OpenBacklog": ["q1"]})
    tool, _desc = _tool(svc)
    await tool(dashboard="OpenBacklog")

    body = next(p for k, p in svc.calls if k.endswith("GetRows"))
    assert body["whereClauseDashBdDef"] == "DefinitionID = 'OpenBacklog'"
    for sibling in ("whereClauseDashBdBAQ", "whereClauseDashBdChunk",
                    "whereClauseDashBdLike"):
        assert sibling in body, f"{sibling} missing — Epicor 400s"
        assert body[sibling] == ""


# --------------------------------------------------------------------------- #
# 41-44 — the ladder
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_spoken_name_resolves_by_squashed_equality():
    """'Open Backlog dashboard' -> 'openbacklog' == 'openbacklog'. No server-side LIKE on
    the whole phrase can ever match a squashed DefinitionID, which is why the
    client-side pass exists at all."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog"), ("OpenAnalysis", "Open Analysis"),
              ("LateBacklog", "Late Backlog")),
        {"OpenBacklog": ["zAR01"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Open Backlog dashboard")
    assert out["dashboard"] == "OpenBacklog"
    assert out["baq_ids"] == ["zAR01"]


@pytest.mark.asyncio
async def test_the_exact_description_resolves_on_its_own_rung():
    """Rung 2. A user reads the DESCRIPTION off the Epicor menu ("Open Backlog"),
    never the squashed ``DefinitionID`` — so if this rung is dropped the phrase
    falls through to a LIKE and then to scoring, which is three round trips for
    an exact match and, over a big corpus, an ambiguity where there was none."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog"), ("LateBacklog", "Late Backlog")), {"OpenBacklog": ["zAR01"]})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Open Backlog")

    assert out["dashboard"] == "OpenBacklog"
    assert svc.count("GetList") == 2, "id rung, then description rung — no further"
    assert svc.wheres()[1] == "Description = 'Open Backlog'"


@pytest.mark.asyncio
async def test_two_dashboards_sharing_a_description_are_offered_not_guessed_between():
    """Rung 2's `rows[0]` was the same silent guess rung 3 removed, one rung up.

    ``DefinitionID`` is the primary key, so rung 1's ``rows[0]`` is unique by
    construction. ``Description`` is NOT unique — Epicor lets two definitions
    carry the same one — so taking the first row there picks a dashboard by
    insertion order and reports it with full confidence.
    """
    svc = FakeDashboardService(
        _defs(("OpenBacklogOld", "Open Backlog"), ("OpenBacklogNew", "Open Backlog")),
        {"OpenBacklogOld": ["zOLD"], "OpenBacklogNew": ["zNEW"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Open Backlog")

    assert "dashboard" not in out, "an ambiguous description must not resolve"
    assert out["error"] == "dashboard_ambiguous"
    offered = {c["id"] for c in out["valid"]["dashboards"]}
    assert offered == {"OpenBacklogOld", "OpenBacklogNew"}
    assert svc.count("GetRows") == 0, "nothing may be fetched for a guess"


@pytest.mark.asyncio
async def test_word_order_does_not_decide_whether_a_name_resolves():
    """The tokenised pass, exercised where the server-side rungs CANNOT help:
    'backlog open' is not a prefix, not a substring, and not the squashed form, so
    only scoring the tokens against the corpus can reach it."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog"), ("ShipmentStatus", "Shipment Status"),
              ("PastDueOrders", "Past Due Orders")),
        {"OpenBacklog": ["zAR01"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="backlog open")
    assert out["dashboard"] == "OpenBacklog"
    assert out["baq_ids"] == ["zAR01"]


@pytest.mark.asyncio
async def test_a_partly_matching_phrase_offers_the_candidate_instead_of_taking_it():
    """The brief's real requirement: a MISS returns candidates rather than a
    wrong confident match. 'open backlog report' matches two of its three tokens
    against OpenBacklog — plausible, and plausible is exactly when a guess is most
    expensive, because nothing downstream will catch it."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog"), ("ShipmentStatus", "Shipment Status")), {"OpenBacklog": ["zAR01"]})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="open backlog report")

    assert out["error"] == "dashboard_not_found", "a 2-of-3 token match was taken"
    assert out["valid"]["dashboards"] == [{"id": "OpenBacklog", "description": "Open Backlog"}]
    assert out["retry_with"] == {"tool": "epicor_dashboards", "dashboard": "OpenBacklog"}
    assert out["terminal"] is False, "there is a candidate to try; the turn is not over"
    assert svc.count("GetRows") == 0, "nothing may be read for an unresolved name"


@pytest.mark.asyncio
async def test_two_full_matches_are_offered_not_guessed_between():
    svc = FakeDashboardService(
        _defs(("MarginAnalysisPlant", "Margin Analysis by Plant"),
              ("MarginAnalysisPart", "Margin Analysis by Part")),
        {},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="margin analysis")

    assert out["error"] == "dashboard_not_found"
    assert out["terminal"] is False
    ids = [c["id"] for c in out["valid"]["dashboards"]]
    assert set(ids) == {"MarginAnalysisPlant", "MarginAnalysisPart"}
    assert out["retry_with"]["tool"] == "epicor_dashboards"
    assert out["retry_with"]["dashboard"] in ids
    assert "ONCE" in out["message"]
    assert "do NOT search for BAQs" in out["message"]
    assert svc.count("GetRows") == 0, "nothing may be read for an unresolved name"


@pytest.mark.asyncio
async def test_a_like_probe_with_two_hits_does_not_take_the_first_row():
    """Two unranked LIKE matches are ambiguous and must not pick rows[0]."""
    svc = FakeDashboardService(
        _defs(("OpenBacklogDetail", "Open Backlog Detail"),
              ("OpenBacklogSummary", "Open Backlog Summary")),
        {"OpenBacklogDetail": ["zD"], "OpenBacklogSummary": ["zS"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="OpenBacklog")
    assert out.get("error") == "dashboard_not_found", (
        "a two-row LIKE was auto-picked — that is a silent guess"
    )
    assert {c["id"] for c in out["valid"]["dashboards"]} == {
        "OpenBacklogDetail", "OpenBacklogSummary"}


@pytest.mark.asyncio
async def test_a_like_probe_with_exactly_one_hit_is_taken():
    svc = FakeDashboardService(
        _defs(("OpenBacklogDetail", "Open Backlog Detail"), ("ShipmentStatus", "Shipment Status")),
        {"OpenBacklogDetail": ["zD"]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="OpenBacklogDet")
    assert out["dashboard"] == "OpenBacklogDetail"
    # Three GetList rungs at most, and the corpus scan never ran.
    assert svc.count("GetList") == 3


def test_a_two_character_token_only_scores_when_it_anchors():
    """Unanchored, 'ar' is inside 'Part', 'Margin' and 'Warehouse', so every
    "AR ..." question would score against a third of the corpus and the
    exactly-one-full-match rule would never fire."""
    assert _group_score(["ar"], "arstatus", "arstatus", "arstatus arstatus") == 1.0
    for other in ("part", "margin", "warehouse"):
        assert _group_score(["ar"], other, other, f"{other} {other}") == 0.0


# --------------------------------------------------------------------------- #
# 45-46 — paging honesty on BOTH BO calls
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_unmatched_name_is_terminal_only_over_a_complete_corpus():
    svc = FakeDashboardService(_defs(("ShipmentStatus", "Shipment Status")), {})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="zzqqxx")
    assert out["error"] == "dashboard_not_found"
    assert out["terminal"] is True
    assert out["detail"]["corpus_complete"] is True


@pytest.mark.asyncio
async def test_a_miss_over_a_truncated_corpus_is_never_terminal():
    """An unverified negative built on a demonstrably partial corpus is not a
    fact; a 500-row page must not be reported as the whole definition list."""
    corpus = _defs(*[(f"Dash{n:03d}", f"Dashboard {n}") for n in range(600)])
    svc = FakeDashboardService(corpus, {})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="zzqqxx")
    assert out["error"] == "dashboard_not_found"
    assert out["terminal"] is False
    assert out["detail"]["corpus_complete"] is False
    assert "PARTIAL" in out["message"]


@pytest.mark.asyncio
async def test_a_full_attached_baq_page_forfeits_completeness_and_terminal():
    svc = FakeDashboardService(
        _defs(("Big", "Big Dashboard")),
        {"Big": [f"q{n}" for n in range(50)]},
    )
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Big")
    assert len(out["baq_ids"]) == 50
    assert out["baq_ids_complete"] is False
    assert out["terminal"] is False
    assert "INCOMPLETE" in out["note"]


# --------------------------------------------------------------------------- #
# 47-48 — list mode and the empty dashboard
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_which_dashboards_lists_them_deduped_sorted_and_labelled():
    corpus = _defs(("ZAlpha", "Alpha"), ("MBeta", "Beta"), ("ZAlpha", "Alpha"))
    svc = FakeDashboardService(corpus, {})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="which dashboards can I see")

    assert out["mode"] == "dashboard_list"
    assert out["count"] == 2, "duplicates must collapse by DefinitionID"
    assert [d["description"] for d in out["dashboards"]] == ["Alpha", "Beta"]
    assert out["count_complete"] is True
    assert out["terminal"] is False
    assert "Do NOT invent" in out["hint"]
    # Dashboards are `unmapped` in menu_security.db — this list is NOT a menu.
    assert "not a personal menu" in out["scope"]
    assert svc.count("GetRows") == 0


@pytest.mark.asyncio
async def test_a_list_longer_than_the_cap_says_so():
    corpus = _defs(*[(f"D{n:03d}", f"Dash {n:03d}") for n in range(90)])
    svc = FakeDashboardService(corpus, {})
    tool, _desc = _tool(svc, max_list=60)
    out = await tool(dashboard="list dashboards")
    assert out["count"] == 90
    assert len(out["dashboards"]) == 60
    assert "90 dashboards exist" in out["note"]


@pytest.mark.asyncio
async def test_a_full_corpus_page_makes_the_count_a_floor():
    corpus = _defs(*[(f"D{n:03d}", f"Dash {n:03d}") for n in range(600)])
    svc = FakeDashboardService(corpus, {})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="list dashboards")
    assert out["count_complete"] is False
    assert "floor" in out["note"]


@pytest.mark.asyncio
async def test_a_dashboard_with_no_baqs_is_an_answer_not_an_error():
    svc = FakeDashboardService(_defs(("Empty", "Empty Dashboard")), {"Empty": []})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Empty")
    assert "error" not in out
    assert out["baq_ids"] == []
    assert out["terminal"] is True
    assert "no BAQ queries attached" in out["note"]


@pytest.mark.asyncio
async def test_an_empty_dashboard_and_a_missing_one_are_told_apart():
    """"It exists and has nothing attached" and "there is no such dashboard" are
    DIFFERENT answers, and only one of them means stop looking.

    Collapsing them is the ``0 parent rows`` vs ``0 attachments`` trap the
    attachments recognizer exists to kill: told "no BAQs" for a name that does
    not exist, the model re-spells the name forever; told "not found" for a real
    but empty dashboard, it reports a real dashboard as missing.
    """
    empty_svc = FakeDashboardService(_defs(("Empty", "Empty Dashboard")), {"Empty": []})
    empty_tool, _d1 = _tool(empty_svc)
    empty = await empty_tool(dashboard="Empty")

    missing_svc = FakeDashboardService(_defs(("Empty", "Empty Dashboard")), {"Empty": []})
    missing_tool, _d2 = _tool(missing_svc)
    missing = await missing_tool(dashboard="zzqqxx")

    assert "error" not in empty and empty["dashboard"] == "Empty"
    assert missing["error"] == "dashboard_not_found" and "dashboard" not in missing
    # Both are terminal, but for opposite reasons — so the note/message must
    # differ, or the distinction exists only in the slug.
    assert empty["terminal"] is missing["terminal"] is True
    assert "no BAQ queries attached" in empty["note"]
    assert "no BAQ queries attached" not in str(missing)
    assert empty_svc.count("GetRows") == 1, "the empty one was really looked up"
    assert missing_svc.count("GetRows") == 0


# --------------------------------------------------------------------------- #
# 49-50 — the hand-off, and what happens when there is nothing to hand off to
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_ids_are_handed_to_epicor_query_and_nothing_is_executed():
    """Regression coverage: test the ids are handed to epicor query and nothing is executed."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog")), {"OpenBacklog": ["zAR01", "zAR02"]})
    tool, _desc = _tool(svc, query_tool_available=True)
    out = await tool(dashboard="OpenBacklog")

    assert out["run_with"] == [
        {"tool": "epicor_query", "saved_baq": "zAR01"},
        {"tool": "epicor_query", "saved_baq": "zAR02"},
    ]
    assert "epicor_query(saved_baq='zAR01')" in out["next_step"]
    assert out["terminal"] is False, "the ids are not the rows; the turn is not over"
    assert not any("/Data" in k for k, _ in svc.calls)


@pytest.mark.asyncio
async def test_nothing_names_epicor_query_when_it_did_not_register():
    """``register_query_tool`` can return a FALSE decision, and
    naming a tool the model cannot call is a guaranteed dead turn."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog")), {"OpenBacklog": ["zAR01"]})
    tool, desc = _tool(svc, query_tool_available=False)
    out = await tool(dashboard="OpenBacklog")

    assert out["baq_ids"] == ["zAR01"]
    assert "run_with" not in out
    assert "next_step" not in out
    assert "epicor_query" not in repr(out)
    assert "epicor_query" not in desc
    assert out["terminal"] is True


def test_the_description_names_epicor_query_when_it_did_register():
    svc = FakeDashboardService([], {})
    _tool_fn, desc = _tool(svc, query_tool_available=True)
    assert "epicor_query(saved_baq=" in desc


# --------------------------------------------------------------------------- #
# 51-52 — literals, wildcards, and Epicor's own refusal
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_apostrophe_is_doubled_rather_than_breaking_the_clause():
    """"Buyer's Dashboard" is a legitimate name. Unescaped it breaks the clause
    — and the same seam is a where-clause injection into a BO."""
    svc = FakeDashboardService(
        _defs(("BuyerDash", "Buyer's Dashboard")), {"BuyerDash": ["zB"]})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="Buyer's Dashboard")

    assert out["dashboard"] == "BuyerDash"
    assert any("Buyer''s Dashboard" in w for w in svc.wheres())


@pytest.mark.asyncio
async def test_like_wildcards_in_the_callers_phrase_are_neutralised():
    """``%`` and ``_`` widen the match set that the LONE-HIT rule then reads
    from — so the caller's own punctuation could decide whether rung 3 auto-
    picks. Here ``%`` must match nothing, leaving the miss."""
    svc = FakeDashboardService(
        _defs(("OpenBacklog", "Open Backlog"), ("ShipmentStatus", "Shipment Status")), {})
    tool, _desc = _tool(svc)
    out = await tool(dashboard="A%g")

    like = next(w for w in svc.wheres() if "like" in w)
    assert "[%]" in like, "a raw % reached the LIKE pattern"
    assert out.get("error") == "dashboard_not_found", (
        "the wildcard matched OpenBacklog, so rung 3 auto-picked on punctuation"
    )


@pytest.mark.asyncio
async def test_an_access_refusal_carries_epicors_own_status_and_message():
    """A blanket except that returns *"Verify the name and try again"* for a 401
    sends the model back to re-check a name that was never the problem."""
    class _Denied(Exception):
        def __init__(self):
            super().__init__("[401] Access denied to Ice.BO.DashBoardSvc")
            self.status_code = 401
            self.message = "Access denied to Ice.BO.DashBoardSvc"

    svc = FakeDashboardService(_defs(("OpenBacklog", "Open Backlog")), {},
                               error=_Denied())
    tool, _desc = _tool(svc)
    out = await tool(dashboard="OpenBacklog")

    assert out["error"] == "dashboard_service_unavailable"
    assert out["terminal"] is True
    assert out["detail"]["status"] == 401
    assert "Access denied" in out["detail"]["message"]


@pytest.mark.asyncio
async def test_a_bug_in_this_module_is_not_reported_as_an_access_problem():
    """A refusal slug is a claim. ``dashboard_service_unavailable`` says Epicor
    said no; a TypeError in our own code did not."""
    svc = FakeDashboardService(_defs(("OpenBacklog", "Open Backlog")), {},
                               error=TypeError("boom"))
    tool, _desc = _tool(svc)
    out = await tool(dashboard="OpenBacklog")
    assert out["error"] == "dashboard_lookup_failed"
    assert out["detail"]["exception"] == "TypeError"
