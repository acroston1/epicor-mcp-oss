"""Schema discovery with deterministic substring retrieval and optional vectors."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

from epicor_mcp.index.substring import substring_score

from epicor_mcp.discovery import rank as _rank
from epicor_mcp.discovery.text import (
    field_document,
    field_name_document,
    table_document,
    table_name_document,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DiscoveryIndex",
    "FIELD_QUERY_PREFIX",
    "TABLE_QUERY_PREFIX",
    "FieldHit",
    "TableHit",
    "EMBED_DIM",
    "remove_vectors",
    "write_vectors",
]

#: Default truncation dimension for ENDPOINT-built vectors. The dimension that
#: actually governs a built index is ``manifest["dim"]``; imported vectors and
#: query embeddings must match it, and truncating only one side would
#: invalidate every similarity score.
EMBED_DIM = 2048

#: Field retrieval asks for a physical database column, not a help document.
#: Keep the task instruction distinct from document-search instructions.
FIELD_QUERY_PREFIX = (
    "Instruct: Given a business question about ERP data, retrieve the "
    "database table column that stores that value\nQuery: "
)

#: Table retrieval asks for the kind of record, rather than a column value.
#: Keep its instruction separate so both retrieval tasks remain explicit.
TABLE_QUERY_PREFIX = (
    "Instruct: Given a business question about ERP data, retrieve the "
    "database table that holds that kind of record\nQuery: "
)

_FTS_STRIP = re.compile(r"[^A-Za-z0-9]+")

#: Words that appear in ordinary questions AND as Epicor column names, so a
#: name match on them is noise rather than a finding.
_ELSEWHERE_STOPWORDS = frozenset({
    "quantity", "number", "date", "code", "name", "type", "value", "total",
    "amount", "description", "status", "line", "part", "job", "order", "company",
    "many", "much", "what", "which", "that", "this", "from", "with", "have",
    "been", "were", "does", "made", "into", "over", "they", "them", "when",
    # plurals of the above: FTS stems, this membership test does not.
    "quantities", "numbers", "dates", "codes", "names", "types", "values",
    "totals", "amounts", "descriptions", "lines", "parts", "jobs", "orders",
    # "reported"/"reporting" stems to "report" and hits Epicor's Rpt*/`*Reporting`
    # currency-mirror family, which rank.column_prior_penalty already treats as
    # noise on the main path.
    "report", "reported", "reporting",
})


def _stem(tok: str) -> str:
    """Crude suffix strip, to agree with the FTS index's porter tokenizer.

    The lexical index found the row by matching the STEM, so a raw membership
    test disagrees with it: ``"scrapped" in "scrapqty"`` is False and the most
    useful answer gets dropped. Only the suffixes that actually cause that are
    stripped — this is a reconciliation, not a stemmer.
    """
    low = tok.lower()
    for suf in ("pped", "gged", "nned", "tted", "ing", "ed", "es", "s"):
        if len(low) > len(suf) + 3 and low.endswith(suf):
            return low[: -len(suf)]
    return low


def _fts_query(text: str) -> str:
    """FTS5 MATCH string. Bare terms OR'd — the lexical leg is a rescue, so
    recall matters more than precision, and an unquoted user string with an
    apostrophe or a hyphen is an FTS5 syntax error rather than zero results."""
    toks = [t for t in _FTS_STRIP.sub(" ", text).split() if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in toks)


@dataclass(frozen=True)
class FieldHit:
    table: str
    field: str
    sql_type: str
    description: str
    label: str
    score: float
    required: bool = False
    like_table: str = ""
    like_field: str = ""


@dataclass(frozen=True)
class TableHit:
    table: str
    schema: str
    full_name: str
    description: str
    score: float
    field_count: int


class DiscoveryIndex:
    """Loads the built index and answers scoped field / global table queries.

    Construct with :meth:`load`; build with :func:`build` in
    ``scripts/build_discovery_index.py``.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest: dict[str, Any] = json.loads(
            (self.root / "manifest.json").read_text()
        )
        self._fields_meta = json.loads((self.root / "fields_meta.json").read_text())
        self._tables_meta = json.loads((self.root / "tables_meta.json").read_text())

        #: table -> {"offset": int, "count": int}
        self._blocks: dict[str, dict[str, int]] = self._fields_meta["blocks"]
        #: flat, index-aligned with ``fields.npy`` rows
        self._rows: list[dict[str, Any]] = self._fields_meta["rows"]
        self._tables: list[dict[str, Any]] = self._tables_meta["tables"]

        self._fmat = self._tmat = None
        self.dim: int | None = None
        #: Why vectors are NOT in use although the manifest says they were
        #: built. Empty when they load cleanly or the index is metadata-only.
        self.semantic_error = ""
        self._dim_warned = False
        if self.manifest.get("semantic"):
            self._load_vectors()
        self._lex = sqlite3.connect(
            f"file:{self.root / 'lex.db'}?mode=ro", uri=True, check_same_thread=False
        )
        #: case-insensitive table lookup, so a caller may say ``partwhse``.
        self._by_lower = {t["table"].lower(): t for t in self._tables}

    def _load_vectors(self) -> None:
        """Load and VERIFY the vector arrays; a bad pair degrades to substring.

        The manifest is a claim, not proof: a truncated copy, arrays left
        behind by an older build, or a dimension that no longer matches the
        rows would otherwise rank every query by garbage under
        ``search_mode: "semantic"``. Verified once at load; the reason rides
        in ``semantic_error`` so the tools can announce it.
        """
        try:
            import numpy as np

            dim = int(self.manifest.get("dim") or 0)
            fmat = np.load(self.root / "fields.npy", mmap_mode="r", allow_pickle=False)
            tmat = np.load(self.root / "tables.npy", allow_pickle=False)
            if dim <= 0 or fmat.shape != (len(self._rows), dim) or tmat.shape != (len(self._tables), dim):
                raise ValueError(
                    f"vector shapes {tuple(fmat.shape)}/{tuple(tmat.shape)} do not match the "
                    f"manifest ({len(self._rows)}x{dim} fields, {len(self._tables)}x{dim} tables)"
                )
            if not np.isfinite(tmat).all():
                raise ValueError("table vectors contain non-finite values")
        except Exception as exc:  # noqa: BLE001 - substring search must survive any vector fault
            self.semantic_error = (
                f"Semantic discovery vectors unavailable ({type(exc).__name__}: {exc}); "
                "rebuild the discovery index with --model. Using substring search."
            )
            logger.warning("%s", self.semantic_error)
            self._fmat = self._tmat = None
            self.dim = None
            return
        self._fmat, self._tmat, self.dim = fmat, tmat, dim

    def _usable(self, qvec: Any) -> Any:
        """A query vector counts only when it matches the loaded arrays."""
        if qvec is None or self._tmat is None:
            return None
        if getattr(qvec, "shape", None) != (self.dim,):
            if not self._dim_warned:
                logger.warning(
                    "discovery query vector has shape %s but the index dimension is %s "
                    "— using substring ranking", getattr(qvec, "shape", None), self.dim,
                )
                self._dim_warned = True
            return None
        return qvec

    # -- introspection ----------------------------------------------------- #
    @classmethod
    def load(cls, root: Path | str) -> "DiscoveryIndex | None":
        root = Path(root)
        if not (root / "manifest.json").exists():
            logger.warning(
                "discovery index not built at %s — epicor_tables/epicor_fields "
                "will be empty. Run scripts/build_schema_catalogue.py (README section 3)",
                root,
            )
            return None
        try:
            return cls(root)
        except Exception:  # noqa: BLE001
            logger.exception("discovery index at %s failed to load", root)
            return None

    @classmethod
    def empty(cls) -> "DiscoveryIndex":
        """Keep discovery tools available before administrator metadata import."""
        index = cls.__new__(cls)
        index.root = Path(".")
        index.manifest = {"table_count": 0, "field_count": 0, "semantic": False}
        index._blocks = {}
        index._rows = []
        index._tables = []
        index._by_lower = {}
        index._fmat = index._tmat = None
        index.dim = None
        index.semantic_error = ""
        index._dim_warned = False
        index._lex = sqlite3.connect(":memory:", check_same_thread=False)
        return index

    def has_table(self, table: str) -> bool:
        return self.resolve_table(table) is not None

    def resolve_table(self, table: str) -> str | None:
        """Caller-supplied name -> canonical casing. Accepts ``Erp.PartWhse``."""
        bare = table.split(".")[-1].strip()
        row = self._by_lower.get(bare.lower())
        if row and "." in table and table.rsplit(".", 1)[0].strip().casefold() != row["schema"].casefold():
            return None
        return row["table"] if row else None

    def table_info(self, table: str) -> dict[str, Any] | None:
        canonical = self.resolve_table(table) if table else None
        return self._by_lower.get(canonical.lower()) if canonical else None

    def all_tables(self) -> list[dict[str, Any]]:
        return list(self._tables)

    def fields_of(self, table: str) -> list[dict[str, Any]]:
        canon = self.resolve_table(table)
        if canon is None:
            return []
        blk = self._blocks[canon]
        return self._rows[blk["offset"] : blk["offset"] + blk["count"]]

    def close(self) -> None:
        self._lex.close()

    # -- search ------------------------------------------------------------ #
    def search_fields(
        self,
        table: str,
        qvec: np.ndarray | None,
        query: str,
        *,
        limit: int = 15,
        exclude: Iterable[str] = (),
        per_term: bool = False,
    ) -> list[FieldHit]:
        """Rank one table's columns. *qvec* is the already-embedded query.

        ``per_term`` marks *query* as ONE term of a split multi-concept query
        (see :meth:`search_fields_terms`): the semantic path switches on the
        per-term fusion terms in :func:`~epicor_mcp.discovery.rank.fuse`, and the
        substring path adds the same name-match / exact-name / localisation terms
        to the substring score (:meth:`_lex_term_fields`).

        A ``None`` *qvec* means the embedding server was unreachable; the method
        degrades to the lexical + deterministic terms rather than failing, which
        is the same posture ``epicor_help`` takes. Degraded is announced by the
        caller, never silent.
        """
        canon = self.resolve_table(table)
        if canon is None:
            return []
        qvec = self._usable(qvec)
        blk = self._blocks[canon]
        off, cnt = blk["offset"], blk["count"]
        rows = self._rows[off : off + cnt]
        skip = {e.lower() for e in exclude}

        keys = [f"{canon}.{r['name']}" for r in rows]
        meta = {k: (canon, r["name"], r["type"]) for k, r in zip(keys, rows)}

        if qvec is not None and self._fmat is not None:
            import numpy as np
            block = np.asarray(self._fmat[off : off + cnt])
            sims = (block @ qvec).tolist()
            dense = sorted(zip(keys, sims), key=lambda kv: -kv[1])[:60]
        else:
            dense = []

        lex = self._lex_fields(canon, query, len(rows))

        if not query.strip():
            # No query: this is "show me this table". Preserve Epicor's own
            # column order (Seq) rather than an arbitrary ranking of noise.
            ordered = [(k, 0.0) for k in keys]
        elif not dense:
            ordered = self._lex_term_fields(canon, query) if per_term else lex
        else:
            ordered = _rank.fuse(
                dense, lex, query, meta, scoped=True, topn=limit + len(skip) + 20,
                per_term=per_term,
            )

        out: list[FieldHit] = []
        by_name = {r["name"]: r for r in rows}
        for key, score in ordered:
            name = key.split(".", 1)[1]
            if name.lower() in skip:
                continue
            r = by_name.get(name)
            if r is None:
                continue
            out.append(
                FieldHit(
                    table=canon,
                    field=name,
                    sql_type=r["type"],
                    description=r.get("description", ""),
                    label=r.get("label", ""),
                    score=round(float(score), 4),
                    required=bool(r.get("required")),
                    like_table=r.get("like_table", ""),
                    like_field=r.get("like_field", ""),
                )
            )
            if len(out) >= limit:
                break
        return out

    def search_fields_terms(
        self,
        table: str,
        terms: Sequence[tuple[str, Any]],
        *,
        limit: int = 15,
        exclude: Iterable[str] = (),
    ) -> list[FieldHit]:
        """Rank one table's columns for a query already split into terms.

        *terms* is ``[(term, vector_or_None), …]`` from
        :func:`~epicor_mcp.discovery.rank.split_terms`. ONE term is exactly
        :meth:`search_fields` — same call, same output — so a query with no
        separator is untouched. Several terms are each ranked on their own and
        merged ROUND-ROBIN in the caller's order: every term's best column first,
        then every term's second, deduplicated, up to *limit*. One blurred
        ranking of the whole list lets whichever concept matches strongest take
        every slot. Works identically with and without vectors.
        """
        if len(terms) == 1:
            term, vec = terms[0]
            return self.search_fields(table, vec, term, limit=limit, exclude=exclude)
        per = [
            self.search_fields(table, vec, term, limit=limit, exclude=exclude, per_term=True)
            for term, vec in terms
        ]
        out: list[FieldHit] = []
        seen: set[str] = set()
        for r in range(max((len(p) for p in per), default=0)):
            for hits in per:
                if r < len(hits) and hits[r].field not in seen:
                    seen.add(hits[r].field)
                    out.append(hits[r])
                    if len(out) >= limit:
                        return out
        return out

    def search_tables(
        self,
        qvec: np.ndarray | None,
        query: str,
        *,
        limit: int = 5,
        allowed: set[str] | None = None,
    ) -> list[TableHit]:
        """Rank tables. *allowed* (lower-cased canonical names) filters to what
        the caller may query; ``None`` means no filter."""
        qvec = self._usable(qvec)
        keys = [t["table"] for t in self._tables]
        meta = {t["table"]: (t["table"], t["table"], "") for t in self._tables}

        if qvec is not None and self._tmat is not None:
            sims = (self._tmat @ qvec).tolist()
            dense = sorted(zip(keys, sims), key=lambda kv: -kv[1])[:120]
        else:
            dense = []
        lex = self._lex_tables(query, len(self._tables))

        if not query.strip():
            fused = [(t["table"], 0.0) for t in self._tables]
        elif not dense:
            fused = lex
        else:
            fused = _rank.fuse(
                dense, lex, query, meta,
                scoped=False, w_prior=0.0, w_type=0.0, topn=len(self._tables),
            )

        by_name = {t["table"]: t for t in self._tables}
        out: list[TableHit] = []
        for key, score in fused:
            if allowed is not None and key.lower() not in allowed and by_name[key]["full_name"].casefold() not in allowed:
                continue
            # `<Table>_UD` is a 4-column custom-field MIRROR, not a subject table.
            # It embeds close to its parent (same name stem) and would otherwise
            # take a top-5 slot from a real answer. It is still fully reachable —
            # epicor_fields surfaces it as `custom_columns` with the join recipe.
            if key.endswith("_UD"):
                continue
            t = by_name[key]
            out.append(
                TableHit(
                    table=t["table"],
                    schema=t["schema"],
                    full_name=t["full_name"],
                    description=t.get("description", ""),
                    score=round(float(score), 4),
                    field_count=t["field_count"],
                )
            )
            if len(out) >= limit:
                break
        return out

    def find_column_elsewhere(self, name: str, *, limit: int = 6) -> tuple[list[str], int]:
        """Exact column owners, with a total to distinguish generic field names."""
        owners = sorted({row["table"] for row in self._rows
                         if row["name"].casefold() == name.casefold()}, key=str.casefold)
        return owners[:limit], len(owners)

    def name_matches_elsewhere(self, query: str, exclude_tables: Iterable[str], *,
                               limit: int = 4) -> list[tuple[str, str]]:
        """Literal name matches on other tables; callers apply their scope."""
        skip = {table.casefold() for table in exclude_tables}
        terms = [term.casefold() for term in query.split()
                 if len(term) >= 4 and term.casefold() not in _ELSEWHERE_STOPWORDS]
        matches = {(row["table"], row["name"]) for row in self._rows
                   if row["table"].casefold() not in skip
                   and any(term in row["name"].casefold() for term in terms)}
        return sorted(matches, key=lambda item: (item[0].casefold(), item[1].casefold()))[:limit]

    # -- lexical legs ------------------------------------------------------ #
    def _lex_fields(self, table: str, query: str, k: int) -> list[tuple[str, float]]:
        matches = []
        for row in self.fields_of(table):
            score = substring_score(query, row["name"], row.get("label", ""), row.get("description", ""))
            if score:
                matches.append((f"{table}.{row['name']}", score))
        return sorted(matches, key=lambda item: (-item[1], item[0].casefold()))[:k]

    def _lex_term_fields(self, table: str, term: str) -> list[tuple[str, float]]:
        """Substring ranking for ONE term of a multi-concept query.

        The plain substring score cannot see that "invoice number" names
        ``InvoiceNum`` (no phrase match in a CamelCase name), so the per-term
        name terms from :mod:`rank` are added on a scale that lets a full name
        match outrank a phrase that merely occurs in some description, and a
        localisation-family or plumbing column fall behind its base column.
        Rows matching neither way are dropped, as on the single-term path.
        """
        matches = []
        for row in self.fields_of(table):
            field = row["name"]
            sub = substring_score(term, field, row.get("label", ""), row.get("description", ""))
            name = _rank.term_name_match(term, table, field)
            if not sub and not name:
                continue
            score = (
                sub
                + 60.0 * name
                + 150.0 * _rank.exact_name_match(term, table, field)
                - 60.0 * _rank.locale_penalty(term, field)
                - 30.0 * _rank.column_prior_penalty(term, field)
            )
            matches.append((f"{table}.{field}", score))
        return sorted(matches, key=lambda item: (-item[1], item[0].casefold()))

    def _lex_tables(self, query: str, k: int) -> list[tuple[str, float]]:
        matches = []
        for row in self._tables:
            score = substring_score(query, row["full_name"], row.get("description", ""))
            if score:
                matches.append((row["table"], score))
        return sorted(matches, key=lambda item: (-item[1], item[0].casefold()))[:k]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def compose_documents(
    catalogue: dict[str, Any],
) -> tuple[list[dict], list[str], list[str], list[dict], list[str], list[str]]:
    """Catalogue -> (field rows, field dense docs, field lexical docs,
    table rows, table dense docs, table lexical docs).

    Field rows are emitted **contiguously per table** — the block layout the
    memmap slice depends on.
    """
    frows: list[dict] = []
    fdense: list[str] = []
    flex: list[str] = []
    trows: list[dict] = []
    tdense: list[str] = []
    tlex: list[str] = []

    for table in sorted(catalogue):
        v = catalogue[table]
        if v.get("error"):
            continue
        fields = v.get("fields") or []
        schema = v.get("schema") or "Erp"
        offset = len(frows)
        for f in fields:
            frows.append(
                {
                    "table": table,
                    "name": f["name"],
                    "type": f.get("type", ""),
                    "description": f.get("description", ""),
                    "label": f.get("label", ""),
                    "required": bool(f.get("required")),
                    "like_table": f.get("like_table", ""),
                    "like_field": f.get("like_field", ""),
                }
            )
            fdense.append(
                field_document(
                    table,
                    f["name"],
                    f.get("type", ""),
                    f.get("description", ""),
                    f.get("label", ""),
                )
            )
            flex.append(field_name_document(table, f["name"]))
        trows.append(
            {
                "table": table,
                "schema": schema,
                "full_name": v.get("full_name") or f"{schema}.{table}",
                "table_type": v.get("table_type", "DB"),
                "description": v.get("description", ""),
                "field_count": len(fields),
                "primary_key": v.get("primary_key", []),
                "offset": offset,
            }
        )
        tdense.append(
            table_document(table, schema, v.get("description", ""), [f["name"] for f in fields])
        )
        tlex.append(table_name_document(table, schema))
    return frows, fdense, flex, trows, tdense, tlex


