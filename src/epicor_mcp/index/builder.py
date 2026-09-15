"""Parse Epicor OpenAPI/Swagger JSON files and department BO lists into a SQLite index.

Usage:
    from epicor_mcp.index.builder import build_index
    stats = build_index(swagger_dir, bo_dir, db_path)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category auto-classification rules
# ---------------------------------------------------------------------------
# Maps (prefix on short_name) -> category.  Checked in order; first match wins.
_CATEGORY_RULES: list[tuple[str, str]] = [
    ("AP", "Accounts Payable"),
    ("AR", "Accounts Receivable"),
    ("GL", "General Ledger"),
    ("Job", "Manufacturing"),
    ("Part", "Inventory"),
    ("PO", "Purchasing"),
    ("SO", "Sales Order"),
    ("Quote", "Quoting"),
    ("RMA", "Returns"),
    ("Receipt", "Receiving"),
    ("Ship", "Shipping"),
    ("Pack", "Shipping"),
    ("BOL", "Shipping"),
    ("Vendor", "Purchasing"),
    ("Customer", "Customer"),
    ("Cust", "Customer"),
    ("Bank", "Banking"),
    ("Cash", "Banking"),
    ("Tax", "Tax"),
    ("Currency", "Currency"),
    ("Asset", "Fixed Assets"),
    ("Budget", "Budgeting"),
    ("Alloc", "Allocations"),
    ("Alc", "Allocations"),
    ("Insp", "Quality"),
    ("NonConf", "Quality"),
    ("Corrective", "Quality"),
    ("DMR", "Quality"),
    ("Emp", "Human Resources"),
    ("Payroll", "Human Resources"),
    ("Labor", "Labor"),
    ("Time", "Labor"),
    ("Project", "Projects"),
    ("Forecast", "Forecasting"),
    ("MRP", "Planning"),
    ("Schedule", "Scheduling"),
    ("ECO", "Engineering"),
    ("EngWorkBench", "Engineering"),
    ("BOM", "Engineering"),
    ("Bom", "Engineering"),
    ("Config", "Configuration"),
    ("Warehouse", "Warehouse"),
    ("Whse", "Warehouse"),
    ("Bin", "Warehouse"),
    ("Transfer", "Warehouse"),
    ("Inventory", "Inventory"),
    ("Count", "Inventory"),
    ("Consol", "Consolidation"),
    ("Journal", "General Ledger"),
    ("TranGLC", "General Ledger"),
    ("COA", "General Ledger"),
    ("Ledger", "General Ledger"),
    ("Report", "Reporting"),
    ("Rpt", "Reporting"),
    ("Dashboard", "Reporting"),
    ("Alert", "System"),
    ("Memo", "System"),
    ("Menu", "System"),
    ("Security", "System"),
    ("UserFile", "System"),
    ("Company", "System"),
    ("Plant", "System"),
    ("Site", "System"),
    ("BAQ", "System"),
    ("DynamicQuery", "System"),
    ("Attachment", "System"),
    ("Ice", "System"),
]


def _categorize(prefix: str, short_name: str) -> str:
    """Return an auto-categorized label based on prefix and short_name."""
    if prefix.startswith("Ice"):
        return "System"
    for pattern, category in _CATEGORY_RULES:
        if short_name.startswith(pattern):
            return category
    return "General"


# ---------------------------------------------------------------------------
# OData type mapping from OpenAPI types
# ---------------------------------------------------------------------------
_TYPE_MAP: dict[tuple[str, str | None], str] = {
    ("string", None): "Edm.String",
    ("string", "uuid"): "Edm.Guid",
    ("string", "date-time"): "Edm.DateTimeOffset",
    ("string", "date"): "Edm.Date",
    ("string", "byte"): "Edm.Binary",
    ("integer", "int32"): "Edm.Int32",
    ("integer", "int64"): "Edm.Int64",
    ("integer", None): "Edm.Int32",
    ("number", "double"): "Edm.Double",
    ("number", "decimal"): "Edm.Decimal",
    ("number", "float"): "Edm.Single",
    ("number", None): "Edm.Double",
    ("boolean", None): "Edm.Boolean",
}


def _map_type(prop: dict[str, Any]) -> str:
    """Convert an OpenAPI property schema to an OData-style type string."""
    base = prop.get("type", "string")
    fmt = prop.get("format")
    return _TYPE_MAP.get((base, fmt), _TYPE_MAP.get((base, None), f"{base}"))


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS services (
    service_id TEXT PRIMARY KEY,
    prefix TEXT,
    short_name TEXT,
    description TEXT,
    category TEXT,
    path_count INTEGER,
    method_count INTEGER
);

CREATE TABLE IF NOT EXISTS methods (
    service_id TEXT,
    method_name TEXT,
    http_method TEXT,
    path TEXT,
    summary TEXT,
    description TEXT,
    is_read_only BOOLEAN,
    has_dataset_param BOOLEAN,
    PRIMARY KEY (service_id, method_name),
    FOREIGN KEY (service_id) REFERENCES services(service_id)
);

CREATE TABLE IF NOT EXISTS entity_sets (
    service_id TEXT,
    entity_set_name TEXT,
    PRIMARY KEY (service_id, entity_set_name),
    FOREIGN KEY (service_id) REFERENCES services(service_id)
);

CREATE TABLE IF NOT EXISTS fields (
    service_id TEXT,
    entity_set_name TEXT,
    field_name TEXT,
    field_type TEXT,
    nullable BOOLEAN DEFAULT TRUE,
    description TEXT,
    PRIMARY KEY (service_id, entity_set_name, field_name)
);

CREATE TABLE IF NOT EXISTS department_services (
    department TEXT,
    service_id TEXT,
    description TEXT,
    PRIMARY KEY (department, service_id)
);

CREATE INDEX IF NOT EXISTS idx_methods_service ON methods(service_id);
CREATE INDEX IF NOT EXISTS idx_entity_sets_service ON entity_sets(service_id);
CREATE INDEX IF NOT EXISTS idx_fields_service_entity ON fields(service_id, entity_set_name);
CREATE INDEX IF NOT EXISTS idx_dept_services_dept ON department_services(department);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS service_search USING fts5(
    service_id,
    short_name,
    description,
    category,
    content='services',
    content_rowid='rowid',
    tokenize='porter unicode61'
);
"""

