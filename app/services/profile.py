from __future__ import annotations

LEVELS = ((2000, "👑 Легенда чата"), (500, "🏅 Старожил"), (100, "⚡ Активный"), (0, "🌱 Новичок"))


def level_for_points(points: int) -> str:
    return next(label for threshold, label in LEVELS if points >= threshold)


def level_progress(points: int) -> tuple[str, str]:
    """Return a compact progress bar and the next-level explanation."""
    ascending = tuple(reversed(LEVELS))
    current_index = max(index for index, (threshold, _) in enumerate(ascending) if points >= threshold)
    if current_index == len(ascending) - 1:
        return '██████████', 'Максимальный уровень достигнут'
    current_threshold = ascending[current_index][0]
    next_threshold, next_title = ascending[current_index + 1]
    ratio = (points - current_threshold) / (next_threshold - current_threshold)
    filled = max(0, min(10, int(ratio * 10)))
    return '█' * filled + '░' * (10 - filled), f'До {next_title}: {next_threshold - points} ⭐'
