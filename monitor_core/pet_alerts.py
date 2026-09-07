"""Low-satiety reminders driven by new verified observations of the user's pet.

The caller supplies monitoring validity and whether an observation belongs to
live input. This controller never guesses freshness from elapsed time, consumes
feeding replies, plays audio, or obtains observations of other players' pets.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Protocol


REPEAT_INTERVAL_SECONDS = 60.0
CRITICAL_SATIETY_THRESHOLD = 10
CRITICAL_REPEAT_INTERVAL_SECONDS = 20.0


class PetObservation(Protocol):
    pet_id: int | None
    pet_name: str
    satiety: int | None
    identity_generation: int
    satiety_revision: int


@dataclass(frozen=True)
class PetAlertEvent:
    pet_id: int
    pet_name: str
    satiety: int
    threshold: int
    identity_generation: int
    satiety_revision: int


class PetAlertController:
    """Use new observations for normal/critical reminders, never a blind timer.

    Evaluate every observation returned by PetTracker.consume in order. Historical,
    disabled and invalid samples are consumed without being replayed when the
    context later changes. A subsequent fresh low sample can produce the first
    reminder; toggling a setting alone never replays an already-seen sample.
    A verified recovery ends the episode. There is deliberately no timer/poll
    API: the same old observation cannot cause a repeat just because time passed.
    Entering 0..10 on a valid new sample alerts immediately when the user's limit
    allows it. Further critical samples wait 20 seconds; other low samples wait
    60 seconds. Disabled reminders still track valid zone changes without replay.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation: int | None = None
        self._pet_id: int | None = None
        self._last_revision = 0
        self._last_alert_at: float | None = None
        self._last_now: float | None = None
        self._was_critical: bool | None = None

    def reset(self) -> None:
        """Use when replacing the tracker or explicitly changing recording/self."""
        with self._lock:
            self._generation = None
            self._pet_id = None
            self._last_revision = 0
            self._last_alert_at = None
            self._last_now = None
            self._was_critical = None

    def evaluate(
        self,
        state: PetObservation,
        *,
        enabled: bool,
        live: bool,
        valid: bool,
        threshold: int = 25,
        monitoring_enabled: bool = True,
        now: float | None = None,
    ) -> PetAlertEvent | None:
        with self._lock:
            generation = state.identity_generation
            revision = state.satiety_revision
            if not isinstance(generation, int) or not isinstance(revision, int):
                return None
            if self._generation is not None and generation < self._generation:
                return None
            if generation != self._generation or state.pet_id != self._pet_id:
                self._generation = generation
                self._pet_id = state.pet_id
                self._last_revision = 0
                self._last_alert_at = None
                self._was_critical = None
            new_observation = revision > self._last_revision
            if new_observation:
                self._last_revision = revision
            try:
                observed_at = time.monotonic() if now is None else float(now)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(observed_at) or (self._last_now is not None and observed_at < self._last_now):
                return None
            self._last_now = observed_at
            if not new_observation:
                return None
            value = state.satiety
            if (not live or not valid or not monitoring_enabled
                    or not isinstance(state.pet_id, int) or isinstance(state.pet_id, bool) or state.pet_id <= 0
                    or not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100
                    or not isinstance(threshold, int) or isinstance(threshold, bool) or not 0 <= threshold <= 100):
                return None
            critical = value <= CRITICAL_SATIETY_THRESHOLD
            entering_critical = critical and self._was_critical is False
            self._was_critical = critical
            if value > threshold:
                self._last_alert_at = None
                return None
            if not enabled:
                return None
            interval = CRITICAL_REPEAT_INTERVAL_SECONDS if critical else REPEAT_INTERVAL_SECONDS
            if (not entering_critical and self._last_alert_at is not None
                    and observed_at - self._last_alert_at < interval):
                return None
            self._last_alert_at = observed_at
            return PetAlertEvent(
                pet_id=state.pet_id,
                pet_name=state.pet_name,
                satiety=value,
                threshold=threshold,
                identity_generation=generation,
                satiety_revision=revision,
            )
