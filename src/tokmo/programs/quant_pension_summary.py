"""Minimal non-ETF pension asset summary from the existing source workbook.

No holdings revision, transaction or available buying power is inferred.
Classification is supplied explicitly by source product name. Old source
valuation dates remain visible when combined with newer ETF quotes.
"""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from tokmo.values.quant_decisions import instant,number
from tokmo.facts.pension_account_status_excel import (
    check_landing_file,zip_entry_pass,load_workbook_from_bytes,
    _map_header_row,_cell_text,_cell_decimal,_validate_content,
    ACCOUNT_STATUS_REQUIRED_FIELDS,ACCOUNT_STATUS_OPTIONAL_FIELDS,
    ACCOUNT_STATUS_HEADER_ALIASES,
)


def summarize_rows(rows,classification,*,account,source_as_of,source):
    totals=defaultdict(lambda:Decimal(0));seen=set()
    for row in rows:
        name=row['name']
        if name in seen:raise ValueError('duplicate pension product')
        seen.add(name);kind=classification.get(name)
        if kind not in {'etf','cash_like','bond','fund','other'}:raise ValueError('pension product classification required')
        totals[kind]+=number(row['evaluation_amount'],minimum=0)
    if set(classification)!=seen:raise ValueError('classification does not match workbook')
    other=sum((v for k,v in totals.items() if k!='etf'),Decimal(0))
    return {'account_summaries':[{'account':account,'scope':'retirement','currency':'KRW',
        'other_assets':str(other),'observed_at':instant(source_as_of).isoformat(),'source':source,'complete':False,
        'source_total':str(sum(totals.values(),Decimal(0))),'source_etf_total':str(totals['etf']),
        'breakdown':{k:str(v) for k,v in totals.items() if k!='etf'},
        'coverage_note':'ETF는 현재 연결 보유자료, 나머지 자산은 표시된 엑셀 기준일의 평가액. 현금성 상품 평가액을 주문가능금액으로 사용하지 않음.'}]}


def read_pension_summary(path,classification,*,account,source_as_of):
    raw=check_landing_file(Path(path));zip_entry_pass(raw)
    workbook,sheet=load_workbook_from_bytes(raw)
    try:
        columns=_map_header_row(sheet,1,ACCOUNT_STATUS_REQUIRED_FIELDS,ACCOUNT_STATUS_OPTIONAL_FIELDS,ACCOUNT_STATUS_HEADER_ALIASES)
        if 'evaluation_amount' not in columns:raise ValueError('pension evaluation amount column missing')
        maximum=_validate_content(sheet,data_start_row=2,max_rows=256)
        rows=[{'name':_cell_text(sheet,n,columns['instrument_repr']),
               'evaluation_amount':_cell_decimal(sheet,n,columns['evaluation_amount'])} for n in range(2,maximum+1)]
        return summarize_rows(rows,classification,account=account,source_as_of=source_as_of,source=str(Path(path)))
    finally:workbook.close()
