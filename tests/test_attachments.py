"""Unit tests for the attachment / PDF recognizer folded into ``epicor_read``.

Covers recognition (attachment/PDF phrasings vs unrelated reads), the ONE-call
group path (``GroupID = 'BATCH-100'`` on APInvHed returning every invoice's
``APInvHedAttch`` row via the Fix-2 ``include_tables`` opt-in), the Windows ->
local path map and its allow-list/traversal/symlink refusals, unreadable files,
the missing-PyMuPDF degradation, oversize text truncation, and the recognizer
argument contract (order_by APPLY + announced args_ignored).

No live Epicor and no real PDF: the GetRows POST is faked, and PyMuPDF is
stubbed so the suite runs on a box without it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import zlib

import pytest

from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools import _attachments as att
from epicor_mcp.tools._attachments import (
    detect_attachments,
    local_path_for,
    read_attachments,
    wants_text,
)

AP_SVC = "Erp.BO.APInvoiceSvc"
WIN_ROOT = "F:\\Accounting-Example\\AP\\"
WIN_DIR = WIN_ROOT + "invoices\\processed\\EXAMPLE-SUPPLIER\\"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Idx:
    """Only the three index methods the route touches."""

    def __init__(self, tables=None, service=AP_SVC, plurals=("APInvoices",)):
        self._service = service
        self._plurals = list(plurals)
        self._tables = tables if tables is not None else [
            "APInvHed", "APInvHedAttch", "APInvDtl", "APInvSched",
        ]

    def get_entity_sets(self, service):
        if service != self._service:
            return []
        # The plurals are plural-only OData collections: no fields, so
        # _dataset_tables must exclude them (and GetRows must not get a
        # whereClause for them).
        return list(self._tables) + self._plurals

    def get_fields(self, service, entity_set):
        if service != self._service or entity_set not in self._tables:
            return []
        return [{"field_name": "InvoiceNum", "field_type": "Edm.String"},
                {"field_name": "VendorNum", "field_type": "Edm.Int32"}]

    def get_field_types(self, service, entity_set):
        return {f["field_name"]: f["field_type"]
                for f in self.get_fields(service, entity_set)}

    def services_for_entity(self, entity_set):
        return []

    def search_services(self, raw, limit=5):
        return []

    def find_field_owners(self, name, limit=6):
        return []


class _RBAC:
    def __init__(self, allowed=True):
        self._allowed = allowed

    def check_access(self, user_id, service_id):
        return (True, "") if self._allowed else (False, f"no access to {service_id}")

    def check_service_access(self, user_id, service_id):
        return types.SimpleNamespace(api_key="K")


class _Client:
    def __init__(self, dataset=None, error=None):
        self._dataset = dataset or {}
        self._error = error
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, api_key, json_body=None):
        self.posts.append((url, dict(json_body or {})))
        if self._error:
            raise self._error
        return {"returnObj": dict(self._dataset)}


_SESSION = types.SimpleNamespace(user_id="tester")


def _run(coro):
    return asyncio.run(coro)


def _hed(inv="INV-100", vend=1234):
    return {"Company": "DEMO", "InvoiceNum": inv, "VendorNum": vend,
            "GroupID": "BATCH-100", "DocInvoiceAmt": 1200.0}


def _attch(inv="INV-100", vend=1234, fname=None, desc="Invoice"):
    return {"Company": "DEMO", "VendorNum": vend, "InvoiceNum": inv,
            "DrawingSeq": 1, "XFileRefNum": 91,
            "SysRowID": "0000-1111", "ForeignSysRowID": "0000-2222",
            "DrawDesc": desc, "DocTypeID": "AP",
            "FileName": fname if fname is not None
            else WIN_DIR + f"EXAMPLE-SUPPLIER_{inv}.pdf"}


def _dataset(n=1, **kw):
    invs = [f"900000{i}" for i in range(n)]
    return {"APInvHed": [_hed(i) for i in invs],
            "APInvHedAttch": [_attch(i, **kw) for i in invs],
            "APInvDtl": [{"InvoiceNum": i} for i in invs]}


class _FakePage:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class _FakeDoc:
    def __init__(self, pages):
        self._pages = [_FakePage(p) for p in pages]
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


def _fake_fitz(pages):
    return types.SimpleNamespace(open=lambda path: _FakeDoc(pages))


class _PagingClient:
    """Epicor-accurate GetRows fake: ``pageSize`` bounds the PARENT page.

    ``_Client`` returns the whole dataset regardless of ``pageSize``, a state
    Epicor can never produce — which is exactly what hid the parent-side
    truncation (``limit`` was the page size, so ``order_by`` ranked page 1 and
    the response still said "N of N").
    """

    def __init__(self, n, with_attachments=True):
        self._hed = [_hed(f"INV{i}") for i in range(n)]
        self._attch = {} if not with_attachments else {
            h["InvoiceNum"]: _attch(h["InvoiceNum"]) for h in self._hed}
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, api_key, json_body=None):
        body = dict(json_body or {})
        self.posts.append((url, body))
        size = int(body.get("pageSize") or 0) or len(self._hed)
        page = self._hed[:size]
        return {"returnObj": {
            "APInvHed": page,
            "APInvHedAttch": [self._attch[h["InvoiceNum"]] for h in page
                              if h["InvoiceNum"] in self._attch],
            "APInvDtl": []}}


@pytest.fixture(autouse=True)
def _no_real_fitz(monkeypatch):
    """Never touch the host's PyMuPDF (or its cache) from a unit test."""
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": None})


