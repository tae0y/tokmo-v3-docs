"""Persistent per-model transport waiting. No ping calls or internal retry loop."""
from datetime import timedelta
import hashlib

from tokmo.records.news_decision_store import kst, timestamp


class ModelDeferred(RuntimeError):
    def __init__(self, state):
        self.state = state
        super().__init__('model waiting: ' + state['category'])


def failure_category(exc):
    category = getattr(exc, 'category', None)
    if category in {'TRANSIENT', 'QUOTA', 'AUTH'}:
        return category
    # Only transport exceptions/known executor codes; never successful prose.
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return 'TRANSIENT'
    details = getattr(exc, 'failure_diagnostics', {})
    if details.get('http_status') in (401, 403):
        return 'AUTH'
    if (details.get('error_class') == 'connection_error'
            or (isinstance(details.get('http_status'), int) and 500 <= details['http_status'] <= 599)):
        return 'TRANSIENT'
    if str(exc) == 'QUOTA_EXCEEDED':
        return 'QUOTA'
    return None


class ModelWait:
    PREFIX = 'model_wait:'
    UNKNOWN_PREFIX = 'model_unknown:'
    UNKNOWN_STREAK, UNKNOWN_WINDOW = 3, timedelta(minutes=30)  # unclassified executor failures in a row = an outage
    PROBE_INTERVAL = timedelta(hours=6)  # a manual-required wait still probes once per interval, so recovery needs no operator

    def __init__(self, store):
        self.store = store
        self.failed_this_pass = set()

    def key(self, callback):
        return self.PREFIX + hashlib.sha256(str(getattr(callback, 'model', 'recorded')).encode()).hexdigest()[:24]

    def check(self, callback, at):
        key = self.key(callback)
        state = self.store.control(key)
        if hasattr(callback, 'available'):
            # Member state owns chain eligibility, including legacy outer QUOTA waits.
            # A failed pass never retries internally, even if its clock advances.
            if state and key in self.failed_this_pass:
                raise ModelDeferred(state)
            if not callback.available(at):
                raise ModelDeferred(callback.hold_state(at))
            return
        if state and (key in self.failed_this_pass or not state.get('retry_at') or timestamp(state['retry_at']) > at):
            if (state.get('manual_required') and key not in self.failed_this_pass and state.get('failed_at')
                    and timestamp(state['failed_at']) + self.PROBE_INTERVAL <= at):
                return  # One bounded probe: a failure re-arms the interval through failed_at; success clears the wait.
            raise ModelDeferred(state)

    def _unknown_streak(self, callback, exc, at):
        """Three unclassified executor failures of one model within the window are treated as a transient
        outage (whole-model wait) instead of three more per-item errors; any success resets the streak."""
        if getattr(exc, 'category', None) != 'EXECUTOR_UNKNOWN':
            return None
        key = self.UNKNOWN_PREFIX + self.key(callback)[len(self.PREFIX):]
        old = self.store.control(key) or {}
        recent = old.get('last_at') and timestamp(old['last_at']) + self.UNKNOWN_WINDOW >= at
        count = old.get('count', 0) + 1 if recent else 1
        self.store.set_control(key, dict(count=count, last_at=kst(at)))
        return 'TRANSIENT' if count >= self.UNKNOWN_STREAK else None

    def failed(self, callback, exc, at, call_id):
        category = failure_category(exc) or self._unknown_streak(callback, exc, at)
        if category is None:
            return None
        key = self.key(callback)
        old = self.store.control(key) or {}
        attempts = old.get('attempts', 0) + 1
        # Initial useful request + at most two recovery attempts. Even when a
        # long pass crosses the deadline, no second attempt in that same pass.
        minutes = (5, 15)[attempts-1] if category == 'TRANSIENT' and attempts < 3 else None
        retry_at = at + timedelta(minutes=minutes) if minutes else None
        # A chain says when its earliest member may be tried again; never retry before that.
        hint = getattr(exc, 'retry_at', None)
        if hasattr(callback, 'available') and hint is not None:
            retry_at = hint
        elif retry_at is not None and hint is not None and hint > retry_at:
            retry_at = hint
        state = dict(model=getattr(callback, 'model', 'recorded'), category=category,
                     attempts=attempts, since=old.get('since') or kst(at), failed_at=kst(at),
                     retry_at=kst(retry_at) if retry_at else None,
                     manual_required=retry_at is None, last_failed_call=call_id)
        self.store.set_control(key, state)
        self.failed_this_pass.add(key)
        return state

    def succeeded(self, callback):
        self.store.set_control(self.key(callback), None)
        self.store.set_control(self.UNKNOWN_PREFIX + self.key(callback)[len(self.PREFIX):], None)

    def states(self):
        return self.store.controls(self.PREFIX)

    def reset(self, model, at, reason):
        from tokmo.records.news_decision_store import encoded
        state = next((r for r in self.states() if r['model'] == model), None)
        if state is None or not isinstance(reason, str) or not reason.strip():
            raise ValueError('모델 대기 상태와 해제 이유를 확인해야 해')
        key = self.PREFIX + hashlib.sha256(model.encode()).hexdigest()[:24]
        with self.store.connection:
            self.store.connection.execute('INSERT INTO news_decision_control VALUES(?,?)',
                ('model_reset:'+key+':'+kst(at), encoded(dict(at=kst(at), reason=reason.strip()[:400], previous=state))))
            self.store.connection.execute('DELETE FROM news_decision_control WHERE key=?', (key,))
        return dict(model=model, reset=True, model_calls=0)
