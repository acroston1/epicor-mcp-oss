"""Regression coverage: test pipe integration."""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.sql import denylist, domains as domainsmod
from epicor_mcp.sql.adhoc import EXECUTE_PATH, PARSE_PATH, _safe_catalogue, run_sql
from epicor_mcp.sql.validate_columns import load_catalogue, validate_columns
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"

#: A statement whose columns all exist and whose WHERE carries exactly one E15a

#: statements of which compared `Company` to `'EXAMPLE'`.
EXAMPLE = (
    "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
    "where [P].[TypeCode] = 'UNKNOWN'"
)
#: The same finding under an OR, so it is true but cannot govern the result.
EXAMPLE_OR = (
    "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
    "where [P].[TypeCode] = 'UNKNOWN' or [P].[PartNum] = 'X'"
)


@pytest.fixture(autouse=True)
def configured_observations(monkeypatch):
    domain = domainsmod.Domain('Part','TypeCode',('M','P'),(7,3),True)
    monkeypatch.setattr(domainsmod,'DOMAINS',{domain.key:domain})
    monkeypatch.setattr(domainsmod,'ALWAYS_FALSE',{'Customer':frozenset({'Inactive'})})
    monkeypatch.setattr(domainsmod,'TABLE_ROWS',{'Customer':10})


def call(sql: str, rows: list[dict] | None = None, *, fixture: str = "clean_top", **kw) -> dict:
    out, _ = call_with_client(sql, rows, fixture=fixture, **kw)
    return out


def call_with_client(
    sql: str, rows: list[dict] | None = None, *, fixture: str = "clean_top", **kw
) -> tuple[dict, MockEpicorClient]:
    _, ds = load(fixture)
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute(rows or []))
    out = asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))
    return out, client


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #


class TestDeniedTableSchemaLeak:
    """E14 must not become a pre-authorization schema oracle.

    The deny-list reads Epicor's own resolved tableset, so it CANNOT run before
    ParseFromSQL. E14's entire value is that it runs before ParseFromSQL. That
    gap is a real one and the shipped catalogue lands in it.
    """

    def test_the_catalogue_really_does_contain_a_denied_table(self):
        """The premise, asserted rather than assumed — if this ever stops being
        true the filter below is still correct, but this test should say so."""
        base = load_catalogue()
        denied = [t for t in base.tables if denylist.is_denied_table(t)]
        assert "UserFile" in denied, (
            "the leak this filter exists for is gone; keep the filter, but the "
            "evidence sentence in _safe_catalogue() needs re-dating"
        )

    def test_the_unfiltered_validator_would_serve_that_tables_schema(self):
        """Regression coverage: test the unfiltered validator would serve that tables schema."""
        result = validate_columns(
            "select top 5 [U].[ZzNope] as [X] from UserFile as [U]",
            catalogue=load_catalogue(),
        )
        assert result.ok is False
        served = result.envelope["valid"]["columns_by_table"]["UserFile"]
        assert len(served) >= 50
        for column in ("AdvBAQRights", "AllowMultipleSessions", "CanCustomize"):
            assert column in served, f"expected the unfiltered leak to include {column}"

    def test_the_pipes_catalogue_has_the_denied_tables_removed(self):
        safe = _safe_catalogue()
        assert not any(denylist.is_denied_table(t) for t in safe.tables)
        assert "UserFile" not in safe.tables
        assert safe.column_names("UserFile") == []

    def test_the_filtered_catalogue_keeps_the_types_that_veto_a_correction(self):
        """`excluding` must copy, not rebuild from names. The `bit` type is what
        stops `OnHandQty` being auto-corrected to `HasOnHandQty` at difflib
        0.857 — the legacy six-dead-end bug."""
        base = load_catalogue()
        safe = _safe_catalogue()
        typed = [
            (t, c)
            for t in safe.tables
            for c in safe.column_names(t)
            if base.column_type(t, c)
        ]
        assert typed, "no types survived the filter"
        table, column = typed[0]
        assert safe.column_type(table, column) == base.column_type(table, column)

    def test_a_phantom_on_a_denied_table_leaks_nothing_and_refuses_at_the_denylist(self):
        """E14 ABSTAINS (the table is not in its catalogue) and the statement
        falls through to Epicor's own resolution, where the deny-list refuses
        it. Nothing about the table's schema is served on the way."""
        out, client = call_with_client(
            "select top 5 [U].[ZzNope] as [X] from Ice.UserFile as [U]",
            fixture="deny_ice_userfile",
        )
        assert out["error"] == "table_access_denied"
        assert out["detail"]["stage"] == "denylist"
        body = json.dumps(out)
        # Distinctive names, so a hit is a leak and not an English word inside
        # the denial's own prose (which does mention `FirstName`/`LastName` while
        # explaining which person columns ARE readable).
        # (`SecurityMgr` is deliberately absent: it is a real UserFile column AND
        # a phrase the denial's own message uses — "including an Epicor
        # SecurityMgr principal" — so it cannot discriminate.)
        leakable = (
            "AdvBAQRights", "AllowMultipleSessions", "CanCustomize",
            "BPMAdvancedUser", "DspPayrollMgr", "PwdExpires", "GroupList",
        )
        catalogued = load_catalogue().column_names("UserFile")
        assert set(leakable) <= set(catalogued), "these must be real UserFile columns"
        for column in leakable:
            assert column not in body, f"leaked {column} from a denied table"
        assert not client.called(EXECUTE_PATH)

    def test_column_lives_on_never_names_a_denied_table(self):
        """The quieter half of the same leak: `lives_on` is a reverse index, so
        an unknown column on a perfectly ordinary table used to be answered with
        *"it lives on UserFile"*."""
        base = load_catalogue()
        shared = [
            c
            for c in base.column_names("UserFile")
            if len(base.lives_on(c)) > 1 and "UserFile" in base.lives_on(c)
        ]
        assert shared, "expected UserFile to share at least one column name"
        safe = _safe_catalogue()
        for column in shared:
            assert "UserFile" not in safe.lives_on(column)


