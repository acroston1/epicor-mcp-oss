"""Shared resolver + INV-1 error-envelope builder for the legacy intent tools.

Every intent tool (``epicor_read``, ``epicor_act``, ``epicor_baq``) routes its
``target``/``fields`` through this module so that:

* fuzzy business terms ("open POs", "vendors") map to a real
  ``service`` + ``entity_set`` (resolution order), and
* every fixable failure returns the *same* structured envelope that hands the
  model the correct names to retry with (INV-1) — never a bare reason code.

Public API
----------
resolve_target(index, raw)          -> dict(service, entity_set, candidates, filter_hint)
resolve_fields(index, service, entity_set, raw_fields)
                                    -> dict(fields, unknown, valid_columns, default_used, suggestions)
error_envelope(reason, message, *, valid=None, retry_with=None, candidates=None) -> dict
"""

from __future__ import annotations

import difflib
import json
import re
from typing import TYPE_CHECKING

from epicor_mcp.tools._inline_schema import (
    CURATED_BY_ENTITY,
    FIELD_OVERRIDES,
    TOP_SERVICES,
    _select_fields_heuristic,
    _table_prefix,
)

if TYPE_CHECKING:
    from epicor_mcp.index.service_index import ServiceIndex

# ---------------------------------------------------------------------------
# Business-synonym map  (resolution order)
# ---------------------------------------------------------------------------
# Ordered most-specific first so "open po" beats "po" and "open order" beats
# "order". Each entry: (phrases, service, entity_set, filter_hint).
# ``filter_hint`` is an OData fragment the read tool can AND into ``where`` when
# the user's phrasing implies it (e.g. "open" POs => OpenOrder eq true).
_SYNONYMS: list[tuple[tuple[str, ...], str, str, str]] = [
    # PO SUGGESTIONS must precede every plain-PO entry: "po suggestions"
    # contains "po", and landing it on POHeader silently answers a
    # suggestions question with purchase orders. "sugpodtl" catches the bare table name
    # via the de-spaced match.
    # PO CHANGE suggestions (reschedule/cancel/qty change on EXISTING POs) live
    # on a DIFFERENT BO than NEW buy suggestions and must precede the plain-PO
    # entry too — "PO change suggestions" otherwise word-matches "po" and lands
    # on POHeader, silently returning purchase orders. Primary routing is the
    # _posugg recognizer in read.py; this is the
    # resolver-path safety net.
    (("po change suggestion", "po change suggestions",
      "purchase order change suggestion", "purchase order change suggestions",
      "po chg suggestion", "po change sugg", "sug po chg", "sugpochg"),
     "Erp.BO.POSuggChgSvc", "SugPOChg", ""),
    (("po suggestion", "po suggestions", "purchase order suggestion",
      "purchase order suggestions", "new po suggestion", "new po suggestions",
      "buy suggestion", "buy suggestions", "po sugg", "sug po dtl", "sugpodtl"),
     "Erp.BO.POSuggSvc", "SugPoDtl", ""),
    (("po line", "po lines", "purchase order line", "purchase order lines",
      "po detail", "po details"),
     "Erp.BO.POSvc", "PODetail", ""),
    (("open po", "open pos", "open purchase order", "open purchase orders"),
     "Erp.BO.POSvc", "POHeader", "OpenOrder eq true"),
    (("purchase order", "purchase orders", "po header", "pos", "po"),
     "Erp.BO.POSvc", "POHeader", ""),
    (("open order", "open orders", "open sales order", "open sales orders"),
     "Erp.BO.SalesOrderSvc", "OrderHed", "OpenOrder eq true"),
    (("sales order", "sales orders", "order header", "order"),
     "Erp.BO.SalesOrderSvc", "OrderHed", ""),
    # "vendor invoice"/"supplier invoice" are AP (money we owe) — they must be
    # listed here, BEFORE the generic ("invoice","invoices") entry, or that
    # generic entry word-matches "invoices" first and silently lands the ask on
    # the AR (customer) ledger — plausible-looking wrong-ledger data.
    (("ap invoice", "ap invoices", "accounts payable invoice", "payable invoice",
      "vendor invoice", "vendor invoices", "supplier invoice", "supplier invoices"),
     "Erp.BO.APInvoiceSvc", "APInvHed", ""),
    (("ar invoice", "ar invoices", "accounts receivable invoice", "receivable invoice"),
     "Erp.BO.ARInvoiceSvc", "InvcHead", ""),
    # Generic "invoice" AFTER the ap/ar entries so the specific phrasing wins.
    (("invoice", "invoices"),
     "Erp.BO.ARInvoiceSvc", "InvcHead", ""),
    (("quote", "quotes", "quote header"),
     "Erp.BO.QuoteSvc", "QuoteHed", ""),
    # Contact CHILD tables must precede their greedy parent ("vendor"/"customer")
    # entries: "vendor contacts" contains the whole word "vendor", so without
    # these the parent synonym word-matches first and silently answers a
    # contacts question with the vendor HEADER row instead of its contacts.
    # VendCnt/CustCnt are keyed by VendorNum/CustNum, not the
    # name/ID — read.py resolves a parent-name `where` to that number.
    (("vendor contact", "vendor contacts", "supplier contact",
      "supplier contacts", "vendor contact list", "vend contact"),
     "Erp.BO.VendorSvc", "VendCnt", ""),
    (("customer contact", "customer contacts", "client contact",
      "client contacts", "customer contact list"),
     "Erp.BO.CustCntSvc", "CustCnt", ""),
    (("vendor", "vendors", "supplier", "suppliers"),
     "Erp.BO.VendorSvc", "Vendors", ""),
    (("part", "parts", "item", "items", "material"),
     "Erp.BO.PartSvc", "Part", ""),
    (("customer", "customers", "client", "clients"),
     "Erp.BO.CustomerSvc", "Customers", ""),
    # Production yield / scrap live on the OPERATION table (JobOper): its
    # QtyCompleted is the good count and ScrapQty/ActScrapQty the scrap.
    # JobHead/JobMtl/JobAsmbl/JobReceipt do not carry the reported-scrap column.
    # Route "yield"/"scrap" straight to JobOper so good-vs-scrap is one resolve
    # away. (The "trend yield over N months" phrasing is intercepted earlier by
    # the yield-trend recognizer in read.py, which computes the ratio by month.)
    (("production yield", "yield", "scrap", "scrap rate", "scrapped",
      "scrap qty", "scrap quantity", "good vs scrap", "completed vs scrapped"),
     "Erp.BO.JobEntrySvc", "JobOper", ""),
    (("open job", "open jobs", "open work order", "open work orders"),
     "Erp.BO.JobEntrySvc", "JobHead", "JobClosed eq false"),
    (("job", "jobs", "work order", "work orders", "job header"),
     "Erp.BO.JobEntrySvc", "JobHead", ""),
    # Labor detail must land on the SEARCH service: Erp.BO.LaborSvc is an
    # obsolete BO whose direct reads fail ("This BO is obsolete, please use
    # Labor service" / neither OData nor GetRows).
    (("labordtl", "labor dtl", "labor detail", "labor details",
      "labor record", "labor records", "labor entry", "labor entries",
      "labor transaction", "labor transactions", "clocking", "clockings",
      "labor"),
     "Erp.BO.LaborDtlSearchSvc", "LaborDtl", ""),
]


