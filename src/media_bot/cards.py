"""How search results reach the chat: one poster card at most, the rest in words."""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

CAPTION_HEADING_LIMIT = 200
CAPTION_OVERVIEW_LIMIT = 420
CARD_TOOLS = frozenset({"search_media", "recommend_media"})


def _nameable(candidate: object) -> bool:
    """A row the model can name and request: a title, a media type and its provider ID."""

    if not isinstance(candidate, Mapping):
        return False
    title = candidate.get("title")
    media_type = candidate.get("media_type")
    if not isinstance(title, str) or not title.strip() or media_type not in {"movie", "series"}:
        return False
    external_id = candidate.get("tmdb_id" if media_type == "movie" else "tvdb_id")
    return isinstance(external_id, int) and not isinstance(external_id, bool) and external_id > 0


def _clip(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: max(1, maximum - 1)].rstrip() + "…"


def caption(candidate: Mapping[str, Any]) -> str:
    title = " ".join(str(candidate.get("title") or "Unknown title").split())
    year = candidate.get("year")
    suffix = f" ({year})" if isinstance(year, int) and not isinstance(year, bool) else ""
    kind = "Movie" if candidate.get("media_type") == "movie" else "Series"
    overview = " ".join(str(candidate.get("overview") or "").split())
    # Escape first, then clip. Clipping raw text lets entity expansion
    # ("&" -> "&amp;") push the caption past Telegram's 1024-character limit.
    heading = (
        f"<b>{_clip(html.escape(_clip(title, 160)), CAPTION_HEADING_LIMIT)}{suffix}</b> · {kind}"
    )
    detail = _clip(html.escape(overview), CAPTION_OVERVIEW_LIMIT)
    return f"{heading}\n\n{detail}" if detail else heading


def safe_https_url(value: object) -> str | None:
    """Telegram, not this process, fetches the poster; only a plain HTTPS URL is passed on."""

    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def candidates(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Keep every row the model can name, in the order the gateway ranked them.

    A row without a usable label cannot be named or requested, so it is
    dropped. Nothing else is: recommend_media takes up to twenty titles and
    search_media a limit of ten, and a title cut here would be invisible --
    ``unmatched_titles`` reports only what the providers failed to match, so a
    truncated one looks exactly like a title that was never asked for.
    """

    rows = result.get("results")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if _nameable(row)]


def presentation(result: Mapping[str, Any], rows: list[dict[str, Any]], delivered: bool) -> str:
    """The instruction attached to a search result for the model's reply.

    There is no picker. A card can only ever offer a link to something already
    on Plex, so every choice -- which of several matches was meant, whether to
    add a missing title, which seasons of a series -- is made by talking to the
    model, which can ask and be answered in the same breath.
    """

    if len(rows) > 1 and result.get("presentation") == "recommendations":
        return (
            "Present these recommendations conversationally. For each title, state its "
            "name, year, media type, and whether it is on Plex. Offer to add any that are "
            "missing. Do not repeat raw candidate data."
        )
    if len(rows) > 1:
        return (
            "Several titles match. Present a numbered list (1., 2., 3., etc.) in the "
            "returned order, with each title, year, media type and Plex availability. "
            "Ask which one the user means and invite them to reply with its number or "
            "title. Never claim a selection was made, and request nothing until they answer."
        )
    # Only one card is sent per message and delivery can fail, so the second
    # lookup in a turn has no poster to point at. Saying otherwise invites a
    # reply that refers the user to a card they were never shown.
    card = (
        "A poster for this result was already sent, carrying an Open in Plex link when "
        "it is available. "
        if delivered
        else ""
    )
    season_states = rows[0].get("season_states")
    airing = isinstance(season_states, list) and any(
        isinstance(state, dict) and state.get("status") == "airing" for state in season_states
    )
    airing_note = (
        "For each season marked airing, label it 'Airing', report files out of episodes as "
        "the aired episodes available, and include next_airing when present. Never describe "
        "an airing season as complete or finished. "
        if airing
        else ""
    )
    return (
        f"{card}{airing_note}Answer only about this result. If this is a recommendation, "
        "explain why it fits the user's question. If the current user message "
        "explicitly asks to add or request it, call the matching request tool now; a "
        "series still needs its seasons named. Otherwise this is a read-only lookup: "
        "never imply it was requested. If the title is missing, offer to get it "
        "('Should I get it?'); for a series, ask which seasons if unspecified. "
        "Do not treat unknown availability as missing."
    )
