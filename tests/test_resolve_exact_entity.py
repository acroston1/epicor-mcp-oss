"""Exact entity-set name resolution (defect: `ambiguous_target` on real tables).

`resolve_target` never looked an entity up BY NAME — it matched service
descriptions via FTS and then INVENTED an entity with `_default_entity`,
discarding the table the user actually named. So `PartWhse` came back as
"ambiguous" with `Erp.BO.PartSvc/Part` (a DIFFERENT table) as the top
`retry_with`. Following that would have produced a silent wrong answer.

Most entity names in the index have exactly ONE host. The few that are
genuinely contested MUST keep returning the INV-1
error — this file pins both halves.
"""

from __future__ import annotations

import json

import pytest

from epicor_mcp.tools._resolve import _default_entity, resolve_target


class _Idx:
    """Fake index recording whether the reverse lookup was consulted."""

    def __init__(self, hosts=None, entity_sets=None, services=None, boom=False):
        self._hosts = hosts or {}
        self._entity_sets = entity_sets or {}
        self._services = services or []
        self._boom = boom
        self.entity_lookups: list[str] = []

    def services_for_entity(self, entity_set):
        self.entity_lookups.append(entity_set)
        if self._boom:
            raise RuntimeError("index unavailable")
        return self._hosts.get(entity_set.strip().lower(), [])

    def get_entity_sets(self, service):
        return self._entity_sets.get(service, [])

    def search_services(self, raw, limit=5):
        return self._services


def _hosts(*pairs):
    return [{"service_id": s, "entity_set_name": e} for s, e in pairs]


def test_sole_host_entity_resolves():
    """The exact call-3/8/10 regression."""
    idx = _Idx(hosts={"partwhse": _hosts(("Erp.BO.PartSvc", "PartWhse"))})
    res = resolve_target(idx, "PartWhse")
    assert res["service"] == "Erp.BO.PartSvc"
    assert res["entity_set"] == "PartWhse"
    assert res["candidates"] == []


def test_canonical_owner_wins_multi_host():
    """Call-18 3 hosts, but Erp.BO.PartTranSvc is name-derivable."""
    idx = _Idx(hosts={"parttran": _hosts(
        ("Erp.BO.GLCostTransSvc", "PartTran"),
        ("Erp.BO.PartTranSvc", "PartTran"),
        ("Erp.BO.ReceiptsFromMfgSvc", "PartTran"))})
    res = resolve_target(idx, "PartTran")
    assert res["service"] == "Erp.BO.PartTranSvc"
    assert res["entity_set"] == "PartTran"
    assert res["candidates"] == []


def test_sole_non_helper_host_wins():
    idx = _Idx(hosts={"apinvdtl": _hosts(
        ("Erp.BO.APInvoiceSvc", "APInvDtl"),
        ("Erp.BO.APInvDtlSearchSvc", "APInvDtl"))})
    res = resolve_target(idx, "APInvDtl")
    assert res["service"] == "Erp.BO.APInvoiceSvc"


def test_genuine_multi_owner_still_errors():
    """INV-1 preserved: blind target promotion is NOT reintroduced."""
    idx = _Idx(hosts={"taxconnectstatus": _hosts(
        ("Erp.BO.APInvoiceSvc", "TaxConnectStatus"),
        ("Erp.BO.ARInvoiceSvc", "TaxConnectStatus"),
        ("Erp.BO.QuoteSvc", "TaxConnectStatus"),
        ("Erp.BO.SalesOrderSvc", "TaxConnectStatus"))})
    res = resolve_target(idx, "TaxConnectStatus")
    assert res["service"] == ""
    assert len(res["candidates"]) == 4


def test_ambiguous_candidates_keep_requested_entity():
    """The envelope must never redirect to a DIFFERENT table."""
    idx = _Idx(hosts={"taxconnectstatus": _hosts(
        ("Erp.BO.APInvoiceSvc", "TaxConnectStatus"),
        ("Erp.BO.ARInvoiceSvc", "TaxConnectStatus"))})
    res = resolve_target(idx, "TaxConnectStatus")
    assert all(c["entity_set"] == "TaxConnectStatus" for c in res["candidates"])
    assert all(c["target"].endswith("/TaxConnectStatus") for c in res["candidates"])
    assert res["retry_with"] == {"target": res["candidates"][0]["target"]}


@pytest.mark.parametrize("raw", ["partwhse", "PARTWHSE", "PartWhse"])
def test_case_insensitive_and_real_casing(raw):
    idx = _Idx(hosts={"partwhse": _hosts(("Erp.BO.PartSvc", "PartWhse"))})
    assert resolve_target(idx, raw)["entity_set"] == "PartWhse"


# --------------------------------------------------------------------------- #
# ORDERING GUARDS — the exact-entity step must stay BELOW the synonym table
# and the curated map. These three would silently break if it moved up.
# --------------------------------------------------------------------------- #

