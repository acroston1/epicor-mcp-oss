"""Regression coverage: test ud authz inheritance."""

from __future__ import annotations

import asyncio

from epicor_mcp.discovery.authz import AuthzScope, TableAuthorizer
from epicor_mcp.discovery.tools import register_discovery_tools
from epicor_mcp.sql import denylist, scope_gate
from epicor_mcp.sql.denylist import is_denied_column, is_denied_table
from epicor_mcp.sql.validate_columns import ColumnCatalogue

# --------------------------------------------------------------------------- #
# AuthzScope.allows() — the SCOPED-membership fallback
# --------------------------------------------------------------------------- #
_JOB = AuthzScope.scoped("u@example.org", ["JobHead"], "test scope")


def test_a_scoped_parent_reaches_its_mirror_in_every_spelling():
    """The inheritance rides normalize_table, so every spelling of the mirror
    resolves exactly as every spelling of the parent does."""
    for spelling in (
        "Erp.JobHead_UD",
        "JobHead_UD",
        "[Erp].[JobHead_UD]",
        "jobhead_ud",
        " JobHead_UD ",
        "JOBHEAD_UD",
    ):
        assert _JOB.allows(spelling), spelling


def test_the_inheritance_never_widens_beyond_the_parent():
    assert not _JOB.allows("Erp.POHeader_UD")
    assert not _JOB.allows("POHeader_UD")
    assert not _JOB.allows("POHeader")


def test_mirror_membership_is_directional():
    """A mirror explicitly in scope is a DIRECT hit (tested first); it grants
    nothing about the parent — inheritance flows parent -> mirror only."""
    scope = AuthzScope.scoped("u@x", ["Part_UD"], "t")
    assert scope.allows("Part_UD")
    assert not scope.allows("Part")


def test_the_bare_suffix_never_retests_the_empty_string():
    """Strip only when the remainder is non-empty: a name that IS the suffix
    must fail on its own membership, never re-test ""."""
    assert not _JOB.allows("_UD")
    assert not _JOB.allows("Erp._UD")
    assert not _JOB.allows("")


def test_unavailable_still_refuses_mirrors_and_parents_alike():
    scope = AuthzScope.unavailable("u@x", "snapshot failed: Boom")
    assert not scope.allows("JobHead")
    assert not scope.allows("JobHead_UD")


def test_unlimited_is_unchanged():
    scope = AuthzScope.unlimited("u@x", "SecurityMgr", security_mgr=True)
    assert scope.allows("Anything_UD")
    assert scope.allows("_UD")  # UNLIMITED never reaches the strip logic


def test_an_empty_default_deny_scope_reaches_no_mirror_either():
    """Default deny: a zero-service user's scope is EMPTY, so inheritance has
    no parent to follow — no table and no mirror is reachable without a menu
    grant."""
    scope = AuthzScope.scoped("u@x", frozenset(), "menu chain resolved zero services — no tables")
    for name in ("Erp.JobHead_UD", "JobHead_UD", "Customer_UD", "Part_UD",
                 "PartMtl_UD", "JobHead", "_UD"):
        assert not scope.allows(name), name


class _OneSnap:
    def __init__(self, services):
        self.is_error = self.security_mgr = self.allow_all = False
        self.error = ""
        self.allowed_services = tuple(services)

    async def ensure_snapshot(self, email):
        return self


class _SvcIndex:
    def get_entity_sets(self, sid):
        return {"Erp.BO.JobEntrySvc": ["JobHeads"]}.get(sid, [])


def test_a_menu_projected_parent_extends_to_its_mirror_through_the_real_authorizer():
    """The inheritance on a scope the REAL TableAuthorizer computed from a menu
    snapshot (default deny): the menu-mapped parent's mirror is reachable; a
    table no menu granted — and its mirror — is not."""
    snap = _OneSnap(["Erp.BO.JobEntrySvc"])
    scope = asyncio.run(
        TableAuthorizer(snap, _SvcIndex(), mode="gate").scope_for("u@example.org")
    )
    assert scope.allows("Erp.JobHead") and scope.allows("Erp.JobHead_UD")
    for name in ("Customer", "Customer_UD", "PartMtl", "PartMtl_UD", "Part_UD"):
        assert not scope.allows(name), name


# --------------------------------------------------------------------------- #
# is_denied_table — deny inheritance
# --------------------------------------------------------------------------- #
def test_wildcard_patterns_already_cover_their_mirrors_pinned():
    """The recon fact that made the code change SMALL: a prefix wildcard
    matches the mirror by construction. Pinned so a rewrite of the pattern
    matcher cannot silently lose it."""
    for name in (
        "Erp.PREmpMas_UD",
        "PREmpMas_UD",
        "Erp.UserFile_UD",
        "UserFile_UD",
        "Erp.PayrollExp_UD",
        "Ice.UserComp_UD",
    ):
        assert is_denied_table(name), name


