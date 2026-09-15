"""The public surface as a CLIENT sees it — failures that live in the wiring.

Every test here pins a defect that module-level unit tests cannot see, because
each lives in the **wiring** between modules rather than inside one. The unit
tests can all pass while the surface is unusable:

1. ``epicor_tables`` raised ``NameError: name 'app' is not defined`` on EVERY
   call. ``tests/test_discovery.py`` drives ``register_discovery_tools`` with an
   injected ``embed_query``, so it never executed the closure ``server.py``
   actually passes.
2. ``epicor_query(query=...)`` was a hard ``unknown_arguments`` with an EMPTY
   ``retry_with``. Three tools, and the two named ``*_tables``/``*_fields`` take
   their search text in a parameter called ``query`` while the one named
   ``epicor_query`` does not.
3. ``denied_column``/``denied_table`` were declared by
   ``register_discovery_tools`` and never passed, so discovery described payroll
   tables that ``epicor_query`` then refuses as final.
4. Nothing in ``sql/`` or in ``epicor_query``'s description mentioned that
   ``epicor_tables`` exists, so a wrong table name had no repair path but another
   guess.
5. The FastMCP ``instructions`` block still described the legacy surface — eight
   tools the server does not register — and the host injects it whether or not those
   tools are in the tool list.
"""

from __future__ import annotations

import re

import pytest

from epicor_mcp.tools._argguard import _screen_arguments


# --------------------------------------------------------------------------- #
# 1 — the closure server.py really passes
# --------------------------------------------------------------------------- #
def _unresolvable_globals(module) -> list[tuple[str, str]]:
    """Every ``(scope, name)`` in *module* that will raise NameError if reached.

    Walks the real symbol table, so a name is reported only when Python itself
    classifies it as a global lookup — a local, a parameter, a closure variable
    and a function-scoped ``import`` are all excluded by construction, and an
    attribute (``app.state``) reduces to its base name. Then the name is checked
    against the module's own namespace and builtins.

    Text matching cannot do this job: it cannot tell a comment from code, and it
    only ever finds the ONE spelling someone thought to search for.
    """
    import ast
    import builtins
    import inspect
    import symtable

    path = inspect.getsourcefile(module)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, path)
    # `except E as name:` binds `name` for the handler body and then DELETES it,
    # so the module namespace never holds it and symtable reports it as an
    # unbound global. Uses inside the handler are perfectly legal — exclude them
    # rather than let one language quirk make this check unusable.
    caught = {
        h.name for h in ast.walk(tree)
        if isinstance(h, ast.ExceptHandler) and h.name
    }
    known = set(vars(module)) | set(dir(builtins)) | caught
    bad: list[tuple[str, str]] = []

    def walk(table, trail):
        for sym in table.get_symbols():
            if sym.is_global() and sym.get_name() not in known:
                bad.append((trail, sym.get_name()))
        for child in table.get_children():
            walk(child, f"{trail}.{child.get_name()}")

    walk(symtable.symtable(source, path, "exec"), module.__name__)
    return bad


def test_no_function_in_server_reads_a_name_that_does_not_exist():
    """The class of bug that made every ``epicor_tables`` call fail.

    ``_embed_discovery_query`` read ``app.state.embed_client``. ``app`` is a
    local of ``create_app``, not of the function containing that closure, so the
    lookup fell through to module globals and raised ``NameError: name 'app' is
    not defined`` — outside the closure's own try/except, so it surfaced to the
    caller as *"Error executing tool epicor_tables"*. Every unit test passed:
    ``tests/test_discovery.py`` injects its own ``embed_query``, so it never ran
    the closure ``server.py`` actually passes.

    **Scoped to server.py deliberately.** The same sweep over all of
    ``epicor_mcp`` reports many names, and each is a benign pattern:
    they are ``if TYPE_CHECKING:`` imports used in string annotations
    (``EpicorClient``, ``ServiceIndex``, ``RBACEnforcer``, ``np`` …) and
    module-level comprehension targets (``_svc``/``entity``/``cols`` in
    ``_inline_schema``, ``svc``/``ents`` in ``_resolve``), which ``symtable``
    classifies as globals but Python binds correctly. ``app`` was the one real
    bug among them. Widening this test would mean suppressing all of those
    legitimate patterns, which is how a guard becomes a rubber stamp — so it
    guards the module where the tools are wired together and the closures live.
    """
    import epicor_mcp.server as server

    assert _unresolvable_globals(server) == []