# --------------------------------------------------------------------------- #
# E14 in the pipe
# --------------------------------------------------------------------------- #


class TestColumnValidationStage:
    def test_a_phantom_column_costs_zero_epicor_calls(self):
        """Regression coverage: test a phantom column costs zero epicor calls."""
        out, client = call_with_client(
            "select top 5 [P].[ZzPhantomCol] as [X] from Erp.Part as [P]"
        )
        assert out["success"] is False
        assert out["error"] == "sql_unknown_column"
        assert out["detail"]["stage"] == "validate_columns"
        assert out["detail"]["epicor_calls"] == 0
        assert client.calls == []

    def test_it_abstains_on_a_cte_output_and_the_query_runs(self):
        """Regression coverage: test it abstains on a cte output and the query runs."""
        sql = (
            "with [c] as (select [P].[PartNum] as [PartNum], count(*) as [N] "
            "from Erp.Part as [P] group by [P].[PartNum]) "
            "select top 5 [c].[PartNum] as [PN], [c].[N] as [N] from [c]"
        )
        out, client = call_with_client(sql, [{"PN": "A", "N": "1"}])
        assert out["success"] is True
        assert client.called(PARSE_PATH)

    def test_turning_it_off_puts_the_statement_back_on_the_epicor_path(self):
        out, client = call_with_client(
            "select top 5 [P].[ZzPhantomCol] as [X] from Erp.Part as [P]",
            validate_columns=False,
        )
        assert client.called(PARSE_PATH)


# --------------------------------------------------------------------------- #
# One coherent envelope
# --------------------------------------------------------------------------- #


