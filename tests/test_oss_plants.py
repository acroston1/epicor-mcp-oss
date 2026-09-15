"""Operator site hints are validated configuration, never authorization or SQL rules."""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from epicor_mcp.config import Settings
from tests.fixtures.oss_server import server_settings


@pytest.mark.parametrize('source', ['dotenv', 'environment'])
@pytest.mark.parametrize('raw,expected', [
    (None, {}), ('', {}), ('   ', {}), ('{}', {}),
    ('{"SYN-A":"Sample North", "SYN-B":"Sample South"}',
     {'SYN-A': 'Sample North', 'SYN-B': 'Sample South'}),
])
def test_site_map_loads_from_supported_configuration_sources(tmp_path, monkeypatch, source, raw, expected):
    monkeypatch.delenv('EPICOR_MCP_PLANTS', raising=False)
    env_file = tmp_path / '.env'
    if source == 'environment':
        if raw is not None:
            monkeypatch.setenv('EPICOR_MCP_PLANTS', raw)
        settings = Settings(_env_file=None)
    else:
        env_file.write_text('' if raw is None else f"EPICOR_MCP_PLANTS='{raw}'\n")
        settings = Settings(_env_file=env_file)
    assert settings.plants == expected


@pytest.mark.parametrize('source', ['dotenv', 'environment'])
@pytest.mark.parametrize('raw', [
    '{', 'null', '[]', '["SYN-A"]', 'true', '7', '"site"',
    json.dumps('{}'), json.dumps('{"SYN-A":"Sample North"}'),
    '{"SYN-A": 7}', '{"SYN-A": null}', '{"SYN-A": []}',
    '{"": "Sample Site"}', '{"   ": "Sample Site"}',
    '{"SYN-A": ""}', '{"SYN-A": "   "}',
])
def test_site_map_rejects_malformed_or_nonstring_entries(tmp_path, monkeypatch, source, raw):
    monkeypatch.delenv('EPICOR_MCP_PLANTS', raising=False)
    env_file = tmp_path / '.env'
    if source == 'environment':
        monkeypatch.setenv('EPICOR_MCP_PLANTS', raw)
        env_file = None
    else:
        env_file.write_text(f"EPICOR_MCP_PLANTS='{raw}'\n")
    with pytest.raises((ValueError, ValidationError, SettingsError)):
        Settings(_env_file=env_file)


def test_process_site_map_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text('EPICOR_MCP_PLANTS=\'{"FILE-SITE":"File Factory"}\'\n')
    monkeypatch.setenv('EPICOR_MCP_PLANTS', '{"ENV-SITE":"Environment Factory"}')
    assert Settings(_env_file=env_file).plants == {'ENV-SITE': 'Environment Factory'}


@pytest.mark.parametrize('value', [{1: 'Sample Site'}, {'SYN-A': 1}, {'SYN-A': True}])
def test_programmatic_site_map_rejects_nonstring_codes_and_names(value):
    with pytest.raises((ValueError, ValidationError)):
        Settings(_env_file=None, plants=value)


def _initialize(client):
    response = client.post('/mcp', headers={
        'Accept': 'application/json, text/event-stream',
        'Authorization': 'Bearer synthetic-token',
    }, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                   'clientInfo': {'name': 'synthetic-client', 'version': '1.0'}},
    })
    assert response.status_code == 200, response.text
    return response.json()['result']['instructions']


@pytest.mark.parametrize('auth_mode,index_present', [('none', True), ('azure_ad', True), ('azure_ad', False)])
def test_initialize_exposes_only_each_servers_site_map(tmp_path, monkeypatch, auth_mode, index_present):
    from fastapi.testclient import TestClient
    from epicor_mcp.auth.oauth import ValidatedToken
    from epicor_mcp.discovery.authz import AuthzScope
    from epicor_mcp.server import create_app
    from epicor_mcp.sql import domains
    from epicor_mcp.tools import _tenant

    maps = [{'SYN-A': 'Sample North'}, {'SYN-B': 'Sample South'}, {}]
    settings_list, clients = [], []
    monkeypatch.setenv('EPICOR_MCP_PLANTS', '{"UNRELATED":"Other Environment"}')
    with ExitStack() as stack:
        for index, configured_map in enumerate(maps):
            directory = tmp_path / str(index)
            directory.mkdir()
            settings = server_settings(directory, auth_mode=auth_mode, plants=configured_map,
                discovery_index_path=directory / 'no-schema', docs_db_path=directory / 'no-docs.db')
            if not index_present:
                settings.service_index_path = directory / 'missing-index.db'
            settings_list.append(settings)
            app = create_app(settings)
            if auth_mode == 'azure_ad':
                monkeypatch.setattr(app.state.token_validator, 'validate_token', AsyncMock(
                    return_value=ValidatedToken('apuser@example.org', {})))
                if app.state.table_authorizer is not None:
                    monkeypatch.setattr(app.state.table_authorizer, 'scope_for', AsyncMock(
                        return_value=AuthzScope.scoped('apuser@example.org', {'Part'}, 'synthetic')))
            clients.append(stack.enter_context(TestClient(app, base_url='https://mcp.example.org')))

        # Read the first app after constructing the others to catch global-map leaks.
        instructions = [_initialize(client) for client in clients]
        for index, text in enumerate(instructions):
            assert 'UNRELATED' not in text and 'Other Environment' not in text
            for other_index, configured_map in enumerate(maps):
                for code, name in configured_map.items():
                    assert (code in text) is (index == other_index)
                    assert (name in text) is (index == other_index)
            if maps[index]:
                objects = []
                for start, character in enumerate(text):
                    if character == '{':
                        try:
                            objects.append(json.JSONDecoder().raw_decode(text[start:])[0])
                        except ValueError:
                            pass
                assert maps[index] in objects, 'the site hint must carry the configured JSON map'

        settings_list[0].plants['MUTATED'] = 'Changed Later'
        assert _initialize(clients[0]) == instructions[0]
        assert not domains.PLANTS, 'site hints must not become global SQL rewrite rules'
        assert not _tenant.PLANTS, 'legacy tools must not read unrelated process configuration'


async def test_site_hints_do_not_grant_tables_or_rewrite_query_literals(tmp_path):
    from epicor_mcp.server import _build_readonly_server
    from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

    settings = server_settings(tmp_path, auth_mode='none',
        plants={'SYN-A': 'Sample North', 'Plant': 'No Table Grant'},
        sql_validate_columns=False, sql_ground_domains=True)
    _mcp, runtime, resources = _build_readonly_server(settings)
    await runtime.client.close()
    try:
        _, parsed = load('clean_top')
        mock = MockEpicorClient(parse_ds=parsed, execute_response=ok_execute([{'PN': 'Sample North'}]))
        runtime.client = mock
        sql = "select top 5 [P].[PartNum] as [PN] from Erp.Part as [P] where [P].[PartNum] = 'Sample North'"
        result = await runtime.run(sql=sql)
        assert result['success'], result
        payload = next(body for url, body in mock.calls if 'ParseFromSQL' in url)
        assert "'Sample North'" in json.dumps(payload)
        assert 'SYN-A' not in json.dumps(payload)

        parsed['QueryTable'][0]['DBTableName'] = 'Plant'
        parsed['QueryField'] = []
        denied = MockEpicorClient(parse_ds=parsed, execute_response=ok_execute([]))
        runtime.client = denied
        result = await runtime.run(sql='select top 5 [P].[Plant] from Erp.Plant as [P]')
        assert result['error'] == 'table_not_authorized', result
        assert not denied.called('Execute')
    finally:
        for resource in resources:
            resource.close()
