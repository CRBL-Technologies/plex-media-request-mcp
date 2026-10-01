"""Server-rendered CRBL dashboard with no client-side dependency."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from .types import Page, Role

CSS = """
/* Layout only. Shared Atelier tokens, fonts and controls live in /assets/brand. */
*{box-sizing:border-box}
.shell{max-width:1240px;margin:auto;padding:var(--space-2xl) var(--space-lg)}
.top{display:flex;align-items:center;justify-content:space-between;gap:var(--space-lg);
padding-bottom:var(--space-xl);margin-bottom:var(--space-xl);border-bottom:1px solid var(--color-border)}
.brand{display:flex;align-items:center;gap:var(--space-md);min-width:0}
.mark{width:48px;height:48px;flex:none;border:1px solid var(--color-border);
background:var(--color-surface);display:grid;place-items:center;color:var(--color-accent-dark)}
h1{font-size:clamp(24px,3vw,32px);margin:0 0 var(--space-xs)}
.subtitle{color:var(--color-text-muted);font-size:var(--font-size-body-sm)}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--space-md);margin-bottom:var(--space-xl)}
.kpi strong{display:block;font-family:var(--font-display);font-weight:500;font-size:40px;line-height:1.2;margin-bottom:var(--space-sm)}
.kpi span{color:var(--color-text-muted);font-family:var(--font-mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
.panel{padding:0;margin:0 0 var(--space-lg);overflow:hidden}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:var(--space-md);
padding:var(--space-lg);border-bottom:1px solid var(--color-border)}
.panel-head h2{font-size:var(--font-size-h3);margin:0}
.inline{display:flex;align-items:center;gap:var(--space-sm)}
.inline .input{width:220px}.inline .btn{flex:none}
.table-wrap{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:var(--font-size-body-sm)}
th,td{text-align:left;padding:var(--space-md);border-bottom:1px solid var(--color-border);vertical-align:middle}
th{color:var(--color-text-muted);font-family:var(--font-mono);font-weight:400;font-size:11px;text-transform:uppercase;letter-spacing:.12em}
td strong{font-weight:500}tr:last-child td{border-bottom:0}
.mono{font-size:12px}.badge{font-size:11px;letter-spacing:.1em;white-space:nowrap}
/* Auto table layout hands the title column the slack, so every short
   column stays on one line and only the title wraps. */
.nowrap{white-space:nowrap}td .subtitle{white-space:nowrap}
.cell-title{min-width:170px}.cell-title strong{display:block}
.admin,.pending,.request{color:var(--color-accent-darker);background:var(--color-warning-bg);border-color:var(--color-warning)}
.user,.available,.requested{color:var(--color-success);background:var(--color-success-bg);border-color:var(--color-success)}
.blocked,.unknown{color:var(--color-error);background:var(--color-error-bg);border-color:var(--color-error)}
.policy{color:var(--color-info);background:var(--color-info-bg);border-color:var(--color-info)}
.btn.danger{color:var(--color-error);border-color:var(--color-error)}
.btn.danger:hover{background:var(--color-error-bg)}
.notice{padding:var(--space-md);border:1px solid var(--color-warning);background:var(--color-warning-bg);margin-bottom:var(--space-lg)}
.empty{padding:var(--space-lg);color:var(--color-text-muted)}
.pager{display:flex;align-items:center;justify-content:flex-end;flex-wrap:wrap;gap:var(--space-md);
padding:var(--space-md);border-top:1px solid var(--color-border);font-size:var(--font-size-body-sm)}
.pager a{font-weight:500;text-decoration:underline;text-underline-offset:4px}
.pager .disabled{color:var(--color-text-muted)}
.login{width:calc(100% - 32px);max-width:460px;margin:12vh auto;padding:var(--space-xl)}
.login .brand{margin-bottom:var(--space-xl)}.login h1{font-size:var(--font-size-h3)}
.login label{display:block;font-size:var(--font-size-body-sm);margin-bottom:var(--space-sm)}
.login .input{margin-bottom:var(--space-md)}.login .btn{width:100%;justify-content:center}
.error{color:var(--color-error);margin-bottom:var(--space-md)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--color-accent-darker);outline-offset:3px}
@media(max-width:760px){.shell{padding:var(--space-lg) var(--space-md)}.grid{grid-template-columns:1fr;gap:var(--space-sm)}
.kpi{display:flex;align-items:center;gap:var(--space-md);padding:var(--space-md)}.kpi strong{margin:0;min-width:48px;font-size:32px}
.top,.panel-head{align-items:flex-start;flex-direction:column}.panel-head{padding:var(--space-md)}
.inline{width:100%;flex-wrap:wrap}.inline .input{flex:1;min-width:140px;width:auto}th,td{padding:12px}
.login{margin-top:var(--space-2xl);padding:var(--space-lg)}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*::before,*::after{animation:none!important;transition:none!important}.btn:active{transform:none}}
"""


def _time(value: object) -> str:
    if not isinstance(value, int):
        return "Never"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


# Brand marks for this dashboard. The glyph is Lucide's clapperboard
# (lucide-static v1.31.0, ISC). A gold letter on a dark rounded square is already
# taken: B is the CRBL favicon and P is the Portless app icon, so a media
# glyph keeps this dashboard distinct from both.
#
# FAVICON follows the Atelier app-icon pattern (warm ink square, ~19% radius, gold
# glyph). MARK_ICON is the in-page chip, which follows the UI icon rule
# instead: 20px at stroke 1.5, inheriting its color from the chip.
FAVICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32"><rect width="32" height="32" rx="6" fill="#221F19"/><g transform="translate(6 6) scale(0.8333)" fill="none" stroke="#C9A45C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12.296 3.464 3.02 3.956"/><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3z"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="m6.18 5.276 3.1 3.899"/></g></svg>'
MARK_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m12.296 3.464 3.02 3.956"/><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3z"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="m6.18 5.276 3.1 3.899"/></svg>'


def _page(content: str, *, title: str = "Media admin") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · CRBL</title>
<meta name="theme-color" content="#EFEBE1">
<link rel="icon" href="/assets/favicon.svg?v=atelier" type="image/svg+xml">
<link rel="stylesheet" href="/assets/brand/colors_and_type.css">
<link rel="stylesheet" href="/assets/app.css"></head><body>{content}</body></html>"""


