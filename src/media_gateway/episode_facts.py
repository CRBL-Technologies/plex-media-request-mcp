"""Strict facts derived from Sonarr episode records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SeasonEpisodeFacts:
    total: int
    aired_total: int
    aired_files: int
    all_have_files: bool
    has_future: bool
    has_unknown_air_date: bool
    finale_type: str | None


def season_episode_facts(
    episodes: list[dict[str, Any]], season_number: int, *, now: datetime
) -> SeasonEpisodeFacts | None:
    """Return trustworthy facts for one season, or ``None`` for invalid rows.

    Sonarr's season statistics can omit unmonitored missing episodes. Episode
    records are therefore the source of truth. Rows for other valid seasons
    are ignored because callers commonly fetch every episode in a series at
    once.
    """

    if (
        isinstance(season_number, bool)
        or not isinstance(season_number, int)
        or season_number < 0
        or now.tzinfo is None
    ):
        return None
    now = now.astimezone(UTC)
    selected: list[tuple[int, bool, datetime | None, str | None]] = []
    seen: set[int] = set()
    for episode in episodes:
        if not isinstance(episode, dict):
            return None
        row_season = episode.get("seasonNumber")
        if isinstance(row_season, bool) or not isinstance(row_season, int) or row_season < 0:
            return None
        if row_season != season_number:
            continue
        number = episode.get("episodeNumber")
        held = episode.get("hasFile")
        air_date = episode.get("airDateUtc")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or number in seen
            or not isinstance(held, bool)
        ):
            return None
        aired_at: datetime | None = None
        if air_date is not None:
            if not isinstance(air_date, str):
                return None
            try:
                aired_at = datetime.fromisoformat(air_date.replace("Z", "+00:00"))
            except ValueError:
                return None
            if aired_at.tzinfo is None:
                return None
            aired_at = aired_at.astimezone(UTC)
        finale = episode.get("finaleType")
        if finale is not None and not isinstance(finale, str):
            return None
        seen.add(number)
        selected.append((number, held, aired_at, finale or None))

    aired = [row for row in selected if row[1] or (row[2] is not None and row[2] <= now)]
    last = max(aired, default=None, key=lambda row: row[0])
    return SeasonEpisodeFacts(
        total=len(selected),
        aired_total=len(aired),
        aired_files=sum(row[1] for row in aired),
        all_have_files=bool(selected) and all(row[1] for row in selected),
        has_future=any(row[2] is not None and row[2] > now for row in selected),
        has_unknown_air_date=any(row[2] is None for row in selected),
        finale_type=last[3] if last is not None else None,
    )


def season_is_finished(
    facts: SeasonEpisodeFacts,
    *,
    season_number: int,
    series_status: object,
    has_later_season: bool,
) -> bool:
    """Whether Sonarr facts identify a completed ordinary season."""

    return (
        season_number > 0
        and facts.total > 0
        and not facts.has_future
        and (
            str(series_status or "").casefold() == "ended"
            or has_later_season
            or str(facts.finale_type or "").casefold() in {"season", "series"}
        )
    )