# --------------------------------------------------------------------------- #
# Recognition (pure)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("target,expected", [
    # Phrasings a caller uses for an A/P invoice attachment:
    ("read the invoice PDF for group BATCH-100", True),
    ("invoice PDF", True),
    ("the PDF for invoice INV-100", True),
    ("attachment for invoice INV-100", True),
    ("attachments for invoice INV-100", True),
    ("attached document", True),
    ("read the invoice attached to BATCH-100", True),
    ("what file is linked to invoice INV-100", True),
    ("scanned copy of the supplier invoice", True),
    ("APInvHedAttch", True),
    ("documents attached to job JOB-100", True),
    ("linked drawings for part PART-100", True),
    # NOT attachments — a bare file/document word must never route here:
    ("open POs", False),
    ("BOM for part PART-100", False),
    ("what is part PART-100 used to make", False),
    ("list the planners", False),
    ("document types", False),
    ("files", False),
    ("invoice INV-100", False),
    ("po change suggestions", False),
    ("", False),
])
def test_detect_attachments(target, expected):
    assert detect_attachments(target) is expected


@pytest.mark.parametrize("target", [
    # "attached" is ORDINARY English, not attachment-specific vocabulary, and
    # it co-occurs freely with BOM / where-used phrasing. Every one of these
    # was answered by read_bom before this route existed, and every one of them
    # dead-ended on need_attachment_filter once it was dispatched first.
    "what BOM is part ABC-123 attached to",
    "the routing attached to part ABC-123",
    "components of part ABC-123 and the attached drawing",
    "bill of materials for part ABC-123 and the attached pdf",
    "what is part ABC-123 used to make, and the attached drawings",
])
def test_bom_and_where_used_vocabulary_keep_the_phrase(target):
    assert detect_attachments(target) is False


@pytest.mark.parametrize("target", [
    # ... and the reverse steal must NOT happen either: an attachment-led
    # phrase carrying a where-used / BOM fragment still belongs here. This is
    # the case the FIRST-among-recognizers dispatch ordering exists for.
    "the drawing attached to the part used to make job 123",
    "the pdf attached to the bom for part ABC-123",
    "attachments for part ABC-123 used on job 123",
])
def test_attachment_led_phrase_still_wins_over_bom(target):
    assert detect_attachments(target) is True


@pytest.mark.parametrize("target,expected", [
    ("read the invoice PDF for BATCH-100", True),
    ("extract the text from the invoice attachment", True),
    ("what does the attached invoice say", True),
    ("summarize the attached invoice", True),
    ("attachments for invoice INV-100", False),
    ("invoice PDF", False),
])
def test_wants_text(target, expected):
    assert wants_text(target) is expected


# --------------------------------------------------------------------------- #
# Windows -> local path mapping + the allow-list
# --------------------------------------------------------------------------- #

MAP = {WIN_ROOT: "/mnt/ap"}


def test_path_map_maps_the_ap_share():
    local, why = local_path_for(WIN_DIR + "EXAMPLE-SUPPLIER_INV-100.pdf", MAP)
    assert why == ""
    assert local == ("/mnt/ap/invoices/processed/EXAMPLE-SUPPLIER/"
                     "EXAMPLE-SUPPLIER_INV-100.pdf")


def test_path_map_prefix_match_is_case_insensitive():
    """Drive-letter (and folder) case varies in Epicor's stored paths."""
    local, why = local_path_for(
        "f:\\accounting-example\\AP\\x\\y.pdf", MAP)
    assert why == ""
    # The REMAINDER keeps its original case — Linux paths are case-sensitive.
    assert local == "/mnt/ap/x/y.pdf"


