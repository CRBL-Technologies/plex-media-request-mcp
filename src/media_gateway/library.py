"""Provider facts shared by searches, request status, and notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .upstream import Upstream, UpstreamError


def poster_url(item: dict[str, Any]) -> str | None:
    candidates: list[object] = [item.get("remotePoster")]
    images = item.get("images")
    if isinstance(images, list):
        candidates.extend(
            image.get("remoteUrl")
            for image in images
            if isinstance(image, dict) and image.get("coverType") == "poster"
        )
    for candidate in candidates:
        if not isinstance(candidate, str) or len(candidate) > 2048:
            continue
        parsed = urlsplit(candidate)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            return candidate
    return None


def _air_date(episode: dict[str, Any]) -> datetime | None:
    value = episode.get("airDateUtc")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


async def read_seasons(upstream: Upstream, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch actual episodes: Sonarr's summary count excludes unmonitored gaps."""

    series_id = record.get("id")
    if not isinstance(series_id, int) or isinstance(series_id, bool) or series_id <= 0:
        return season_states(record, None)
    raw = await upstream.call("sonarr_get_episodes", {"seriesId": series_id})
    episodes = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(episodes, list) or any(not isinstance(item, dict) for item in episodes):
        raise UpstreamError("Sonarr episode availability is unavailable")
    if isinstance(raw, dict) and raw.get("total", len(episodes)) != len(episodes):
        raise UpstreamError("Sonarr episode availability is incomplete")
    return season_states(record, episodes)


def season_states(
    record: dict[str, Any], episodes: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    seasons = record.get("seasons")
    if not isinstance(seasons, list):
        return []
    numbers = {
        season["seasonNumber"]
        for season in seasons
        if isinstance(season, dict)
        and isinstance(season.get("seasonNumber"), int)
        and not isinstance(season["seasonNumber"], bool)
        and season["seasonNumber"] >= 0
    }
    result: list[dict[str, Any]] = []
    for season in seasons:
        if not isinstance(season, dict) or season.get("seasonNumber") not in numbers:
            continue
        number = season["seasonNumber"]
        state: dict[str, Any] = {
            "number": number,
            "monitored": season.get("monitored") is True,
            "files": None,
            "episodes": None,
            "total_episodes": None,
            "complete": None,
            "partial": None,
            "finished": False,
            "status": "unknown",
        }
        result.append(state)
        if episodes is None:
            continue
        rows = [item for item in episodes if item.get("seasonNumber") == number]
        stats = season.get("statistics") or {}
        expected = stats.get("totalEpisodeCount") if isinstance(stats, dict) else None
        if isinstance(expected, int) and expected > len(rows):
            continue
        if any(not isinstance(item.get("hasFile"), bool) for item in rows):
            continue
        dated = [(item, _air_date(item)) for item in rows]
        # An undated missing episode may already have aired. Don't call it unaired.
        if any(date is None and not item["hasFile"] for item, date in dated):
            continue
        aired = [item for item, date in dated if item["hasFile"] or (date and date <= now)]
        future = sorted(date for _, date in dated if date is not None and date > now)
        files = sum(item["hasFile"] for item in rows)
        last = max(rows, key=lambda item: item.get("episodeNumber", 0), default={})
        finished = (
            number > 0
            and bool(rows)
            and not future
            and (
                str(last.get("finaleType") or "").casefold() in {"season", "series"}
                or record.get("status") == "ended"
                or any(other > number for other in numbers)
            )
        )
        state.update(
            files=files,
            episodes=len(aired),
            total_episodes=len(rows),
            complete=bool(aired) and all(item["hasFile"] for item in aired),
            partial=0 < files < len(aired),
            finished=finished,
            status="finished"
            if finished
            else "airing"
            if aired and future
            else "upcoming"
            if future
            else "unknown",
        )
        if future:
            state["next_airing"] = future[0].isoformat().replace("+00:00", "Z")
    return sorted(result, key=lambda state: int(state["number"]))


def apply_series_availability(candidate: dict[str, Any], states: list[dict[str, Any]]) -> None:
    candidate["season_states"] = states
    # Specials have their own availability; they don't make an ordinary show missing.
    ordinary = [state for state in states if state["number"] > 0]
    candidate["seasons_complete"] = [state["number"] for state in states if state["complete"]]
    candidate["seasons_missing"] = [
        state["number"] for state in states if state["complete"] is False and state["episodes"]
    ]
    candidate["downloaded"] = (
        None
        if not ordinary or any(state["complete"] is None for state in ordinary)
        else any(state["complete"] for state in ordinary)
        and all(state["complete"] or state["episodes"] == 0 for state in ordinary)
    )
