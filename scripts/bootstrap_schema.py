#!/usr/bin/env python3
"""Build local schema metadata from administrator-supplied Swagger exports.

--catalogue accepts authoritative SQL metadata. Without it, discovery explicitly
labels tables/fields as unverified BO projections; Swagger cannot prove that a
field exists in a physical SQL table. Only import exports you are entitled to use.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from epicor_mcp.index.builder import build_index
from build_baq_index import build_baq_index
from build_discovery_index import build_discovery_index

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPES = {"Edm.Int32": "int", "Edm.Int64": "bigint", "Edm.Int16": "smallint",
          "Edm.String": "nvarchar", "Edm.Boolean": "bit", "Edm.Decimal": "decimal",
          "Edm.Double": "float", "Edm.DateTimeOffset": "datetimeoffset",
          "Edm.DateTime": "datetime", "Edm.Guid": "uniqueidentifier", "Edm.Binary": "varbinary"}


def catalogue_from_services(database: Path) -> dict:
    conn = sqlite3.connect(database)
    tables: dict = {}
    try:
        for service, entity, field, data_type, nullable, description in conn.execute(
                "SELECT service_id, entity_set_name, field_name, field_type, nullable, description "
                "FROM fields ORDER BY service_id, entity_set_name, field_name"):
            schema = service.split(".")[0]
            if schema not in {"Erp", "Ice"} or not _NAME.fullmatch(entity):
                continue
            table = tables.setdefault(entity, {"schema": schema, "full_name": f"{schema}.{entity}",
                   "description": "Swagger BO projection; physical SQL table and columns are unverified.",
                   "table_type": "BO projection", "fields": []})
            if table["schema"] != schema:
                raise ValueError(f"Ambiguous unqualified table {entity}; supply an authoritative --catalogue")
            if not any(existing["name"].casefold() == field.casefold() for existing in table["fields"]):
                table["fields"].append({"name": field, "type": _TYPES.get(data_type, data_type),
                                        "description": description or "", "label": "",
                                        "required": not bool(nullable)})
    finally:
        conn.close()
    return {"source": "swagger_projection_unverified", "tables": tables}


def validate_catalogue(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise ValueError("Catalogue must be an object containing a tables object")
    seen = set()
    for name, table in data["tables"].items():
        if not _NAME.fullmatch(name) or name.casefold() in seen:
            raise ValueError(f"Invalid or duplicate unqualified table name: {name!r}")
        seen.add(name.casefold())
        if not isinstance(table, dict) or table.get("schema") not in {"Erp", "Ice"}:
            raise ValueError(f"{name}: schema must be Erp or Ice")
        if table.get("full_name", f"{table['schema']}.{name}") != f"{table['schema']}.{name}":
            raise ValueError(f"{name}: full_name must match schema and table name")
        fields = table.get("fields")
        if not isinstance(fields, list):
            raise ValueError(f"{name}: fields must be a list")
        names = set()
        for field in fields:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str) or not _NAME.fullmatch(field["name"]):
                raise ValueError(f"{name}: invalid field name")
            if field["name"].casefold() in names:
                raise ValueError(f"{name}: duplicate field {field['name']}")
            names.add(field["name"].casefold())
    return data


def to_dictionary(catalogue: dict) -> dict:
    result = {"tables": {}}
    for name, table in catalogue["tables"].items():
        schema = table["schema"]
        result["tables"][f"{schema}.{name}"] = {"returnObj": {
            "FileSchema": [{"SchemaName": schema, "TableName": name, "Description": table.get("description", "")}],
            "FieldSchema": [{"FieldName": field["name"], "DataType": field.get("type", ""),
                             "FieldLabel": field.get("label", ""), "Mandatory": bool(field.get("required")),
                             "Description": field.get("description", "")} for field in table["fields"]]}}
    return result


def bootstrap(swagger_dir: Path, output_dir: Path, catalogue_path: Path | None = None) -> dict:
    if not swagger_dir.is_dir() or not list(swagger_dir.glob("*.json")):
        raise ValueError("swagger-dir must contain Epicor OpenAPI 3 / Swagger 2 JSON exports")
    catalogue = None
    if catalogue_path:
        catalogue = validate_catalogue(json.loads(catalogue_path.read_text(encoding="utf-8")))
        catalogue.setdefault("source", "administrator physical SQL catalogue")
    output_dir.mkdir(parents=True, exist_ok=True)
    stats = build_index(swagger_dir, None, output_dir / "service_index.db")
    if not stats["services"]:
        raise ValueError("No services found; exports need info.title, paths, and components.schemas or definitions")
    if catalogue is None:
        catalogue = validate_catalogue(catalogue_from_services(output_dir / "service_index.db"))
    if not catalogue["tables"]:
        raise ValueError("No table fields found in Swagger exports; supply --catalogue with physical metadata")
    catalogue["table_count"] = len(catalogue["tables"])
    catalogue["field_count"] = sum(len(row["fields"]) for row in catalogue["tables"].values())
    (output_dir / "schema_catalogue.json").write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dictionary = output_dir / "all_schemas.json"
    dictionary.write_text(json.dumps(to_dictionary(catalogue), ensure_ascii=False), encoding="utf-8")
    build_baq_index(dictionary, output_dir / "baq_schema.db")
    discovery = build_discovery_index(catalogue, output_dir / "discovery_index")
    return {"service_index": stats, "discovery": discovery}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swagger-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--catalogue", type=Path, help="Optional authoritative physical table/column JSON")
    args = parser.parse_args()
    print(json.dumps(bootstrap(args.swagger_dir, args.output_dir, args.catalogue), indent=2))


if __name__ == "__main__":
    main()