_FTS_POPULATE = """
INSERT INTO service_search(rowid, service_id, short_name, description, category)
    SELECT rowid, service_id, short_name, description, category FROM services;
"""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_service_id(info: dict[str, Any]) -> str:
    """Extract the service_id from the OpenAPI info block."""
    return info.get("title", "").strip()


def _split_service_id(service_id: str) -> tuple[str, str]:
    """Split 'Erp.BO.APInvoiceSvc' into ('Erp.BO', 'APInvoice').

    Removes trailing 'Svc' from the last segment to produce the short_name.
    """
    parts = service_id.rsplit(".", 1)
    if len(parts) == 2:
        prefix = parts[0]
        raw = parts[1]
    else:
        prefix = ""
        raw = service_id
    short_name = raw[:-3] if raw.endswith("Svc") else raw
    return prefix, short_name


def _extract_method_name(path: str, http_method: str, endpoint: dict[str, Any]) -> str | None:
    """Derive a unique method_name from a path endpoint.

    For POST endpoints we prefer operationId.
    For GET endpoints we also use operationId (stripping the 'Get_' prefix that
    Epicor adds for the GET variant of a POST method).
    Returns None if no sensible name can be derived (skip the entry).
    """
    op_id = endpoint.get("operationId", "")
    if op_id:
        # GET variants duplicate POST with a 'Get_' prefix — skip them
        if http_method == "get" and op_id.startswith("Get_"):
            return None
        return op_id
    # Fallback: last path segment
    segment = path.rsplit("/", 1)[-1]
    return segment or None


