"""Regression coverage: test ud column rewrite."""

from __future__ import annotations

import asyncio
import json

import pytest

from epicor_mcp.discovery.authz import AuthzScope, normalize_table
from epicor_mcp.sql import denylist
from epicor_mcp.sql import validate_columns as colvalid
from epicor_mcp.sql.adhoc import EXECUTE_PATH, PARSE_PATH, run_sql
from epicor_mcp.sql.transpile import WEDGE_POLICY, Outcome, transpile
from epicor_mcp.sql.validate_columns import (
    ColumnCatalogue,
    UdMirror,
    UdMirrorMap,
    load_ud_mirrors,
    splice_ud_joins,
    ud_retry_sql,
    validate_columns,
    _parse,
)
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

BASE = "https://example.invalid/api/v2/odata/DEMO"

# --------------------------------------------------------------------------- #
# Fixtures — a small catalogue and mirror map, in the loader's own shapes
# --------------------------------------------------------------------------- #

CAT = ColumnCatalogue(
    {
        "Customer": [
            {"name": "CustID", "type": "nvarchar"},
            {"name": "Name", "type": "nvarchar"},
            {"name": "SysRowID", "type": "uniqueidentifier"},
        ],
        "Part": [
            {"name": "PartNum", "type": "nvarchar"},
            {"name": "SysRowID", "type": "uniqueidentifier"},
        ],
    }
)

MIRRORS = UdMirrorMap(
    [
        UdMirror(
            parent="Customer",
            table="Erp.Customer_UD",
            columns=frozenset({"foreignsysrowid", "ud_sysrevid", "flagged_c", "sponsor_c"}),
            custom=("Flagged_c", "Sponsor_c"),
        ),
        UdMirror(
            parent="Part",
            table="Erp.Part_UD",
            columns=frozenset({"foreignsysrowid", "ud_sysrevid", "onhold_c"}),
            custom=("OnHold_c",),
        ),
    ]
)

SQL = (
    "select top 5 [P].[CustID] as [ID], [P].[Flagged_c] as [FLAG] "
    "from Erp.Customer as [P] where [P].[Flagged_c] = 1 "
    "group by [P].[CustID], [P].[Flagged_c] order by [P].[Flagged_c]"
)

JOIN_ON = "ON [P].[SysRowID] = [Customer_UD].[ForeignSysRowID]"


def splice(sql: str, mirrors=MIRRORS, catalogue=CAT):
    root = _parse(sql)
    assert root is not None, sql
    return root, splice_ud_joins(root, mirrors=mirrors, catalogue=catalogue)


def out_sql(root) -> str:
    return root.sql(dialect="tsql", pretty=False)


# --------------------------------------------------------------------------- #
# The splice engine — what qualifies
# --------------------------------------------------------------------------- #


def test_every_clause_is_requalified_select_where_group_by_order_by():
    root, splices = splice(SQL)
    assert len(splices) == 1
    s = splices[0]
    assert (s.parent, s.mirror, s.alias, s.qualifier) == (
        "Customer", "Erp.Customer_UD", "Customer_UD", "P"
    )
    assert s.columns == ("Flagged_c",)
    out = out_sql(root)
    assert f"LEFT OUTER JOIN Erp.Customer_UD AS [Customer_UD] {JOIN_ON}" in out
    # Every reference moved to the mirror — none left on the parent.
    assert "[P].[Flagged_c]" not in out
    assert out.count("[Customer_UD].[Flagged_c]") == 4  # select + where + group + order
    assert "GROUP BY [P].[CustID], [Customer_UD].[Flagged_c]" in out
    assert "ORDER BY [Customer_UD].[Flagged_c]" in out


def test_a_bare_ref_on_a_single_unaliased_source_qualifies_by_table_name():
    root, splices = splice("select top 5 Flagged_c from Erp.Customer")
    assert len(splices) == 1
    assert splices[0].qualifier == "Customer"
    out = out_sql(root)
    assert "ON [Customer].[SysRowID] = [Customer_UD].[ForeignSysRowID]" in out
    assert "[Customer_UD].Flagged_c" in out or "[Customer_UD].[Flagged_c]" in out


