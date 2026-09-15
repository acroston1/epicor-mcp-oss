"""The operator-editable table blacklist (table_blacklist.txt).

ONE registration seam, everything inherits: the file installs into
``sql/denylist.py``'s module state and is consulted by the SAME ``_denied_base``
funnel as the built-in patterns — so ``is_denied_table``, ``check_parsed_ds``
(ad-hoc SQL AND the saved-BAQ relaxed path), discovery's ``_table_denied``,
E14's ``_safe_catalogue`` exclusion and the ``_UD`` one-strip inheritance all
see a file entry with ZERO extra call sites. Everything here is mock-only —
no live Epicor, no index files.

The pinned behaviours, each with its reason:

* the parser: comments / blanks / qualified / case / dupes are all one entry;
  a malformed line is a WARNING naming the line, never an exception;
* install REPLACES the previous file set (idempotent — a restart is the only
  reload, and the suite stays hermetic) and is ADDITIVE ONLY (the built-ins
  live in their own immutable tuple, so the file can never un-deny one);
* a blacklisted table's ``<X>_UD`` mirror is denied with it, for free;
* ``check_parsed_ds`` refuses the table on the ad-hoc path AND with
  ``unattributed_denies=False`` (the saved-BAQ relaxation touches only the
  attribution backstop, never the table deny-list);
* the envelope keeps the SAME error code and leak rules but must not claim
  "payroll / security data" for a table the operator blacklisted for other
  reasons — and must never name the file path (server-internal detail);
* E14's catalogue excludes the table, including across install's cache reset;
* the discovery tools hide it / refuse it with the blacklist wording;
* startup wiring: ``server.create_app`` and ``wedge_server.create_mcp_server``
  both install from ``Settings.table_blacklist_path`` before tools serve;
* BASELINE ORDERING: ``discovery/baseline.py`` deny-filters at IMPORT time, so
  a blacklisted baseline member may sit in the frozenset forever — acceptable
  because deny is re-consulted live at every decision point, which is exactly
  what this file pins.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from epicor_mcp.sql import denylist
from epicor_mcp.sql.denylist import (
    check_parsed_ds,
    denial_envelope,
    denial_source,
    install_table_blacklist,
    install_table_blacklist_from_file,
    is_denied_table,
    parse_table_blacklist,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _hermetic_blacklist():
    """Every test leaves the file layer EMPTY — install is a wholesale replace,
    so this one call is a complete reset and no state leaks across tests (or
    into the rest of the suite)."""
    yield
    install_table_blacklist((), source="test-teardown")


def _ds(*tables: str, schema: str = "Erp") -> dict:
    """A minimal Epicor parse output naming *tables* — the shape
    ``check_parsed_ds`` enforces on (TableType='DB' rows only)."""
    return {
        "QueryTable": [
            {
                "TableID": f"T{i}",
                "TableType": "DB",
                "DBSchemaName": schema,
                "DBTableName": t,
            }
            for i, t in enumerate(tables, start=1)
        ],
        "QueryField": [{"TableID": "T1", "DBFieldName": "Amount"}],
    }


# --------------------------------------------------------------------------- #
# The parser — pure, testable
# --------------------------------------------------------------------------- #
def test_parser_comments_blanks_case_qualification_and_dupes_are_one_entry():
    text = (
        "# full-line comment\n"
        "Erp.PayTranHed   # trailing comment\n"
        "\n"
        "paytranhed\n"
        "PAYTRANHED\n"
        "Ice.PayTranHed\n"
        "QuoteTran*\n"
    )
    names, warnings = parse_table_blacklist(text)
    assert names == ("PayTranHed", "QuoteTran*"), (
        "bare / qualified / any-case spellings are the SAME entry, first "
        "spelling wins, and the wildcard survives"
    )
    assert warnings == []


def test_parser_skips_malformed_lines_with_a_warning_naming_the_line():
    names, warnings = parse_table_blacklist(
        "Erp.PayTranHed\ndrop table x\n*\nErp.\nGood_One\n"
    )
    assert names == ("PayTranHed", "Good_One"), "the good lines still install"
    assert len(warnings) == 3
    for lineno in (2, 3, 4):
        assert any(f"line {lineno}" in w for w in warnings), warnings
    # A lone '*' would deny EVERY table — it must be a refused line, not a rule.
    assert any("line 3" in w for w in warnings)


def test_parser_on_the_shipped_file_yields_no_active_entries_and_no_warnings():
    """The repo ships documentation + ONE commented-out example. An active
    entry here would silently deny a table on every dev box and in every app
    build the suite performs."""
    text = (_REPO_ROOT / "table_blacklist.example.txt").read_text(encoding="utf-8")
    names, warnings = parse_table_blacklist(text)
    assert names == ()
    assert warnings == []


# --------------------------------------------------------------------------- #
# Install: replace-idempotent, additive-only
# --------------------------------------------------------------------------- #
def test_reinstall_replaces_the_previous_file_set():
    install_table_blacklist(["PayTranHed"], source="a")
    assert is_denied_table("PayTranHed")
    install_table_blacklist(["QuoteTran"], source="b")
    assert not is_denied_table("PayTranHed"), (
        "a re-install REPLACES the file set — that is what makes a restart "
        "the only reload and this suite hermetic"
    )
    assert is_denied_table("QuoteTran")
    install_table_blacklist((), source="c")
    assert not is_denied_table("QuoteTran")


def test_the_file_can_never_undeny_a_builtin_denial():
    """Additive only: the built-ins live in their own immutable tuple. An empty
    install, and a redundant entry naming a built-in denial, both change
    nothing about it — and when both layers deny, the built-in CLAIM wins,
    because the payroll/security statement is then true."""
    install_table_blacklist((), source="empty")
    assert is_denied_table("Erp.PREmpMas")
    assert is_denied_table("Ice.UserFile")
    install_table_blacklist(["PREmpMas"], source="redundant")
    assert is_denied_table("Erp.PREmpMas")
    assert denial_source("Erp.PREmpMas") == "builtin"


def test_every_spelling_of_a_file_entry_is_denied():
    """Bare, qualified, any case — and schema-INSENSITIVE on purpose: the
    built-in list carries both `Erp.` and `Ice.` spellings for the same reason
    (a wrong deny entry costs nothing, a missing one leaks)."""
    install_table_blacklist(["Erp.PayTranHed"], source="test")
    for spelling in (
        "PayTranHed",
        "Erp.PayTranHed",
        "erp.paytranhed",
        "PAYTRANHED",
        "Ice.PayTranHed",
    ):
        assert is_denied_table(spelling), spelling
    assert not is_denied_table("Erp.PayTran"), "no substring creep"


def test_the_trailing_wildcard_rides_the_existing_prefix_machinery():
    install_table_blacklist(["PayTran*"], source="test")
    assert is_denied_table("Erp.PayTranHed")
    assert is_denied_table("PayTranXyz")
    assert not is_denied_table("Erp.PayT")


def test_a_blacklisted_tables_ud_mirror_is_denied_with_it():
    """The `_UD` one-strip inheritance re-runs `_denied_base`, which is the
    single funnel the file registers into — so the mirror comes along with
    zero extra code, exactly as it does for the built-ins."""
    install_table_blacklist(["PayTranHed"], source="test")
    for spelling in ("Erp.PayTranHed_UD", "PayTranHed_UD", "paytranhed_ud"):
        assert is_denied_table(spelling), spelling
    assert denial_source("Erp.PayTranHed_UD") == "blacklist"
    assert not is_denied_table("Erp.QuoteHed_UD")


def test_denial_source_attributes_the_layer_that_denied():
    install_table_blacklist(["PayTranHed"], source="test")
    assert denial_source("Erp.PayTranHed") == "blacklist"
    assert denial_source("Erp.PREmpMas") == "builtin"
    assert denial_source("Ice.SysUserFile_UD") == "builtin", (
        "a built-in denial's mirror keeps the built-in claim"
    )


# --------------------------------------------------------------------------- #
# check_parsed_ds — ad-hoc AND the saved-BAQ relaxed path
# --------------------------------------------------------------------------- #
def test_check_parsed_ds_refuses_a_blacklisted_table_on_the_adhoc_path():
    assert not check_parsed_ds(_ds("PayTranHed")), "clean before install"
    install_table_blacklist(["PayTranHed"], source="test")
    denial = check_parsed_ds(_ds("PayTranHed"))
    assert denial
    assert denial.denied_tables == ["Erp.PayTranHed"]


def test_check_parsed_ds_refuses_it_on_the_saved_baq_relaxed_path_too():
    """`unattributed_denies=False` relaxes ONLY the attribution backstop for
    hand-authored BAQ vocabulary — the table deny-list (built-in AND file)
    runs unchanged, so a saved BAQ over a blacklisted table is refused exactly
    as ad-hoc SQL is."""
    install_table_blacklist(["PayTranHed"], source="test")
    denial = check_parsed_ds(_ds("PayTranHed"), unattributed_denies=False)
    assert denial
    assert denial.denied_tables == ["Erp.PayTranHed"]


# --------------------------------------------------------------------------- #
# The envelope: same code, same leak rules, DIFFERENT claim
# --------------------------------------------------------------------------- #
def test_a_file_sourced_denial_is_not_described_as_payroll_data():
    install_table_blacklist(["PayTranHed"], source="test")
    env = denial_envelope(check_parsed_ds(_ds("PayTranHed")))
    assert env["error"] == "table_access_denied", "SAME error code as a built-in"
    assert env["terminal"] is True
    assert "table blacklist" in env["message"]
    assert "payroll / security data" not in env["message"], (
        "the built-in claim would be a false statement about a table the "
        "operator blacklisted for other reasons — and models repeat what an "
        "envelope asserts"
    )
    assert "table_blacklist.txt" not in repr(env), (
        "the file path is a server-internal detail; 'the server's table "
        "blacklist' is source enough"
    )


def test_a_mixed_denial_attributes_each_table_to_its_own_layer():
    install_table_blacklist(["PayTranHed"], source="test")
    env = denial_envelope(check_parsed_ds(_ds("PREmpMas", "PayTranHed")))
    msg = env["message"]
    assert "Erp.PREmpMas" in msg and "hold payroll / security data" in msg
    assert "Erp.PayTranHed" in msg and "table blacklist" in msg
    # the payroll sentence must not name the blacklisted table (and vice
    # versa): the parts are '; '-joined, payroll clause first.
    payroll_clause = msg.split("hold payroll / security data")[0]
    assert "Erp.PayTranHed" not in payroll_clause


# --------------------------------------------------------------------------- #
# E14 — the catalogue exclusion, across install's cache reset
# --------------------------------------------------------------------------- #
def test_e14_catalogue_excludes_a_blacklisted_table_even_after_a_warm_cache(
    monkeypatch,
):
    """Regression coverage: test e14 catalogue excludes a blacklisted table even after a warm cache."""
    from epicor_mcp.sql import adhoc
    from epicor_mcp.sql import validate_columns as colvalid

    cat = colvalid.ColumnCatalogue({"PayTranHed": ["Amount"], "JobHead": ["JobNum"]})
    monkeypatch.setattr(colvalid, "load_catalogue", lambda paths=None: cat)
    monkeypatch.setattr(adhoc, "_SAFE_CATALOGUE", None)
    # Warm the deny-filtered cache BEFORE the install:
    assert adhoc._safe_catalogue().knows_table("PayTranHed")
    install_table_blacklist(["PayTranHed"], source="test")
    safe = adhoc._safe_catalogue()
    assert not safe.knows_table("PayTranHed"), (
        "E14 must ABSTAIN on the blacklisted table (table_not_in_catalogue), "
        "so a column-existence miss can never serve its schema"
    )
    assert safe.knows_table("JobHead"), "nothing else is dropped"


def test_install_clears_the_ud_mirror_cache(monkeypatch):
    from epicor_mcp.sql import validate_columns as colvalid

    calls: list[int] = []

    class _Stub:
        def cache_clear(self):
            calls.append(1)

    monkeypatch.setattr(colvalid, "_load_ud_cached", _Stub())
    install_table_blacklist(["PayTranHed"], source="test")
    assert calls, (
        "the deny-filtered UD-mirror map is lru-cached on (path, mtime); "
        "without the clear, a pre-install load keeps naming a blacklisted "
        "mirror for the life of the process"
    )


# --------------------------------------------------------------------------- #
# Discovery: hidden from epicor_tables, refused by epicor_fields
# (registered tools + fake index — test_discovery_gate.py's pattern)
# --------------------------------------------------------------------------- #
_TABLES = {
    "JobHead": "Erp.JobHead",
    "PartTran": "Erp.PartTran",
    "PREmpMas": "Erp.PREmpMas",
}
_FIELDS = {
    "JobHead": [{"name": "JobNum"}, {"name": "PartNum"}],
    "PartTran": [{"name": "TranQty"}, {"name": "TranDate"}],
    "PREmpMas": [{"name": "PayRate"}, {"name": "EmpID"}],
}


class _Hit:
    def __init__(self, table: str, full: str) -> None:
        self.table, self.full_name = table, full
        self.description, self.field_count, self.score = "", 2, 0.9


class _Index:
    manifest = {"table_count": len(_TABLES), "field_count": 6}

    def search_tables(self, vec, q, limit=5, allowed=None):
        out = []
        for name, full in _TABLES.items():
            if allowed is not None and name.lower() not in allowed:
                continue
            out.append(_Hit(name, full))
            if len(out) >= limit:
                break
        return out

    def search_fields(self, table, vec, q, limit=6):
        return []

    def fields_of(self, table):
        return list(_FIELDS.get(table, []))

    def resolve_table(self, name):
        return {k.lower(): k for k in _TABLES}.get(str(name).strip().lower())

    def table_info(self, canon):
        return {"full_name": _TABLES[canon], "field_count": 2, "description": ""}

    def find_column_elsewhere(self, core):
        return [], 0

    def name_matches_elsewhere(self, q, here):
        return []


async def _embed(text, prefix):
    return None


def _register_discovery(index=None):
    from epicor_mcp.discovery.tools import register_discovery_tools
    from epicor_mcp.sql.denylist import is_denied_column

    registered: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                registered[name] = fn
                return fn

            return deco

    register_discovery_tools(
        _MCP(),
        index if index is not None else _Index(),
        embed_query=_embed,
        authorizer=None,
        denied_column=is_denied_column,
        denied_table=is_denied_table,
        denial_source=denial_source,
    )
    return registered


async def test_epicor_tables_hides_a_blacklisted_table():
    install_table_blacklist(["PartTran"], source="test")
    tools = _register_discovery()
    resp = await tools["epicor_tables"](query="inventory transactions")
    names = [t["table"] for t in resp["tables"]]
    assert "Erp.PartTran" not in names
    assert "Erp.JobHead" in names, "the over-fetch must still fill the page"


async def test_epicor_fields_refuses_a_blacklisted_table_with_the_blacklist_claim():
    install_table_blacklist(["PartTran"], source="test")
    tools = _register_discovery()
    resp = await tools["epicor_fields"](table="PartTran", query="quantity")
    assert resp["error"] == "table_access_denied", "SAME code as a built-in denial"
    assert resp["terminal"] is True
    assert "table blacklist" in resp["message"]
    assert "payroll" not in resp["message"]
    body = repr(resp)
    for leak in ("TranQty", "TranDate", "closest_tables", "total_columns"):
        assert leak not in body, f"the refusal leaked {leak} — same leak rules apply"


async def test_epicor_fields_keeps_the_payroll_claim_for_a_builtin_denial():
    """The source split must not weaken the true claim: a payroll table keeps
    its stronger wording even with the source lookup wired."""
    tools = _register_discovery()
    resp = await tools["epicor_fields"](table="PREmpMas", query="pay")
    assert resp["error"] == "table_access_denied"
    assert "payroll" in resp["message"]
    assert "table blacklist" not in resp["message"]


async def test_custom_columns_never_names_a_blacklisted_ud_mirror():
    """Under the built-ins a denied mirror always implied a
    denied PARENT (inheritance runs parent→mirror), so `epicor_fields`' parent
    block could never reach one. The file blacklist creates the first
    mirror-denied-parent-clean case, and without the mirror's own deny check
    the parent's `custom_columns` block named the mirror AND served its `_c`
    column list — the exact schema leak the refusal envelope withholds."""
    mtables = {"QuoteHed": "Erp.QuoteHed", "QuoteHed_UD": "Erp.QuoteHed_UD"}
    mfields = {
        "QuoteHed": [{"name": "QuoteNum"}],
        "QuoteHed_UD": [{"name": "Secret_c"}, {"name": "Hidden_c"}],
    }

    class _MirrorIndex(_Index):
        manifest = {"table_count": 2, "field_count": 3}

        def fields_of(self, table):
            return list(mfields.get(table, []))

        def resolve_table(self, name):
            return {k.lower(): k for k in mtables}.get(str(name).strip().lower())

        def table_info(self, canon):
            return {"full_name": mtables[canon], "field_count": 2, "description": ""}

    install_table_blacklist(["QuoteHed_UD"], source="test")
    tools = _register_discovery(_MirrorIndex())
    resp = await tools["epicor_fields"](table="QuoteHed", query="")
    assert resp.get("success") is True, "the CLEAN parent must still serve"
    body = repr(resp)
    for leak in ("QuoteHed_UD", "Secret_c", "Hidden_c"):
        assert leak not in body, f"the parent's block leaked {leak}"
    # And with nothing blacklisted, the block comes back — the check hides
    # a DENIED mirror, never the feature.
    install_table_blacklist((), source="test")
    resp = await tools["epicor_fields"](table="QuoteHed", query="")
    assert "QuoteHed_UD" in repr(resp)


# --------------------------------------------------------------------------- #
# Baseline interplay — deny beats scope at DECISION time
# --------------------------------------------------------------------------- #
async def test_a_blacklisted_baseline_member_is_still_hidden_and_refused():
    """`discovery/baseline.py` deny-filters at IMPORT time, which may precede
    the install — so the frozenset legitimately still carries the table. That
    is safe ONLY because every decision point re-consults `is_denied_table`
    live; this test is the pin on that reasoning."""
    from epicor_mcp.discovery.baseline import BASELINE_TABLES

    assert "jobhead" in BASELINE_TABLES, "precondition: a real baseline member"
    install_table_blacklist(["JobHead"], source="test")
    # The import-time snapshot is stale by design...
    assert "jobhead" in BASELINE_TABLES
    # ...and every decision point refuses anyway:
    assert is_denied_table("Erp.JobHead")
    denial = check_parsed_ds(_ds("JobHead"))
    assert denial.denied_tables == ["Erp.JobHead"]
    tools = _register_discovery()
    resp = await tools["epicor_fields"](table="JobHead")
    assert resp["error"] == "table_access_denied"
    listing = await tools["epicor_tables"](query="jobs")
    assert "Erp.JobHead" not in [t["table"] for t in listing.get("tables", [])]


# --------------------------------------------------------------------------- #
# The file loader: missing file, malformed lines, typo warnings
# --------------------------------------------------------------------------- #
def test_missing_file_installs_empty_at_info_level(tmp_path, caplog):
    install_table_blacklist(["Leftover"], source="previous")
    with caplog.at_level(logging.INFO, logger="epicor_mcp.sql.denylist"):
        n = install_table_blacklist_from_file(tmp_path / "absent.txt")
    assert n == 0
    assert not is_denied_table("Leftover"), (
        "a missing file installs EMPTY — a deleted file plus a restart really "
        "clears the blacklist"
    )
    hits = [r for r in caplog.records if "table blacklist" in r.getMessage()]
    assert hits and all(r.levelno == logging.INFO for r in hits), (
        "missing is the NORMAL state (the file ships with no entries) — it "
        "must never log as an error"
    )


def test_malformed_lines_are_skipped_loudly_and_the_rest_installs(tmp_path, caplog):
    p = tmp_path / "bl.txt"
    p.write_text("Good_One\nnot a table!!\nAlso_Good\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="epicor_mcp.sql.denylist"):
        n = install_table_blacklist_from_file(p)
    assert n == 2
    assert is_denied_table("Good_One") and is_denied_table("Also_Good")
    assert any("line 2" in r.getMessage() for r in caplog.records)


def test_an_entry_matching_no_catalogue_table_still_denies_but_warns_the_line(
    tmp_path, caplog
):
    p = tmp_path / "bl.txt"
    p.write_text("JobHead\nNoSuchTbl\nJob*\nZz*\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="epicor_mcp.sql.denylist"):
        n = install_table_blacklist_from_file(
            p, known_tables={"Erp.JobHead", "POHeader"}
        )
    assert n == 4
    assert is_denied_table("NoSuchTbl"), "fail-safe: a typo still DENIES"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("line 2" in m and "NoSuchTbl" in m for m in msgs), (
        "the typo must be loud and name its line — a typo'd entry protects "
        "nothing"
    )
    assert any("line 4" in m for m in msgs), "a wildcard matching nothing warns too"
    assert not any("line 1" in m or "line 3" in m for m in msgs), (
        "real names (exact or prefix) must not false-alarm"
    )


def test_a_bom_and_crlf_file_still_installs_its_first_entry(tmp_path):
    """Windows editors save 'UTF-8 with BOM' by default, and
    under plain utf-8 the BOM rode into line 1's entry, which was skipped as
    malformed — an entry that fails to INSTALL is the fail-UNSAFE direction
    for a blacklist, warning or no warning. utf-8-sig strips it."""
    p = tmp_path / "bl.txt"
    p.write_bytes(b"\xef\xbb\xbfErp.PayTranHed\r\nQuoteTran*\r\n")
    n = install_table_blacklist_from_file(p, known_tables=())
    assert n == 2
    assert is_denied_table("PayTranHed"), "the BOM'd first entry must install"
    assert is_denied_table("QuoteTranHist"), "CRLF endings parse per-line"


def test_one_bad_byte_skips_its_own_line_not_the_whole_file(tmp_path, caplog):
    """A strict read_text raises UnicodeDecodeError, and the blanket
    handler would install EMPTY — ONE stray byte voiding every GOOD entry under a
    single WARNING. errors='replace' confines the damage to its line, which
    then fails the entry regex and warns per-line ('skip bad lines')."""
    p = tmp_path / "bl.txt"
    p.write_bytes(b"Good_One\n\xff\xfe garbage\nAlso_Good\n")
    with caplog.at_level(logging.WARNING, logger="epicor_mcp.sql.denylist"):
        n = install_table_blacklist_from_file(p, known_tables=())
    assert n == 2
    assert is_denied_table("Good_One") and is_denied_table("Also_Good"), (
        "the good lines around the bad byte must still install"
    )
    assert any("line 2" in r.getMessage() for r in caplog.records), (
        "the damaged line must warn, naming itself"
    )


def test_no_catalogue_means_no_typo_check_not_a_flood_of_warnings(tmp_path, caplog):
    p = tmp_path / "bl.txt"
    p.write_text("PayTranHed\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="epicor_mcp.sql.denylist"):
        install_table_blacklist_from_file(p, known_tables=())
    assert is_denied_table("PayTranHed")
    assert not any(
        "matches no table" in r.getMessage() for r in caplog.records
    ), "an empty corpus is 'no basis to warn', never 'everything is a typo'"


# --------------------------------------------------------------------------- #
# Startup wiring — config default, server.py, wedge_server.py
# --------------------------------------------------------------------------- #
def test_config_default_is_the_cwd_relative_file():
    """On the FIELD, not on Settings(): Settings() reads .env, which could pin
    a deployed override and mask a quietly-changed default."""
    from epicor_mcp.config import Settings

    assert str(Settings.model_fields["table_blacklist_path"].default) == (
        "table_blacklist.txt"
    )


def test_wedge_create_mcp_server_installs_the_blacklist(tmp_path):
    """The wedge entry points never pass through server.py's
    ``_create_mcp_server``, so ``wedge_server.create_mcp_server`` must install
    on its own — both wedge transports funnel through it."""
    import epicor_mcp.wedge_server as wedge
    from epicor_mcp.config import Settings

    bl = tmp_path / "bl.txt"
    bl.write_text("Erp.PayTranHed\n", encoding="utf-8")
    stub = wedge.WedgeRuntime.__new__(wedge.WedgeRuntime)
    from tests.fixtures.oss_server import server_settings
    stub.settings = server_settings(tmp_path,
        dev_mode=False, environment="pilot", table_blacklist_path=bl
    )
    wedge.create_mcp_server(stub)
    assert is_denied_table("Erp.PayTranHed")
    assert is_denied_table("PayTranHed")


# The full-app build needs the symlinked data tree (same guard as
# test_authz_wiring.py, whose hermetic pattern this copies).
_SVC_INDEX = _REPO_ROOT / "data" / "service_index.db"


def test_server_startup_installs_from_the_env_pointed_file(tmp_path, monkeypatch):
    """The env var reaches Settings, and ``create_app`` installs BEFORE tools
    serve (top of ``_create_mcp_server``, shared by the HTTP and stdio paths).
    Hermetic: fake authz client, tmp menu db, no discovery index."""
    import epicor_mcp.server as server
    from epicor_mcp.config import Settings
    from epicor_mcp.discovery import DiscoveryIndex

    import fixtures.authz as fa
    from fixtures.authz.fakes import ident, mrow, srow

    class _FakeAuthzClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def fetch_user(self, email):
            return ident(email.split("@")[0], groups=("APP",), email=email)

        async def fetch_menus(self):
            return [mrow("AP0100", sec_code="APSEC", program="Erp.UI.APInvoiceEntry")]

        async def fetch_security_rows(self):
            return [srow("APSEC", entry_list="APP")]

        async def aclose(self):
            pass

    bl = tmp_path / "blacklist.txt"
    bl.write_text(
        "# startup wiring probe\nErp.PayTranHed\nQuoteTran*  # trailing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EPICOR_MCP_TABLE_BLACKLIST_PATH", str(bl))
    monkeypatch.setattr(server, "EpicorAuthzClient", _FakeAuthzClient)
    monkeypatch.setattr(DiscoveryIndex, "load", classmethod(lambda cls, root: None))
    db = fa.build_menu_security_db(tmp_path / "menu_security.db")
    from tests.fixtures.oss_server import server_settings
    settings = server_settings(tmp_path,
        dev_mode=False,
        menu_authz_mode="shadow",
        table_authz_mode="gate",
        admin_secret="test-secret",
        vector_search_enabled=False,
        forum_live_enabled=False,
        audit_log_path=tmp_path / "audit.db",
        menu_map_db_path=db,
    )
    assert str(settings.table_blacklist_path) == str(bl), (
        "EPICOR_MCP_TABLE_BLACKLIST_PATH must reach Settings"
    )
    server.create_app(settings)
    assert is_denied_table("Erp.PayTranHed")
    assert is_denied_table("PayTranHed")
    assert is_denied_table("Erp.QuoteTranHist"), "the wildcard installed too"
    assert denylist._FILE_BLACKLIST_SOURCE == str(bl)
