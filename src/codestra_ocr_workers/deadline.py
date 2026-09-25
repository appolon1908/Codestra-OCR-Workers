"""Monotonic request deadline shared by every stage of an extraction."""

from __future__ import annotations

import time

from .errors import DeadlineExceededError


class Deadline:
    def __init__(self, seconds: float) -> None:
        self._expires_at = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, stage: str) -> float:
        remaining = self.remaining()
        if remaining <= 0.0:
            raise DeadlineExceededError(f"deadline exceeded during {stage}")
        return remaining