def test_two_parents_get_one_join_each():
    sql = (
        "select top 5 [C].[Flagged_c], [P].[OnHold_c] from Erp.Customer as [C] "
        "inner join Erp.Part as [P] on [C].[CustID] = [P].[PartNum]"
    )
    root, splices = splice(sql)
    assert {(s.mirror, s.alias) for s in splices} == {
        ("Erp.Customer_UD", "Customer_UD"),
        ("Erp.Part_UD", "Part_UD"),
    }
    out = out_sql(root)
    assert out.count("LEFT OUTER JOIN") == 2


def test_many_refs_on_one_parent_still_mean_one_join():
    sql = "select top 5 [P].[Flagged_c], [P].[Sponsor_c] from Erp.Customer as [P]"
    root, splices = splice(sql)
    assert len(splices) == 1
    assert splices[0].columns == ("Flagged_c", "Sponsor_c")
    assert out_sql(root).count("LEFT OUTER JOIN") == 1


def test_a_self_join_gets_one_mirror_join_per_alias_with_distinct_aliases():
    sql = (
        "select top 5 [A].[Flagged_c], [B].[Flagged_c] from Erp.Customer as [A] "
        "inner join Erp.Customer as [B] on [A].[CustID] = [B].[CustID]"
    )
    root, splices = splice(sql)
    assert {s.alias for s in splices} == {"Customer_UD", "Customer_UD2"}
    out = out_sql(root)
    assert "ON [A].[SysRowID] = [Customer_UD].[ForeignSysRowID]" in out
    assert "ON [B].[SysRowID] = [Customer_UD2].[ForeignSysRowID]" in out


def test_the_mirror_alias_never_collides_with_an_existing_alias():
    sql = "select top 5 [Customer_UD].[Flagged_c] from Erp.Customer as [Customer_UD]"
    root, splices = splice(sql)
    assert len(splices) == 1
    assert splices[0].alias == "Customer_UD2"
    assert "[Customer_UD2].[Flagged_c]" in out_sql(root)


def test_a_ref_already_on_the_mirror_needs_nothing_and_gets_nothing():
    sql = (
        "select top 5 [U].[Flagged_c] from Erp.Customer as [P] "
        "left outer join Erp.Customer_UD as [U] "
        "on [P].[SysRowID] = [U].[ForeignSysRowID]"
    )
    _, splices = splice(sql)
    assert splices == []


# --------------------------------------------------------------------------- #
# The splice engine — what falls back to layer 1 (decline = NOTHING mutated)
# --------------------------------------------------------------------------- #

DECLINED = [
    pytest.param(  # the caller already joined the mirror but misqualified the ref
        "select top 5 [P].[Flagged_c] from Erp.Customer as [P] left outer join "
        "Erp.Customer_UD as [U] on [P].[SysRowID] = [U].[ForeignSysRowID]",
        id="mirror-already-joined",
    ),
    pytest.param(  # set operation
        "select top 5 [P].[Flagged_c] from Erp.Customer as [P] union all "
        "select top 5 [X].[Flagged_c] from Erp.Customer as [X]",
        id="setop",
    ),
    pytest.param(  # CTE — a CTE name can shadow a base table (the S1/S3 class)
        "with [C] as (select [P].[Flagged_c] from Erp.Customer as [P]) "
        "select top 5 [C].[Flagged_c] from [C]",
        id="cte",
    ),
    pytest.param(  # subquery in the WHERE
        "select top 5 [P].[Flagged_c] from Erp.Customer as [P] where [P].[CustID] in "
        "(select [X].[CustID] from Erp.Customer as [X])",
        id="subquery",
    ),
    pytest.param(  # bare ref with two sources: attribution would be a guess
        "select top 5 Flagged_c from Erp.Customer as [C], Erp.Part as [P]",
        id="ambiguous-bare",
    ),
    pytest.param(  # unresolvable alias
        "select top 5 [Z].[Flagged_c] from Erp.Customer as [P]",
        id="unresolved-alias",
    ),
    pytest.param(  # the mirror does not carry this column — refusing to guess
        "select top 5 [P].[Nope_c] from Erp.Customer as [P]",
        id="not-on-the-mirror",
    ),
    pytest.param(  # a bare non-_c column that exists on the mirror would become
        # ambiguous the moment the join lands
        "select top 5 ForeignSysRowID, [P].[Flagged_c] from Erp.Customer as [P]",
        id="bare-collision-with-mirror-column",
    ),
    pytest.param(  # `_c` ref inside an existing ON clause: T-SQL scoping means
        # an EARLIER on-clause cannot see the join we append last
        "select top 5 [C].[CustID] from Erp.Customer as [C] inner join "
        "Erp.Part as [P] on [C].[Flagged_c] = [P].[PartNum]",
        id="ref-inside-a-join-on",
    ),
    pytest.param(  # a bare `_c` name that is also an OUTPUT alias is not
        # attributable without guessing which one the caller meant
        "select top 5 [P].[Flagged_c] as [Flagged_c] from Erp.Customer as [P] "
        "order by Flagged_c",
        id="output-alias-shadow",
    ),
    pytest.param(  # select * — the lint owns that refusal and the rewrite would
        # silently widen what * covers
        "select top 5 * from Erp.Customer as [P] where [P].[Flagged_c] = 1",
        id="select-star",
    ),
]