def login_page(*, error: str | None = None) -> str:
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<main class="card panel login"><div class="brand"><div class="mark">{MARK_ICON}</div><div><h1>Media admin</h1>
<div class="subtitle">CRBL private dashboard</div></div></div>{error_html}
<form method="post" action="/login"><label for="password" class="muted">Dashboard password</label>
<input class="input" id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button class="btn btn-primary" type="submit">Sign in</button></form></main>""",
        title="Sign in",
    )


def dashboard_page(
    *,
    users: list[dict[str, Any]],
    activity: Page,
    requests: Page,
    csrf: str,
    notice: str | None,
) -> str:
    admins = sum(user["role"] == Role.ADMIN.value for user in users)
    allowed = sum(user["role"] in {Role.ADMIN.value, Role.USER.value} for user in users)
    blocked = sum(user["last_blocked"] is not None for user in users)
    user_rows = (
        "".join(_user_row(user, csrf) for user in users)
        or '<tr><td colspan="6" class="empty">No users observed yet.</td></tr>'
    )
    activity_rows = (
        "".join(
            f'<tr><td>{_time(item["occurred_at"])}</td><td><span class="badge {html.escape(str(item["kind"]))}">'
            f'{html.escape(str(item["kind"]).title())}</span></td><td class="mono">{html.escape(str(item["user_id"] or "—"))}</td>'
            f"<td>{html.escape(str(item['label']))}</td></tr>"
            for item in activity.items
        )
        or '<tr><td colspan="4" class="empty">No activity recorded yet.</td></tr>'
    )
    request_rows = (
        "".join(
            f'<tr><td class="cell-title"><strong>{html.escape(str(item["title"]))}</strong>'
            f'<div class="subtitle">{("TMDB" if item["media_type"] == "movie" else "TVDB")} {item["external_id"]}</div></td>'
            f'<td class="nowrap">{html.escape(str(item["media_type"]).title())}</td>'
            f"""<td class="nowrap">{html.escape(", ".join("S" + str(s) for s in item["seasons"]) or "—")}</td>"""
            f'<td class="nowrap">{_requester(item)}</td>'
            f'<td class="nowrap"><span class="badge {html.escape(str(item["state"]))}">'
            f"{html.escape(_request_status(item))}</span></td>"
            f'<td class="nowrap">{len(item["destinations"])}</td>'
            f'<td class="nowrap">{_time(item["created_at"])}</td></tr>'
            for item in requests.items
        )
        or '<tr><td colspan="7" class="empty">No bot requests recorded yet.</td></tr>'
    )
    notice_html = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    return _page(
        f"""<main class="shell"><header class="top"><div class="brand"><div class="mark">{MARK_ICON}</div><div><h1>Media admin</h1>
