"""Analytical 'top N by measure' recipes for the business-object read path.

Most "top / biggest / total X by Y" questions are single-entity group-bys once
you know WHERE the measure lives — sales sit on ``InvcDtl.ExtPrice``, on-hand on
``PartWhse.OnHandQty`` (``Part`` only has a boolean ``HasOnHandQty``!). Local
models don't know that and thrash guessing entities/columns (16-call
non-terminations in testing). This module maps the common analytical intents to
a concrete ``(service, entity, group_by, aggregate)`` recipe so ``epicor_read``
answers them in ONE business-object call — no BAQ, available to every user.

Honest bound: client-side aggregation only sees the fetched page, so results are
scoped (the ``order_hint`` biases the fetch toward the measure / most-recent, and
the tool labels the scope). A complete all-time ranking still needs a BAQ — but
that is a gated last resort, not the common path.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

# Each recipe answers "<rank> <subject> by <measure>". ``order_hint`` biases the
# bounded fetch so the true top-N is likely captured within it.
#
# ``date_field`` is the column an explicit timeframe ("in 2025", "Q3 2025")
# filters on; ``None`` means the measure is current-state (on-hand has no
# history — a window earns a note, not a filter). ``scope_noun`` names the
# rows for the windowed scope label; ``measure_name`` is the human name of the
# measure for the "rank by what?" self-correction envelope.
#
# CRITICAL: business-object GetRows only returns rows for a service's PRIMARY
# (header) table — child/detail tables (InvcDtl, APInvDtl, PartWhse) come back
# empty. So a recipe's ``entity`` MUST be a retrievable header table. Measures
# that only live on detail lines (sales BY PART, on-hand BY PART) are flagged
# ``bo_retrievable=False`` — they genuinely need a BAQ, so the tool converges
# with an honest one-call answer instead of thrashing or returning empty.
_RECIPES: list[dict] = [
    {
        "id": "customers_by_sales",
        "subjects": ("customer", "customers", "client", "clients",
                     "account", "accounts"),
        "measures": ("sale", "sales", "sold", "revenue", "spend", "buy",
                     "buying", "invoiced", "biggest", "top", "largest"),
        "service": "Erp.BO.ARInvoiceSvc", "entity": "InvcHead",
        "group_by": "CustNum", "aggregate": "sum(InvoiceAmt) as total_sales",
        "measure_alias": "total_sales", "order_hint": "InvoiceDate desc",
        "date_field": "InvoiceDate", "scope_noun": "AR invoices",
        "measure_name": "sales",
        "scope": "most recent invoices", "bo_retrievable": True,
        "label": "customers ranked by invoiced sales ($)",
    },
    {
        "id": "vendors_by_spend",
        "subjects": ("vendor", "vendors", "supplier", "suppliers"),
        "measures": ("spend", "spent", "purchase", "purchases", "cost",
                     "paid", "payable", "biggest", "top", "most"),
        "service": "Erp.BO.APInvoiceSvc", "entity": "APInvHed",
        "group_by": "VendorNum", "aggregate": "sum(InvoiceAmt) as total_spend",
        "measure_alias": "total_spend", "order_hint": "InvoiceDate desc",
        "date_field": "InvoiceDate", "scope_noun": "AP invoices",
        "measure_name": "spend",
        "scope": "most recent AP invoices", "bo_retrievable": True,
        "label": "vendors ranked by AP spend ($)",
    },
    {
        # Line-level: sales sit on InvcDtl.ExtPrice. BO GetRows can't retrieve a
        # detail table on its own, but query_with_children pulls InvcHead+InvcDtl
        # in one paged call and joins them, so we CAN rank parts by sales without
        # a BAQ. ``via_join`` routes this recipe through that parent/child engine.
        "id": "parts_by_sales",
        "subjects": ("part", "parts", "part number", "part numbers",
                     "item", "items", "sku", "skus"),
        "measures": ("sale", "sales", "sold", "revenue", "booking",
                     "bookings", "invoiced", "dollar", "dollars"),
        "bo_retrievable": True, "via_join": True,
        "service": "Erp.BO.ARInvoiceSvc",
        "parent_entity": "InvcHead", "child_entity": "InvcDtl",
        "group_by": "PartNum", "aggregate": "sum(ExtPrice) as total_sales",
        "measure_alias": "total_sales",
        "date_field": "InvoiceDate", "scope_noun": "invoice lines",
        "measure_name": "sales",
        # Older invoice lines may carry no PartNum; bound the parent
        # page to recent invoices so the join lands on populated, part-tagged
        # lines (and stays within the GetRows latency budget). An explicit
        # timeframe in the query REPLACES this default.
        "recency": {"field": "InvoiceDate", "days": 180},
        "join_page_size": 300, "join_max_pages": 1,
        "scope": "recent invoice lines (last ~180 days, bounded fetch)",
        "label": "parts ranked by invoiced sales ($)",
    },
    {
        # Line-level: on-hand sits on PartWhse.OnHandQty (Part only has a boolean
        # HasOnHandQty). Reached via the Part+PartWhse parent/child join.
        "id": "parts_by_onhand",
        "subjects": ("part", "parts", "item", "items", "inventory",
                     "stock", "sku", "skus"),
        "measures": ("on hand", "on-hand", "onhand", "hand", "stock",
                     "inventory", "quantity", "qty"),
        "bo_retrievable": True, "via_join": True,
        "service": "Erp.BO.PartSvc",
        "parent_entity": "Part", "child_entity": "PartWhse",
        "group_by": "PartNum", "aggregate": "sum(OnHandQty) as on_hand",
        "measure_alias": "on_hand",
        "date_field": None, "scope_noun": "current warehouse on-hand",
        "measure_name": "on-hand quantity",
        "join_page_size": 1000, "join_max_pages": 1,
        "scope": "current warehouse on-hand across a bounded part scan",
        "label": "parts ranked by on-hand quantity",
    },
]

_RANK_WORDS = ("top", "biggest", "largest", "most", "highest", "greatest",
               "leading", "best", "ranked", "rank")
_COUNT_PHRASES = ("how many", "number of", "count of", "how much", "total number")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def _has_word(term: str, norm: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", norm) is not None


# ---------------------------------------------------------------------------
# Time windows — "in 2025", "Q3 2025", "June 2025", "last quarter", "last 90
# days"… Explicit periods outrank relative words: "last year, in 2025" is the
# user correcting the window to 2025, so 2025 wins.
# ---------------------------------------------------------------------------

_YEAR_RE = r"(?<!\d)(20\d\d)(?!\d)"
_MONTHS = {name: i + 1 for i, name in enumerate(
    ("january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"))}
_MONTHS.update({name[:3]: n for name, n in list(_MONTHS.items())})
# Longest names first so "june" isn't consumed as "jun" + trailing junk.
_MONTH_RE = r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b"
_LAST_N_RE = re.compile(r"\b(?:last|past)\s+(\d{1,3})\s+(day|week|month|year)s?\b")


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months_back(d: date, n: int) -> date:
    """Same day-of-month *n* months earlier (clamped to the shorter month)."""
    idx = d.year * 12 + d.month - 1 - n
    y, m = divmod(idx, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _win(start: date, end: date, label: str) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "label": label}


def parse_time_window(query: str) -> dict | None:
    """Extract a timeframe from *query* → ``{"start", "end", "label"}``
    (inclusive YYYY-MM-DD bounds) or ``None``.

    Precedence: year range > quarter+year > month+year > explicit year >
    relative ("last year", "ytd", "last quarter", "this month", "last N
    days/weeks/months/years").
    """
    if not query:
        return None
    q = query.lower()
    today = datetime.now().date()

    # Year range: "between 2020 and 2025" / "2020-2025" / "from 2020 to 2025".
    m = re.search(
        r"(?:between\s+|from\s+)?" + _YEAR_RE
        + r"\s*(?:-|–|—|\bto\b|\bthrough\b|\band\b)\s*" + _YEAR_RE, q)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 <= y2:
            return _win(date(y1, 1, 1), date(y2, 12, 31), f"{y1}-{y2}")

    m = re.search(r"\bq([1-4])[\s,]*(?:of\s+)?" + _YEAR_RE, q)
    if m:
        qtr, year = int(m.group(1)), int(m.group(2))
        return _win(date(year, 3 * qtr - 2, 1), _month_end(year, 3 * qtr),
                    f"Q{qtr} {year}")

    m = re.search(_MONTH_RE + r"\.?[\s,]+(?:of\s+)?" + _YEAR_RE, q)
    if m:
        month, year = _MONTHS[m.group(1)], int(m.group(2))
        return _win(date(year, month, 1), _month_end(year, month),
                    f"{m.group(1).capitalize()} {year}")

    # Bare/explicit year ("in 2025", "for 2025", …) — beats every relative
    # phrase below, so "last year, in 2025" resolves to 2025.
    m = re.search(_YEAR_RE, q)
    if m:
        year = int(m.group(1))
        return _win(date(year, 1, 1), date(year, 12, 31), str(year))

    if re.search(r"\bytd\b|\byear\s+to\s+date\b", q):
        return _win(date(today.year, 1, 1), today, f"YTD {today.year}")
    if re.search(r"\b(?:last|past)\s+year\b", q):
        y = today.year - 1
        return _win(date(y, 1, 1), date(y, 12, 31), f"last year ({y})")
    if re.search(r"\b(?:this|current)\s+year\b", q):
        return _win(date(today.year, 1, 1), date(today.year, 12, 31),
                    f"this year ({today.year})")
    if re.search(r"\b(?:last|past)\s+quarter\b", q):
        qtr, y = (today.month - 1) // 3, today.year  # 0 = prior year's Q4
        if qtr == 0:
            qtr, y = 4, y - 1
        return _win(date(y, 3 * qtr - 2, 1), _month_end(y, 3 * qtr),
                    f"last quarter (Q{qtr} {y})")
    if re.search(r"\b(?:this|current)\s+quarter\b", q):
        qtr = (today.month - 1) // 3 + 1
        return _win(date(today.year, 3 * qtr - 2, 1),
                    _month_end(today.year, 3 * qtr),
                    f"this quarter (Q{qtr} {today.year})")
    if re.search(r"\b(?:last|past)\s+month\b", q):
        first = _months_back(today.replace(day=1), 1)
        return _win(first, _month_end(first.year, first.month),
                    f"last month ({first.strftime('%B %Y')})")
    if re.search(r"\b(?:this|current)\s+month\b", q):
        return _win(today.replace(day=1), _month_end(today.year, today.month),
                    f"this month ({today.strftime('%B %Y')})")
    m = _LAST_N_RE.search(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            start = today - timedelta(days=n)
        elif unit == "week":
            start = today - timedelta(weeks=n)
        elif unit == "month":
            start = _months_back(today, n)
        else:
            start = _months_back(today, 12 * n)
        return _win(start, today, f"last {n} {unit}{'s' if n != 1 else ''}")

    return None


def parse_top_n(query: str, default: int | None = None) -> int | None:
    """Pull an explicit N from 'top 10 …' / 'top ten …'; else *default*."""
    m = re.search(r"\btop\s+(\d{1,4})\b", query.lower())
    if m:
        return int(m.group(1))
    return default


def is_count_query(query: str) -> bool:
    'Internal framework helper retained for compatibility.'
    q = _norm(query)
    return any(p in q for p in _COUNT_PHRASES)


def match_analytical(query: str) -> dict | None:
    """Return a measure recipe if *query* is a 'rank/total <subject> by <measure>'
    intent a single business-object group-by can answer, else ``None``.

    Requires an explicit ranking/total trigger so single-record or time-series
    asks ("YoY sales for customer X") do NOT mis-fire into a top-N rollup,
    AND at least one real measure word — subject + trigger alone must not fire
    ("top parts by profit margin" is not a sales ranking). Rank words that
    double as measures ("biggest customers") live in the recipes' own
    measures lists, so those still match.
    """
    if not query:
        return None
    q = _norm(query)
    trigger = any(_has_word(w, q) for w in _RANK_WORDS) or (
        "total" in q and (" by " in q or " per " in q))
    if not trigger:
        return None
    best, best_score = None, 0
    for r in _RECIPES:
        if not any(_has_word(s, q) for s in r["subjects"]):
            continue
        m_hit = sum(1 for m in r["measures"] if m in q)
        if m_hit < 1:
            continue  # never fire on subject alone — the measure must match
        score = m_hit * 2 + 1
        if score > best_score:
            best, best_score = r, score
    return best


def subjects_without_measure(query: str) -> list[dict]:
    """Recipes whose SUBJECT matches a ranking *query* but whose measure words
    don't — the half-match behind "top parts by profit margin". read.py turns
    this into a 'rank by what?' self-correction envelope listing the measures
    each matching subject supports. Empty when there is no ranking trigger, no
    subject hit, or some recipe matches fully (``match_analytical`` owns that).
    """
    if not query:
        return []
    q = _norm(query)
    trigger = any(_has_word(w, q) for w in _RANK_WORDS) or (
        "total" in q and (" by " in q or " per " in q))
    if not trigger:
        return []
    out: list[dict] = []
    for r in _RECIPES:
        subject = next((s for s in r["subjects"] if _has_word(s, q)), None)
        if subject is None:
            continue
        if any(m in q for m in r["measures"]):
            return []  # a full match exists — the recipe path handles it
        plural = subject if subject.endswith("s") else subject + "s"
        out.append({
            "recipe": r["id"],
            "subject": subject,
            "measure": r["measure_name"],
            "label": r["label"],
            "example": f"top 10 {plural} by {r['measure_name']}",
        })
    return out
