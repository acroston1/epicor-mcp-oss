"""Configurable Python/native connector; no OAuth required by default."""
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
    for key in ('MCP_SERVER_URL', 'MCP_AUTH_MODE', 'MCP_SERVER_TOKEN', 'MCP_CA_BUNDLE'):
        monkeypatch.delenv(key, raising=False)
    path = Path(__file__).resolve().parents[1] / 'bridge/epicor_mcp_bridge.py'
    spec = importlib.util.spec_from_file_location('oss_bridge_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bridge_default_mode_needs_no_oauth_discovery(bridge, monkeypatch):
    monkeypatch.setenv('MCP_SERVER_URL', 'https://mcp.example.org/erp/mcp')
    config = bridge.load_config()
    assert config.server_url == 'https://mcp.example.org/erp/mcp'
    assert config.auth_mode == 'none'
    assert not config.server_token


def test_bridge_missing_server_url_is_actionable(bridge):
    with pytest.raises(ValueError, match='MCP_SERVER_URL'):
        bridge.load_config()


def test_bridge_token_mode_requires_and_uses_explicit_token(bridge, monkeypatch):
    monkeypatch.setenv('MCP_SERVER_URL', 'https://mcp.example.org/mcp')
    monkeypatch.setenv('MCP_AUTH_MODE', 'token')
    with pytest.raises(ValueError, match='MCP_SERVER_TOKEN'):
        bridge.load_config()
    monkeypatch.setenv('MCP_SERVER_TOKEN', 'synthetic-connector-token')
    config = bridge.load_config()
    assert config.server_token == 'synthetic-connector-token'
    assert config.auth_mode == 'token'


def test_bridge_rejects_unknown_authentication_mode(bridge, monkeypatch):
    monkeypatch.setenv('MCP_SERVER_URL', 'https://mcp.example.org/mcp')
    monkeypatch.setenv('MCP_AUTH_MODE', 'typo')
    with pytest.raises(ValueError):
        bridge.load_config()


async def test_bridge_relays_tool_arguments_without_reading_local_paths(bridge, monkeypatch, tmp_path):
    local_file = tmp_path / "private.txt"
    local_file.write_text("synthetic private local content")
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "epicor_query", "arguments": {"attachments": [{"path": str(local_file)}]}}}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + "\n").encode())))
    relayed = []

    class Stream:
        async def send(self, message):
            relayed.append(message.message.model_dump(mode="json", exclude_none=True))

    with pytest.raises(bridge._BridgeShutdown, match="stdin closed"):
        await bridge._stdin_to_server(Stream())
    assert relayed == [request]
