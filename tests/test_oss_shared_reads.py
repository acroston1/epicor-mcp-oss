"""Shared SSO policy without widening the OSS no-SSO operator ceiling; PLAN #1–5."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from epicor_mcp.config import Settings
from epicor_mcp.discovery.authz import AuthzScope, ScopeState, TableAuthorizer
from epicor_mcp.rbac.table_whitelist import TableWhitelist
from epicor_mcp.sql.scope_gate import check_table_scope
from tests.wedge_fixtures import MockEpicorClient, load, ok_execute

ROOT = Path(__file__).resolve().parents[1]
EMAIL = 'reader@example.org'


def read_policy(path):
    return importlib.import_module('epicor_mcp.discovery.shared_reads').read_allowlist(path)


def policy(tmp_path, text):
    path = tmp_path / 'shared.txt'
    path.write_text(text, encoding='utf-8')
    return path


class Menu:
    def __init__(self, *, services=(), failed=False):
        self.snapshot = SimpleNamespace(allowed_services=services, is_error=failed,
            error='synthetic identity unavailable' if failed else '', security_mgr=False, allow_all=False)

    async def ensure_snapshot(self, email):
        return self.snapshot


class Index:
    def get_entity_sets(self, service):
        return ['Parts'] if service == 'Erp.BO.PartSvc' else []


def authorizer(path, **kwargs):
    return TableAuthorizer(Menu(**kwargs), Index(), table_read_allowlist_path=path)


def get_scope(auth, email=EMAIL):
    return asyncio.run(auth.scope_for(email))


def test_parser_normalizes_schema_case_comments_and_duplicates(tmp_path):
    path = policy(tmp_path, '# local exact identifiers\nPartMtl\nERP.PARTMTL # duplicate\nIce.UD01\n')
    assert read_policy(path) == frozenset({'erp.partmtl', 'ice.ud01'})


@pytest.mark.parametrize('entry', ['*', 'Erp.*', '../Part', '/tmp/Part', 'Part;DROP TABLE Part',
    'SELECT * FROM Erp.Part', 'Other.Part', 'Erp.Ice.Part', '[Erp].[Part]', 'Erp.',
    'Part Name', 'Part\\Name', 'Part\x00Name'])
def test_parser_rejects_invalid_policy_atomically(tmp_path, entry):
    with pytest.raises(ValueError):
        read_policy(policy(tmp_path, 'Erp.PartMtl\n' + entry + '\nIce.UD01\n'))


def test_missing_empty_and_unreadable_policy_are_distinct(tmp_path):
    assert read_policy(tmp_path / 'missing.txt') == frozenset()
    assert read_policy(policy(tmp_path, '# empty\n')) == frozenset()
    with pytest.raises(OSError):
        read_policy(tmp_path)


def test_invalid_utf8_never_returns_partial_shared_grants(tmp_path):
    path = tmp_path / 'invalid.txt'
    path.write_bytes(b'Erp.PartMtl\n\xff\n')
    with pytest.raises(UnicodeError):
        read_policy(path)


def test_relative_policy_uses_operator_working_directory(tmp_path, monkeypatch):
    policy(tmp_path, 'Ice.UD01\n')
    monkeypatch.chdir(tmp_path)
    assert read_policy('shared.txt') == frozenset({'ice.ud01'})


@pytest.mark.parametrize('services', [(), ('Erp.BO.PartSvc',)])
def test_successful_sso_scope_adds_exact_shared_grant_with_provenance(tmp_path, services):
    scope = get_scope(authorizer(policy(tmp_path, 'Erp.PartMtl\n'), services=services))
    assert scope.state is ScopeState.SCOPED
    assert scope.allows('Erp.PartMtl') and scope.allows('PARTMTL')
    assert not scope.allows('Ice.PartMtl')
    assert not scope.allows('Erp.PartMtl_UD')
    assert not scope.allows('Erp.PartMtlExtra')
    assert scope.shared_read_tables == frozenset({'erp.partmtl'})
    assert 'shared' in scope.note('gate').lower()
    if services:
        assert scope.allows('Erp.Part') and scope.allows('Erp.Part_UD')
    else:
        assert not scope.allows('Erp.Part')


def test_ice_and_explicit_ud_shared_grants_do_not_expand_schema_or_parent(tmp_path):
    scope = get_scope(authorizer(policy(tmp_path, 'Ice.UD01\nErp.PartMtl_UD\n')))
    assert scope.allows('Ice.UD01')
    assert not scope.allows('Erp.UD01') and not scope.allows('UD01')
    assert not scope.allows('Ice.UD01_UD')
    assert scope.allows('Erp.PartMtl_UD')
    assert not scope.allows('Erp.PartMtl')


def test_removed_empty_missing_and_instance_policy_have_no_hidden_fallback(tmp_path):
    path = policy(tmp_path, 'Erp.PartMtl\n')
    original = authorizer(path)
    assert get_scope(original).allows('Erp.PartMtl')
    path.write_text('Ice.UD01\n', encoding='utf-8')
    replacement = authorizer(path)
    assert get_scope(original, 'another@example.org').allows('Erp.PartMtl')
    assert not get_scope(original, 'another@example.org').allows('Ice.UD01')
    assert get_scope(replacement).allows('Ice.UD01')
    assert not get_scope(replacement).allows('Erp.PartMtl')
    path.write_text('', encoding='utf-8')
    for blank_path in (path, tmp_path / 'missing.txt'):
        scope = get_scope(authorizer(blank_path))
        assert scope.tables == frozenset()
        assert scope.shared_read_tables == frozenset()
        assert 'shared' not in scope.note('gate').lower()
    assert get_scope(replacement).allows('Ice.UD01')


@pytest.mark.parametrize('failure', ['empty_identity', 'unknown_disabled', 'missing_snapshot', 'exception', 'unmapped_service'])
def test_shared_policy_never_masks_sso_authorization_failure(tmp_path, failure):
    path = policy(tmp_path, 'Erp.PartMtl\n')
    menu = Menu(failed=failure == 'unknown_disabled',
                services=('Erp.BO.UnknownSvc',) if failure == 'unmapped_service' else ())
    if failure == 'missing_snapshot':
        menu.snapshot = None
    if failure == 'exception':
        async def fail(email):
            raise RuntimeError('synthetic lookup failure')
        menu.ensure_snapshot = fail
    auth = TableAuthorizer(menu, Index(), table_read_allowlist_path=path)
    result = get_scope(auth, '' if failure == 'empty_identity' else EMAIL)
    assert result.state is ScopeState.UNAVAILABLE
    assert not result.allows('Erp.PartMtl')


def test_config_default_and_environment_override(tmp_path, monkeypatch):
    assert str(Settings.model_fields['table_read_allowlist_path'].default) == 'table_read_allowlist.txt'
    path = policy(tmp_path, 'Ice.UD01\n')
    monkeypatch.setenv('EPICOR_MCP_TABLE_READ_ALLOWLIST_PATH', str(path))
    assert str(Settings(_env_file=None).table_read_allowlist_path) == str(path)


def test_real_sso_server_receives_operator_policy(tmp_path, monkeypatch):
    from tests.test_authz_wiring import _build_app
    path = policy(tmp_path, 'Erp.PartMtl\n')
    monkeypatch.setenv('EPICOR_MCP_TABLE_READ_ALLOWLIST_PATH', str(path))
    app = _build_app(tmp_path, monkeypatch)
    result = get_scope(app.state.table_authorizer, 'apuser@example.org')
    assert result.allows('Erp.PartMtl')
    assert not result.allows('Ice.PartMtl') and not result.allows('Erp.PartMtl_UD')


def test_invalid_shared_file_refuses_sso_startup(tmp_path, monkeypatch):
    from tests.test_authz_wiring import _build_app
    monkeypatch.setenv('EPICOR_MCP_TABLE_READ_ALLOWLIST_PATH', str(policy(tmp_path, 'Erp.*\n')))
    with pytest.raises((ValueError, RuntimeError)):
        _build_app(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_no_sso_operator_ceiling_is_unchanged_even_when_shared_file_grants_more(tmp_path):
    from epicor_mcp.server import create_app
    from tests.fixtures.oss_server import server_settings
    from tests.test_query_table_gate import runtime_for
    shared = policy(tmp_path, 'Erp.PartMtl\nErp.Part\n')
    settings = server_settings(tmp_path, auth_mode='none', table_read_allowlist_path=str(shared))
    Path(settings.table_whitelist_path).write_text('Erp.JobHead\n')
    app = create_app(settings)
    scope = await app.state.table_authorizer.scope_for(EMAIL)
    assert scope.allows('Erp.JobHead')
    assert not scope.allows('Erp.Part') and not scope.allows('Erp.PartMtl')
    _, ds = load('clean_top')
    assert check_table_scope(scope, ds)['error'] == 'table_not_authorized'
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    runtime = runtime_for(client, authorizer=app.state.table_authorizer)
    runtime.settings.auth_mode = 'none'
    result = await runtime.run(saved_baq='AUTO-parts')
    assert result['error'] == 'table_not_authorized'
    assert not client.called('/Data') and not client.called('Execute')


def test_sensitive_denylist_still_beats_shared_table_scope(tmp_path):
    from epicor_mcp.sql import denylist
    from epicor_mcp.sql.adhoc import run_sql
    scope = get_scope(authorizer(policy(tmp_path, 'Erp.PREmpMas\n')))
    assert denylist.is_denied_table('Erp.PREmpMas')
    assert scope.shared_read_tables == frozenset({'erp.prempmas'})
    sql, parsed = load('clean_top')
    ds = json.loads(json.dumps(parsed).replace('PartNum', 'EmpID').replace('Part', 'PREmpMas'))
    client = MockEpicorClient(parse_ds=ds, execute_response=ok_execute([]))
    result = asyncio.run(run_sql(sql.replace('PartNum', 'EmpID').replace('Part', 'PREmpMas'),
        client=client, api_key='synthetic-key', base_url='https://erp.example.org/api/v2/odata/DEMO',
        table_scope=scope, validate_columns=False, ground_domains=False))
    assert result['error'] == 'table_access_denied', result
    assert not client.called('Execute')


@pytest.mark.asyncio
async def test_real_discovery_preserves_shared_schema_and_ud_boundaries(tmp_path):
    from mcp.server.fastmcp import FastMCP
    from epicor_mcp.index.local_retrieval import register_local_retrieval
    spec = importlib.util.spec_from_file_location('shared_policy_builder', ROOT / 'scripts/build_discovery_index.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    index = tmp_path / 'discovery'
    catalogue = {'tables': {name: {'schema':schema, 'full_name':f'{schema}.{name}',
        'fields':[{'name':'PartNum','type':'nvarchar'}]} for name, schema in
        [('PartMtl','Erp'), ('PartMtl_UD','Erp'), ('OtherPart','Ice')]}}
    builder.build_discovery_index(catalogue, index)
    auth = authorizer(policy(tmp_path, 'Erp.PartMtl\nErp.OtherPart\n'))
    settings = SimpleNamespace(discovery_index_path=index, docs_db_path=tmp_path/'missing.db',
        document_vectors_path=tmp_path/'vectors', vector_search_enabled=False)
    mcp = FastMCP('synthetic')
    resources = register_local_retrieval(mcp, settings, auth, session_email=lambda:EMAIL)
    try:
        tables = await mcp._tool_manager.get_tool('epicor_tables').fn(query='Part')
        assert [item['table'] for item in tables['tables']] == ['Erp.PartMtl']
        allowed = await mcp._tool_manager.get_tool('epicor_fields').fn(table='Erp.PartMtl')
        assert 'PartNum' in json.dumps(allowed) and not allowed.get('error')
        for name in ('Erp.PartMtl_UD', 'Ice.PartMtl', 'Ice.OtherPart'):
            denied = await mcp._tool_manager.get_tool('epicor_fields').fn(table=name)
            assert denied.get('error'), (name, denied)
    finally:
        for resource in resources:
            resource.close()


@pytest.mark.parametrize('valid', [True, False])
def test_validator_cli_checks_local_file_without_modification(tmp_path, valid):
    path = policy(tmp_path, 'Ice.UD01\n' if valid else 'Erp.*\n')
    before = path.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'epicor_mcp.discovery.shared_reads', '--check', '--path', str(path)],
        cwd=tmp_path, env={**os.environ,'PYTHONPATH':str(ROOT/'src')}, capture_output=True, text=True, timeout=15)
    assert path.read_bytes() == before
    if valid:
        assert result.returncode == 0, result.stderr
        assert 'ice.ud01' in result.stdout.lower()
        assert re.search(r'\b1\b', result.stdout)
    else:
        assert result.returncode != 0


def test_editable_operator_policy_can_change_without_hardcoded_test_grants():
    path = ROOT / 'table_read_allowlist.txt'
    entries = read_policy(path)
    result = get_scope(authorizer(path))
    assert result.shared_read_tables == entries
    for name in entries:
        assert result.allows(name)


def test_policy_is_in_wheel_and_source_distribution_configuration():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    wheel = project['tool']['hatch']['build']['targets']['wheel']
    assert wheel['force-include']['table_read_allowlist.txt'] == 'epicor_mcp/discovery/table_read_allowlist.example.txt'
    assert (ROOT / 'table_read_allowlist.txt').is_file()
