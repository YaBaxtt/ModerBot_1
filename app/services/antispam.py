from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class SpamDecision:
    delete: bool = False
    mute: bool = False


class AntiSpamGuard:
    """Process-local flood guard; deliberately keeps no Redis dependency."""

    def __init__(self, *, min_interval: float = 1.0, repeat_limit: int = 5, repeat_window: float = 120.0) -> None:
        self.min_interval = min_interval
        self.repeat_limit = repeat_limit
        self.repeat_window = repeat_window
        self._last_message: dict[tuple[int, int], float] = {}
        self._repeats: dict[tuple[int, int], deque[tuple[str, float]]] = defaultdict(deque)

    def inspect(self, *, chat_id: int, user_id: int, text: str, now: float | None = None) -> SpamDecision:
        current = monotonic() if now is None else now
        key = (chat_id, user_id)
        last_time = self._last_message.get(key)
        self._last_message[key] = current
        too_fast = last_time is not None and current - last_time < self.min_interval

        normalized = " ".join(text.casefold().split())
        repeats = self._repeats[key]
        if repeats and repeats[-1][0] != normalized:
            repeats.clear()
        repeats.append((normalized, current))
        while repeats and current - repeats[0][1] > self.repeat_window:
            repeats.popleft()
        repeated_spam = bool(normalized) and len(repeats) >= self.repeat_limit
        if repeated_spam:
            repeats.clear()  # the same burst cannot trigger another mute
        return SpamDecision(delete=too_fast or repeated_spam, mute=repeated_spam)
