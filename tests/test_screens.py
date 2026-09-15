"""Unit tests for screen-grounded discovery (menu -> BOs -> entities -> fields).

Covers the recognizer (screen ask vs ordinary read), the ScreenMap reader over
a temp menu_security.db, the RBAC-filtered one-call answer (BOs + entities +
key fields + retry_with), the not-found / no-access / list envelopes, and the
screens_for_term envelope enrichment. No live Epicor; the map is a fixture db.
"""

from __future__ import annotations

import json
import sqlite3
import types

import pytest

from epicor_mcp.tools._screens import (
    ScreenMap,
    detect_screen_query,
    screen_discovery,
    screens_for_term,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE menus (menu_id TEXT PRIMARY KEY, parent_menu_id TEXT,
            menu_desc TEXT, sec_code TEXT, program TEXT, app_id TEXT,
            enabled INTEGER, hidden INTEGER, map_strategy TEXT,
            map_confidence REAL);
        CREATE TABLE menu_services (menu_id TEXT, service_id TEXT, source TEXT,
            PRIMARY KEY (menu_id, service_id));
        """
    )
    conn.executemany(
        "INSERT INTO menus (menu_id, menu_desc, program) VALUES (?,?,?)",
        [
            ("CRMG1150", "Job Tracker", "Erp.UI.JobTracker.dll"),
            ("JCGO3003", "Job Tracker", "Erp.UI.JobTracker.dll"),   # dup caption
            ("POMN1000", "Purchase Order Entry", "Erp.UI.POEntry.dll"),
        ],
    )
    conn.executemany(
        "INSERT INTO menu_services (menu_id, service_id, source) VALUES (?,?,?)",
        [
            ("CRMG1150", "Erp.BO.JobEntrySvc", "override"),
            ("CRMG1150", "Erp.BO.JobStatusSvc", "override"),
            ("JCGO3003", "Erp.BO.JobEntrySvc", "override"),
            ("JCGO3003", "Erp.BO.JobStatusSvc", "override"),
            ("POMN1000", "Erp.BO.POSvc", "metafx"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def screen_map(tmp_path):
    db = tmp_path / "menu_security.db"
    _make_db(str(db))
    return ScreenMap(db)


class _FakeRBAC:
    """Allows every service except any in *deny*."""

    def __init__(self, deny=()):
        self._deny = set(deny)

    def check_access(self, user_id, service_id):
        if service_id in self._deny:
            return (False, f"no access to {service_id}")
        return (True, "")


class _FakeIndex:
    def __init__(self, entity_sets=None, fields=None):
        self._entity_sets = entity_sets or {}
        self._fields = fields or {}

    def get_entity_sets(self, service):
        return self._entity_sets.get(service, [])

    def get_fields(self, service, entity):
        return [{"field_name": f} for f in self._fields.get((service, entity), [])]


_SESSION = types.SimpleNamespace(user_id="tester")


# --------------------------------------------------------------------------- #
# Recognizer (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,expected", [
    ("Job Tracker screen", ("lookup", "Job Tracker")),
    ("what tables are behind the Job Tracker screen", ("lookup", "Job Tracker")),
    ("tables behind Job Tracker", ("lookup", "Job Tracker")),
    ("what's behind the Part Tracker", ("lookup", "Part Tracker")),
    ("fields for the Purchase Order Entry form", ("lookup", "Purchase Order Entry")),
    ("which screens can I see", ("list", "")),
    ("what screens are available", ("list", "")),
    # Ordinary data reads must NOT be captured:
    ("open jobs", None),
    ("time phase for part 6205", None),
    ("job entry records for part X", None),   # "entry" ≠ a screen trigger
    ("PONum = 500003", None),
    ("trend production yield over 3 months", None),
])
def test_detect_screen_query(target, expected):
    assert detect_screen_query(target) == expected


# --------------------------------------------------------------------------- #
# ScreenMap reader
# --------------------------------------------------------------------------- #

def test_screens_matching_dedupes_captions(screen_map):
    hits = screen_map.screens_matching("Job Tracker")
    assert len(hits) == 1                       # two menu ids collapse to one sig
    hit = hits[0]
    assert hit["caption"] == "Job Tracker"
    assert set(hit["menu_ids"]) == {"CRMG1150", "JCGO3003"}
    assert hit["services"] == ["Erp.BO.JobEntrySvc", "Erp.BO.JobStatusSvc"]


def test_screens_matching_partial_words(screen_map):
    assert screen_map.screens_matching("purchase order")[0]["caption"] ==\
        "Purchase Order Entry"
    assert screen_map.screens_matching("nonexistent screen") == []


# --------------------------------------------------------------------------- #
# screen_discovery — the one-call answer
# --------------------------------------------------------------------------- #

def test_screen_discovery_returns_bos_entities_fields(screen_map):
    index = _FakeIndex(
        entity_sets={"Erp.BO.JobStatusSvc": ["JobStatus"]},
        fields={("Erp.BO.JobStatusSvc", "JobStatus"): ["JobNum", "Status"]},
    )
    out = json.loads(screen_discovery(
        index, _FakeRBAC(), _SESSION, screen_map,
        mode="lookup", name="Job Tracker"))

    assert "error" not in out
    svcs = {bo["service"] for bo in out["business_objects"]}
    assert "Erp.BO.JobEntrySvc" in svcs
    # JobEntrySvc entities come from the curated map with key fields attached.
    je = next(bo for bo in out["business_objects"]
              if bo["service"] == "Erp.BO.JobEntrySvc")
    ents = {e["entity_set"] for e in je["entities"]}
    assert {"JobHead", "JobOper"} <= ents
    joboper = next(e for e in je["entities"] if e["entity_set"] == "JobOper")
    # The yield columns are surfaced right on the screen answer.
    assert "ScrapQty" in joboper["key_fields"]
    assert "QtyCompleted" in joboper["key_fields"]
    assert joboper["target"] == "Erp.BO.JobEntrySvc/JobOper"
    # A concrete next call (INV-1) + a stop hint.
    assert out["retry_with"]["target"].startswith("Erp.BO.JobEntrySvc/")
    assert "do not hunt" in out["stop_hint"].lower()


def test_screen_discovery_rbac_hides_denied_services(screen_map):
    # Deny JobEntrySvc → only JobStatusSvc (index-sourced) should surface.
    index = _FakeIndex(
        entity_sets={"Erp.BO.JobStatusSvc": ["JobStatus"]},
        fields={("Erp.BO.JobStatusSvc", "JobStatus"): ["JobNum", "Status"]},
    )
    out = json.loads(screen_discovery(
        index, _FakeRBAC(deny={"Erp.BO.JobEntrySvc"}), _SESSION, screen_map,
        mode="lookup", name="Job Tracker"))
    svcs = {bo["service"] for bo in out["business_objects"]}
    assert "Erp.BO.JobEntrySvc" not in svcs
    assert "Erp.BO.JobStatusSvc" in svcs
    assert out["hidden_services"] == 1


def test_screen_discovery_no_access_to_any(screen_map):
    out = json.loads(screen_discovery(
        _FakeIndex(), _FakeRBAC(deny={"Erp.BO.JobEntrySvc", "Erp.BO.JobStatusSvc"}),
        _SESSION, screen_map, mode="lookup", name="Job Tracker"))
    assert out["error"] == "screen_no_access"


def test_screen_discovery_not_found(screen_map):
    out = json.loads(screen_discovery(
        _FakeIndex(), _FakeRBAC(), _SESSION, screen_map,
        mode="lookup", name="Nonexistent Widget"))
    assert out["error"] == "screen_not_found"


def test_screen_discovery_list_mode_asks_for_a_name(screen_map):
    out = json.loads(screen_discovery(
        _FakeIndex(), _FakeRBAC(), _SESSION, screen_map, mode="list", name=""))
    assert out["error"] == "name_a_screen"


# --------------------------------------------------------------------------- #
# Envelope enrichment
# --------------------------------------------------------------------------- #

def test_screens_for_term_enriches(screen_map):
    hits = screens_for_term(screen_map, "Job Tracker")
    assert hits and hits[0]["screen"] == "Job Tracker"
    assert "Erp.BO.JobEntrySvc" in hits[0]["services"]


def test_screens_for_term_empty_on_miss(screen_map):
    assert screens_for_term(screen_map, "totally unknown") == []
