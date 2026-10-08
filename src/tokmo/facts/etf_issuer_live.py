"""Public issuer GET transport, independent of brokerage credentials."""
from urllib import request
from urllib.parse import urlsplit

HOSTS={'www.plusetf.co.kr','www.soletf.com','www.samsungfund.com',
       'papi.aceetf.co.kr','www.riseetf.co.kr','investments.miraeasset.com'}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('issuer redirect rejected')


class IssuerLiveTransport:
    def __init__(self):
        self.opener=request.build_opener(request.ProxyHandler({}),NoRedirect())
        self.requests=0

    def __call__(self,url):
        parts=urlsplit(url)
        if parts.scheme!='https' or parts.hostname not in HOSTS or parts.username or parts.password or parts.port not in (None,443):
            raise ValueError('unsupported issuer URL')
        self.requests+=1
        req=request.Request(url,headers={'User-Agent':'Tokmo/1.0 (personal portfolio research)',
                                        'Accept':'application/json,text/html'},method='GET')
        with self.opener.open(req,timeout=6) as reply: body=reply.read(3_000_001)
        if len(body)>3_000_000:raise ValueError('issuer response too large')
        return body.decode('utf-8')