def write_lexical(root: Path, frows: Sequence[dict], flex: Sequence[str],
                  trows: Sequence[dict], tlex: Sequence[str]) -> None:
    path = root / "lex.db"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE VIRTUAL TABLE field_names USING fts5(
            key UNINDEXED, tbl UNINDEXED, doc, tokenize='porter unicode61');
        CREATE VIRTUAL TABLE table_names USING fts5(
            key UNINDEXED, doc, tokenize='porter unicode61');
        CREATE TABLE field_owner (fld TEXT, tbl TEXT);
        """
    )
    con.executemany(
        "INSERT INTO field_names (key, tbl, doc) VALUES (?,?,?)",
        [(f"{r['table']}.{r['name']}", r["table"], d) for r, d in zip(frows, flex)],
    )
    con.executemany(
        "INSERT INTO table_names (key, doc) VALUES (?,?)",
        [(r["table"], d) for r, d in zip(trows, tlex)],
    )
    con.executemany(
        "INSERT INTO field_owner (fld, tbl) VALUES (?,?)",
        [(r["name"].lower(), r["table"]) for r in frows],
    )
    con.execute("CREATE INDEX ix_field_owner ON field_owner(fld)")
    con.commit()
    con.close()


def write_vectors(root: Path, fmat: Any, tmat: Any) -> None:
    """Write both arrays atomically, so a crash never leaves a manifest
    pointing at half a file."""
    import numpy as np

    for name, matrix in (("fields.npy", fmat), ("tables.npy", tmat)):
        tmp = root / (name + ".tmp")
        with tmp.open("wb") as file:
            np.save(file, np.ascontiguousarray(matrix, dtype=np.float32), allow_pickle=False)
        tmp.replace(root / name)


def remove_vectors(root: Path) -> None:
    """Drop arrays an older build left behind; a metadata-only manifest must
    not sit beside vectors that no longer align with its rows."""
    for name in ("fields.npy", "tables.npy"):
        path = root / name
        if path.exists():
            path.unlink()