def _norm(s: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _phrase_matches(phrase: str, norm_spaced: str) -> bool:
    """Match a synonym *phrase* against a normalized target on WORD boundaries.

    Substring matching was the bug: bare ``"order"`` matched inside
    ``"purchaseorder"`` and stole the resolution to Sales Orders. We require a
    whole-word/whole-phrase hit; multi-word phrases additionally match their
    de-spaced (concatenated) form so ``"PurchaseOrder"`` still resolves via the
    ``"purchase order"`` synonym.
    """
    if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", norm_spaced):
        return True
    if " " in phrase and phrase.replace(" ", "") in norm_spaced.replace(" ", ""):
        return True
    return False


def _collapse_initialisms(norm_spaced: str) -> str:
    """Join runs of consecutive single-letter tokens: ``"p o 10001"`` -> ``"po 10001"``.

    ``_norm`` turns ``"P.O. 10001"`` into ``"p o 10001"``, which defeats the
    word-boundary synonym match for ``"po"``. Collapsing dotted initialisms back
    into one token lets the ordinary synonym table catch them. Runs of length 1
    are left untouched (``"what s on"`` stays ``"what s on"``).
    """
    tokens = norm_spaced.split()
    out: list[str] = []
    run: list[str] = []
    for tok in tokens:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0])
            run = []
        out.append(tok)
    if run:
        out.append("".join(run) if len(run) > 1 else run[0])
    return " ".join(out)


def _is_helper_service(service: str, entity_set: str) -> bool:
    """True for search/list *helper* services that should rank below real BOs.

    ``Erp.BO.PODetailSearchSvc/List`` is a lookup helper — its ``List`` call
    hard-fails for normal reads, and it should never outrank ``Erp.BO.POSvc``
    when both match a fuzzy target.
    """
    s = (service or "").lower()
    if s.endswith("searchsvc") or s.endswith("listsvc"):
        return True
    if (entity_set or "").strip().lower() == "list":
        return True
    return False


def _entity_to_service() -> dict[str, str]:
    """Map bare entity-set name (lowercased) -> service, from curated data."""
    out: dict[str, str] = {}
    # TOP_SERVICES lists the common entity sets per service.
    for service, entities in TOP_SERVICES:
        for ent in entities:
            out.setdefault(ent.lower(), service)
    # FIELD_OVERRIDES keys are the authoritative (service, entity_set) pairs.
    for (service, ent) in FIELD_OVERRIDES:
        out.setdefault(ent.lower(), service)
    return out


