"""Ordered model chain: the preferred model answers; the next one only covers a provider outage.

Rules (the same vocabulary as news_model_wait):

* A member is skipped while it is *down*. Down is set only by a classified transport failure:
  QUOTA, AUTH or TRANSIENT (``failure_category``). A bad answer, a REQUEST error or an
  unknown executor failure is raised as-is: another provider must not pay for a broken request.
* TRANSIENT waits 5 min, then 15 min, then 6 h between attempts; QUOTA and AUTH wait 6 h
  (``PROBE_INTERVAL``). With a ``quota_reset`` lookup (the Hermes quota snapshot), a QUOTA
  failure instead waits until the provider's known reset plus 5 min; unknown reset = 6 h.
  Waiting is sticky across worker passes (JSON state file) and the preferred model is tried
  again once its wait ends, so recovery needs no operator.
* "Retry is due" and "recovered" are different things. A member stays *unresolved* (its state
  entry is kept, the health item stays, ``active`` still names the fallback) until it actually
  answers again; only that success clears the entry and produces the recovery notice.
* One call tries each available member in order and stops at the first answer. When every
  member is down, the last classified failure is raised so the engine's ModelWait treats the
  whole chain as waiting, exactly like a single model.
* The chain presents ONE stable identity to the engine (``model`` = ``chain:<stage>[a>b]``,
  ``reasoning_effort`` = the preferred member's): journal keys, budget reservations and the
  whole-chain wait are keyed by the chain, never by whichever member happened to answer. The
  member that actually answered is in the response's ``_model``.
* Every journal row is one physical attempt. When a member fails and another will be tried, the
  failed attempt is handed to ``attempt_sink`` (bound by the engine for one journaled call) and
  gets its own durable ERROR row keeping its reservation in the day's budget; the call's own row
  then stands for the next attempt. The sink may refuse the next member by raising (budget). The
  last attempt of a call that fails entirely is the call row itself.
* Every switch and every return to a better member is recorded in ``notices`` (Korean, one
  line each) for the worker's health notice, and every failed attempt in ``events``.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from tokmo.programs.news_model_wait import ModelWait, failure_category
from tokmo.records.news_decision_store import kst, timestamp

TRANSIENT_BACKOFF = (timedelta(minutes=5), timedelta(minutes=15))
PROBE_INTERVAL = ModelWait.PROBE_INTERVAL  # 6h, shared with the single-model wait
RESET_BUFFER = timedelta(minutes=5)  # try again this long after a known quota reset
RESET_MAX_AHEAD = timedelta(
    days=7
)  # a reset further away than a weekly window is not trusted


class ChainExhausted(RuntimeError):
    """Every member is down; carries the category of the preferred member's last failure and
    ``retry_at``, the earliest instant any member may be tried again (ModelWait honours it so the
    whole-chain wait never ends before a member is eligible)."""

    def __init__(self, stage, category, states, retry_at=None):
        self.stage, self.category, self.states, self.retry_at = (
            stage,
            category,
            states,
            retry_at,
        )
        super().__init__(
            "subscription inference failed: "
            + category
            + " (chain exhausted: "
            + stage
            + ")"
        )


class ModelChain:
    def __init__(self, members, *, stage, clock, state_path=None, quota_reset=None):
        """``clock`` returns the current aware instant (injected; the worker passes the wall clock or
        the replay time). ``quota_reset(provider, at)`` (optional) returns the aware instant the
        provider's exhausted quota window resets, or None; a QUOTA failure then waits until that
        instant plus RESET_BUFFER instead of the flat PROBE_INTERVAL. It is consulted at every QUOTA
        failure, so a reset learned at a later probe applies from then on."""
        if not members:
            raise ValueError("a chain needs at least one model")
        self.members = list(members)
        self.stage = stage
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self.quota_reset = quota_reset
        self.notices, self.events = [], []
        self.attempt_sink = None  # bound by the engine per journaled call: (member, category, at) -> None
        self._state = self._load()

    # --- identity the engine sees --------------------------------------------------------------
    @property
    def model(self):
        """Stable chain identity; the same key whichever member answers."""
        return (
            "chain:" + self.stage + "[" + ">".join(m.model for m in self.members) + "]"
        )

    @property
    def reasoning_effort(self):
        return getattr(self.members[0], "reasoning_effort", None)

    @property
    def max_output_tokens(self):
        return max(getattr(m, "max_output_tokens", 0) or 0 for m in self.members)

    @property
    def policy_fingerprint(self):
        return getattr(self.members[0], "policy_fingerprint", None)

    @property
    def incremental_policy_fingerprint(self):
        return getattr(self.members[0], "incremental_policy_fingerprint", None)

    @property
    def review_efficiency_fingerprint(self):
        return getattr(self.members[0], "review_efficiency_fingerprint", None)

    @property
    def primary(self):
        return self.members[0]

    # --- state -------------------------------------------------------------------------------
    def _load(self):
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload.get(self.stage, {}) if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        if self.state_path is None:
            return
        payload = {}
        if self.state_path.exists():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError):
                payload = {}
        payload[self.stage] = (
            self._state
        )  # other stages' keys are re-read and preserved
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_name(
            self.state_path.name + f".{os.getpid()}.tmp"
        )
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=1))
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(self.state_path)

    def _now(self):
        return timestamp(self.clock())

    @staticmethod
    def _provider(member):
        provider = getattr(member, "provider", None)
        if provider:
            return provider
        return member.model.split(":", 1)[0] if ":" in member.model else "zai"

    def unresolved(self, member):
        """True from a classified failure until the member answers again (retry due or not)."""
        return member.model in self._state

    def down(self, member, at):
        """True while ``member`` must not be tried (its retry time is still ahead)."""
        state = self._state.get(member.model)
        if not state:
            return False
        return timestamp(state["retry_at"]) > at

    def available(self, at):
        return [m for m in self.members if not self.down(m, at)]

    def next_retry_at(self, at):
        """Earliest instant a member becomes eligible; None when one is eligible now."""
        if self.available(at):
            return None
        return min(timestamp(self._state[m.model]["retry_at"]) for m in self.members)

    def hold_state(self, at):
        """The deferral the engine reports when no member may be tried yet: no journal row, no
        reservation, no ModelWait attempt, because no physical attempt happens."""
        primary = self._state.get(self.members[0].model) or {}
        return dict(
            model=self.model,
            category=primary.get("category", "TRANSIENT"),
            attempts=0,
            since=primary.get("since"),
            failed_at=primary.get("failed_at"),
            retry_at=kst(self.next_retry_at(at)),
            manual_required=False,
            held=True,
        )

    @property
    def active(self):
        """The member currently trusted to answer: the first one with no unresolved failure. A
        member whose retry is merely due is still the fallback's turn until it really answers."""
        trusted = [m for m in self.members if not self.unresolved(m)]
        return trusted[0] if trusted else self.members[0]

    # --- the call ----------------------------------------------------------------------------
    def __call__(self, request):
        at = self._now()
        before = self.active.model
        last_error = None
        for index, member in enumerate(self.members):
            if self.down(member, at):
                continue
            try:
                result = member(request)
            except Exception as exc:
                category = failure_category(exc)
                if category is None:
                    raise
                has_next = any(not self.down(m, at) for m in self.members[index + 1 :])
                self._failed(member, category, at, has_next)
                last_error = exc
                continue
            self._succeeded(member, at, before)
            return result
        primary_state = self._state.get(self.members[0].model) or {}
        raise ChainExhausted(
            self.stage,
            primary_state.get("category")
            or failure_category(last_error)
            or "TRANSIENT",
            dict(self._state),
            retry_at=self.next_retry_at(at),
        ) from last_error

    def _failed(self, member, category, at, has_next=False):
        old = self._state.get(member.model) or {}
        attempts = old.get("attempts", 0) + 1
        source, known = "probe_interval", None
        if category == "TRANSIENT" and attempts <= len(TRANSIENT_BACKOFF):
            wait = TRANSIENT_BACKOFF[attempts - 1]
        else:
            wait = PROBE_INTERVAL
            if category == "QUOTA" and self.quota_reset is not None:
                known = self.quota_reset(self._provider(member), at)
                if known is not None and at < known <= at + RESET_MAX_AHEAD:
                    wait, source = known - at + RESET_BUFFER, "quota_reset"
                else:
                    known = None
        state = dict(
            category=category,
            attempts=attempts,
            since=old.get("since") or kst(at),
            failed_at=kst(at),
            retry_at=kst(at + wait),
            retry_source=source,
            **({"quota_reset_at": kst(known)} if known else {}),
        )
        self._state[member.model] = state
        self._save()
        self.events.append(
            dict(
                kind="failed",
                stage=self.stage,
                model=member.model,
                category=category,
                at=kst(at),
                attempts=attempts,
                retry_at=state["retry_at"],
                retry_source=source,
            )
        )
        if source == "quota_reset":
            local = lambda text: text[5:16].replace("T", " ")
            self.notices.append(
                f"{self.stage}: {member.model} QUOTA · 초기화 {local(state['quota_reset_at'])} 뒤 {local(state['retry_at'])}에 복귀 확인"
            )
        else:
            hours = wait.total_seconds() / 3600
            when = (
                ("%d분" % (wait.total_seconds() // 60))
                if hours < 1
                else ("%d시간" % hours)
            )
            self.notices.append(
                f"{self.stage}: {member.model} {category} · {when} 뒤 다시 시도"
            )
        if self.attempt_sink is not None and has_next:
            # Another member will be tried: journal this attempt separately (the call row now stands
            # for the next attempt). May raise to refuse the next member on budget. A last member's
            # failure needs no extra row: the call row itself is that attempt.
            self.attempt_sink(member, category, at)

    def _succeeded(self, member, at, before):
        was_down = member.model in self._state
        if was_down:
            del self._state[member.model]
            self._save()
        if member is not self.members[0]:
            self.events.append(
                dict(
                    kind="answered_by",
                    stage=self.stage,
                    model=member.model,
                    preferred=self.members[0].model,
                    at=kst(at),
                )
            )
        # Notices mark a change only: the call that first moved to a fallback, and the call that
        # brought the preferred model back. A pass that keeps using the fallback stays quiet.
        if member is not self.members[0] and member.model != before:
            self.notices.append(
                f"{self.stage}: {self.members[0].model} 대신 {member.model}가 답함"
            )
        elif member is self.members[0] and was_down:
            self.notices.append(f"{self.stage}: {member.model} 복귀")

    def summary(self):
        at = self._now()
        return dict(
            stage=self.stage,
            identity=self.model,
            preferred=self.members[0].model,
            active=self.active.model,
            members=[m.model for m in self.members],
            waiting={
                model: dict(
                    category=s["category"],
                    retry_at=s["retry_at"],
                    attempts=s["attempts"],
                    retry_due=timestamp(s["retry_at"]) <= at,
                )
                for model, s in self._state.items()
            },
            events=list(self.events),
        )