class TestOneCoherentEnvelope:
    def test_every_note_names_the_detector_that_produced_it(self):
        """`lint.Finding.to_dict()` and `grain.GrainFinding.to_dict()` are
        byte-identical in shape, so without `source` a claim derived from
        Epicor's own parsed tableset is indistinguishable from one derived from
        our AST — and those have very different standing."""
        sql, _ = load("gov_fanout")
        out = call(sql, [{"J": "100001", "Q": "5"}], fixture="gov_fanout")
        assert out["notes"], "expected this fixture to produce notes"
        assert all(n["source"] in {"lint", "grain", "domains"} for n in out["notes"])

    def test_notes_are_ordered_lint_then_grain_then_domains(self):
        """Regression coverage: test notes are ordered lint then grain then domains."""
        sql, _ = load("gov_fanout")
        out = call(sql, [{"J": "100001", "Q": "5"}], fixture="gov_fanout")
        rank = {"lint": 0, "grain": 1, "domains": 2}
        seen = [rank[n["source"]] for n in out["notes"]]
        assert seen == sorted(seen)
        assert out["notes"][0]["rule"] == "aggregate_fanout"

    def test_a_finding_the_returned_rows_refute_is_dropped_not_served(self):
        """`Company = 'EXAMPLE'` in a top-level AND chain cannot return rows if
        the catalogue is right. One row came back, so the catalogue is stale —
        and serving "this matches nothing" above a row that matched teaches the
        model to distrust the whole notes channel."""
        out = call(EXAMPLE, [{"PN": "ABC-1"}])
        assert out["success"] is True
        assert out["notes"] == []
        assert "EXAMPLE" not in out["summary"]

    def test_the_same_finding_under_an_or_is_served_as_info(self):
        """It is still true — it just cannot govern the result — so it is
        reported, and reported as INFO rather than WARN."""
        out = call(EXAMPLE_OR, [{"PN": "ABC-1"}])
        note = out["notes"][0]
        assert note["source"] == "domains"
        assert note["severity"] == "INFO"
        assert note["rule"] == "value_outside_an_epicor_code_set"
        assert note["detail"]["as_of"] == domainsmod.AS_OF

    def test_a_domain_note_never_touches_success_complete_or_terminal(self):
        out = call(EXAMPLE_OR, [{"PN": "ABC-1"}])
        assert out["success"] is True
        assert out["complete"] is True
        assert out["terminal"] is True
        assert out["row_count"] == 1


# --------------------------------------------------------------------------- #
# The zero-row channel has exactly one owner
# --------------------------------------------------------------------------- #


class TestZeroRowChannel:
    def test_static_findings_are_folded_into_the_diagnosis_not_into_notes(self):
        """Two answers to "why is this empty" is the failure this wiring exists
        to prevent. E15a rides inside E15b's annotation or not at all."""
        out = call(EXAMPLE, [])
        assert out["notes"] == []
        assert "diagnosis" in out
        assert out["diagnosis"]["static_evidence"][0]["column"] == "Part.TypeCode"

    def test_the_folded_evidence_is_named_in_the_one_message(self):
        """`message` said "the cause is not established" while `static_evidence`
        beside it named the cause. One object, two answers — resolved by putting
        the lead in the message and labelling it a dated snapshot."""
        out = call(EXAMPLE, [])
        message = out["diagnosis"]["message"]
        assert "DATED SNAPSHOT" in message
        assert "M" in message

    def test_static_evidence_can_never_raise_likely_mistake_or_move_terminal(self):
        """Regression coverage: test static evidence can never raise likely mistake or move terminal."""
        out = call(EXAMPLE, [])
        assert out["diagnosis"]["likely_mistake"] is False
        assert out["terminal"] is True
        assert out["success"] is True
        assert out["row_count"] == 0

    def test_a_live_measurement_beats_the_snapshot_and_the_clash_is_published(self):
        """Regression coverage: test a live measurement beats the snapshot and the clash is published."""
        from epicor_mcp.sql.adhoc import _fold_static_into_diagnosis

        diagnosis = {
            "verdict": "genuinely_empty",
            "likely_mistake": False,
            "message": "0 rows, and this is the correct answer.",
            "killing_predicates": [],
            "satisfied_predicates": ["[Part].[TypeCode] = 'UNKNOWN'"],
        }
        findings = domainsmod.ground(EXAMPLE, row_count=0)
        _fold_static_into_diagnosis(diagnosis, findings)
        assert "static_evidence" not in diagnosis
        stale = diagnosis["static_catalogue_stale"]
        assert stale[0]["column"] == "Part.TypeCode"
        assert "live" in stale[0]["superseded_by"]
        assert diagnosis["likely_mistake"] is False
        assert diagnosis["verdict"] == "genuinely_empty"

    def test_corroboration_is_dropped_rather_than_repeated(self):
        """The probe already named the column, with a measurement behind it.
        Saying it twice in two vocabularies is noise, not confirmation."""
        from epicor_mcp.sql.adhoc import _fold_static_into_diagnosis

        diagnosis = {
            "verdict": "killing_predicate",
            "likely_mistake": True,
            "message": "0 rows, and the cause is identified.",
            "killing_predicates": [
                {"predicate": "[Part].[TypeCode] = 'UNKNOWN'", "column": "Part.TypeCode"}
            ],
            "satisfied_predicates": [],
        }
        _fold_static_into_diagnosis(diagnosis, domainsmod.ground(EXAMPLE, row_count=0))
        assert "static_evidence" not in diagnosis
        assert "static_catalogue_stale" not in diagnosis
        assert diagnosis["message"] == "0 rows, and the cause is identified."

    def test_with_the_diagnostician_off_the_snapshot_is_served_as_notes(self):
        """A dated lead is better than silence, but it stays a note and the
        answer stays terminal."""
        out = call(EXAMPLE, [], diagnose=False)
        assert "diagnosis" not in out
        assert out["notes"][0]["source"] == "domains"
        assert out["notes"][0]["severity"] == "WARN"
        assert out["terminal"] is True

    def test_an_empty_later_page_is_a_paging_artefact_not_a_domain_problem(self):
        """`empty_later_page` already owns that explanation; a value note there
        would be a second, competing one about a page that cannot hold rows."""
        out = call(
            "select top 500 [P].[PartNum] as [PN] from Erp.Part as [P] "
            "where [P].[TypeCode] = 'UNKNOWN'",
            [],
            page_size=200,
            page_num=2,
        )
        assert out["summary"].startswith("EMPTY PAGE")
        assert "diagnosis" not in out
        assert out["notes"] == []


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #


