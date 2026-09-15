"""Optional operator-supplied value-domain diagnostics. No tenant observations are bundled."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
__all__ = ['AS_OF', 'COMPANY_ID', 'PLANTS', 'PLANT_NAME_TO_CODE', 'Domain', 'DOMAINS', 'ALWAYS_FALSE', 'ALWAYS_TRUE', 'EMPTY_COLUMNS', 'SURROGATE_KEYS', 'TABLE_ROWS', 'Finding', 'ground', 'plant_code', 'domain_block', 'dead_flags_for', 'known_domain', 'BRAND_COLUMN', 'PLANT_JOB_ROWS', 'SECURED_FOR_CRITERIA']
AS_OF = 'operator configured'
COMPANY_ID = ''
PLANTS: dict[str, str] = {}
PLANT_JOB_ROWS: dict[str, int] = {}
PLANT_NAME_TO_CODE: dict[str, str] = {}
_SITE_COLUMNS = {'plant', 'siteid', 'site', 'plant1'}

def plant_code(text: str | None) -> str | None:
    """Value-domain diagnostic helper; observations must be supplied by the operator."""
    if not text:
        return None
    low = text.strip().lower()
    if low in PLANTS:
        return low
    for name in sorted(PLANT_NAME_TO_CODE, key=len, reverse=True):
        if re.search(f'\\b{re.escape(name)}\\b', low):
            return PLANT_NAME_TO_CODE[name]
    embedded = re.fullmatch('(?:site|plant)\\s*[-_ ]?\\s*(\\d+)', low)
    if embedded and embedded.group(1) in PLANTS:
        return embedded.group(1)
    return None

def _norm(value: str) -> str:
    """Value-domain diagnostic helper; observations must be supplied by the operator."""
    return (value or '').rstrip().casefold()

@dataclass(frozen=True)
class Domain:
    """Value-domain diagnostic helper; observations must be supplied by the operator."""
    table: str
    column: str
    values: tuple[str, ...]
    counts: tuple[int, ...]
    enumerated: bool
    meanings: dict[str, str] = field(default_factory=dict)
    note: str = ''

    @property
    def key(self) -> tuple[str, str]:
        return (self.table.lower(), self.column.lower())

    def contains(self, literal: str) -> bool:
        """Value-domain diagnostic helper; observations must be supplied by the operator."""
        return _norm(literal) in {_norm(v) for v in self.values}

    def render(self) -> str:
        """Compact ``code=meaning`` rendering for the card."""
        parts = []
        for v in self.values:
            label = self.meanings.get(v)
            shown = "''" if v == '' else v
            parts.append(f'{shown} {label}' if label else shown)
        return ' | '.join(parts)

def _dom(table: str, column: str, rows: Iterable[tuple[str, int]], *, enumerated: bool, meanings: dict[str, str] | None=None, note: str='') -> Domain:
    rows = list(rows)
    return Domain(table, column, tuple((v for v, _ in rows)), tuple((n for _, n in rows)), enumerated, meanings or {}, note)
_DOMAIN_LIST: tuple[Domain, ...] = ()
DOMAINS: dict[tuple[str, str], Domain] = {d.key: d for d in _DOMAIN_LIST}
EMPTY_COLUMNS: dict[tuple[str, str], tuple[str, int, str]] = {}
SECURED_FOR_CRITERIA: frozenset[tuple[str, str]] = frozenset()
SURROGATE_KEYS: dict[str, tuple[int, int, int, str]] = {}
_FREE_TEXT_COLUMNS = re.compile('^(name|.*description|linedesc|opdesc|partdescription|description)$', re.I)
BRAND_COLUMN = 'CommercialBrand'
BRAND_TOP: tuple[tuple[str, int], ...] = ()
ALWAYS_FALSE: dict[str, frozenset[str]] = {}
ALWAYS_TRUE: dict[str, frozenset[str]] = {}
TABLE_ROWS: dict[str, int] = {}

def dead_flags_for(table: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(always-false, always-true)`` bit columns for *table*; empty off-card."""
    name = table.rsplit('.', 1)[-1]
    for known in ALWAYS_FALSE:
        if known.lower() == name.lower():
            return (ALWAYS_FALSE[known], ALWAYS_TRUE.get(known, frozenset()))
    for known in ALWAYS_TRUE:
        if known.lower() == name.lower():
            return (frozenset(), ALWAYS_TRUE[known])
    return (frozenset(), frozenset())

