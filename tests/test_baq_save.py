"""``save_as`` — the write gate, the ordering, and the dispatch table.

TWO PROPERTIES ARE ASSERTED OVER AND OVER, because they are what a refactor
silently breaks:

* **Nothing is deleted before everything that can refuse has refused.**
  ``BAQDesignerSvc`` has no atomic replace, so ``DeleteByID`` is the point of no
  return. Every failure test therefore asserts the *absence* of ``DeleteByID``
  and ``Update`` in the call log, not merely the shape of the response.
* **A refusal makes ZERO Epicor calls.** ``MockEpicorClient`` raises on an
  unrecognised URL, and the call log is asserted empty — a control that returns
  a refusal *after* the work has happened is not a control.
"""

from __future__ import annotations

import asyncio

import pytest

from epicor_mcp.baq_ops.gate import SaveRight
from epicor_mcp.baq_ops.save import (
    delete_saved_baq,
    sanitize_baq_name,
    save_query_as_baq,
    sql_for_save,
)
from epicor_mcp.sql.adhoc import DEFAULT_PAGE_SIZE
from epicor_mcp.wedge_server import WedgeRuntime
from tests.wedge_fixtures import (
    FakeEpicorError,
    MockEpicorClient,
    getbyid_returnobj,
    load,
    ok_execute,
)

BASE = "https://example.invalid/api/v2/odata/DEMO"

#: A synthetic read-only session with an explicit BAQ-save grant. This checks
#: the permission branch independently from general write access.
ALLOWED = SaveRight(
    allowed=True,
    reason="ok",
    user_id="adminuser@example.org",
    access_level="read_only",
    can_write_baqs=True,
    epicor_username="adminuser",
    right_source="users_json",
)
#: The OTHER limb of the save-right expression: full write access, no explicit BAQ flag.
ALLOWED_BY_ACCESS_LEVEL = SaveRight(
    allowed=True, reason="ok", user_id="rw@x", access_level="read_write",
    can_write_baqs=False, epicor_username="rw", right_source="users_json",
)
DENIED = SaveRight(
    allowed=False,
    reason="no_baq_right",
    user_id="nobody@example.org",
    access_level="read_only",
    can_write_baqs=False,
    epicor_username="nobody",
    right_source="users_json",
)


class _Settings:
    auth_mode = "azure_ad"
    dev_mode = False
    environment = "live"
    epicor_company_id = "DEMO"
    response_max_bytes = 700_000
    sql_diagnose_empty = False
    sql_validate_columns = False
    sql_ground_domains = False
    port = 8061


def runtime_for(client: MockEpicorClient, right: SaveRight | None = ALLOWED) -> WedgeRuntime:
    """A WedgeRuntime with the network and the credential loader stubbed out."""
    rt = WedgeRuntime.__new__(WedgeRuntime)
    rt.settings = _Settings()
    rt.credentials = None
    rt.api_key = "k"
    rt.base_url = BASE
    from epicor_mcp.sql.governor import CostGovernor, GovernorPolicy

    rt.governor = CostGovernor(GovernorPolicy())
    rt.domain_cache = None
    rt.client = client
    rt.can_save = (lambda: right) if right is not None else None
    return rt


def clean_client(rows: list[dict] | None = None, **kw) -> MockEpicorClient:
    _, ds = load("clean_top")
    kw.setdefault("baq_data_rows", [{"PN": "ABC-1"}])
    return MockEpicorClient(
        parse_ds=ds, execute_response=ok_execute(rows if rows is not None else [{"PN": "A"}]),
        **kw,
    )


CLEAN_SQL = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P]"


def run(rt: WedgeRuntime, **kw) -> dict:
    return asyncio.run(rt.run(**kw))


# --------------------------------------------------------------------------- #
# 1 — the no-save path is untouched
# --------------------------------------------------------------------------- #