class TestHappyPathCost:
    def test_the_happy_path_still_makes_exactly_two_epicor_calls(self):
        sql, _ = load("clean_top")
        out, client = call_with_client(sql, [{"PN": "ABC-1"}])
        assert out["success"] is True
        assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]

    def test_both_new_detectors_off_produces_an_identical_response(self):
        """Not "similar" — identical, once the wall-clock fields are removed.
        A detector that changes the shape of a clean answer is not free."""
        sql, _ = load("clean_top")
        volatile = ("elapsed_s", "parse_ms", "execute_ms", "stage_ms")
        on = call(sql, [{"PN": "ABC-1"}])
        off = call(sql, [{"PN": "ABC-1"}], validate_columns=False, ground_domains=False)
        for key in volatile:
            on.pop(key, None)
            off.pop(key, None)
        assert on == off

    def test_stage_ms_is_absent_when_every_local_stage_is_sub_millisecond(self):
        """The timings exist so the latency budget stays auditable in
        production, not so the model reads five zeros. Anything under 1 ms is
        not recorded, so a warm happy path carries no `stage_ms` at all."""
        sql, _ = load("clean_top")
        call(sql, [{"PN": "ABC-1"}])  # warm the catalogue and the sqlglot caches
        out = call(sql, [{"PN": "ABC-1"}])
        assert set(out.get("stage_ms", {})) <= {
            "transpile", "validate_columns", "lint", "governor", "grain", "ground"
        }
        assert all(v >= 1.0 for v in out.get("stage_ms", {}).values())


# --------------------------------------------------------------------------- #
# Every stage names itself, and every gate returns
# --------------------------------------------------------------------------- #


class TestStageAttribution:
    @pytest.mark.parametrize(
        "fixture,stage",
        [
            ("top_percent", "transpile"),
            ("deny_ice_userfile", "denylist"),
            ("select_star", "lint"),
            ("gov_company_only_join", "governor"),
        ],
    )
    def test_each_refusal_names_the_gate_that_produced_it(self, fixture, stage):
        sql, _ = load(fixture)
        out = call(sql, fixture=fixture)
        assert out["success"] is False
        assert out["detail"]["stage"] == stage

    def test_the_column_validator_names_itself_too(self):
        out = call("select top 5 [P].[ZzPhantomCol] as [X] from Erp.Part as [P]")
        assert out["detail"]["stage"] == "validate_columns"

    def test_a_refusal_never_carries_the_advisory_channels(self):
        """A refusal is mutually exclusive with a result. Mixing an INV-1
        envelope with `notes`/`diagnosis` would hand the model an error and an
        answer in the same object."""
        out = call("select top 5 [P].[ZzPhantomCol] as [X] from Erp.Part as [P]")
        assert out["success"] is False
        for key in ("notes", "diagnosis", "grain_checks", "rows", "row_count"):
            assert key not in out


