"""Attachments / linked documents folded into ``epicor_read`` (INV-2, no new tool).

This helper resolves attachment requests such as "the invoice PDF" or
"attachments for X" through the owning business object's GetRows dataset.
Attachment child tables such as ``APInvHedAttch`` may be listed in metadata
without a usable standalone OData collection. ``include_tables`` opts those
children into the dataset instead of filtering them out with ``1=0``.

File contents are read from an operator-configured local mount, using
``Settings.attachment_path_map`` to map Epicor's stored paths. No file share,
mount, or AttachmentSvc permission is assumed. A group-level GetRows filter
can fetch the invoice headers and their attachment paths together.

**GetRows, not GetByID.** ``APInvoiceSvc/GetByID {vendorNum, invoiceNum}`` also
returns ``APInvHedAttch``, but it needs BOTH keys — and ``VendorNum`` is exactly
what a caller holding an invoice number does not have — while the GetRows
whereClause serves the single-invoice case (``InvoiceNum = '…'``) *and* the
group case from one code path. A second, rarely-reachable fetch path would be
untested surface for no coverage.

Ordering: **APPLY**, client-side, over the FULL attachment set BEFORE the trim
to ``limit`` (the where-used pattern) — the rows are materialised here anyway,
and GetRows' ``By <col>`` orders the PARENT table, not the sibling. Refusals go
through ``sort_records``/``order_refusal`` so ``order_by='qty*cost'`` reads the
same here as on every other route.

Paging: the GetRows ``pageSize`` is the fixed ``_PARENT_PAGE_SIZE``, NOT
``limit``. ``limit`` is a display trim only. Binding the two made the parent
MATCH SET silently truncated — "order_by over the full result" ranked one page,
and a page that came back full was summarised as "N of N … the complete
answer". A full parent page is announced and forfeits every completeness claim
(including the ``terminal`` no-attachments one). PDF text is extracted AFTER
the sort and the trim, over the rows actually returned, so the response-wide
budget is never spent on rows the caller ranked away.

Security posture — this is a file reader driven by a database field, so the
stored path is UNTRUSTED input: only paths under an allow-listed local root are
opened, containment is re-checked after ``os.path.realpath`` (so ``..`` and
symlinks cannot escape), file size and extracted-text size are capped and
truncation is announced, and text is extracted only from types we actually
support (PDF via PyMuPDF). Anything else returns the path and a note — never
raw bytes. The extracted-text cap does not bound ALLOCATION (``get_text()``
materialises a whole page first, and a Flate-compressed content stream can
expand thousands-fold), so a page/time cap and a bounded decompressed
content-stream probe guard the OOM path. Imported files may originate outside
the operator's organization, so their bytes must be treated as untrusted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
import zlib
from typing import TYPE_CHECKING

from epicor_mcp.config import get_settings
from epicor_mcp.epicor_client.error_handler import EpicorError
from epicor_mcp.tools._engine import (
    _build_getrows_where,
    _dataset_tables,
    date_columns_for,
    extract_getrows_tables,
    resolve_include_tables,
    sql_to_odata,
)
from epicor_mcp.tools._inline_schema import order_refusal, sort_records
from epicor_mcp.tools._partviews import _BOM_RE
from epicor_mcp.tools._resolve import error_envelope, resolve_target
from epicor_mcp.tools._whereused import _WHEREUSED_RE

if TYPE_CHECKING:  # pragma: no cover
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex
    from epicor_mcp.rbac.enforcer import RBACEnforcer

logger = logging.getLogger(__name__)

# Byte/char caps. A 700 KB inline budget is shared with the rows, so text is
# capped per file AND across the response; both truncations are announced.
_MAX_FILE_BYTES = 25_000_000
_MAX_TEXT_CHARS = 12_000
_TOTAL_TEXT_CHARS = 60_000

# One fetch page for the PARENT table, INDEPENDENT of `limit`. `limit` is a
# DISPLAY trim (the _whereused/_posugg pattern): binding it to GetRows'
# `pageSize` silently truncated the parent MATCH SET, so `order_by` ranked one
# page and announced a full-set ordering, and a page that came back full was
# summarised as "N of N ... the complete answer". A full page is announced.
_PARENT_PAGE_SIZE = 500









_MAX_PDF_PAGES = 50
_MAX_PAGE_STREAM_BYTES = 1_000_000
_PDF_TIME_BUDGET_S = 10.0

# Extensions we have a real extractor for. Everything else returns path + note.
_TEXT_EXTS = {".pdf"}

# Bookkeeping columns on an *Attch row that carry no information for a reader.
_ATTACH_NOISE = {"Company", "SysRowID", "SysRevID", "ForeignSysRowID", "RowMod"}

# Phrases that unambiguously ask for the FILE attached to a record.
_ATTACH_STRONG_RE = re.compile(
    r"\battach(?:ed|ment|ments)\b"
    r"|\bpdfs?\b"
    r"|\bscanned\s+(?:copy|image|document|invoice|file)\b"
    # No leading \b: the model may name the table it found, and there is no
    # word boundary inside "APInvHedAttch".
    r"|attch\b",
    re.IGNORECASE)
# Weaker forms that need a "linked/attached" pairing — a bare "file"/"document"
# is far too common in ordinary reads to route on.
_ATTACH_WEAK_RE = re.compile(
    r"\b(?:files?|documents?|docs?|drawings?|images?)\s+(?:is\s+|are\s+|that\s+)?"
    r"(?:linked|attached)\b"
    r"|\b(?:linked|attached)\s+(?:files?|documents?|docs?|drawings?|images?)\b",
    re.IGNORECASE)
# The caller wants the CONTENTS, not just the path.
_WANT_TEXT_RE = re.compile(
    r"\bread\b|\bextract\w*\b|\bcontents?\b|\btext\b|\bparse\b|\bocr\b"
    r"|\bwhat\s+does\s+.*\bsay\b|\bsummar\w+\b|\bopen\b",
    re.IGNORECASE)

# Trigger/instruction words removed before the residual phrase is handed to
# resolve_target — "read the invoice PDF for group GROUP001" must resolve on
# "invoice group GROUP001", not on "read … pdf".
_TRIGGER_STRIP_RE = re.compile(
    r"(?i)\b(?:attach|attached|attachment|attachments|attch|pdf|pdfs|file|"
    r"files|document|documents|doc|docs|drawing|drawings|scan|scans|scanned|"
    r"image|images|read|reads|extract|extracted|contents|content|text|parse|"
    r"ocr|summarize|summarise|summary|open|linked|link|show|me|what|which|"
    r"whats|is|are|the|to|for|from|on|of|directly)\b")

# Look-alike BOs the model cannot separate on the word "invoice" alone. Every
# response names THIS BO and how to pivot, so a wrong pick self-corrects in one
# turn (the _posugg pattern) instead of reading as "there are no attachments".
_PIVOT_HINTS: dict[str, dict[str, str]] = {
    "InvcHead": {
        "note": ("This is Erp.BO.ARInvoiceSvc/InvcHead — the CUSTOMER (A/R) "
                 "invoice, which is where a BARE 'invoice' resolves. "
                 "Supplier/vendor bills (the ones in an AP group like "
                 "'GROUP001') are a DIFFERENT table: re-call with "
                 "target=\"AP invoice attachments\"."),
        "retry_target": "AP invoice attachments"},
    "APInvHed": {
        "note": ("This is Erp.BO.APInvoiceSvc/APInvHed — the SUPPLIER (A/P) "
                 "invoice. Customer invoices are a different table: re-call "
                 "with target=\"AR invoice attachments\"."),
        "retry_target": "AR invoice attachments"},
}


# --------------------------------------------------------------------------- #
# Recognition (pure)
# --------------------------------------------------------------------------- #

def detect_attachments(target: str, where: str = "") -> bool:
    """True when the phrase asks for the file(s) attached to a record.

    Guarded on purpose: a bare "file"/"document" does NOT route here (it shows
    up in ordinary reads), and neither does anything that only mentions a
    record. The strong forms — attachment/attached/pdf/scanned copy, or the
    literal ``*Attch`` table name — are unambiguous.

    **"attached" is ordinary English, not attachment-specific vocabulary**, so
    it co-occurs freely with BOM / where-used phrasing: "the routing attached
    to part X", "what BOM is X attached to", "components of X and the attached
    drawing" all matched here and — because this route is dispatched FIRST —
    STOLE the phrase from ``read_bom``, which had answered them, and dead-ended
    on ``need_attachment_filter`` with the part number discarded. The reverse
    steal is just as real ("the drawing attached to the part used to make job
    123" belongs here, not to where-used), so neither ordering alone is
    correct. Tie-break on POSITION: whichever vocabulary the phrase LEADS with
    owns it. The rival patterns are imported, not restated, so a phrase added
    to ``_BOM_RE`` / ``_WHEREUSED_RE`` is honoured here automatically.
    """
    t = (target or "").strip()
    if not t:
        return False
    hits = [m for m in (_ATTACH_STRONG_RE.search(t), _ATTACH_WEAK_RE.search(t))
            if m is not None]
    if not hits:
        return False
    rivals = [m for m in (_BOM_RE.search(t), _WHEREUSED_RE.search(t))
              if m is not None]
    if rivals and min(m.start() for m in rivals) < min(m.start() for m in hits):
        return False
    return True


def wants_text(target: str) -> bool:
    """True when the caller asked for the CONTENTS, not just the file path.

    ``epicor_read`` has no ``include_text`` parameter and inventing one would
    widen the tool surface for something the phrasing already states plainly
    ("read the invoice PDF", "extract the text"). Path-only responses say how
    to ask for the text, so the affordance is never hidden.
    """
    return bool(_WANT_TEXT_RE.search(target or ""))


# --------------------------------------------------------------------------- #
# Windows path -> allow-listed local path (untrusted input)
# --------------------------------------------------------------------------- #

def _norm_sep(p: str) -> str:
    """Separator-normalised path (case PRESERVED) for prefix comparison."""
    s = (p or "").strip().replace("/", "\\")
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s


def _path_map() -> dict[str, str]:
    """windows-prefix -> local root, from settings (env-overridable)."""
    try:
        return dict(get_settings().attachment_path_map or {})
    except Exception:  # noqa: BLE001 — never fail a read over config
        logger.exception("attachment_path_map unreadable")
        return {}


def local_path_for(
    windows_path: str, path_map: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """``(local_path, reason)`` for an Epicor ``FileName``.

    The stored path is UNTRUSTED (a database field), so:

    * only a path under an allow-listed windows prefix is considered at all —
      the drive-letter comparison is case-INSENSITIVE because Epicor's stored
      case varies;
    * the candidate is ``realpath``-ed and containment re-checked AFTER
      resolution, so ``..`` segments and symlinks cannot escape the root.

    ``reason`` is non-empty exactly when ``local_path`` is None, and is written
    to be shown to the user (a refusal is a normal per-attachment outcome).
    """
    raw = (windows_path or "").strip()
    if not raw:
        return None, "the attachment row has no FileName"
    mapping = _path_map() if path_map is None else dict(path_map)
    if not mapping:
        return None, ("no attachment path map is configured "
                      "(EPICOR_MCP_ATTACHMENT_PATH_MAP)")
    normed = _norm_sep(raw)
    lowered = normed.lower()
    for win_prefix, local_root in mapping.items():
        pfx = _norm_sep(win_prefix)
        if not pfx.endswith("\\"):
            pfx += "\\"
        if not lowered.startswith(pfx.lower()):
            continue
        rel = normed[len(pfx):].replace("\\", "/").lstrip("/")
        try:
            root = os.path.realpath(str(local_root))
            candidate = os.path.realpath(os.path.join(root, rel))
        except (ValueError, OSError) as exc:
            # e.g. an embedded NUL in the stored FileName. A malformed database
            # field must be a per-attachment note, never a 500 on the read.
            return None, f"the stored path could not be resolved ({exc})"
        # Containment re-checked AFTER realpath: this is the check that stops
        # "..\\..\\etc\\passwd" and a symlink pointing off the share.
        if candidate != root and not candidate.startswith(root + os.sep):
            return None, (
                f"path resolves OUTSIDE the allow-listed root {local_root} — "
                "refused (traversal or symlink escape)")
        return candidate, ""
    return None, (
        "no local mount is configured for this path; allow-listed roots: "
        + (", ".join(sorted(mapping)) or "none"))


# --------------------------------------------------------------------------- #
# File inspection + text extraction
# --------------------------------------------------------------------------- #

_FITZ_CACHE: dict = {}


def _load_fitz():
    """PyMuPDF, or None when it isn't installed here.

    Imported lazily and cached: the module must import (and the suite must run)
    on a box without PyMuPDF, degrading to "path only, text extraction
    unavailable" rather than breaking every attachment read.
    """
    if "mod" not in _FITZ_CACHE:
        try:
            import fitz  # type: ignore
        except Exception:  # noqa: BLE001 — absence is a normal outcome
            fitz = None
        _FITZ_CACHE["mod"] = fitz
    return _FITZ_CACHE["mod"]


def _page_stream_bytes(doc, page) -> int | None:
    """Decompressed content-stream size of *page*, BOUNDED — None if unknown.

    This is the only pre-flight bound available: PyMuPDF has no extraction call
    whose OUTPUT is capped (``get_text()`` builds the whole structured-text page
    first, and a clip rect does not help because glyph cost does not scale with
    page area). ``zlib.decompressobj().decompress(raw, max_length)`` stops at
    the cap and parks the rest in ``unconsumed_tail``, so probing NEVER
    allocates more than ``_MAX_PAGE_STREAM_BYTES`` even for a decompression
    bomb. A page over the cap is skipped and announced.

    Returns None when the shape is not probe-able (no ``get_contents`` on this
    PyMuPDF build, a non-stream page, an unreadable xref) — extraction then
    proceeds under the page/time caps alone rather than refusing a good file.
    """
    try:
        xrefs = list(page.get_contents() or [])
    except Exception:  # noqa: BLE001 — a probe must never break extraction
        return None
    if not xrefs:
        return None
    total = 0
    for xref in xrefs:
        try:
            raw = bytes(doc.xref_stream_raw(xref) or b"")
        except Exception:  # noqa: BLE001
            return None
        # +1 so "exactly at the cap" is distinguishable from "over it".
        room = max(0, _MAX_PAGE_STREAM_BYTES - total) + 1
        try:
            out = zlib.decompressobj().decompress(raw, room)
        except zlib.error:
            # Stored, or a filter we don't speak: the raw bytes ARE the size.
            out = raw
        total += len(out)
        if total > _MAX_PAGE_STREAM_BYTES:
            return total
    return total


def _extract_pdf_text(path: str, limit: int) -> tuple[str | None, bool, str, str]:
    """``(text, truncated, error, cap_note)`` — page text up to *limit* chars."""
    mod = _load_fitz()
    if mod is None:
        return None, False, (
            "PyMuPDF (fitz) is not installed here, so text could not be "
            "extracted; the local path above is still readable"), ""
    try:
        doc = mod.open(path)
    except Exception as exc:  # noqa: BLE001 — a corrupt file is not a crash
        return None, False, f"could not open the PDF ({exc})", ""
    caps: list[str] = []
    deadline = time.monotonic() + _PDF_TIME_BUDGET_S
    try:
        chunks: list[str] = []
        total = 0
        pages = 0
        skipped = 0
        for page in doc:
            if pages >= _MAX_PDF_PAGES:
                caps.append(f"stopped after {_MAX_PDF_PAGES} pages")
                break
            if time.monotonic() > deadline:
                caps.append(
                    f"stopped after the {_PDF_TIME_BUDGET_S:g}s extraction "
                    "budget")
                break
            pages += 1
            size = _page_stream_bytes(doc, page)
            if size is not None and size > _MAX_PAGE_STREAM_BYTES:
                # Refused BEFORE get_text() — this is the decompression-bomb
                # guard; the page's text is never materialised.
                skipped += 1
                continue
            try:
                piece = page.get_text() or ""
            except Exception:  # noqa: BLE001
                piece = ""
            chunks.append(piece)
            total += len(piece)
            if total >= limit:
                break
        if skipped:
            caps.append(
                f"skipped {skipped} page(s) whose content stream exceeds the "
                f"{_MAX_PAGE_STREAM_BYTES}-byte per-page cap")
        text = "".join(chunks)
    except Exception as exc:  # noqa: BLE001
        return None, False, f"could not read the PDF text ({exc})", ""
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    cap_note = "; ".join(caps)
    if len(text) > limit:
        return text[:limit], True, "", cap_note
    return text, False, "", cap_note


def _inspect(local_path: str) -> dict:
    """Cheap ``os.stat`` facts for one resolved local path. Never raises.

    Deliberately does NOT extract text: extraction is deferred until after the
    sort and the trim (see ``_extract_into``). These columns are the ones a
    caller can sort on, so they must exist BEFORE ``sort_records`` runs.
    """
    info: dict = {"local_path": local_path}
    try:
        st = os.stat(local_path)
    except OSError as exc:
        info["readable"] = False
        info["unavailable"] = (
            f"not readable on this host ({exc.strerror or exc})")
        return info
    if not stat.S_ISREG(st.st_mode):
        info["readable"] = False
        info["unavailable"] = "path exists but is not a regular file"
        return info
    info["readable"] = True
    info["size_bytes"] = st.st_size
    return info


def _extract_into(rec: dict, *, budget: int) -> int:
    """Extract PDF text into *rec* in place; returns the characters consumed.

    Runs over the rows that are actually SHOWN, after ordering and the trim.
    Spending the response-wide budget in raw GetRows order left the rows the
    caller ranked to the TOP textless while rows that were then thrown away had
    consumed it — and the summary counted extractions from records not in the
    response.
    """
    local_path = rec.get("local_path") or ""
    if not local_path or not rec.get("readable"):
        return 0
    size = int(rec.get("size_bytes") or 0)
    if size > _MAX_FILE_BYTES:
        rec["text_note"] = (
            f"{size} bytes is over the {_MAX_FILE_BYTES}-byte read cap — "
            "path only")
        return 0
    ext = os.path.splitext(local_path)[1].lower()
    if ext not in _TEXT_EXTS:
        rec["text_note"] = (
            f"no text extractor for '{ext or 'no extension'}' files — path "
            "only (raw bytes are never returned)")
        return 0
    if budget <= 0:
        rec["text_note"] = (
            "the response-wide extracted-text budget was already spent on "
            "earlier attachments — narrow `where` or lower `limit` to read "
            "this one")
        return 0
    text, truncated, err, cap_note = _extract_pdf_text(
        local_path, min(_MAX_TEXT_CHARS, budget))
    if text is None:
        rec["text_note"] = err
        return 0
    rec["text"] = text
    notes = []
    if truncated:
        rec["text_truncated"] = True
        notes.append(f"text cut at {len(text)} characters (per-file cap); the "
                     "file holds more")
    if cap_note:
        rec["text_truncated"] = True
        notes.append(cap_note)
    if notes:
        rec["text_note"] = "; ".join(notes)
    return len(text)


# --------------------------------------------------------------------------- #
# Parent + attachment-table resolution
# --------------------------------------------------------------------------- #

def _resolve_parent(index: "ServiceIndex", target: str) -> tuple[str, str, dict | None]:
    """``(service, parent_entity, error)`` for the record whose files are wanted."""
    residual = _TRIGGER_STRIP_RE.sub(" ", target or "")
    residual = re.sub(r"\s+", " ", residual).strip(" ,.-")
    if not residual:
        return "", "", error_envelope(
            "need_attachment_parent",
            "Name the RECORD whose attachments you want — attachments hang off "
            "a business record, not off a file store. Re-call with e.g. "
            "target=\"AP invoice attachments\" (or job / part / customer / "
            "purchase order) plus a `where` that identifies the record.",
            retry_with={"target": "AP invoice attachments",
                        "where": "GroupID = '<group>'"},
        )
    res = resolve_target(index, residual)
    service = res.get("service") or ""
    entity = res.get("entity_set") or ""
    if not service:
        return "", "", error_envelope(
            "unresolved_attachment_parent"
            if not res.get("candidates") else "ambiguous_attachment_parent",
            f"Could not resolve '{residual}' to the record that owns the "
            "attachments. Re-call with an exact 'Service/Entity' (e.g. "
            "'Erp.BO.APInvoiceSvc/APInvHed') plus the `where` that identifies "
            "the record.",
            candidates=res.get("candidates") or None,
        )
    tables = _dataset_tables(index, service)
    # resolve_target can hand back the PLURAL OData collection ("Customers"),
    # which is not a DataSet table — GetRows and <Entity>Attch both key off the
    # singular name. Same translation _build_getrows_where applies downstream.
    if entity not in tables:
        for stripped in (entity[:-1] if entity.endswith("s") else None,
                         entity[:-2] if entity.endswith("es") else None):
            if stripped and stripped in tables:
                entity = stripped
                break
    # The model may name the CHILD table it discovered ("APInvHedAttch"); the
    # whereClause has to go on the PARENT, so walk back up.
    if entity.lower().endswith("attch"):
        parent = entity[: -len("Attch")]
        if parent in tables:
            entity = parent
    return service, entity, None


def _attachment_table(
    index: "ServiceIndex", service: str, entity: str,
) -> tuple[str, dict | None]:
    """``(attch_table, error)`` — the ``<Entity>Attch`` sibling, INV-1 on miss."""
    resolved, err = resolve_include_tables(index, service, [f"{entity}Attch"])
    if err is not None:
        tables = _dataset_tables(index, service)
        attch = [t for t in tables if t.lower().endswith("attch")]
        return "", error_envelope(
            "no_attachment_table",
            f"{service}/{entity} has no {entity}Attch table, so this record "
            "type carries no Epicor attachments. "
            + (f"Attachment tables on this service: {', '.join(attch)} — "
               "re-target the record type that owns them."
               if attch else
               "This service has no attachment tables at all; the document is "
               "probably filed against a different record (invoice, job, "
               "part)."),
            valid={"attachment_tables": attch} if attch else None,
            retry_with=({"target": f"{attch[0][:-len('Attch')]} attachments"}
                        if attch else None),
        )
    return sorted(resolved)[0], None


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

async def read_attachments(
    client: "EpicorClient",
    index: "ServiceIndex",
    rbac: "RBACEnforcer",
    session,
    *,
    target: str,
    where: str = "",
    limit: int = 100,
    order_by: str = "",
    fields: str = "",
    soft: dict | None = None,
) -> str:
    """Answer "the PDF attached to X" in ONE GetRows call (+ local file reads)."""
    notes = dict(soft or {})

    service, entity, err = _resolve_parent(index, target)
    if err is not None:
        return json.dumps(err)

    if not (where or "").strip():
        # An unbounded attachment read would page every record on the BO. Hand
        # back a ready-to-paste filter rather than guessing which key an
        # unlabelled token in the phrase belongs to (GroupID? InvoiceNum?
        # VendorNum?) — that guess is the silent-wrong-answer trap.
        return json.dumps(error_envelope(
            "need_attachment_filter",
            f"Say WHICH {entity} record(s) — attachments are fetched by "
            "filtering the parent record, and an unfiltered read would scan "
            f"every {entity} in the company. Re-call with the same target plus "
            "a `where`; for an AP invoice group that is "
            "where=\"GroupID = 'GROUP001'\", for one invoice "
            "where=\"InvoiceNum = 'INV-001'\".",
            # A PLACEHOLDER, deliberately: inventing a plausible column name
            # here ("APInvHedKey") would be a wrong name shipped as a fix.
            retry_with={"target": target, "where": "<KeyColumn> = '<value>'"},
        ))

    attch, err = _attachment_table(index, service, entity)
    if err is not None:
        return json.dumps(err)

    allowed, msg = rbac.check_access(session.user_id, service)
    if not allowed:
        return json.dumps(error_envelope("access_denied", msg))
    api_key = rbac.check_service_access(session.user_id, service).api_key or ""

    odata_filter = sql_to_odata(
        where, date_columns=date_columns_for(index, service, entity))
    # include_tables is the Fix-2 sibling opt-in: the named table gets "" where
    # every other table keeps "1=0". Without it Epicor returns the header rows
    # and NOTHING else, which is why attachments were unreachable.
    where_body, target_table = _build_getrows_where(
        index, service, entity, odata_filter, "", include_tables={attch})
    # pageSize is the PARENT page and is deliberately NOT `limit`: `limit` is
    # the display trim. Tying them made a filter matching more records than
    # `limit` come back as "N of N ... complete", with `order_by` ranking only
    # the rows that happened to land on page 1.
    body = {"pageSize": _PARENT_PAGE_SIZE, "absolutePage": 1, **where_body}

    try:
        resp = await client.post(f"{service}/GetRows", api_key, json_body=body)
    except EpicorError as exc:
        return json.dumps(error_envelope(
            "attachments_failed",
            f"Could not read {service}/{entity} attachments: "
            f"{exc.message or exc}",
            detail={"status": getattr(exc, "status_code", None),
                    "message": exc.message or str(exc)},
        ))

    tables = extract_getrows_tables(resp, [target_table, attch])
    parent_rows = [r for r in (tables.get(target_table) or [])
                   if isinstance(r, dict)]
    attch_rows = [r for r in (tables.get(attch) or []) if isinstance(r, dict)]
    hint = _PIVOT_HINTS.get(entity) or {}
    pivot = hint.get("note", "")

    # A FULL parent page means more records match than were read, so nothing
    # downstream may claim completeness (paging honesty; this route has no
    # cursor, which is why the recovery is a narrower `where`).
    parent_page_full = len(parent_rows) >= _PARENT_PAGE_SIZE
    if parent_page_full:
        notes["incomplete"] = (
            f"GetRows returned a FULL page ({_PARENT_PAGE_SIZE} {entity} "
            f"rows); MORE records match {where!r} and their attachments are "
            "NOT in this response. Narrow `where` to reach the rest — this "
            "route has no cursor, and raising `limit` will not help (it trims "
            "the attachments shown, it does not widen the fetch).")

    if not parent_rows:
        # NOT "no attachments" — the filter matched no parent record at all.
        # Collapsing the two is how "wrong BO" reads as a terminal answer: a
        # bare "invoice" resolves to A/R, so an A/P group id legitimately
        # matches nothing here. Hand back the pivot as a copy-paste retry.
        payload: dict = {
            "summary": (f"No {entity} record matched {where!r}, so there is "
                        "nothing to read attachments from. This is NOT "
                        "'the record has no attachments'."),
            "stop_hint": ("Check the key and the record TYPE before retrying. "
                          + (pivot or "If the record lives on a different BO, "
                             "re-target that BO.")),
            "row_count": 0,
            "resolved": {"service": service, "entity_set": entity,
                         "attachment_table": attch, "via": "GetRows",
                         **({"assumptions": notes} if notes else {})},
            "records": [],
        }
        if hint.get("retry_target"):
            payload["retry_with"] = {"target": hint["retry_target"],
                                     "where": where}
        return json.dumps(payload, default=str)

    if not attch_rows:
        return json.dumps({
            "summary": (
                (f"INCOMPLETE: the first {len(parent_rows)} {entity} record(s) "
                 f"matching {where!r} have no attachment on file, but MORE "
                 "records match and were NOT read. This is NOT 'no documents "
                 "exist'.")
                if parent_page_full else
                (f"{len(parent_rows)} {entity} record(s) matched "
                 f"{where!r} and NONE has an attachment on file. That "
                 "is the complete answer.")),
            "stop_hint": (
                ("Narrow `where` and re-call — the match set was cut off at "
                 f"{_PARENT_PAGE_SIZE} records, so an absent document proves "
                 "nothing yet. " + pivot)
                if parent_page_full else
                ("FINAL — do NOT hunt for the document in other "
                 "tables; *Attch is the only place Epicor records "
                 "one, and it is empty for these records. " + pivot)),
            **({} if parent_page_full else {"terminal": True}),
            "row_count": 0,
            "parent_rows": len(parent_rows),
            "resolved": {"service": service, "entity_set": entity,
                         "attachment_table": attch, "via": "GetRows",
                         **({"assumptions": notes} if notes else {})},
            "records": [],
        }, default=str)

    want_text = wants_text(target)
    records: list[dict] = []
    for row in attch_rows:
        rec = {k: v for k, v in row.items()
               if k not in _ATTACH_NOISE and not k.startswith("@")}
        win_path = str(row.get("FileName") or "")
        local, reason = local_path_for(win_path)
        if local is None:
            rec["readable"] = False
            rec["unavailable"] = reason
        else:
            rec.update(_inspect(local))
        records.append(rec)

    total_rows = len(records)
    available = sorted({k for r in records for k in r})
    records, err_kind, valid_cols = sort_records(
        records, order_by, available=available)
    if err_kind:
        return json.dumps(order_refusal(err_kind, order_by, valid_cols))
    if (order_by or "").strip():
        notes["order"] = f"{order_by} (client-side, over the full result)"

    # Capture the TOTAL before the slice — a trimmed list reported as the whole
    # set is the BOM trap (a partial answer with a specific, wrong count).
    shown = records[: max(1, int(limit) or 1)]
    if total_rows > len(shown):
        notes["limit_trim"] = (
            f"showed {len(shown)} of {total_rows} attachment(s) (limit); "
            "raise `limit` for the rest.")

    # Text extraction happens HERE — after the ordering and the trim, over the
    # rows that are actually returned. Extracting in fetch order spent the
    # response-wide budget on rows the caller had ranked away.
    budget = _TOTAL_TEXT_CHARS
    readable = sum(1 for r in shown if r.get("readable"))
    extracted = 0
    if want_text:
        for rec in shown:
            budget -= _extract_into(rec, budget=budget)
            if rec.get("text"):
                extracted += 1
    if budget <= 0 and want_text:
        notes["text_budget"] = (
            f"the {_TOTAL_TEXT_CHARS}-character response-wide text budget was "
            "reached; later attachments came back as paths only.")

    if (fields or "").strip():
        cols = sorted({k for r in shown for k in r})
        want = [c for c in cols
                if c.lower() in {t.strip().lower()
                                 for t in fields.split(",") if t.strip()}]
        if want:
            shown = [{c: r[c] for c in want if c in r} for r in shown]
            notes["fields"] = f"showed {len(want)} of {len(cols)} columns"
        else:
            notes["fields"] = (
                f"none of '{fields}' are attachment columns "
                f"({', '.join(cols)}); all columns shown")

    text_bit = (f", text extracted from {extracted}" if want_text
                else "; ask \"read the <record> PDF\" to get the text")
    return json.dumps({
        "summary": (("INCOMPLETE: " if parent_page_full else "")
                    + f"{len(shown)} of {total_rows} attachment(s) on "
                    f"{len(parent_rows)} {entity} record(s) matching {where!r} "
                    f"— {readable} readable on the local file share{text_bit}."),
        "stop_hint": ("These ARE the attachments; present them (and any "
                      "extracted text) now. Do NOT call "
                      "Ice.BO.AttachmentSvc/DownloadFile — it is access-denied "
                      "for this key — and do NOT read *Attch as an OData "
                      "collection; it 500s."),
        "row_count": len(shown),
        "total_rows": total_rows,
        "parent_rows": len(parent_rows),
        "resolved": {"service": service, "entity_set": entity,
                     "attachment_table": attch,
                     "via": f"GetRows include_tables={attch}",
                     "text_extracted": want_text,
                     **({"assumptions": notes} if notes else {})},
        "note": ("`FileName` is the path Epicor stores; `local_path` is the "
                 "same file on the mounted share and is what was read. "
                 + pivot).strip(),
        "records": shown,
    }, default=str)