def test_1_without_save_as_the_response_is_the_ad_hoc_one_and_nothing_is_saved():
    """The whole feature's blast radius: absent ``save_as`` this must be the
    call it was before the parameter existed, byte for byte."""
    from epicor_mcp.sql.adhoc import run_sql

    baseline_client = clean_client()
    baseline = asyncio.run(
        run_sql(CLEAN_SQL, client=baseline_client, api_key="k", base_url=BASE,
                page_size=DEFAULT_PAGE_SIZE, page_num=1, diagnose=False,
                validate_columns=False, ground_domains=False, company_id="DEMO")
    )
    client = clean_client()
    out = run(runtime_for(client), sql=CLEAN_SQL)

    assert "saved" not in out
    # elapsed_s / *_ms are wall-clock, and `stage_ms` only appears when a local
    # stage crossed 1 ms — so they are compared for PRESENCE, never for value.
    volatile = {"elapsed_s", "parse_ms", "execute_ms", "stage_ms"}
    assert set(out) - volatile == set(baseline) - volatile
    for key in set(out) - volatile:
        assert out[key] == baseline[key], key
    assert [p.rsplit("/", 1)[-1] for p in client.paths] == ["ParseFromSQL", "Execute"]


# --------------------------------------------------------------------------- #
# 2-6 — the gate
# --------------------------------------------------------------------------- #


def test_2_a_save_runs_the_whole_sequence_in_the_safe_order():
    client = clean_client(getbyid_missing=False)
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="open-parts")

    assert [p.rsplit("/", 1)[-1] for p in client.paths] == [
        "ParseFromSQL",   # the ad-hoc run
        "Execute",
        "GetByID",        # the existence probe — before ANY write
        "ParseFromSQL",   # the save's own parse
        "GetByID",        # the DESIGNER-shaped snapshot, taken before the delete
        "DeleteByID",     # only because the probe found a definition
        "Update",
        "GetByID",        # the verification run reads the definition first…
        "Data",           # …and only then executes it
    ]
    assert out["saved"]["saved"] is True
    assert out["saved"]["verified"] is True
    assert out["saved"]["baq_id"] == "AUTO-open-parts"


def test_3_the_read_write_limb_also_carries_the_right():
    """Regression coverage: test 3 the read write limb also carries the right."""
    client = clean_client()
    out = run(runtime_for(client, ALLOWED_BY_ACCESS_LEVEL), sql=CLEAN_SQL, save_as="x")
    assert out["saved"]["saved"] is True
    assert out["saved"]["author"] == "rw"


def test_4_a_denied_user_is_refused_before_anything_runs():
    client = clean_client()
    out = run(runtime_for(client, DENIED), sql=CLEAN_SQL, save_as="x")

    assert client.calls == [], "the gate must refuse BEFORE any Epicor call"
    assert out["error"] == "baq_save_not_authorized"
    assert out["terminal"] is True
    assert out["valid"]["epicor_groups"] == ["ExtBAQDesigner", "BAQ", "BAMP", "BAMS"]
    assert out["detail"]["right_source"] == "users_json"
    # It must not assert Epicor group membership as FACT: for a configured user
    # the flag comes from users.json and the groups may be perfectly fine.
    assert "or from an explicit entry in this server's user record" in out["message"]
    assert out["retry_with"] == {"sql": CLEAN_SQL, "page_size": DEFAULT_PAGE_SIZE}


def test_4b_the_refusal_names_the_RIGHT_and_not_merely_the_groups():
    """A denial the user cannot act on is a dead turn. The envelope has to name
    the right itself — ``can_write_baqs`` is the string an administrator greps
    for in ``users.json`` — not just the four Epicor groups, because for a
    CONFIGURED user the flag comes from the file and the groups may be fine."""
    client = clean_client()
    out = run(runtime_for(client, DENIED), sql=CLEAN_SQL, save_as="x")
    assert out["valid"]["right"] == "can_write_baqs"
    assert "can_write_baqs" in out["message"]
    assert out["detail"]["can_write_baqs"] is False
    assert out["detail"]["stage"] == "baq_save_gate"
    # The rows were not served either, and the recovery says how to get them.
    assert "row" not in out or out.get("row_count") is None
    assert "save_as" not in out["retry_with"]