_ENTITY_TO_SERVICE = _entity_to_service()

# Preferred default entity set per service (first listed in TOP_SERVICES).
_SERVICE_DEFAULT_ENTITY = {svc: ents[0] for svc, ents in TOP_SERVICES if ents}


# Entity-set names that are lookup stubs, not tables. "List" is an entity set
# on hundreds of services and carries ZERO fields, and get_entity_sets returns
# ALPHABETICAL order — so it won the untargeted `sets[0]` default almost every
# time (Erp.BO.PartTranSvc -> "List", though the service exposes PartTran).
_ENTITY_LOOKUP_BLOCKLIST = frozenset({"list", "row", "rows", "table", "data"})


def _default_entity(index: "ServiceIndex", service: str) -> str:
    """Pick a sensible default entity set for *service*."""
    if service in _SERVICE_DEFAULT_ENTITY:
        return _SERVICE_DEFAULT_ENTITY[service]
    try:
        sets = index.get_entity_sets(service) or []
    except Exception:
        sets = []
    sets = [s for s in sets if s.lower() not in _ENTITY_LOOKUP_BLOCKLIST] or sets
    return sets[0] if sets else ""


def _pick_entity_host(entity: str, hosts: list[dict]) -> str:
    """The one service that owns *entity*, or "" when genuinely contested.

    Tiers, first hit wins; each must yield EXACTLY ONE service:
      T1 sole host            — most entity names; not ambiguity at all
      T2 canonical owner      — a host named ``Erp.BO.<Entity>Svc``
      T3 sole non-helper host — one real BO alongside N *SearchSvc/*ListSvc
      T4 sole SearchSvc twin  — a host named ``Erp.BO.<Entity>SearchSvc``
    Anything else returns "" so the caller keeps the INV-1 error.
    """
    ids = [h["service_id"] for h in hosts]
    if len(ids) == 1:
        return ids[0]
    canon = [s for s in ids
             if s.rsplit(".", 1)[-1].lower() == entity.lower() + "svc"]
    if len(canon) == 1:
        return canon[0]
    real = [s for s in ids if not _is_helper_service(s, "")]
    if len(real) == 1:
        return real[0]
    # T4 — LAST on purpose. It runs ONLY where this function already returned
    # "", so it is strictly additive: it can turn an `ambiguous_target`
    # dead-end into a resolution but can never change a T1/T2/T3 pick.
    # Reached when several REAL BOs host the entity and none is name-canonical
    # (APInvMsc: APInvoiceSvc + ContainerTrackingSvc + APInvMscSearchSvc, with
    # no Erp.BO.APInvMscSvc — 2 of the 4 post-restart ambiguous_target errors).
    # For a READ the ``<Entity>SearchSvc`` twin is the right target: same
    # columns, filterable OData — the same reasoning as read.py's
    # `_search_service_for` fast routing. This is NOT the reverted blind target
    # promotion: the entity the caller NAMED is kept and the choice is made
    # only among THAT entity's own hosts, by name derivation, never similarity.
    # The twin must be the named entity's own (`<Entity>SearchSvc`); a lone
    # unrelated *SearchSvc host would be a guess and still returns "".
    twin = [s for s in ids
            if s.rsplit(".", 1)[-1].lower() == entity.lower() + "searchsvc"]
    if len(twin) == 1:
        return twin[0]
    return ""


