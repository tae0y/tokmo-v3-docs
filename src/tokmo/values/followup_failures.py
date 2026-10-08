"""Small persisted retry policy for read-only analysis, not trade execution."""
from datetime import timedelta
from email.utils import parsedate_to_datetime
import errno
import math
from urllib.error import HTTPError, URLError

from tokmo.values.quant_decisions import instant

MAX_ATTEMPTS = 3
RETRY_DELAYS = (60, 300)


def retry_time(error, *, at, tries):
    when=instant(at)+timedelta(seconds=RETRY_DELAYS[min(tries-1,len(RETRY_DELAYS)-1)])
    raw=(getattr(error,'headers',None) or {}).get('Retry-After')
    if raw:
        try:
            seconds=float(raw)
            if math.isfinite(seconds) and 0<=seconds<=31536000:
                when=max(when,instant(at)+timedelta(seconds=seconds))
        except (ValueError,TypeError,OverflowError):
            try:when=max(when,instant(parsedate_to_datetime(raw)))
            except (ValueError,TypeError,OverflowError):pass
    return when.isoformat()


def transient_model_error(error):
    if getattr(error,'inference_retryable',None) is True:return True
    if isinstance(error, HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504}
    if isinstance(error, (TimeoutError, ConnectionError, URLError)):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ECONNRESET, errno.ECONNREFUSED, errno.ENETUNREACH,
        errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EPIPE,
    }


def failed_attempt(error, previous, *, at, attempt):
    previous = previous if previous and previous.get('attempt') == attempt else {}
    tries = previous.get('tries', 0) + 1
    retryable = transient_model_error(error)
    requested_limit=getattr(error,'inference_retry_limit',MAX_ATTEMPTS)
    limit=requested_limit if type(requested_limit) is int and 1<=requested_limit<=MAX_ATTEMPTS else MAX_ATTEMPTS
    code = getattr(error, 'code', None)
    return {'attempt': attempt, 'tries': tries, 'error': type(error).__name__,
            'http_status': code if type(code) is int else None,
            'failed_at': instant(at).isoformat(), 'retryable': retryable,
            'retry_at': retry_time(error,at=at,tries=tries)
                if retryable and tries < limit else None}


def retry_due(failure, *, at, attempt):
    return (not failure or failure.get('attempt') != attempt or
            bool(failure.get('retry_at') and instant(failure['retry_at']) <= instant(at)))


def retry_attempt(attempt, failure):
    if failure and failure.get('attempt') == attempt:
        return attempt + '/retry-' + str(failure.get('tries', 1))
    return attempt


def failure_notice(failure):
    """No provider text, URLs or credentials in operator-visible diagnostics."""
    return {k: failure.get(k) for k in
            ('error', 'http_status', 'tries', 'failed_at', 'retry_at', 'retryable')}