def test_path_outside_the_allow_list_is_refused():
    local, why = local_path_for("F:\\..\\..\\etc\\passwd", MAP)
    assert local is None
    assert "no local mount is configured" in why


def test_other_drive_is_refused():
    local, why = local_path_for("C:\\Windows\\System32\\config\\SAM", MAP)
    assert local is None and "allow-listed roots" in why


def test_traversal_out_of_the_root_is_refused_after_realpath():
    local, why = local_path_for(
        WIN_ROOT + "..\\..\\..\\etc\\passwd", MAP)
    assert local is None
    assert "OUTSIDE the allow-listed root" in why


def test_symlink_escape_is_refused_after_realpath(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.pdf"
    secret.write_text("classified")
    (root / "link.pdf").symlink_to(secret)

    mapping = {WIN_ROOT: str(root)}
    local, why = local_path_for(WIN_ROOT + "link.pdf", mapping)
    assert local is None, f"symlink escaped the root: {local}"
    assert "OUTSIDE the allow-listed root" in why


def test_forward_slashes_and_blank_filename():
    local, why = local_path_for("F:/Accounting-Example/AP/x.pdf", MAP)
    assert local == "/mnt/ap/x.pdf" and why == ""
    local, why = local_path_for("", MAP)
    assert local is None and "no FileName" in why


def test_malformed_stored_path_is_a_note_not_a_crash():
    """A NUL in the database field makes realpath raise; a malformed row must
    never take the whole read down."""
    local, why = local_path_for(WIN_ROOT + "bad\x00name.pdf", MAP)
    assert local is None and "could not be resolved" in why


def test_empty_map_reads_nothing():
    local, why = local_path_for(WIN_DIR + "x.pdf", {})
    assert local is None and "no attachment path map" in why


# --------------------------------------------------------------------------- #
# The route — GetRows request shape
# --------------------------------------------------------------------------- #

def _call(client, idx=None, rbac=None, **kw):
    # "AP" matters: a BARE "invoice" resolves to the CUSTOMER (A/R) BO, which
    # is why every response carries the AP<->AR pivot note.
    kw.setdefault("target", "ap invoice attachments")
    kw.setdefault("where", "GroupID = 'BATCH-100'")
    return json.loads(_run(read_attachments(
        client, idx or _Idx(), rbac or _RBAC(), _SESSION, **kw)))


def test_group_is_one_call_with_the_sibling_opted_in(monkeypatch):
    """The headline case: 8 invoices, 8 PDFs, ONE GetRows POST."""
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(8))
    out = _call(client, target="ap invoice attachments")

    assert "error" not in out, out
    assert len(client.posts) == 1, "the group must not fan out per invoice"
    url, body = client.posts[0]
    assert url == f"{AP_SVC}/GetRows"
    # (a) EVERY DataSet table needs a whereClause param or Epicor 400s with
    # "Parameter whereClauseX is not found in the input object" ...
    assert set(body) == {"pageSize", "absolutePage",
                         "whereClauseAPInvHed", "whereClauseAPInvHedAttch",
                         "whereClauseAPInvDtl", "whereClauseAPInvSched"}
    # ... and the plural-only OData collection must NOT get one.
    assert "whereClauseAPInvoices" not in body
    # (b) the sibling's value must be "" — "1=0" is exactly why attachments
    # were unreachable before Fix 2.
    assert body["whereClauseAPInvHedAttch"] == ""
    assert body["whereClauseAPInvDtl"] == "1=0"
    assert "BATCH-100" in body["whereClauseAPInvHed"]

    assert out["row_count"] == 8
    assert out["total_rows"] == 8
    assert out["parent_rows"] == 8
    assert out["resolved"]["attachment_table"] == "APInvHedAttch"
    assert out["records"][0]["FileName"].startswith(WIN_ROOT)
    assert out["records"][0]["local_path"].startswith("/mnt/ap/")
    # Bookkeeping columns are stripped; the parent key is kept.
    assert "SysRowID" not in out["records"][0]
    assert out["records"][0]["InvoiceNum"] == "9000000"


def test_missing_where_is_an_envelope_not_a_full_scan():
    client = _Client(_dataset(1))
    out = _call(client, where="")
    assert out["error"] == "need_attachment_filter"
    assert "GroupID = 'GROUP001'" in out["message"]
    assert client.posts == []


def test_unnamed_parent_is_an_envelope():
    client = _Client(_dataset(1))
    out = _call(client, target="read the attached pdf")
    assert out["error"] == "need_attachment_parent"
    assert client.posts == []