def test_5_the_writers_are_self_gating_too():
    """Each writer gates INSIDE itself, as create_baq and the delete tool do, so
    every write path is self-gating regardless of who calls it. Defence in depth,
    both writers."""
    client = clean_client()
    saved = asyncio.run(
        save_query_as_baq(
            client=client, api_key="k", base_url=BASE, right=DENIED,
            query_id="AUTO-x", description="d", sql=CLEAN_SQL,
        )
    )
    assert saved["saved"] is False and saved["reason"] == "not_authorized"
    assert client.calls == []

    deleted = asyncio.run(delete_saved_baq(client, "k", BASE, DENIED, "AUTO-x"))
    assert deleted["success"] is False
    assert deleted["error"] == "baq_delete_not_authorized"
    assert client.calls == []


def test_5b_delete_refuses_a_non_auto_id_even_for_an_allowed_user():
    client = clean_client()
    out = asyncio.run(delete_saved_baq(client, "k", BASE, ALLOWED, "zCustomerAR01"))
    assert out["error"] == "baq_delete_refused"
    assert client.calls == []


def test_5c_the_constructor_takes_can_save_as_a_keyword_only_argument():
    """The seam ``server.py`` injects through. It is keyword-only and defaults to
    None so the bare wedge entry point (``create_mcp_server``) fails closed
    without having to know anything about it."""
    import inspect

    sig = inspect.signature(WedgeRuntime.__init__)
    param = sig.parameters["can_save"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_6_no_user_map_means_the_save_path_fails_closed():
    client = clean_client()
    out = run(runtime_for(client, None), sql=CLEAN_SQL, save_as="x")
    assert client.calls == []
    assert out["error"] == "baq_save_unavailable"
    assert out["terminal"] is True
    # It blames the DEPLOYMENT, not the caller.
    assert "without a user map" in out["message"]


# --------------------------------------------------------------------------- #
# 7-9 — names
# --------------------------------------------------------------------------- #


def test_7_the_name_is_sanitised_and_announced():
    name, changed = sanitize_baq_name("Open POs / Oakridge_v2")
    assert name == "Open-POs---Oakridge"
    assert changed == {
        "submitted": "Open POs / Oakridge_v2",
        "saved_as": "Open-POs---Oakridge",
        # Illegal characters were collapsed, so two different names CAN land on
        # this id — the case the collision guard exists for.
        "lossy": True,
    }

    assert sanitize_baq_name("AUTO-foo")[0] == "foo"
    long_name = "a" * 40
    assert sanitize_baq_name(long_name)[0] == "a" * 25
    assert sanitize_baq_name("open-parts") == ("open-parts", {})


def test_7a_convention_only_renames_are_not_lossy():
    """The collision guard must not fire on the id this server itself returns.

    `saved.baq_id` and `run_it_with.saved_baq` both hand back the FULL
    `AUTO-<name>` id, and re-sending it is the advertised replace-in-place
    path. Keying the guard on "the name changed at all" made that exact
    round trip refuse with a fabricated `name_collision` claiming the id was
    "ALREADY a different saved BAQ" — it was the caller's own.
    """
    for submitted, expected in (
        ("AUTO-open-parts", "open-parts"),   # the id we handed back
        ("open-parts_v2", "open-parts"),     # retry-bump normalisation
        ("AUTO-open-parts_v3", "open-parts"),
    ):
        name, changed = sanitize_baq_name(submitted)
        assert name == expected, submitted
        assert changed.get("lossy") is False, submitted

    # …while a genuinely lossy transform still is one.
    assert sanitize_baq_name("a" * 40)[1]["lossy"] is True
    assert sanitize_baq_name("Open POs")[1]["lossy"] is True


def test_8_a_whitespace_only_save_as_is_refused_not_silently_renamed():
    """`save_as="   "` is TRUTHY. Left alone it sanitises to the fallback and
    persists as a SHARED scratch id that every such call destroys."""
    client = clean_client()
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="   ")
    assert client.calls == []
    assert out["error"] == "save_as_blank"
    assert out["retry_with"]["save_as"]
    assert out["retry_with"]["sql"] == CLEAN_SQL