def test_exact_patterns_now_deny_their_mirrors():
    """Regression coverage: test exact patterns now deny their mirrors."""
    for name in (
        "Ice.SysUserFile_UD",
        "Erp.SysUserFile_UD",
        "SysUserFile_UD",
        "ICE.SYSUSERFILE_UD",
        "ice.sysuserfile_ud",
        "Erp.ExtPREmp_UD",
        "ExtPREmp_UD",
        "Ice.ExtSecurity_UD",
        "Erp.ExtSecurity_UD",
        "ExtSecurity_UD",
        "Erp.EmpBasicAttch_UD",
        "EmpBasicAttch_UD",
    ):
        assert is_denied_table(name), name


def test_an_allowed_parents_mirror_stays_allowed():
    """Deny inheritance must not widen the deny-list: every catalogued mirror
    of a non-denied parent stays servable."""
    for name in (
        "Erp.JobHead_UD",
        "JobHead_UD",
        "Erp.Customer_UD",
        "Part_UD",
        "Erp.Vendor_UD",
        "Erp.QuoteHed_UD",
        "Erp.DMRHead_UD",
    ):
        assert not is_denied_table(name), name


def test_field_resolution_shapes_and_their_denied_mirrors_are_untouched():
    """`Erp.Pr<lower>` business tables must never regress into the payroll
    family — with or without the suffix."""
    for name in (
        "Erp.ProdGrup",
        "Erp.Project",
        "Erp.Project_UD",
        "Erp.PriceLst",
        "Prospect",
        "Erp.ProjPhase",
    ):
        assert not is_denied_table(name), name


def test_the_bare_suffix_is_not_itself_denied():
    """`_UD` alone strips to the empty string, which must never be re-tested
    (the bare-name fallback prefix test would fail OPEN on ""), so the strip
    is refused instead."""
    assert not is_denied_table("_UD")
    assert not is_denied_table("Erp._UD")


def test_check_parsed_ds_denies_a_security_masters_mirror():
    """The runtime deny gate reads Epicor's own resolution — the inheritance
    must fire there, not just on the helper."""
    ds = {
        "QueryTable": [
            {
                "TableID": "SUF",
                "TableType": "DB",
                "DBSchemaName": "Ice",
                "DBTableName": "SysUserFile_UD",
            }
        ]
    }
    d = denylist.check_parsed_ds(ds)
    assert d, "the mirror must deny the query"
    assert any("SysUserFile_UD" in t for t in d.denied_tables)


# --------------------------------------------------------------------------- #
# E14 — the catalogue exclusion inherits the denial automatically
# --------------------------------------------------------------------------- #
_SYNTHETIC_CATALOGUE = {
    "Ice.SysUserFile_UD": ["Config_c", "ForeignSysRowID"],
    "Erp.UserFile_UD": ["Note_c"],
    "Erp.JobHead": ["JobNum"],
    "Erp.JobHead_UD": ["Approver_c", "ForeignSysRowID"],
}


def test_e14_catalogue_exclusion_inherits_the_denial():
    """`_safe_catalogue` filters with `is_denied_table` over bare names, so the
    inheritance propagates with no adhoc.py change — pinned at the seam the
    pipe actually uses (`ColumnCatalogue.excluding`)."""
    cat = ColumnCatalogue(_SYNTHETIC_CATALOGUE)
    safe = cat.excluding(is_denied_table)
    assert not safe.knows_table("SysUserFile_UD")
    assert not safe.knows_table("UserFile_UD")
    assert safe.knows_table("JobHead")
    assert safe.knows_table("JobHead_UD"), "an allowed parent's mirror is servable"
    # `lives_on` feeds every envelope's column_lives_on — it may not name a
    # Denied mirror: a forbidden table must not leak field metadata.
    assert safe.lives_on("Config_c") == []
    assert safe.lives_on("Note_c") == []
    assert safe.lives_on("Approver_c") == ["JobHead_UD"]


def test_the_pipes_safe_catalogue_drops_a_denied_mirror(monkeypatch):
    """Regression coverage: test the pipes safe catalogue drops a denied mirror."""
    from epicor_mcp.sql import adhoc

    cat = ColumnCatalogue(_SYNTHETIC_CATALOGUE)
    monkeypatch.setattr(adhoc.colvalid, "load_catalogue", lambda: cat)
    monkeypatch.setattr(adhoc, "_SAFE_CATALOGUE", None)
    safe = adhoc._safe_catalogue()
    assert not safe.knows_table("SysUserFile_UD")
    assert not safe.knows_table("UserFile_UD")
    assert safe.knows_table("JobHead_UD")


