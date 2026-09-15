"""Real HTTP middleware and MCP transport with synthetic offline configuration."""
import pytest
from fastapi.testclient import TestClient
from epicor_mcp.server import create_app
from tests.fixtures.oss_server import server_settings


@pytest.mark.parametrize('token',['','synthetic-shared-token'])
def test_sso_disabled_http_exposes_five_tools_and_refuses_admin(tmp_path,token):
    settings=server_settings(tmp_path,auth_mode='none',server_token=token,
        discovery_index_path=tmp_path/'missing-schema',docs_db_path=tmp_path/'missing-docs.db')
    app=create_app(settings)
    headers={'Accept':'application/json, text/event-stream'}
    with TestClient(app,base_url='https://mcp.example.org') as client:
        if token:
            assert client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers=headers).status_code == 401
            assert client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers=dict(headers,Authorization='Bearer wrong')).status_code == 401
            headers['Authorization']='Bearer '+token
        result=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers=headers)
        assert result.status_code == 200
        assert {item['name'] for item in result.json()['result']['tools']} == {'epicor_query','epicor_tables','epicor_fields','epicor_help','epicor_dashboards'}
        for method,path in [('GET','/admin/users'),('POST','/admin/reload'),('GET','/.well-known/oauth-protected-resource'),('GET','/oauth/authorize')]:
            assert client.request(method,path,headers=dict(headers,**{'X-Admin-Secret':'anything'})).status_code == 404
        assert client.get('/health').json()['read_only'] is True
        assert app.state.token_validator is None
        assert app.state.user_map is None