def test_synonym_table_still_wins_over_exact_entity():
    """LaborDtl: the canonical rule would pick LaborDtlSvc, but Erp.BO.LaborSvc
    is obsolete; collection reads use the SearchSvc."""
    idx = _Idx(hosts={"labordtl": _hosts(
        ("Erp.BO.LaborDtlSvc", "LaborDtl"),
        ("Erp.BO.LaborSvc", "LaborDtl"),
        ("Erp.BO.LaborDtlSearchSvc", "LaborDtl"))})
    res = resolve_target(idx, "LaborDtl")
    assert res["service"] == "Erp.BO.LaborDtlSearchSvc"
    assert idx.entity_lookups == []  # never even consulted


def test_sugpochg_synonym_still_wins():
    idx = _Idx(hosts={"sugpochg": _hosts(
        ("Erp.BO.POSuggChgSvc", "SugPOChg"),
        ("Erp.BO.SomeOtherSvc", "SugPOChg"))})
    assert resolve_target(idx, "SugPOChg")["service"] == "Erp.BO.POSuggChgSvc"


def test_curated_map_still_wins():
    idx = _Idx(hosts={"joboper": _hosts(
        ("Erp.BO.JobOperSvc", "JobOper"),
        ("Erp.BO.JobEntrySvc", "JobOper"),
        ("Erp.BO.JobOperSearchSvc", "JobOper"))})
    res = resolve_target(idx, "JobOper")
    assert res["service"] == "Erp.BO.JobEntrySvc"
    assert res["entity_set"] == "JobOper"


def test_blocklist_and_guards():
    idx = _Idx(hosts={"list": _hosts(("Erp.BO.AnySvc", "List"))})
    assert resolve_target(idx, "List")["service"] != "Erp.BO.AnySvc"
    # A whitespace phrase is not a table name — never look it up.
    idx2 = _Idx(hosts={})
    resolve_target(idx2, "some unknown phrase here")
    assert idx2.entity_lookups == []
    # Too short to be a table name.
    idx3 = _Idx(hosts={"ab": _hosts(("Erp.BO.AbSvc", "Ab"))})
    resolve_target(idx3, "Ab")
    assert idx3.entity_lookups == []


# --------------------------------------------------------------------------- #
# T4 — sole `Erp.BO.<Entity>SearchSvc` twin. Runs LAST, only where the function
# already returned "": strictly additive, it can only convert an existing
# `ambiguous_target` dead-end into a resolution.
# --------------------------------------------------------------------------- #

def test_sole_searchsvc_twin_resolves_apinvmsc():
    """APInvMsc: 3 hosts, no canonical Erp.BO.APInvMscSvc, 2 real BOs — T1/T2/T3
    all fail, so it dead-ended as `ambiguous_target` (2 of the 4 post-restart
    occurrences). The named entity's own search twin is the right read target."""
    idx = _Idx(hosts={"apinvmsc": _hosts(
        ("Erp.BO.APInvMscSearchSvc", "APInvMsc"),
        ("Erp.BO.APInvoiceSvc", "APInvMsc"),
        ("Erp.BO.ContainerTrackingSvc", "APInvMsc"))})
    res = resolve_target(idx, "APInvMsc")
    assert res["service"] == "Erp.BO.APInvMscSearchSvc"
    assert res["entity_set"] == "APInvMsc"       # the entity the caller NAMED
    assert res["candidates"] == []


def test_twin_wins_alongside_an_unrelated_searchsvc_host():
    """The rule is the NAMED entity's twin, not 'exactly one *SearchSvc host'.
    PartRev is hosted by BomSearchSvc too; PartRevSearchSvc is still the twin."""
    idx = _Idx(hosts={"partrev": _hosts(
        ("Erp.BO.BomSearchSvc", "PartRev"),
        ("Erp.BO.PartRevSearchSvc", "PartRev"),
        ("Erp.BO.EngWorkBenchSvc", "PartRev"),
        ("Erp.BO.QuoteSvc", "PartRev"))})
    assert resolve_target(idx, "PartRev")["service"] == "Erp.BO.PartRevSearchSvc"


def test_multiple_searchsvc_hosts_without_a_twin_still_error():
    """Two search helpers, neither derived from the named entity: picking one
    would be a similarity guess, which is exactly what stays reverted."""
    idx = _Idx(hosts={"taxconnectstatus": _hosts(
        ("Erp.BO.APInvDtlSearchSvc", "TaxConnectStatus"),
        ("Erp.BO.OrderDtlSearchSvc", "TaxConnectStatus"),
        ("Erp.BO.APInvoiceSvc", "TaxConnectStatus"),
        ("Erp.BO.SalesOrderSvc", "TaxConnectStatus"))})
    res = resolve_target(idx, "TaxConnectStatus")
    assert res["service"] == ""
    assert len(res["candidates"]) == 4
    assert all(c["entity_set"] == "TaxConnectStatus" for c in res["candidates"])