def test_9_a_sanitisation_collision_refuses_and_deletes_nothing():
    """A 40-char name truncates to 25 — two different names can collide on one
    id, and a naive writer would delete whichever BAQ already held it."""
    client = clean_client(
        getbyid_obj=getbyid_returnobj(query_id="AUTO-" + "a" * 25, description="Somebody's")
    )
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="a" * 40)
    assert out["saved"]["saved"] is False
    assert out["saved"]["reason"] == "name_collision"
    assert "Somebody's" in out["saved"]["message"]
    assert not client.called("DeleteByID")
    assert not client.called("BAQDesignerSvc/Update")


def test_9b_a_name_that_sanitises_to_itself_overwrites_in_place():
    """Iterate-in-place is the contract; the collision guard must not break it."""
    client = clean_client(getbyid_obj=getbyid_returnobj(query_id="AUTO-open-parts"))
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="open-parts")
    assert out["saved"]["saved"] is True
    assert out["saved"]["replaced_existing"] is True
    assert client.count("DeleteByID") == 1


# --------------------------------------------------------------------------- #
# 10-11 — evidence, and which text is persisted
# --------------------------------------------------------------------------- #


def test_10_replaced_existing_is_evidence_based():
    """Setting it True whenever DeleteByID does not throw is wrong: then even
    a fresh create, with nothing there, reports destroying the user's data."""
    fresh = clean_client(getbyid_missing=True)
    out = run(runtime_for(fresh), sql=CLEAN_SQL, save_as="brand-new")
    assert out["saved"]["replaced_existing"] is False
    assert not fresh.called("DeleteByID"), "nothing existed, so nothing may be deleted"

    existing = clean_client(getbyid_obj=getbyid_returnobj(query_id="AUTO-brand-new"))
    out = run(runtime_for(existing), sql=CLEAN_SQL, save_as="brand-new")
    assert out["saved"]["replaced_existing"] is True
    assert existing.count("DeleteByID") == 1


def test_11_the_persisted_text_is_the_transpiled_sql():
    """The transpiled text is the only string Epicor ever parsed and ran.
    Persisting the caller's raw string persists text Epicor has never seen."""
    raw = "select [P].[PartNum] as [PN] from Erp.Part as [P]"   # no `top`
    client = clean_client()
    out = run(runtime_for(client), sql=raw, save_as="x")

    persisted = client.last_body_for("BAQDesignerSvc/Update")["ds"]
    phrase = persisted["DynamicQueryDesigner"][0]["DisplayPhrase"]
    assert "top" in phrase.lower(), "the injected bound must be in the saved text"
    assert phrase.replace("\r\n", "\n") == out["sql_executed"].replace("\r\n", "\n")
    assert phrase != raw


def test_11b_sql_for_save_falls_back_to_the_raw_string():
    text, bound = sql_for_save({}, "select 1")
    assert "select 1" in text
    assert bound == {}


# --------------------------------------------------------------------------- #
# 12-13 — keys
# --------------------------------------------------------------------------- #


def test_12_a_blank_key_is_reported_before_any_dispatch_refusal():
    client = clean_client()
    rt = runtime_for(client)
    rt.api_key = ""
    # Deliberately ALSO an invalid combination: the key check must win, or the
    # caller goes off fixing their arguments while the credential is missing.
    out = run(rt, sql=CLEAN_SQL, saved_baq="AUTO-x")
    assert out["error"] == "server_error"
    assert "EPICOR_MCP_EPICOR_BAQ_API_KEY" in out["message"]
    assert client.calls == []


