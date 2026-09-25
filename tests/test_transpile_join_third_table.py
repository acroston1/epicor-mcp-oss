"""ON-clause conjuncts Epicor would evaluate out of scope.

Measured with a raw parse+execute matrix: ParseFromSQL keeps ONE parent per
join — the other table of the FIRST conjunct linking the joined table — and
files every other ON conjunct under the table on its LEFT-hand side. Filed
under the joined table (rendered in its own ON) or under the FROM table of an
inner join (rendered in WHERE), it runs. Filed under an EARLIER JOINED table it
is rendered inside that table's join, before the joined table is in scope, and
Execute fails *"The multi-part identifier "PC.CostID" could not be bound."*
Measured repairs:

* swap the sides so the joined table's column leads — the same rows as the
  WHERE form (inner) and as an independent CTE (LEFT join);
* a Company-only first link yields to the table carrying the real key — rows
  identical to the hand-corrected statement.

Replaying a corpus of real model-written statements, every statement this pass
changes had failed before, and none that ran is touched.
"""

from __future__ import annotations

import sqlglot

from epicor_mcp.sql.transpile import WEDGE_POLICY, Outcome, transpile


def tp(sql: str):
    return transpile(sql, policy=WEDGE_POLICY)


def _parts(sql: str) -> tuple[str, list[str]]:
    root = sqlglot.parse_one(sql, read="tsql")
    where = root.args.get("where")
    ons = [j.args["on"].sql("tsql") for j in root.args.get("joins") or [] if j.args.get("on")]
    return (where.this.sql("tsql") if where else ""), ons


W_PL = (
    "from Erp.Warehse as [W] "
    "inner join Erp.Plant as [PL] on [W].[Company] = [PL].[Company] and [W].[Plant] = [PL].[Plant] "
)
SEL = "select top 5 [W].[Plant] as [Plant], [PC].[PartNum] as [PartNum] "


# --------------------------------------------------------------------------- #
# Repair 1 — swap the sides (inner AND outer joins)
# --------------------------------------------------------------------------- #


def test_a_condition_led_by_an_earlier_joined_table_is_swapped_and_stays_in_the_on():
    sql = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and "
        "[W].[WarehouseCode] = [PC].[PartNum] and [PL].[PlantCostID] = [PC].[CostID]"
    )
    r = tp(sql)
    assert r.outcome is Outcome.REWRITTEN
    assert r.rules == ["join_on_sides_swapped"]
    where, ons = _parts(r.sql)
    assert where == ""
    assert ons[1] == (
        "[W].[Company] = [PC].[Company] AND [W].[WarehouseCode] = [PC].[PartNum] "
        "AND [PC].[CostID] = [PL].[PlantCostID]"
    )
    t = r.transformations[0]
    assert "[PL].[PlantCostID] = [PC].[CostID]" in t.message
    assert "could not be bound" in t.evidence


def test_a_left_join_is_swapped_not_refused_so_it_stays_a_left_join():
    """A derived-table LEFT join. Moving to WHERE would have made it inner."""
    sql = (
        "select top 5 [PB].[PartNum] as [PartNum] from Erp.PartBin as [PB] "
        "inner join Erp.Warehse as [W] on [PB].[Company] = [W].[Company] and "
        "[PB].[WarehouseCode] = [W].[WarehouseCode] "
        "left outer join (select [PT].[Company] as [Company], [PT].[PartNum] as [PartNum], "
        "[PT].[Plant] as [Plant] from Erp.PartTran as [PT]) as [LT] on "
        "[PB].[Company] = [LT].[Company] and [PB].[PartNum] = [LT].[PartNum] and "
        "[W].[Plant] = [LT].[Plant]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_sides_swapped"]
    where, ons = _parts(r.sql)
    assert where == ""
    assert ons[1].endswith("[LT].[Plant] = [W].[Plant]")
    assert "LEFT OUTER JOIN" in r.sql.upper()


def test_a_relational_operator_is_flipped_when_the_sides_swap():
    sql = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and "
        "[W].[WarehouseCode] = [PC].[PartNum] and [PL].[PlantCostID] < [PC].[CostID]"
    )
    _, ons = _parts(tp(sql).sql)
    assert ons[1].endswith("[PC].[CostID] > [PL].[PlantCostID]")


def test_a_join_whose_parent_is_a_cte_is_swapped():
    sql = (
        "with [oh] as (select [PB].[Company] as [Company], [PB].[PartNum] as [PartNum], "
        "[W].[Plant] as [Plant] from Erp.PartBin as [PB] inner join Erp.Warehse as [W] on "
        "[PB].[Company] = [W].[Company] and [PB].[WarehouseCode] = [W].[WarehouseCode]) "
        "select top 5 [oh].[PartNum] as [PartNum] from [oh] "
        "inner join Erp.Plant as [PL] on [oh].[Company] = [PL].[Company] and [oh].[Plant] = [PL].[Plant] "
        "inner join Erp.PartCost as [PC] on [oh].[Company] = [PC].[Company] and "
        "[oh].[PartNum] = [PC].[PartNum] and [PL].[PlantCostID] = [PC].[CostID]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_sides_swapped"]
    assert "[PC].[CostID] = [PL].[PlantCostID]" in r.sql
    assert "[PB].[WarehouseCode] = [W].[WarehouseCode])" in r.sql  # CTE body untouched


