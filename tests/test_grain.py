"""Grain and fan-out detection with offline regression fixtures.

The suite covers inflated aggregates, material rows spanning multiple jobs,
and parent measures repeated by child joins. Correct join shapes are checked
individually to prevent false-positive warnings. No live Epicor calls occur."""

from __future__ import annotations

import pytest

from epicor_mcp.sql.grain import (
    GrainFinding,
    Severity,
    analyse_grain,
    apply_verification,
    candidate_keys,
    duplicate_collapse,
)

# --------------------------------------------------------------------------- #
# Regression statement shapes
# --------------------------------------------------------------------------- #

#: Aggregate a job measure across a child relation.
B16_SQL = """select top 100
    [JH].[JobNum] as [JobNum],
    [JH].[PartNum] as [PartNum],
    sum([ID].[ExtPrice]) as [Revenue],
    sum([ID].[ExtPrice] - ([PT].[MtlUnitCost] + [PT].[LbrUnitCost] + [PT].[SubUnitCost] + [PT].[MtlBurUnitCost]) * [PT].[TranQty]) as [DirectMargin]
from Erp.JobHead as [JH]
inner join Erp.JobMtl as [JM] on [JH].[Company] = [JM].[Company] and [JH].[JobNum] = [JM].[JobNum]
inner join Erp.PartTran as [PT] on [JM].[Company] = [PT].[Company] and [JM].[PartNum] = [PT].[PartNum] and [JM].[JobNum] = [PT].[JobNum] and [JM].[AssemblySeq] = [PT].[AssemblySeq] and [JM].[MtlSeq] = [PT].[JobSeq]
inner join Erp.InvcDtl as [ID] on [JH].[Company] = [ID].[Company] and [JH].[PartNum] = [ID].[PartNum]
where [PT].[TranDate] >= dateadd(month, -3, getdate())
group by [JH].[JobNum], [JH].[PartNum]"""

#: Material rows selected across jobs for a part.
B18_SQL = """select top 100 [JM].[PartNum] as [ComponentPartNum],
       [JM].[Description] as [ComponentDescription],
       [JM].[QtyPer] as [QtyPerParent],
       [JM].[IUM] as [UnitOfMeasure],
       [JM].[RequiredQty] as [RequiredQty],
       [JM].[RevisionNum] as [RevisionNum]
from Erp.JobHead as [JH]
inner join Erp.JobMtl as [JM] on [JH].[Company] = [JM].[Company] and [JH].[JobNum] = [JM].[JobNum]
where [JH].[PartNum] = 'PART-200'
order by [JM].[MtlSeq] asc"""

#: The same grain defect with `jh.JobNum` PROJECTED.
#: It is why the "no parent label" condition is a message variant, not a gate.
B18_LABELLED_SQL = """SELECT
    jh.Company AS Company,
    jh.JobNum AS JobNum,
    jm.MtlSeq AS MtlSeq,
    jm.PartNum AS ComponentPartNum,
    jm.RequiredQty AS RequiredQty
FROM Erp.JobHead AS jh
    INNER JOIN Erp.JobMtl AS jm ON jh.Company = jm.Company AND jh.JobNum = jm.JobNum
WHERE jh.PartNum = 'PART-200' AND jh.JobEngineered = 1
ORDER BY jm.MtlSeq"""

#: A complete job key restricts the method to one parent.
#: A single job key restricts the material recipe.
B18_CORRECT_SQL = """select top 100 [JM].[PartNum] as [ComponentPartNum],
       [JM].[RequiredQty] as [RequiredQty]
from Erp.JobHead as [JH]
inner join Erp.JobMtl as [JM] on [JH].[Company] = [JM].[Company] and [JH].[JobNum] = [JM].[JobNum]
where [JH].[JobNum] = 'JOB-101'
order by [JM].[MtlSeq] asc"""

#: A header measure is multiplied by its matching child rows.
S4_SQL = (
    "select sum([OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
    "inner join Erp.OrderDtl as [OD] "
    "on [OH].[Company] = [OD].[Company] and [OH].[OrderNum] = [OD].[OrderNum] "
    "where [OH].[OrderNum] = 1001"
)