def test_13_one_key_serves_every_baq_designer_call():
    """Regression coverage: test 13 one key serves every baq designer call."""

    class KeyRecordingClient(MockEpicorClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.keys: list[tuple[str, str]] = []

        async def post(self, url, api_key, json_body=None):
            self.keys.append((url, api_key))
            return await super().post(url, api_key, json_body)

        async def get(self, url, api_key, params=None):
            self.keys.append((url, api_key))
            return await super().get(url, api_key, params)

    for right in (ALLOWED, ALLOWED_BY_ACCESS_LEVEL):
        _, ds = load("clean_top")
        client = KeyRecordingClient(
            parse_ds=ds, execute_response=ok_execute([{"PN": "A"}]),
            baq_data_rows=[{"PN": "A"}],
        )
        run(runtime_for(client, right), sql=CLEAN_SQL, save_as="x")
        designer = [k for url, k in client.keys if "BAQDesignerSvc" in url]
        assert designer and set(designer) == {"k"}, right.access_level


# --------------------------------------------------------------------------- #
# 14 — the injected row bound is disclosed twice
# --------------------------------------------------------------------------- #


def test_14_an_injected_top_is_disclosed_in_the_note_and_the_summary():
    client = clean_client()
    out = run(runtime_for(client), sql="select [P].[PartNum] as [PN] from Erp.Part as [P]",
              save_as="x", page_size=200)
    bound = out["saved"]["row_bound"]
    assert bound["source"] == "injected_by_server"
    assert bound["top"] == 200
    assert "200" in bound["note"]
    assert "SAVED BAQ ROW CAP" in out["summary"]


def test_14b_a_caller_written_top_carries_no_note_and_no_summary_clause():
    client = clean_client()
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")
    assert out["saved"]["row_bound"] == {"top": 5, "source": "caller"}
    assert "SAVED BAQ ROW CAP" not in out["summary"]


# --------------------------------------------------------------------------- #
# 15-18 — every save-side failure keeps the rows
# --------------------------------------------------------------------------- #


def _unresolved_ds(field_name: str) -> dict:
    return {
        "QueryTable": [
            {"TableID": "OR", "DBSchemaName": "Erp", "DBTableName": "OrderRel",
             "TableType": "DB"}
        ],
        "QueryField": [
            {"TableID": "OR", "FieldName": field_name, "Alias": field_name,
             "DataType": "", "Formula": ""}
        ],
    }


def test_15_an_unresolved_field_keeps_the_rows_and_deletes_nothing():
    client = clean_client(save_parse_ds=_unresolved_ds("Bogus"))
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")

    assert out["success"] is True and out["row_count"] == 1, "the rows are not discarded"
    assert out["saved"]["saved"] is False
    assert out["saved"]["reason"] == "unresolved_fields"
    assert out["terminal"] is False
    assert out["next_step"]
    assert out["summary"].startswith("SAVE FAILED:")
    assert not client.called("DeleteByID")
    assert not client.called("BAQDesignerSvc/Update")


def test_16_a_c_field_gets_the_index_free_ud_remediation():
    client = clean_client(save_parse_ds=_unresolved_ds("Note_c"))
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")
    fix = out["saved"]["fix"]
    assert "_UD" in fix
    assert "SysRowID = <T>_UD.ForeignSysRowID" in fix
    assert "LEFT OUTER JOIN" in fix


def test_17_a_failed_run_never_reaches_the_save_path():
    _, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds, execute_error=FakeEpicorError("Bad SQL statement.", 400)
    )
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")
    assert out["success"] is False
    assert out["saved"] == {
        "attempted": True,
        "saved": False,
        "reason": "statement_did_not_run",
        "message": out["saved"]["message"],
    }
    for forbidden in ("GetByID", "DeleteByID", "BAQDesignerSvc/Update"):
        assert not client.called(forbidden), forbidden
    assert client.count("ParseFromSQL") == 1, "no save-side parse"


