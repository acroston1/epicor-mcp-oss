"""Regression coverage: test diagnose empty."""

from __future__ import annotations

import asyncio
import re
import time

import pytest

from epicor_mcp.sql import diagnose_empty as de
from epicor_mcp.sql.adhoc import (
    EXECUTE_PATH,
    PARSE_PATH,
    PROBE_PAGE_SIZE,
    make_probe_runner,
    run_sql,
)
from epicor_mcp.sql.diagnose_empty import (
    DEFAULT_PROBE_BUDGET,
    DomainCache,
    ProbeResult,
    diagnose_empty,
)
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"
COMPANY = "DEMO"


# --------------------------------------------------------------------------- #
# A scripted probe: regex -> rows. Records every probe SQL it was asked for.
# --------------------------------------------------------------------------- #


class ScriptedProbe:
    def __init__(
        self,
        script: list[tuple[str, list[dict]]] | None = None,
        *,
        default: list[dict] | None = None,
    ) -> None:
        self.script = script or []
        self.seen: list[str] = []
        self.default = default
        self.fail_all = False

    def __call__(self, sql: str):
        async def _run() -> ProbeResult:
            self.seen.append(sql)
            if self.fail_all:
                return ProbeResult(False, error="probe unavailable")
            for pattern, rows in self.script:
                if re.search(pattern, sql, re.I):
                    return ProbeResult(True, rows=[dict(r) for r in rows])
            if self.default is not None:
                return ProbeResult(True, rows=[dict(r) for r in self.default])
            return ProbeResult(False, error=f"no scripted answer for {sql}")

        return _run()


def diagnose(sql: str, probe: ScriptedProbe, **kw) -> dict:
    kw.setdefault("company_id", COMPANY)
    kw.setdefault("cache", DomainCache())
    return asyncio.run(diagnose_empty(sql, probe=probe, **kw))


# --------------------------------------------------------------------------- #
# INVARIANT 1 — it costs NOTHING on any query that is not empty
# --------------------------------------------------------------------------- #


def _call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def test_a_query_that_returns_rows_makes_the_same_two_calls_it_always_did():
    """The gate is `row_count == 0`. A non-empty answer must be byte-for-byte
    the shape it was before E15b existed — no probe, no `diagnosis` key."""
    sql, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"PN": "ABC-1"}]))
    out = _call(sql, client)
    assert out["success"] is True and out["row_count"] == 1
    assert "diagnosis" not in out
    assert "diagnose_ms" not in out
    assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]


def test_the_diagnostician_is_not_even_entered_when_rows_come_back(monkeypatch):
    """Not just 'no extra Epicor call' — the module function is never called, so
    there is no sqlglot parse either."""
    calls: list[str] = []

    async def spy(sql, **kw):  # pragma: no cover - must never run
        calls.append(sql)
        return {"verdict": "x", "message": "x", "likely_mistake": False}

    monkeypatch.setattr("epicor_mcp.sql.adhoc.diagnose_empty", spy)
    sql, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"PN": "A"}]))
    _call(sql, client)
    assert calls == []


def test_an_empty_LATER_page_is_not_diagnosed():
    """Regression coverage: test an empty LATER page is not diagnosed."""
    sql = "select distinct [P].[ClassID] as [C] from Erp.Part as [P]"
    _, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    out = _call(sql, client, page_size=200, page_num=3)
    assert out["row_count"] == 0
    assert "diagnosis" not in out
    assert out["summary"].startswith("EMPTY PAGE:")
    assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]


def test_diagnose_false_turns_the_whole_feature_off():
    sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[ClassID] = 'Z'"
    _, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    out = _call(sql, client, diagnose=False)
    assert "diagnosis" not in out
    assert len(client.calls) == 2


# --------------------------------------------------------------------------- #
# INVARIANT 2 — never an exception, never a refusal, never changes the result
# --------------------------------------------------------------------------- #


def test_the_diagnosis_annotates_and_changes_nothing_about_the_result():
    sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[ClassID] = 'ZZZ'"
    _, ds = load("clean_top")

    class Client(MockEpicorClient):
        """Remembers the DisplayPhrase it was last asked to parse, so the Execute
        that follows can be answered per-statement."""

        last_phrase = ""

        async def post(self, url, api_key, json_body=None):
            self.calls.append((url, json_body or {}))
            if "ParseFromSQL" in url:
                from tests.wedge_fixtures import as_designer

                self.last_phrase = json_body["ds"]["DynamicQueryDesigner"][0]["DisplayPhrase"]
                return {"parameters": {"ds": as_designer(ds)}}
            phrase = self.last_phrase
            if "group by" in phrase:
                return ok_execute([{"value": "MFG", "n": "12"}, {"value": "RAW", "n": "3"}])
            if "count(*)" in phrase:
                return ok_execute([{"n": "0"}])
            return ok_execute([])

    client = Client(parse_ds=ds)
    out = _call(sql, client)
    assert out["success"] is True
    assert out["row_count"] == 0
    assert out["rows"] == ""
    assert out["complete"] is True          # paging honesty is untouched
    assert "error" not in out
    assert out["diagnosis"]["verdict"] == "killing_predicate"
    assert out["summary"].startswith("ZERO ROWS — ")


def test_terminal_is_only_ever_taken_away_and_only_for_a_likely_mistake():
    """Regression coverage: test terminal is only ever taken away and only for a likely mistake."""
    sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"
    _, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    out = _call(sql, client)
    assert out["diagnosis"]["verdict"] == "no_predicates"
    assert out["diagnosis"]["likely_mistake"] is False
    assert out["terminal"] is True