#: DMRHead joined to two independent children.
B23_SQL = (
    "select top 100 [DMR].[DMRNum] as [D], [A].[Quantity] as [Q], [L].[ScrapQty] as [S] "
    "from Erp.DMRHead as [DMR] "
    "inner join Erp.DMRActn as [A] on [DMR].[Company] = [A].[Company] "
    "and [DMR].[DMRNum] = [A].[DMRNum] "
    "inner join Erp.LaborDtl as [L] on [DMR].[Company] = [L].[Company] "
    "and [DMR].[JobNum] = [L].[JobNum] where [DMR].[JobNum] = 'JOB-102'"
)

#: the join is a filter, the projection is one table, no DISTINCT.
B08_SQL = (
    "select top 100 [PartRev].[PartNum] as [PartNum], "
    "[PartRev].[RevisionNum] as [RevisionNum] from Erp.PartRev as [PartRev] "
    "inner join Erp.OrderDtl as [OrderDtl] on [PartRev].[Company] = [OrderDtl].[Company] "
    "and [PartRev].[PartNum] = [OrderDtl].[PartNum] "
    "inner join Erp.Customer as [Customer] on [OrderDtl].[Company] = [Customer].[Company] "
    "and [OrderDtl].[CustNum] = [Customer].[CustNum] "
    "where [Customer].[CustID] = 'EXAMPLE CUSTOMER' and [PartRev].[Approved] = 1"
)


def rules(sql: str) -> list[str]:
    return analyse_grain(sql).rules


# --------------------------------------------------------------------------- #
# The key dictionary
# --------------------------------------------------------------------------- #


class TestKeyDictionary:
    def test_the_dictionary_loaded(self):
        assert candidate_keys("Erp.OrderDtl") == (frozenset({"company", "ordernum", "orderline"}),)

    def test_lookup_ignores_schema_and_case(self):
        assert candidate_keys("JobHead") == candidate_keys("Erp.JobHead") == candidate_keys("erp.jobhead")

    def test_an_unknown_table_is_unjudgeable_not_fanning(self):
        """A missing key definition is not evidence that the join fans out."""
        assert candidate_keys("Erp.NoSuchTableAnywhere") == ()
        sql = (
            "select sum([A].[Qty]) as [Q] from Erp.NoSuchTableAnywhere as [A] "
            "inner join Erp.AlsoNotReal as [B] on [A].[Company] = [B].[Company]"
        )
        report = analyse_grain(sql)
        assert report.findings == ()
        assert report.unjudgeable  # ...and the residual is COUNTED, not hidden


# --------------------------------------------------------------------------- #
# aggregate_fanout
# --------------------------------------------------------------------------- #