# --------------------------------------------------------------------------- #
# 2 — `query` is the word all three tools invite, and only one of them means it
# --------------------------------------------------------------------------- #
#: The REGISTERED signature, mirrored, including `epicor_query`'s save /
#: saved-BAQ parameters: a stale copy here silently defeats
#: every alias test below, because `_screen_arguments` treats anything not in
#: `declared` as unknown and would "helpfully" hard-reject the very parameters
#: the tool now takes. `test_tool_descriptions.py` pins the real schema.
_QUERY_DECLARED = {
    "sql": "string", "page_size": "integer", "page": "integer",
    "saved_baq": "string", "params": "object",
    "save_as": "string", "save_description": "string",
}

_REAL_SQL = (
    "SELECT TOP 50 PONum, VendorNum, OrderDate FROM POOrder "
    "WHERE OrderDate >= '2026-07-01' ORDER BY OrderDate DESC"
)


@pytest.mark.parametrize("spelling", ["query", "statement", "sql_query", "q", "select"])
def test_a_sql_string_under_any_query_spelling_reaches_sql(spelling):
    out, notes, env = _screen_arguments(
        "epicor_query", {spelling: _REAL_SQL}, _QUERY_DECLARED
    )
    assert env is None, f"{spelling}= must not be a hard reject"
    assert out["sql"] == _REAL_SQL
    assert notes["arg_aliased"] == {spelling: "sql"}


def test_the_declared_sql_wins_over_an_alias():
    """Precedence, not a coin flip — same rule as `limit` over `top`."""
    out, notes, env = _screen_arguments(
        "epicor_query",
        {"sql": "select top 1 [P].[PONum] as [N] from Erp.POHeader as [P]",
         "query": _REAL_SQL},
        _QUERY_DECLARED,
    )
    assert env is None
    assert out["sql"].startswith("select top 1")
    assert "query" in notes["arg_ignored"]


@pytest.mark.parametrize(
    "english",
    [
        "show me open purchase orders since July",
        "what parts did we scrap last month",
        "top 10 customers by revenue",
    ],
)
def test_plain_english_routes_to_epicor_tables_instead_of_the_sql_parser(english):
    """Aliasing ``query`` -> ``sql`` is right for a SELECT and WRONG for English.

    Handing English to the pipe produces a parser complaint about a keyword the
    caller never wrote. The alias is only half the fix; this is the other half.
    """
    _out, _notes, env = _screen_arguments(
        "epicor_query", {"query": english}, _QUERY_DECLARED
    )
    assert env is not None and env["error"] == "not_sql"
    assert env["retry_with"]["tool"] == "epicor_tables"
    assert "epicor_tables" in env["message"]


def test_english_is_caught_even_when_sql_was_spelled_correctly():
    """No unknown argument exists here, so an alias table structurally cannot
    see it — the check has to run before the no-unknowns early return."""
    _out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": "list the open jobs"}, _QUERY_DECLARED
    )
    assert env is not None and env["error"] == "not_sql"


@pytest.mark.parametrize(
    "sql",
    [
        "select top 5 [P].[PONum] as [N] from Erp.POHeader as [P]",
        "  SELECT 1 as [x] from Erp.Company as [C]",
        "with [c] as (select top 5 [P].[PONum] as [N] from Erp.POHeader as [P]) "
        "select [c].[N] as [N] from [c]",
        "-- a leading comment\nselect top 1 [C].[Company] as [Co] from Erp.Company as [C]",
    ],
)
def test_real_sql_is_never_called_english(sql):
    """The gate decides only *is this SQL at all*. Every judgement about whether
    the SQL is any GOOD belongs to the transpiler, the lint and the deny-list,
    each of which gives a better message than a keyword sniff could."""
    _out, _notes, env = _screen_arguments("epicor_query", {"sql": sql}, _QUERY_DECLARED)
    assert env is None, f"refused real SQL: {sql!r}"