def _has_ds_param(method_name: str, schemas: dict[str, Any]) -> bool:
    """Check whether the *_input schema for a method contains a 'ds' property."""
    input_key = f"{method_name}_input"
    input_schema = schemas.get(input_key, {})
    props = input_schema.get("properties", {})
    return "ds" in props


def _is_read_only(method_name: str) -> bool:
    """A method is read-only if it starts with 'Get' but NOT 'GetNew'."""
    return method_name.startswith("Get") and not method_name.startswith("GetNew")


def _extract_entity_sets_from_schemas(schemas: dict[str, Any], service_id: str) -> dict[str, str]:
    """Discover entity sets by inspecting the main Tableset schema.

    Returns {entity_set_name: row_schema_key} for each entity set found.
    The main tableset is typically named like 'Erp.Tablesets.<ShortName>Tableset'.
    We skip the ListTableset and UpdExtTableset variants.
    """
    entity_map: dict[str, str] = {}
    prefix_dot = service_id.rsplit(".", 1)[0] if "." in service_id else ""

    for schema_name, schema_def in schemas.items():
        # Look for the primary Tableset (not List or UpdExt variants)
        if not schema_name.endswith("Tableset"):
            continue
        if "ListTableset" in schema_name or "UpdExt" in schema_name:
            continue
        # Must look like a Tablesets schema
        if ".Tablesets." not in schema_name:
            continue

        props = schema_def.get("properties", {})
        for prop_name, prop_def in props.items():
            if prop_name == "ExtensionTables":
                continue
            # prop_name is the entity set name (e.g. "ABCCode", "Vendors")
            # The items $ref points to the Row schema
            items = prop_def.get("items", {})
            ref = items.get("$ref", "")
            row_schema = ref.split("/")[-1] if ref else ""
            if row_schema:
                entity_map[prop_name] = row_schema

    return entity_map


def _extract_fields_from_row_schema(
    schema_def: dict[str, Any],
) -> list[tuple[str, str, bool, str]]:
    """Extract fields from a Row schema.

    Returns list of (field_name, field_type, nullable, description).
    """
    fields: list[tuple[str, str, bool, str]] = []
    props = schema_def.get("properties", {})
    for field_name, field_def in props.items():
        if field_name in ("RowMod", "BitFlag"):
            continue
        field_type = _map_type(field_def)
        nullable = field_def.get("nullable", True)
        desc = field_def.get("description", "")
        fields.append((field_name, field_type, nullable, desc))
    return fields


# ---------------------------------------------------------------------------
# Parse a single swagger JSON
# ---------------------------------------------------------------------------

def _parse_swagger_file(
    filepath: Path,
) -> tuple[
    dict[str, Any] | None,  # service row
    list[dict[str, Any]],   # method rows
    list[str],               # entity_set names
    list[tuple[str, str, str, str, bool, str]],  # field rows (entity_set, field, type, nullable, desc)
] | None:
    """Parse one OpenAPI JSON file.  Returns None on failure."""
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Skipping %s: %s", filepath.name, exc)
        return None

    info = data.get("info", {})
    service_id = _parse_service_id(info)
    if not service_id:
        logger.warning("Skipping %s: no info.title", filepath.name)
        return None

    description = info.get("description", "")
    prefix, short_name = _split_service_id(service_id)
    category = _categorize(prefix, short_name)

    paths = data.get("paths", {})
    schemas = data.get("components", {}).get("schemas") or data.get("definitions", {})

    # --- Methods ---
    methods: list[dict[str, Any]] = []
    seen_methods: set[str] = set()
    for path_str, path_item in paths.items():
        for http_method in ("get", "post", "put", "patch", "delete"):
            endpoint = path_item.get(http_method)
            if endpoint is None:
                continue
            method_name = _extract_method_name(path_str, http_method, endpoint)
            if method_name is None:
                continue
            if method_name in seen_methods:
                continue
            seen_methods.add(method_name)

            methods.append({
                "service_id": service_id,
                "method_name": method_name,
                "http_method": http_method,
                "path": path_str,
                "summary": endpoint.get("summary", ""),
                "description": endpoint.get("description", ""),
                "is_read_only": _is_read_only(method_name),
                "has_dataset_param": _has_ds_param(method_name, schemas),
            })

    # --- Entity sets and fields from schemas ---
    entity_map = _extract_entity_sets_from_schemas(schemas, service_id)
    entity_set_names = list(entity_map.keys())

    field_rows: list[tuple[str, str, str, str, bool, str]] = []
    for es_name, row_schema_key in entity_map.items():
        row_schema = schemas.get(row_schema_key, {})
        if not row_schema:
            continue
        for field_name, field_type, nullable, desc in _extract_fields_from_row_schema(row_schema):
            field_rows.append((service_id, es_name, field_name, field_type, nullable, desc))

    service_row = {
        "service_id": service_id,
        "prefix": prefix,
        "short_name": short_name,
        "description": description,
        "category": category,
        "path_count": len(paths),
        "method_count": len(methods),
    }

    return service_row, methods, entity_set_names, field_rows