def resolve_target(index: "ServiceIndex", raw: str) -> dict:
    """Resolve a business term / entity / service-path to a query target.

    Resolution order:
        explicit ``Service/Entity`` -> inline-schema entity map ->
        business-synonym map -> service_index FTS fallback.

    Returns a dict::

        {
          "service":     "Erp.BO.POSvc" | "",
          "entity_set":  "POHeader" | "",
          "candidates":  [ {"target","service","entity_set","why"}, ... ],
          "filter_hint": "OpenOrder eq true" | "",
        }

    On a confident hit ``candidates`` is empty. When not confidently resolved,
    ``service``/``entity_set`` are blank and ``candidates`` carries the top
    options with the exact ``target`` string to re-call (INV-1 inline
    correction).
    """
    result = {"service": "", "entity_set": "", "candidates": [], "filter_hint": ""}
    if not raw or not raw.strip():
        return result
    raw = raw.strip()
    norm = raw.lower()
    norm_spaced = _norm(raw)

    # 1) Explicit "Service/Entity" (service id contains a dot: Erp.BO.POSvc).
    if "/" in raw:
        left, right = raw.split("/", 1)
        left, right = left.strip(), right.strip()
        if "." in left and left.lower().endswith("svc"):
            result["service"] = left
            result["entity_set"] = right or _default_entity(index, left)
            return result

    # A bare dotted service id with no slash — pick a default entity.
    if "." in raw and raw.lower().endswith("svc") and "/" not in raw:
        result["service"] = raw
        result["entity_set"] = _default_entity(index, raw)
        return result

    # 2) Inline-schema entity map: a bare entity-set name ("OrderHed").
    if norm in _ENTITY_TO_SERVICE:
        service = _ENTITY_TO_SERVICE[norm]
        result["service"] = service
        # Preserve the real casing of the requested entity.
        result["entity_set"] = next(
            (e for _s, ents in TOP_SERVICES for e in ents if e.lower() == norm),
            raw,
        )
        return result

    # 3) Business-synonym map — most specific phrase first. Also match a
    #    variant with dotted initialisms collapsed ("p.o." -> "po").
    norm_collapsed = _collapse_initialisms(norm_spaced)
    for phrases, service, entity_set, hint in _SYNONYMS:
        for phrase in phrases:
            if _phrase_matches(phrase, norm_spaced) or (
                norm_collapsed != norm_spaced
                and _phrase_matches(phrase, norm_collapsed)
            ):
                result["service"] = service
                result["entity_set"] = entity_set
                result["filter_hint"] = hint
                return result

    # 3.5) Exact entity-set NAME in the index. This is NOT fuzzy ambiguity:
    #      one real table with several hosts is a HOST choice the engine makes
    #      deterministically (INV-2), not a similarity guess. Genuine
    #      multi-owner contention still falls through to the candidate error.
    #
    #      ORDER IS LOAD-BEARING — this MUST stay below the synonym table and
    #      the curated _ENTITY_TO_SERVICE map. LaborDtl has SIX hosts and the
    #      canonical-name rule would pick Erp.BO.LaborDtlSvc, but the synonym
    #      above requires LaborDtlSearchSvc (Erp.BO.LaborSvc is obsolete);
    #      SugPOChg and JobOper are pinned by the same reasoning.
    if ("/" not in raw and " " not in norm_spaced and len(raw) >= 3
            and norm not in _ENTITY_LOOKUP_BLOCKLIST):
        try:
            hosts = index.services_for_entity(raw) or []
        except Exception:  # noqa: BLE001 — fail open to the FTS fallback
            hosts = []
        if hosts:
            real_name = hosts[0]["entity_set_name"]  # index casing wins
            picked = _pick_entity_host(real_name, hosts)
            if picked:
                result["service"] = picked
                result["entity_set"] = real_name
                return result
            # Contested. Real ambiguity — but every candidate keeps the entity
            # the caller NAMED. Handing back a DIFFERENT table (PartWhse ->
            # Erp.BO.PartSvc/Part) is a silent wrong guess smuggled in through
            # the error envelope; the model correctly ignored it and guessed.
            cands = [{
                "target": f'{h["service_id"]}/{h["entity_set_name"]}',
                "service": h["service_id"],
                "entity_set": h["entity_set_name"],
                "why": f'hosts {h["entity_set_name"]}',
            } for h in hosts]
            cands.sort(key=lambda c: _is_helper_service(
                c["service"], c["entity_set"]))
            result["candidates"] = cands
            result["retry_with"] = {"target": cands[0]["target"]}
            return result

    # 4) service_index FTS fallback.
    try:
        hits = index.search_services(raw, limit=5) or []
    except Exception:
        hits = []
    if len(hits) == 1:
        service = hits[0]["service_id"]
        result["service"] = service
        result["entity_set"] = _default_entity(index, service)
        return result
    if hits:
        candidates = []
        for h in hits:
            service = h["service_id"]
            # Keep the entity the caller NAMED when this service exposes it —
            # `_default_entity` would otherwise throw it away and point at a
            # different table (PartWhse -> Erp.BO.PartSvc/Part).
            ent = ""
            try:
                available = index.get_entity_sets(service) or []
                ent = next((e for e in available if e.lower() == norm), "")
            except Exception:
                ent = ""
            ent = ent or _default_entity(index, service)
            target = f"{service}/{ent}" if ent else service
            candidates.append({
                "target": target,
                "service": service,
                "entity_set": ent,
                "why": h.get("description") or h.get("short_name") or "",
            })
        # Rank primary business-object services (Erp.BO.<X>Svc) above
        # search/list helpers — a *SearchSvc/List candidate must never lead
        # when a real BO service also matched. Stable sort keeps FTS order
        # within each tier.
        candidates.sort(
            key=lambda c: _is_helper_service(c["service"], c["entity_set"])
        )
        result["candidates"] = candidates
        # Concrete next call for weak models (INV-1): retry with the
        # top-ranked candidate instead of guessing among the list.
        if candidates[0].get("target"):
            result["retry_with"] = {"target": candidates[0]["target"]}
    return result


# Below this width the median entity (18 columns) is untouched; above it a
# default read is unusable inline. See the cap in ``resolve_fields``.
_CAP_THRESHOLD = 40


