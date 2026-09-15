"""Invented metadata for parser regression tests, never an Epicor schema export.

Names required by the tested SQL contract are combined with obviously synthetic
padding columns. This fixture tests loading, filtering and bounded diagnostics;
it makes no claim about the complete schema of any Epicor installation.
"""
from __future__ import annotations
import json
from pathlib import Path


def build(root: Path) -> tuple[Path, Path]:
    from epicor_mcp.sql.card import CARD_COLUMNS
    cols = {table: {name: 'nvarchar' for name in names} for table, names in CARD_COLUMNS.items()}
    additions = {
        'Part': {'SysRowID':'uniqueidentifier','InActive':'bit','HasOnHandQty':'bit'},
        'JobHead': {'ProdQty':'decimal','SysRowID':'uniqueidentifier'},
        'PartWhse': {'OnHandQty':'decimal'},
        'LaborDtl': {'ScrapQty':'decimal','LaborRate':'decimal'},
        'JobOper': {'EstScrap':'decimal','EstScrapType':'nvarchar'},
        'JobOpDtl': {'ResourceGrpID':'nvarchar'},
        'OrderDtl': {'DocExtPriceDtl':'decimal','ExtPriceDtl':'decimal'},
        'Customer': {'CustID':'nvarchar','Inactive':'bit','SysRowID':'uniqueidentifier'},
        'UserFile': {'AdvBAQRights':'bit','AllowMultipleSessions':'bit','CanCustomize':'bit','DcdUserID':'nvarchar','BPMAdvancedUser':'bit','DspPayrollMgr':'bit','PwdExpires':'bit','GroupList':'nvarchar'},
        'OrderRel': {'SysRowID':'uniqueidentifier'},
        'Resource': {'ResourceType':'nvarchar','ResourceID':'nvarchar'},
        'PartMtl': {'Company':'nvarchar','PartNum':'nvarchar'},
        'PartOpr': {'Company':'nvarchar','PartNum':'nvarchar'},
        'SugPoChg': {'Company':'nvarchar','PONum':'int'},
        'PartRev': {'Company':'nvarchar','PartNum':'nvarchar'},
        'PartOpDtl': {'Company':'nvarchar','PartNum':'nvarchar'},
        'RcvHead': {'Company':'nvarchar'}, 'Company': {'Company':'nvarchar'}, 'APTran': {'Company':'nvarchar'}, 'BankTran':{'Company':'nvarchar'},
    }
    for table, fields in additions.items():
        cols.setdefault(table, {}).update(fields)
    # Distinct metadata widths exercise truncation independently of live exports.
    for table in ('LaborDtl','UserFile','PartWhse'):
        cols[table].update({f'SyntheticPadding{i:03d}':'nvarchar' for i in range(70)})
    cols['LaborDtl'].pop('PartNum',None)
    for table, bad in [('Part','OnHandQty'),('JobOper','ScrapQty'),('JobOper','ActScrapQty'),('OrderDtl','ExtPrice')]:
        cols[table].pop(bad,None)
    columns = {'generated':'2000-01-01 00:00:00 UTC', 'tables':{t:[{'name':c,'type':k} for c,k in f.items()] for t,f in cols.items()}}
    physical = {'generated':columns['generated'], 'tables':{t:{'schema':'Erp','full_name':f'Erp.{t}','table_type':'DB','fields':f} for t,f in columns['tables'].items()}}
    physical['tables']['OrderRel_UD']={'schema':'Erp','full_name':'Erp.OrderRel_UD','table_type':'DB','fields':[{'name':'ForeignSysRowID','type':'uniqueidentifier'},{'name':'Note_c','type':'nvarchar'}]}
    cp=root/'synthetic_columns.json'; cp.write_text(json.dumps(columns))
    sp=root/'synthetic_schema.json'; sp.write_text(json.dumps(physical))
    return cp,sp