def _table_rows(table: str) -> int | None:
    for known, n in TABLE_ROWS.items():
        if known.lower() == table.lower():
            return n
    return None

@dataclass(frozen=True)
class Finding:
    """One dated, falsifiable statement about one predicate.

    It is never a refusal and never a rewrite. ``certain`` says whether the
    finding may fire pre-flight; ``causes_empty`` says whether the predicate sits
    in a plain top-level AND chain (so it alone can zero the result) or under an
    OR / NOT / join condition, where it cannot.
    """
    kind: str
    table: str
    column: str
    given: str
    message: str
    certain: bool
    causes_empty: bool
    suggest: str | None = None
    domain: tuple[str, ...] | None = None
    as_of: str = AS_OF

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {'finding': self.kind, 'column': f'{self.table}.{self.column}' if self.table else self.column, 'given': self.given, 'message': self.message, 'as_of': self.as_of}
        if self.suggest:
            d['suggest'] = self.suggest
        if self.domain is not None:
            d['domain'] = list(self.domain)
        return d

def _parse(sql: str):
    """Parse to a sqlglot tree, or ``None``. Never raises."""
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return None
    for dialect in ('tsql', None):
        try:
            tree = sqlglot.parse_one(sql, read=dialect) if dialect else sqlglot.parse_one(sql)
        except Exception:
            continue
        if tree is not None:
            return tree
    return None