class TestAggregateFanout:
    def test_b16_is_flagged(self):
        assert "aggregate_fanout" in rules(B16_SQL)

    def test_b16_names_the_table_that_multiplies_and_the_key_it_leaves_free(self):
        f = next(f for f in analyse_grain(B16_SQL).findings if f.rule == "aggregate_fanout")
        tables = {m["table"] for m in f.detail["multiplies_by"]}
        assert "Erp.JobMtl" in tables
        gaps = {
            c
            for m in f.detail["multiplies_by"]
            for c in m["key_columns_left_free"]
        }
        # Naming the FREE key column is the whole recovery: it is what tells the
        # caller which predicate is missing.
        assert {"AssemblySeq", "MtlSeq"} & gaps

    def test_s4_the_known_answer_case_is_flagged(self):
        f = next(f for f in analyse_grain(S4_SQL).findings if f.rule == "aggregate_fanout")
        assert f.detail["measure_tables"] == ["Erp.OrderHed"]
        assert f.detail["multiplies_by"][0]["table"] == "Erp.OrderDtl"
        assert f.detail["multiplies_by"][0]["key_columns_left_free"] == ["OrderLine"]

    def test_the_child_side_measure_is_NOT_flagged(self):
        """`sum(child)` over header and child preserves the child measure."""
        sql = (
            "select sum([OD].[ExtPriceDtl]) as [T] from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and [OH].[OrderNum] = [OD].[OrderNum] where [OH].[OrderNum] = 1001"
        )
        assert rules(sql) == []

    def test_a_child_to_master_rollup_is_NOT_flagged(self):
        """A child-to-master rollup follows functional dependencies.

        Once an OrderHed row is determined, its CustNum can determine the
        joined Customer even when no predicate names CustNum directly."""
        sql = (
            "select top 100 [C].[Name] as [N], sum([OD].[OrderQty]) as [Q] "
            "from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and [OH].[OrderNum] = [OD].[OrderNum] "
            "inner join Erp.Customer as [C] on [OH].[Company] = [C].[Company] "
            "and [OH].[CustNum] = [C].[CustNum] group by [C].[Name]"
        )
        assert rules(sql) == []

    def test_a_three_level_chain_is_NOT_flagged(self):
        sql = (
            "select top 100 [JH].[JobNum] as [J], sum([JO].[QtyCompleted]) as [Q] "
            "from Erp.JobHead as [JH] "
            "inner join Erp.JobAsmbl as [JA] on [JH].[Company] = [JA].[Company] "
            "and [JH].[JobNum] = [JA].[JobNum] "
            "inner join Erp.JobOper as [JO] on [JA].[Company] = [JO].[Company] "
            "and [JA].[JobNum] = [JO].[JobNum] and [JA].[AssemblySeq] = [JO].[AssemblySeq] "
            "group by [JH].[JobNum]"
        )
        assert rules(sql) == []

    def test_min_and_max_are_fanout_immune(self):
        """Duplicating a row cannot move an extreme. Firing here is pure noise."""
        for fn in ("max", "min"):
            sql = (
                f"select {fn}([OH].[OrderDate]) as [D] from Erp.OrderHed as [OH] "
                "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
                "and [OH].[OrderNum] = [OD].[OrderNum]"
            )
            assert rules(sql) == [], fn

    def test_a_distinct_aggregate_is_immune(self):
        sql = (
            "select sum(distinct [OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and [OH].[OrderNum] = [OD].[OrderNum]"
        )
        assert "aggregate_fanout" not in rules(sql)

    def test_a_grouped_derived_table_is_keyed_by_its_group_by(self):
        """Pre-aggregating in a subquery is the FIX; it must not be flagged.

        Without deriving the subquery's key from its GROUP BY, the whole shape is
        unjudgeable and the model gets no credit for writing it correctly.
        """
        sql = (
            "select top 100 [d].[JobNum] as [J], [d].[Cost] as [C], [JH].[PartNum] as [P] "
            "from (select [JM].[Company] as [Company], [JM].[JobNum] as [JobNum], "
            "sum([JM].[RequiredQty]) as [Cost] from Erp.JobMtl as [JM] "
            "group by [JM].[Company], [JM].[JobNum]) as [d] "
            "inner join Erp.JobHead as [JH] on [d].[Company] = [JH].[Company] "
            "and [d].[JobNum] = [JH].[JobNum]"
        )
        assert rules(sql) == []

    def test_an_ungrouped_derived_source_stands_down_and_is_counted(self):
        sql = (
            "select sum([d].[Amt]) as [T] "
            "from (select [OH].[OrderAmt] as [Amt], [OH].[OrderNum] as [OrderNum] "
            "from Erp.OrderHed as [OH]) as [d] "
            "inner join Erp.OrderDtl as [OD] on [d].[OrderNum] = [OD].[OrderNum]"
        )
        report = analyse_grain(sql)
        assert "aggregate_fanout" not in report.rules
        assert "d" in report.unjudgeable


# --------------------------------------------------------------------------- #
# sibling_child_cross
# --------------------------------------------------------------------------- #


