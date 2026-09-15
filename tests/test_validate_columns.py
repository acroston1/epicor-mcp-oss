"""Regression coverage: test validate columns."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from epicor_mcp.sql import validate_columns as vc
from epicor_mcp.sql.validate_columns import (
    ColumnCatalogue,
    load_catalogue,
    validate_columns,
)

# Synthetic typed catalogue: numeric quantity references must not be
# auto-corrected to similarly named boolean flags.
CATALOGUE = ColumnCatalogue(
    {
        "Part": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "PartDescription", "type": "nvarchar"},
            {"name": "ClassID", "type": "nvarchar"},
            {"name": "InActive", "type": "bit"},
            {"name": "UnitPrice", "type": "decimal"},
        ],
        "PartWhse": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "WarehouseCode", "type": "nvarchar"},
            {"name": "OnHandQty", "type": "decimal"},
        ],
        "OrderDtl": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "OrderNum", "type": "int"},
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "ExtPriceDtl", "type": "decimal"},
            {"name": "OpenLine", "type": "bit"},
        ],
        "JobHead": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "JobNum", "type": "nvarchar"},
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "XRefCustNum", "type": "int"},
        ],
        "Customer": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "CustNum", "type": "int"},
            {"name": "Name", "type": "nvarchar"},
        ],
    }
)

#: A catalogue with a boolean flag whose name CONTAINS the decimal one — the legacy
#: exact `Part.HasOnHandQty` shape, which difflib rates 0.857 (above the cutoff).
BOOLEAN_TRAP = ColumnCatalogue(
    {
        "Part": [
            {"name": "Company", "type": "nvarchar"},
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "HasOnHandQty", "type": "bit"},
        ]
    }
)


# --------------------------------------------------------------------------- #
# 1. FALSE POSITIVES — the half that can do damage
# --------------------------------------------------------------------------- #
def test_a_cte_output_name_is_not_a_base_column():
    """Regression coverage: test a cte output name is not a base column."""
    sql = (
        "with [u] as (select [OD].[PartNum] as [PartNum], "
        "sum([OD].[ExtPriceDtl]) as [Revenue] from Erp.OrderDtl as [OD] "
        "group by [OD].[PartNum]) "
        "select top 5 [u].[PartNum] as [PartNum], [u].[Revenue] as [Rev] "
        "from [u] order by [u].[Revenue] desc"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.unknown == []
    assert result.to_dict()["not_checked"]["derived_or_cte_output"] >= 2


def test_a_derived_table_output_name_is_not_a_base_column():
    sql = (
        "select top 5 [t].[PartNum] as [PartNum], [t].[Revenue] as [Rev] "
        "from (select [OD].[PartNum] as [PartNum], sum([OD].[ExtPriceDtl]) as [Revenue] "
        "      from Erp.OrderDtl as [OD] group by [OD].[PartNum]) as [t] "
        "order by [t].[Revenue] desc"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.envelope is None


def test_a_derived_table_joined_to_a_base_table_does_not_leak_across():
    """The S1 input verbatim: a derived source is NOT a candidate owner."""
    sql = (
        "select [t].[PartNum] as [PartNum], [t].[Qty] as [Qty] "
        "from (select [PW].[PartNum] as [PartNum], sum([PW].[OnHandQty]) as [Qty] "
        "      from Erp.PartWhse as [PW] group by [PW].[PartNum]) as [t] "
        "join Erp.PartWhse as [PW2] on [PW2].[PartNum] = [t].[PartNum]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok


def test_a_nested_cte_chain_is_not_judged():
    sql = (
        "with [a] as (select [OD].[PartNum] as [PN] from Erp.OrderDtl as [OD]), "
        "     [b] as (select [a].[PN] as [PN2] from [a]) "
        "select top 5 [b].[PN2] as [X] from [b]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok


def test_a_correlated_subquery_resolves_against_the_outer_alias():
    sql = (
        "select top 5 [P].[PartNum] as [PartNum] from Erp.Part as [P] "
        "where [P].[PartNum] in (select [OD].[PartNum] from Erp.OrderDtl as [OD] "
        "                        where [OD].[Company] = [P].[Company])"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.coverage[0] == result.coverage[1]  # every reference judged


def test_a_table_outside_the_catalogue_is_unjudgeable_never_hallucinated():
    sql = "select top 5 [X].[WhoKnows] as [W] from Erp.SomeTableNobodyIndexed as [X]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.to_dict()["not_checked"] == {"table_not_in_catalogue": 1}


def test_a_non_erp_schema_is_not_matched_by_bare_name():
    """`Ice.Part` must not inherit `Erp.Part`'s column list."""
    sql = "select top 5 [P].[PartNum] as [P] from Ice.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.to_dict()["not_checked"] == {"table_not_in_catalogue": 1}


