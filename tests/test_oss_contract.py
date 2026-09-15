"""OSS defaults and real table authorization boundaries, offline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from epicor_mcp.config import Settings


def test_oss_defaults_are_sso_disabled_and_cpu_only(monkeypatch):
    for name in tuple(__import__('os').environ):
        if name.startswith('EPICOR_MCP_'):
            monkeypatch.delenv(name)
    settings = Settings(_env_file=None)
    assert settings.auth_mode == 'none'
    assert settings.dev_mode is False
    assert settings.public_surface is True
    assert settings.vector_search_enabled is False
    assert settings.forum_live_enabled is False
    assert settings.azure_tenant_id == ''
    assert settings.azure_client_id == ''
    assert settings.attachment_path_map == {}
    assert settings.table_whitelist_path


def test_allowlist_is_exact_case_insensitive_and_does_not_expand_ud(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path / 'tables.txt'
    path.write_text('# read-only tables\nErp.Part\n Ice.UD01 \n')
    policy = TableWhitelist.from_file(path)
    assert policy.active
    assert policy.allows('Part')
    assert policy.allows('erp.PART')
    assert not policy.allows('Ice.Part'), 'a qualified grant must preserve its schema'
    assert not policy.allows('Erp.UD01'), 'Ice-only grants must not cross schemas'
    assert policy.allows('Ice.UD01')
    assert not policy.allows('PartCost')
    assert not policy.allows('Part_UD')
    assert not policy.allows('UD01A')


def test_empty_allowlist_denies_every_table(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path / 'tables.txt'
    path.write_text('# no grants\n\n')
    policy = TableWhitelist.from_file(path)
    assert policy.active
    assert not policy.allows('Erp.Part')


@pytest.mark.parametrize('contents', ['*\n', 'Erp.*\n', 'Part;DROP TABLE Part\n', '{"tables": "*"}', 'Part\nInvalid table name\n'])
def test_malformed_allowlist_fails_closed(tmp_path, contents):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path / 'tables.txt'
    path.write_text(contents)
    with pytest.raises(ValueError):
        TableWhitelist.from_file(path)


def test_missing_allowlist_never_means_unrestricted(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    with pytest.raises((ValueError, FileNotFoundError)):
        TableWhitelist.from_file(tmp_path / 'missing.txt')


def test_blank_allowlist_setting_is_explicit_unrestricted_read_mode():
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    policy = TableWhitelist.from_file('')
    assert not policy.active
    assert policy.allows('Erp.Part')


async def test_sso_disabled_saved_query_checks_definition_before_execution():
    from epicor_mcp.discovery.authz import AuthzScope
    from tests.test_query_table_gate import FakeAuthorizer, runtime_for
    from tests.wedge_fixtures import MockEpicorClient
    client = MockEpicorClient(baq_data_rows=[{'PN': 'SYNTHETIC'}])
    auth = FakeAuthorizer(AuthzScope.scoped('reader@example.org', {'JobHead'}, 'allowlist'))
    runtime = runtime_for(client, authorizer=auth)
    runtime.settings.auth_mode = 'none'
    result = await runtime.run(saved_baq='AUTO-parts')
    assert result['error'] == 'table_not_authorized'
    assert not client.called('/Data'), result
    assert auth.scope_calls, 'saved BAQ definition bypassed the table whitelist'


async def test_sso_disabled_saved_query_can_read_allowed_table():
    from epicor_mcp.discovery.authz import AuthzScope
    from tests.test_query_table_gate import FakeAuthorizer, runtime_for
    from tests.wedge_fixtures import MockEpicorClient
    client = MockEpicorClient(baq_data_rows=[{'PN': 'SYNTHETIC'}])
    auth = FakeAuthorizer(AuthzScope.scoped('reader@example.org', {'Part'}, 'allowlist'))
    runtime = runtime_for(client, authorizer=auth)
    runtime.settings.auth_mode = 'none'
    result = await runtime.run(saved_baq='AUTO-parts')
    assert result['success'] is True, result
    assert client.called('/Data')


async def test_sso_disabled_save_refuses_before_any_write_even_with_legacy_grant():
    from tests.test_query_table_gate import CLEAN_SQL, client_for, runtime_for
    client = client_for('clean_top')
    runtime = runtime_for(client)
    runtime.settings.auth_mode = 'none'
    result = await runtime.run(sql=CLEAN_SQL, save_as='test-save')
    assert not result.get('success', False), result
    assert not client.called('Update')
    assert not client.called('DeleteByID')


@pytest.mark.parametrize('fixture', ['clean_in_subquery', 'clean_rollup'])
async def test_allowlist_checks_every_table_in_subquery_or_join(fixture):
    from epicor_mcp.discovery.authz import AuthzScope
    from epicor_mcp.sql.adhoc import run_sql
    from tests.wedge_fixtures import MockEpicorClient, load, ok_execute
    sql, ds = load(fixture)
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    result = await run_sql(sql, client=client, api_key='test-key', base_url='https://example.invalid/api/v2/odata/DEMO', table_scope=AuthzScope.scoped('reader@example.org', {'Part'}, 'allowlist'), validate_columns=False, ground_domains=False)
    assert result.get('error') == 'table_not_authorized', result
    assert not client.called('Execute')


async def test_allowlist_used_by_discovery_and_field_tools():
    from epicor_mcp.discovery.authz import AuthzScope
    from tests.test_discovery_gate import _register
    registered, _ = _register(AuthzScope.scoped('reader@example.org', {'JobHead'}, 'allowlist'))
    tables = await registered['epicor_tables'](query='jobs and purchase orders')
    assert [table['name'] for table in tables['tables']] == ['JobHead']
    fields = await registered['epicor_fields'](table='POHeader')
    assert fields['error'] == 'table_not_authorized'
    assert 'PONum' not in json.dumps(fields)


@pytest.mark.parametrize('entry',['Evil.Part','Erp.Ice.Part','[Erp].[Part]'])
def test_allowlist_rejects_unknown_or_extra_schema_qualification(tmp_path, entry):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    path = tmp_path/'tables.txt'; path.write_text(entry)
    with pytest.raises(ValueError):
        TableWhitelist.from_file(path)


async def test_duplicate_aliases_cannot_hide_a_denied_subquery_table(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    from epicor_mcp.sql.adhoc import run_sql
    from tests.wedge_fixtures import MockEpicorClient, load, ok_execute
    sql,ds = load('clean_top')
    ds['QueryTable'] = [
        {'TableID':'P','SubQueryID':'inner','TableType':'DB','DBSchemaName':'Erp','DBTableName':'JobHead'},
        {'TableID':'P','SubQueryID':'outer','TableType':'DB','DBSchemaName':'Erp','DBTableName':'Part'},
    ]
    path=tmp_path/'tables.txt';path.write_text('Part')
    client=MockEpicorClient(parse_ds=ds,execute_response=ok_execute([]))
    result=await run_sql(sql,client=client,api_key='synthetic',base_url='https://example.invalid',table_scope=TableWhitelist.from_file(path),validate_columns=False,ground_domains=False)
    assert result.get('success') is False
    assert not client.called('Execute')


async def test_diagnostic_probe_obeys_the_original_whitelist(tmp_path):
    from epicor_mcp.rbac.table_whitelist import TableWhitelist
    from epicor_mcp.sql.adhoc import make_probe_runner
    from tests.wedge_fixtures import MockEpicorClient, load, ok_execute
    _,ds=load('clean_top')
    ds['QueryTable'][0]['DBTableName']='Plant'
    ds['QueryField']=[]
    path=tmp_path/'tables.txt';path.write_text('Part')
    client=MockEpicorClient(parse_ds=ds,execute_response=ok_execute([]))
    probe=make_probe_runner(client=client,api_key='synthetic',base_url='https://example.invalid',timeout_s=1,table_scope=TableWhitelist.from_file(path))
    result=await probe('select top 5 Plant from Erp.Plant')
    assert not result.ok
    assert 'authorization' in result.error
    assert not client.called('Execute')


async def test_real_discovery_wiring_preserves_schema_and_ud_whitelist(tmp_path):
    import importlib.util
    from epicor_mcp.index.local_retrieval import register_local_retrieval
    from epicor_mcp.rbac.table_whitelist import TableWhitelist, NoneTableAuthorizer
    from types import SimpleNamespace
    from mcp.server.fastmcp import FastMCP
    spec=importlib.util.spec_from_file_location('discovery_builder',Path('scripts/build_discovery_index.py'))
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    index_path=tmp_path/'index'
    names={'Part':'Erp','OtherPart':'Ice','Part_UD':'Erp','UserFile':'Erp'}
    catalogue={'tables':{name:{'schema':schema,'full_name':f'{schema}.{name}','fields':[{'name':'Name','type':'nvarchar'},{'name':'Password','type':'nvarchar'}]} for name,schema in names.items()}}
    builder.build_discovery_index(catalogue,index_path)
    path=tmp_path/'tables.txt';path.write_text('Erp.Part\nErp.OtherPart\nUserFile')
    settings=SimpleNamespace(discovery_index_path=index_path,docs_db_path=tmp_path/'missing.db',document_vectors_path=tmp_path/'vectors',vector_search_enabled=False)
    mcp=FastMCP('test')
    resources=register_local_retrieval(mcp,settings,NoneTableAuthorizer(TableWhitelist.from_file(path)))
    try:
        tables=await mcp._tool_manager.get_tool('epicor_tables').fn(query='Part')
        assert [t['table'] for t in tables['tables']] == ['Erp.Part']
        for table in ('Part_UD','Ice.Part','Ice.OtherPart','UserFile'):
            fields=await mcp._tool_manager.get_tool('epicor_fields').fn(table=table)
            assert fields.get('error'), (table,fields)
            assert 'Password' not in json.dumps(fields)
    finally:
        for resource in resources: resource.close()