class TestSiblingChildCross:
    def test_b23_two_children_of_one_parent(self):
        f = next(f for f in analyse_grain(B23_SQL).findings if f.rule == "sibling_child_cross")
        assert f.detail["parent"] == "Erp.DMRHead"
        assert sorted(f.detail["crossed"][0]) == ["Erp.DMRActn", "Erp.LaborDtl"]

    def test_it_fires_without_any_aggregate(self):
        """Independent child joins invent pairings in listings as well as aggregates."""
        assert "sibling_child_cross" in rules(B23_SQL)

    def test_a_parent_child_grandchild_chain_is_NOT_a_cross(self):
        """One row per grandchild is not an invented pairing."""
        sql = (
            "select top 100 [JH].[JobNum] as [J], [JO].[OprSeq] as [O], [JD].[OpDtlSeq] as [D] "
            "from Erp.JobHead as [JH] "
            "inner join Erp.JobOper as [JO] on [JH].[Company] = [JO].[Company] "
            "and [JH].[JobNum] = [JO].[JobNum] "
            "inner join Erp.JobOpDtl as [JD] on [JO].[Company] = [JD].[Company] "
            "and [JO].[JobNum] = [JD].[JobNum] and [JO].[AssemblySeq] = [JD].[AssemblySeq] "
            "and [JO].[OprSeq] = [JD].[OprSeq] where [JH].[JobNum] = 'JOB-103'"
        )
        assert "sibling_child_cross" not in rules(sql)

    def test_one_child_plus_one_master_is_NOT_a_cross(self):
        sql = (
            "select top 100 [OD].[PartNum] as [P], [C].[Name] as [N] from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and [OH].[OrderNum] = [OD].[OrderNum] "
            "inner join Erp.Customer as [C] on [OH].[Company] = [C].[Company] "
            "and [OH].[CustNum] = [C].[CustNum] where [OH].[OrderNum] = 1001"
        )
        assert "sibling_child_cross" not in rules(sql)


# --------------------------------------------------------------------------- #
# duplicate_projection
# --------------------------------------------------------------------------- #


class TestDuplicateProjection:
    def test_b08_the_join_is_a_filter_and_there_is_no_distinct(self):
        f = next(f for f in analyse_grain(B08_SQL).findings if f.rule == "duplicate_projection")
        assert f.detail["projected_from"] == ["Erp.PartRev"]
        assert "Erp.OrderDtl" in f.detail["hidden_multiplier"]

    def test_adding_distinct_silences_it(self):
        """`select distinct` is the documented fix, so it must clear the warning."""
        assert "duplicate_projection" not in rules(B08_SQL.replace("select top 100", "select distinct top 100"))

    def test_projecting_the_hidden_table_silences_it(self):
        sql = B08_SQL.replace(
            "[PartRev].[RevisionNum] as [RevisionNum]",
            "[PartRev].[RevisionNum] as [RevisionNum], [OrderDtl].[OrderLine] as [L], "
            "[OrderDtl].[OrderNum] as [O]",
        )
        assert "duplicate_projection" not in rules(sql)

    def test_an_aggregate_query_is_left_to_aggregate_fanout(self):
        assert "duplicate_projection" not in rules(S4_SQL)


# --------------------------------------------------------------------------- #
# unlabelled_parent_scope — the B18 shape
# --------------------------------------------------------------------------- #


