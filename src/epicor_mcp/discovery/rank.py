"""Optional semantic score fusion with deterministic ERP name/type priors.

Metadata-only discovery uses direct substring ranking and does not require
these semantic fusion functions or any learned embedding dependency.
"""

from __future__ import annotations

import re

from epicor_mcp.discovery.text import CANON, split_camel

__all__ = [
    "column_prior_penalty",
    "name_match_score",
    "type_affinity",
    "thin_description",
    "fuse",
    "NUMERIC_SQL_TYPES",
    "DATE_SQL_TYPES",
    "BOOL_SQL_TYPES",
    "TEXT_SQL_TYPES",
]

# --------------------------------------------------------------------------- #
# Canonical-column prior
# --------------------------------------------------------------------------- #
#: Epicor ships each business value several times over: base currency, document
#: currency, three reporting currencies, a display string, a what-if copy. The
#: unqualified question ("extended price") always means the BASE one, but the
#: variants embed near-identically. Penalty applies on a CamelCase TOKEN boundary
#: (so ``InvoiceAmt`` is never mistaken for the ``In`` prefix) and is WAIVED when
#: the query asks for that flavour.
PREFIX_PENALTY: dict[str, tuple[float, tuple[str, ...]]] = {
    "Sys":     (1.00, ()),
    "Rpt1":    (0.55, ("reporting", "rpt")),
    "Rpt2":    (0.55, ("reporting", "rpt")),
    "Rpt3":    (0.55, ("reporting", "rpt")),
    "Dsp":     (0.45, ("display",)),
    "Scr":     (0.45, ("screen",)),
    "Doc":     (0.30, ("document", "doc", "transaction currency")),
    "In":      (0.30, ("inbound", "intrastat")),
    "Base":    (0.25, ("base",)),
    "Orig":    (0.25, ("original", "originally")),
    "Calc":    (0.25, ("calculated",)),
    "Enable":  (0.40, ("enable", "enabled")),
    "Disable": (0.40, ("disable",)),
    "WI":      (0.35, ("what if", "whatif")),
    "EDI":     (0.35, ("edi",)),
    "Config":  (0.25, ("configur",)),
    "Salvage": (0.35, ("salvage",)),
    "Est":     (0.15, ("estimate", "estimated", "planned")),
    "KB":      (0.35, ("kanban",)),
    "TFOrd":   (0.30, ("transfer",)),
}

#: Suffix tokens that mark plumbing rather than data.
SUFFIX_PENALTY: dict[str, float] = {
    "RowID": 1.0, "RevID": 1.0, "RowMod": 1.0, "BitFlag": 0.6, "GUID": 0.6,
}


def column_prior_penalty(query: str, field: str) -> float:
    """0.0 (canonical) .. 1.0 (never the answer)."""
    ql = query.lower()
    toks = split_camel(field)
    if not toks:
        return 0.0
    pen = 0.0
    p = PREFIX_PENALTY.get(toks[0])
    if p and not any(w in ql for w in p[1]):
        pen = max(pen, p[0])
    if toks[0] == "Rpt" and "reporting" not in ql and "rpt" not in ql:
        pen = max(pen, 0.55)
    for t in toks:
        if t in SUFFIX_PENALTY:
            pen = max(pen, SUFFIX_PENALTY[t])
    return min(pen, 1.0)


# --------------------------------------------------------------------------- #
# Name match
# --------------------------------------------------------------------------- #
_FTS_STRIP = re.compile(r"[^A-Za-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "for", "on", "in", "to", "is", "are", "was", "were",
    "what", "which", "how", "did", "do", "does", "and", "or", "by", "with",
    "that", "this", "it", "we", "i", "me", "my", "s",
}


def name_tokens(query: str) -> list[str]:
    return [
        t.lower()
        for t in _FTS_STRIP.sub(" ", query).split()
        if len(t) > 1 and t.lower() not in _STOP
    ]


def canon_set(tokens) -> set[str]:
    """Normalise through the abbreviation dictionary so "on hand quantity" and
    ``OnHandQty`` become the same token set.

    Deliberately **structural only**. Business synonymy (planner -> ``PersonID``,
    "Example Site" -> ``Plant='10'``) is the dense leg's job; hard-coding it here would
    be fitting the eval set rather than the schema.
    """
    return {CANON.get(t.lower(), t.lower()) for t in tokens}


def _overlap(q: set[str], f: set[str]) -> float:
    if not q or not f:
        return 0.0
    if q == f:
        return 1.0
    inter = len(q & f)
    if not inter:
        return 0.0
    return inter / len(q | f)  # Jaccard: rewards using the WHOLE name


def name_match_score(query: str, table: str, field: str, *, scoped: bool = True) -> float:
    """0.0-1.0 agreement between the query and the column NAME alone.

    Ignores the description on purpose: this is the term that must not be
    outvoted by a semantically-plausible wrong-table field. Scoped mode strips
    query tokens that merely name the already-known table; unscoped mode scores
    the table name as a separate 30% term.
    """
    q = canon_set(name_tokens(query))
    if not q:
        return 0.0
    ftok = canon_set(split_camel(field))
    ttok = canon_set(split_camel(table))
    if scoped:
        return _overlap(q - ttok or q, ftok)
    return 0.7 * _overlap(q - ttok or q, ftok) + 0.3 * _overlap(q, ttok)


