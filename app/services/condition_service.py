from __future__ import annotations

import asyncio
import hashlib
import logging
import string
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    ConditionDelivery,
    ConditionDeliveryState,
    ConditionObservation,
    ConditionSubscription,
    ConditionSubscriptionState,
    ConditionTransition,
    User,
)
from app.db.session import SessionLocal
from app.services.condition_provider import (
    ConditionObservation as ProviderObservation,
)
from app.services.condition_provider import (
    ConditionProviderError,
    ConditionProviderRegistry,
    build_default_condition_provider_registry,
    normalize_provider_type,
    parse_provider_config,
    serialize_provider_config,
)
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

DEFAULT_CONDITION_MESSAGE = "Условие изменилось: {previous_state} → {current_state}"
MAX_CONDITION_MESSAGE_LENGTH = 4096
_MESSAGE_FIELDS = {"previous_state", "current_state", "provider_type"}


SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True, slots=True)
class ConditionClaim:
    id: int
    user_id: int
    chat_id: int
    provider_type: str
    target: str
    config_json: str
    message_template: str
    poll_interval_seconds: int
    trigger_on_initial: bool
    lease_token: str


class ConditionPollOutcome:
    SUCCESS = "success"
    FAILURE = "failure"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class ConditionProcessResult:
    subscription_id: int
    outcome: str
    state: str | None = None
    error_code: str | None = None
    transition_sequence: int | None = None
    transition_created: bool = False
    deduplicated: bool = False


@dataclass
class ConditionCycleSummary:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    stale: int = 0
    transitions: int = 0
    deduplicated: int = 0
    disabled: bool = False

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "stale": self.stale,
            "transitions": self.transitions,
            "deduplicated": self.deduplicated,
            "disabled": self.disabled,
        }


@dataclass
class ConditionMetrics:
    polls: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    rate_limited: int = 0
    transitions: int = 0
    deduplicated: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "polls": self.polls,
            "successes": self.successes,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "rate_limited": self.rate_limited,
            "transitions": self.transitions,
            "deduplicated": self.deduplicated,
        }


condition_metrics = ConditionMetrics()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_message_template(template: str) -> str:
    if not isinstance(template, str) or len(template) > MAX_CONDITION_MESSAGE_LENGTH:
        raise ValueError("condition message template is too long")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in template):
        raise ValueError("condition message template contains control characters")
    try:
        parsed = string.Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is not None and field_name not in _MESSAGE_FIELDS:
                raise ValueError("condition message template contains an unsupported field")
            if format_spec or conversion:
                raise ValueError("condition message template contains an unsupported format")
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid condition message template") from exc
    return template


def render_condition_message(
    template: str,
    *,
    previous_state: str | None,
    current_state: str,
    provider_type: str,
) -> str:
    _validate_message_template(template)
    try:
        message = template.format(
            previous_state=previous_state or "unknown",
            current_state=current_state,
            provider_type=provider_type,
        )
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError("invalid condition message template") from exc
    if len(message) > MAX_CONDITION_MESSAGE_LENGTH:
        raise ValueError("rendered condition message is too long")
    return message


def _bounded_target_hash(target: str) -> str:
    return hashlib.sha256(target.encode("utf-8")).hexdigest()


def _bounded_error_code(value: str) -> str:
    normalized = value.strip().lower()
    return normalized[:64] if normalized else "provider_error"


def _retry_delay_seconds(
    failure_count: int,
    retry_after: int | None,
    *,
    settings_obj: Any,
) -> int:
    configured_base = settings_obj.condition_retry_base_seconds
    configured_max = settings_obj.condition_retry_max_seconds
    exponential = configured_base * (2 ** min(max(failure_count - 1, 0), 20))
    delay = min(exponential, configured_max)
    if retry_after is not None:
        delay = min(max(int(retry_after), 1), configured_max)
    return max(1, int(delay))