class TestUnlabelledParentScope:
    def test_b18_is_flagged(self):
        f = next(
            f for f in analyse_grain(B18_SQL).findings if f.rule == "unlabelled_parent_scope"
        )
        assert f.detail["parent_table"] == "Erp.JobHead"
        assert f.detail["selected_by"] == ["PartNum"]
        assert f.detail["parent_key_not_pinned"] == ["JobNum"]
        assert f.detail["parent_identified_in_output"] is False

    def test_the_labelled_variant_of_b18_is_still_flagged(self):
        """A projected parent label does not constrain the query to one parent.

        Keep the scope warning and explain how to inspect distinct parents."""
        f = next(
            f
            for f in analyse_grain(B18_LABELLED_SQL).findings
            if f.rule == "unlabelled_parent_scope"
        )
        assert f.detail["parent_identified_in_output"] is True
        assert "count the DISTINCT values" in f.message

    def test_pinning_the_parent_key_silences_it(self):
        """Pinning a complete parent key narrows the result to one job method."""
        assert rules(B18_CORRECT_SQL) == []

    def test_a_key_PREFIX_selector_is_a_coherent_subtree_not_this_shape(self):
        """A PO-number selector identifies a coherent subtree of its lines."""
        sql = (
            "select top 100 [POD].[PartNum] as [P], [PM].[PartNum] as [U] "
            "from Erp.PODetail as [POD] "
            "inner join Erp.POHeader as [POH] on [POD].[Company] = [POH].[Company] "
            "and [POD].[PONum] = [POH].[PONum] "
            "left outer join Erp.PartMtl as [PM] on [POD].[Company] = [PM].[Company] "
            "and [POD].[PartNum] = [PM].[MtlPartNum] where [POD].[PONum] = 2001"
        )
        assert "unlabelled_parent_scope" not in rules(sql)

    def test_a_boolean_FLAG_is_not_an_entity_selector(self):
        """`[PR].[Approved] = 1` narrows a population; it does not name one parent."""
        sql = (
            "select distinct top 100 [PR].[PartNum] as [P] from Erp.PartRev as [PR] "
            "inner join Erp.OrderDtl as [OD] on [PR].[Company] = [OD].[Company] "
            "and [PR].[PartNum] = [OD].[PartNum] where [PR].[Approved] = 1"
        )
        assert rules(sql) == []

    def test_a_quoted_numeric_literal_is_still_a_selector(self):
        """`Plant = '10'` is an id that looks like a flag. Quoting is the tell."""
        sql = (
            "select top 100 [JM].[PartNum] as [P] from Erp.JobHead as [JH] "
            "inner join Erp.JobMtl as [JM] on [JH].[Company] = [JM].[Company] "
            "and [JH].[JobNum] = [JM].[JobNum] where [JH].[Plant] = '10'"
        )
        assert "unlabelled_parent_scope" in rules(sql)

    def test_no_selector_at_all_is_not_this_shape(self):
        sql = (
            "select top 100 [JM].[PartNum] as [P] from Erp.JobHead as [JH] "
            "inner join Erp.JobMtl as [JM] on [JH].[Company] = [JM].[Company] "
            "and [JH].[JobNum] = [JM].[JobNum]"
        )
        assert "unlabelled_parent_scope" not in rules(sql)

    def test_an_aggregate_query_is_left_to_aggregate_fanout(self):
        assert "unlabelled_parent_scope" not in rules(B16_SQL)


# --------------------------------------------------------------------------- #
# join_missing_company
# --------------------------------------------------------------------------- #


class TestJoinMissingCompany:
    def test_a_join_without_company_is_named_accurately_not_called_a_fanout(self, monkeypatch):
        """Omitting Company is a missing-key warning, not proof of fan-out.

        A single-company dataset can have no additional rows, so describe
        the missing key instead of asserting an observed multiplier."""
        from epicor_mcp.sql import grain
        monkeypatch.setitem(grain.TABLE_KEYS, 'person', (frozenset({'company','personid'}),))
        sql = (
            "select top 100 [jh].[JobNum] as [J], [jh].[PartNum] as [P] "
            "from Erp.JobHead as [jh] "
            "inner join Erp.Person as [p] on [jh].[PersonID] = [p].[PersonID] "
            "where [p].[Name] = 'Avery Example' and [jh].[JobClosed] = 0"
        )
        assert rules(sql) == ["join_missing_company"]

    def test_it_never_hides_a_real_fanout(self):
        """A missing Company AND a missing business key is still a fan-out."""
        sql = (
            "select sum([OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[OrderNum] = [OD].[OrderNum]"
        )
        assert "aggregate_fanout" in rules(sql)


# --------------------------------------------------------------------------- #
# Post-execution: duplicate_collapse
# --------------------------------------------------------------------------- #


class TestDuplicateCollapse:
    def test_a_collapsing_page_is_reported_with_both_numbers(self):
        rows = [{"Part": "A", "Qty": "1"}] * 40 + [{"Part": "B", "Qty": "2"}] * 40
        f = duplicate_collapse(rows)
        assert f is not None
        assert f.detail == {"rows": 80, "distinct_rows": 2}
        assert f.severity == Severity.WARN

    def test_a_clean_page_says_nothing(self):
        rows = [{"Part": f"P{i}"} for i in range(50)]
        assert duplicate_collapse(rows) is None

    def test_a_short_page_says_nothing(self):
        """On 4 rows a 2:1 collapse is noise, not a fan-out."""
        assert duplicate_collapse([{"a": 1}, {"a": 1}, {"a": 2}, {"a": 2}]) is None

    def test_an_empty_page_says_nothing(self):
        """An empty result is sometimes the TRUE answer. It is never a grain fault."""
        assert duplicate_collapse([]) is None

    def test_it_rides_the_public_entry_point(self):
        rows = [{"Part": "A"}] * 30
        report = analyse_grain("select top 100 [P].[PartNum] as [Part] from Erp.Part as [P]", rows=rows)
        assert report.rules == ["duplicate_rows_returned"]


