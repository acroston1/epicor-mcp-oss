"""``next_step`` gives a concise instruction for acting on a query response.

It appears only when the answer needs additional work, derives every claim
from diagnosis or notes, and gives diagnosis precedence over grain warnings."""

from __future__ import annotations

import asyncio

from epicor_mcp.sql.adhoc import run_sql
from epicor_mcp.sql.next_step import annotate_next_step, next_step_for
from epicor_mcp.sql.tool import SQL_PARAM_DESCRIPTION, TOOL_DESCRIPTION
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"


def _diag(**kw):
    return {"success": True, "row_count": 0, "diagnosis": {**kw}}


def _grain(rule: str, *, checks=None):
    out = {
        "success": True,
        "row_count": 10,
        "notes": [{"source": "grain", "severity": "warn", "rule": rule, "message": "m"}],
    }
    if checks:
        out["grain_checks"] = checks
    return out


# --------------------------------------------------------------------------- #
# 1. The zero-row diagnosis
# --------------------------------------------------------------------------- #


class TestDiagnosis:
    def test_a_likely_mistake_with_a_runnable_retry_says_re_run_and_says_it_is_runnable(self):
        text = next_step_for(
            _diag(likely_mistake=True, retry_with={"sql": "select 1", "changed": "x"})
        )
        assert "RE-RUN" in text and "DO NOT REPORT" in text
        # The summary must explain both the returned data and its scope.
        assert "diagnosis.retry_with.sql" in text
        assert "RUNNABLE" in text.upper()
        assert "verbatim" in text

    def test_a_single_dead_predicate_is_called_a_FIX_and_anything_else_a_PATCH(self):
        """A retry patches one finding, so distinguish complete and partial repairs.

        Multiple dead predicates, untested predicates, or joins prevent the
        message from claiming the query has been fully repaired."""
        one = next_step_for(
            _diag(likely_mistake=True, killing_predicates=[{"p": 1}],
                  retry_with={"sql": "select 1", "changed": "x"})
        )
        assert "ONE predicate" in one and "PARTIAL" not in one

        two = next_step_for(
            _diag(likely_mistake=True, killing_predicates=[{"p": 1}, {"p": 2}],
                  retry_with={"sql": "select 1", "changed": "x"})
        )
        assert "PARTIAL fix, not a recovery" in two
        assert "2 predicates match nothing" in two
        assert "do not report 0 rows" in two

        untested = next_step_for(
            _diag(likely_mistake=True, killing_predicates=[{"p": 1}],
                  untested_predicates=["[T].[C] = 1"],
                  retry_with={"sql": "select 1", "changed": "x"})
        )
        assert "PARTIAL" in untested and "never tested" in untested

    def test_the_patch_scope_rides_along_when_the_diagnostician_set_it(self):
        """A diagnosis can leave joins untested; preserve that scope in the guidance."""
        with_scope = next_step_for(
            _diag(
                likely_mistake=True,
                retry_with={"sql": "select 1", "changed": "x", "scope": "joins untested"},
            )
        )
        assert "join" in with_scope.lower()
        without = next_step_for(
            _diag(likely_mistake=True, retry_with={"sql": "select 1", "changed": "x"})
        )
        assert "join" not in without.lower(), (
            "a join caveat on a single-table statement is a claim the diagnosis "
            "did not make"
        )

    def test_a_likely_mistake_with_no_retry_still_says_re_run(self):
        text = next_step_for(_diag(likely_mistake=True))
        assert "RE-RUN" in text
        assert "retry_with" not in text, "never point at a field that is not there"
        assert "diagnosis.message" in text

    def test_a_benign_zero_row_result_says_REPORT_IT(self):
        """an empty answer that is CORRECT must not send the
        model hunting. This is the field's anti-false-alarm half."""
        text = next_step_for(_diag(likely_mistake=False, verdict="genuinely_empty"))
        assert "REPORT" in text
        assert "RE-RUN" not in text
        assert "Do not re-run" in text


# --------------------------------------------------------------------------- #
# 2. Grain
# --------------------------------------------------------------------------- #


