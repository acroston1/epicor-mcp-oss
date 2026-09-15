"""Optional semantic table/field discovery.

Build the discovery index WITH vectors from a chosen provider, verify the build
at load, use it only when the configuration matches, and say why whenever the
ranking is substring instead. Everything is synthetic: a deterministic
bag-of-words "encoder" stands in for sentence-transformers, and a recording
stub stands in for the embeddings endpoint. No model is downloaded, no network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]

CATALOGUE = {"tables": {
    "POHeader": {"schema": "Erp", "full_name": "Erp.POHeader",
                 "description": "Purchase order header sent to a supplier",
                 "fields": [{"name": "PONum", "type": "int", "description": "Purchase order number"},
                            {"name": "VendorNum", "type": "int", "description": "Supplier number"}]},
    "OrderHed": {"schema": "Erp", "full_name": "Erp.OrderHed",
                 "description": "Sales order header from a customer",
                 "fields": [{"name": "OrderNum", "type": "int", "description": "Sales order number"},
                            {"name": "CustNum", "type": "int", "description": "Customer number"}]},
}}
VOCAB = ["supplier", "vendor", "purchase", "customer", "sales", "order"]


def _vec(text: str) -> np.ndarray:
    low = text.lower()
    raw = np.asarray([float(word in low) for word in VOCAB] + [0.5], dtype=np.float32)
    return raw / np.linalg.norm(raw)


def _builder():
    spec = importlib.util.spec_from_file_location("discovery_builder", REPO / "scripts/build_discovery_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_sentence_transformers(monkeypatch, calls: list):
    class Encoder:
        def __init__(self, model, device="cpu", local_files_only=False):
            calls.append(("load", model, local_files_only))

        def encode(self, texts, **kwargs):
            calls.append(("encode", list(texts)))
            return np.stack([_vec(text) for text in texts])

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = Encoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return Encoder


def _built_with_local_model(tmp_path, monkeypatch):
    from epicor_mcp.discovery.embeddings import LocalEmbedder
    calls: list = []
    _fake_sentence_transformers(monkeypatch, calls)
    output = tmp_path / "index"
    manifest = _builder().build_discovery_index(CATALOGUE, output, LocalEmbedder("synthetic-model"))
    return output, manifest, calls


def _settings(index_path, tmp_path, **overrides):
    values = dict(discovery_index_path=index_path, docs_db_path=tmp_path / "missing.db",
                  document_vectors_path=tmp_path / "vectors", vector_search_enabled=False,
                  embedding_model="", embed_endpoint="", embed_model_name="", embed_dim=2048)
    values.update(overrides)
    return SimpleNamespace(**values)


def _register(index_path, tmp_path, **overrides):
    from mcp.server.fastmcp import FastMCP
    from epicor_mcp.index.local_retrieval import register_local_retrieval
    from epicor_mcp.rbac.table_whitelist import NoneTableAuthorizer, TableWhitelist
    allow = tmp_path / "tables.txt"
    allow.write_text("Erp.POHeader\nErp.OrderHed\n")
    mcp = FastMCP("test")
    resources = register_local_retrieval(mcp, _settings(index_path, tmp_path, **overrides),
                                         NoneTableAuthorizer(TableWhitelist.from_file(allow)))
    return mcp, resources


async def _tables(mcp, query):
    return await mcp._tool_manager.get_tool("epicor_tables").fn(query=query)


async def _fields(mcp, table, query):
    return await mcp._tool_manager.get_tool("epicor_fields").fn(table=table, query=query)


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def test_build_with_a_local_model_records_provider_model_and_dimension(tmp_path, monkeypatch):
    from epicor_mcp.discovery.store import DiscoveryIndex
    output, manifest, calls = _built_with_local_model(tmp_path, monkeypatch)
    assert manifest["semantic"] is True
    assert manifest["provider"] == "sentence-transformers"
    assert manifest["model"] == "synthetic-model"
    assert manifest["dim"] == len(VOCAB) + 1
    assert (output / "fields.npy").is_file() and (output / "tables.npy").is_file()
    assert ("load", "synthetic-model", True) in calls, "the build must respect local_files_only"
    index = DiscoveryIndex.load(output)
    assert index.semantic_error == ""
    assert index.dim == len(VOCAB) + 1
    index.close()


def test_metadata_only_rebuild_removes_the_previous_vectors(tmp_path, monkeypatch):
    output, _manifest, _calls = _built_with_local_model(tmp_path, monkeypatch)
    manifest = _builder().build_discovery_index(CATALOGUE, output)
    assert manifest["semantic"] is False
    assert not (output / "fields.npy").exists() and not (output / "tables.npy").exists()


def test_build_script_exposes_both_providers_and_builds_through_argv(tmp_path, monkeypatch, capsys):
    calls: list = []
    _fake_sentence_transformers(monkeypatch, calls)
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text(json.dumps(CATALOGUE))
    output = tmp_path / "index"
    builder = _builder()
    monkeypatch.setattr(sys, "argv", ["build_discovery_index.py", "--input", str(catalogue), "--output",
                                      str(output), "--model", "synthetic-model", "--local-files-only"])
    builder.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["semantic"] is True and printed["model"] == "synthetic-model"
    monkeypatch.setattr(sys, "argv", ["build_discovery_index.py", "--endpoint", "http://127.0.0.1:1/v1/embeddings"])
    with pytest.raises(SystemExit):
        builder.main()  # --endpoint without --model is a usage error, never a silent metadata build


# --------------------------------------------------------------------------- #
# Runtime: used only when the configuration matches the build
# --------------------------------------------------------------------------- #
async def test_semantic_ranking_when_the_configured_model_matches(tmp_path, monkeypatch):
    output, _manifest, calls = _built_with_local_model(tmp_path, monkeypatch)
    calls.clear()
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="synthetic-model")
    try:
        tables = await _tables(mcp, "which suppliers did we send purchase orders to")
        assert tables["search_mode"] == "semantic", tables
        assert "notes" not in tables
        assert tables["tables"][0]["table"] == "Erp.POHeader"
        fields = await _fields(mcp, "POHeader", "supplier")
        assert fields["search_mode"] == "semantic"
        assert fields["tables"][0]["fields"][0]["name"] == "VendorNum"
        assert ("load", "synthetic-model", True) in calls, "query-time loads must be local-files-only"
        assert any(text.startswith("Instruct:") for kind, text in
                   ((c[0], c[1][0]) for c in calls if c[0] == "encode")), "queries carry the discovery prefix"
    finally:
        for resource in resources:
            resource.close()


async def test_switch_off_keeps_substring_and_never_loads_a_model(tmp_path, monkeypatch):
    output, _manifest, calls = _built_with_local_model(tmp_path, monkeypatch)
    calls.clear()
    mcp, resources = _register(output, tmp_path, vector_search_enabled=False, embedding_model="synthetic-model")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        assert any("VECTOR_SEARCH_ENABLED" in note for note in tables["notes"])
        assert calls == [], "a disabled switch must not touch the model"
    finally:
        for resource in resources:
            resource.close()


async def test_model_mismatch_falls_back_and_names_both_models(tmp_path, monkeypatch):
    output, _manifest, calls = _built_with_local_model(tmp_path, monkeypatch)
    calls.clear()
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="other-model")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        note = " ".join(tables["notes"])
        assert "other-model" in note and "synthetic-model" in note
        assert calls == []
        listing = await _fields(mcp, "POHeader", "")
        assert "notes" not in listing, "an empty-query listing has nothing to rank, so no note"
    finally:
        for resource in resources:
            resource.close()


async def test_missing_model_setting_falls_back_with_the_setting_named(tmp_path, monkeypatch):
    output, _manifest, _calls = _built_with_local_model(tmp_path, monkeypatch)
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        assert any("EPICOR_MCP_EMBEDDING_MODEL" in note for note in tables["notes"])
    finally:
        for resource in resources:
            resource.close()


async def test_corrupt_vectors_are_refused_at_load_and_announced(tmp_path, monkeypatch):
    from epicor_mcp.discovery.store import DiscoveryIndex
    output, _manifest, _calls = _built_with_local_model(tmp_path, monkeypatch)
    np.save(output / "tables.npy", np.zeros((1, 3), dtype=np.float32), allow_pickle=False)
    index = DiscoveryIndex.load(output)
    assert index is not None and index.dim is None and "unavailable" in index.semantic_error
    index.close()
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="synthetic-model")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        assert any("unavailable" in note for note in tables["notes"])
        assert tables["tables"], "substring results still come back"
    finally:
        for resource in resources:
            resource.close()


async def test_provider_failure_at_query_time_degrades_per_call(tmp_path, monkeypatch):
    output, _manifest, _calls = _built_with_local_model(tmp_path, monkeypatch)
    broken = types.ModuleType("sentence_transformers")

    class Broken:
        def __init__(self, *args, **kwargs):
            raise OSError("model files are not present locally")

    broken.SentenceTransformer = Broken
    monkeypatch.setitem(sys.modules, "sentence_transformers", broken)
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="synthetic-model")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        assert any("model files are not present" in note for note in tables["notes"])
    finally:
        for resource in resources:
            resource.close()


async def test_query_vector_of_the_wrong_dimension_is_never_used(tmp_path, monkeypatch):
    from epicor_mcp.discovery.embeddings import LocalEmbedder
    calls: list = []
    _fake_sentence_transformers(monkeypatch, calls)
    output = tmp_path / "index"
    _builder().build_discovery_index(CATALOGUE, output, LocalEmbedder("synthetic-model"))
    module = types.ModuleType("sentence_transformers")

    class Narrow:
        def __init__(self, *args, **kwargs):
            pass

        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 2), dtype=np.float32)

    module.SentenceTransformer = Narrow
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    mcp, resources = _register(output, tmp_path, vector_search_enabled=True, embedding_model="synthetic-model")
    try:
        tables = await _tables(mcp, "supplier")
        assert tables["search_mode"] == "substring"
        assert any("dimension" in note for note in tables["notes"])
    finally:
        for resource in resources:
            resource.close()


# --------------------------------------------------------------------------- #
# Endpoint provider
# --------------------------------------------------------------------------- #
class _RecordingClient:
    def __init__(self, dim=4, fail=False):
        self.texts: list[str] = []
        self.dim = dim
        self.fail = fail
        self.closed = False

    async def embed_docs(self, texts, batch_size=64):
        if self.fail:
            raise ConnectionError("embeddings server is down")
        self.texts.extend(texts)
        return np.stack([_vec(text)[: self.dim] for text in texts])

    async def aclose(self):
        self.closed = True


async def test_endpoint_provider_uses_the_discovery_prefix_and_degrades_on_outage():
    from epicor_mcp.discovery.embeddings import EndpointEmbedder
    from epicor_mcp.discovery.store import TABLE_QUERY_PREFIX
    client = _RecordingClient()
    embedder = EndpointEmbedder("http://127.0.0.1:1/v1/embeddings", "endpoint-model", dim=4, client=client)
    vec = await embedder.encode_query("supplier", TABLE_QUERY_PREFIX)
    assert vec.shape == (4,)
    assert client.texts == [TABLE_QUERY_PREFIX + "supplier"], "not the document-search instruction"
    client.fail = True
    assert await embedder.encode_query("supplier", TABLE_QUERY_PREFIX) is None
    assert "embeddings server is down" in embedder.last_error
    await embedder.aclose()
    assert client.closed


def test_endpoint_manifest_requires_matching_endpoint_settings():
    from epicor_mcp.discovery.embeddings import EndpointEmbedder, embedder_for_index
    manifest = {"semantic": True, "provider": "openai-embeddings-endpoint", "model": "endpoint-model", "dim": 4}
    on = dict(vector_search_enabled=True, embedding_model="", embed_endpoint="", embed_model_name="", embed_dim=4)
    embedder, note = embedder_for_index(SimpleNamespace(**on), manifest)
    assert embedder is None and "EPICOR_MCP_EMBED_ENDPOINT" in note
    embedder, note = embedder_for_index(SimpleNamespace(**dict(on, embed_endpoint="http://127.0.0.1:1/v1/embeddings",
                                                             embed_model_name="endpoint-model", embed_dim=8)), manifest)
    assert embedder is None and "8" in note and "4" in note
    embedder, note = embedder_for_index(SimpleNamespace(**dict(on, embed_endpoint="http://127.0.0.1:1/v1/embeddings",
                                                             embed_model_name="endpoint-model")), manifest)
    assert isinstance(embedder, EndpointEmbedder) and note == ""
    embedder.close()
    embedder, note = embedder_for_index(SimpleNamespace(**on), {"semantic": True, "provider": "mystery", "model": "m", "dim": 4})
    assert embedder is None and "Unknown" in note
    assert embedder_for_index(SimpleNamespace(**on), {"semantic": False}) == (None, "")