# --------------------------------------------------------------------------- #
# Verification — measure it instead of guessing
# --------------------------------------------------------------------------- #


class TestVerification:
    def test_the_dedup_plan_is_generated_for_s4(self):
        plans = analyse_grain(S4_SQL).verifications
        plan = next(p for p in plans if p["kind"] == "dedup_aggregate")
        sql = plan["sql"]
        assert "select distinct" in sql
        assert "[OH].[Company], [OH].[OrderNum]" in sql
        assert "sum([d].[Measure])" in sql
        # The recovery must carry the caller's own filter or it answers a
        # different question.
        assert "1001" in sql

    def test_the_dedup_plan_keeps_the_group_by(self):
        plan = next(
            p for p in analyse_grain(B16_SQL).verifications if p["kind"] == "dedup_aggregate"
        )
        assert "group by" in plan["sql"].lower()

    def test_the_parent_cardinality_plan_is_generated_for_b18(self):
        plan = next(
            p
            for p in analyse_grain(B18_SQL).verifications
            if p["kind"] == "parent_cardinality"
        )
        assert "select distinct [JH].[JobNum]" in plan["sql"]
        assert "PART-200" in plan["sql"]

    def test_a_measured_fanout_promotes_the_finding_and_states_the_multiple(self):
        """Synthetic aggregate values verify promotion after a measured multiplier."""
        f = next(f for f in analyse_grain(S4_SQL).findings if f.rule == "aggregate_fanout")
        assert f.severity == Severity.WARN
        proved = apply_verification(
            f, reported_value=8_800.0, corrected_value=100.0
        )
        assert proved.severity == Severity.REFUSE
        assert proved.detail["verified"] == "fanout_measured"
        assert round(proved.detail["multiple"]) == 88
        assert "88" in proved.message

    def test_a_measured_NON_fanout_WITHDRAWS_the_warning(self):
        """A warning nobody can clear is a warning everybody learns to ignore."""
        f = next(f for f in analyse_grain(S4_SQL).findings if f.rule == "aggregate_fanout")
        cleared = apply_verification(f, reported_value=1000.0, corrected_value=1000.0)
        assert cleared.severity == Severity.WARN
        assert cleared.detail["verified"] == "no_fanout"
        assert "no fan-out" in cleared.message

    def test_an_unmeasured_finding_is_returned_unchanged(self):
        f = next(f for f in analyse_grain(S4_SQL).findings if f.rule == "aggregate_fanout")
        assert apply_verification(f, reported_value=None, corrected_value=1.0) is f
        assert apply_verification(f, reported_value=1.0, corrected_value=None) is f


# --------------------------------------------------------------------------- #
# It fails OPEN, always
# --------------------------------------------------------------------------- #


class TestNeverRaises:
    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "not sql at all (((",
            "select",
            "update Erp.Part set [PartNum] = 'x'",
            "select top 1 [P].[PartNum] from Erp.Part as [P] where [P].[PartNum] = 'a--b'",
            "select sum([A].[X]) from Erp.OrderHed as [A] join Erp.OrderDtl as [B] on 1 = 1",
            "select 1",
        ],
    )
    def test_odd_input_returns_a_report_not_an_exception(self, sql):
        report = analyse_grain(sql)
        assert isinstance(report.findings, tuple)

    def test_an_or_predicate_never_argues_a_join_is_safe(self):
        """Under an OR nothing is guaranteed, so no equality inside one may bind.

        Failing the other way — treating an OR-ed equality as a binding — would
        make a fanning join look safe, which is the one direction this module
        must never fail in.
        """
        sql = (
            "select sum([OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
            "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and ([OH].[OrderNum] = [OD].[OrderNum] or [OD].[OrderLine] = 1)"
        )
        assert "aggregate_fanout" in rules(sql)

    def test_a_single_table_statement_is_never_flagged(self):
        assert rules("select top 100 [P].[PartNum] as [P] from Erp.Part as [P]") == []

    def test_a_missing_key_file_degrades_to_silence(self, monkeypatch):
        import epicor_mcp.sql.grain as grain

        monkeypatch.setattr(grain, "TABLE_KEYS", {})
        assert analyse_grain(S4_SQL).findings == ()

    def test_findings_serialise(self):
        for f in analyse_grain(B16_SQL).findings:
            d = f.to_dict()
            assert set(d) >= {"rule", "severity", "message", "evidence"}
            assert isinstance(f, GrainFinding)


