from datetime import datetime,timezone
from tokmo.programs.quant_decision_fundamentals import sec_metrics,dart_metrics

NOW=datetime(2026,9,7,tzinfo=timezone.utc)


def fact(value,end='2026-01-25',filed='2026-02-25',start=None):
    r={'val':value,'end':end,'filed':filed,'form':'10-K','accn':'0000000000-26-000001'}
    if start:r['start']=start
    return r


def test_sec_selects_current_concept_and_omits_discontinued_balances():
    payload={'cik':1045810,'facts':{'us-gaap':{
        'RevenueFromContractWithCustomerExcludingAssessedTax':{'units':{'USD':[fact(100,end='2022-01-30',filed='2022-02-25',start='2021-01-31')]}},
        'Revenues':{'units':{'USD':[fact(300,start='2025-01-26')]}},
        'Assets':{'units':{'USD':[fact(400)]}},
        'Liabilities':{'units':{'USD':[fact(60,end='2022-01-30',filed='2022-02-25')]}}
    }}}
    metrics,evidence=sec_metrics(payload,as_of=NOW)
    assert metrics['revenue']['value']=='300'
    assert 'liabilities' not in metrics
    assert all('2022' not in e['text'] for e in evidence)


def test_sec_filters_future_filings_and_quarterly_duration():
    payload={'cik':1,'facts':{'us-gaap':{'Revenues':{'units':{'USD':[
        fact(300,start='2025-01-26'),fact(999,start='2025-01-26',filed='2026-12-30'),
        fact(1000,start='2025-11-01')
    ]}}}}}
    metrics,_=sec_metrics(payload,as_of=NOW)
    assert metrics['revenue']['value']=='300'


def test_dart_annual_scope_and_publication_date():
    row={'corp_code':'00164779','bsns_year':'2025','reprt_code':'11011','fs_div':'CFS','account_nm':'매출액',
         'rcept_no':'20260318001394','currency':'KRW','thstrm_amount':'1,500','frmtrm_amount':'1,000'}
    data={'status':'000','list':[row,dict(row,fs_div='OFS',thstrm_amount='999')]}
    metrics,_=dart_metrics(data,corp_code='00164779',year=2025,as_of=NOW)
    assert metrics['revenue']['value']=='1500' and metrics['revenue_growth']['value']=='0.5'
    early=datetime(2026,3,18,1,tzinfo=timezone.utc)
    assert dart_metrics(data,corp_code='00164779',year=2025,as_of=early)==({},[])
