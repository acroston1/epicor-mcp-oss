#!/usr/bin/env python3
"""CLI script to build the BAQ schema SQLite index from the Epicor data dictionary.

Parses an operator-supplied ``all_schemas.json`` dictionary and creates a
SQLite database with FTS5 indexes for table and field lookups. No input
dictionary is bundled. For physical metadata import from Epicor, use
``scripts/build_schema_catalogue.py`` as described in README.md.

Usage:
    python scripts/build_baq_index.py --input /path/to/all_schemas.json --output data/baq_schema.db
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Default path to the Epicor data dictionary
_DEFAULT_INPUT = "data/all_schemas.json"

# Resolve the repo root so relative output paths work from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tables (
    full_name TEXT PRIMARY KEY,   -- "Erp.POHeader"
    schema_name TEXT,             -- "Erp"
    table_name TEXT,              -- "POHeader"
    description TEXT
);

CREATE TABLE IF NOT EXISTS fields (
    full_table_name TEXT,         -- "Erp.POHeader"
    field_name TEXT,              -- "PONum"
    data_type TEXT,               -- "int", "nvarchar", "bit", "decimal"
    field_label TEXT,             -- "PO Number"
    mandatory BOOLEAN,
    field_format TEXT,            -- "x(8)", ">>>>>9", etc.
    description TEXT,
    PRIMARY KEY (full_table_name, field_name)
);

CREATE INDEX IF NOT EXISTS idx_fields_table ON fields(full_table_name);
"""

_FTS_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS table_search USING fts5(
    full_name,
    table_name,
    description,
    content='tables',
    content_rowid='rowid'
);

CREATE VIRTUAL TABLE IF NOT EXISTS field_search USING fts5(
    full_table_name,
    field_name,
    field_label,
    description,
    content='fields',
    content_rowid='rowid'
);
"""

_FTS_POPULATE_TABLES = """\
INSERT INTO table_search(rowid, full_name, table_name, description)
    SELECT rowid, full_name, table_name, description FROM tables;
"""

_FTS_POPULATE_FIELDS = """\
INSERT INTO field_search(rowid, full_table_name, field_name, field_label, description)
    SELECT rowid, full_table_name, field_name, field_label, description FROM fields;
"""


# ---------------------------------------------------------------------------
# Build logic
# ---------------------------------------------------------------------------

def build_baq_index(input_path: str | Path, output_path: str | Path) -> dict[str, int]:
    """Parse all_schemas.json and create the BAQ schema SQLite database.

    Parameters
    ----------
    input_path:
        Path to all_schemas.json.
    output_path:
        Output SQLite database path.

    Returns
    -------
    dict with summary statistics.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()

    # --- Load the data dictionary ---
    logger.info("Loading data dictionary from %s ...", input_path)
    with open(input_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    tables_dict = data.get("tables", {})
    logger.info("Found %d tables in data dictionary", len(tables_dict))

    # --- Connect and create schema ---
    conn = sqlite3.connect(str(output_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Drop existing tables for idempotent rebuild
    for tbl in ["table_search", "field_search", "fields", "tables"]:
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.commit()

    conn.executescript(_SCHEMA_SQL)
    conn.executescript(_FTS_SQL)
    conn.commit()

    # --- Parse and insert ---
    table_rows: list[tuple[str, str, str, str]] = []
    field_rows: list[tuple[str, str, str, str, bool, str, str]] = []

    for full_name, table_data in tables_dict.items():
        return_obj = table_data.get("returnObj", {})

        # Extract table info from FileSchema
        file_schema = return_obj.get("FileSchema", [])
        if file_schema:
            fs = file_schema[0]
            schema_name = fs.get("SchemaName", "")
            table_name = fs.get("TableName", "")
            description = fs.get("Description", "")
        else:
            # Derive from full_name (e.g., "Erp.POHeader")
            parts = full_name.split(".", 1)
            if len(parts) == 2:
                schema_name, table_name = parts
            else:
                schema_name = ""
                table_name = full_name
            description = ""

        table_rows.append((full_name, schema_name, table_name, description))

        # Extract fields from FieldSchema
        field_schema = return_obj.get("FieldSchema", [])
        for field in field_schema:
            field_name = field.get("FieldName", "")
            if not field_name:
                continue
            data_type = field.get("DataType", "")
            field_label = field.get("FieldLabel", "")
            mandatory = bool(field.get("Mandatory", False))
            field_format = field.get("FieldFormat", "")
            field_desc = field.get("Description", "")

            field_rows.append((
                full_name,
                field_name,
                data_type,
                field_label,
                mandatory,
                field_format,
                field_desc,
            ))

    # --- Bulk insert ---
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tables (full_name, schema_name, table_name, description) "
            "VALUES (?, ?, ?, ?)",
            table_rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO fields "
            "(full_table_name, field_name, data_type, field_label, mandatory, field_format, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            field_rows,
        )

    # --- Build FTS5 indexes ---
    conn.execute(_FTS_POPULATE_TABLES)
    conn.execute(_FTS_POPULATE_FIELDS)
    conn.commit()

    conn.close()

    elapsed = time.monotonic() - t0

    stats = {
        "tables": len(table_rows),
        "fields": len(field_rows),
        "elapsed_seconds": round(elapsed, 2),
    }
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Build the BAQ schema SQLite index from the Epicor data dictionary."
    )
    parser.add_argument(
        "--input",
        default=_DEFAULT_INPUT,
        help="Path to all_schemas.json (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / "data" / "baq_schema.db"),
        help="Output SQLite database path (default: %(default)s)",
    )
    args = parser.parse_args()

    # Validate input
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Input:  {input_path}")
    print(f"Output: {args.output}")
    print()

    stats = build_baq_index(args.input, args.output)

    print("\n=== BAQ Schema Index Build Complete ===")
    print(f"  Tables:   {stats['tables']:,}")
    print(f"  Fields:   {stats['fields']:,}")
    print(f"  Elapsed:  {stats['elapsed_seconds']:.2f}s")
    print(f"  Database: {args.output}")


if __name__ == "__main__":
    main()