def test_service_without_an_attch_table_names_the_ones_it_has():
    idx = _Idx(tables=["APInvHed", "APInvDtl", "APInvDtlAttch"])
    client = _Client(_dataset(1))
    out = _call(client, idx=idx)
    assert out["error"] == "no_attachment_table"
    assert out["valid"]["attachment_tables"] == ["APInvDtlAttch"]
    assert out["retry_with"]["target"] == "APInvDtl attachments"
    assert client.posts == []


def test_access_denied_never_hits_epicor():
    client = _Client(_dataset(1))
    out = _call(client, rbac=_RBAC(allowed=False))
    assert out["error"] == "access_denied"
    assert client.posts == []


def test_epicor_error_becomes_an_envelope():
    client = _Client(error=EpicorError(status_code=500, message="boom"))
    out = _call(client)
    assert out["error"] == "attachments_failed"
    assert "boom" in out["message"]


def test_no_parent_match_is_not_no_attachments():
    """Collapsing the two is how a WRONG BO reads as a terminal answer."""
    client = _Client({"APInvHed": [], "APInvHedAttch": []})
    out = _call(client)
    assert "error" not in out
    assert out["row_count"] == 0
    assert "NOT" in out["summary"]
    assert out.get("terminal") is not True


def test_parents_without_attachments_is_terminal():
    client = _Client({"APInvHed": [_hed()], "APInvHedAttch": []})
    out = _call(client)
    assert out["terminal"] is True
    assert out["row_count"] == 0
    assert out["parent_rows"] == 1
    assert "complete answer" in out["summary"]


# --------------------------------------------------------------------------- #
# File reading / text extraction
# --------------------------------------------------------------------------- #

def test_unreadable_file_is_reported_per_attachment_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    client = _Client({"APInvHed": [_hed()],
                      "APInvHedAttch": [_attch(fname=WIN_ROOT + "gone.pdf")]})
    out = _call(client, target="read the ap invoice pdf")
    assert "error" not in out
    rec = out["records"][0]
    assert rec["readable"] is False
    assert "not readable on this host" in rec["unavailable"]


def test_unmapped_path_is_reported_per_attachment(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client({"APInvHed": [_hed()],
                      "APInvHedAttch": [_attch(fname="Z:\\elsewhere\\x.pdf")]})
    out = _call(client)
    rec = out["records"][0]
    assert rec["readable"] is False
    assert "no local mount is configured" in rec["unavailable"]
    assert "local_path" not in rec


def _pdf_fixture(monkeypatch, tmp_path, body=b"%PDF-1.4 stub", name="inv.pdf"):
    (tmp_path / name).write_bytes(body)
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    return _Client({"APInvHed": [_hed()],
                    "APInvHedAttch": [_attch(fname=WIN_ROOT + name)]})


def test_text_is_extracted_when_the_caller_asks_to_read(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE",
                        {"mod": _fake_fitz(["Invoice INV-100\n", "PO 500002\n"])})
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert rec["readable"] is True and rec["size_bytes"] > 0
    assert rec["text"] == "Invoice INV-100\nPO 500002\n"
    assert "text extracted from 1" in out["summary"]


def test_no_text_when_the_caller_only_wants_the_files(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["secret"])})
    out = _call(client, target="ap invoice attachments")
    rec = out["records"][0]
    assert rec["readable"] is True
    assert "text" not in rec
    assert "read the <record> PDF" in out["summary"]


def test_missing_fitz_degrades_to_path_only(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": None})   # not installed
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert rec["readable"] is True
    assert "text" not in rec
    assert "PyMuPDF" in rec["text_note"]


def test_load_fitz_never_raises_when_absent(monkeypatch):
    """Import failure must degrade, never break module import or the route."""
    monkeypatch.setattr(att, "_FITZ_CACHE", {})
    # None in sys.modules makes `import fitz` raise ImportError — the exact
    # shape of a box without PyMuPDF.
    monkeypatch.setitem(sys.modules, "fitz", None)
    assert att._load_fitz() is None


def test_non_pdf_returns_path_and_a_note_never_bytes(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path, body=b"\x00\x01binary",
                          name="scan.tif")
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["x"])})
    out = _call(client, target="read the ap invoice attachment")
    rec = out["records"][0]
    assert rec["readable"] is True
    assert "text" not in rec
    assert "no text extractor for '.tif'" in rec["text_note"]


def test_oversize_file_is_not_read(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path, body=b"x" * 500)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["x"])})
    monkeypatch.setattr(att, "_MAX_FILE_BYTES", 100)
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert "text" not in rec
    assert "over the 100-byte read cap" in rec["text_note"]


def test_oversize_text_is_truncated_honestly(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["A" * 500])})
    monkeypatch.setattr(att, "_MAX_TEXT_CHARS", 40)
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert len(rec["text"]) == 40
    assert rec["text_truncated"] is True
    assert "cut at 40 characters" in rec["text_note"]


