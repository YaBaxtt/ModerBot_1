from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from time import monotonic
from typing import Any


@dataclass(frozen=True)
class SpamDecision:
    delete: bool = False
    punish: bool = False
    reason: str | None = None
    message_ids: tuple[int, ...] = ()


def message_signature(message: Any) -> str:
    """Return stable content identity for text, stickers, GIFs and other media."""
    raw_text = getattr(message, 'text', None)
    normalized_text = ' '.join(str(raw_text).casefold().split()) if raw_text else ''
    if normalized_text:
        return f'text:{normalized_text}'

    raw_caption = getattr(message, 'caption', None)
    normalized_caption = ' '.join(str(raw_caption).casefold().split()) if raw_caption else ''
    caption_suffix = f':caption:{normalized_caption}' if normalized_caption else ''

    for field in ('sticker', 'animation', 'video', 'video_note', 'voice', 'audio', 'document'):
        media = getattr(message, field, None)
        if media:
            identifier = getattr(media, 'file_unique_id', None) or getattr(media, 'file_id', None)
            return f'{field}:{identifier or "unknown"}{caption_suffix}'

    photos = getattr(message, 'photo', None)
    if photos:
        photo = photos[-1]
        identifier = getattr(photo, 'file_unique_id', None) or getattr(photo, 'file_id', None)
        return f'photo:{identifier or "unknown"}{caption_suffix}'

    if normalized_caption:
        return f'caption:{normalized_caption}'

    dice = getattr(message, 'dice', None)
    if dice:
        return f'dice:{getattr(dice, "emoji", "")}:{getattr(dice, "value", "")}'
    poll = getattr(message, 'poll', None)
    if poll:
        question = ' '.join(str(getattr(poll, 'question', '')).casefold().split())
        return f'poll:{question}'
    contact = getattr(message, 'contact', None)
    if contact:
        return f'contact:{getattr(contact, "user_id", None) or getattr(contact, "phone_number", "")}'
    location = getattr(message, 'location', None)
    if location:
        latitude = getattr(location, 'latitude', '')
        longitude = getattr(location, 'longitude', '')
        return f'location:{latitude}:{longitude}'

    content_type = getattr(message, 'content_type', None)
    return f'content:{content_type}' if content_type else 'content:unknown'


class AntiSpamGuard:
    """Process-local flood guard with separate speed and duplicate rules."""

    def __init__(
        self,
        *,
        burst_limit: int = 4,
        burst_window: float = 1.0,
        repeat_limit: int = 4,
        repeat_window: float = 5.0,
    ) -> None:
        self.burst_limit = burst_limit
        self.burst_window = burst_window
        self.repeat_limit = repeat_limit
        self.repeat_window = repeat_window
        self._bursts: dict[tuple[int, int], deque[tuple[float, int | None]]] = defaultdict(deque)
        self._repeats: dict[tuple[int, int], dict[str, deque[tuple[float, int | None]]]] = defaultdict(dict)
        self._last_cleanup: float | None = None

    @staticmethod
    def _trim(queue: deque[tuple[float, int | None]], current: float, window: float) -> None:
        while queue and current - queue[0][0] > window:
            queue.popleft()

    def inspect(
        self,
        *,
        chat_id: int,
        user_id: int,
        signature: str,
        message_id: int | None = None,
        now: float | None = None,
    ) -> SpamDecision:
        current = monotonic() if now is None else now
        self._cleanup_stale(current)
        key = (chat_id, user_id)

        burst = self._bursts[key]
        self._trim(burst, current, self.burst_window)
        burst.append((current, message_id))

        repeat_buckets = self._repeats[key]
        for saved_signature, queue in list(repeat_buckets.items()):
            self._trim(queue, current, self.repeat_window)
            if not queue:
                del repeat_buckets[saved_signature]
        repeated = repeat_buckets.setdefault(signature, deque())
        repeated.append((current, message_id))

        rapid_spam = len(burst) >= self.burst_limit
        repeated_spam = bool(signature) and len(repeated) >= self.repeat_limit
        if not rapid_spam and not repeated_spam:
            return SpamDecision()

        source = repeated if repeated_spam else burst
        ids = tuple(dict.fromkeys(saved_id for _, saved_id in source if saved_id is not None))
        reason = (
            f'{self.repeat_limit} одинаковых сообщения за {self.repeat_window:g} секунд'
            if repeated_spam
            else f'{self.burst_limit} сообщения за {self.burst_window:g} секунду'
        )
        # One burst must produce exactly one punishment. New messages start a
        # fresh window after the trigger.
        self._bursts.pop(key, None)
        self._repeats.pop(key, None)
        return SpamDecision(delete=True, punish=True, reason=reason, message_ids=ids)

    def _cleanup_stale(self, current: float) -> None:
        """Bound process memory for users who stop writing after one message."""
        cleanup_interval = max(self.burst_window, self.repeat_window, 60.0)
        if self._last_cleanup is not None and current - self._last_cleanup < cleanup_interval:
            return
        self._last_cleanup = current
        expiry = max(self.burst_window, self.repeat_window)
        keys = set(self._bursts) | set(self._repeats)
        for key in keys:
            latest = 0.0
            burst = self._bursts.get(key)
            if burst:
                latest = max(latest, burst[-1][0])
            for queue in self._repeats.get(key, {}).values():
                if queue:
                    latest = max(latest, queue[-1][0])
            if current - latest > expiry:
                self._bursts.pop(key, None)
                self._repeats.pop(key, None)

    def reset_chat(self, chat_id: int) -> None:
        """Forget partial bursts when protection is disabled for a group."""
        keys = {key for key in (*self._bursts.keys(), *self._repeats.keys()) if key[0] == chat_id}
        for key in keys:
            self._bursts.pop(key, None)
            self._repeats.pop(key, None)