def test_two_namespaced_twins_still_error():
    """`Erp.BO.XSearchSvc` + `Ice.BO.XSearchSvc` is not EXACTLY ONE twin."""
    idx = _Idx(hosts={"uomclass": _hosts(
        ("Erp.BO.UOMClassSearchSvc", "UOMClass"),
        ("Ice.BO.UOMClassSearchSvc", "UOMClass"),
        ("Erp.BO.PartSvc", "UOMClass"),
        ("Erp.BO.QuoteSvc", "UOMClass"))})
    assert resolve_target(idx, "UOMClass")["service"] == ""


def test_t4_never_overrides_an_earlier_tier():
    """T1/T2/T3 keep their picks even when a twin is present — additive only."""
    # T2: canonical owner beats the twin.
    idx = _Idx(hosts={"parttran": _hosts(
        ("Erp.BO.PartTranSearchSvc", "PartTran"),
        ("Erp.BO.PartTranSvc", "PartTran"),
        ("Erp.BO.GLCostTransSvc", "PartTran"))})
    assert resolve_target(idx, "PartTran")["service"] == "Erp.BO.PartTranSvc"
    # T3: the sole real BO beats the twin.
    idx2 = _Idx(hosts={"apinvdtl": _hosts(
        ("Erp.BO.APInvDtlSearchSvc", "APInvDtl"),
        ("Erp.BO.APInvoiceSvc", "APInvDtl"))})
    assert resolve_target(idx2, "APInvDtl")["service"] == "Erp.BO.APInvoiceSvc"


@pytest.mark.parametrize("raw,hosts,expected", [
    # LaborDtl: the synonym table pins the SearchSvc, T4 must not be what did it.
    ("LaborDtl", (("Erp.BO.LaborDtlSvc", "LaborDtl"),
                  ("Erp.BO.LaborSvc", "LaborDtl"),
                  ("Erp.BO.LaborDtlSearchSvc", "LaborDtl")),
     "Erp.BO.LaborDtlSearchSvc"),
    # JobOper: curated map owns it — the twin must NOT steal it.
    ("JobOper", (("Erp.BO.JobOperSearchSvc", "JobOper"),
                 ("Erp.BO.PCIDJobOperSearchSvc", "JobOper"),
                 ("Erp.BO.JobEntrySvc", "JobOper"),
                 ("Erp.BO.JobOperSvc", "JobOper")),
     "Erp.BO.JobEntrySvc"),
    # SugPOChg: synonym-owned.
    ("SugPOChg", (("Erp.BO.SugPOChgSearchSvc", "SugPOChg"),
                  ("Erp.BO.POSuggChgSvc", "SugPOChg"),
                  ("Erp.BO.SomeOtherSvc", "SugPOChg")),
     "Erp.BO.POSuggChgSvc"),
])
def test_curated_owners_unchanged_when_a_twin_exists(raw, hosts, expected):
    idx = _Idx(hosts={raw.lower(): _hosts(*hosts)})
    res = resolve_target(idx, raw)
    assert res["service"] == expected
    assert idx.entity_lookups == []  # step 3.5 never even runs for these


def test_default_entity_skips_list_stub():
    """`List` is an entity set on 595 services and carries ZERO fields."""
    idx = _Idx(entity_sets={"Erp.BO.PartTranSvc": ["List", "PartTran", "PartTrans"]})
    assert _default_entity(idx, "Erp.BO.PartTranSvc") == "PartTran"


def test_fts_candidates_keep_named_entity():
    """Even on the FTS path the caller's entity survives."""
    idx = _Idx(
        hosts={},
        entity_sets={"Erp.BO.PartSvc": ["Part", "PartWhse"],
                     "Erp.BO.OtherSvc": ["Other"]},
        services=[{"service_id": "Erp.BO.PartSvc", "description": "parts"},
                  {"service_id": "Erp.BO.OtherSvc", "description": "other"}],
    )
    res = resolve_target(idx, "PartWhse")
    targets = [c["target"] for c in res["candidates"]]
    assert "Erp.BO.PartSvc/PartWhse" in targets
    assert "Erp.BO.PartSvc/Part" not in targets


def test_index_errors_are_non_fatal():
    """A broken reverse lookup falls open to the pre-existing FTS result."""
    idx = _Idx(
        boom=True,
        entity_sets={"Erp.BO.PartSvc": ["Part"]},
        services=[{"service_id": "Erp.BO.PartSvc", "description": "parts"}],
    )
    res = resolve_target(idx, "PartWhse")
    assert res["service"] == "Erp.BO.PartSvc"  # lone FTS hit


def test_envelope_shape_is_inv1():
    """A contested entity still yields the uniform INV-1 envelope."""
    from epicor_mcp.tools._resolve import error_envelope
    idx = _Idx(hosts={"taxconnectstatus": _hosts(
        ("Erp.BO.APInvoiceSvc", "TaxConnectStatus"),
        ("Erp.BO.ARInvoiceSvc", "TaxConnectStatus"))})
    res = resolve_target(idx, "TaxConnectStatus")
    env = json.loads(json.dumps(error_envelope(
        "ambiguous_target", "pick one", candidates=res["candidates"])))
    assert env["error"] == "ambiguous_target"
    assert env["message"]
    assert set(env) <= {"error", "message", "valid", "retry_with",
                        "candidates", "detail"}