# --------------------------------------------------------------------------- #
# Repair 2 — the key's table becomes the parent
# --------------------------------------------------------------------------- #


def test_a_company_only_first_link_yields_to_the_table_holding_the_key():
    """`on [L].[Company] = [S].[Company] and [E].[SupervisorID] = [S].[EmpID]`:
    Epicor would record L->S on Company only (refused as a
    cartesian) and carry the key as a stray criterion."""
    sql = (
        "select top 10 [L].[EmployeeNum] as [EmpNum], [S].[Name] as [Supervisor] "
        "from Erp.LaborDtl as [L] "
        "inner join Erp.EmpBasic as [E] on [L].[Company] = [E].[Company] and [L].[EmployeeNum] = [E].[EmpID] "
        "inner join Erp.EmpBasic as [S] on [L].[Company] = [S].[Company] and [E].[SupervisorID] = [S].[EmpID]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_parent_reordered"]
    _, ons = _parts(r.sql)
    # Same two conditions, key first. `[L]` is the FROM table of an inner join,
    # so its Company condition is left exactly as written (Epicor puts it in WHERE).
    assert ons[1] == "[E].[SupervisorID] = [S].[EmpID] AND [L].[Company] = [S].[Company]"


def test_on_a_left_join_the_reordered_company_condition_is_also_swapped():
    """The FROM-table exemption is inner-join only, so on a LEFT
    join the demoted Company condition is swapped to keep it in the ON."""
    sql = (
        "select top 1000 [F].[PartNum] as [PartNumber], [PC].[Name] as [PartCustName] "
        "from Erp.Forecast as [F] "
        "left outer join Erp.Part as [P] on [F].[Company] = [P].[Company] and [F].[PartNum] = [P].[PartNum] "
        "left outer join Erp.Customer as [PC] on [F].[Company] = [PC].[Company] and "
        "[P].[ProdCode] = [PC].[CustID]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_parent_reordered", "join_on_sides_swapped"]
    where, ons = _parts(r.sql)
    assert where == ""
    assert ons[1] == "[P].[ProdCode] = [PC].[CustID] AND [PC].[Company] = [F].[Company]"


def test_a_company_only_link_to_the_from_table_is_reordered_onto_the_key_table():
    """[W] linked on Company only, key to [PL]. Written key-first it was measured
    running; `[W]` is the FROM table of an inner join, so its Company condition
    stays as written."""
    sql = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and "
        "[PL].[PlantCostID] = [PC].[CostID]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_parent_reordered"]
    _, ons = _parts(r.sql)
    assert ons[1] == "[PL].[PlantCostID] = [PC].[CostID] AND [W].[Company] = [PC].[Company]"


def test_a_company_only_join_with_no_other_key_is_left_for_the_governor():
    sql = (
        "select top 5 [A].[PartNum] as [P] from Erp.Part as [A] "
        "inner join Erp.Customer as [C] on [A].[Company] = [C].[Company]"
    )
    r = tp(sql)
    assert r.outcome is Outcome.UNCHANGED


# --------------------------------------------------------------------------- #
# Repair 3 — WHERE, inner joins only; otherwise REFUSE
# --------------------------------------------------------------------------- #

NOT_SWAPPABLE = SEL + W_PL + (
    "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and "
    "[W].[WarehouseCode] = [PC].[PartNum] and [PL].[Plant] = '10'"
)


def test_a_non_swappable_inner_join_condition_moves_to_where():
    """`[PL].[Plant] = '10'` names no column of PC, so it cannot lead with one."""
    r = tp(NOT_SWAPPABLE)
    assert r.rules == ["join_on_third_table"]
    where, ons = _parts(r.sql)
    assert where == "[PL].[Plant] = '10'"
    assert "Plant" not in ons[1]


def test_the_moved_condition_is_anded_onto_an_existing_where_with_its_or_intact():
    r = tp(NOT_SWAPPABLE + " where [W].[Plant] = '10' or [W].[Plant] = '20'")
    where, _ = _parts(r.sql)
    assert where == "([W].[Plant] = '10' OR [W].[Plant] = '20') AND [PL].[Plant] = '10'"