def _coerce_observation(value: Any) -> ProviderObservation:
    if isinstance(value, ProviderObservation):
        return value
    if isinstance(value, Mapping):
        state = value.get("state")
        if not isinstance(state, str):
            raise ConditionProviderError("malformed_observation")
        fingerprint = value.get("fingerprint")
        if fingerprint is not None and not isinstance(fingerprint, str):
            raise ConditionProviderError("malformed_observation")
        return ProviderObservation(state=state, fingerprint=fingerprint)
    return ProviderObservation(
        state=value.state,
        fingerprint=getattr(value, "fingerprint", None),
    )


class ConditionService:
    """Persistence and orchestration boundary for isolated condition polling."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        registry: ConditionProviderRegistry | None = None,
        settings_obj: Any | None = None,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session_factory = session_factory or SessionLocal
        self.settings = settings_obj or settings
        self.registry = registry or build_default_condition_provider_registry(self.settings)
        self.now_fn = now_fn

    def _now(self, now_utc: datetime | None = None) -> datetime:
        return _as_utc(now_utc or self.now_fn())

    def _interval(self, value: int | None) -> int:
        interval = self.settings.condition_poll_interval_seconds if value is None else int(value)
        if interval < 30 or interval > 86_400:
            raise ValueError("condition poll interval must be between 30 and 86400 seconds")
        return interval

    async def create_subscription(
        self,
        *,
        user_id: int,
        chat_id: int,
        provider_type: str,
        target: str,
        message_template: str | None = None,
        poll_interval_seconds: int | None = None,
        trigger_on_initial: bool = False,
        config: Mapping[str, Any] | None = None,
        enabled: bool = True,
        next_poll_at_utc: datetime | None = None,
    ) -> ConditionSubscription:
        normalized_provider_type = normalize_provider_type(provider_type)
        provider = self.registry.get(normalized_provider_type)
        if not isinstance(target, str) or not target or len(target) > 2048:
            raise ValueError("condition target is invalid")
        if any(char.isspace() or ord(char) < 32 for char in target):
            raise ValueError("condition target is invalid")
        validator = getattr(provider, "validate_target", None)
        if callable(validator):
            validator(target)
        normalized_config = serialize_provider_config(
            config,
            authorization_env_allowlist=getattr(
                self.settings, "condition_authorization_env_allowlist", ()
            ),
        )
        template = _validate_message_template(message_template or DEFAULT_CONDITION_MESSAGE)
        interval = self._interval(poll_interval_seconds)
        now = self._now(next_poll_at_utc)

        async with self.session_factory() as session, session.begin():
            owner = await session.scalar(select(User).where(User.id == user_id))
            if owner is None or owner.chat_id != chat_id:
                raise ValueError("condition subscription owner is invalid")
            subscription = ConditionSubscription(
                user_id=user_id,
                chat_id=chat_id,
                provider_type=normalized_provider_type,
                target=target,
                target_hash=_bounded_target_hash(target),
                config_json=normalized_config,
                message_template=template,
                poll_interval_seconds=interval,
                trigger_on_initial=bool(trigger_on_initial),
                state=(
                    ConditionSubscriptionState.ACTIVE.value
                    if enabled
                    else ConditionSubscriptionState.DISABLED.value
                ),
                next_poll_at_utc=now,
                failure_count=0,
                transition_sequence=0,
            )
            session.add(subscription)
            await session.flush()
            return subscription

    async def claim_due_subscriptions(
        self,
        *,
        limit: int | None = None,
        now_utc: datetime | None = None,
    ) -> list[ConditionClaim]:
        batch_size = self.settings.condition_poll_batch_size if limit is None else int(limit)
        batch_size = max(1, min(batch_size, 100))
        now = self._now(now_utc)
        claims: list[ConditionClaim] = []
        async with self.session_factory() as session, session.begin():
            statement = (
                select(ConditionSubscription)
                .where(
                    ConditionSubscription.state == ConditionSubscriptionState.ACTIVE.value,
                    ConditionSubscription.next_poll_at_utc <= now,
                    or_(
                        ConditionSubscription.lease_until_utc.is_(None),
                        ConditionSubscription.lease_until_utc <= now,
                    ),
                )
                .order_by(
                    ConditionSubscription.next_poll_at_utc,
                    ConditionSubscription.id,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            subscriptions = list((await session.scalars(statement)).all())
            lease_until = now + timedelta(seconds=self.settings.condition_lease_duration_seconds)
            for subscription in subscriptions:
                lease_token = uuid4().hex
                subscription.lease_token = lease_token
                subscription.lease_until_utc = lease_until
                claims.append(
                    ConditionClaim(
                        id=subscription.id,
                        user_id=subscription.user_id,
                        chat_id=subscription.chat_id,
                        provider_type=subscription.provider_type,
                        target=subscription.target,
                        config_json=subscription.config_json,
                        message_template=subscription.message_template,
                        poll_interval_seconds=subscription.poll_interval_seconds,
                        trigger_on_initial=subscription.trigger_on_initial,
                        lease_token=lease_token,
                    )
                )
        return claims

    async def renew_claim_lease(
        self,
        claim: ConditionClaim,
        *,
        now_utc: datetime | None = None,
    ) -> bool:
        """Renew a still-live token-owned lease immediately before provider I/O."""

        now = self._now(now_utc)
        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(ConditionSubscription)
                .where(
                    ConditionSubscription.id == claim.id,
                    ConditionSubscription.state == ConditionSubscriptionState.ACTIVE.value,
                    ConditionSubscription.lease_token == claim.lease_token,
                    ConditionSubscription.lease_until_utc > now,
                )
                .with_for_update()
            )
            if subscription is None:
                return False
            subscription.lease_until_utc = now + timedelta(
                seconds=self.settings.condition_lease_duration_seconds
            )
            return True

    async def _record_failure(
        self,
        claim: ConditionClaim,
        *,
        code: str,
        retry_after_seconds: int | None,
        latency_ms: int | None,
        status_code: int | None = None,
        now_utc: datetime | None = None,
    ) -> ConditionProcessResult:
        now = self._now(now_utc)
        safe_code = _bounded_error_code(code)
        safe_retry_after = (
            None if retry_after_seconds is None else max(1, min(int(retry_after_seconds), 86_400))
        )
        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(ConditionSubscription)
                .where(
                    ConditionSubscription.id == claim.id,
                    ConditionSubscription.lease_token == claim.lease_token,
                )
                .with_for_update()
            )
            if subscription is None:
                return ConditionProcessResult(
                    subscription_id=claim.id,
                    outcome=ConditionPollOutcome.STALE,
                )
            subscription.failure_count = int(subscription.failure_count or 0) + 1
            delay = _retry_delay_seconds(
                subscription.failure_count,
                safe_retry_after,
                settings_obj=self.settings,
            )
            next_retry = now + timedelta(seconds=delay)
            subscription.next_retry_at_utc = next_retry
            subscription.next_poll_at_utc = next_retry
            subscription.last_error_code = safe_code
            subscription.lease_until_utc = None
            subscription.lease_token = None
            session.add(
                ConditionObservation(
                    subscription_id=subscription.id,
                    observed_at_utc=now,
                    success=False,
                    state=None,
                    fingerprint=None,
                    error_code=safe_code,
                    retry_after_seconds=safe_retry_after,
                    latency_ms=latency_ms,
                    status_code=status_code,
                )
            )
        condition_metrics.failures += 1
        if safe_code == "timeout":
            condition_metrics.timeouts += 1
        if safe_code == "rate_limited":
            condition_metrics.rate_limited += 1
        return ConditionProcessResult(
            subscription_id=claim.id,
            outcome=ConditionPollOutcome.FAILURE,
            error_code=safe_code,
        )

    async def _record_success(
        self,
        claim: ConditionClaim,
        observation: ProviderObservation,
        *,
        latency_ms: int | None,
        now_utc: datetime | None = None,
    ) -> ConditionProcessResult:
        now = self._now(now_utc)
        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(ConditionSubscription)
                .where(
                    ConditionSubscription.id == claim.id,
                    ConditionSubscription.lease_token == claim.lease_token,
                )
                .with_for_update()
            )
            if subscription is None:
                return ConditionProcessResult(
                    subscription_id=claim.id,
                    outcome=ConditionPollOutcome.STALE,
                )

            previous_state = subscription.last_state
            transition_created = previous_state is None and claim.trigger_on_initial
            if previous_state is not None and previous_state != observation.state:
                transition_created = True
            transition_sequence: int | None = None
            transition: ConditionTransition | None = None
            if transition_created:
                transition_sequence = int(subscription.transition_sequence or 0) + 1
                transition = ConditionTransition(
                    subscription_id=subscription.id,
                    sequence=transition_sequence,
                    previous_state=previous_state,
                    current_state=observation.state,
                    fingerprint=observation.fingerprint,
                    observed_at_utc=now,
                )
                session.add(transition)
                await session.flush()
                session.add(
                    ConditionDelivery(
                        subscription_id=subscription.id,
                        transition_id=transition.id,
                        transition_sequence=transition_sequence,
                        chat_id=subscription.chat_id,
                        previous_state=previous_state,
                        current_state=observation.state,
                        message_text=render_condition_message(
                            subscription.message_template,
                            previous_state=previous_state,
                            current_state=observation.state,
                            provider_type=subscription.provider_type,
                        ),
                        state=ConditionDeliveryState.PENDING.value,
                        attempt_count=0,
                    )
                )
                subscription.transition_sequence = transition_sequence

            session.add(
                ConditionObservation(
                    subscription_id=subscription.id,
                    observed_at_utc=now,
                    success=True,
                    state=observation.state,
                    fingerprint=observation.fingerprint,
                    error_code=None,
                    retry_after_seconds=None,
                    latency_ms=latency_ms,
                    status_code=200,
                )
            )
            subscription.last_state = observation.state
            subscription.last_fingerprint = observation.fingerprint
            subscription.last_observed_at_utc = now
            subscription.last_error_code = None
            subscription.failure_count = 0
            subscription.next_retry_at_utc = None
            subscription.next_poll_at_utc = now + timedelta(seconds=claim.poll_interval_seconds)
            subscription.lease_until_utc = None
            subscription.lease_token = None

        condition_metrics.successes += 1
        if transition_created:
            condition_metrics.transitions += 1
        elif previous_state is not None and previous_state == observation.state:
            condition_metrics.deduplicated += 1
        return ConditionProcessResult(
            subscription_id=claim.id,
            outcome=ConditionPollOutcome.SUCCESS,
            state=observation.state,
            transition_sequence=transition_sequence,
            transition_created=transition_created,
            deduplicated=previous_state is not None and previous_state == observation.state,
        )

    async def process_claim(self, claim: ConditionClaim) -> ConditionProcessResult:
        started = time.monotonic()
        condition_metrics.polls += 1
        try:
            if not await self.renew_claim_lease(claim):
                return ConditionProcessResult(
                    subscription_id=claim.id,
                    outcome=ConditionPollOutcome.STALE,
                )
            provider = self.registry.get(claim.provider_type)
            config = parse_provider_config(
                claim.config_json,
                authorization_env_allowlist=getattr(
                    self.settings, "condition_authorization_env_allowlist", ()
                ),
            )
            async with asyncio.timeout(self.settings.condition_request_timeout_seconds):
                raw_observation = await provider.observe(claim.target, config=config)
            observation = _coerce_observation(raw_observation)
        except asyncio.CancelledError:
            raise
        except ConditionProviderError as exc:
            latency_ms = min(int((time.monotonic() - started) * 1000), 2_147_483_647)
            return await self._record_failure(
                claim,
                code=exc.code,
                retry_after_seconds=exc.retry_after_seconds,
                latency_ms=latency_ms,
                status_code=exc.status_code,
            )
        except TimeoutError:
            latency_ms = min(int((time.monotonic() - started) * 1000), 2_147_483_647)
            return await self._record_failure(
                claim,
                code="timeout",
                retry_after_seconds=None,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = min(int((time.monotonic() - started) * 1000), 2_147_483_647)
            logger.error(
                "Condition provider failed",
                extra={
                    "extra_data": (
                        f"subscription_id={claim.id} "
                        f"provider={claim.provider_type} "
                        f"error_type={type(exc).__name__[:80]}"
                    )
                },
            )
            return await self._record_failure(
                claim,
                code="provider_error",
                retry_after_seconds=None,
                latency_ms=latency_ms,
            )

        latency_ms = min(int((time.monotonic() - started) * 1000), 2_147_483_647)
        return await self._record_success(claim, observation, latency_ms=latency_ms)

    async def poll_due_conditions(
        self,
        *,
        limit: int | None = None,
        now_utc: datetime | None = None,
    ) -> ConditionCycleSummary:
        summary = ConditionCycleSummary()
        batch_size = self.settings.condition_poll_batch_size if limit is None else int(limit)
        batch_size = max(1, min(batch_size, 100))
        for _ in range(batch_size):
            claims = await self.claim_due_subscriptions(limit=1, now_utc=now_utc)
            if not claims:
                break
            claim = claims[0]
            summary.claimed += 1
            try:
                result = await self.process_claim(claim)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Condition subscription processing failed",
                    extra={
                        "extra_data": (
                            f"subscription_id={claim.id} error_type={type(exc).__name__[:80]}"
                        )
                    },
                )
                try:
                    result = await self._record_failure(
                        claim,
                        code="internal_error",
                        retry_after_seconds=None,
                        latency_ms=None,
                    )
                except Exception as recovery_exc:
                    logger.error(
                        "Condition failure state could not be persisted",
                        extra={
                            "extra_data": (
                                f"subscription_id={claim.id} "
                                f"error_type={type(recovery_exc).__name__[:80]}"
                            )
                        },
                    )
                    summary.failed += 1
                    continue
            if result.outcome == ConditionPollOutcome.SUCCESS:
                summary.succeeded += 1
            elif result.outcome == ConditionPollOutcome.FAILURE:
                summary.failed += 1
            else:
                summary.stale += 1
            if result.transition_created:
                summary.transitions += 1
            if result.deduplicated:
                summary.deduplicated += 1
        return summary

    async def cleanup_history(
        self,
        *,
        now_utc: datetime | None = None,
        retention_days: int | None = None,
    ) -> int:
        days = (
            self.settings.condition_history_retention_days
            if retention_days is None
            else int(retention_days)
        )
        if days < 1 or days > 3650:
            raise ValueError("condition history retention must be between 1 and 3650 days")
        cutoff = self._now(now_utc) - timedelta(days=days)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                delete(ConditionObservation).where(ConditionObservation.observed_at_utc < cutoff)
            )
            return int(getattr(result, "rowcount", 0) or 0)


async def create_condition_subscription(**kwargs: Any) -> ConditionSubscription:
    return await ConditionService().create_subscription(**kwargs)


async def claim_due_conditions(
    *,
    limit: int | None = None,
    now_utc: datetime | None = None,
) -> list[ConditionClaim]:
    return await ConditionService().claim_due_subscriptions(limit=limit, now_utc=now_utc)


async def poll_due_conditions(
    *,
    limit: int | None = None,
    now_utc: datetime | None = None,
) -> ConditionCycleSummary:
    return await ConditionService().poll_due_conditions(limit=limit, now_utc=now_utc)


async def cleanup_condition_history(
    *,
    now_utc: datetime | None = None,
    retention_days: int | None = None,
) -> int:
    return await ConditionService().cleanup_history(
        now_utc=now_utc,
        retention_days=retention_days,
    )