def test_response_wide_text_budget_is_announced(monkeypatch, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "b.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["Z" * 100])})
    monkeypatch.setattr(att, "_TOTAL_TEXT_CHARS", 50)
    client = _Client({
        "APInvHed": [_hed("1"), _hed("2")],
        "APInvHedAttch": [_attch("1", fname=WIN_ROOT + "a.pdf"),
                          _attch("2", fname=WIN_ROOT + "b.pdf")]})
    out = _call(client, target="read the ap invoice pdfs")
    first, second = out["records"]
    assert "text" in first
    assert "text" not in second
    assert "budget" in second["text_note"]
    assert "text_budget" in out["resolved"]["assumptions"]


def test_corrupt_pdf_is_a_note_not_a_crash(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)

    def _boom(path):
        raise RuntimeError("cannot open broken document")

    monkeypatch.setattr(att, "_FITZ_CACHE",
                        {"mod": types.SimpleNamespace(open=_boom)})
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert "error" not in out
    assert "could not open the PDF" in rec["text_note"]


def test_directory_is_not_a_readable_attachment(monkeypatch, tmp_path):
    (tmp_path / "folder.pdf").mkdir()
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    client = _Client({"APInvHed": [_hed()],
                      "APInvHedAttch": [_attch(fname=WIN_ROOT + "folder.pdf")]})
    out = _call(client, target="read the ap invoice pdf")
    assert out["records"][0]["readable"] is False
    assert "not a regular file" in out["records"][0]["unavailable"]


# --------------------------------------------------------------------------- #
# Recognizer argument contract
# --------------------------------------------------------------------------- #

def test_order_by_is_applied_over_the_full_set_before_the_trim(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(5))
    out = _call(client, order_by="InvoiceNum desc", limit=2)
    assert [r["InvoiceNum"] for r in out["records"]] == ["9000004", "9000003"]
    assert out["total_rows"] == 5
    assert "2 of 5" in out["summary"]
    notes = out["resolved"]["assumptions"]
    assert "InvoiceNum desc" in notes["order"]
    assert "limit_trim" in notes


def test_order_by_expression_uses_the_shared_refusal(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(2))
    out = _call(client, order_by="qty*cost")
    assert out["error"] == "order_expression_unsupported"
    assert "columns" in out["valid"]


def test_unknown_order_column_is_an_envelope(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(2))
    out = _call(client, order_by="Nope desc")
    assert out["error"] == "unknown_order_column"
    assert "FileName" in out["valid"]["columns"]


def test_fields_projects_and_announces(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(2))
    out = _call(client, fields="FileName, DrawDesc")
    assert set(out["records"][0]) == {"FileName", "DrawDesc"}
    assert "showed 2 of" in out["resolved"]["assumptions"]["fields"]


def test_unknown_fields_keep_the_rows_and_name_the_real_columns(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(1))
    out = _call(client, fields="Nope")
    assert "FileName" in out["records"][0]
    assert "are attachment columns" in out["resolved"]["assumptions"]["fields"]


def test_soft_bag_rides_into_assumptions(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(1))
    out = _call(client, soft={"site_resolved": {"Plant='Oakridge'": "Plant='10'"},
                              "args_ignored": "having ... were NOT applied"})
    notes = out["resolved"]["assumptions"]
    assert notes["site_resolved"] == {"Plant='Oakridge'": "Plant='10'"}
    assert "args_ignored" in notes


def test_pivot_hint_names_this_bo_and_the_lookalike(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(1))
    out = _call(client, target="ap invoice attachments")
    assert "APInvHed" in out["note"] and "AR invoice attachments" in out["note"]


def test_zero_parent_match_hands_back_the_lookalike_bo(monkeypatch):
    """A BARE 'invoice' resolves to A/R, so an A/P group id matches nothing.

    That must come back as a copy-paste pivot, not as "no attachments" — it is
    the common phrasing "read the invoice PDF for group ...".
    """
    idx = _Idx(tables=["InvcHead", "InvcHeadAttch", "InvcDtl"],
               service="Erp.BO.ARInvoiceSvc", plurals=("ARInvoices",))
    client = _Client({"InvcHead": [], "InvcHeadAttch": []})
    out = _call(client, idx=idx, target="read the invoice PDF for the group")
    assert "error" not in out
    assert out["resolved"]["entity_set"] == "InvcHead"
    assert out["retry_with"] == {"target": "AP invoice attachments",
                                 "where": "GroupID = 'BATCH-100'"}


def test_plural_collection_target_is_normalized_to_the_dataset_table(monkeypatch):
    """resolve_target hands back 'Customers'; GetRows and *Attch key off
    'Customer' — without the plural strip this dead-ends as
    no_attachment_table on a service that plainly has CustomerAttch."""
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    idx = _Idx(tables=["Customer", "CustomerAttch"],
               service="Erp.BO.CustomerSvc", plurals=("Customers",))
    client = _Client({"Customer": [{"CustNum": 58}],
                      "CustomerAttch": [{"CustNum": 58, "DrawDesc": "W-9",
                                         "FileName": WIN_ROOT + "w9.pdf"}]})
    out = _call(client, idx=idx, target="attachments for customer 58",
                where="CustNum = 58")
    assert "error" not in out, out
    assert out["resolved"]["entity_set"] == "Customer"
    assert out["resolved"]["attachment_table"] == "CustomerAttch"
    assert client.posts[0][1]["whereClauseCustomerAttch"] == ""
    assert "whereClauseCustomers" not in client.posts[0][1]


def test_attch_table_name_as_target_walks_up_to_the_parent(monkeypatch):
    """A model that discovered APInvHedAttch must not be sent to its 500ing
    OData collection — the whereClause belongs on the PARENT."""
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(1))
    out = _call(client, target="Erp.BO.APInvoiceSvc/APInvHedAttch")
    assert "error" not in out, out
    assert out["resolved"]["entity_set"] == "APInvHed"
    assert client.posts[0][1]["whereClauseAPInvHedAttch"] == ""