# --------------------------------------------------------------------------- #
# Wiring — a detector that is never called is dead code
# --------------------------------------------------------------------------- #


class TestPipeIntegration:
    """`run_sql` carries the grain findings back, and never refuses on them.

    The wiring is deliberately ADDITIVE: `sql/lint.py`'s own DS-side
    `aggregate_fanout` stays `notes[0]` and keeps ownership of the summary, so
    this module can only add. It is asserted rather than assumed because
    "appended, not prepended" is exactly the kind of thing a later edit reorders
    without noticing.
    """

    @staticmethod
    def _run(fixture: str, rows: list[dict] | None = None) -> dict:
        import asyncio

        from epicor_mcp.sql.adhoc import run_sql
        from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

        sql, ds = load(fixture)
        client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute(rows or []))
        return asyncio.run(
            run_sql(
                sql,
                client=client,
                api_key="k",
                base_url="https://example.invalid/api/v2/odata/DEMO",
            )
        )

    def test_grain_notes_ride_back_and_the_query_still_succeeds(self):
        out = self._run("gov_fanout", [{"J": "JOB-103", "Q": "5"}])
        assert out["success"] is True  # a WARNING never becomes a refusal
        rules_seen = [n["rule"] for n in out["notes"]]
        assert rules_seen[0] == "aggregate_fanout"  # the lint's, still first
        assert "sibling_child_cross" in rules_seen  # ...and grain's, appended

    def test_the_verification_statement_is_offered_not_run(self):
        out = self._run("gov_fanout", [{"J": "JOB-103", "Q": "5"}])
        checks = out.get("grain_checks") or []
        assert any(c["kind"] == "dedup_aggregate" for c in checks)
        assert all("select distinct" in c["sql"] for c in checks)

    def test_a_clean_query_carries_no_grain_note_and_no_checks(self):
        out = self._run("clean_top", [{"PN": "ABC-1"}])
        assert out["notes"] == []
        assert "grain_checks" not in out
        assert out["summary"].startswith("1 row(s)")


class TestCteShadowing:
    """A CTE is referenced by a BARE NAME and parses like a real table.

    Judging `with OrderDtl as (...)` with the real `Erp.OrderDtl`'s primary key
    would be a silent-wrong of exactly the class this module exists to catch.
    """

    def test_a_cte_named_like_a_real_table_does_not_borrow_its_key(self):
        sql = (
            "with OrderDtl as (select [P].[PartNum] as [PartNum], [P].[Company] as [Company] "
            "from Erp.Part as [P]) "
            "select sum([OH].[OrderAmt]) as [T] from Erp.OrderHed as [OH] "
            "inner join OrderDtl as [OD] on [OH].[Company] = [OD].[Company] "
            "and [OH].[OrderNum] = [OD].[PartNum]"
        )
        report = analyse_grain(sql)
        assert "OrderDtl" in report.unjudgeable
        assert "aggregate_fanout" not in report.rules

    def test_a_grouped_cte_earns_a_key_and_silences_the_warning(self):
        sql = (
            "with LineTotals as (select [OD].[Company] as [Company], "
            "[OD].[OrderNum] as [OrderNum], sum([OD].[ExtPriceDtl]) as [T] "
            "from Erp.OrderDtl as [OD] group by [OD].[Company], [OD].[OrderNum]) "
            "select sum([OH].[OrderAmt]) as [Amt] from Erp.OrderHed as [OH] "
            "inner join LineTotals as [LT] on [OH].[Company] = [LT].[Company] "
            "and [OH].[OrderNum] = [LT].[OrderNum]"
        )
        assert analyse_grain(sql).rules == []