# --------------------------------------------------------------------------- #
# Type affinity  (SQL types — see the module docstring)
# --------------------------------------------------------------------------- #
NUMERIC_SQL_TYPES = frozenset({
    "decimal", "numeric", "int", "bigint", "smallint", "tinyint",
    "float", "real", "money", "smallmoney",
})
DATE_SQL_TYPES = frozenset({
    "datetime", "datetime2", "smalldatetime", "date", "time", "datetimeoffset",
})
BOOL_SQL_TYPES = frozenset({"bit"})
TEXT_SQL_TYPES = frozenset({
    "nvarchar", "varchar", "char", "nchar", "text", "ntext", "uniqueidentifier",
})

_Q_NUMERIC = re.compile(
    r"\b(how many|how much|quantity|qty|amount|total|count|cost|price|"
    r"hours|percent|value|number of|sum)\b", re.I)
_Q_DATE = re.compile(
    r"\b(when|date|day|month|year|deadline|due|finished|completed on|scheduled)\b", re.I)
_Q_BOOL = re.compile(
    r"\b(is |are |was |were |flag|closed\?|whether|true|false|yes/no)\b", re.I)
_Q_TEXT = re.compile(
    r"\b(name|description|called|code|id\b|who|which \w+ is|comment|text)\b", re.I)


def type_affinity(query: str, sql_type: str) -> float:
    """-1..+1 agreement between the SHAPE the question implies and the column type.

    "how many parts were scrapped" wants a number, so ``ScrapQty`` (decimal)
    should beat ``ScrapReasonCode`` (nvarchar); "when did the operation finish"
    wants a date, so ``ActualEndDate`` should beat ``OpComplete`` (bit).
    """
    t = (sql_type or "").lower()
    want_num = bool(_Q_NUMERIC.search(query))
    want_date = bool(_Q_DATE.search(query))
    want_bool = bool(_Q_BOOL.search(query))
    want_text = bool(_Q_TEXT.search(query))
    if not (want_num or want_date or want_bool or want_text):
        return 0.0
    score = 0.0
    if want_date:
        score += 1.0 if t in DATE_SQL_TYPES else -0.5
    if want_num:
        score += 0.7 if t in NUMERIC_SQL_TYPES else -0.4
    if want_bool and not (want_num or want_date):
        score += 0.7 if t in BOOL_SQL_TYPES else -0.2
    if want_text and not (want_num or want_date):
        score += 0.5 if t in TEXT_SQL_TYPES else -0.3
    return max(-1.0, min(1.0, score))


#: A small static prior over commonly queried manufacturing tables, most
#: common first. It only breaks near-ties: ``TRAFFIC_PRIOR_WEIGHT`` keeps it
#: far below the lexical and dense scores.
TRAFFIC_PRIOR_WEIGHT = 0.02

HOT_TABLES: tuple[str, ...] = (
    "JobHead", "Part", "LaborDtl", "JobOper", "POHeader", "DMRHead", "APInvHed",
    "JobMtl", "OrderHed", "InvcHead", "APInvDtl", "Vendor", "OrderDtl", "PartWhse",
    "DMRActn", "CheckHed", "Customer", "PartTran", "SugPoDtl", "JobAsmbl", "Warehse",
    "PartMtl", "PODetail", "InvcDtl", "PartBin", "Resource", "VendCnt", "RcvDtl",
    "PartCost", "Plant",
)
_HOT_RANK = {t: i for i, t in enumerate(HOT_TABLES)}


def traffic_prior(table: str) -> float:
    """0.0 (not a listed common table) .. 1.0 (the first, most common table)."""
    r = _HOT_RANK.get(table)
    return 0.0 if r is None else 1.0 - (r / len(HOT_TABLES)) * 0.5


def thin_description(field: str, description: str) -> bool:
    """True when the data dictionary gives the dense leg nothing to embed.

    Missing descriptions and descriptions that repeat the field name need
    name matching to remain discoverable.
    """
    d = (description or "").strip()
    if not d:
        return True
    flat = re.sub(r"[^a-z0-9]", "", d.lower())
    return flat == field.lower() or len(d) <= len(field) + 2


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def fuse(
    dense: list[tuple[str, float]],
    lex: list[tuple[str, float]],
    query: str,
    meta: dict[str, tuple[str, str, str]],
    *,
    scoped: bool = True,
    w_name: float = 0.05,
    w_lex: float = 0.01,
    w_prior: float = 0.10,
    w_type: float = 0.02,
    topn: int = 50,
) -> list[tuple[str, float]]:
    """Score-level fusion. *meta* maps key -> ``(table, field, sql_type)``.

    ``lex`` contributes RANK credit, not its raw BM25 score: BM25 magnitudes are
    not comparable to cosines, and the whole point of ``w_lex = 0.01`` is that
    the lexical leg can rescue a column the dense leg missed entirely without
    ever outvoting it.
    """
    scores: dict[str, float] = dict(dense)
    lex_credit = {k: 10.0 / (10 + i) for i, (k, _) in enumerate(lex)}
    for k in lex_credit:
        scores.setdefault(k, 0.0)

    out: list[tuple[str, float]] = []
    for k, s in scores.items():
        m = meta.get(k)
        if m is None:
            continue
        table, field, sql_type = m
        out.append((
            k,
            s
            + w_name * name_match_score(query, table, field, scoped=scoped)
            + w_lex * lex_credit.get(k, 0.0)
            + w_type * type_affinity(query, sql_type)
            - w_prior * column_prior_penalty(query, field),
        ))
    return sorted(out, key=lambda kv: -kv[1])[:topn]