# --------------------------------------------------------------------------- #
# Dispatch — through the REGISTERED epicor_read
# --------------------------------------------------------------------------- #

def test_registered_epicor_read_routes_to_attachments(monkeypatch):
    """Nothing ahead of the attachment recognizer may swallow the phrase.

    test_recognizer_arg_forwarding stubs the route out with a spy, so it proves
    the kwargs but not that the REAL route runs end to end. This does — and it
    fails loudly if a future recognizer is inserted above this one.
    """
    from epicor_mcp.tools import read as read_mod
    from tests.test_read_routing_e2e import _RBAC as _E2ERBAC, _Server

    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    client = _Client(_dataset(3))
    srv = _Server()
    read_mod.register(srv, _Idx(), _E2ERBAC(), client)

    out = json.loads(asyncio.run(srv.fn(
        target="attachments for the ap invoice group",
        where="GroupID = 'BATCH-100'")))

    assert "error" not in out, out
    assert out["resolved"]["attachment_table"] == "APInvHedAttch"
    assert out["row_count"] == 3
    assert len(client.posts) == 1
    assert client.posts[0][1]["whereClauseAPInvHedAttch"] == ""
    # The dispatch-site announcement channel is wired (INV: never drop an arg).
    assert isinstance(out["resolved"].get("assumptions", {}), dict)