@pytest.mark.parametrize("sql", DECLINED)
def test_declined_shapes_mutate_nothing(sql):
    root = _parse(sql)
    before = out_sql(root)
    assert splice_ud_joins(root, mirrors=MIRRORS, catalogue=CAT) == []
    assert out_sql(root) == before  # decline touched NOTHING


@pytest.mark.parametrize("sql", DECLINED)
def test_declined_shapes_pass_through_transpile_byte_identical(sql):
    result = transpile(sql, policy=WEDGE_POLICY, ud_mirrors=MIRRORS, ud_catalogue=CAT)
    if result.outcome is Outcome.UNCHANGED:
        assert result.sql == sql
    assert "ud_mirror_join" not in result.rules


def test_a_real_parent_column_ending_in_c_is_left_alone():
    """A column the catalogue PROVES on the parent is not ours to touch."""
    cat = ColumnCatalogue(
        {
            "Customer": [
                {"name": "CustID"},
                {"name": "SysRowID"},
                {"name": "Weird_c"},  # hypothetical: a real physical `_c` on the base
            ]
        }
    )
    mirrors = UdMirrorMap(
        [
            UdMirror(
                "Customer", "Erp.Customer_UD",
                frozenset({"foreignsysrowid", "weird_c"}), ("Weird_c",),
            )
        ]
    )
    sql = "select top 5 [P].[Weird_c] from Erp.Customer as [P]"
    root = _parse(sql)
    assert splice_ud_joins(root, mirrors=mirrors, catalogue=cat) == []


# --------------------------------------------------------------------------- #
# Layer 2 through transpile — the announced rewrite, and byte-identity
# --------------------------------------------------------------------------- #


def test_the_rewrite_is_announced_with_the_probe_as_proof():
    result = transpile(SQL, policy=WEDGE_POLICY, ud_mirrors=MIRRORS, ud_catalogue=CAT)
    assert result.outcome is Outcome.REWRITTEN
    assert result.rules == ["ud_mirror_join"]
    t = result.transformations[0]
    assert t.proof == "VERIFIED"
    assert "Engine compatibility behavior" in t.evidence  # the known-answer proof, cited
    assert "Erp.Customer_UD" in t.message
    assert t.before == "[P].[Flagged_c]"
    assert t.after == "[Customer_UD].[Flagged_c]"
    assert JOIN_ON in result.sql


def test_no_mirror_map_means_the_pass_does_not_exist():
    """Every pre-UD call site is byte-identical: same outcome, same text."""
    with_none = transpile(SQL, policy=WEDGE_POLICY)
    assert with_none.outcome is Outcome.UNCHANGED
    assert with_none.sql == SQL
    assert "ud_mirror_join" not in with_none.rules


def test_a_statement_without_c_refs_is_unchanged_byte_identical():
    sql = "select top 5 [P].[CustID] from Erp.Customer as [P]"
    result = transpile(sql, policy=WEDGE_POLICY, ud_mirrors=MIRRORS, ud_catalogue=CAT)
    assert result.outcome is Outcome.UNCHANGED
    assert result.sql == sql


def test_a_catalogued_parent_without_sysrowid_declines_the_rewrite():
    """The rewrite must clear E14 one stage later — a retry another gate
    refuses is turn two of the same loop."""
    cat = ColumnCatalogue({"Customer": [{"name": "CustID"}]})  # no SysRowID
    sql = "select top 5 [P].[Flagged_c] from Erp.Customer as [P]"
    root = _parse(sql)
    assert splice_ud_joins(root, mirrors=MIRRORS, catalogue=cat) == []