def test_the_hard_reject_still_names_what_did_map():
    """A genuinely unknown argument must not throw away the SQL beside it."""
    _out, _notes, env = _screen_arguments(
        "epicor_query",
        {"query": _REAL_SQL, "database": "live"},
        _QUERY_DECLARED,
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert env["retry_with"]["sql"] == _REAL_SQL, (
        "retry_with is EMPTY when built from the raw arguments instead of the "
        "post-alias dict"
    )


#: The mirrored declared schemas omit `for_email`: that parameter would
#: OUTRANK the session in gate mode — an identity-spoofing vector — so it is
#: not on the registered surface.
#: `tests/test_identity_from_session.py` pins its absence on the REGISTERED
#: schemas and the `unknown_arguments` treatment of a stray spelling.
@pytest.mark.parametrize(
    "tool, declared, args, expect",
    [
        ("epicor_tables", {"query": "string", "limit": "integer"},
         {"search": "purchase order lines"}, ("query", "purchase order lines")),
        ("epicor_tables", {"query": "string", "limit": "integer"},
         {"table": "POHeader"}, ("query", "POHeader")),
        ("epicor_fields", {"table": "string", "query": "string",
                           "limit": "integer"},
         {"tables": ["POHeader", "PODetail"], "query": "price"},
         ("table", ["POHeader", "PODetail"])),
        ("epicor_fields", {"table": "string", "query": "string",
                           "limit": "integer"},
         {"table": "POHeader", "columns": "unit price"},
         ("query", "unit price")),
    ],
)
def test_the_discovery_tools_accept_the_obvious_synonyms(tool, declared, args, expect):
    key, value = expect
    out, _notes, env = _screen_arguments(tool, args, declared)
    assert env is None
    assert out[key] == value


# --------------------------------------------------------------------------- #
# 3 — discovery must not describe what epicor_query refuses
# --------------------------------------------------------------------------- #
def test_server_passes_both_deny_callables_into_discovery():
    """Both were declared parameters that server.py never supplied, so
    ``_visible`` returned True unconditionally: ``epicor_fields("PREmpMas")``
    enumerated a payroll table's schema to an unauthenticated dev-mode caller.
    Discovery must apply the same denylist as query execution."""
    import inspect
    import epicor_mcp.server as server

    from epicor_mcp.index.local_retrieval import register_local_retrieval
    src = inspect.getsource(register_local_retrieval)
    assert "denied_column = denied_column or is_denied_column" in src
    assert "denied_table = denied_table or is_denied_table" in src
    assert "denied_table=denied_table, denied_column=denied_column" in src


def test_the_table_gate_is_wired_outside_the_discovery_block():
    """The table gate rests on three source-level facts, each of which fails no
    unit test when broken; it just ships an ungated server:

    1. ``TableAuthorizer`` is constructed BEFORE ``DiscoveryIndex.load`` runs.
       Inside the discovery try-block behind ``if _discovery_index is not
       None:``, deleting ``data/discovery_index/`` (or any exception in that
       block, which swallows everything) would remove the query gate while
       every tool kept answering.
    2. The ``WedgeRuntime`` construction site passes ``table_authorizer`` — the
       gate must reach the object that actually executes SQL, and a forgotten
       kwarg there means None, which is a documented gate-off.
    3. Discovery receives the SAME authorizer (``authorizer=_table_authorizer``)
       — a second construction would split the session-pinned cache in two and
       leave admin eviction reaching only half of it.

    Behavioural pins (no-discovery-index construction, eviction call-through,
    /health, explain) live in ``tests/test_authz_wiring.py``; this is the
    source tripwire in the style of the deny-callable test above.
    """
    import inspect
    import epicor_mcp.server as server

    src = inspect.getsource(server._create_mcp_server)
    assert src.index("TableAuthorizer(") < src.index("register_local_retrieval("), (
        "TableAuthorizer moved back inside/below the discovery block — the "
        "table gate vanishes whenever the discovery index is absent"
    )
    assert '_runtime_kwargs["table_authorizer"] = _table_authorizer' in src
    assert "mcp, settings, _table_authorizer" in src


def _register(**kw):
    """Register the real tools against a stub MCP, with no embedding server."""
    from epicor_mcp.discovery.tools import register_discovery_tools

    registered: dict = {}

    class _MCP:
        def tool(self, *, name, description):
            def deco(fn):
                registered[name] = fn
                return fn
            return deco

    class _Hit:
        def __init__(self, table, full):
            self.table, self.full_name = table, full
            self.description, self.field_count, self.score = "", 3, 0.9

    class _Index:
        manifest = {"table_count": 2, "field_count": 6}

        def search_tables(self, vec, q, limit=5, allowed=None):
            return [_Hit("PREmpMas", "Erp.PREmpMas"),
                    _Hit("POHeader", "Erp.POHeader")][:limit]

        def search_fields(self, table, vec, q, limit=6):
            return []

        def fields_of(self, table):
            return [{"name": "PayRate"}, {"name": "EmpID"}]

        def resolve_table(self, name):
            n = str(name).strip().lower()
            return {"prempmas": "PREmpMas", "poheader": "POHeader"}.get(n)

        def table_info(self, canon):
            return {"full_name": f"Erp.{canon}", "field_count": 2,
                    "description": ""}

        def find_column_elsewhere(self, core):
            return [], 0

        def name_matches_elsewhere(self, q, here):
            return []

    async def _embed(text, prefix):
        return None

    register_discovery_tools(_MCP(), _Index(), embed_query=_embed, **kw)
    return registered


@pytest.mark.asyncio
async def test_a_denied_table_is_not_offered_by_epicor_tables():
    from epicor_mcp.sql.denylist import is_denied_column, is_denied_table

    tools = _register(denied_column=is_denied_column, denied_table=is_denied_table)
    resp = await tools["epicor_tables"](query="employee pay")
    names = [t["table"] for t in resp["tables"]]
    assert "Erp.PREmpMas" not in names
    assert "Erp.POHeader" in names, "the over-fetch must still fill the page"


@pytest.mark.asyncio
async def test_epicor_fields_refuses_a_denied_table_and_leaks_no_recovery():
    from epicor_mcp.sql.denylist import is_denied_column, is_denied_table

    tools = _register(denied_column=is_denied_column, denied_table=is_denied_table)
    resp = await tools["epicor_fields"](table="PREmpMas", query="pay")
    assert resp["error"] == "table_access_denied"
    assert resp["terminal"] is True, (
        "the message says the refusal is final; `terminal` must agree with it"
    )
    body = repr(resp)
    for leak in ("PayRate", "EmpID", "closest_tables", "total_columns"):
        assert leak not in body, f"the refusal leaked {leak}"


# --------------------------------------------------------------------------- #
# 4 — a wrong name must have a repair path that is not another guess
# --------------------------------------------------------------------------- #
def test_a_schema_miss_names_the_tools_that_fix_it():
    from epicor_mcp.sql.tool import _point_schema_miss_at_discovery

    refusal = {
        "success": False,
        "error": "sql_unknown_table",
        "message": "`POOrder` has no schema prefix ...",
        "valid": {"unknown_tables": ["POOrder"]},
    }
    out = _point_schema_miss_at_discovery(
        refusal, "select ... from POOrder", discovery_available=True
    )
    assert "epicor_tables" in out["how_to_fix"]
    assert "epicor_fields" in out["how_to_fix"]
    assert out["retry_with"]["tool"] == "epicor_tables"
    assert out["retry_with"]["query"] == "POOrder"


def test_no_pointer_when_the_discovery_tools_are_not_registered():
    """Naming a tool the model cannot call is a guaranteed dead turn."""
    from epicor_mcp.sql.tool import _point_schema_miss_at_discovery

    refusal = {"success": False, "error": "sql_unknown_table", "message": "x"}
    out = _point_schema_miss_at_discovery(refusal, "x", discovery_available=False)
    assert "how_to_fix" not in out


def test_a_successful_result_is_returned_untouched():
    from epicor_mcp.sql.tool import _point_schema_miss_at_discovery

    ok = {"success": True, "rows": "a\tb", "row_count": 1}
    assert _point_schema_miss_at_discovery(
        ok, "select ...", discovery_available=True
    ) is ok


def test_the_description_names_the_discovery_tools_only_when_they_exist():
    from epicor_mcp.sql.tool import TOOL_DESCRIPTION, tool_description

    with_disc = tool_description(discovery_available=True)
    without = tool_description(discovery_available=False)
    assert "epicor_tables" in with_disc and "epicor_fields" in with_disc
    assert "epicor_tables" not in without
    # `without` is TOOL_DESCRIPTION plus the unconditional save / saved-BAQ
    # paragraph, which is not conditional on anything and is appended after the
    # router's 500-char window (see the next assertion).
    assert without.startswith(TOOL_DESCRIPTION)
    
    # paragraph must not push business vocabulary out of that window.
    assert with_disc[:500] == TOOL_DESCRIPTION[:500]


# --------------------------------------------------------------------------- #
# 5 — hiding a tool is not the same as removing it
# --------------------------------------------------------------------------- #
async def _surface(public_surface: bool):
    """Build the real server and return (listed_names, call_tool, instructions)."""
    import epicor_mcp.server as server
    from epicor_mcp.config import Settings
    from mcp.types import ListToolsRequest

    captured: dict = {}
    original = server._create_mcp_server

    def capture(*a, **kw):
        captured["mcp"] = original(*a, **kw)
        return captured["mcp"]

    settings = _synthetic_settings()
    object.__setattr__(settings, "public_surface", public_surface) if hasattr(
        settings, "__dataclass_fields__") else setattr(settings, "public_surface", public_surface)
    server._create_mcp_server = capture
    try:
        server.create_app(settings)
    finally:
        server._create_mcp_server = original
    mcp = captured["mcp"]
    handler = mcp._mcp_server.request_handlers[ListToolsRequest]
    res = await handler(ListToolsRequest(method="tools/list"))
    return ({t.name for t in res.root.tools}, mcp._tool_manager.call_tool,
            mcp.instructions or "")


@pytest.mark.asyncio
async def test_the_instructions_block_describes_the_surface_it_ships_with():
    """Regression coverage: test the instructions block describes the surface it ships with."""
    listed, _call, instructions = await _surface(True)
    named = set(re.findall(r"\bepicor_[a-z_]+\b", instructions))

    assert not (named - listed), (
        f"instructions name unregistered tool(s): {sorted(named - listed)}. "
        "Do not fix this by adding a denial — name only what is registered; a "
        "model that reads 'there is no epicor_read' reaches for epicor_read."
    )
    assert not (listed - named), (
        f"registered but never named in the instructions: {sorted(listed - named)}"
    )


@pytest.mark.asyncio
async def test_a_hidden_legacy_tool_is_refused_not_merely_unlisted():
    """Regression coverage: test a hidden legacy tool is refused not merely unlisted."""
    listed, call_tool, _instr = await _surface(True)
    assert listed == {"epicor_query", "epicor_tables", "epicor_fields",
                      "epicor_help", "epicor_dashboards"}

    result = await call_tool("epicor_read", {"target": "POHeader", "limit": 2})
    body = str(result)
    assert "tool_not_available" in body, "a hidden tool must be REFUSED, not executed"
    assert "epicor_tables" in body, "the refusal must name the tools that do exist"


@pytest.mark.asyncio
async def test_every_listed_tool_passes_the_gate():
    """The gate must refuse only what is hidden. A surface tool that cannot be
    called is worse than a legacy tool that can."""
    listed, call_tool, _instr = await _surface(True)
    for name in sorted(listed):
        result = await call_tool(name, {})
        assert "tool_not_available" not in str(result), f"{name} was gated OFF"


# --------------------------------------------------------------------------- #
# 6 — the save / saved_baq / dashboards surface
# --------------------------------------------------------------------------- #
def _synthetic_settings():
    import tempfile
    from pathlib import Path
    from tests.fixtures.oss_server import server_settings
    return server_settings(Path(tempfile.mkdtemp(prefix='oss-surface-')))


async def _build(public_surface: bool = True) -> dict:
    """Build the real server and capture the objects only ``server.py`` sees.

    ``_surface`` above returns what a CLIENT sees. This returns what the WIRING
    produced: the constructed ``WedgeRuntime`` and the ``RBACEnforcer`` that was
    passed in. Both are locals of ``_create_mcp_server``, and the one thing this
    file exists to catch is a kwarg that was never passed at that call site.

    ``WedgeRuntime`` is imported INSIDE ``_create_mcp_server``, so patching the
    module attribute is what the function will resolve at call time.
    """
    import epicor_mcp.server as server
    import epicor_mcp.wedge_server as wedge
    from epicor_mcp.config import Settings

    captured: dict = {}
    original_create = server._create_mcp_server
    original_runtime = wedge.WedgeRuntime

    class _Capturing(original_runtime):  # type: ignore[misc,valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["runtime"] = self

    def capture(*a, **kw):
        captured["rbac"] = a[2] if len(a) > 2 else kw.get("rbac")
        captured["mcp"] = original_create(*a, **kw)
        return captured["mcp"]

    settings = _synthetic_settings()
    setattr(settings, "public_surface", public_surface)
    server._create_mcp_server = capture
    wedge.WedgeRuntime = _Capturing
    try:
        server.create_app(settings)
    finally:
        server._create_mcp_server = original_create
        wedge.WedgeRuntime = original_runtime
    return captured


@pytest.mark.asyncio
async def test_the_instructions_name_the_dashboard_tool():
    """Both directions of instructions<->registry are pinned above;
    this one names the tool explicitly so a rename cannot pass by deleting the
    section AND the registration together."""
    listed, _call, instructions = await _surface(True)
    assert "epicor_dashboards" in listed
    assert "epicor_dashboards" in instructions


@pytest.mark.asyncio
async def test_the_dashboard_tool_is_callable_and_argues_before_it_dials():
    """``epicor_dashboards`` must pass the surface gate, and a call
    with no arguments must be answered LOCALLY.

    ``dashboard`` is required precisely so that
    ``test_every_listed_tool_passes_the_gate``'s ``{}`` call cannot reach the
    body: a defaulted parameter would send it into list mode and make the
    deterministic suite issue a live HTTP request to Epicor. The
    ``invalid_argument_type`` envelope IS the proof the body never ran.
    """
    _listed, call_tool, _instr = await _surface(True)
    body = str(await call_tool("epicor_dashboards", {}))
    assert "tool_not_available" not in body
    assert "invalid_argument_type" in body, (
        "the no-argument call reached the tool body — that is a live HTTP call "
        "from the deterministic suite"
    )


@pytest.mark.asyncio
async def test_the_empty_query_call_is_refused_locally_too():
    """``sql`` acquired a default when ``saved_baq`` became an
    alternative to it, so ``{}`` now REACHES the runner. It must refuse there,
    with no Epicor call, or the same suite starts dialling out."""
    _listed, call_tool, _instr = await _surface(True)
    body = str(await call_tool("epicor_query", {}))
    assert "no_statement" in body
    assert "epicor_tables" in body


@pytest.mark.parametrize(
    "args, target, value",
    [
        ({"baq_id": "AUTO-open-pos"}, "saved_baq", "AUTO-open-pos"),
        ({"baq": "EXAMPLE-DASH"}, "saved_baq", "EXAMPLE-DASH"),
        ({"query_id": "zCustAR01"}, "saved_baq", "zCustAR01"),
        ({"saved_query": "zCustAR01"}, "saved_baq", "zCustAR01"),
        ({"saved_baq": "X", "parameters": {"P": 1}}, "params", {"P": 1}),
        ({"saved_baq": "X", "baq_params": {"P": 1}}, "params", {"P": 1}),
        ({"sql": _REAL_SQL, "save_as": "x", "baq_description": "d"},
         "save_description", "d"),
    ],
)
def test_the_read_side_aliases_resolve(args, target, value):
    """Every alias added for the new parameters points at the READ
    side — running an existing BAQ, or describing one being saved."""
    out, notes, env = _screen_arguments("epicor_query", args, _QUERY_DECLARED)
    assert env is None, f"{args} must not be a hard reject"
    assert out[target] == value
    assert target in notes.get("arg_aliased", {}).values()


@pytest.mark.parametrize("spelling", ["baq_name", "save_name", "save_baq_as", "name"])
def test_a_name_that_could_mean_either_is_refused_never_aliased(spelling):
    """THE one alias this file must not have.

    ``baq_name`` is the legacy *create* parameter, so a model carrying legacy habits
    reaches for it — and it reads equally as "the BAQ I want you to run".
    Aliased to ``save_as`` beside a ``sql``, ``baq_name="AUTO-open-pos"`` would
    OVERWRITE that existing definition in place, silently, on turn one. No other
    alias in ``_argguard`` can destroy data.
    """
    out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": _REAL_SQL, spelling: "AUTO-open-pos"},
        _QUERY_DECLARED,
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert "save_as" in env["message"] and "saved_baq" in env["message"]
    assert "save_as" not in out and "saved_baq" not in out
    # The SQL beside it survives into the recovery.
    assert env["retry_with"]["sql"] == _REAL_SQL


def test_a_boolean_save_flag_is_refused_and_names_the_real_parameter():
    """``save=True`` is the shape a model reaches for first, and the
    answer has to teach `save_as` or it just invents the next argument."""
    _out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": _REAL_SQL, "save": True}, _QUERY_DECLARED)
    assert env is not None and env["error"] == "unknown_arguments"
    assert "save_as" in env["message"]


def test_description_is_not_quietly_taken_as_the_baqs_description():
    """A model uses ``description`` for its own QUESTION as often as
    for the artifact, so it fails the semantic-identity half of this file's own
    alias rule. Refused, not guessed — and the refusal is what makes
    ``save_description_needs_save_as`` safe as a refusal downstream."""
    out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": _REAL_SQL, "description": "open POs for Oakridge"},
        _QUERY_DECLARED,
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert out.get("save_description") != "open POs for Oakridge"


def test_a_dashboard_argument_on_the_query_tool_names_the_dashboard_tool():
    """A ``dashboard`` argument on the query tool is refused, and the refusal
    names ``epicor_dashboards``."""
    _out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": _REAL_SQL, "dashboard": "Open Backlog"},
        _QUERY_DECLARED,
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert "epicor_dashboards" in env["message"]


def test_cursor_is_refused_with_the_paging_this_tool_actually_has():
    """The public pagination parameter is `page`. One
    spelling, and the other one has to say which."""
    _out, _notes, env = _screen_arguments(
        "epicor_query", {"sql": _REAL_SQL, "cursor": "eyJza2lwIjo1MH0="},
        _QUERY_DECLARED,
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert "page" in env["message"]


@pytest.mark.parametrize(
    "args",
    [
        {"name": "Open Backlog"},
        {"dashboard_name": "Open Backlog"},
        {"dashboard_id": "OpenBacklog"},
        {"id": "OpenBacklog"},
        {"query": "Open Backlog"},
        {"q": "Open Backlog"},
    ],
)
def test_the_dashboard_tool_accepts_the_obvious_synonyms(args):
    """``epicor_dashboards`` takes ONE parameter, and every word a
    model reaches for means exactly it."""
    out, _notes, env = _screen_arguments(
        "epicor_dashboards", args, {"dashboard": "string"})
    assert env is None
    assert out["dashboard"] in ("Open Backlog", "OpenBacklog")


def test_sql_on_the_dashboard_tool_names_the_tool_that_runs_sql():
    """``valid.arguments`` on a one-parameter tool dead-ends the
    model; naming the owner converges it in one hop."""
    _out, _notes, env = _screen_arguments(
        "epicor_dashboards", {"dashboard": "OpenBacklog", "sql": _REAL_SQL},
        {"dashboard": "string"},
    )
    assert env is not None and env["error"] == "unknown_arguments"
    assert "epicor_query" in env["message"]


def test_the_not_sql_refusal_lists_every_parameter_the_tool_really_takes():
    """The hard-coded list in ``_query_rejects`` is a second copy of
    the signature. Stale, it teaches the model that a parameter which DOES
    exist does not — the same silent-drop defect this guard was written to
    remove, one layer up."""
    from epicor_mcp.tools._argguard import _query_rejects

    env = _query_rejects({"sql": "show me open purchase orders"})
    assert env is not None and env["error"] == "not_sql"
    assert set(env["valid"]["arguments"]) == set(_QUERY_DECLARED)


@pytest.mark.asyncio
async def test_the_mirrored_signature_matches_the_registered_one():
    """``_QUERY_DECLARED`` is a copy of the tool's schema, and every
    alias test above is meaningless if it drifts: ``_screen_arguments`` treats
    anything absent from `declared` as unknown, so a stale copy would make the
    guard hard-reject the very parameters the tool takes."""
    captured = await _build(True)
    tool = captured["mcp"]._tool_manager.get_tool("epicor_query")
    assert set(tool.parameters["properties"]) == set(_QUERY_DECLARED)


@pytest.mark.asyncio
async def test_the_save_gate_is_actually_wired_to_the_running_server():
    """THE REAL-WIRE TEST.

    ``WedgeRuntime.can_save`` defaults to ``None`` and ``None`` fails CLOSED
    with ``baq_save_unavailable``. So a forgotten kwarg at the construction site
    does not crash, does not log, and does not fail a single unit test: it ships
    as a permanent, perfectly honest-looking refusal under a fully green suite.
    Nothing but this test looks at the object the server actually built.

    The synthetic configured profile is read-only with an explicit BAQ-save
    grant. An access-level-only check would incorrectly reject that grant.
    """
    from epicor_mcp.auth.session import MCPSession
    from epicor_mcp.context import clear_current_session, set_current_session

    captured = await _build(True)
    runtime = captured["runtime"]
    assert runtime.can_save is not None, (
        "WedgeRuntime was constructed without can_save — every save will "
        "return baq_save_unavailable, silently and forever"
    )

    rbac = captured["rbac"]
    first = rbac._user_map.get_first_user()
    assert first is not None, "synthetic wiring fixture must configure a user"

    token = set_current_session(MCPSession(
        session_id="wiring-test",
        user_id=first.user_id,
        department=first.department,
        access_level=first.access_level.value,
        environment=first.environment,
    ))
    try:
        right = runtime.can_save()
    finally:
        clear_current_session(token)

    assert right.allowed is True, f"the dev-mode user cannot save: {right.reason}"
    assert right.can_write_baqs is True, (
        "the right came from access_level, not from can_write_baqs — a "
        "read_only user with a BAQ-save grant would be refused by an "
        "access_level-only gate"
    )
    assert right.epicor_username, "the BAQ's AuthorID would be blank"


@pytest.mark.asyncio
async def test_no_session_means_no_save_rather_than_a_crash():
    """The closure resolves PER CALL off a request contextvar. With
    no session there is nobody to authorise, and the answer is a refusal — not
    an exception, and certainly not an allow."""
    captured = await _build(True)
    right = captured["runtime"].can_save()
    assert right.allowed is False