# ---------------------------------------------------------------------------
# Department BO list parsing
# ---------------------------------------------------------------------------

_BO_LINE_RE = re.compile(r"^([\w.]+)\s*[—–-]\s*(.+)$")

_DEPT_FILE_MAP: dict[str, str] = {
    "finance_bos.txt": "Finance",
    "engineering_bos.txt": "Engineering",
    "hr_bos.txt": "HR",
    "purchasing_bos.txt": "Purchasing",
    "production_bos.txt": "Production",
    "shipping_bos.txt": "Shipping",
}

_COMMON_BO_FILE = "common_bos.txt"


def _parse_bo_file(filepath: Path) -> list[tuple[str, str]]:
    """Parse a single BO list file.

    Returns list of (service_id, description).
    """
    entries: list[tuple[str, str]] = []
    if not filepath.exists():
        logger.warning("BO list not found: %s", filepath)
        return entries
    with open(filepath, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _BO_LINE_RE.match(line)
            if m:
                entries.append((m.group(1).strip(), m.group(2).strip()))
    return entries


def _parse_bo_lists(bo_dir: Path) -> list[tuple[str, str, str]]:
    """Parse all department BO list files plus the common file.

    common_bos.txt entries are added to every department.
    Returns list of (department, service_id, description).
    """
    rows: list[tuple[str, str, str]] = []

    # Load common services (shared across all departments)
    common_entries = _parse_bo_file(bo_dir / _COMMON_BO_FILE)
    if common_entries:
        logger.info("Loaded %d common services from %s", len(common_entries), _COMMON_BO_FILE)

    all_depts = list(_DEPT_FILE_MAP.values())

    for filename, dept in _DEPT_FILE_MAP.items():
        for svc_id, desc in _parse_bo_file(bo_dir / filename):
            rows.append((dept, svc_id, desc))

    # Add common entries to every department
    for svc_id, desc in common_entries:
        for dept in all_depts:
            rows.append((dept, svc_id, desc))

    return rows


# ---------------------------------------------------------------------------
# Entity sets JSON import
# ---------------------------------------------------------------------------

_ENTITY_DIR_MAP: dict[str, str] = {
    "Finance": "Finance",
    "Engineering": "Engineering",
    "HR": "HR",
    "Production": "Production",
    "Purchasing": "Purchasing",
    "Shipping": "Shipping",
}


def _load_entity_sets_json(bo_dir: Path) -> dict[str, list[str]]:
    """Load entity_sets.json from each department directory.

    Returns {service_id: [entity_set_name, ...]}, merged across departments.
    """
    merged: dict[str, list[str]] = {}
    for dept_dir_name in _ENTITY_DIR_MAP.values():
        filepath = bo_dir / dept_dir_name / "entity_sets.json"
        if not filepath.exists():
            logger.debug("No entity_sets.json in %s", dept_dir_name)
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", filepath, exc)
            continue
        for svc_id, es_list in data.items():
            if svc_id not in merged:
                merged[svc_id] = []
            for es_name in es_list:
                if es_name not in merged[svc_id]:
                    merged[svc_id].append(es_name)
    return merged


# ---------------------------------------------------------------------------
# Field reference file parsing
# ---------------------------------------------------------------------------

_FIELD_HEADER_RE = re.compile(r"^##\s+(\w+)\s+\((\d+)\s+fields?\)")
_FIELD_LINE_RE = re.compile(r"^(\w+):(\w+)$")


def _load_field_references(bo_dir: Path) -> list[tuple[str, str, str, str, bool, str]]:
    """Parse all field_reference/*.txt files across department directories.

    Returns list of (service_id, entity_set_name, field_name, field_type, nullable, description).
    The field_reference files use simplified types (string, integer, number, boolean)
    which we map to OData types.
    """
    simple_type_map = {
        "string": "Edm.String",
        "integer": "Edm.Int32",
        "number": "Edm.Double",
        "boolean": "Edm.Boolean",
    }
    rows: list[tuple[str, str, str, str, bool, str]] = []
    seen_dirs: set[str] = set()

    for dept_dir_name in _ENTITY_DIR_MAP.values():
        field_dir = bo_dir / dept_dir_name / "field_reference"
        if not field_dir.exists() or not field_dir.is_dir():
            continue
        # Avoid re-processing if the same physical directory
        resolved = str(field_dir.resolve())
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)

        for txt_file in sorted(field_dir.glob("*.txt")):
            # Service ID from filename (e.g., Erp.BO.PartSvc.txt -> Erp.BO.PartSvc)
            service_id = txt_file.stem
            current_entity: str | None = None

            with open(txt_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip()
                    if not line or line.startswith("#"):
                        continue
                    # Check for entity set header
                    hdr = _FIELD_HEADER_RE.match(line)
                    if hdr:
                        current_entity = hdr.group(1)
                        continue
                    # Check for field line
                    if current_entity is not None:
                        fld = _FIELD_LINE_RE.match(line)
                        if fld:
                            fname = fld.group(1)
                            ftype_raw = fld.group(2)
                            ftype = simple_type_map.get(ftype_raw, f"Edm.{ftype_raw.capitalize()}")
                            rows.append((service_id, current_entity, fname, ftype, True, ""))
    return rows


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_index(
    swagger_dir: str | Path,
    bo_dir: str | Path | None,
    db_path: str | Path,
) -> dict[str, int]:
    """Build the SQLite service index from swagger JSONs and department files.

    Parameters
    ----------
    swagger_dir : path to directory containing *.json OpenAPI files
    bo_dir : optional directory of department BO lists and entity/field metadata;
        filenames and JSON formats are defined by the loaders in this module
    db_path : output SQLite database path

    Returns
    -------
    dict with summary statistics
    """
    swagger_dir = Path(swagger_dir)
    bo_dir = Path(bo_dir) if bo_dir is not None else None
    db_path = Path(db_path)

    # Ensure output directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()

    # --- Connect and create schema (idempotent: drop and recreate) ---
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Drop existing tables for idempotent rebuild
    for table in [
        "service_search", "department_services", "fields",
        "entity_sets", "methods", "services",
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()

    conn.executescript(_SCHEMA_SQL)
    conn.executescript(_FTS_SQL)
    conn.commit()

    # --- Parse swagger files ---
    json_files = sorted(swagger_dir.glob("*.json"))
    logger.info("Found %d swagger JSON files", len(json_files))

    all_services: list[dict[str, Any]] = []
    all_methods: list[dict[str, Any]] = []
    all_entity_sets: list[tuple[str, str]] = []  # (service_id, entity_set_name)
    all_fields: list[tuple[str, str, str, str, bool, str]] = []
    skipped = 0

    for jf in json_files:
        result = _parse_swagger_file(jf)
        if result is None:
            skipped += 1
            continue
        svc_row, methods, es_names, field_rows = result
        all_services.append(svc_row)
        all_methods.extend(methods)
        for es in es_names:
            all_entity_sets.append((svc_row["service_id"], es))
        all_fields.extend(field_rows)

    # --- Merge entity sets from JSON files ---
    ext_entity_sets = _load_entity_sets_json(bo_dir) if bo_dir is not None else {}
    existing_es: set[tuple[str, str]] = set(all_entity_sets)
    for svc_id, es_list in ext_entity_sets.items():
        for es_name in es_list:
            key = (svc_id, es_name)
            if key not in existing_es:
                all_entity_sets.append(key)
                existing_es.add(key)

    # --- Load field references ---
    field_ref_rows = _load_field_references(bo_dir) if bo_dir is not None else []
    # Merge: only add fields not already present from swagger schemas
    existing_fields: set[tuple[str, str, str]] = {
        (r[0], r[1], r[2]) for r in all_fields
    }
    for row in field_ref_rows:
        key = (row[0], row[1], row[2])
        if key not in existing_fields:
            all_fields.append(row)
            existing_fields.add(key)

    # --- Bulk insert into SQLite ---
    with conn:
        # Services
        conn.executemany(
            "INSERT OR REPLACE INTO services (service_id, prefix, short_name, description, category, path_count, method_count) "
            "VALUES (:service_id, :prefix, :short_name, :description, :category, :path_count, :method_count)",
            all_services,
        )

        # Methods
        conn.executemany(
            "INSERT OR REPLACE INTO methods (service_id, method_name, http_method, path, summary, description, is_read_only, has_dataset_param) "
            "VALUES (:service_id, :method_name, :http_method, :path, :summary, :description, :is_read_only, :has_dataset_param)",
            all_methods,
        )

        # Entity sets
        conn.executemany(
            "INSERT OR REPLACE INTO entity_sets (service_id, entity_set_name) VALUES (?, ?)",
            all_entity_sets,
        )

        # Fields
        conn.executemany(
            "INSERT OR REPLACE INTO fields (service_id, entity_set_name, field_name, field_type, nullable, description) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            all_fields,
        )

    # --- Department BO lists ---
    dept_rows = _parse_bo_lists(bo_dir) if bo_dir is not None else []
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO department_services (department, service_id, description) VALUES (?, ?, ?)",
            dept_rows,
        )

    # --- Build FTS5 index ---
    conn.execute("INSERT INTO service_search(service_search) VALUES('rebuild')")
    conn.commit()

    conn.close()

    elapsed = time.monotonic() - t0

    stats = {
        "services": len(all_services),
        "methods": len(all_methods),
        "entity_sets": len(all_entity_sets),
        "fields": len(all_fields),
        "department_mappings": len(dept_rows),
        "skipped_files": skipped,
        "elapsed_seconds": round(elapsed, 2),
    }

    return stats


# ---------------------------------------------------------------------------
# CLI entry point (registered as build-index console script)
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the ``build-index`` console script."""
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Build the Epicor service SQLite index")
    parser.add_argument(
        "--swagger-dir",
        required=True,
        help="Directory containing OpenAPI/Swagger JSON files",
    )
    parser.add_argument(
        "--bo-dir",
        default=None,
        help="Directory containing department BO lists and entity set data",
    )
    parser.add_argument(
        "--output",
        default="data/service_index.db",
        help="Output SQLite database path",
    )
    args = parser.parse_args()

    stats = build_index(args.swagger_dir, args.bo_dir, args.output)

    print("\n=== Service Index Build Complete ===")
    print(f"  Services:            {stats['services']:,}")
    print(f"  Methods:             {stats['methods']:,}")
    print(f"  Entity sets:         {stats['entity_sets']:,}")
    print(f"  Fields:              {stats['fields']:,}")
    print(f"  Department mappings: {stats['department_mappings']:,}")
    print(f"  Skipped files:       {stats['skipped_files']:,}")
    print(f"  Elapsed:             {stats['elapsed_seconds']:.2f}s")
    print(f"  Output:              {args.output}")


if __name__ == "__main__":
    main()