# --------------------------------------------------------------------------- #
# Layer 1 — the E14 envelope names the verified mirror and hands back the fix
# --------------------------------------------------------------------------- #


def test_layer1_envelope_names_the_mirror_and_serves_the_spliced_retry():
    v = validate_columns(SQL, catalogue=CAT, ud_mirrors=MIRRORS)
    assert v.ok is False
    env = v.envelope
    help_text = env["valid"]["user_defined_columns"]["Customer.Flagged_c"]
    assert "Erp.Customer_UD" in help_text and "VERIFIED" in help_text
    assert "ForeignSysRowID" in help_text
    # The mirror leads column_lives_on — the column genuinely lives there.
    assert env["valid"]["column_lives_on"]["Customer.Flagged_c"][0] == "Erp.Customer_UD"
    # No difflib guess beside the verified answer.
    assert "Customer.Flagged_c" not in env["valid"].get("did_you_mean", {})
    retry = env["retry_with"]["sql"]
    assert JOIN_ON in retry
    assert "[P].[Flagged_c]" not in retry


def test_layer1_retry_equals_the_layer2_rewrite_one_engine_no_disagreement():
    v = validate_columns(SQL, catalogue=CAT, ud_mirrors=MIRRORS)
    t = transpile(SQL, policy=WEDGE_POLICY, ud_mirrors=MIRRORS, ud_catalogue=CAT)
    assert v.envelope["retry_with"]["sql"] == t.sql


def test_layer1_without_the_map_keeps_the_original_prose_and_no_retry():
    """`ud_mirrors=None` is byte-identically the pre-UD-mirror behaviour."""
    v = validate_columns(SQL, catalogue=CAT)
    assert v.ok is False
    help_text = v.envelope["valid"]["user_defined_columns"]["Customer.Flagged_c"]
    assert "extension table Erp.Customer_UD" in help_text  # the old, unverified recipe
    assert "VERIFIED" not in help_text
    assert "retry_with" not in v.envelope


def test_layer1_is_honest_when_the_mirror_lacks_the_column():
    v = validate_columns(
        "select top 5 [P].[Nope_c] from Erp.Customer as [P]",
        catalogue=CAT,
        ud_mirrors=MIRRORS,
    )
    help_text = v.envelope["valid"]["user_defined_columns"]["Customer.Nope_c"]
    assert "does not carry it" in help_text
    assert "Flagged_c" in help_text  # the _c columns that DO exist there
    assert "retry_with" not in v.envelope


def test_layer1_withholds_the_retry_when_the_splice_declines():
    """Same statement class layer 2 declines (subquery) — the envelope still
    names the verified mirror, but hands back no statement it cannot build."""
    sql = (
        "select top 5 [P].[Flagged_c] from Erp.Customer as [P] where [P].[CustID] in "
        "(select [X].[CustID] from Erp.Customer as [X])"
    )
    v = validate_columns(sql, catalogue=CAT, ud_mirrors=MIRRORS)
    assert v.ok is False
    assert "VERIFIED" in v.envelope["valid"]["user_defined_columns"]["Customer.Flagged_c"]
    assert "retry_with" not in v.envelope


def test_mixed_unknowns_get_no_half_fixed_retry():
    """One verified `_c` + one ordinary typo: a retry that fixes only half the
    statement is a second failed hop, so none is served."""
    sql = "select top 5 [P].[Custd], [P].[Flagged_c] from Erp.Customer as [P]"
    v = validate_columns(sql, catalogue=CAT, ud_mirrors=MIRRORS)
    assert v.ok is False
    assert "retry_with" not in v.envelope
    # Both halves are still individually diagnosed.
    assert "Customer.Flagged_c" in v.envelope["valid"]["user_defined_columns"]
    assert v.corrections.get("P.Custd") == "CustID"


def test_ud_retry_sql_refuses_a_statement_that_leaves_a_name_unfixed():
    assert (
        ud_retry_sql(SQL, mirrors=MIRRORS, catalogue=CAT, must_fix=["Flagged_c", "Ghost_c"])
        is None
    )


# --------------------------------------------------------------------------- #
# The loader — deny-filtered, Erp-only, absence-safe (invariant 14 for the map)
# --------------------------------------------------------------------------- #


