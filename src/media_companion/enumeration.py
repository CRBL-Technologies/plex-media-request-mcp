"""Versioned Sonarr/TMDB episode enumeration semantics.

The notification planner must know what a requested season means before it can
choose ``season_completion`` or ``airing_episode``.  This module turns bounded
provider records into a deterministic, raw-data-free snapshot and computes
safe changes between snapshots.  Unknown/TBA/postponed episodes stay
unresolved; only authoritative cancellation/removal excludes an episode from
the required set.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from .models import RequestMode

MAX_EPISODES_PER_SEASON = 500
MAX_PROVIDER_ID_LENGTH = 64
_POSITIVE_ID = re.compile(r"[1-9][0-9]*\Z")
_SYNTHETIC_EPISODE_ID = re.compile(r"s[0-9]{2,3}e[0-9]{2,4}\Z", re.IGNORECASE)
_CANCELED_STATES = frozenset(
    {
        "canceled",
        "cancelled",
        "removed",
        "deleted",
        "not aired",
        "not_aired",
    }
)
_FUTURE_STATES = frozenset(
    {
        "unaired",
        "upcoming",
        "announced",
        "tba",
        "postponed",
        "delayed",
        "unknown",
    }
)
_ENDED_STATES = frozenset({"ended", "complete", "completed", "final"})
_CONTINUING_STATES = frozenset(
    {
        "continuing",
        "continuing_series",
        "airing",
        "releasing",
        "ongoing",
        "active",
        "returning series",
    }
)


class EnumerationError(ValueError):
    """The provider snapshot cannot be interpreted safely."""


class EnumerationConflictError(EnumerationError):
    """Records conflict by stable ID or season/episode number."""


class EpisodeState(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    FUTURE = "future"
    TBA = "tba"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    REMOVED = "removed"
    UNKNOWN = "unknown"

    @property
    def terminal_exclusion(self) -> bool:
        return self in {EpisodeState.CANCELED, EpisodeState.REMOVED}

    @property
    def unresolved(self) -> bool:
        return self in {
            EpisodeState.FUTURE,
            EpisodeState.TBA,
            EpisodeState.POSTPONED,
            EpisodeState.UNKNOWN,
        }


EpisodeStatus = EpisodeState


def validate_season_scope(
    season_number: object,
    *,
    explicitly_requested: bool = False,
    explicit_seasons: Iterable[object] | None = None,
) -> int:
    """Validate a season number; specials (season 0) require explicit scope."""

    if (
        isinstance(season_number, bool)
        or not isinstance(season_number, int)
        or season_number < 0
    ):
        raise EnumerationError("season_number_invalid")
    explicit = explicitly_requested or (
        explicit_seasons is not None
        and any(
            not isinstance(value, bool)
            and isinstance(value, int)
            and value == season_number
            for value in explicit_seasons
        )
    )
    if season_number == 0 and not explicit:
        raise EnumerationError("specials_require_explicit_scope")
    return season_number


validate_season = validate_season_scope


def _text(value: object, field_name: str, *, maximum: int = 512) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EnumerationError(f"{field_name}_invalid")
    cleaned = "".join(
        character for character in value if 0x20 <= ord(character) != 0x7F
    ).strip()
    if not cleaned:
        return None
    if len(cleaned) > maximum:
        raise EnumerationError(f"{field_name}_too_long")
    return cleaned


def _provider_id(value: object, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise EnumerationError("episode_id_missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise EnumerationError("episode_id_invalid")
    text = str(value).strip()
    if len(text) > MAX_PROVIDER_ID_LENGTH or not (
        _POSITIVE_ID.fullmatch(text) or _SYNTHETIC_EPISODE_ID.fullmatch(text)
    ):
        raise EnumerationError("episode_id_invalid")
    return text


def _number(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EnumerationError(f"{field_name}_invalid")
    return value


def _parse_air_date(value: object, field_name: str = "air_date") -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise EnumerationError(f"{field_name}_invalid")
    text = value.strip()
    if not text:
        return None
    # Providers use both ``2026-08-15`` and ISO UTC timestamps.  Date-only
    # values are interpreted at midnight UTC for deterministic comparisons.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as exc:
            raise EnumerationError(f"{field_name}_invalid") from exc
        parsed = datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=timezone.utc,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status(value: object) -> str | None:
    text = _text(value, "status", maximum=64)
    return text.casefold() if text else None


def _has_file(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise EnumerationError("has_file_invalid")


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """One normalized episode unit; no provider object or path is retained."""

    provider_id: str
    season_number: int
    episode_number: int
    title: str | None = None
    air_date: datetime | None = None
    state: EpisodeState = EpisodeState.UNKNOWN
    has_file: bool | None = None
    cancellation_reason: str | None = None
    authoritative: bool = False
    monitored: bool | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        normalized_id = _provider_id(self.provider_id, required=True)
        if normalized_id is None:
            raise EnumerationError("episode_id_missing")
        object.__setattr__(self, "provider_id", normalized_id)
        object.__setattr__(
            self, "season_number", _number(self.season_number, "season_number")
        )
        object.__setattr__(
            self, "episode_number", _number(self.episode_number, "episode_number")
        )
        object.__setattr__(self, "title", _text(self.title, "title", maximum=1024))
        object.__setattr__(self, "air_date", _parse_air_date(self.air_date))
        if not isinstance(self.state, EpisodeState):
            try:
                object.__setattr__(self, "state", EpisodeState(str(self.state)))
            except ValueError as exc:
                raise EnumerationError("episode_state_invalid") from exc
        if self.has_file is not None and not isinstance(self.has_file, bool):
            raise EnumerationError("has_file_invalid")
        object.__setattr__(
            self,
            "cancellation_reason",
            _text(self.cancellation_reason, "cancellation_reason", maximum=256),
        )
        if not isinstance(self.authoritative, bool):
            raise EnumerationError("authoritative_invalid")
        if self.monitored is not None and not isinstance(self.monitored, bool):
            raise EnumerationError("monitored_invalid")
        if self.fingerprint is not None:
            fingerprint = _text(self.fingerprint, "fingerprint", maximum=128)
            object.__setattr__(self, "fingerprint", fingerprint)

    @property
    def logical_key(self) -> str:
        return f"s{self.season_number:02d}e{self.episode_number:02d}"

    @property
    def unit_key(self) -> str:
        return f"episode:{self.provider_id}"

    @property
    def required(self) -> bool:
        return not self.state.terminal_exclusion

    @property
    def currently_available(self) -> bool:
        return self.state is EpisodeState.AVAILABLE

    @property
    def currently_missing(self) -> bool:
        # Future/TBA/unknown is not counted as currently missing; it remains
        # unresolved and keeps an airing subscription open.
        return self.state is EpisodeState.MISSING

    @property
    def future(self) -> bool:
        return self.state in {
            EpisodeState.FUTURE,
            EpisodeState.TBA,
            EpisodeState.POSTPONED,
        }

    @property
    def canceled(self) -> bool:
        return self.state in {EpisodeState.CANCELED, EpisodeState.REMOVED}

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "logical_key": self.logical_key,
            "title": self.title,
            "air_date": _iso(self.air_date),
            "state": self.state.value,
            "has_file": self.has_file,
            "cancellation_reason": self.cancellation_reason,
            "authoritative": self.authoritative,
            "monitored": self.monitored,
            "fingerprint": self.fingerprint,
        }


Episode = EpisodeRecord
NormalizedEpisode = EpisodeRecord


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _infer_state(
    raw: Mapping[str, Any],
    *,
    air_date: datetime | None,
    has_file: bool | None,
    now: datetime,
) -> tuple[EpisodeState, str | None, bool]:
    status = _status(raw.get("status", raw.get("episodeStatus")))
    canceled_flag = raw.get("isCanceled", raw.get("isCancelled", raw.get("cancelled")))
    removed_flag = raw.get("isRemoved", raw.get("removed"))
    cancellation_reason = _text(
        raw.get("cancellationReason", raw.get("removedReason")),
        "cancellation_reason",
        maximum=256,
    )
    authoritative_cancel = bool(
        canceled_flag is True or removed_flag is True or status in _CANCELED_STATES
    )
    if removed_flag is True or status in {"removed", "deleted"}:
        return EpisodeState.REMOVED, cancellation_reason or status, authoritative_cancel
    if authoritative_cancel:
        return EpisodeState.CANCELED, cancellation_reason or status, True
    if status in {"tba", "to be announced"}:
        return EpisodeState.TBA, None, False
    if status in {"postponed", "delayed"}:
        return EpisodeState.POSTPONED, None, False
    if has_file is True:
        return EpisodeState.AVAILABLE, None, True
    if status in _FUTURE_STATES:
        return EpisodeState.FUTURE, None, False
    if air_date is not None and air_date > now:
        return EpisodeState.FUTURE, None, False
    if air_date is None:
        return EpisodeState.UNKNOWN, None, False
    # A past/airing episode with no file is known to be missing.  This is a
    # currently missing unit, unlike an unaired or TBA episode.
    return EpisodeState.MISSING, None, True


def _record_from_provider(
    raw: Mapping[str, Any],
    *,
    requested_season: int,
    now: datetime,
) -> EpisodeRecord:
    if not isinstance(raw, Mapping):
        raise EnumerationError("episode_record_invalid")
    raw_season = raw.get("seasonNumber", raw.get("season_number", raw.get("season")))
    raw_episode = raw.get(
        "episodeNumber", raw.get("episode_number", raw.get("episode"))
    )
    season = _number(raw_season, "season_number")
    episode = _number(raw_episode, "episode_number")
    if season != requested_season:
        raise EnumerationError("episode_season_mismatch")
    provider_id = _provider_id(
        raw.get("id", raw.get("episodeId", raw.get("provider_id")))
    )
    if provider_id is None:
        # A provider ID is normally present.  A deterministic synthetic key is
        # retained only for fixture/provider responses that omit it and is
        # marked unresolved by the enumeration's diagnostics.
        provider_id = f"s{season:02d}e{episode:02d}"
    air_date = _parse_air_date(
        raw.get(
            "airDateUtc",
            raw.get("air_date_utc", raw.get("airDate", raw.get("air_date"))),
        )
    )
    has_file = _has_file(raw.get("hasFile", raw.get("has_file")))
    state, cancellation_reason, authoritative = _infer_state(
        raw,
        air_date=air_date,
        has_file=has_file,
        now=now,
    )
    monitored = raw.get("monitored")
    if monitored is not None and not isinstance(monitored, bool):
        raise EnumerationError("monitored_invalid")
    title = _text(raw.get("title"), "title", maximum=1024)
    # Fingerprint only safe identity/state fields.  Do not hash or preserve a
    # provider object wholesale because that makes future payload expansion a
    # persistence boundary.
    fingerprint_input = json.dumps(
        {
            "id": provider_id,
            "season": season,
            "episode": episode,
            "air_date": _iso(air_date),
            "state": state.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return EpisodeRecord(
        provider_id=provider_id,
        season_number=season,
        episode_number=episode,
        title=title,
        air_date=air_date,
        state=state,
        has_file=has_file,
        cancellation_reason=cancellation_reason,
        authoritative=authoritative,
        monitored=monitored,
        fingerprint=hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class EpisodeEnumeration:
    """One versioned, sanitized snapshot for a requested season."""

    provider: str
    provider_id: str
    season_number: int
    version: int
    episodes: tuple[EpisodeRecord, ...] = ()
    expected_count: int | None = None
    authoritative: bool = False
    season_ended: bool = False
    requested_explicitly: bool = False
    source: str = "sonarr"
    diagnostics: tuple[str, ...] = ()
    snapshot_hash: str = ""
    mode: RequestMode = RequestMode.AIRING_EPISODE

    def __post_init__(self) -> None:
        provider = _text(self.provider, "provider", maximum=64)
        provider_id = _provider_id(self.provider_id, required=True)
        if provider is None or provider_id is None:
            raise EnumerationError("provider_identity_missing")
        object.__setattr__(self, "provider", provider.casefold())
        object.__setattr__(self, "provider_id", provider_id)
        validate_season_scope(
            self.season_number,
            explicitly_requested=self.requested_explicitly,
        )
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise EnumerationError("enumeration_version_invalid")
        episodes = tuple(
            sorted(
                self.episodes,
                key=lambda item: (
                    item.season_number,
                    item.episode_number,
                    item.provider_id,
                ),
            )
        )
        if len(episodes) > MAX_EPISODES_PER_SEASON:
            raise EnumerationError("too_many_episodes")
        if any(not isinstance(item, EpisodeRecord) for item in episodes):
            raise EnumerationError("episode_record_invalid")
        object.__setattr__(self, "episodes", episodes)
        if self.expected_count is not None and (
            isinstance(self.expected_count, bool)
            or not isinstance(self.expected_count, int)
            or self.expected_count < 0
        ):
            raise EnumerationError("expected_count_invalid")
        if (
            not isinstance(self.authoritative, bool)
            or not isinstance(self.season_ended, bool)
            or not isinstance(self.requested_explicitly, bool)
        ):
            raise EnumerationError("enumeration_flag_invalid")
        object.__setattr__(
            self, "diagnostics", tuple(sorted({str(item) for item in self.diagnostics}))
        )
        if self.snapshot_hash:
            if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_hash):
                raise EnumerationError("snapshot_hash_invalid")
        else:
            object.__setattr__(self, "snapshot_hash", _snapshot_hash(self.episodes))
        if not isinstance(self.mode, RequestMode):
            try:
                object.__setattr__(self, "mode", RequestMode(str(self.mode)))
            except ValueError as exc:
                raise EnumerationError("request_mode_invalid") from exc
        if self.mode is RequestMode.MOVIE:
            raise EnumerationError("episode_mode_invalid")

    @property
    def complete(self) -> bool:
        return self.authoritative and not self.diagnostics and self._count_matches()

    @property
    def expected_known(self) -> bool:
        return self.expected_count is not None

    @property
    def required(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode for episode in self.episodes if episode.required)

    @property
    def available(self) -> tuple[EpisodeRecord, ...]:
        return tuple(
            episode for episode in self.required if episode.currently_available
        )

    @property
    def missing(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode for episode in self.required if episode.currently_missing)

    @property
    def future(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode for episode in self.required if episode.future)

    @property
    def unresolved(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode for episode in self.required if episode.state.unresolved)

    @property
    def canceled(self) -> tuple[EpisodeRecord, ...]:
        return tuple(episode for episode in self.episodes if episode.canceled)

    @property
    def fully_available(self) -> bool:
        return (
            self.complete
            and not self.unresolved
            and all(episode.currently_available for episode in self.required)
        )

    @property
    def has_future(self) -> bool:
        return bool(self.future)

    def _count_matches(self) -> bool:
        if self.expected_count is None:
            return bool(self.episodes) and self.season_ended
        # Canceled/removed records remain in the snapshot as explicit history,
        # so they still count toward the provider's expected enumeration.  A
        # missing row without such evidence must never make completion appear
        # true.
        return len(self.episodes) == self.expected_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_id": self.provider_id,
            "season_number": self.season_number,
            "version": self.version,
            "episodes": [episode.as_dict() for episode in self.episodes],
            "expected_count": self.expected_count,
            "authoritative": self.authoritative,
            "season_ended": self.season_ended,
            "requested_explicitly": self.requested_explicitly,
            "source": self.source,
            "diagnostics": list(self.diagnostics),
            "snapshot_hash": self.snapshot_hash,
            "mode": self.mode.value,
        }

    def sanitized_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


EnumerationSnapshot = EpisodeEnumeration
SeasonEnumeration = EpisodeEnumeration


def _snapshot_hash(episodes: Iterable[EpisodeRecord]) -> str:
    payload = [episode.as_dict() for episode in episodes]
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def choose_request_mode(enumeration: EpisodeEnumeration) -> RequestMode:
    """Choose mode from provider truth, not webhook burst size."""

    if enumeration.has_future or enumeration.unresolved or not enumeration.season_ended:
        return RequestMode.AIRING_EPISODE
    if enumeration.complete:
        return RequestMode.SEASON_COMPLETION
    return RequestMode.AIRING_EPISODE


select_request_mode = choose_request_mode
classify_season_mode = choose_request_mode


def _coerce_season_end(
    *,
    season_ended: bool | None,
    season_status: object,
    series_status: object,
) -> bool:
    if season_ended is not None:
        if not isinstance(season_ended, bool):
            raise EnumerationError("season_ended_invalid")
        return season_ended
    for value in (season_status, series_status):
        status = _status(value)
        if status in _ENDED_STATES:
            return True
        if status in _CONTINUING_STATES:
            return False
    return False


def enumerate_episodes(
    records: Iterable[Mapping[str, Any]],
    season_number: int,
    *,
    provider: str = "sonarr",
    provider_id: object = "0",
    requested_explicitly: bool = False,
    explicit_seasons: Iterable[object] | None = None,
    expected_count: int | None = None,
    authoritative: bool = False,
    season_ended: bool | None = None,
    season_status: object = None,
    series_status: object = None,
    source: str = "sonarr",
    version: int = 1,
    now: datetime | None = None,
) -> EpisodeEnumeration:
    """Normalize one provider episode list for one explicitly scoped season."""

    explicit_values = tuple(explicit_seasons or ())
    season = validate_season_scope(
        season_number,
        explicitly_requested=requested_explicitly,
        explicit_seasons=explicit_values,
    )
    normalized_provider_id = _provider_id(provider_id, required=True)
    if normalized_provider_id is None:
        raise EnumerationError("provider_identity_missing")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    if expected_count is not None and (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        raise EnumerationError("expected_count_invalid")
    diagnostics: set[str] = set()
    normalized: list[EpisodeRecord] = []
    by_id: dict[str, EpisodeRecord] = {}
    by_number: dict[tuple[int, int], EpisodeRecord] = {}
    records_tuple = tuple(records)
    if len(records_tuple) > MAX_EPISODES_PER_SEASON * 2:
        raise EnumerationError("too_many_provider_records")
    for raw in records_tuple:
        if not isinstance(raw, Mapping):
            diagnostics.add("episode_record_invalid")
            continue
        raw_season = raw.get(
            "seasonNumber", raw.get("season_number", raw.get("season"))
        )
        if raw_season != season:
            # A provider endpoint occasionally returns all seasons.  Ignore
            # unrelated seasons, but malformed season values are diagnostic.
            if isinstance(raw_season, int) and not isinstance(raw_season, bool):
                continue
            diagnostics.add("episode_season_invalid")
            continue
        if raw.get("id", raw.get("episodeId", raw.get("provider_id"))) is None:
            diagnostics.add("episode_id_missing")
        try:
            episode = _record_from_provider(raw, requested_season=season, now=current)
        except EnumerationError as exc:
            diagnostics.add(exc.args[0] if exc.args else "episode_record_invalid")
            continue
        prior_id = by_id.get(episode.provider_id)
        prior_number = by_number.get((episode.season_number, episode.episode_number))
        if prior_id is not None:
            if (
                prior_id.logical_key != episode.logical_key
                or prior_id.state != episode.state
            ):
                diagnostics.add("conflicting_episode_id")
            continue
        if prior_number is not None and prior_number.provider_id != episode.provider_id:
            diagnostics.add("conflicting_episode_number")
            continue
        by_id[episode.provider_id] = episode
        by_number[(episode.season_number, episode.episode_number)] = episode
        normalized.append(episode)
    if not normalized:
        diagnostics.add("episode_enumeration_empty")
    end_state = _coerce_season_end(
        season_ended=season_ended,
        season_status=season_status,
        series_status=series_status,
    )
    if any(
        episode.state
        in {EpisodeState.TBA, EpisodeState.POSTPONED, EpisodeState.UNKNOWN}
        for episode in normalized
    ):
        diagnostics.add("episode_state_unresolved")
    if expected_count is not None and len(normalized) < expected_count:
        diagnostics.add("expected_episode_count_incomplete")
    if expected_count is not None and len(normalized) > expected_count:
        diagnostics.add("expected_episode_count_conflict")
    if expected_count is None:
        diagnostics.add("expected_episode_count_unknown")
    if not authoritative:
        diagnostics.add("enumeration_not_authoritative")
    provisional = EpisodeEnumeration(
        provider=provider,
        provider_id=normalized_provider_id,
        season_number=season,
        version=version,
        episodes=tuple(normalized),
        expected_count=expected_count,
        authoritative=authoritative,
        season_ended=end_state,
        requested_explicitly=requested_explicitly or season in set(explicit_values),
        source=source,
        diagnostics=tuple(diagnostics),
        mode=RequestMode.AIRING_EPISODE,
    )
    return EpisodeEnumeration(
        provider=provisional.provider,
        provider_id=provisional.provider_id,
        season_number=provisional.season_number,
        version=provisional.version,
        episodes=provisional.episodes,
        expected_count=provisional.expected_count,
        authoritative=provisional.authoritative,
        season_ended=provisional.season_ended,
        requested_explicitly=provisional.requested_explicitly,
        source=provisional.source,
        diagnostics=provisional.diagnostics,
        snapshot_hash=provisional.snapshot_hash,
        mode=choose_request_mode(provisional),
    )


enumerate_season = enumerate_episodes
build_episode_enumeration = enumerate_episodes
build_enumeration = enumerate_episodes


@dataclass(frozen=True, slots=True)
class EnumerationDelta:
    previous_version: int
    current_version: int
    added: tuple[EpisodeRecord, ...] = ()
    removed: tuple[EpisodeRecord, ...] = ()
    canceled: tuple[EpisodeRecord, ...] = ()
    changed: tuple[EpisodeRecord, ...] = ()
    renumbered: tuple[EpisodeRecord, ...] = ()
    conflicts: tuple[str, ...] = ()
    late: tuple[EpisodeRecord, ...] = ()
    new_generation: bool = False

    @property
    def quarantined(self) -> bool:
        return bool(self.conflicts)

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.added,
                self.removed,
                self.canceled,
                self.changed,
                self.renumbered,
                self.late,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "added": [item.as_dict() for item in self.added],
            "removed": [item.as_dict() for item in self.removed],
            "canceled": [item.as_dict() for item in self.canceled],
            "changed": [item.as_dict() for item in self.changed],
            "renumbered": [item.as_dict() for item in self.renumbered],
            "conflicts": list(self.conflicts),
            "late": [item.as_dict() for item in self.late],
            "new_generation": self.new_generation,
        }


def diff_enumerations(
    previous: EpisodeEnumeration,
    current: EpisodeEnumeration,
    *,
    completion_sent: bool = False,
) -> EnumerationDelta:
    """Compute authoritative changes without deleting episode history."""

    if (
        previous.provider != current.provider
        or previous.provider_id != current.provider_id
        or previous.season_number != current.season_number
    ):
        raise EnumerationError("enumeration_scope_mismatch")
    old_by_id = {episode.provider_id: episode for episode in previous.episodes}
    new_by_id = {episode.provider_id: episode for episode in current.episodes}
    added: list[EpisodeRecord] = []
    removed: list[EpisodeRecord] = []
    canceled: list[EpisodeRecord] = []
    changed: list[EpisodeRecord] = []
    renumbered: list[EpisodeRecord] = []
    late: list[EpisodeRecord] = []
    conflicts: set[str] = set(previous.diagnostics) | set(current.diagnostics)
    for provider_id, episode in sorted(new_by_id.items()):
        old = old_by_id.get(provider_id)
        if old is None:
            if episode.canceled:
                canceled.append(episode)
            else:
                added.append(episode)
                if completion_sent:
                    late.append(episode)
            continue
        if old.logical_key != episode.logical_key:
            renumbered.append(episode)
            conflicts.add("episode_renumbered")
            continue
        if old.state != episode.state or old.has_file != episode.has_file:
            changed.append(episode)
            if episode.canceled and not old.canceled:
                canceled.append(episode)
    # Absence alone is not authoritative removal.  A record is removed only
    # when the current snapshot explicitly marks it canceled/removed; preserve
    # old units until such evidence arrives.
    for provider_id, old in sorted(old_by_id.items()):
        if provider_id not in new_by_id and current.authoritative is False:
            conflicts.add("episode_missing_without_authoritative_removal")
        elif provider_id not in new_by_id:
            conflicts.add("episode_missing_without_removal_reason")
    return EnumerationDelta(
        previous.version,
        current.version,
        tuple(added),
        tuple(removed),
        tuple(canceled),
        tuple(changed),
        tuple(renumbered),
        tuple(sorted(conflicts)),
        tuple(late),
        bool(late),
    )


enumeration_diff = diff_enumerations
diff_episode_enumerations = diff_enumerations


class EnumerationStore:
    """In-memory versioning primitive for a later durable repository."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str, int], EpisodeEnumeration] = {}

    def latest(
        self, provider: str, provider_id: object, season_number: int
    ) -> EpisodeEnumeration | None:
        key = (str(provider).casefold(), str(provider_id), season_number)
        return self._snapshots.get(key)

    def put(
        self, snapshot: EpisodeEnumeration
    ) -> tuple[EpisodeEnumeration | None, EnumerationDelta | None]:
        key = (snapshot.provider, snapshot.provider_id, snapshot.season_number)
        previous = self._snapshots.get(key)
        if previous is not None and previous.snapshot_hash == snapshot.snapshot_hash:
            return previous, None
        if previous is not None and snapshot.version <= previous.version:
            snapshot = EpisodeEnumeration(
                provider=snapshot.provider,
                provider_id=snapshot.provider_id,
                season_number=snapshot.season_number,
                version=previous.version + 1,
                episodes=snapshot.episodes,
                expected_count=snapshot.expected_count,
                authoritative=snapshot.authoritative,
                season_ended=snapshot.season_ended,
                requested_explicitly=snapshot.requested_explicitly,
                source=snapshot.source,
                diagnostics=snapshot.diagnostics,
                mode=snapshot.mode,
            )
        delta = None if previous is None else diff_enumerations(previous, snapshot)
        self._snapshots[key] = snapshot
        return snapshot, delta

    def snapshots(self) -> tuple[EpisodeEnumeration, ...]:
        return tuple(self._snapshots[key] for key in sorted(self._snapshots))


VersionedEnumerationStore = EnumerationStore


__all__ = [
    "MAX_EPISODES_PER_SEASON",
    "EnumerationConflictError",
    "EnumerationDelta",
    "EnumerationError",
    "EnumerationSnapshot",
    "EnumerationStore",
    "Episode",
    "EpisodeRecord",
    "EpisodeState",
    "EpisodeStatus",
    "NormalizedEpisode",
    "SeasonEnumeration",
    "VersionedEnumerationStore",
    "build_enumeration",
    "build_episode_enumeration",
    "choose_request_mode",
    "classify_season_mode",
    "diff_enumerations",
    "diff_episode_enumerations",
    "enumerate_episodes",
    "enumerate_season",
    "enumeration_diff",
    "select_request_mode",
    "validate_season",
    "validate_season_scope",
]