# --------------------------------------------------------------------------- #
# Query-side scope gate — inherits through allows(), zero scope_gate changes
# --------------------------------------------------------------------------- #
def _ds(*qualified: str) -> dict:
    return {
        "QueryTable": [
            {
                "TableID": f"T{i}",
                "TableType": "DB",
                "DBSchemaName": q.split(".", 1)[0],
                "DBTableName": q.split(".", 1)[1],
            }
            for i, q in enumerate(qualified)
        ]
    }


def test_scope_gate_passes_the_prescribed_ud_join_for_a_scoped_parent():
    """The O1 loop, closed: the join `epicor_fields` prescribes now clears the
    gate for any caller whose scope covers the parent."""
    scope = AuthzScope.scoped("u@x", {"JobHead"}, "menu chain")
    assert (
        scope_gate.check_table_scope(scope, _ds("Erp.JobHead", "Erp.JobHead_UD"))
        is None
    )


def test_scope_gate_still_refuses_an_out_of_scope_parents_mirror():
    scope = AuthzScope.scoped("u@x", {"JobHead"}, "menu chain")
    env = scope_gate.check_table_scope(
        scope, _ds("Erp.Customer", "Erp.Customer_UD")
    )
    assert env is not None and env["error"] == "table_not_authorized"
    assert "Erp.Customer" in env["detail"]["unauthorized_tables"]
    assert "Erp.Customer_UD" in env["detail"]["unauthorized_tables"]


# --------------------------------------------------------------------------- #
# Discovery tools — gate mode, REAL deny-list callables, real AuthzScope
# --------------------------------------------------------------------------- #
_TABLES = {
    "JobHead": "Erp.JobHead",
    "JobHead_UD": "Erp.JobHead_UD",
    "POHeader": "Erp.POHeader",
    "POHeader_UD": "Erp.POHeader_UD",
    "PREmpMas": "Erp.PREmpMas",
    "PREmpMas_UD": "Erp.PREmpMas_UD",
    "SysUserFile_UD": "Ice.SysUserFile_UD",
}
_FIELDS = {
    "JobHead": [("JobNum", "nvarchar"), ("PartNum", "nvarchar")],
    "JobHead_UD": [("Approver_c", "nvarchar"), ("ForeignSysRowID", "uniqueidentifier")],
    "POHeader": [("PONum", "int")],
    "POHeader_UD": [("Custom01_c", "nvarchar"), ("ForeignSysRowID", "uniqueidentifier")],
    "PREmpMas": [("PayRate", "decimal"), ("EmpID", "nvarchar")],
    "PREmpMas_UD": [("Note_c", "nvarchar")],
    "SysUserFile_UD": [("Secret_c", "nvarchar"), ("ForeignSysRowID", "uniqueidentifier")],
}


class _Hit:
    def __init__(self, table: str, full: str) -> None:
        self.table, self.full_name = table, full
        self.description, self.field_count, self.score = "", 2, 0.9


class _FieldHit:
    def __init__(self, table: str, field: str, sql_type: str) -> None:
        self.table, self.field, self.sql_type = table, field, sql_type
        self.label, self.description, self.required = "", "", False


class _Index:
    """Duck-typed DiscoveryIndex carrying parents AND their `_UD` mirrors.

    `search_tables` honours the real store's two contracts: `allowed=` is a
    hard bare-lowercase filter, and `_UD` mirrors never rank as subject tables
    (they stay reachable via `custom_columns` — pinned by
    test_ud_mirrors_never_rank_as_subject_tables in test_discovery.py).
    """

    manifest = {"table_count": len(_TABLES), "field_count": 14}

    def search_tables(self, vec, q, limit=5, allowed=None):
        out = []
        for name, full in _TABLES.items():
            if name.endswith("_UD"):
                continue
            if allowed is not None and name.lower() not in allowed:
                continue
            out.append(_Hit(name, full))
            if len(out) >= limit:
                break
        return out

    def search_fields(self, table, vec, q, limit=6):
        return [_FieldHit(table, n, t) for n, t in _FIELDS.get(table, [])][:limit]

    def fields_of(self, table):
        return [{"name": n} for n, _ in _FIELDS.get(table, [])]

    def resolve_table(self, name):
        return {k.lower(): k for k in _TABLES}.get(str(name).strip().lower())

    def table_info(self, canon):
        return {"full_name": _TABLES[canon], "field_count": 2, "description": ""}

    def find_column_elsewhere(self, core):
        owners = [
            t
            for t, fields in _FIELDS.items()
            if any(n.lower() == core.lower() for n, _ in fields)
        ]
        return owners, len(owners)

    def name_matches_elsewhere(self, q, here):
        return []


