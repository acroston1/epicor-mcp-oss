"""The documented schema imports supply physical SQL preflight on a clean setup."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from epicor_mcp.sql.validate_columns import load_catalogue, load_ud_mirrors, validate_columns

_ROOT = Path(__file__).resolve().parents[1]
_INVENTED_SQL = "select top 5 [P].[InventedColumnXYZ] as [X] from Erp.Part as [P]"


@pytest.fixture
def operator_setup(tmp_path, monkeypatch):
    """Use the documented working directory, without the legacy test catalogue."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPICOR_MCP_COLUMN_CATALOGUE", raising=False)
    monkeypatch.delenv("EPICOR_MCP_SCHEMA_CATALOGUE", raising=False)
    monkeypatch.syspath_prepend(str(_ROOT / "scripts"))
    return tmp_path


def _physical_catalogue(schema="Erp"):
    return {"source": "BAQDesigner GetTableList + GetFieldList (physical SQL metadata)",
            "generated": "2000-01-01T12:00:00Z", "tables": {
                "Part": {"schema": schema, "full_name": f"{schema}.Part", "table_type": "DB",
                         "fields": [{"name": "PartNum", "type": "nvarchar"},
                                    {"name": "UnitPrice", "type": "decimal"},
                                    {"name": "HasOnHandQty", "type": "bit"}]}}}


def _swagger_directory(root):
    exports = root / "swagger"
    exports.mkdir()
    data = {"openapi": "3.0.1", "info": {"title": "Erp.BO.PartSvc", "version": "1"},
            "paths": {"/Parts": {"get": {"responses": {"200": {"description": "OK"}}}}},
            "components": {"schemas": {
                "Erp.Tablesets.PartTableset": {"type": "object", "properties": {
                    "Part": {"type": "array", "items": {"$ref": "#/components/schemas/Erp.Tablesets.PartRow"}}}},
                "Erp.Tablesets.PartRow": {"type": "object", "properties": {
                    "PartNum": {"type": "string"}, "ProjectionOnly": {"type": "string"}}}}}}
    (exports / "Erp.BO.PartSvc.json").write_text(json.dumps(data))
    return exports


@pytest.mark.parametrize("import_method", ["physical_writer", "swagger_with_catalogue"])
def test_documented_import_supplies_default_column_validation(operator_setup, import_method):
    from epicor_mcp.sql import lint
    root = operator_setup
    if import_method == "physical_writer":
        importlib.import_module("build_schema_catalogue").write_catalogue(_physical_catalogue(), Path("data"))
    else:
        source = root / "physical.json"
        source.write_text(json.dumps(_physical_catalogue()))
        importlib.import_module("bootstrap_schema").bootstrap(_swagger_directory(root), Path("data"), source)

    catalogue = load_catalogue()
    assert catalogue.has("Part", "PartNum")
    assert catalogue.column_type("Part", "UnitPrice") == "decimal"
    assert catalogue.column_type("Part", "HasOnHandQty") == "bit"
    assert catalogue.generated == "2000-01-01T12:00:00Z"
    assert lint._schema_index().authoritative, "table lint must use the same imported physical catalogue"
    assert validate_columns("select top 5 [P].[PartNum] as [PartNum] from Erp.Part as [P]").ok
    invalid = validate_columns(_INVENTED_SQL)
    assert not invalid.ok
    assert invalid.envelope["detail"]["stage"] == "validate_columns"
    numeric = validate_columns("select top 5 [P].[PartNum] as [PartNum] from Erp.Part as [P] where [P].[OnHandQty] > 10")
    assert not numeric.ok
    assert numeric.corrected_sql is None, "a numeric quantity must not be repaired to a boolean flag"


def test_swagger_only_bootstrap_cannot_reject_physical_columns(operator_setup):
    importlib.import_module("bootstrap_schema").bootstrap(_swagger_directory(operator_setup), Path("data"))
    assert json.loads(Path("data/schema_catalogue.json").read_text())["source"] == "swagger_projection_unverified"
    assert not load_catalogue().knows_table("Part")
    assert validate_columns(_INVENTED_SQL).ok