def test_18_a_failed_update_reports_the_destroyed_definition_and_tries_a_restore():
    """Without a handler here, the exception would propagate with the previous
    definition already gone and no word to the caller about it."""

    class OneShotUpdateFailure(MockEpicorClient):
        """Fails the write of the NEW definition, accepts the restore."""

        async def post(self, url, api_key, json_body=None):
            if "BAQDesignerSvc/Update" in url and not self.called(
                "BAQDesignerSvc/Update"
            ):
                self.calls.append((url, json_body or {}))
                raise FakeEpicorError("Update rejected", 500)
            return await super().post(url, api_key, json_body)

    _, ds = load("clean_top")
    client = OneShotUpdateFailure(
        parse_ds=ds,
        execute_response=ok_execute([{"PN": "A"}]),
        getbyid_obj=getbyid_returnobj(query_id="AUTO-x"),
    )
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")

    assert out["success"] is True and out["row_count"] == 1
    saved = out["saved"]
    assert saved["saved"] is False and saved["reason"] == "update_failed"
    assert saved["previous_definition_destroyed"] is True
    assert saved["previous_definition_restored"] is True
    assert saved["previous_definition_snapshot"] is True
    assert saved["detail"]["message"] == "Update rejected"
    assert out["terminal"] is False
    # The restore must post the DESIGNER tableset. `DynamicQuerySvc/GetByID`
    # returns the RUNTIME naming, and `BAQDesignerSvc/Update` cannot take it —
    # the original restore posted that one, so the recovery for the worst
    # failure mode was a call that could never succeed.
    restore_body = [b for u, b in client.calls if "BAQDesignerSvc/Update" in u][-1]
    assert "DynamicQueryDesigner" in (restore_body.get("ds") or {})


def test_18a_a_failed_update_with_no_snapshot_says_so_rather_than_claiming_a_restore():
    """"Could not read it back beforehand" is a THIRD outcome, not a failed restore."""

    class UpdateAlwaysFails(MockEpicorClient):
        async def post(self, url, api_key, json_body=None):
            if "BAQDesignerSvc/Update" in url:
                self.calls.append((url, json_body or {}))
                raise FakeEpicorError("Update rejected", 500)
            return await super().post(url, api_key, json_body)

    _, ds = load("clean_top")
    client = UpdateAlwaysFails(
        parse_ds=ds,
        execute_response=ok_execute([{"PN": "A"}]),
        getbyid_obj=getbyid_returnobj(query_id="AUTO-x"),
        designer_getbyid_missing=True,
    )
    saved = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")["saved"]

    assert saved["reason"] == "update_failed"
    assert saved["previous_definition_destroyed"] is True
    assert saved["previous_definition_restored"] is False
    assert saved["previous_definition_snapshot"] is False
    assert "nothing to restore it from" in saved["message"]


def test_18b_a_failed_pre_delete_proceeds_to_update_and_says_so():
    """A failed pre-delete can still be followed by Update.

    Asynchronous definition propagation can make a newly created BAQ briefly
    unavailable to DeleteByID. The result must report the partial replacement
    accurately even if the subsequent Update succeeds.
    """
    _, ds = load("clean_top")
    client = MockEpicorClient(
        parse_ds=ds,
        execute_response=ok_execute([{"PN": "A"}]),
        getbyid_obj=getbyid_returnobj(query_id="AUTO-x"),
        delete_error=FakeEpicorError("Record not found", 404),
    )
    saved = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")["saved"]

    assert saved["saved"] is True, "a transient 404 must not block the overwrite"
    assert saved["baq_id"] == "AUTO-x"
    assert saved["pre_delete_failed"]["status"] == 404
    assert "could not be deleted first" in saved["note"]
    assert client.called("BAQDesignerSvc/Update"), "Update must still be attempted"