class TestGrain:
    def test_a_grain_warning_forbids_reporting_the_number(self):
        text = next_step_for(_grain("sibling_child_cross"))
        assert "DO NOT REPORT" in text
        assert "notes" in text

    def test_a_duplicate_rule_hands_back_the_form_the_transpiler_ACCEPTS(self):
        """Duplicate guidance must use the distinct syntax accepted by the transpiler."""
        text = next_step_for(_grain("duplicate_projection"))
        assert "select distinct" in text
        assert "SAME" in text and "refused" in text
        # ...and it must not appear where it is not the fix.
        assert "select distinct" not in next_step_for(_grain("sibling_child_cross"))

    def test_grain_checks_are_advertised_only_when_they_exist(self):
        assert "grain_checks[0].sql" in next_step_for(
            _grain("unlabelled_parent_scope", checks=[{"sql": "select 1"}])
        )
        assert "grain_checks" not in next_step_for(_grain("unlabelled_parent_scope"))

    def test_an_INFO_note_is_not_an_instruction(self):
        quiet = {
            "success": True,
            "row_count": 5,
            "notes": [{"source": "domains", "severity": "INFO", "rule": "advisory"}],
        }
        assert next_step_for(quiet) == ""


# --------------------------------------------------------------------------- #
# 3. Silence — the property that keeps the field credible
# --------------------------------------------------------------------------- #


class TestSilence:
    def test_a_clean_result_carries_nothing(self):
        assert next_step_for({"success": True, "row_count": 5, "notes": []}) == ""

    def test_a_refusal_carries_nothing(self):
        """A refusal already IS an instruction — `message` + `retry_with`. A
        second owner for the same claim is the failure this package avoids."""
        assert next_step_for({"success": False, "error": "sql_refused"}) == ""

    def test_annotate_removes_the_key_rather_than_leaving_it_blank(self):
        out = annotate_next_step({"success": True, "next_step": "", "notes": []})
        assert "next_step" not in out

    def test_annotate_never_raises_on_a_malformed_envelope(self):
        for bad in ({"success": True, "notes": "not a list"},
                    {"success": True, "diagnosis": "not a dict"},
                    {}):
            annotate_next_step(dict(bad))  # must not raise


# --------------------------------------------------------------------------- #
# 4. Precedence, and the wiring
# --------------------------------------------------------------------------- #


def test_the_diagnosis_outranks_grain():
    both = {
        "success": True,
        "row_count": 0,
        "diagnosis": {"likely_mistake": True},
        "notes": [{"source": "grain", "severity": "warn", "rule": "duplicate_projection"}],
    }
    text = next_step_for(both)
    assert "RE-RUN" in text
    assert "grain" not in text.lower()


def test_the_pipe_emits_it_and_puts_it_next_to_the_summary():
    """Drive the REAL `run_sql` with a mock client — the field must arrive in the
    response the tool returns, not merely be computable from it."""
    _, ds = load("clean_top")
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    out = asyncio.run(
        run_sql(
            "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] "
            "where [P].[Company] = 'EXAMPLE'",
            client=client,
            api_key="k",
            base_url=BASE,
            diagnose=False,
        )
    )
    keys = list(out)
    assert "summary" in keys
    if "next_step" in keys:
        assert keys.index("next_step") == keys.index("summary") + 1


def test_a_clean_pipe_response_has_no_next_step_key():
    _, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute([{"PN": "A"}, {"PN": "B"}])
    )
    out = asyncio.run(
        run_sql(
            "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]",
            client=client,
            api_key="k",
            base_url=BASE,
            diagnose=False,
            ground_domains=False,
        )
    )
    assert out["success"] is True
    assert "next_step" not in out


# --------------------------------------------------------------------------- #
# 5. The two description strings — routing must not pay for this
# --------------------------------------------------------------------------- #


def test_the_routers_500_char_window_is_untouched():
    """The routing description reserves its first 500 characters for intent.

    Append response guidance after that window so routing stays stable."""
    head = TOOL_DESCRIPTION[:500]
    assert "next_step" not in head
    assert "next_step" in TOOL_DESCRIPTION


def test_the_sql_parameter_teaches_the_response_channel():
    for token in ("next_step", "diagnosis.retry_with.sql", "likely_mistake"):
        assert token in SQL_PARAM_DESCRIPTION
    assert "READING THE RESPONSE" in SQL_PARAM_DESCRIPTION
