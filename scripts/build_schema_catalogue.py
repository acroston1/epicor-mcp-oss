#!/usr/bin/env python3
"""Import the physical SQL catalogue from your Epicor BAQDesigner read methods.

Calls GetTableList and GetFieldList using your configured service account and
BAQ API key. This is an explicit, read-only network operation; it never saves a
BAQ or changes ERP records. Run only against a server you administer. This is
the recommended metadata source for SQL table/field discovery.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from epicor_mcp.auth.credentials import CredentialManager
from epicor_mcp.config import Settings
from epicor_mcp.epicor_client.http_client import EpicorClient
from bootstrap_schema import to_dictionary, validate_catalogue
from build_baq_index import build_baq_index
from build_discovery_index import build_discovery_index

TABLE_LIST_PATH = "Ice.BO.BAQDesignerSvc/GetTableList"
FIELD_LIST_PATH = "Ice.BO.BAQDesignerSvc/GetFieldList"


def _clean(value: str | None) -> str:
    return " ".join(str(value or "").split())


async def fetch_catalogue(settings: Settings, *, concurrency: int = 4,
                          tables: list[str] | None = None) -> dict:
    """Read physical metadata, refusing a partial result on any API error."""
    if not 1 <= concurrency <= 8:
        raise ValueError("concurrency must be between 1 and 8")
    credentials = CredentialManager(settings)
    credentials.load()
    key = credentials.get_baq_key()
    if not key or not credentials.service_username or not credentials.service_password:
        raise ValueError("Set EPICOR_MCP_EPICOR_USERNAME, EPICOR_PASSWORD and EPICOR_BAQ_API_KEY (or EPICOR_API_KEY)")
    base = credentials.get_base_url().rstrip("/")
    client = EpicorClient(username=credentials.service_username, password=credentials.service_password,
                          company_id=settings.epicor_company_id, timeout=120.0)
    try:
        response = await client.post(f"{base}/{TABLE_LIST_PATH}", key, json_body={})
        rows = (response.get("returnObj") or {}).get("FullTableList")
        if not isinstance(rows, list) or not rows:
            raise ValueError("GetTableList returned no FullTableList; verify your Epicor API version and permissions")
        if tables:
            wanted = {table.casefold() for table in tables}
            rows = [row for row in rows if str(row.get("TableID", "")).casefold() in wanted]
            missing = wanted - {str(row.get("TableID", "")).casefold() for row in rows}
            if missing:
                raise ValueError(f"Requested tables were not returned by GetTableList: {', '.join(sorted(missing))}")
        sem = asyncio.Semaphore(concurrency)

        async def one(row: dict) -> tuple[str, dict]:
            name, schema = row["TableID"], row.get("DBSchemaName") or "Erp"
            async with sem:
                response = await client.post(f"{base}/{FIELD_LIST_PATH}", key,
                                              json_body={"dataTableID": name, "schema": schema})
            fields = (response.get("returnObj") or {}).get("TableFieldList")
            if not isinstance(fields, list) or not fields:
                raise ValueError(f"GetFieldList returned no fields for {schema}.{name}; existing catalogue retained")
            return name, {"schema": schema, "full_name": f"{schema}.{name}",
                          "description": _clean(row.get("Description")),
                          "table_type": row.get("TableType") or "DB", "fields": [
                {"name": field["FieldName"], "type": field.get("DataType", ""),
                 "description": _clean(field.get("Description")), "label": _clean(field.get("FieldLabel")),
                 "required": bool(field.get("Required")), "nullable": bool(field.get("IsNullable")),
                 "like_table": field.get("LikeDataFieldTableID") or "",
                 "like_field": field.get("LikeDataFieldName") or ""}
                for field in fields if field.get("FieldName")]}

        pairs = await asyncio.gather(*(one(row) for row in rows))
    finally:
        await client.close()
    if len({name.casefold() for name, _ in pairs}) != len(pairs):
        raise ValueError("Duplicate unqualified table names across schemas; curate an explicit catalogue before import")
    result = {"source": "BAQDesigner GetTableList + GetFieldList (physical SQL metadata)",
              "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
              "tables": dict(pairs), "table_count": len(pairs),
              "field_count": sum(len(table["fields"]) for _, table in pairs)}
    return validate_catalogue(result)


def write_catalogue(catalogue: dict, output_dir: Path) -> None:
    """Materialize the same local indexes used by offline bootstrap_schema.py."""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "schema_catalogue.json"
    # Read failures happen before touching previous outputs.
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    dictionary = output_dir / "all_schemas.json"
    dictionary.write_text(json.dumps(to_dictionary(catalogue), ensure_ascii=False), encoding="utf-8")
    build_baq_index(dictionary, output_dir / "baq_schema.db")
    build_discovery_index(catalogue, output_dir / "discovery_index")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--tables", help="Optional comma-separated unqualified table names; writes only that selection")
    args = parser.parse_args()
    selected = [name.strip() for name in args.tables.split(",") if name.strip()] if args.tables else None
    catalogue = asyncio.run(fetch_catalogue(Settings(), concurrency=args.concurrency, tables=selected))
    write_catalogue(catalogue, args.output_dir)
    print(json.dumps({"tables": catalogue["table_count"], "fields": catalogue["field_count"],
                      "output": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