def _alias_to_table(tree) -> dict[str, str]:
    """Value-domain diagnostic helper; observations must be supplied by the operator."""
    from sqlglot import exp
    shadowed = {(cte.alias or '').lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    out: dict[str, str] = {}
    clashes: set[str] = set()
    for node in tree.find_all(exp.Table):
        table = node.name
        if not table or table.lower() in shadowed:
            continue
        keys = {table.lower()}
        alias = node.alias
        if alias:
            keys.add(alias.lower())
        for k in keys:
            if k in out and out[k].lower() != table.lower():
                clashes.add(k)
            out[k] = table
    for k in clashes | shadowed:
        out.pop(k, None)
    return out

def _literal_text(node) -> str | None:
    """The literal value of *node* as text, or ``None`` if it is not a literal."""
    from sqlglot import exp
    if isinstance(node, exp.Literal):
        return str(node.this)
    if isinstance(node, exp.Boolean):
        return 'true' if node.this else 'false'
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return '-' + str(node.this.this)
    return None

def _column_ref(node, aliases: dict[str, str]) -> tuple[str, str] | None:
    """``(table, column)`` for a qualified column, or ``None``.

    An UNQUALIFIED column resolves only when the statement has exactly one
    source; otherwise it is ambiguous and produces no finding.
    """
    from sqlglot import exp
    if not isinstance(node, exp.Column):
        return None
    col = node.name
    if not col:
        return None
    qual = node.table
    if qual:
        table = aliases.get(qual.lower())
        return (table, col) if table else None
    distinct = {t.lower() for t in aliases.values()}
    if len(distinct) == 1:
        return (next(iter(aliases.values())), col)
    return None

def _under_or_not(node) -> bool:
    """True when *node* sits under an OR, a NOT, or a join condition."""
    from sqlglot import exp
    cur = node.parent
    while cur is not None:
        if isinstance(cur, (exp.Or, exp.Not, exp.Join, exp.Case)):
            return True
        if isinstance(cur, exp.Select):
            return False
        cur = cur.parent
    return False

def _comparisons(tree, aliases: dict[str, str]):
    """Yield ``(table, column, literal, negated, causes_empty, node)`` for every
    column-to-literal comparison, plus every ``IN`` list membership."""
    from sqlglot import exp
    for node in tree.find_all(exp.EQ, exp.NEQ):
        for left, right in ((node.this, node.expression), (node.expression, node.this)):
            ref = _column_ref(left, aliases)
            lit = _literal_text(right)
            if ref and lit is not None:
                yield (*ref, lit, isinstance(node, exp.NEQ), not isinstance(node, exp.NEQ) and (not _under_or_not(node)), node)
                break
    for node in tree.find_all(exp.In):
        ref = _column_ref(node.this, aliases)
        if not ref:
            continue
        items = node.args.get('expressions') or []
        for item in items:
            lit = _literal_text(item)
            if lit is not None:
                yield (*ref, lit, False, len(items) == 1 and (not _under_or_not(node)), node)

def _rule_company(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    return None

def _rule_site(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    if not PLANTS or column.lower() not in _SITE_COLUMNS or literal in PLANTS:
        return None
    code = plant_code(literal)
    if code is None:
        return None
    return Finding('site_name_where_a_code_is_stored', table, column, literal, f"The configured site map associates '{literal}' with code '{code}'.", certain=False, causes_empty=causes, suggest=f"[{table}].[{column}] = '{code}'", domain=tuple(PLANTS))

def _rule_dead_flag(table: str, column: str, literal: str, negated: bool, causes: bool) -> Finding | None:
    false_flags, true_flags = dead_flags_for(table)
    rows = _table_rows(table)
    if rows is None:
        return None
    truthy = literal.lower() in ('true', '1')
    falsy = literal.lower() in ('false', '0')
    if not (truthy or falsy):
        return None
    if negated:
        truthy, falsy = (falsy, truthy)
    hit_false = truthy and any((c.lower() == column.lower() for c in false_flags))
    hit_true = falsy and any((c.lower() == column.lower() for c in true_flags))
    if not (hit_false or hit_true):
        return None
    if hit_false:
        return Finding('flag_never_set', table, column, f'{column} = {literal}', f'{table}.{column} is TRUE on 0 of {rows:,} rows ({AS_OF}) — the flag exists but is not used in this install, so `= true` returns an empty result with no error. Drop the predicate to see the whole set, or filter on something that is populated.', certain=True, causes_empty=causes, domain=('false',))
    return Finding('flag_always_set', table, column, f'{column} = {literal}', f'{table}.{column} is TRUE on ALL {rows:,} rows ({AS_OF}), so `= false` returns an empty result with no error and `= true` filters nothing.', certain=True, causes_empty=causes, domain=('true',))

def _rule_empty_column(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    entry = EMPTY_COLUMNS.get((table.lower(), column.lower()))
    if entry is None:
        return None
    only, rows, advice = entry
    if _norm(literal) == _norm(only):
        return None
    shown = "''" if only == '' else only
    return Finding('column_is_empty_on_every_row', table, column, literal, f"{table}.{column} is {shown} on all {rows:,} rows ({AS_OF}), so '{literal}' matches nothing. {advice}", certain=True, causes_empty=causes, domain=(only,))

def _rule_secured(table: str, column: str) -> Finding | None:
    if (table.lower(), column.lower()) not in SECURED_FOR_CRITERIA:
        return None
    return Finding('column_secured_for_criteria', table, column, '', f'{table}.{column} may be SELECTed but not filtered — Epicor answers "A secured field can not be referenced by a criteria" ({AS_OF}). This is field security, not a bad value.', certain=True, causes_empty=False)

def _rule_like_pattern(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    if '%' not in literal or len(literal) < 2:
        return None
    dom = DOMAINS.get((table.lower(), column.lower()))
    if dom is not None and dom.contains(literal):
        return None
    return Finding('wildcard_under_equals', table, column, literal, f"'{literal}' contains a percent sign, but `=` compares the whole value literally. Use LIKE if the percent sign is intended as a wildcard.", certain=True, causes_empty=causes, suggest=f"[{table}].[{column}] like '{literal}'")

def _rule_brand(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    return None

def _rule_domain(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    dom = DOMAINS.get((table.lower(), column.lower()))
    if dom is None or dom.contains(literal):
        return None
    if column.lower() in _SITE_COLUMNS and (not re.fullmatch('\\d+', literal.strip())):
        return None
    total = sum(dom.counts)
    tail = f' {dom.note}' if dom.note else ''
    kind = 'value_outside_an_epicor_code_set' if dom.enumerated else 'value_not_present_in_this_column'
    return Finding(kind, table, column, literal, f"{table}.{column} carried exactly {len(dom.values)} distinct values over {total:,} rows ({AS_OF}) and '{literal}' was not one of them: {dom.render()}.{tail}", certain=dom.enumerated, causes_empty=causes, domain=dom.values)

def _rule_surrogate(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    entry = SURROGATE_KEYS.get(column.lower())
    if entry is None:
        return None
    lo, hi, n, human = entry
    try:
        value = int(literal)
    except (TypeError, ValueError):
        return None
    if lo <= value <= hi:
        return None
    return Finding('surrogate_key_outside_its_range', table, column, literal, f'{column} is a surrogate key: it ran {lo}–{hi} over {n:,} records ({AS_OF}) and {value} is outside that. It is not a customer or supplier NUMBER anyone quotes — the human id is {human}.', certain=False, causes_empty=causes)

def _rule_free_text_equals(table: str, column: str, literal: str, causes: bool) -> Finding | None:
    if not _FREE_TEXT_COLUMNS.match(column):
        return None
    if not literal or len(literal) > 60:
        return None
    return Finding('exact_match_on_a_free_text_column', table, column, literal, f"{table}.{column} is free text entered by people; `=` requires the whole value to match '{literal}'. Use LIKE if a partial match is intended.", certain=False, causes_empty=causes, suggest=f"[{table}].[{column}] like '%{literal}%'")

def ground(sql: str, *, row_count: int | None=None) -> list[Finding]:
    """Diagnose value/domain problems in *sql*. Never raises, never refuses.

    ``row_count=None`` — **pre-flight**. Only ``certain`` findings: rules whose
    evidence is a whole-table measurement that a legitimately-right query cannot
    contradict.

    ``row_count == 0`` — **diagnosis**. Everything, including the rules that can
    go stale, because the result is already empty and the only open question is
    which predicate emptied it.

    ``row_count > 0`` — the ``certain`` findings only. They stay true whether or
    not rows came back (a `Buy = true` under an OR still matches nothing), but
    nothing speculative is added to an answer that worked.
    """
    if not sql or not sql.strip():
        return []
    tree = _parse(sql)
    if tree is None:
        return []
    try:
        aliases = _alias_to_table(tree)
        findings: list[Finding] = []
        seen: set[tuple[str, str, str, str]] = set()
        for table, column, literal, negated, causes, _node in _comparisons(tree, aliases):
            for rule in (_rule_company(table, column, literal, causes) if not negated else None, _rule_site(table, column, literal, causes) if not negated else None, _rule_dead_flag(table, column, literal, negated, causes), _rule_empty_column(table, column, literal, causes) if not negated else None, _rule_secured(table, column), _rule_like_pattern(table, column, literal, causes) if not negated else None, _rule_domain(table, column, literal, causes) if not negated else None, _rule_surrogate(table, column, literal, causes) if not negated else None, _rule_brand(table, column, literal, causes) if not negated else None, _rule_free_text_equals(table, column, literal, causes) if not negated else None):
                if rule is None:
                    continue
                key = (rule.kind, rule.table.lower(), rule.column.lower(), rule.given)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(rule)
    except Exception:
        return []
    if row_count is None or row_count > 0:
        return [f for f in findings if f.certain]
    return findings

def known_domain(table: str, column: str) -> Domain | None:
    """Value-domain diagnostic helper; observations must be supplied by the operator."""
    return DOMAINS.get((table.rsplit('.', 1)[-1].lower(), column.lower()))
_CARD_DOMAINS: tuple[tuple[str, str], ...] = (('Part', 'TypeCode'), ('LaborDtl', 'LaborType'), ('PartTran', 'CostMethod'), ('POHeader', 'ApprovalStatus'), ('InvcHead', 'InvoiceType'), ('DMRActn', 'ActionType'), ('SugPoDtl', 'SugType'), ('Customer', 'CustomerType'), ('Resource', 'ResourceType'))
_LIVE_FLAGS = ''
_MEASURED_FAILURE_DOMAINS = frozenset({('resource', 'resourcetype')})

def domain_block(*, compact: bool=False) -> str:
    lines = ["VALUES: use your installation's actual company, site and domain values.", 'CustNum/VendorNum are numeric keys; CustID/VendorID are human-readable identifiers.', 'Do not infer customer ownership from part numbers or assume optional flags are populated.']
    if PLANTS:
        lines.append('Configured sites: ' + ', '.join((f'{code} {name}' for code, name in PLANTS.items())))
    for dom in DOMAINS.values():
        lines.append(f'  {dom.table}.{dom.column}: {dom.render()}')
    return '\n'.join(lines)