def _catalogue_file(tmp_path, tables: dict) -> str:
    p = tmp_path / "schema_catalogue.json"
    p.write_text(json.dumps({"generated": "2000-01-01", "tables": tables}))
    return str(p)


def _entry(schema: str, *names: str) -> dict:
    return {"schema": schema, "fields": [{"name": n, "type": "nvarchar"} for n in names]}


def test_the_loader_keeps_a_clean_mirror_and_drops_every_unsafe_one(tmp_path):
    path = _catalogue_file(
        tmp_path,
        {
            # the one good mirror
            "Customer": _entry("Erp", "CustID", "SysRowID"),
            "Customer_UD": _entry("Erp", "ForeignSysRowID", "Flagged_c"),
            # denied parent (exact pattern) — the mirror must be as invisible
            "UserFile": _entry("Erp", "SysRowID"),
            "UserFile_UD": _entry("Erp", "ForeignSysRowID", "Secret_c"),
            # denied parent, payroll wildcard
            "PREmpMas": _entry("Erp", "SysRowID"),
            "PREmpMas_UD": _entry("Erp", "ForeignSysRowID", "Pay_c"),
            # An Ice security-master mirror must not enter the Erp-only map.
            # Erp-only is a STRUCTURAL exclusion here, independent of the
            # deny-list's pattern coverage.
            "SysUserFile": _entry("Ice", "SysRowID"),
            "SysUserFile_UD": _entry("Ice", "ForeignSysRowID", "Hack_c"),
            # mirror without ForeignSysRowID: the join cannot be proven
            "Vendor": _entry("Erp", "VendorID", "SysRowID"),
            "Vendor_UD": _entry("Erp", "VendGrp_c"),
            # parent without SysRowID: the other half of the join is unproven
            "NoRowId": _entry("Erp", "Id"),
            "NoRowId_UD": _entry("Erp", "ForeignSysRowID", "Y_c"),
            # mirror whose parent is absent from the catalogue entirely
            "Orphan_UD": _entry("Erp", "ForeignSysRowID", "X_c"),
            # mirror with no _c columns at all: nothing to route
            "Part": _entry("Erp", "PartNum", "SysRowID"),
            "Part_UD": _entry("Erp", "ForeignSysRowID", "UD_SysRevID"),
        },
    )
    m = load_ud_mirrors(path)
    assert len(m) == 1
    mirror = m.mirror_for("Customer")
    assert mirror is not None and mirror.table == "Erp.Customer_UD"
    assert mirror.custom == ("Flagged_c",)
    for denied in ("UserFile", "PREmpMas", "SysUserFile", "Vendor", "NoRowId", "Orphan"):
        assert m.mirror_for(denied) is None


def test_the_loader_returns_an_empty_map_for_a_missing_file(tmp_path):
    m = load_ud_mirrors(tmp_path / "nowhere.json")
    assert not m
    assert m.mirror_for("Customer") is None


def test_the_loader_survives_garbage_without_raising(tmp_path):
    p = tmp_path / "schema_catalogue.json"
    p.write_text("{not json")
    assert not load_ud_mirrors(p)


def test_the_deployed_map_if_present_names_no_denied_table():
    """Belt and braces over the REAL data file (vacuous on a clean checkout —
    the file is gitignored — which is exactly the absence-safe contract)."""
    m = load_ud_mirrors()
    for parent in ("UserFile", "SysUserFile", "PREmpMas", "PayrollExp"):
        assert m.mirror_for(parent) is None


# --------------------------------------------------------------------------- #
# The whole pipe — mock client, real gates, in the real order
# --------------------------------------------------------------------------- #