class _Authorizer:
    def __init__(self, scope: AuthzScope, mode: str = "gate") -> None:
        self._scope, self.mode = scope, mode
        self.calls: list[str] = []

    # resolve_identity dropped `for_email` —
    # it outranked the session in gate mode, an identity-spoofing vector.
    def resolve_identity(self, session_email: str = "") -> str:
        return session_email or "user@example.org"

    async def scope_for(self, email: str) -> AuthzScope:
        self.calls.append(email)
        return self._scope


async def _embed(text, prefix):
    return None


def _register(scope: AuthzScope, mode: str = "gate"):
    registered: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                registered[name] = fn
                return fn

            return deco

    auth = _Authorizer(scope, mode)
    register_discovery_tools(
        _MCP(),
        _Index(),
        embed_query=_embed,
        authorizer=auth,
        denied_column=is_denied_column,
        denied_table=is_denied_table,
    )
    return registered, auth


async def test_fields_serves_the_mirror_directly_for_a_user_scoped_to_the_parent():
    """`epicor_fields("JobHead_UD")` works in gate mode for a
    caller whose scope covers only the PARENT — the allows() fallback, driven
    through the registered tool."""
    tools, _ = _register(_JOB, mode="gate")
    resp = await tools["epicor_fields"](table="JobHead_UD")
    assert resp["success"] is True
    assert [b["name"] for b in resp["tables"]] == ["JobHead_UD"]
    assert "Approver_c" in repr(resp)
    assert "not_authorized" not in resp


async def test_the_custom_columns_block_survives_gate_mode_for_an_in_scope_parent():
    """The block that PRESCRIBES the join must keep appearing under the gate —
    otherwise O1 is closed on the query side and reopened on discovery."""
    tools, _ = _register(_JOB, mode="gate")
    resp = await tools["epicor_fields"](table="JobHead")
    assert resp["success"] is True
    block = resp["tables"][0]["custom_columns"]
    assert block["table"] == "Erp.JobHead_UD"
    assert "ForeignSysRowID" in block["join"]
    assert block["columns"] == ["Approver_c"]


async def test_an_out_of_scope_parents_mirror_is_refused_as_not_authorized():
    tools, _ = _register(_JOB, mode="gate")
    resp = await tools["epicor_fields"](table="POHeader_UD", query="custom")
    assert resp["error"] == "table_not_authorized"
    assert resp["terminal"] is True
    assert resp["detail"]["stage"] == "authz"
    assert "Custom01_c" not in repr(resp), "the refusal leaked the mirror's columns"


async def test_a_denied_parents_mirror_is_access_denied_before_identity():
    """Deny beats the authz inheritance, and the deny check precedes the
    snapshot fetch for both wildcard-covered and exact-pattern mirrors."""
    for mirror, leak in (("PREmpMas_UD", "Note_c"), ("SysUserFile_UD", "Secret_c")):
        scope = AuthzScope.scoped("u@x", ["JobHead", "PREmpMas"], "t")
        tools, auth = _register(scope, mode="gate")
        resp = await tools["epicor_fields"](table=mirror)
        assert resp["error"] == "table_access_denied", mirror
        assert resp["terminal"] is True
        assert leak not in repr(resp), f"the refusal leaked {leak}"
        assert auth.calls == [], "the deny refusal must not depend on identity"


async def test_the_denied_parent_envelope_never_names_its_mirror():
    """`epicor_fields("PREmpMas")` refuses at the deny stage BEFORE the
    custom_columns block is built — the envelope may not name the mirror in
    any channel."""
    tools, _ = _register(_JOB, mode="gate")
    resp = await tools["epicor_fields"](table="PREmpMas")
    assert resp["error"] == "table_access_denied"
    assert "_UD" not in repr(resp), "the denied envelope named the UD mirror"


async def test_elsewhere_never_names_a_denied_mirror_but_serves_an_in_scope_one():
    """Both `elsewhere` legs filter through `_suggestable` = deny + allows():
    the deny inheritance hides `SysUserFile_UD`; the authz inheritance serves
    `JobHead_UD` to a caller scoped only to the parent — the filter is a scope
    test, not a mirror kill switch."""
    tools, _ = _register(_JOB, mode="gate")

    resp = await tools["epicor_fields"](table="JobHead", query="Secret_c")
    assert resp["success"] is True
    assert "SysUserFile" not in repr(resp), "a denied mirror leaked via elsewhere"

    resp = await tools["epicor_fields"](table="JobHead", query="Approver_c")
    assert resp["success"] is True
    assert any(
        "JobHead_UD" in repr(entry) for entry in resp.get("elsewhere", [])
    ), "an in-scope parent's mirror must survive the elsewhere scope filter"