def resolve_fields(
    index: "ServiceIndex",
    service: str,
    entity_set: str,
    raw_fields: str,
) -> dict:
    """Resolve/validate a comma-separated field list against the real schema.

    * Blank ``raw_fields`` => curated default set from
      ``_inline_schema.FIELD_OVERRIDES`` (``default_used=True``); empty when no
      curated set exists (the engine then applies its own heuristic).
    * Otherwise each requested field is matched case-insensitively to a real
      column; misses go into ``unknown`` with close-match ``suggestions``.

    Returns::

        {
          "fields":        ["PONum", ...],   # resolved real column names
          "unknown":       ["badcol", ...],  # requested names with no match
          "valid_columns": ["PONum", ...],   # every real column on the entity
          "default_used":  bool,
          "suggestions":   {"badcol": ["PONum", ...]},
        }
    """
    try:
        col_rows = index.get_fields(service, entity_set) or []
    except Exception:
        col_rows = []
    valid_columns = [r["field_name"] for r in col_rows]
    valid_lower = {c.lower(): c for c in valid_columns}

    if not raw_fields or not raw_fields.strip():
        defaults = FIELD_OVERRIDES.get((service, entity_set))
        if defaults is None:
            # The SearchSvc / alias-service key miss (see CURATED_BY_ENTITY).
            defaults = CURATED_BY_ENTITY.get(entity_set, [])
        # Only keep curated defaults that actually exist on the entity when we
        # have a column list to check against.
        if valid_columns:
            defaults = [c for c in defaults if c.lower() in valid_lower]
        out = {
            "fields": list(defaults),
            "unknown": [],
            "valid_columns": valid_columns,
            "default_used": True,
            "suggestions": {},
        }
        if not defaults and len(valid_columns) > _CAP_THRESHOLD:









            picked = _select_fields_heuristic(col_rows, _table_prefix(entity_set))
            names = [f["field_name"] for f in picked]
            if names:
                out["fields"] = names
                out["capped"] = True
                out["total_columns"] = len(valid_columns)
        return out

    requested = [f.strip() for f in raw_fields.split(",") if f.strip()]
    fields: list[str] = []
    unknown: list[str] = []
    suggestions: dict[str, list[str]] = {}
    for name in requested:
        real = valid_lower.get(name.lower())
        if real is not None:
            fields.append(real)
        elif not valid_columns:
            # No schema to validate against — pass the name through unchanged.
            fields.append(name)
        else:
            unknown.append(name)
            close = difflib.get_close_matches(name, valid_columns, n=5, cutoff=0.5)
            if not close:
                nlow = name.lower()
                close = [c for c in valid_columns if nlow in c.lower()][:5]
            suggestions[name] = close

    return {
        "fields": fields,
        "unknown": unknown,
        "valid_columns": valid_columns,
        "default_used": False,
        "suggestions": suggestions,
    }


def column_help(
    service: str,
    entity_set: str,
    valid_columns: list[str],
    unknown,
    cap: int = 30,
    index: "ServiceIndex | None" = None,
) -> dict:
    """Build a *tight, relevant* column hint for an unknown-column envelope.

    Dumping all ~200 columns of a header table buries the right one and defeats
    INV-1 — the model can't find ``OpenOrder`` in a wall of names. Instead return
    the curated default set for the entity (which carries the canonical,
    semantically-important columns) plus close matches for each bad name, and a
    ``did_you_mean`` map. ``total_columns`` tells the model more exist.
    """
    valid_lower = {c.lower(): c for c in valid_columns}
    cols: list[str] = []
    for c in FIELD_OVERRIDES.get((service, entity_set), []):
        real = valid_lower.get(c.lower())
        if real and real not in cols:
            cols.append(real)
    did: dict[str, list[str]] = {}
    for u in (unknown or []):
        u = str(u)
        close = difflib.get_close_matches(u, valid_columns, n=4, cutoff=0.5)
        if not close:
            ul = u.lower()
            close = [c for c in valid_columns if ul in c.lower()][:4]
        did[u] = close
        for c in close:
            if c not in cols:
                cols.append(c)
    out = {
        "columns": cols[:cap],
        "did_you_mean": did,
        "total_columns": len(valid_columns),
    }
    # "Not here" is only half the answer. OnHandQty IS a real column — it just
    # lives on PartWhse, not Part — and because column_help only ever searched
    # the RESOLVED entity it could never say so. The model was told
    # `did_you_mean: HasOnHandQty` (a boolean) and believed it.
    if index is not None:
        lives_on: dict[str, list[str]] = {}
        for u in (unknown or []):
            u = str(u)
            if u.lower() in valid_lower:
                continue
            try:
                owners = index.find_field_owners(u) or []
            except Exception:  # noqa: BLE001 — a hint is never worth a failure
                owners = []
            targets = [f'{o["service_id"]}/{o["entity_set_name"]}' for o in owners
                       if o["service_id"] != service or o["entity_set_name"] != entity_set]
            if targets:
                lives_on[u] = targets
        if lives_on:
            out["column_lives_on"] = lives_on
    return out