def _ud_parse_ds() -> dict:
    """A ParseFromSQL runtime DS for the SPLICED statement, in the captured
    fixtures' own field shapes (`gov_bounded_join` is the two-table template)."""
    sq = "063211e4-9fda-40d9-883f-7037136600ud"
    return {
        "QuerySubQuery": [
            {"SubQueryID": sq, "Type": "TopLevel", "SelectListClause": "Top",
             "TopInPercent": False, "TopRowExpr": 5.0}
        ],
        "QueryTable": [
            {"SubQueryID": sq, "TableID": "P", "DBSchemaName": "Erp",
             "DBTableName": "Customer", "TableType": "DB"},
            {"SubQueryID": sq, "TableID": "Customer_UD", "DBSchemaName": "Erp",
             "DBTableName": "Customer_UD", "TableType": "DB"},
        ],
        "QueryField": [
            {"SubQueryID": sq, "TableID": "P", "DBSchemaName": "Erp",
             "DBTableName": "Customer", "DBFieldName": "CustID", "FieldName": "CustID",
             "Alias": "ID", "DataType": "nvarchar", "IsCalculated": False,
             "Formula": "", "IsGroupBy": True, "Seq": 1},
            {"SubQueryID": sq, "TableID": "Customer_UD", "DBSchemaName": "Erp",
             "DBTableName": "Customer_UD", "DBFieldName": "Flagged_c",
             "FieldName": "Flagged_c", "Alias": "FLAG", "DataType": "int",
             "IsCalculated": False, "Formula": "", "IsGroupBy": True, "Seq": 2},
        ],
        "QuerySortBy": [],
        "QueryWhereItem": [],
        "QueryRelation": [
            {"SubQueryID": sq, "RelationID": "r1", "ParentTableID": "P",
             "ChildTableID": "Customer_UD", "JoinType": "Outer", "OuterJoin": True}
        ],
        "QueryRelationField": [],
        "QueryGroupBy": [],
    }


@pytest.fixture
def mirrors_loaded(monkeypatch):
    monkeypatch.setattr(colvalid, "load_ud_mirrors", lambda path=None: MIRRORS)


@pytest.fixture
def parent_only_scope(monkeypatch):
    """A REAL AuthzScope holding only the parent. The `_UD` pass comes from
    `AuthzScope.allows`' UD inheritance. The compatibility branch supplies
    inheritance for isolated runs against older authorization implementations."""
    scope = AuthzScope.scoped(
        "ud@example.org", {"Customer"}, "menu chain"
    )
    if not scope.allows("Erp.Customer_UD"):  # pragma: no cover — inheritance landed
        real = AuthzScope.allows

        def with_inheritance(self, name):
            if real(self, name):
                return True
            norm = normalize_table(name)
            return norm.endswith("_ud") and bool(norm[:-3]) and real(self, norm[:-3])

        monkeypatch.setattr(AuthzScope, "allows", with_inheritance)
    return scope


def call(sql: str, client: MockEpicorClient, **kw) -> dict:
    return asyncio.run(run_sql(sql, client=client, api_key="k", base_url=BASE, **kw))


def test_layer2_rewrite_traverses_every_gate_and_returns_rows(
    mirrors_loaded, parent_only_scope
):
    """transpile (splice) → E14 → parse → deny-list → scope gate (parent-only
    SCOPED scope: the mirror passes by INHERITANCE) → lint → governor → Execute."""
    client = MockEpicorClient(
        parse_ds=_ud_parse_ds(),
        execute_response=ok_execute([{"ID": "C001", "FLAG": 1}]),
    )
    out = call(SQL, client, table_scope=parent_only_scope, diagnose=False)
    assert out["success"] is True, out
    assert out["row_count"] == 1
    # The rewrite is ANNOUNCED, with the probe as proof.
    rewrites = out["assumptions"]["rewrites"]
    assert [r["rule"] for r in rewrites] == ["ud_mirror_join"]
    assert "Engine compatibility behavior" in rewrites[0]["evidence"]
    # What Epicor was actually sent is the spliced statement.
    sent = client.body_for("ParseFromSQL")["ds"]["DynamicQueryDesigner"][0][
        "DisplayPhrase"
    ]
    assert JOIN_ON in sent
    assert out["sql_executed"] == sent
    assert set(out["tables_read"]) == {"Erp.Customer", "Erp.Customer_UD"}
    assert client.paths == [f"{BASE}/{PARSE_PATH}", f"{BASE}/{EXECUTE_PATH}"]


def test_the_spliced_statement_is_refused_when_even_the_parent_is_out_of_scope(
    mirrors_loaded,
):
    """Inheritance widens a scope to the mirror, never to the parent: no
    Customer grant means the spliced statement still refuses at stage authz,
    with ZERO Execute calls."""
    scope = AuthzScope.scoped("ud@example.org", {"Part"}, "menu chain")
    client = MockEpicorClient(parse_ds=_ud_parse_ds())
    out = call(SQL, client, table_scope=scope, diagnose=False)
    assert out["success"] is False
    assert out["error"] == "table_not_authorized"
    assert out["detail"]["stage"] == "authz"
    assert not client.called("Execute")


