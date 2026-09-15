"""Optional value diagnostics over operator-provided synthetic observations."""
import pytest
import re
from epicor_mcp.sql import domains


def test_no_observed_tenant_domains_are_bundled():
    assert not domains.DOMAINS
    assert not domains.PLANTS
    assert not domains.TABLE_ROWS
    assert not domains.ALWAYS_FALSE
    assert not domains.ALWAYS_TRUE
    assert not domains.EMPTY_COLUMNS
    assert not domains.SURROGATE_KEYS


@pytest.fixture
def observations(monkeypatch):
    resource = domains.Domain('Resource', 'ResourceType', ('MACHINE', 'LABOR'), (8, 2), True)
    scrap = domains.Domain('JobOper', 'EstScrapType', ('%', 'Q'), (7, 3), True)
    monkeypatch.setattr(domains, 'DOMAINS', {resource.key:resource, scrap.key:scrap})
    monkeypatch.setattr(domains, 'ALWAYS_FALSE', {'SugPoDtl':frozenset({'Buy'})})
    monkeypatch.setattr(domains, 'ALWAYS_TRUE', {'SugPoDtl':frozenset({'CreateApprovedPO'})})
    monkeypatch.setattr(domains, 'TABLE_ROWS', {'SugPoDtl':10})
    monkeypatch.setattr(domains, 'PLANTS', {'10':'North Factory', '20':'South Factory'})
    monkeypatch.setattr(domains, 'PLANT_NAME_TO_CODE', {'north factory':'10','south factory':'20'})
    return resource


def kinds(sql, **kwargs):
    return {f.kind for f in domains.ground(sql, **kwargs)}


def test_operator_domain_normalizes_case_and_trailing_space(observations):
    assert observations.contains('machine ')
    assert not observations.contains('unknown')
    assert domains.known_domain('Erp.Resource', 'resourcetype') is observations


def test_operator_flags_are_diagnosed_without_rewriting_sql(observations):
    sql = 'select top 5 count(*) as N from Erp.SugPoDtl as S where S.Buy = true'
    findings = domains.ground(sql)
    found = next(f for f in findings if f.kind == 'flag_never_set')
    assert found.causes_empty and found.certain
    assert found.table == 'SugPoDtl' and found.column == 'Buy'


@pytest.mark.parametrize('predicate', ['S.Buy = false', 'S.Buy <> true', 'NOT (S.Buy = true)'])
def test_correct_negative_flag_filters_are_not_certain_empty_predictions(observations, predicate):
    findings = domains.ground(f'select top 5 count(*) as N from Erp.SugPoDtl as S where {predicate}')
    assert not any(f.causes_empty for f in findings)


def test_or_branch_does_not_claim_the_query_must_be_empty(observations):
    findings = domains.ground('select top 5 count(*) as N from Erp.SugPoDtl as S where S.Buy = true OR S.PartNum = \'SYNTHETIC\'')
    assert not any(f.causes_empty for f in findings)


def test_site_names_only_resolve_from_configured_map(observations):
    assert domains.plant_code('North Factory') == '10'
    assert domains.plant_code('site 20') == '20'
    assert domains.plant_code('99') is None
    found = domains.ground("select top 5 count(*) as N from Erp.JobHead as J where J.Plant = 'North Factory'", row_count=0)
    assert any(f.suggest == "[JobHead].[Plant] = '10'" for f in found)


def test_unconfigured_company_and_brand_never_create_tenant_claims():
    assert 'company_not_the_install_id' not in kinds("select top 5 count(*) as N from Erp.Part as P where P.Company = 'OTHER'")
    assert 'brand_code_matched_exactly' not in kinds("select top 5 count(*) as N from Erp.Part as P where P.CommercialBrand = 'SYNTHETIC'", row_count=0)


def test_literal_wildcard_under_equals_is_diagnosed():
    assert 'wildcard_under_equals' in kinds("select top 5 count(*) as N from Erp.Part as P where P.PartNum = 'PART%'")


def test_literal_percent_in_known_enum_is_valid(observations):
    assert not kinds("select top 5 count(*) as N from Erp.JobOper as J where J.EstScrapType = '%'")


def test_unknown_enumerated_code_is_diagnosed(observations):
    assert 'value_outside_an_epicor_code_set' in kinds("select top 5 count(*) as N from Erp.Resource as R where R.ResourceType = 'UNKNOWN'")


def test_free_text_equals_only_suggests_after_zero_rows():
    sql = "select top 5 count(*) as N from Erp.Customer as C where C.Name = 'Example Customer'"
    assert not kinds(sql)
    assert 'exact_match_on_a_free_text_column' in kinds(sql, row_count=0)


def test_invalid_sql_is_failsoft():
    assert domains.ground('') == []
    assert domains.ground('this is not sql') == []


@pytest.mark.parametrize('table,column,literal,kind,certain,suggestion', [
    ('Part', 'PartNum', 'SYNTHETIC%', 'wildcard_under_equals', True,
     "[Part].[PartNum] like 'SYNTHETIC%'"),
    ('Customer', 'Name', 'Sample Buyer', 'exact_match_on_a_free_text_column', False,
     "[Customer].[Name] like '%Sample Buyer%'"),
])
def test_generic_diagnostic_messages_use_only_the_callers_values(
    table, column, literal, kind, certain, suggestion,
):
    sql = f"select top 5 * from Erp.{table} as T where T.{column} = '{literal}'"
    finding = next(item for item in domains.ground(sql, row_count=0) if item.kind == kind)
    assert finding.given == literal
    assert finding.certain is certain
    assert finding.causes_empty
    assert finding.suggest == suggestion
    assert '=' in finding.message
    assert not re.search(r'\d', finding.message), 'generic advice must not embed observed counts'
    assert domains.AS_OF not in finding.message, 'generic SQL advice has no tenant measurement'
    assert set(re.findall(r"'([^']*)'", finding.message)) <= {literal, f'%{literal}%'}
