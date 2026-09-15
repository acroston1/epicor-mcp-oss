"""Pytest configuration — add src/ to sys.path so tests import epicor_mcp.*."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
# The repo root, so `from tests.wedge_fixtures import ...` resolves regardless of
# the working directory pytest was launched from.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest


@pytest.fixture(autouse=True)
def synthetic_schema_for_engine_regressions(request, tmp_path, monkeypatch):
    """Supply metadata explicitly to tests that formerly relied on private data."""
    modules = {'test_validate_columns','test_pipe_integration','test_lint_unknown_table',
               'test_wedge_query_pipe','test_query_table_gate','test_ud_column_rewrite',
               'test_table_authz_core','test_denylist_rvalue_and_schema','test_discovery'}
    if request.module.__name__.rsplit('.',1)[-1] not in modules:
        return
    from tests.fixtures.synthetic_catalogue import build
    columns, schema = build(tmp_path)
    if request.module.__name__.rsplit('.',1)[-1] == 'test_discovery':
        import importlib.util
        import json
        module_path = _ROOT/'scripts/build_discovery_index.py'
        spec = importlib.util.spec_from_file_location('test_schema_builder',module_path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        index_root = tmp_path/'discovery'
        builder.build_discovery_index(json.loads(schema.read_text()),index_root)
        monkeypatch.setattr(request.module,'INDEX',index_root)
    monkeypatch.setenv('EPICOR_MCP_COLUMN_CATALOGUE',str(columns))
    monkeypatch.setenv('EPICOR_MCP_SCHEMA_CATALOGUE',str(schema))
    if hasattr(request.module,'CATALOGUE') and isinstance(request.module.CATALOGUE,Path):
        monkeypatch.setattr(request.module,'CATALOGUE',schema)


@pytest.fixture(autouse=True)
def reject_external_network(monkeypatch):
    """A copied fixture omission must never contact an actual ERP or model API."""
    import socket
    real_resolve = socket.getaddrinfo
    def resolve(host,*args,**kwargs):
        if host not in {'127.0.0.1','localhost','::1',None}:
            raise AssertionError(f'External DNS is forbidden in the deterministic suite: {host}')
        return real_resolve(host,*args,**kwargs)
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    real_connect = socket.socket.connect
    def connect(sock,address):
        if isinstance(address,tuple) and address[0] not in {'127.0.0.1','localhost','::1'}:
            raise AssertionError(f'External network is forbidden in the deterministic suite: {address[0]}')
        return real_connect(sock,address)
    monkeypatch.setattr(socket.socket,'connect',connect)