# --------------------------------------------------------------------------- #
# 19-21 — verification
# --------------------------------------------------------------------------- #


def test_19_a_failed_verification_keeps_the_baq_and_attempts_no_peel():
    """There is deliberately NO order-by peel: this server's `top N` is mandatory, so
    stripping a non-[T].[C] sort returns an ARBITRARY N rows, not the same N
    unsorted, and the saved BAQ would answer a different question forever."""
    client = clean_client(
        baq_data_error=FakeEpicorError(
            "Invalid column name 'Calculated_Qty'. Order by must be [Table].[Column]", 400
        )
    )
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="ranked")

    assert out["saved"]["saved"] is True
    assert out["saved"]["verified"] is False
    assert "SAME save_as" in out["saved"]["fix"]
    assert client.count("ParseFromSQL") == 2, "one ad-hoc parse, one save parse"
    assert client.count("BAQDesignerSvc/Update") == 1, "a peel would re-save"


def test_20_a_saved_but_broken_baq_leads_the_summary_and_is_not_deleted():
    client = clean_client(baq_data_error=FakeEpicorError("BAQ execution failed", 400))
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")

    assert out["saved"]["saved"] is True and out["saved"]["verified"] is False
    assert out["saved"]["detail"]["message"]
    assert out["terminal"] is False
    assert out["summary"].startswith("SAVED BUT DOES NOT RUN:")
    assert out["next_step"], "annotate_next_step popped it; the save block must refill it"
    assert client.count("DeleteByID") <= 1, "the BAQ must not be deleted after the save"


def test_20b_a_baq_that_saved_but_400s_on_run_keeps_its_id_and_claims_no_success():
    """The ``_do_create`` spread-order precedent, in this server's shape.

    Spreading ``**create_result`` before the literal ``"success": False`` lets
    ``create_baq``'s own ``success: True`` overwrite it, so a BAQ that saved but
    400s on EVERY run is handed to the user as a working id. This server's equivalent
    is the ``saved`` block: it must retain ``baq_id`` (the caller needs it to
    retry or to delete by hand) while carrying **no** key a reader could take
    for a working query.
    """
    client = clean_client(baq_data_error=FakeEpicorError("BAQ execution failed", 400))
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")
    saved = out["saved"]

    assert saved["baq_id"] == "AUTO-x", "the id is the only handle on a broken BAQ"
    assert saved["verified"] is False
    assert saved.get("success") is not True, "no key may re-assert success"
    # Every channel that decides whether the model stops here says NO.
    assert out["terminal"] is False
    assert out["summary"].startswith("SAVED BUT DOES NOT RUN:")
    assert out["next_step"]
    assert "AUTO-x" in saved["fix"]


def test_20c_the_save_block_does_not_disown_the_baq_it_just_wrote():
    """``saved.detail`` is produced by the verification run, which is the
    ordinary ``run_saved_baq`` path — and that path's failure message ends *"This
    server did not write this BAQ — the definition lives in Epicor"*. True when
    running somebody else's saved BAQ; FALSE here, three keys away from a
    ``cleanup_hint`` that says this query now exists because we wrote it.

    A response that both claims and disclaims authorship of the same id is the
    contradiction class this repo refuses to ship: the model has to pick one,
    and the wrong pick is "tell the user it is not ours to fix".
    """
    client = clean_client(baq_data_error=FakeEpicorError("BAQ execution failed", 400))
    saved = run(runtime_for(client), sql=CLEAN_SQL, save_as="x")["saved"]

    assert "BAQ execution failed" in saved["detail"]["message"], (
        "Epicor's own message must survive"
    )
    assert "did not write this BAQ" not in saved["detail"]["message"], (
        "the save path disowns the BAQ it just created, contradicting "
        f"cleanup_hint in the same block: {saved['detail']['message']!r}"
    )