def test_an_unknown_alias_is_an_alias_defect_not_a_column_defect():
    sql = "select top 5 [Q].[PartNum] as [P] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.to_dict()["not_checked"] == {"unresolved_alias": 1}


def test_an_output_alias_reused_in_order_by_is_not_a_column():
    """`order by [Revenue]` is the dialect's alias-sort rule, owned by the
    transpiler's `order_by_alias` repair. Calling it a phantom column would
    misattribute a defect another pass already FIXES."""
    sql = (
        "select top 5 [OD].[PartNum] as [PartNum], sum([OD].[ExtPriceDtl]) as [Revenue] "
        "from Erp.OrderDtl as [OD] group by [OD].[PartNum] order by [Revenue] desc"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.to_dict()["not_checked"]["output_alias"] == 1


def test_an_unqualified_column_on_a_join_is_not_guessed():
    sql = (
        "select top 5 [PartNum] as [P] from Erp.Part as [A] "
        "inner join Erp.PartWhse as [B] on [A].[Company] = [B].[Company] "
        "and [A].[PartNum] = [B].[PartNum]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.to_dict()["not_checked"]["unqualified_ambiguous"] == 1


def test_star_is_left_to_the_lint():
    for sql in (
        "select top 5 * from Erp.Part as [P]",
        "select top 5 [P].* from Erp.Part as [P]",
        "select count(*) as [N] from Erp.Part as [P]",
    ):
        result = validate_columns(sql, catalogue=CATALOGUE)
        assert result.ok, sql


@pytest.mark.parametrize(
    "sql",
    [
        # function-argument keywords that a naive walker reads as columns
        "select top 5 datepart(quarter, [OD].[OrderNum]) as [Q] from Erp.OrderDtl as [OD]",
        "select top 5 [OD].[OrderNum] as [N] from Erp.OrderDtl as [OD] "
        "where [OD].[OrderNum] > 1 order by [OD].[OrderNum] offset 0 rows fetch next 5 rows only",
        "select top 5 cast([OD].[ExtPriceDtl] as decimal) as [X] from Erp.OrderDtl as [OD]",
        "select top 5 case when [P].[InActive] = true then 'x' else 'y' end as [S] "
        "from Erp.Part as [P]",
        "select top 5 [D].[PartNum] as [K] from Erp.OrderDtl as [D] union all "
        "select top 5 [P].[PartNum] as [K] from Erp.Part as [P]",
        "select top 5 [A].[PartNum] as [P] from Erp.Part as [A] "
        "left outer join Erp.OrderDtl as [B] on [A].[Company] = [B].[Company] "
        "and [A].[PartNum] = [B].[PartNum] where [B].[PartNum] is null",
    ],
)
def test_dialect_shapes_do_not_produce_false_positives(sql):
    assert validate_columns(sql, catalogue=CATALOGUE).ok, sql


def test_an_empty_result_is_never_this_module_s_business():
    """THE overriding invariant: a legitimately-empty query is a clean terminal
    answer. Nothing here looks at rows, and a filter that will match nothing is
    not a column defect."""
    sql = (
        "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] "
        "where [P].[ClassID] = 'NO-SUCH-CLASS'"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok and result.envelope is None


# --------------------------------------------------------------------------- #
# 2. DETECTION
# --------------------------------------------------------------------------- #
def test_a_phantom_in_the_select_list_is_named_with_its_table():
    sql = "select top 5 [P].[PartNum] as [P], [P].[ZzNope] as [X] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert not result.ok
    env = result.envelope
    assert env["error"] == "sql_unknown_column"
    assert "Part.ZzNope" in env["message"]
    assert env["valid"]["unknown_by_position"] == {"select": ["Part.ZzNope"]}
    assert "PartDescription" in env["valid"]["columns_by_table"]["Part"]


def test_a_phantom_in_the_where_clause_is_caught_before_epicor_sees_it():
    """Regression coverage: test a phantom in the where clause is caught before epicor sees it."""
    sql = (
        "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] where [P].[OnHandQty] > 0"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert not result.ok
    assert result.envelope["valid"]["unknown_by_position"] == {"where": ["Part.OnHandQty"]}


def test_a_phantom_in_the_order_by_is_caught():
    sql = "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] order by [P].[Nope] desc"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert not result.ok
    assert "order_by" in result.envelope["valid"]["unknown_by_position"]


def test_the_envelope_names_the_side_that_broke():
    """The legacy `unknown_by_argument` in SQL terms: a model holding a good filter and
    one bad projection column must be able to tell which half was wrong."""
    sql = (
        "select top 5 [P].[Nope] as [X] from Erp.Part as [P] "
        "where [P].[ClassID] = 'FG' order by [P].[PartNum] asc"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    env = result.envelope
    assert env["valid"]["validated_clean"] == ["order_by", "where"]
    assert "VALID" in env["message"]


def test_column_lives_on_names_the_table_that_does_own_it():
    """A missing JobHead.CustNum points to tables that actually carry CustNum."""
    sql = "select top 5 [J].[JobNum] as [J], [J].[CustNum] as [C] from Erp.JobHead as [J]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.envelope["valid"]["column_lives_on"]["JobHead.CustNum"] == ["Customer"]


def test_two_phantoms_on_two_tables_are_both_reported():
    sql = (
        "select top 5 [P].[Nope1] as [A], [OD].[Nope2] as [B] "
        "from Erp.Part as [P] inner join Erp.OrderDtl as [OD] "
        "on [P].[Company] = [OD].[Company] and [P].[PartNum] = [OD].[PartNum]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert sorted(r.written for r in result.unknown) == ["OD.Nope2", "P.Nope1"]
    assert set(result.envelope["valid"]["columns_by_table"]) == {"Part", "OrderDtl"}


# --------------------------------------------------------------------------- #
# 3. CORRECTION — unambiguous only, never a guess
# --------------------------------------------------------------------------- #
def test_an_unambiguous_typo_produces_runnable_corrected_sql():
    sql = "select top 5 [P].[Description] as [D] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.corrections == {"P.Description": "PartDescription"}
    fixed = result.envelope["retry_with"]["sql"]
    assert "PartDescription" in fixed
    # and the corrected statement is itself clean
    assert validate_columns(fixed, catalogue=CATALOGUE).ok


def test_a_correction_to_a_boolean_column_is_vetoed_not_applied():
    """The legacy exact trap: `onhandqty` IS a substring of `hasonhandqty` and difflib
    rates the pair 0.857, above the 0.82 cutoff. Correcting a `> 0` comparison to
    a bit column builds a filter Epicor answers with its generic 500."""
    sql = "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] where [P].[OnHandQty] > 0"
    result = validate_columns(sql, catalogue=BOOLEAN_TRAP)
    assert not result.ok
    assert result.corrections == {}
    assert result.envelope.get("retry_with") is None
    # ...but the candidate is still NAMED, so the recovery is still one hop.
    assert "HasOnHandQty" in result.envelope["valid"]["did_you_mean"]["Part.OnHandQty"]


def test_a_boolean_comparison_may_still_be_corrected_to_a_boolean_column():
    sql = "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] where [P].[OnHandQty] = true"
    result = validate_columns(sql, catalogue=BOOLEAN_TRAP)
    assert result.corrections == {"P.OnHandQty": "HasOnHandQty"}


def test_a_name_with_no_close_match_gets_no_did_you_mean():
    """A weak suggestion on an invented name is the model's next wrong hop."""
    sql = "select top 5 [P].[Zqxjkv] as [X] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert "did_you_mean" not in result.envelope["valid"]


def test_retry_with_is_absent_when_only_some_phantoms_are_correctable():
    """`retry_with` is runnable or it is absent — a half-fixed statement is a
    second failed hop."""
    sql = (
        "select top 5 [P].[Description] as [D], [P].[Zqxjkv] as [X] from Erp.Part as [P]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert len(result.unknown) == 2
    assert result.envelope.get("retry_with") is None
    assert result.corrected_sql is None


# --------------------------------------------------------------------------- #
# 4. THE CATALOGUE
# --------------------------------------------------------------------------- #
def test_an_empty_catalogue_judges_nothing():
    sql = "select top 5 [P].[AnythingAtAll] as [X] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=ColumnCatalogue({}))
    assert result.ok
    assert result.unparsed == "no column catalogue is loaded"


def test_an_explicit_synthetic_fixture_is_a_usable_catalogue():
    """Explicit metadata overrides work without bundled operator data."""
    source = Path(os.environ["EPICOR_MCP_COLUMN_CATALOGUE"])
    catalogue = load_catalogue([source])
    assert len(catalogue) == len(json.loads(source.read_text())["tables"])
    assert catalogue.has("Part", "PartNum")
    assert not catalogue.has("Part", "OnHandQty")  # the OData-only phantom
    assert catalogue.has("PartWhse", "OnHandQty")


def test_the_default_catalogue_covers_the_card():
    from epicor_mcp.sql.card import CARD_TABLES

    catalogue = load_catalogue()
    missing = [t for t in CARD_TABLES if not catalogue.knows_table(t)]
    assert missing == []


def test_the_catalogue_refuses_the_odata_projection():
    """`data/service_index.db` is the OData field list and disagrees with the SQL
    layer by 93-249 phantom columns per hot table."""
    with pytest.raises(ValueError, match="OData projection"):
        vc._read(Path("data/service_index.db"))


def test_a_missing_catalogue_file_degrades_to_empty_not_to_an_exception():
    catalogue = load_catalogue([Path("/nonexistent/never/here.json")])
    assert len(catalogue) == 0
    assert validate_columns("select [P].[X] as [X] from Erp.Part as [P]",
                            catalogue=catalogue).ok


# --------------------------------------------------------------------------- #
# 5. CONTRACT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    ["", "   ", ";", "-- just a comment", "select", "not sql at all {{{",
     "select 1; select 2", "select top 5 [P].[X] from"],
)
def test_it_never_raises_and_never_flags_what_it_cannot_parse(sql):
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert result.ok
    assert result.envelope is None


def test_it_makes_no_epicor_call():
    """Zero extra round trips is the whole cost argument, so assert it
    STRUCTURALLY. The scan runs over the module with docstrings stripped — the
    prose names the endpoints in order to explain why it does not call them, and
    prose must not be able to pass or fail this test."""
    from tests.test_query_no_write_methods import _code_only

    code = _code_only(Path(vc.__file__))
    for forbidden in ("ParseFromSQL", "DynamicQuerySvc", "Analyze", "httpx",
                      "requests", "EpicorClient", "await ", "async def"):
        assert forbidden not in code, forbidden


def test_the_result_is_json_serialisable_and_carries_its_own_coverage():
    sql = (
        "select top 5 [P].[Nope] as [X], [t].[Y] as [Y] from Erp.Part as [P] "
        "inner join (select [OD].[OrderNum] as [Y] from Erp.OrderDtl as [OD]) as [t] "
        "on [t].[Y] = [P].[UnitPrice]"
    )
    result = validate_columns(sql, catalogue=CATALOGUE)
    payload = json.dumps({"result": result.to_dict(), "envelope": result.envelope})
    assert "Part.Nope" in payload
    assert result.to_dict()["columns_checked"] < result.to_dict()["columns_seen"]


def test_no_business_data_lives_in_this_module():
    """Column NAMES are schema; a part number or a customer name is not."""
    source = inspect.getsource(vc)
    for leak in ("PRIVATE-PART-EXAMPLE", "DEMO", "PRIVATE-TENANT", "private-tenant.example"):
        assert leak not in source, leak


def test_a_user_defined_column_is_flagged_WITH_the_extension_join_recipe():
    """Regression coverage: test a user defined column is flagged WITH the extension join recipe."""
    sql = "select top 5 [P].[PartNum] as [P], [P].[Fixture_c] as [F] from Erp.Part as [P]"
    result = validate_columns(sql, catalogue=CATALOGUE)
    assert not result.ok
    help_text = result.envelope["valid"]["user_defined_columns"]["Part.Fixture_c"]
    assert "Erp.Part_UD" in help_text
    assert "ForeignSysRowID" in help_text
    # ...and no auto-correction is invented for it.
    assert result.corrections == {}


def test_the_envelope_carries_the_catalogue_timestamp_for_stale_diagnosis():
    """A column added to Epicor after the snapshot is the ONE false-positive
    class the corpus cannot measure. Serving the timestamp makes it diagnosable."""
    catalogue = load_catalogue()
    result = validate_columns(
        "select top 5 [P].[ZzNope] as [X] from Erp.Part as [P]", catalogue=catalogue
    )
    assert result.envelope["detail"]["catalogue_generated"] == catalogue.generated
    assert result.envelope["terminal"] is False


def test_the_recovery_list_leads_with_the_card_s_curated_columns():
    """Regression coverage: test the recovery list leads with the card s curated columns."""
    from epicor_mcp.sql.card import CARD_COLUMNS

    catalogue = load_catalogue()
    result = validate_columns(
        "select top 5 [LD].[JobNum] as [J], [LD].[PartNum] as [P] "
        "from Erp.LaborDtl as [LD]",
        catalogue=catalogue,
    )
    served = result.envelope["valid"]["columns_by_table"]["LaborDtl"]
    assert served[: len(CARD_COLUMNS["LaborDtl"])] == list(CARD_COLUMNS["LaborDtl"])
    assert "JobNum" in served
    assert len(served) == vc.MAX_COLUMNS_SERVED


def test_column_lives_on_is_ranked_and_capped():
    """`PartNum` lives on 23 of the 42 catalogued tables and 477 Epicor tables
    overall. An unranked list of 23 is a second search, not an answer."""
    catalogue = load_catalogue()
    result = validate_columns(
        "select top 5 [LD].[PartNum] as [P] from Erp.LaborDtl as [LD]",
        catalogue=catalogue,
    )
    from epicor_mcp.sql.card import CARD_TABLES

    owners = result.envelope["valid"]["column_lives_on"]["LaborDtl.PartNum"]
    assert len(owners) <= vc.MAX_OWNERS_SERVED
    # Ranked by the card's own listed order, first entry first — asserted against
    # CARD_TABLES rather than a pinned table name, so re-curating the card
    # cannot make this test wrong about a rule the code still obeys.
    rank = [CARD_TABLES.index(t) for t in owners if t in CARD_TABLES]
    assert rank == sorted(rank)
    assert owners[0] in CARD_TABLES


def test_the_envelope_stays_small_enough_to_be_read():
    """An error nobody can read is an error nobody acts on."""
    catalogue = load_catalogue()
    result = validate_columns(
        "select top 5 [LD].[PartNum] as [P], [P2].[Nope] as [N] "
        "from Erp.LaborDtl as [LD] inner join Erp.Part as [P2] "
        "on [LD].[Company] = [P2].[Company] and [LD].[JobNum] = [P2].[PartNum]",
        catalogue=catalogue,
    )
    assert len(json.dumps(result.envelope)) < 8000
