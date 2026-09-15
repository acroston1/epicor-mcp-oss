"""Argument FORWARDING from ``epicor_read`` into the recognizer routes.

The gap these close: ``tests/test_recognizer_order_by.py`` exercises the
recognizer functions in isolation, so it proves each one *can* sort — but never
that ``epicor_read`` actually hands it the caller's ``order_by`` / ``fields`` /
``soft``. Verified by micro-revert: stripping every ``order_by=order_by`` and
``soft=soft`` argument from the six dispatch sites in ``read.py`` left the whole
suite green. The recognizer routes return BEFORE the main argument pipeline, so
the forwarding IS the fix for the dropped-argument defect, and it was untested.

Each test drives the REGISTERED ``epicor_read`` (not the helper) and asserts on
the kwargs the recognizer was actually called with.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from epicor_mcp.tools import read as read_mod
from tests.test_partviews import configured_plants
from tests.test_read_routing_e2e import _Client, _Idx, _RBAC, _Server


class _Spy:
    """Stands in for a recognizer; records the kwargs it was dispatched with."""

    def __init__(self, payload=None):
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {"records": []}

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.payload)


def _make(monkeypatch):
    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    srv = _Server()
    read_mod.register(srv, _Idx(fields={}), _RBAC(), _Client())
    return srv.fn


def _run(fn, **kw):
    return json.loads(asyncio.run(fn(**kw)))


# (recognizer attribute on read_mod, a target phrase that triggers it,
#  the kwarg name that carries the caller's `fields`)
ROUTES = [
    ("read_attachments", "attachments for invoice INV-100", "fields"),
    ("where_used", "what is part ABC-123 used to make", "fields"),
    ("read_timephase", "time phase for part ABC-123", "fields"),
    ("read_bom", "bill of materials for part ABC-123", "fields"),
    ("planner_jobs", "jobs for planner Avery Example", None),
    ("po_suggestions", "po change suggestions", "fields_wanted"),
]


@pytest.mark.parametrize("name,target,fields_kw", ROUTES)
def test_order_by_reaches_the_recognizer(monkeypatch, name, target, fields_kw):
    spy = _Spy()
    monkeypatch.setattr(read_mod, name, spy)
    fn = _make(monkeypatch)
    _run(fn, target=target, order_by="SomeCol desc")
    assert spy.calls, f"{name} was never dispatched for target={target!r}"
    # The decisive assertion: the clause ARRIVED. Deleting `order_by=order_by`
    # at the dispatch site must fail here.
    assert spy.calls[0].get("order_by") == "SomeCol desc"


@pytest.mark.parametrize("name,target,fields_kw", ROUTES)
def test_soft_notes_reach_the_recognizer(monkeypatch, configured_plants, name, target, fields_kw):
    """`soft` carries the pre-dispatch coercions (site_resolved, arg aliasing).

    Dropping it means a deterministic Plant='North Works'->'101' rewrite is applied
    to the query and announced nowhere — the fail-soft contract requires the
    assumptions ride along.
    """
    spy = _Spy()
    monkeypatch.setattr(read_mod, name, spy)
    fn = _make(monkeypatch)
    # A site NAME is deterministically rewritten to its plant CODE before
    # dispatch; `soft['site_resolved']` is the only record that it happened.
    _run(fn, target=target, where="Plant='North Works'")
    assert spy.calls
    soft = spy.calls[0].get("soft")
    assert isinstance(soft, dict), f"{name} got soft={soft!r}"
    # Content, not just shape: an empty dict would mean the coercion was
    # applied to `where` and announced nowhere.
    assert "site_resolved" in soft, f"{name} dropped the site coercion: {soft!r}"


@pytest.mark.parametrize("name,target,fields_kw",
                         [r for r in ROUTES if r[2]])
def test_fields_reach_the_recognizer(monkeypatch, name, target, fields_kw):
    spy = _Spy()
    monkeypatch.setattr(read_mod, name, spy)
    fn = _make(monkeypatch)
    _run(fn, target=target, fields="PartNum")
    assert spy.calls
    assert spy.calls[0].get(fields_kw) == "PartNum"