def test_a_non_swappable_outer_join_condition_is_refused_with_no_rewrites_recorded():
    sql = NOT_SWAPPABLE.replace("inner join Erp.PartCost", "left outer join Erp.PartCost")
    r = tp(sql)
    assert r.outcome is Outcome.REFUSED
    assert r.error["error"] == "sql_join_on_third_table"
    assert r.error["detail"] == {
        "join": "Erp.PartCost as [PC]",
        "parent_table": "W",
        "misplaced_conditions": ["[PL].[Plant] = '10'"],
    }
    assert "left outer join" in r.error["message"]
    assert "on the LEFT" in r.error["message"]
    assert r.transformations == []


def test_a_right_join_elsewhere_in_the_select_blocks_the_where_move():
    sql = NOT_SWAPPABLE + (
        " right outer join Erp.Company as [CO] on [W].[Company] = [CO].[Company] and "
        "[W].[Company] = [CO].[Company]"
    )
    r = tp(sql)
    assert r.outcome is Outcome.REFUSED
    assert "RIGHT/FULL" in r.error["message"]


# --------------------------------------------------------------------------- #
# Shapes measured WORKING — must stay byte-identical
# --------------------------------------------------------------------------- #


def test_a_condition_filed_under_the_from_table_of_an_inner_join_is_untouched():
    """Measured running: `[JP]` is the FROM table, so Epicor renders it in WHERE."""
    sql = (
        "select top 20 [JP].[JobNum] as [JobNum] from Erp.JobProd as [JP] "
        "inner join Erp.OrderHed as [OH] on [JP].[Company] = [OH].[Company] and [JP].[OrderNum] = [OH].[OrderNum] "
        "inner join Erp.OrderDtl as [OD] on [OH].[Company] = [OD].[Company] and "
        "[OH].[OrderNum] = [OD].[OrderNum] and [JP].[OrderLine] = [OD].[OrderLine]"
    )
    r = tp(sql)
    assert r.outcome is Outcome.UNCHANGED and r.sql == sql


def test_a_condition_led_by_the_joined_table_is_untouched_even_on_a_left_join():
    """Measured running: the joined table leads, so it renders in its own ON."""
    sql = (
        "select top 5 [P].[PartNum] as [P] from Erp.Part as [P] "
        "inner join Erp.PartPlant as [PP] on [P].[Company] = [PP].[Company] and [P].[PartNum] = [PP].[PartNum] "
        "left outer join Erp.PartCost as [C] on [P].[Company] = [C].[Company] and "
        "[P].[PartNum] = [C].[PartNum] and [C].[CostID] = [PP].[Plant]"
    )
    r = tp(sql)
    assert r.outcome is Outcome.UNCHANGED and r.sql == sql


def test_a_two_table_on_clause_with_a_literal_is_untouched():
    sql = (
        "select top 5 [A].[PartNum] as [P] from Erp.Part as [A] left outer join "
        "Erp.PartPlant as [B] on [A].[Company] = [B].[Company] and [A].[PartNum] = [B].[PartNum] "
        "and [B].[Plant] = 'MfgSys'"
    )
    r = tp(sql)
    assert r.outcome is Outcome.UNCHANGED and r.sql == sql


def test_shapes_it_cannot_read_with_certainty_are_left_alone():
    unqualified = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and [PlantCostID] = [PC].[CostID]"
    )
    subquery = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and [PC].[CostID] in "
        "(select [X].[PlantCostID] from Erp.Plant as [X] where [X].[Plant] = [PL].[Plant])"
    )
    expression_led = SEL + W_PL + (
        "inner join Erp.PartCost as [PC] on [W].[Company] = [PC].[Company] and "
        "isnull([PL].[PlantCostID], '') = [PC].[CostID]"
    )
    for sql in (unqualified, subquery, expression_led):
        r = tp(sql)
        assert not {"join_on_sides_swapped", "join_on_third_table",
                    "join_on_parent_reordered"} & set(r.rules), sql
        assert r.outcome is not Outcome.REFUSED


def test_every_join_in_the_statement_is_repaired_in_one_pass():
    """Two joins in one statement, both led by an earlier joined table."""
    sql = (
        "select top 5 [A].[PartNum] as [P] from Erp.PartBin as [A] "
        "inner join Erp.Warehse as [W] on [A].[Company] = [W].[Company] and "
        "[A].[WarehouseCode] = [W].[WarehouseCode] "
        "inner join Erp.Plant as [PL] on [W].[Company] = [PL].[Company] and [W].[Plant] = [PL].[Plant] "
        "inner join Erp.PartCost as [PC] on [A].[Company] = [PC].[Company] and "
        "[A].[PartNum] = [PC].[PartNum] and [PL].[PlantCostID] = [PC].[CostID] "
        "inner join Erp.PartPlant as [PP] on [A].[Company] = [PP].[Company] and "
        "[A].[PartNum] = [PP].[PartNum] and [W].[Plant] = [PP].[Plant]"
    )
    r = tp(sql)
    assert r.rules == ["join_on_sides_swapped", "join_on_sides_swapped"]
    assert "[PC].[CostID] = [PL].[PlantCostID]" in r.sql
    assert "[PP].[Plant] = [W].[Plant]" in r.sql