def coerce_csv(value) -> str:
    """Normalise a list / JSON-array-string / scalar to a comma-separated string.

    Weak clients emit ``tables=['Erp.Part','Erp.PartTran']`` (a real list, which
    fails pydantic validation against a ``str`` annotation and leaks the raw
    ValidationError text) and ``tables='["Erp.Part","Erp.PartTran"]'`` (a JSON
    STRING, which passes validation and then gets comma-split into the garbage
    terms ``'["Erp.Part"'`` / ``'"Erp.PartTran"]'`` -> a misleading
    ``unknown_tables``). The second is the more dangerous of the two: it is a
    silent wrong guess wearing an unrelated error code.

    A plain string is returned UNCHANGED, so the BAQ aggregate grammar
    (``count(PONum)``, ``sum(x) as Y``, ``[Alias].[Col]``, ``month(OrderDate)``)
    is never reinterpreted.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                return value
            if isinstance(parsed, list):
                return ", ".join(
                    str(v).strip() for v in parsed if str(v).strip())
        return value
    return str(value)


def error_envelope(
    reason: str,
    message: str,
    *,
    valid: dict | None = None,
    retry_with: dict | None = None,
    candidates: list | None = None,
    detail: dict | None = None,
) -> dict:
    """Build the uniform INV-1 self-correcting error envelope.

    Shape (structured error envelope)::

        {
          "error":      "<short_reason_code>",
          "message":    "<one line: what to do next>",
          "valid":      { "columns": [...], "entities": [...], ... },   # optional
          "retry_with": { "target": "...", "fields": "...", ... },       # optional
          "candidates": [ {...}, ... ],                                   # optional
        }

    Only include the keys that carry information; never return a bare reason
    code, which leaves the caller guessing in a loop.
    """
    env: dict = {"error": reason, "message": message}
    if detail is not None:
        env["detail"] = detail
    if valid is not None:
        env["valid"] = valid
    if retry_with is None and candidates:
        # Ambiguity resolution aid (INV-1): give weak models one concrete
        # next call — the top-ranked candidate — instead of a guess among
        # ``candidates``. Callers that pass an explicit retry_with win.
        top = candidates[0]
        if isinstance(top, dict) and top.get("target"):
            retry_with = {"target": top["target"]}
    if retry_with is not None:
        env["retry_with"] = retry_with
    if candidates is not None:
        env["candidates"] = candidates
    return env


# ---------------------------------------------------------------------------
# unknown_columns: attribute the failure to a SIDE  (INV-1)
# ---------------------------------------------------------------------------
# A request can contain both ``where`` and ``fields``. An unknown-column error
# must identify which argument contains the bad name, so the caller can keep
# a valid filter while correcting an invalid projection.
#
# This is an ERROR-QUALITY rule, NOT a fail-soft one. Nothing is dropped and
# nothing is guessed — unknown fields are never silently dropped, because
# a silent wrong guess makes the model hunt. The error simply says which side
# broke, which side validated clean, and hands back a runnable retry built from
# the RESOLVED names ("hard rejects echo what DID map", the rule `_argguard`
# already follows).

_ARG_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_ARG_LITERAL_RE = re.compile(
    r"'[^']*'|\"[^\"]*\"|\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?")

# Operators / functions / keywords that appear inside a `where`, `order_by` or
# an aggregate spec and are NOT column references. Used ONLY to make the
# "N of M" count honest — never to decide attribution, because Epicor really
# does ship columns called ``Total``, ``Length`` and ``Added``, and dropping
# one of those from the token list would hand its argument a false clean bill.
_ARG_NOISE = frozenset("""
and or not eq ne gt ge lt le like in between is null true false asc desc
contains startswith endswith substringof indexof tolower toupper trim length
concat year month quarter day date now add sub mul div mod as sum avg min max
count cnt total average mean minimum maximum distinct
""".split())


def _argument_columns(spec) -> list[str]:
    """Identifier tokens in *spec*, with quoted literals and dates masked out.

    Masking the literals FIRST is load-bearing: ``GroupID = 'GROUP001'`` must
    not contribute the VALUE as a column name, and the unquoted OData date form
    ``2025-07-20T00:00:00Z`` must not contribute ``T00``. Order and the
    caller's own casing are preserved — the message quotes each name back
    exactly as it was written.
    """
    if not spec:
        return []
    masked = _ARG_LITERAL_RE.sub("''", str(spec))
    out: list[str] = []
    seen: set[str] = set()
    for tok in _ARG_TOKEN_RE.findall(masked):
        low = tok.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(tok)
    return out


def attribute_unknown_columns(unknown, arguments: dict) -> dict:
    """Split *unknown* column names across the caller arguments that mention them.

    ``arguments`` is an ORDERED mapping of caller-facing argument name -> the
    string that was ACTUALLY validated for it. Only pass arguments that were
    validated: declaring an unchecked argument "clean" is the same class of lie
    as never saying which side broke.

    Returns ``{"by_argument", "clean", "unplaced", "totals"}``.
    """
    names: list[str] = []
    for u in (unknown or []):
        u = str(u)
        if u and u not in names:
            names.append(u)
    lower = {u.lower() for u in names}

    by_argument: dict[str, list[str]] = {}
    totals: dict[str, int] = {}
    hit: set[str] = set()
    for arg, spec in (arguments or {}).items():
        if not str(spec or "").strip():
            continue
        cols = _argument_columns(spec)
        # Attribution keys on EVERY token; only the count drops the operators.
        named = [c for c in cols
                 if c.lower() not in _ARG_NOISE or c.lower() in lower]
        totals[arg] = len(named) or len(cols)
        bad = [c for c in cols if c.lower() in lower]
        if bad:
            by_argument[arg] = bad
            hit.update(c.lower() for c in bad)
    return {
        "by_argument": by_argument,
        "clean": [a for a in totals if a not in by_argument],
        "unplaced": [u for u in names if u.lower() not in hit],
        "totals": totals,
    }


def unknown_columns_envelope(
    *,
    target: str,
    unknown,
    arguments: dict,
    valid: dict | None = None,
    good_fields: list[str] | None = None,
    retry_with: dict | None = None,
    detail: dict | None = None,
    lead: str = "",
    tail: str = "",
    reason: str = "unknown_columns",
) -> dict:
    """The uniform side-attributed ``unknown_columns`` envelope.

    ``good_fields`` are the RESOLVED survivors of the caller's ``fields`` — the
    ones that DID map. Building ``retry_with`` from the raw input instead is a
    known bug shape (``_argguard`` shipped it once), so the caller passes the
    validated list and this builder never re-derives it from the raw string.

    ``lead`` replaces the "don't exist on <target>" clause for callers whose
    key space isn't one entity's columns (a two-table join, a rollup).
    """
    attr = attribute_unknown_columns(unknown, arguments)
    clause = lead or f"don't exist on {target}"

    parts: list[str] = []
    for arg, bad in attr["by_argument"].items():
        total = attr["totals"].get(arg) or len(bad)
        parts.append(
            f"{len(bad)} of {total} `{arg}` name(s) {clause}: "
            + ", ".join(bad))
    if attr["unplaced"]:
        parts.append(f"Column(s) {clause}: " + ", ".join(attr["unplaced"]))
    if attr["clean"]:
        one = len(attr["clean"]) == 1
        listed = " and ".join(f"`{a}`" for a in attr["clean"])
        parts.append(
            f"Your {listed} {'is' if one else 'are'} VALID — "
            f"{'that side is' if one else 'those sides are'} NOT the problem; "
            f"re-send {'it' if one else 'them'} unchanged (retry_with carries "
            f"{'it' if one else 'them'})")
    parts.append(tail or (
        "Fix ONLY the argument named above and retry (see "
        "valid.did_you_mean / valid.columns)"))
    message = ". ".join(p.rstrip(". ") for p in parts if p) + "."

    rt = dict(retry_with or {})
    if "fields" not in rt:
        if "fields" in attr["by_argument"]:
            # The survivors, so the model has a runnable next call instead of
            # having to reconstruct its own projection from the error.
            if good_fields:
                rt["fields"] = ", ".join(good_fields)
        elif str(arguments.get("fields") or "").strip():
            rt["fields"] = arguments["fields"]
    for arg in attr["clean"]:
        if arg != "fields" and arg not in rt:
            rt[arg] = arguments[arg]

    env = error_envelope(reason, message, valid=valid,
                         retry_with=rt or None, detail=detail)
    by = dict(attr["by_argument"])
    if attr["unplaced"]:
        by["unplaced"] = attr["unplaced"]
    if by:
        env["unknown_by_argument"] = by
    if attr["clean"]:
        env["validated_clean"] = attr["clean"]
    return env


# ---------------------------------------------------------------------------
# Epicor-exception classifier (INV-1 on the execution path)
# ---------------------------------------------------------------------------
# An ``EpicorError`` on the OData read path must not fall into
# ``epicor_read``'s blanket ``except Exception``, which returns a FIXED
# generic string and discards ``exc.message`` — the one thing in the whole
# response the model can act on. Without this, the same target + same where
# returns a bare ``read_failed`` on the plain path and the real "incompatible
# types" text on the paged path. Every execution failure routes through here.

_UNKNOWN_PROP_RE = re.compile(r"could not find a property named '([^']+)'", re.I)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def epicor_error_envelope(
    exc,
    *,
    service: str = "",
    entity_set: str = "",
    valid_columns: list | None = None,
    odata_filter: str = "",
    fields: str = "",
    where: str = "",
    attempted: tuple = ("odata", "getrows"),
    alternatives: list | None = None,
    index: "ServiceIndex | None" = None,
) -> dict:
    """Classify an ``EpicorError`` into the INV-1 envelope, cause intact.

    ``detail`` ALWAYS carries the server's own message and status — even on the
    unclassified fallback, so a bare reason code can never ship again. Only
    ``.message``/``.status_code`` are served: ``exc.details`` can carry request
    headers, and the traceback stays in the log.
    """
    raw_msg = getattr(exc, "message", None) or str(exc)
    msg = raw_msg.lower()
    detail = {
        "status": getattr(exc, "status_code", None),
        "message": _clip(raw_msg, 600),
    }
    valid_columns = valid_columns or []
    path = " + ".join(attempted) if attempted else "the read"

    # 1) Epicor named the bad column — hand back the real ones. Same shape the
    #    local pre-flight check emits, so the model sees ONE recovery path
    #    whether the miss is caught here or upstream.
    m = _UNKNOWN_PROP_RE.search(raw_msg)
    if m:
        bad = m.group(1)
        # Epicor names ONE column and says nothing about which argument it came
        # from — the same which-side ambiguity the pre-flight envelope had.
        # `where`/`fields` are already threaded in for the date rewrite, so the
        # attribution is free.
        return unknown_columns_envelope(
            target=f"{service}/{entity_set}",
            unknown=[bad],
            arguments={"where": where, "fields": fields},
            # `index` threads the column_lives_on redirect through to a
            # SERVER-reported unknown column (OnHandQty is real — it lives on
            # PartWhse, not Part). Without it that redirect could only ever
            # fire on locally pre-validated names.
            valid=column_help(service, entity_set, valid_columns, [bad],
                              index=index),
            retry_with={"target": f"{service}/{entity_set}"},
            detail=detail,
            lead=f"don't exist on {service}/{entity_set} "
                 "(Epicor rejected the read)",
            tail="Retry with a valid column name (see valid.did_you_mean / "
                 "valid.columns)",
        )

    # 2) A date literal reached a date column as a string. The date coercion in
    #    query.py normally prevents this; this is the safety net for an aliased
    #    or computed column the type set didn't cover. Hand back the corrected
    #    `where` rather than silently retrying it (fail-soft lesson: a silent
    #    guess makes the model hunt; a precise error converges in one hop).
    if "incompatible types" in msg and "edm.datetimeoffset" in msg:
        return error_envelope(
            "filter_type_mismatch",
            "A date column was compared against a quoted string. Epicor wants "
            "an UNQUOTED ISO-8601 literal with Z "
            "(e.g. `TranDate ge 2025-07-20T00:00:00Z`). Re-call with "
            "retry_with.where.",
            retry_with={"where": _rewrite_iso_dates(where or odata_filter)},
            detail=detail,
        )

    # 3) Epicor's generic apology — no model-fixable fact in it at all, so the
    #    only useful payload is a different route.
    from epicor_mcp.tools import query as _query  # local: avoids a cycle
    if _query._is_generic_epicor_apology(raw_msg):
        return error_envelope(
            "upstream_error",
            f"{service}/{entity_set} returned Epicor's generic internal error "
            f"via {path}. This is a routing problem, not your criteria — try "
            "an alternative target (valid.alternatives) or epicor_baq.",
            valid={"alternatives": alternatives or []},
            detail=detail,
        )

    # 4) Filter/syntax family. Echo what was ACTUALLY sent (post-translation)
    #    next to what the caller wrote — the two differ, and that difference is
    #    usually the fix. Deliberately asserts nothing about which constructs
    #    are supported: arithmetic in a $filter IS supported (mul/div/add/sub),
    #    so a blanket "expressions aren't allowed" message would be a lie.
    if any(k in msg for k in
           ("syntax error", "unrecognized", "invalid token", "binary operator")):
        return error_envelope(
            "filter_rejected",
            f"Epicor rejected the filter sent to {service}/{entity_set}. See "
            "detail.message for its exact complaint and "
            "valid.attempted_filter for what was actually sent.",
            valid={"attempted_filter": odata_filter, "your_where": where},
            detail=detail,
        )

    return error_envelope(
        "read_failed",
        f"Read against {service}/{entity_set} failed via {path}. "
        "See detail.message for Epicor's own explanation.",
        detail=detail,
    )


def _rewrite_iso_dates(where: str) -> str:
    """Rewrite every anchored quoted ISO date in *where* to the unquoted Z-form.

    Used only to populate ``retry_with.where`` after Epicor itself named the
    type mismatch — the server confirmed the column is a date, so this is a
    repair, not a guess.
    """
    if not where:
        return where
    from epicor_mcp.tools.query import _to_odata_datetime

    def _sub(m: re.Match) -> str:
        iso = _to_odata_datetime(m.group(0))
        return iso or m.group(0)

    return re.sub(r"'(\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+Z?)?)'", _sub, where)