def test_registered_epicor_read_announces_arguments_it_cannot_apply(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    from epicor_mcp.tools import read as read_mod
    from tests.test_read_routing_e2e import _RBAC as _E2ERBAC, _Server

    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    srv = _Server()
    read_mod.register(srv, _Idx(), _E2ERBAC(), _Client(_dataset(1)))

    out = json.loads(asyncio.run(srv.fn(
        target="ap invoice attachments", where="GroupID = 'BATCH-100'",
        group_by="VendorNum", count_only=True)))
    note = out["resolved"]["assumptions"]["args_ignored"]
    assert "count_only" in note and "group_by" in note


@pytest.mark.parametrize("target,expected_route", [
    ("the routing attached to part ABC-123", "bom"),
    ("what BOM is part ABC-123 attached to", "bom"),
    ("components of part ABC-123 and the attached drawing", "bom"),
    ("what is part ABC-123 used to make, and the attached drawings",
     "where_used"),
    # The motivating case for dispatching attachments FIRST — unchanged.
    ("the drawing attached to the part used to make job 123", "attachments"),
    ("attachments for invoice INV-100", "attachments"),
])
def test_registered_read_does_not_let_attachments_shadow_bom(
        monkeypatch, target, expected_route):
    """The dispatch-site claim that attachments "cannot shadow" BOM/where-used
    was false: `\\battach(ed|ment|ments)\\b` matches ordinary English. Proven
    through the REGISTERED tool, which is where the steal actually happened."""
    from epicor_mcp.tools import read as read_mod
    from tests.test_read_routing_e2e import _RBAC as _E2ERBAC, _Server

    monkeypatch.setattr(read_mod, "get_current_session",
                        lambda: types.SimpleNamespace(user_id="tester"))
    seen: list[str] = []

    def _spy(name):
        async def _fn(*a, **kw):
            seen.append(name)
            return json.dumps({"records": []})
        return _fn

    for attr, name in (("read_attachments", "attachments"),
                       ("read_bom", "bom"),
                       ("where_used", "where_used")):
        monkeypatch.setattr(read_mod, attr, _spy(name))
    srv = _Server()
    read_mod.register(srv, _Idx(), _E2ERBAC(), _Client(_dataset(1)))

    asyncio.run(srv.fn(target=target))
    assert seen == [expected_route], f"{target!r} routed to {seen}"


# --------------------------------------------------------------------------- #
# Paging honesty — the PARENT page is not the display limit
# --------------------------------------------------------------------------- #

def test_parent_page_size_is_fixed_not_the_display_limit(monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _Client(_dataset(3))
    _call(client, limit=2)
    body = client.posts[0][1]
    assert body["pageSize"] == att._PARENT_PAGE_SIZE
    assert body["pageSize"] != 2, (
        "`limit` is the display trim; binding it to pageSize truncates the "
        "PARENT match set with nothing reporting it")


def test_order_by_ranks_the_whole_match_set_not_the_first_page(monkeypatch):
    """`pageSize == limit` made "order_by ... over the full result" a lie: the
    announced ranking covered only the parents that landed on page 1."""
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    client = _PagingClient(8)
    out = _call(client, limit=3, order_by="InvoiceNum desc")
    assert [r["InvoiceNum"] for r in out["records"]] == ["INV7", "INV6", "INV5"]
    assert out["total_rows"] == 8
    assert out["parent_rows"] == 8
    assert "limit_trim" in out["resolved"]["assumptions"]


def test_a_full_parent_page_is_announced_and_never_claims_completeness(
        monkeypatch):
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    monkeypatch.setattr(att, "_PARENT_PAGE_SIZE", 3)
    client = _PagingClient(50)
    out = _call(client, limit=100)
    assert out["summary"].startswith("INCOMPLETE:"), out["summary"]
    note = out["resolved"]["assumptions"]["incomplete"]
    assert "FULL page" in note and "Narrow `where`" in note


def test_no_attachments_on_a_full_parent_page_is_not_terminal(monkeypatch):
    """A full parent page without attachments may have more pages to inspect.
    The route must report incomplete coverage instead of a final empty answer.
    """
    monkeypatch.setattr(att, "_path_map", lambda: MAP)
    monkeypatch.setattr(att, "_PARENT_PAGE_SIZE", 2)
    client = _PagingClient(50, with_attachments=False)
    out = _call(client)
    assert out.get("terminal") is not True
    assert "complete answer" not in out["summary"]
    assert out["summary"].startswith("INCOMPLETE:")
    assert "FINAL" not in out["stop_hint"]


# --------------------------------------------------------------------------- #
# Text extraction runs AFTER the sort and the trim
# --------------------------------------------------------------------------- #

def test_text_budget_is_spent_on_the_rows_the_caller_ranked(monkeypatch, tmp_path):
    """Extracting in raw GetRows order left the #1-ranked row textless while
    rows that were then trimmed away had consumed the response-wide budget —
    and the summary counted extractions that are not in `records`."""
    for i in range(4):
        (tmp_path / f"f{i}.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["Z" * 100])})
    monkeypatch.setattr(att, "_TOTAL_TEXT_CHARS", 100)   # room for ONE file
    client = _Client({
        "APInvHed": [_hed(f"INV{i}") for i in range(4)],
        "APInvHedAttch": [_attch(f"INV{i}", fname=WIN_ROOT + f"f{i}.pdf")
                          for i in range(4)]})

    out = _call(client, target="read the ap invoice pdfs",
                order_by="InvoiceNum desc", limit=2)

    assert [r["InvoiceNum"] for r in out["records"]] == ["INV3", "INV2"]
    assert out["records"][0].get("text") == "Z" * 100, (
        "the budget went to a row the caller ranked away")
    # The summary must describe the rows that are actually in the body.
    assert sum(1 for r in out["records"] if r.get("text")) == 1
    assert "text extracted from 1" in out["summary"]


def test_readable_count_describes_the_rows_that_are_returned(monkeypatch, tmp_path):
    (tmp_path / "aaa.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(att, "_path_map", lambda: {WIN_ROOT: str(tmp_path)})
    client = _Client({
        "APInvHed": [_hed("1"), _hed("2")],
        "APInvHedAttch": [_attch("1", fname=WIN_ROOT + "aaa.pdf"),
                          _attch("2", fname=WIN_ROOT + "zzz.pdf")]})
    out = _call(client, limit=1, order_by="FileName desc")
    # "zzz.pdf" sorts first descending and does NOT exist on the share.
    assert out["records"][0]["readable"] is False
    assert "0 readable" in out["summary"]


# --------------------------------------------------------------------------- #
# PDF extraction is bounded BEFORE the allocation (decompression bomb)
# --------------------------------------------------------------------------- #

class _StreamDoc:
    """A doc whose pages expose content-stream xrefs, like real PyMuPDF."""

    def __init__(self, pages, streams):
        self._pages = pages
        self._streams = streams
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def xref_stream_raw(self, xref):
        return self._streams[xref]

    def close(self):
        self.closed = True


def test_decompression_bomb_page_is_refused_before_get_text(monkeypatch, tmp_path):
    """`page.get_text()` materialises a WHOLE page before any char cap is
    consulted, and _MAX_FILE_BYTES bounds the COMPRESSED size — so a small file
    can allocate gigabytes. The content-stream probe must refuse the page
    without ever calling get_text()."""
    client = _pdf_fixture(monkeypatch, tmp_path)
    touched: list[str] = []

    class _BombPage:
        def get_contents(self):
            return [7]

        def get_text(self):                      # pragma: no cover - must not run
            touched.append("allocated")
            return "A" * 5_000_000

    doc = _StreamDoc([_BombPage()],
                     {7: zlib.compress(b"A" * 5_000_000)})
    monkeypatch.setattr(att, "_FITZ_CACHE",
                        {"mod": types.SimpleNamespace(open=lambda p: doc)})
    monkeypatch.setattr(att, "_MAX_PAGE_STREAM_BYTES", 1000)

    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert touched == [], "the bomb page's text was materialised anyway"
    assert "per-page cap" in rec["text_note"]
    assert rec.get("text_truncated") is True


def test_probe_never_allocates_more_than_the_cap():
    """The bound itself: decompressobj(max_length) stops AT the cap, so probing
    a 5 MB-expanding stream costs ~1 KB, not 5 MB."""
    class _P:
        def get_contents(self):
            return [1]

    doc = _StreamDoc([], {1: zlib.compress(b"B" * 5_000_000)})
    size = att._page_stream_bytes(doc, _P())
    assert size is not None
    assert size <= att._MAX_PAGE_STREAM_BYTES + 1
    assert size > att._MAX_PAGE_STREAM_BYTES


def test_ordinary_page_passes_the_probe(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)

    class _Page:
        def get_contents(self):
            return [3]

        def get_text(self):
            return "Invoice INV-100"

    doc = _StreamDoc([_Page()], {3: zlib.compress(b"BT (hi) Tj ET")})
    monkeypatch.setattr(att, "_FITZ_CACHE",
                        {"mod": types.SimpleNamespace(open=lambda p: doc)})
    out = _call(client, target="read the ap invoice pdf")
    assert out["records"][0]["text"] == "Invoice INV-100"
    assert "text_note" not in out["records"][0]


def test_page_count_cap_stops_a_pathological_document(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["x"] * 5000)})
    monkeypatch.setattr(att, "_MAX_PDF_PAGES", 5)
    out = _call(client, target="read the ap invoice pdf")
    rec = out["records"][0]
    assert rec["text"] == "x" * 5
    assert "stopped after 5 pages" in rec["text_note"]


def test_wall_clock_budget_stops_a_slow_document(monkeypatch, tmp_path):
    client = _pdf_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(att, "_FITZ_CACHE", {"mod": _fake_fitz(["x"] * 50)})
    monkeypatch.setattr(att, "_PDF_TIME_BUDGET_S", 0.0)
    ticks = iter([0.0] + [99.0] * 200)
    # Patch the module's OWN reference, never the stdlib module: asyncio calls
    # time.monotonic too and would drain the ticks before extraction starts.
    monkeypatch.setattr(
        att, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    out = _call(client, target="read the ap invoice pdf")
    assert "extraction budget" in out["records"][0]["text_note"]


# --------------------------------------------------------------------------- #
# Settings wiring
# --------------------------------------------------------------------------- #

def test_default_settings_map_exposes_no_local_share():
    from epicor_mcp.config import Settings
    mapping = Settings().attachment_path_map
    assert mapping == {}


def test_path_map_reads_settings(monkeypatch):
    monkeypatch.setattr(
        att, "get_settings",
        lambda: types.SimpleNamespace(attachment_path_map={"X:\\": "/srv/x"}))
    assert att._path_map() == {"X:\\": "/srv/x"}
    local, _ = local_path_for("X:\\a\\b.pdf")
    assert local == os.path.realpath("/srv/x/a/b.pdf")
