"""Part C — ResilientClient: compose classification, retry, breaker and fallback.

The simulator calls ``start`` for every logical LLM call, then ``on_success``
or ``on_error`` for every attempt. You answer with ``Send`` (try a provider,
maybe after a delay) or ``GiveUp`` (user gets a static degraded answer).

Tests: tests/test_client.py, then `make compare` (6 incident tapes).

Per-call scratch space lives in ``call.state`` (a dict). Suggested keys:
    "ladder": list[str]  providers this call may use, in order
    "idx":    int        position of the provider we are currently on
    "tries":  int        attempts already made on that provider

Decision table for ``on_error`` (implement it!):

| ErrorKind        | breaker              | then                                                    |
|------------------|----------------------|---------------------------------------------------------|
| CLIENT_ERROR     | release (not health) | GiveUp("client_error") — never retry, never fail over   |
| QUOTA_EXHAUSTED  | release              | cooldown provider for quota_cooldown_s; fail over now   |
| RATE_LIMITED     | release              | cooldown provider until now + retry_after; fail over now|
| OVERLOADED /     | on_failure           | retry SAME provider after backoff.delay(tries) if       |
| SERVER_ERROR /   |                      |   tries < max_attempts_per_provider AND budget allows   |
| TIMEOUT          |                      |   AND it fits the deadline; otherwise fail over         |

Cooldowns are per provider and shared by all calls (other calls must avoid a
rate-limited provider too). When every provider in the ladder is unusable,
you may wait for a *short* (<= 5 s) rate-limit cooldown if it fits the deadline.
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker
from gameday_lab.errors import ErrorKind, classify, counts_toward_breaker
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

RATE_LIMIT_WAIT_MAX_S = 5.0  # only wait for short (rate-limit) cooldowns, never for quota


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """PROVIDED: reads the `client:` block of configs/gameday.yaml."""
        self.providers = providers
        self.order = list(fallback_order)
        self.timeout_s = float(settings.get("attempt_timeout_s", 8.0))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))
        self.backoff = FullJitterBackoff(
            float(settings.get("backoff_base_s", 0.25)),
            float(settings.get("backoff_cap_s", 4.0)),
            random.Random(seed),
        )
        self.budget = RetryBudget(
            float(settings.get("retry_budget_ratio", 0.1)),
            float(settings.get("retry_budget_min_per_s", 0.5)),
            float(settings.get("retry_budget_max_tokens", 20.0)),
        )
        breaker_cfg = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_cfg) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}
        self.cooldown_kind: dict[str, ErrorKind | None] = {name: None for name in providers}

    # ------------------------------------------------------------------ provided helper
    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """PROVIDED. Build a Send for ``provider`` (after ``delay``), or None if its breaker refuses.

        Takes a breaker permit (carried in ``tag``), and caps the attempt timeout
        so it never runs past the call's deadline. Returns GiveUp("deadline") if
        there is not enough time left for a meaningful attempt.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None
        timeout = min(self.timeout_s, call.deadline - (now + delay))
        if timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")
        return Send(provider, timeout_s=timeout, delay_s=delay, tag=permit)

    # ------------------------------------------------------------------ routing and results
    def ladder(self, task: str) -> list[str]:
        """Return ``self.order`` filtered by capability —
        a "tool" call may only use providers with ``supports_tools``."""
        return [name for name in self.order if task != "tool" or self.providers[name].supports_tools]

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """Pick the provider for the next attempt.

        Walk the ladder from call.state["idx"]:
          * skip a provider still in cooldown at send time (now + delay)
          * otherwise try self._send(...); a non-None result is your answer
          * when skipping, move idx forward, reset tries, and set delay = 0
            (a failover goes out immediately)
        If the ladder is exhausted: optionally wait for the earliest *short*
        rate-limit cooldown (<= RATE_LIMIT_WAIT_MAX_S, breaker not OPEN, fits the
        deadline), else GiveUp("no_provider_available").
        """
        ladder: list[str] = call.state["ladder"]
        while call.state["idx"] < len(ladder):
            provider = ladder[call.state["idx"]]
            if now + delay >= self.cooldown_until[provider]:
                action = self._send(call, provider, now, delay)
                if action is not None:
                    if isinstance(action, Send):
                        call.state["tries"] += 1
                        counts = call.state["sent_counts"]
                        counts[provider] = counts.get(provider, 0) + 1
                    return action
            call.state["idx"] += 1
            call.state["tries"] = 0
            delay = 0.0

        # A brief rate limit may expire before this call's deadline. Quota
        # cooldowns are deliberately excluded, even when configured as short.
        candidates = sorted(
            (self.cooldown_until[name], idx, name)
            for idx, name in enumerate(ladder)
            if self.cooldown_kind[name] is ErrorKind.RATE_LIMITED
            and 0.0 < self.cooldown_until[name] - now <= RATE_LIMIT_WAIT_MAX_S
            and self.breakers[name].state is not BreakerState.OPEN
        )
        for ready_at, idx, provider in candidates:
            wait_s = ready_at - now
            if call.deadline - ready_at < self.min_time_left_s:
                continue
            sent_count = call.state["sent_counts"].get(provider, 0)
            if sent_count >= self.max_attempts_per_provider:
                continue
            action = self._send(call, provider, now, wait_s)
            if action is None:
                continue
            if isinstance(action, Send) and sent_count > 0 and not self.budget.try_acquire_retry(now):
                self.breakers[provider].release(action.tag, now)
                continue
            call.state["idx"] = idx
            call.state["tries"] = sent_count + 1 if isinstance(action, Send) else 0
            if isinstance(action, Send):
                call.state["sent_counts"][provider] = sent_count + 1
            return action
        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        """Record the request in the retry budget, initialise
        call.state (ladder, idx=0, tries=0) and route."""
        self.budget.record_request(now)
        call.state.update(ladder=self.ladder(call.task), idx=0, tries=0, sent_counts={})
        return self._route(call, now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Report success and latency to the provider's breaker."""
        self.breakers[attempt.provider].on_success(attempt.tag, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Classify the error, then retry, fail over, or stop."""
        kind = classify(err)
        breaker = self.breakers[attempt.provider]
        call = attempt.call

        if counts_toward_breaker(kind):
            breaker.on_failure(attempt.tag, now)
        else:
            breaker.release(attempt.tag, now)

        if kind is ErrorKind.CLIENT_ERROR:
            return GiveUp("client_error")

        if kind in (ErrorKind.QUOTA_EXHAUSTED, ErrorKind.RATE_LIMITED):
            cooldown_s = self.quota_cooldown_s if kind is ErrorKind.QUOTA_EXHAUSTED else max(
                0.0, err.retry_after if err.retry_after is not None else 0.0
            )
            until = now + cooldown_s
            if until >= self.cooldown_until[attempt.provider]:
                self.cooldown_until[attempt.provider] = until
                self.cooldown_kind[attempt.provider] = kind
        elif call.state["tries"] < self.max_attempts_per_provider and breaker.state is not BreakerState.OPEN:
            delay = self.backoff.delay(call.state["tries"])
            if call.deadline - (now + delay) >= self.min_time_left_s and self.budget.try_acquire_retry(now):
                return self._route(call, now, delay)

        call.state["idx"] += 1
        call.state["tries"] = 0
        return self._route(call, now)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """Release a permit if a delayed attempt was cancelled."""
        if isinstance(tag, Permit):
            self.breakers[tag.breaker].release(tag, now)