def test_layer1_recovery_traverses_the_same_pipe(mirrors_loaded, parent_only_scope):
    """The retry the envelope hands back is driven
    through the WHOLE pipe and is not refused by any later gate."""
    v = validate_columns(SQL, catalogue=CAT, ud_mirrors=MIRRORS)
    retry = v.envelope["retry_with"]["sql"]
    assert JOIN_ON in retry
    client = MockEpicorClient(
        parse_ds=_ud_parse_ds(),
        execute_response=ok_execute([{"ID": "C001", "FLAG": 1}]),
    )
    out = call(retry, client, table_scope=parent_only_scope, diagnose=False)
    assert out["success"] is True, out
    # The recovery needed NO further rewriting — the splice engine recognises
    # its own output (refs already on the mirror) and touches nothing.
    assert "rewrites" not in out["assumptions"]
    assert set(out["tables_read"]) == {"Erp.Customer", "Erp.Customer_UD"}


def test_in_pipe_layer1_envelope_costs_zero_epicor_calls(mirrors_loaded):
    """A shape layer 2 declines (subquery) with a judged `_c` phantom: E14
    refuses BEFORE ParseFromSQL, the envelope names the VERIFIED mirror, and
    no retry is served because the same engine cannot splice it either."""
    sql = (
        "select top 5 [P].[Flagged_c] from Erp.Customer as [P] where [P].[CustID] in "
        "(select [X].[CustID] from Erp.Customer as [X])"
    )
    client = MockEpicorClient(parse_ds=_ud_parse_ds())
    out = call(sql, client, diagnose=False)
    assert out["success"] is False
    assert out["error"] == "sql_unknown_column"
    assert out["detail"]["stage"] == "validate_columns"
    assert out["detail"]["epicor_calls"] == 0
    assert client.calls == []
    help_text = out["valid"]["user_defined_columns"]["Customer.Flagged_c"]
    assert "Erp.Customer_UD" in help_text and "VERIFIED" in help_text
    assert "retry_with" not in out


def test_a_statement_without_c_refs_never_loads_the_mirror_map(monkeypatch):
    """The `_c` hint gate: for a `_c`-free statement the whole feature —
    including the catalogue read — must not exist."""

    def boom(path=None):  # pragma: no cover — the point is it is never reached
        raise AssertionError("load_ud_mirrors was called for a _c-free statement")

    monkeypatch.setattr(colvalid, "load_ud_mirrors", boom)
    sql, ds = load("clean_top")
    assert "_c" not in sql.lower()
    client = MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute([{"PN": "ABC-1"}])
    )
    out = call(sql, client, diagnose=False)
    assert out["success"] is True


def test_the_deny_list_still_beats_the_mirror_map_in_the_pipe(mirrors_loaded):
    """A statement reading a denied table refuses at stage denylist exactly as
    before — the mirror map changes nothing on that path, and the envelope
    names no `_UD` mirror."""
    sql, ds = load("deny_table")
    client = MockEpicorClient(parse_ds=ds)
    out = call(sql, client, diagnose=False)
    assert out["success"] is False
    assert out["detail"]["stage"] == "denylist"
    assert "_UD" not in json.dumps(out)
    assert not client.called("Execute")


def test_denylist_ud_inheritance_holds_for_the_maps_own_universe():
    """Every mirror the loader could ever serve has a non-denied parent AND a
    non-denied mirror name — asserted through `is_denied_table` itself, so the
    denylist's `_UD` inheritance and this map can never disagree."""
    assert denylist.is_denied_table("Erp.UserFile_UD")
    assert denylist.is_denied_table("Ice.SysUserFile_UD")
    assert denylist.is_denied_table("SysUserFile_UD")
    assert denylist.is_denied_table("Erp.PREmpMas_UD")
    assert not denylist.is_denied_table("Erp.Customer_UD")
    for mirror in (MIRRORS.mirror_for("Customer"), MIRRORS.mirror_for("Part")):
        assert not denylist.is_denied_table(mirror.table)
        assert not denylist.is_denied_table(mirror.parent)