def test_21_a_budget_exhausted_verification_is_not_doubt_about_the_baq():
    """"Not verified because the budget ran out" must not read as "the BAQ is
    broken". Driven at ``save_query_as_baq`` because the runtime's own budget
    check would refuse the ad-hoc run first — the budget can only run out
    BETWEEN the two calls, and this is that state."""
    client = clean_client()

    async def _exhausted(_baq_id: str) -> dict:
        return {"success": False, "error": "query_budget_exhausted", "message": "..."}

    saved = asyncio.run(
        save_query_as_baq(
            client=client, api_key="k", base_url=BASE, right=ALLOWED,
            query_id="AUTO-x", description="d", sql=CLEAN_SQL, verify=_exhausted,
        )
    )
    assert saved["saved"] is True
    assert saved["verified"] is None
    assert saved["verification"] == "not_attempted_budget_exhausted"
    assert "fix" not in saved, "there is nothing to fix — the budget ran out"


# --------------------------------------------------------------------------- #
# 22 — the dispatch table, zero Epicor calls each
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs, slug",
    [
        ({"sql": CLEAN_SQL, "saved_baq": "AUTO-x"}, "sql_and_saved_baq"),
        ({"save_as": "x", "saved_baq": "AUTO-x"}, "save_requires_sql"),
        ({}, "no_statement"),
        ({"sql": CLEAN_SQL, "params": {"FromDate": "2026-01-01"}}, "params_need_saved_baq"),
        ({"sql": CLEAN_SQL, "save_description": "a report"},
         "save_description_needs_save_as"),
        ({"saved_baq": "AUTO-x", "page": 2}, "saved_baq_paging_unsupported"),
    ],
)
def test_22_every_unhonourable_combination_refuses_for_free(kwargs, slug):
    client = clean_client()
    out = run(runtime_for(client), **kwargs)
    assert out["error"] == slug
    assert client.calls == [], f"{slug} must cost zero Epicor calls"
    assert out["message"]


def test_22b_no_statement_points_at_the_discovery_tool():
    """This refusal is what keeps `test_every_listed_tool_passes_the_gate`
    offline once `sql` has a default."""
    client = clean_client()
    out = run(runtime_for(client), **{})
    assert out["retry_with"] == {"tool": "epicor_tables", "query": "<subject>"}


def test_22c_params_names_the_supported_path():
    client = clean_client()
    out = run(runtime_for(client), sql=CLEAN_SQL, params={"X": 1})
    assert "WHERE" in out["message"]
    assert "`@Name` fails" in out["message"]


# --------------------------------------------------------------------------- #
# 23-24 — the hint, and the author
# --------------------------------------------------------------------------- #


def test_23_the_cleanup_hint_names_no_tool_this_server_does_not_register():
    client = clean_client()
    out = run(runtime_for(client), sql=CLEAN_SQL, save_as="one-off")
    hint = out["saved"]["cleanup_hint"]
    assert "epicor_baq" not in hint
    assert "epicor_baq_delete" not in hint
    assert "AUTO-one-off" in hint
    assert "OVERWRITES it in place" in hint
    assert "BAQ Designer" in hint


def test_24_the_author_is_the_epicor_username_not_a_service_account():
    client = clean_client()
    run(runtime_for(client), sql=CLEAN_SQL, save_as="x")
    header = client.last_body_for("BAQDesignerSvc/Update")["ds"]["DynamicQueryDesigner"][0]
    assert header["AuthorID"] == "adminuser"
    assert header["QueryID"] == "AUTO-x"
    assert header["Description"].startswith("Saved via epicor_query ")


def test_24b_a_supplied_description_is_used_verbatim():
    client = clean_client()
    run(runtime_for(client), sql=CLEAN_SQL, save_as="x", save_description="Weekly PO report")
    header = client.last_body_for("BAQDesignerSvc/Update")["ds"]["DynamicQueryDesigner"][0]
    assert header["Description"] == "Weekly PO report"