# --------------------------------------------------------------------------- #
# The invariant that overrides everything else here
# --------------------------------------------------------------------------- #


class TestAnEmptyResultIsSometimesTheTrueAnswer:
    @pytest.mark.parametrize("diagnose", [True, False])
    @pytest.mark.parametrize("ground_domains", [True, False])
    @pytest.mark.parametrize("validate_columns", [True, False])
    def test_no_configuration_turns_an_empty_result_into_an_error(
        self, diagnose, ground_domains, validate_columns
    ):
        """Sixteen combinations over two statements — one that E15a has an
        opinion about and one it does not. None of them may refuse, raise, or
        report anything but a successful zero-row answer."""
        for sql in (EXAMPLE, "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"):
            out = call(
                sql,
                [],
                diagnose=diagnose,
                ground_domains=ground_domains,
                validate_columns=validate_columns,
            )
            assert out["success"] is True
            assert out["row_count"] == 0
            assert out["complete"] is True
            assert "error" not in out

    def test_an_ordinary_empty_result_says_nothing_at_all(self):
        """Regression coverage: test an ordinary empty result says nothing at all."""
        out = call(
            "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
            "where [P].[PartNum] = 'NO-SUCH-PART-12345'",
            [],
            diagnose=False,
        )
        assert out["notes"] == []
        assert out["terminal"] is True
        assert out["success"] is True


# --------------------------------------------------------------------------- #

# not outrank the completeness sentence
# --------------------------------------------------------------------------- #


def test_F8_the_catalogue_denial_set_is_exactly_what_is_expected():
    """Regression coverage: test F8 the catalogue denial set is exactly what is expected."""
    from epicor_mcp.sql.adhoc import EXPECTED_CATALOGUE_DENIALS, _safe_catalogue

    base = load_catalogue()
    dropped = {t for t in base.tables if denylist.is_denied_table(t)}
    assert dropped == set(EXPECTED_CATALOGUE_DENIALS), (
        "the deny-list now removes a different set of catalogue tables. Either "
        "add them to EXPECTED_CATALOGUE_DENIALS with a reason, or narrow the "
        "pattern — E14 abstains on every one of them."
    )
    assert len(_safe_catalogue().tables) == len(base.tables) - len(dropped)


def test_F8_a_new_collision_is_logged_rather_than_silently_swallowed(caplog, monkeypatch):
    """The whole point of F8: the loss must reach a human."""
    import logging

    import epicor_mcp.sql.adhoc as adhocmod

    monkeypatch.setattr(adhocmod, "_SAFE_CATALOGUE", None)
    monkeypatch.setattr(
        adhocmod.denylist, "is_denied_table", lambda t: t in {"UserFile", "Part"}
    )
    with caplog.at_level(logging.WARNING, logger="epicor_mcp.sql.adhoc"):
        adhocmod._safe_catalogue()
    monkeypatch.setattr(adhocmod, "_SAFE_CATALOGUE", None)
    assert any("Part" in r.getMessage() for r in caplog.records)


def test_F9_an_INFO_domain_note_does_not_take_over_the_summary():
    """Regression coverage: test F9 an INFO domain note does not take over the summary."""
    sql = (
        "select top 3 count(*) as [Total], "
        "sum(case when [C].[Inactive] = true then 1 else 0 end) as [InactiveRows] "
        "from Erp.Customer as [C]"
    )
    _, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute([{"Total": "447", "InactiveRows": "0"}])
    )
    out = asyncio.run(
        run_sql(sql, client=client, api_key="k", base_url=BASE, diagnose=False)
    )
    assert out["summary"].startswith("1 row(s) — a partial page")
    # The advisory is still SERVED, just not as the headline.
    assert any(n["source"] == "domains" for n in out["notes"])
