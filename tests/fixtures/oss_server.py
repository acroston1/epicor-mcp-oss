"""Minimal synthetic metadata and configuration for real server wiring tests."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path


def server_settings(tmp_path: Path, **overrides):
    from epicor_mcp.index.builder import _SCHEMA_SQL, _FTS_SQL, _FTS_POPULATE
    from epicor_mcp.config import Settings
    path = tmp_path / 'service_index.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_FTS_SQL)
        for service, table in [('Erp.BO.APInvoiceSvc', 'APInvHed'), ('Erp.BO.APAdjustmentSvc', 'APInvHed'), ('Erp.BO.VendorSvc', 'Vendor'), ('Erp.BO.POSvc', 'POHeader'), ('Ice.BO.CompanySvc', 'Company')]:
            conn.execute('INSERT OR IGNORE INTO services VALUES (?, ?, ?, ?, ?, ?, ?)', (service, 'Erp.BO', service.split('.')[-1], 'Synthetic service', '', 1, 1))
            conn.execute('INSERT OR IGNORE INTO entity_sets VALUES (?, ?)', (service, table))
            conn.execute('INSERT OR IGNORE INTO fields VALUES (?, ?, ?, ?, ?, ?)', (service, table, 'Company', 'Edm.String', 0, 'Synthetic company'))
        conn.executescript(_FTS_POPULATE)
    users = tmp_path / 'users.json'
    users.write_text(json.dumps({'users': {'apuser@example.org': {'department':'Finance', 'epicor_username':'apuser', 'access_level':'read_only', 'can_write_baqs':True, 'environment':'live'}}}))
    keys = tmp_path / 'department_keys.json'
    keys.write_text(json.dumps({'admin_keys': {'read_key':'synthetic-read-key', 'baq_key':'synthetic-baq-key'}, 'environments': {'live':'https://erp.example.org/api/v2/odata/DEMO/'}}))
    whitelist = tmp_path / 'tables.txt'
    whitelist.write_text('Part\nJobHead\n')
    kwargs = dict(auth_mode='azure_ad', azure_tenant_id='00000000-0000-0000-0000-000000000000', azure_client_id='11111111-1111-1111-1111-111111111111', azure_client_secret='synthetic-client-secret', response_public_base_url='https://mcp.example.org', epicor_live_url='https://erp.example.org/api/v2/odata/DEMO/', epicor_pilot_url='https://pilot.example.org/api/v2/odata/DEMO/', epicor_company_id='DEMO', epicor_api_key='synthetic-read-key', epicor_baq_api_key='synthetic-baq-key', epicor_username='synthetic-service', epicor_password='synthetic-password', service_index_path=path, users_config_path=users, department_keys_path=keys, table_whitelist_path=str(whitelist), audit_log_path=tmp_path/'audit.db', vector_search_enabled=False, forum_live_enabled=False)
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)