def test_swagger_projection_cannot_supply_authoritative_lint_or_ud_rewrites(operator_setup, monkeypatch):
    from epicor_mcp.sql import lint
    exports = _swagger_directory(operator_setup)
    source = exports / "Erp.BO.PartSvc.json"
    document = json.loads(source.read_text())
    schemas = document["components"]["schemas"]
    schemas["Erp.Tablesets.PartRow"]["properties"]["SysRowID"] = {"type": "string", "format": "uuid"}
    schemas["Erp.Tablesets.PartTableset"]["properties"]["Part_UD"] = {
        "type": "array", "items": {"$ref": "#/components/schemas/Erp.Tablesets.Part_UDRow"}}
    schemas["Erp.Tablesets.Part_UDRow"] = {"type": "object", "properties": {
        "ForeignSysRowID": {"type": "string", "format": "uuid"}, "SyntheticFlag_c": {"type": "boolean"}}}
    source.write_text(json.dumps(document))
    importlib.import_module("bootstrap_schema").bootstrap(exports, Path("data"))
    # An explicit path tests source trust separately from cwd default discovery.
    monkeypatch.setenv("EPICOR_MCP_SCHEMA_CATALOGUE", str(operator_setup / "data/schema_catalogue.json"))
    assert not lint._schema_index().authoritative
    assert load_ud_mirrors().mirror_for("Part") is None


def test_ice_catalogue_cannot_supply_an_erp_table_with_the_same_name(operator_setup):
    importlib.import_module("build_schema_catalogue").write_catalogue(_physical_catalogue("Ice"), Path("data"))
    assert not load_catalogue().knows_table("Erp.Part")
    assert validate_columns(_INVENTED_SQL).ok


def test_projection_table_cannot_claim_physical_columns_under_a_generic_source(operator_setup):
    data = _physical_catalogue()
    data["source"] = "operator metadata"
    data["tables"]["Part"]["table_type"] = "BO projection"
    importlib.import_module("build_schema_catalogue").write_catalogue(data, Path("data"))
    assert not load_catalogue().knows_table("Part")
    assert validate_columns(_INVENTED_SQL).ok


@pytest.mark.parametrize("bad_content", [None, "{broken", "[]",
    '{"tables": {"Part": {"schema": "Erp", "fields": "broken"}}}',
    '{"tables": {"Part": {"schema": "Erp", "fields": [{}]}}}',
])
def test_missing_or_malformed_default_metadata_abstains(operator_setup, bad_content):
    if bad_content is not None:
        Path("data").mkdir()
        Path("data/schema_catalogue.json").write_text(bad_content)
    catalogue = load_catalogue()
    assert not catalogue.knows_table("Part")
    assert validate_columns(_INVENTED_SQL).ok


def test_explicit_legacy_override_preserves_typed_physical_metadata(operator_setup, monkeypatch):
    first = operator_setup / "names.json"
    second = operator_setup / "typed.json"
    first.write_text(json.dumps({"tables": {"Part": ["PartNum", "HasOnHandQty"]}}))
    second.write_text(json.dumps({"tables": {"Part": _physical_catalogue()["tables"]["Part"]["fields"]}}))
    monkeypatch.setenv("EPICOR_MCP_COLUMN_CATALOGUE", os.pathsep.join([str(first), str(second)]))
    catalogue = load_catalogue()
    assert catalogue.has("Part", "PartNum")
    assert catalogue.column_type("Part", "HasOnHandQty") == "bit"
    assert not validate_columns(_INVENTED_SQL).ok


def test_schema_catalogue_override_supplies_the_same_imported_metadata(operator_setup, monkeypatch):
    output = operator_setup / "operator_metadata"
    importlib.import_module("build_schema_catalogue").write_catalogue(_physical_catalogue(), output)
    monkeypatch.setenv("EPICOR_MCP_SCHEMA_CATALOGUE", str(output / "schema_catalogue.json"))
    assert load_catalogue().column_type("Part", "UnitPrice") == "decimal"
    assert not validate_columns(_INVENTED_SQL).ok
