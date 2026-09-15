"""Document composition for table and column retrieval.

Dense documents combine qualified names, CamelCase tokens, abbreviation
expansions, types and descriptions. Splitting ``PartWhse`` into ``Part Whse``
without also expanding ``Whse`` to ``warehouse`` loses useful meaning, so
``split_camel`` and ``ABBREV`` must be used together.

Lexical documents contain names only. Dictionary prose can repeat a query's
words on unrelated sibling columns and obscure the column being requested.
Both paths return ranked candidates rather than claiming one match is certain.
The local regression coverage is in ``tests/test_discovery.py``.
"""

from __future__ import annotations

import re

__all__ = [
    "ABBREV",
    "CANON",
    "split_camel",
    "expand_abbrevs",
    "field_document",
    "field_name_document",
    "table_document",
    "table_name_document",
]

#: Epicor's abbreviation vocabulary. Values may be MULTI-WORD on purpose: ``req``
#: expands to "required request" because the corpus uses it for both, and the
#: dense leg benefits from carrying both senses. Extending this dictionary is the
#: shared vocabulary for document and query text; verify both paths when changing it.
ABBREV: dict[str, str] = {
    "qty": "quantity", "qtys": "quantities", "num": "number", "nbr": "number",
    "no": "number", "dtl": "detail", "dtls": "details", "hed": "header",
    "hd": "header", "hdr": "header", "whse": "warehouse", "cust": "customer",
    "vend": "vendor", "amt": "amount", "amts": "amounts", "desc": "description",
    "dt": "date", "seq": "sequence", "oper": "operation", "opr": "operation",
    "op": "operation", "ops": "operations", "mtl": "material",
    "mtls": "materials", "asmbl": "assembly", "asm": "assembly",
    "req": "required request", "sched": "schedule scheduled",
    "cmpl": "complete", "comp": "complete", "est": "estimated",
    "act": "actual", "prod": "production product", "rcv": "receipt received",
    "rcvd": "received", "invc": "invoice", "inv": "invoice inventory",
    "ap": "accounts payable supplier", "ar": "accounts receivable customer",
    "po": "purchase order", "so": "sales order", "ext": "extended",
    "doc": "document currency", "rpt": "reporting currency",
    "dsp": "display", "scr": "screen", "uom": "unit of measure",
    "emp": "employee", "lbr": "labor", "grp": "group", "id": "identifier code",
    "chk": "check", "pmt": "payment", "loc": "location", "xfer": "transfer",
    "tran": "transaction", "trn": "transaction", "rev": "revision",
    "cfg": "configuration", "config": "configuration", "alloc": "allocated",
    "sug": "suggestion suggested", "dmr": "discrepant material report",
    "mrp": "material requirements planning", "wip": "work in process",
    "pct": "percent", "min": "minimum", "max": "maximum", "avg": "average",
    "std": "standard", "mfg": "manufacturing", "eng": "engineering",
    "tot": "total", "bal": "balance", "curr": "currency", "addr": "address",
    "ph": "phone", "cnt": "contact count", "cnts": "contacts",
    "bin": "bin location", "fifo": "first in first out",
    "sub": "subcontract", "subcont": "subcontract", "burden": "overhead burden",
    "hrs": "hours", "wi": "what if", "kb": "kanban", "ud": "user defined",
    "sm": "shipment", "brw": "browse", "jm": "job material",
    "etc": "expected time of completion", "glb": "global",
    "rfq": "request for quote", "atp": "available to promise",
    "ium": "inventory unit of measure", "attch": "attachment attached file",
}

#: Single canonical word per abbreviation, for set-overlap name matching.
CANON: dict[str, str] = {k: v.split()[0] for k, v in ABBREV.items()}

_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def split_camel(name: str) -> list[str]:
    """``OnHandQty`` -> ``['On','Hand','Qty']``; ``APInvHed`` -> ``['AP','Inv','Hed']``.

    Two boundary behaviours are relied on downstream and must not change:

    * the digit stays **glued** to its word — ``Rpt1InvoiceAmt`` ->
      ``['Rpt1','Invoice','Amt']`` — which is what lets the reporting-currency
      prior key on ``Rpt1``/``Rpt2``/``Rpt3`` without also demoting every column
      that merely contains the letters;
    * ``InExtPriceDtl`` -> ``['In','Ext','Price','Dtl']`` while ``InvoiceAmt``
      -> ``['Invoice','Amt']``. That split is what makes the ``In``-prefix
      penalty safe — it fires on the tax-inclusive ``In*`` family without
      touching ``Invoice*``.
    """
    return [t for t in _CAMEL_RE.findall(name.replace("_", " ")) if t]


def expand_abbrevs(tokens: list[str]) -> list[str]:
    """Expansions only — callers concatenate them AFTER the original tokens."""
    out: list[str] = []
    for t in tokens:
        exp = ABBREV.get(t.lower())
        if exp:
            out.extend(exp.split())
    return out


def _name_blob(table: str, field: str = "") -> str:
    """``Table.Field`` + split tokens + expansions. The lexical document, and the
    prefix of the dense one."""
    ftok = split_camel(field) if field else []
    ttok = split_camel(table)
    parts = [f"{table}.{field}" if field else table]
    parts += ftok + ttok
    parts += expand_abbrevs(ftok + ttok)
    return " ".join(parts)


def field_document(
    table: str, field: str, sql_type: str, description: str, label: str = ""
) -> str:
    'The dense document for one column: name, SQL type, description, optional label.'
    blob = _name_blob(table, field)
    parts = [blob]
    if label:
        parts.append(label)
    if sql_type:
        parts.append(f"({sql_type})")
    if description:
        parts.append(description)
    return " ".join(parts).strip()


def field_name_document(table: str, field: str) -> str:
    """The **N** lexical document — name material only, never the prose."""
    return _name_blob(table, field)


def table_document(table: str, schema: str, description: str, columns: list[str]) -> str:
    'Dense document for a TABLE.'
    blob = _name_blob(table)
    head = f"{schema}.{table} {blob}"
    body = description or ""
    if columns:
        body = f"{body} columns: {', '.join(columns[:40])}".strip()
    return f"{head} {body}".strip()


def table_name_document(table: str, schema: str) -> str:
    """Lexical document for a TABLE — name material only."""
    return f"{schema}.{table} {_name_blob(table)}"