<div class="subtitle">Users, requests, and Plex activity</div></div></div><form method="post" action="/logout">
<input type="hidden" name="csrf" value="{html.escape(csrf)}"><button class="btn btn-secondary">Sign out</button></form></header>
{notice_html}<section class="grid"><div class="card kpi"><strong>{allowed}</strong><span>Allowed users</span></div>
<div class="card kpi"><strong>{admins}</strong><span>Administrators</span></div><div class="card kpi"><strong>{blocked}</strong><span>Blocked contacts observed</span></div></section>
<section class="card panel"><div class="panel-head"><h2>Users</h2><form class="inline" method="post" action="/users/add">
<input type="hidden" name="csrf" value="{html.escape(csrf)}"><label class="sr-only" for="user-id">Telegram user ID</label><input class="input mono" id="user-id" name="user_id" inputmode="numeric"
placeholder="Telegram user ID" required><button class="btn btn-primary">Allow user</button></form></div><div class="table-wrap"><table>
<thead><tr><th>User</th><th>Telegram ID</th><th>Role</th><th>Last seen</th><th>Last blocked</th><th></th></tr></thead>
<tbody>{user_rows}</tbody></table></div></section><section class="card panel" id="requests"><div class="panel-head"><h2>Requests</h2></div>
<div class="table-wrap"><table><thead><tr><th>Title</th><th>Type</th><th>Seasons</th><th>Requester</th><th>Status</th><th>Destinations</th><th>Created</th></tr></thead>
<tbody>{request_rows}</tbody></table></div>{_pager(requests, section="requests", other=activity)}</section><section class="card panel" id="activity"><div class="panel-head"><h2>Activity</h2></div>
<div class="table-wrap"><table><thead><tr><th>Time</th><th>Event</th><th>User ID</th><th>Detail</th></tr></thead>
<tbody>{activity_rows}</tbody></table></div>{_pager(activity, section="activity", other=requests)}</section></main>"""
    )


def _request_status(item: dict[str, Any]) -> str:
    """One status per request.

    ``state`` is derived from ``provider_status`` ("available" or otherwise),
    so showing both said the same thing twice while hiding the distinction the
    operator actually needs: whether an active acquisition is still hunting a
    release or already waiting on a Plex scan.
    """

    state = str(item["state"])
    if state in {"pending", "unknown"}:
        return {"pending": "Intent pending", "unknown": "Needs reconciliation"}[state]
    provider_status = str(item.get("provider_status") or "")
    return {
        "available": "Available",
        "awaiting_plex": "Waiting for Plex",
        "search_started": "Searching for a release",
        "requested": "Queued in Radarr/Sonarr",
    }.get(provider_status, provider_status.replace("_", " ").title() or state.title())


def _requester(item: dict[str, Any]) -> str:
    username = f"@{item['username']}" if item.get("username") else None
    display = item.get("name") or username or f"User {item['user_id']}"
    identifier = f'<div class="subtitle mono">{item["user_id"]}</div>'
    return f"<strong>{html.escape(str(display))}</strong>{identifier}"


def _pager(page: Page, *, section: str, other: Page) -> str:
    request_page = page.number if section == "requests" else other.number
    activity_page = page.number if section == "activity" else other.number

    def href(number: int) -> str:
        requests = number if section == "requests" else request_page
        activity = number if section == "activity" else activity_page
        return f"/?request_page={requests}&amp;activity_page={activity}#{section}"

    previous = (
        f'<a href="{href(page.number - 1)}">Previous</a>'
        if page.number > 1
        else '<span class="disabled">Previous</span>'
    )
    following = (
        f'<a href="{href(page.number + 1)}">Next</a>'
        if page.number < page.pages
        else '<span class="disabled">Next</span>'
    )
    return (
        f'<nav class="pager" aria-label="{section.title()} pages">{previous}'
        f"<span>Page {page.number} of {page.pages} · {page.total} total</span>{following}</nav>"
    )


def _user_row(user: dict[str, Any], csrf: str) -> str:
    username = f"@{user['username']}" if user["username"] else None
    display = user["name"] or username or "Unknown user"
    sub = username if username and username != display else ""
    action = ""
    if user["role"] == Role.USER.value:
        action = f"""<form method="post" action="/users/remove"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<input type="hidden" name="user_id" value="{user["user_id"]}"><button class="btn btn-secondary danger">Remove</button></form>"""
    elif user["role"] == Role.BLOCKED.value:
        action = f"""<form method="post" action="/users/add"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<input type="hidden" name="user_id" value="{user["user_id"]}"><button class="btn btn-secondary">Allow</button></form>"""
    return f"""<tr><td><strong>{html.escape(str(display))}</strong><div class="subtitle">{html.escape(str(sub))}</div></td>
<td class="mono">{user["user_id"]}</td><td><span class="badge {html.escape(str(user["role"]))}">{html.escape(str(user["role"]).title())}</span></td>
<td>{_time(user["last_seen"])}</td><td>{_time(user["last_blocked"])}</td><td>{action}</td></tr>"""