def test_a_probe_that_explodes_degrades_to_a_plain_empty_result():
    class Exploding(ScriptedProbe):
        def __call__(self, sql: str):
            async def _boom():
                raise RuntimeError("network on fire")

            return _boom()

    out = diagnose(
        "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[ClassID] = 'Z'",
        Exploding(),
    )
    assert out["verdict"] == "undetermined"
    assert out["likely_mistake"] is False
    assert "may" in out["message"]


def test_unparseable_sql_is_undetermined_not_an_error():
    out = diagnose("this is not sql at all ((", ScriptedProbe())
    assert out["verdict"] == "undetermined"
    assert out["likely_mistake"] is False


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #

_PLANT_ROWS = [
    {"value": "21", "label": "Example Company - Oakridge Division", "n": "9000"},
    {"value": "34", "label": "Example Company - Pinefield Division", "n": "4000"},
    {"value": "56", "label": "Example Company - Mapleworks Division", "n": "3000"},
    {"value": "67", "label": "Example Company - Sample Coast Division", "n": "2000"},
    {"value": "78", "label": "Example Company - Sample Valley Division", "n": "1000"},
    {"value": "97", "label": "Example Office", "n": "10"},
]


def test_plant_oakridge_names_the_predicate_the_domain_and_the_code():
    """Regression coverage: test plant oakridge names the predicate the domain and the code."""
    sql = (
        "select top 100 [JH].[JobNum] as [JobNum] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Oakridge' and [JH].[JobClosed] = 0"
    )
    probe = ScriptedProbe(
        [
            (r"where \[JH\]\.\[Plant\]", [{"n": "0"}]),
            (r"where \[JH\]\.\[JobClosed\]", [{"n": "240"}]),
            (r"from Erp\.Plant", _PLANT_ROWS),
        ]
    )
    out = diagnose(sql, probe)
    assert out["verdict"] == "killing_predicate"
    assert out["likely_mistake"] is True
    killer = out["killing_predicates"][0]
    assert killer["predicate"] == "[JH].[Plant] = 'Oakridge'"
    assert killer["domain"]["values"] == ["21", "34", "56", "67", "78", "97"]
    assert killer["correction"]["value"] == "21"
    assert "Oakridge" in killer["correction"]["match_basis"]
    # The other predicate is reported as fine, which is half the recovery.
    assert out["satisfied_predicates"] == ["[JH].[JobClosed] = 0"]
    # retry_with is RUNNABLE, not a template.
    assert "'21'" in out["retry_with"]["sql"]
    assert "Oakridge" not in out["retry_with"]["sql"]


def test_plant_site_21_recovers_the_code_out_of_the_prose():
    """A numeric site code can be recovered from a caller's descriptive label."""
    sql = (
        "select top 100 [JH].[JobNum] as [JobNum] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Site 21'"
    )
    probe = ScriptedProbe(
        [(r"where \[JH\]\.\[Plant\]", [{"n": "0"}]), (r"from Erp\.Plant", _PLANT_ROWS)]
    )
    out = diagnose(sql, probe)
    assert out["killing_predicates"][0]["correction"]["value"] == "21"
    assert "'21'" in out["retry_with"]["sql"]


def test_the_plant_domain_is_read_off_Erp_Plant_not_off_the_big_table():
    """Enumerate the Plant master instead of scanning JobHead transactions."""
    sql = (
        "select top 100 [JH].[JobNum] as [JobNum] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [(r"where \[JH\]\.\[Plant\]", [{"n": "0"}]), (r"from Erp\.Plant", _PLANT_ROWS)]
    )
    diagnose(sql, probe)
    enumerations = [s for s in probe.seen if "group by" in s]
    assert len(enumerations) == 1
    assert "from Erp.Plant as [D]" in enumerations[0]
    assert "Erp.JobHead" not in enumerations[0]


def test_company_filter_requires_measurement_in_a_multi_company_database():
    sql = (
        "select top 100 [QM].[EstUnitCost] as [Cost] from Erp.QuoteMtl as [QM] "
        "where [QM].[Company] = 'EXAMPLE' and [QM].[QuoteNum] = 10001"
    )
    probe = ScriptedProbe([(r".*", [{"n": "7"}])])
    out = diagnose(sql, probe)
    assert out['verdict'] == 'genuinely_empty'
    assert out['likely_mistake'] is False
    assert not out['killing_predicates']
    assert 'retry_with' not in out
    assert any('[Company]' in statement for statement in probe.seen)


def test_sugpodtl_buy_true_says_REMOVE_the_predicate_not_invert_it():
    """Regression coverage: test sugpodtl buy true says REMOVE the predicate not invert it."""
    sql = (
        "select top 100 [S].[SugNum] as [SugNum] from Erp.SugPoDtl as [S] "
        "where [S].[Buy] = true and [S].[Review] = false"
    )
    probe = ScriptedProbe(
        [
            (r"where \[S\]\.\[Buy\]", [{"n": "0"}]),
            (r"where \[S\]\.\[Review\]", [{"n": "120"}]),
            (r"group by", [{"value": "false", "n": "120"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["suggested_action"] == "drop_predicate"
    assert killer["likely_mistake"] is True
    assert "120" in killer["note"]
    assert "correction" not in killer
    # The recovery removes the predicate and keeps the rest of the statement.
    retry = out["retry_with"]["sql"]
    assert "[Buy]" not in retry
    assert "[Review]" in retry
    # And it quotes the caller's own spelling back, not sqlglot's `= 1`.
    assert killer["predicate"] == "[S].[Buy] = true"


def test_resource_type_S_returns_the_real_three_value_domain():
    """Regression coverage: test resource type S returns the real three value domain."""
    sql = (
        "select top 100 [R].[ResourceID] as [M] from Erp.ResourceGroup as [R] "
        "where [R].[ResourceType] = 'S'"
    )
    probe = ScriptedProbe(
        [
            (r"where \[R\]\.\[ResourceType\]", [{"n": "0"}]),
            (
                r"group by",
                [
                    {"value": "", "n": "12"},
                    {"value": "MACHINE", "n": "9"},
                    {"value": "LABOR", "n": "6"},
                    {"value": "OSV", "n": "20"},
                ],
            ),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["likely_mistake"] is True
    assert killer["domain"]["complete"] is True
    assert "MACHINE" in killer["note"] and "LABOR" in killer["note"]
    # 'S' is not close enough to anything to be auto-corrected — and guessing
    # would be worse than the ambiguity.
    assert "correction" not in killer


def test_custnum_10000_reports_the_real_range_and_calls_it_a_mistake():
    """Regression coverage: test custnum 10000 reports the real range and calls it a mistake."""
    sql = (
        "select top 100 [OD].[PartNum] as [PN] from Erp.OrderDtl as [OD] "
        "where [OD].[CustNum] = 10000"
    )
    probe = ScriptedProbe(
        [
            (r"where \[OD\]\.\[CustNum\]", [{"n": "0"}]),
            (r"min\(", [{"lo": "1", "hi": "600"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["domain"]["kind"] == "range"
    assert killer["domain"]["max"] == "600"
    assert killer["likely_mistake"] is True
    assert "600" in killer["note"]


# --------------------------------------------------------------------------- #
# THE FALSE-POSITIVE CONTROL — an empty answer that is CORRECT
# --------------------------------------------------------------------------- #


def test_a_missing_AP_invoice_is_reported_as_a_fact_not_as_a_mistake():
    """Regression coverage: test a missing AP invoice is reported as a fact not as a mistake."""
    sql = (
        "select top 100 [H].[InvoiceNum] as [InvoiceNum] from Erp.APInvHed as [H] "
        "where [H].[InvoiceNum] = 'INV-10001'"
    )
    probe = ScriptedProbe([(r"where \[H\]\.\[InvoiceNum\]", [{"n": "0"}])])
    out = diagnose(sql, probe)
    assert out["verdict"] == "killing_predicate"
    assert out["likely_mistake"] is False, "a missing record is not a mistake"
    killer = out["killing_predicates"][0]
    assert "correction" not in killer
    assert "Erp.InvcHead" in killer["sibling_hint"]
    assert "may still be the correct answer" in out["message"]
    # APInvHed is a big transaction table: its InvoiceNum values were NOT
    # enumerated, so no scan was run to produce a non-answer.
    assert not any("group by" in s for s in probe.seen)


def test_a_value_INSIDE_the_real_range_that_has_no_row_is_not_a_mistake():
    """The mirror of `custnum_10000`. Customer 400 exists as a number; if no
    order carries it, 0 rows is a real business answer."""
    sql = (
        "select top 100 [OD].[PartNum] as [PN] from Erp.OrderDtl as [OD] "
        "where [OD].[CustNum] = 400"
    )
    probe = ScriptedProbe(
        [
            (r"where \[OD\]\.\[CustNum\]", [{"n": "0"}]),
            (r"min\(", [{"lo": "1", "hi": "600"}]),
        ]
    )
    out = diagnose(sql, probe)
    assert out["killing_predicates"][0]["likely_mistake"] is False
    assert out["likely_mistake"] is False
    assert "may simply mean that record does not exist" in out["killing_predicates"][0]["note"]


def test_every_predicate_satisfiable_is_a_TERMINAL_CORRECT_ANSWER():
    """The invariant that overrides everything else: when the combination is
    genuinely empty, SAY SO, and do not let it read as a failure."""
    sql = (
        "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] "
        "where [J].[JobClosed] = 1 and [J].[JobComplete] = 0"
    )
    probe = ScriptedProbe(default=[{"n": "500"}])
    out = diagnose(sql, probe)
    assert out["verdict"] == "genuinely_empty"
    assert out["likely_mistake"] is False
    assert "this is the correct answer" in out["message"]
    assert out["killing_predicates"] == []
    assert len(out["satisfied_predicates"]) == 2
    assert not any("group by" in s for s in probe.seen), "no domain probe was needed"


def test_a_genuinely_empty_join_names_the_join_as_part_of_the_combination():
    sql = (
        "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] "
        "inner join Erp.Part as [P] on [J].[Company] = [P].[Company] "
        "and [J].[PartNum] = [P].[PartNum] "
        "where [J].[JobClosed] = 1"
    )
    probe = ScriptedProbe(default=[{"n": "500"}])
    out = diagnose(sql, probe)
    assert out["verdict"] == "genuinely_empty"
    assert "join" in out["message"].lower()


def test_an_empty_table_with_no_predicates_is_the_complete_correct_answer():
    out = diagnose("select top 100 [X].[A] as [A] from Erp.Thing as [X]", ScriptedProbe())
    assert out["verdict"] == "no_predicates"
    assert out["likely_mistake"] is False
    assert "correct answer" in out["message"]
    assert out["probes_used"] == 0


def test_no_where_but_a_join_blames_the_join_and_still_costs_nothing():
    sql = (
        "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] "
        "inner join Erp.Part as [P] on [J].[Company] = [P].[Company] "
        "and [J].[PartNum] = [P].[PartNum]"
    )
    out = diagnose(sql, ScriptedProbe())
    assert out["verdict"] == "no_predicates"
    assert "JOIN" in out["message"]
    assert out["probes_used"] == 0


# --------------------------------------------------------------------------- #
# INVARIANT 3 — never leak a denied column
# --------------------------------------------------------------------------- #


def test_a_denied_column_is_never_probed_and_never_enumerated():
    """A diagnostic that reports the distinct values of a pay-rate column is an
    authorization bypass wearing a helpful hat. This gate is BEFORE the SQL is
    even built, so nothing about the column reaches Epicor."""
    sql = (
        "select top 100 [L].[JobNum] as [J] from Erp.LaborDtl as [L] "
        "where [L].[LaborRate] = 999 and [L].[JobNum] = 'X'"
    )
    probe = ScriptedProbe(default=[{"n": "0"}])
    out = diagnose(sql, probe)
    body = str(out)
    assert not any("LaborRate" in s for s in probe.seen)
    skipped = {s["predicate"]: s["reason"] for s in out["skipped_predicates"]}
    assert any("LaborRate" in p for p in skipped)
    assert "denied" in " ".join(skipped.values())
    # The refusal names the column (that IS the recovery) but no VALUE of it
    # appears anywhere.
    assert "999" not in body.replace("[L].[LaborRate] = 999", "")


def test_a_denied_table_is_never_probed():
    sql = (
        "select top 100 [E].[EmpID] as [E] from Erp.PREmpMas as [E] "
        "where [E].[EmpID] = 'X'"
    )
    out = diagnose(sql, ScriptedProbe(default=[{"n": "0"}]))
    assert out["verdict"] in {"undetermined", "genuinely_empty", "killing_predicate"}
    reasons = " ".join(s["reason"] for s in out["skipped_predicates"])
    assert "deny-list" in reasons


def test_the_production_probe_runner_re_enforces_the_denylist_on_epicors_own_parse():
    """Generated probe SQL must pass the same parsed-dataset authorization as
    caller-supplied SQL. The probe runner applies
    `check_parsed_ds` exactly as the caller's query did — and Execute is never
    reached."""
    _, ds = load("deny_star_payroll")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"n": "5"}]))
    probe = make_probe_runner(
        client=client, api_key="k", base_url=BASE, timeout_s=5.0
    )
    result = asyncio.run(probe("select top 5 count(*) as [n] from Erp.Part as [P]"))
    assert result.ok is False
    assert "deny" in result.error.lower()
    assert not client.called("Execute")


# --------------------------------------------------------------------------- #
# INVARIANT 4 — bounded, capped, logged
# --------------------------------------------------------------------------- #


def test_every_probe_is_a_bounded_single_table_aggregate():
    sql = (
        "select top 100 [R].[ResourceID] as [M] from Erp.ResourceGroup as [R] "
        "where [R].[ResourceType] = 'S' and [R].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\)", [{"n": "0"}]),
            (r"from Erp\.Plant", _PLANT_ROWS),
            (r"group by", [{"value": "MACHINE", "n": "9"}]),
        ]
    )
    diagnose(sql, probe)
    assert probe.seen
    for probe_sql in probe.seen:
        assert probe_sql.startswith("select top "), probe_sql
        assert " join " not in probe_sql.lower(), probe_sql
        assert probe_sql.lower().count(" from ") == 1, probe_sql
        assert "*" not in probe_sql.replace("count(*)", ""), probe_sql


def test_the_budget_is_hard_and_what_it_could_not_test_is_NAMED():
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "where [P].[ClassID] = 'A' and [P].[TypeCode] = 'B' and [P].[ProdCode] = 'C' "
        "and [P].[UOMClassID] = 'D' and [P].[PartNum] = 'E' and [P].[Method] = 'F' "
        "and [P].[NonStock] = 1"
    )
    probe = ScriptedProbe(default=[{"n": "17"}])
    out = diagnose(sql, probe, budget=DEFAULT_PROBE_BUDGET)
    assert out["probes_used"] <= DEFAULT_PROBE_BUDGET
    assert len(probe.seen) <= DEFAULT_PROBE_BUDGET
    assert out["probes_exhausted"] is True
    assert out["untested_predicates"], "the untested predicates must be named"
    # And with the cause unestablished it must NOT claim a genuinely-empty answer.
    assert out["verdict"] == "undetermined"
    assert out["likely_mistake"] is False


def test_budget_zero_makes_no_calls_at_all():
    sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[ClassID] = 'Z'"
    probe = ScriptedProbe(default=[{"n": "0"}])
    out = diagnose(sql, probe, budget=0)
    assert probe.seen == []
    assert out["probes_used"] == 0
    assert out["likely_mistake"] is False


def test_every_extra_call_is_logged_for_the_audit_trail():
    sql = (
        "select top 100 [JH].[JobNum] as [J] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [(r"where \[JH\]", [{"n": "0"}]), (r"from Erp\.Plant", _PLANT_ROWS)]
    )
    out = diagnose(sql, probe)
    assert len(out["probes"]) == len(probe.seen) == out["probes_used"]
    kinds = [p["kind"] for p in out["probes"]]
    assert kinds == ["satisfiability", "enumeration"]
    assert all(p["sql"] for p in out["probes"])


def test_the_probe_runner_sends_a_page_size_on_every_execute():
    """Invariant 9: nothing leaves unbounded, diagnostics included."""
    _, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([{"n": "3"}]))
    probe = make_probe_runner(client=client, api_key="k", base_url=BASE, timeout_s=5.0)
    asyncio.run(probe("select top 5 count(*) as [n] from Erp.Part as [P]"))
    body = client.body_for("Execute")
    settings = {s["Name"]: s["Value"] for s in body["executionParams"]["ExecutionSetting"]}
    assert settings["PageSize"] == str(PROBE_PAGE_SIZE)
    assert settings["PageNum"] == "1"


def test_the_domain_cache_stops_the_same_column_being_asked_twice():
    """Repeated site predicates reuse a cached Plant enumeration."""
    cache = DomainCache()
    sql = (
        "select top 100 [JH].[JobNum] as [J] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [(r"where \[JH\]", [{"n": "0"}]), (r"from Erp\.Plant", _PLANT_ROWS)]
    )
    first = diagnose(sql, probe, cache=cache)
    seen_after_first = len(probe.seen)
    second = diagnose(sql, probe, cache=cache)
    added = len(probe.seen) - seen_after_first
    assert seen_after_first == 2
    assert added == 1, "the enumeration must come from the cache"
    assert second["killing_predicates"][0]["domain"]["from_cache"] is True
    assert second["killing_predicates"][0]["correction"]["value"] == "21"
    assert first["retry_with"]["sql"] == second["retry_with"]["sql"]


# --------------------------------------------------------------------------- #
# Decomposition is the AST, not text matching
# --------------------------------------------------------------------------- #


def test_an_OR_group_is_one_term_and_is_never_probed_piecewise():
    """Relaxing half an OR answers a different question."""
    sql = (
        "select top 100 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "where ([P].[ClassID] = 'A' or [P].[ClassID] = 'B') and [P].[NonStock] = 1"
    )
    probe = ScriptedProbe(default=[{"n": "4"}])
    out = diagnose(sql, probe)
    assert len(probe.seen) == 1, probe.seen
    reasons = " ".join(s["reason"] for s in out["skipped_predicates"])
    assert "OR group" in reasons


def test_a_predicate_spanning_two_tables_is_reported_not_probed():
    sql = (
        "select top 100 [A].[PartNum] as [PN] from Erp.Part as [A] "
        "inner join Erp.PartPlant as [B] on [A].[Company] = [B].[Company] "
        "and [A].[PartNum] = [B].[PartNum] "
        "where [A].[ClassID] = [B].[PartNum] and [A].[NonStock] = 1"
    )
    probe = ScriptedProbe(default=[{"n": "4"}])
    out = diagnose(sql, probe)
    reasons = " ".join(s["reason"] for s in out["skipped_predicates"])
    assert "more than one table" in reasons
    assert not any("[A].[ClassID] = [B]" in s for s in probe.seen)


def test_a_cte_reference_has_no_physical_table_and_is_not_probed():
    sql = (
        "with [c] as (select [P].[PartNum] as [PartNum] from Erp.Part as [P]) "
        "select top 100 [c].[PartNum] as [PN] from [c] where [c].[PartNum] = 'X'"
    )
    out = diagnose(sql, ScriptedProbe(default=[{"n": "0"}]))
    assert out["verdict"] in {"undetermined", "genuinely_empty"}
    assert out["likely_mistake"] is False


def test_a_null_test_explains_the_epicor_convention_instead_of_a_domain():
    """A NULL predicate should explain the zero-default convention without enumeration."""
    sql = (
        "select top 100 [S].[SugNum] as [S] from Erp.SugPoDtl as [S] "
        "where [S].[PONUM] is null"
    )
    probe = ScriptedProbe(default=[{"n": "0"}])
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert "= 0" in killer["note"]
    assert "domain" not in killer
    assert len(probe.seen) == 1, "a NULL test needs no domain probe"


def test_a_HAVING_that_removed_every_group_is_named():
    sql = (
        "select top 100 [OD].[PartNum] as [PN], sum([OD].[OrderQty]) as [Q] "
        "from Erp.OrderDtl as [OD] where [OD].[OpenLine] = 1 "
        "group by [OD].[PartNum] having sum([OD].[OrderQty]) > 999999"
    )
    probe = ScriptedProbe(
        [(r"as \[t\]", [{"n": "160"}]), (r"where \[OD\]\.\[OpenLine\]", [{"n": "9000"}])]
    )
    out = diagnose(sql, probe)
    assert out["verdict"] == "killing_having"
    assert out["likely_mistake"] is True
    assert "160" in out["message"]


# --------------------------------------------------------------------------- #
# The rewrite is the AST, and it must survive being re-parsed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql, target, replacement",
    [
        (
            "select top 5 [P].[A] as [A] from Erp.T as [P] where [P].[X] = 'a'",
            "[P].[X] = 'a'",
            None,
        ),
        (
            "select top 5 [P].[A] as [A] from Erp.T as [P] "
            "where [P].[X] = 'a' and [P].[Y] = 'b' and [P].[Z] = 'c'",
            "[P].[Y] = 'b'",
            None,
        ),
    ],
)
def test_dropping_a_conjunct_yields_re_parseable_sql(sql, target, replacement):
    import sqlglot

    out = de._rewrite_where(sql, target, replacement)
    assert out is not None
    assert target.split(" = ")[0] not in out
    sqlglot.parse_one(out, read="tsql")  # must not raise


def test_a_correction_is_never_a_guess():
    """Uniqueness is required on every basis. Two divisions containing the word
    would be a guess, and a guess is the failure class this module removes."""
    assert de._correction("oakridge", ["21", "34"], {"21": "Oakridge North", "34": "Oakridge South"}) is None
    assert de._correction("zzz", ["21", "34"], {}) is None
    hit = de._correction("oakridge", ["21", "34"], {"21": "Oakridge North", "34": "Pinefield"})
    assert hit["value"] == "21"


def test_a_sample_of_a_wide_column_is_never_presented_as_a_domain():
    """26 rows back from a `top 26` proves there are MORE than 25 distinct
    values. Saying 'your value is not in this list' about 25 of thousands is the
    false alarm this module refuses."""
    rows = [{"value": f"V{i}", "n": "5"} for i in range(de.DOMAIN_LIMIT + 1)]
    sql = (
        "select top 100 [R].[ResourceID] as [M] from Erp.Resource as [R] "
        "where [R].[ResourceID] = 'NOPE'"
    )
    probe = ScriptedProbe([(r"group by", rows), (r"count\(\*\) as \[n\] from", [{"n": "0"}])])
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["domain"]["complete"] is False
    assert killer["likely_mistake"] is False
    assert "SAMPLE" in killer["note"]
    assert out["likely_mistake"] is False


# --------------------------------------------------------------------------- #

# offline tests could not have found, so each gets a regression test.
# --------------------------------------------------------------------------- #


def test_min_max_on_a_BIT_column_fails_and_falls_back_to_the_enumeration():
    """Regression coverage: test min max on a BIT column fails and falls back to the enumeration."""
    sql = (
        "select top 100 [S].[SugNum] as [S] from Erp.SugPoDtl as [S] "
        "where [S].[Buy] = 1 and [S].[VendorNum] > 0"
    )

    class FailMinMax(ScriptedProbe):
        """`min()`/`max()` on a bit column errors, exactly as Epicor does."""

        def __call__(self, sql_):
            if "min(" in sql_:
                self.seen.append(sql_)

                async def _fail():
                    return ProbeResult(False, error="the probe returned an error")

                return _fail()
            return super().__call__(sql_)

    failing = FailMinMax(
        [
            (r"where \[S\]\.\[Buy\] = 1", [{"n": "0"}]),
            (r"where \[S\]\.\[VendorNum\] > 0", [{"n": "120"}]),
            (r"group by", [{"value": "false", "n": "120"}]),
        ]
    )
    out = diagnose(sql, failing)
    killer = out["killing_predicates"][0]
    assert killer["range_probe"] == "unavailable"
    assert killer["likely_mistake"] is True
    assert killer["suggested_action"] == "drop_predicate"
    assert any("min(" in s for s in failing.seen)
    assert any("group by" in s for s in failing.seen)


# --------------------------------------------------------------------------- #

# Legitimately empty results must not be labeled likely mistakes.
# An empty result can be the correct terminal answer.
# --------------------------------------------------------------------------- #


def test_F1_a_real_site_with_no_rows_is_NOT_a_mistake():
    """Regression coverage: test F1 a real site with no rows is NOT a mistake."""
    sql = "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] where [J].[Plant] = '85'"
    probe = ScriptedProbe(
        [
            (r"count\(\*\) as \[n\] from Erp\.JobHead", [{"n": "0"}]),
            (
                r"group by",
                [
                    {"value": v, "label": f"Example Company - {v}", "n": "1"}
                    for v in ("21", "22", "34", "45", "56", "67", "78", "85", "97")
                ],
            ),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["likely_mistake"] is False
    assert out["likely_mistake"] is False
    assert "correction" not in killer
    assert killer.get("retry_sql") is None
    assert out.get("retry_with") is None
    assert killer["value_exists_elsewhere"] is True
    # ...and the message must not assert the refutation it prints.
    assert "is not one of them" not in killer["note"]
    assert "IS a real value" in killer["note"]


def test_F1_a_value_absent_from_the_authority_is_STILL_a_mistake():
    """Regression coverage: test F1 a value absent from the authority is STILL a mistake."""
    sql = (
        "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] "
        "where [J].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\) as \[n\] from Erp\.JobHead", [{"n": "0"}]),
            (
                r"group by",
                [
                    {"value": "21", "label": "Example Company - Oakridge Division", "n": "1"},
                    {"value": "34", "label": "Example Company - Pinefield", "n": "1"},
                ],
            ),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["likely_mistake"] is True
    assert killer["correction"]["value"] == "21"
    assert out["retry_with"]["sql"]


def test_F2_a_dead_flag_that_is_the_WHOLE_question_is_a_fact_not_a_mistake():
    """Regression coverage: test F2 a dead flag that is the WHOLE question is a fact not a mistake."""
    sql = (
        "select top 100 [C].[CustID] as [C] from Erp.Customer as [C] "
        "where [C].[Inactive] = true"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\) as \[n\] from Erp\.Customer", [{"n": "0"}]),
            (r"group by", [{"value": "false", "n": "24"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["likely_mistake"] is False
    assert killer.get("retry_sql") is None
    assert out.get("retry_with") is None
    assert killer["suggested_action"] == "none"
    assert "correct, complete" in killer["note"]
    # The FACT is still served — it is the most useful sentence in the response.
    assert "ONE value across this whole install" in killer["note"]
    # ...and so is the runnable alternative, under a name that is NOT the fix.
    assert killer["broader_query"]["sql"].lower().startswith("select")
    assert "Inactive" not in killer["broader_query"]["sql"]
    assert "DIFFERENT question" in killer["broader_query"]["answers"]


def test_F3_a_document_number_past_its_high_water_mark_is_not_a_mistake():
    """Regression coverage: test F3 a document number past its high water mark is not a mistake."""
    sql = (
        "select top 10 [PO].[PONum] as [P] from Erp.POHeader as [PO] "
        "where [PO].[PONum] = 99999999"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\) as \[n\] from Erp\.POHeader", [{"n": "0"}]),
            (r"min\(", [{"lo": "4127", "hi": "50000"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["likely_mistake"] is False
    assert "SEQUENCE" in killer["note"]


def test_F3_a_small_dense_code_set_still_accuses():
    """Regression coverage: test F3 a small dense code set still accuses."""
    sql = (
        "select top 10 [OD].[OrderNum] as [O] from Erp.OrderDtl as [OD] "
        "where [OD].[CustNum] = 10000"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\) as \[n\] from Erp\.OrderDtl", [{"n": "0"}]),
            (r"min\(", [{"lo": "2", "hi": "600"}]),
        ]
    )
    out = diagnose(sql, probe)
    assert out["killing_predicates"][0]["likely_mistake"] is True


def test_F5_a_correction_is_never_the_callers_own_value():
    """Regression coverage: test F5 a correction is never the callers own value."""
    from epicor_mcp.sql.diagnose_empty import _correction

    values = ["21", "34", "85", "97"]
    assert _correction("85", values, {}) is None
    assert _correction("Site 85", values, {}) == {
        "value": "85",
        "match_basis": "the digits in 'Site 85' are a real value",
    }


def test_F7_a_person_table_sample_reports_cardinality_not_names():
    """Regression coverage: test F7 a person table sample reports cardinality not names."""
    sql = (
        "select top 3 [E].[EmpID] as [E] from Erp.EmpBasic as [E] "
        "where [E].[Name] = 'ZZ-NO-SUCH-PERSON'"
    )
    names = ["SYNTHETIC PERSON A", "SYNTHETIC PERSON B", "SYNTHETIC PERSON C", "SYNTHETIC PERSON D"]
    probe = ScriptedProbe(
        [
            # `group by` FIRST: the enumeration probe's text also contains
            # `count(*) as [n] from Erp.EmpBasic`, so the count pattern would
            # otherwise answer it and the domain would come back as one null.
            (r"group by", [{"value": n, "n": "1"} for n in names] + [
                {"value": f"P{i}", "n": "1"} for i in range(30)
            ]),
            (r"count\(\*\) as \[n\] from Erp\.EmpBasic", [{"n": "0"}]),
        ]
    )
    out = diagnose(sql, probe)
    note = out["killing_predicates"][0]["note"]
    assert "more than 25 distinct values" in note
    for name in names:
        assert name not in note
    assert out["likely_mistake"] is False


def test_F11_the_enumerations_LABEL_column_is_deny_checked_too():
    """Regression coverage: test F11 the enumerations LABEL column is deny checked too."""
    import epicor_mcp.sql.diagnose_empty as de

    sql = "select top 10 [J].[JobNum] as [J] from Erp.JobHead as [J] where [J].[Plant] = 'X'"
    probe = ScriptedProbe([(r"count\(\*\) as \[n\] from Erp\.JobHead", [{"n": "0"}])])
    original = dict(de._DOMAIN_AUTHORITY)
    try:
        de._DOMAIN_AUTHORITY["plant"] = ("Erp.Plant", "Plant", "LaborRate")
        out = diagnose(sql, probe)
    finally:
        de._DOMAIN_AUTHORITY.clear()
        de._DOMAIN_AUTHORITY.update(original)
    killer = out["killing_predicates"][0]
    assert "denied" in killer["note"]
    assert not any("group by" in s for s in probe.seen)


def test_F10_a_cached_domain_expires_and_carries_its_measurement_time():
    """Regression coverage: test F10 a cached domain expires and carries its measurement time."""
    from epicor_mcp.sql.diagnose_empty import DomainCache

    cache = DomainCache(ttl_s=0.0001)
    cache.put("Erp.Plant", "Plant", "enumeration", {"values": ["21"]})
    stored = cache.get("Erp.Plant", "Plant", "enumeration")
    assert stored is not None and stored["measured_at"]
    time.sleep(0.002)
    assert cache.get("Erp.Plant", "Plant", "enumeration") is None
    assert cache.expired == 1


def test_the_JOIN_caveat_rides_with_every_retry_on_a_joined_statement():
    """Regression coverage: test the JOIN caveat rides with every retry on a joined statement."""
    sql = (
        "select top 100 [J].[JobNum] as [J] from Erp.JobHead as [J] "
        "join Erp.Customer as [C] on [C].[CustNum] = [J].[ProjectID] "
        "where [J].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [
            (
                r"group by",
                [
                    {"value": "21", "label": "Example Company - Oakridge Division",
                     "n": "180"},
                    {"value": "34", "label": "Example Company - Pinefield",
                     "n": "90"},
                ],
            ),
            (r"count\(\*\) as \[n\] from Erp\.JobHead", [{"n": "0"}]),
        ]
    )
    out = diagnose(sql, probe)
    assert "joins themselves were NOT tested" in out["message"]
    assert "not tested" in out["retry_with"]["scope"]


def test_an_OPEN_ENDED_bound_outside_the_data_is_a_FACT_not_an_accusation():
    """A future open-ended range can correctly return no rows.

    Report the observed range without labeling the caller's filter a mistake.
    """
    sql = (
        "select top 20 [OH].[OrderNum] as [O] from Erp.OrderHed as [OH] "
        "where [OH].[OrderDate] >= '2099-01-01'"
    )
    probe = ScriptedProbe(
        [
            (r"count\(\*\)", [{"n": "0"}]),
            (r"min\(", [{"lo": "2/3/2001 12:00:00 AM", "hi": "8/15/2030 12:00:00 AM"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["domain"]["kind"] == "range"
    assert killer["likely_mistake"] is False
    assert out["likely_mistake"] is False
    assert "2/3/2001" in killer["note"] and "outside" in killer["note"]
    assert "may well be the correct answer" in killer["note"]


def test_a_us_style_datetime_from_epicor_compares_as_a_DATE_not_as_text():
    """Regression coverage: test a us style datetime from epicor compares as a DATE not as text."""
    assert de._outside_range("2099-01-01", "2/3/2001 12:00:00 AM", "8/15/2030 12:00:00 AM")
    assert not de._outside_range("2020-05-01", "2/3/2001 12:00:00 AM", "8/15/2030 12:00:00 AM")
    # Unknown shapes are never guessed at.
    assert de._outside_range("banana", "apple", "cherry") is False


def test_a_negative_bound_is_read_as_a_number():
    """`< -1000000` parses as LT(Neg(Literal)); reading only the bare Literal
    made every negative bound invisible and sent it down the enumeration path."""
    _, conjuncts, _ = de._decompose(
        "select top 5 [P].[A] as [A] from Erp.PartWhse as [P] "
        "where [P].[OnHandQty] < -1000000",
        None,
    )
    assert conjuncts[0].literal == "-1000000"
    assert conjuncts[0].literal_is_numeric is True


def test_a_scalar_subquery_predicate_is_probed_WITH_its_subquery_intact():
    """Regression coverage: test a scalar subquery predicate is probed WITH its subquery intact."""
    sql = (
        "select top 100 [od].[PartNum] as [PN] from Erp.OrderDtl as [od] "
        "where [od].[CustNum] = (select top 1 [c].[CustNum] as [CustNum] "
        "from Erp.Customer as [c] where [c].[Name] = 'AcmeIndustrial')"
    )
    probe = ScriptedProbe(default=[{"n": "0"}])
    out = diagnose(sql, probe)
    assert out["killing_predicates"][0]["column"] == "Erp.OrderDtl.CustNum"
    assert len(probe.seen) >= 1
    assert "Erp.Customer" in probe.seen[0]
    assert probe.seen[0].startswith("select top ")


def test_a_probe_that_returns_an_unreadable_count_is_SKIPPED_not_believed():
    """A broken probe must never manufacture a `genuinely_empty` verdict — that
    would be a confident answer built on no measurement at all."""
    sql = (
        "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
        "where [P].[ClassID] = 'A' and [P].[TypeCode] = 'B'"
    )
    probe = ScriptedProbe(default=[{"nope": "?"}])
    out = diagnose(sql, probe)
    assert out["verdict"] == "undetermined"
    assert out["likely_mistake"] is False
    assert len(out["skipped_predicates"]) == 2


def test_the_domain_note_names_the_table_it_actually_MEASURED():
    """The Plant domain is read off `Erp.Plant`, so saying 'Erp.JobHead.Plant
    contains exactly these 9' would assert something no probe checked."""
    sql = (
        "select top 100 [JH].[JobNum] as [J] from Erp.JobHead as [JH] "
        "where [JH].[Plant] = 'Oakridge'"
    )
    probe = ScriptedProbe(
        [(r"where \[JH\]", [{"n": "0"}]), (r"from Erp\.Plant", _PLANT_ROWS)]
    )
    note = diagnose(sql, probe)["killing_predicates"][0]["note"]
    assert note.startswith("Erp.Plant.Plant (the authoritative list for Erp.JobHead.Plant)")


def test_the_AP_AR_pivot_is_MEASURED_and_never_flips_likely_mistake():
    """Regression coverage: test the AP AR pivot is MEASURED and never flips likely mistake."""
    sql = (
        "select top 100 [H].[InvoiceNum] as [I] from Erp.APInvHed as [H] "
        "where [H].[InvoiceNum] = 'INV-10001'"
    )
    probe = ScriptedProbe(
        [
            (r"from Erp\.APInvHed", [{"n": "0"}]),
            (r"from Erp\.InvcHead", [{"n": "1"}]),
        ]
    )
    out = diagnose(sql, probe)
    killer = out["killing_predicates"][0]
    assert killer["sibling_evidence"] == {
        "table": "Erp.InvcHead",
        "matching_rows": 1,
        "verified": True,
    }
    assert "has 1 row(s)" in killer["sibling_hint"]
    assert killer["likely_mistake"] is False
    assert out["likely_mistake"] is False
