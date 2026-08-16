"""Server-rendered CRBL dashboard with no client-side dependency."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from .types import Page, Role

CSS = """
:root{--canvas:#fffbf5;--surface:#fff7ed;--card:#fff;--ink:#1c1917;--muted:#78716c;
--border:#e7e5e4;--border-strong:#d6d3d1;--accent:#f59e0b;--action:#d97706;
--action-hover:#b45309;--success:#16a34a;--danger:#dc2626;--shadow:0 12px 30px #1c19170d}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.5 Inter,
ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--action)}
button,input{font:inherit}.shell{max-width:1240px;margin:auto;padding:28px}.top{display:flex;align-items:center;
justify-content:space-between;gap:16px;margin-bottom:28px}.brand{display:flex;align-items:center;gap:12px}.mark{width:38px;
height:38px;border:1px solid #fcd34d;border-radius:12px;background:var(--surface);display:grid;place-items:center;
font-weight:900;color:var(--action)}h1{font-size:20px;margin:0}.subtitle{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:22px}.kpi,.panel{
background:var(--card);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}.kpi{padding:18px}
.kpi strong{display:block;font-size:28px}.kpi span{color:var(--muted)}.panel{margin:0 0 18px;overflow:hidden}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:18px 20px;border-bottom:1px solid var(--border)}
.panel-head h2{font-size:16px;margin:0}.inline{display:flex;align-items:center;gap:8px}.table-wrap{overflow:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 16px;border-bottom:1px solid var(--border);vertical-align:middle}
th{background:var(--surface);color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}.mono{font-family:"JetBrains Mono",ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.badge{display:inline-flex;padding:3px 8px;border-radius:999px;font-weight:700;font-size:12px;border:1px solid;white-space:nowrap}
/* Auto table layout hands the title column the slack, so every short
   column stays on one line and only the title wraps. */
.nowrap{white-space:nowrap}td .subtitle{white-space:nowrap}
.cell-title{min-width:170px}.cell-title strong{display:block}
.admin{color:#92400e;background:#fffbeb;border-color:#fcd34d}.user{color:#166534;background:#f0fdf4;border-color:#bbf7d0}
.blocked{color:#991b1b;background:#fef2f2;border-color:#fecaca}
.available,.requested{color:#166534;background:#f0fdf4;border-color:#bbf7d0}.pending{color:#92400e;background:#fffbeb;border-color:#fcd34d}
.unknown{color:#991b1b;background:#fef2f2;border-color:#fecaca}.request{color:#92400e;background:#fffbeb;border-color:#fcd34d}
.policy{color:#1e40af;background:#eff6ff;border-color:#bfdbfe}
.btn{border:1px solid var(--action);background:var(--action);color:#fff;border-radius:8px;padding:8px 12px;font-weight:700;cursor:pointer}
.btn:hover{background:var(--action-hover)}.btn.secondary{background:#fff;
color:var(--ink);border-color:var(--border-strong)}.btn.danger{color:var(--danger);border-color:#fecaca;background:#fff}
.input{min-width:180px;border:1px solid var(--border-strong);border-radius:8px;padding:9px 11px;background:#fff;color:var(--ink)}
.notice{padding:12px 14px;border:1px solid #fde68a;background:#fffbeb;border-radius:8px;margin-bottom:18px}.empty{padding:24px;color:var(--muted)}
.pager{display:flex;align-items:center;justify-content:flex-end;gap:12px;padding:13px 16px;border-top:1px solid var(--border)}
.pager a{text-decoration:none;font-weight:700}.pager .disabled{color:var(--muted)}
.login{max-width:420px;margin:12vh auto;padding:26px}.login h1{font-size:24px;margin-bottom:8px}.login .input{width:100%;margin:18px 0 12px}
.login .btn{width:100%}.error{color:var(--danger);margin-top:12px}.muted{color:var(--muted)}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #fbbf2488;outline-offset:2px}
@media(max-width:760px){.shell{padding:18px}.grid{grid-template-columns:1fr}.top,.panel-head{align-items:flex-start;flex-direction:column}
.inline{width:100%;flex-wrap:wrap}.input{flex:1;min-width:0}th,td{padding:10px 12px}}
"""


def _time(value: object) -> str:
    if not isinstance(value, int):
        return "Never"
    return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


# Canonical CRBL favicon, copied verbatim from the design system
# (assets/crbl-favicon.svg): the amber B on a dark rounded square, ~19%
# corner radius. Do not redraw it here.
FAVICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">\n  <rect width="32" height="32" rx="6" fill="#1C1917"></rect>\n  <text x="16" y="23" font-family="&#39;JetBrains Mono&#39;, &#39;Geist Mono&#39;, &#39;Fira Code&#39;, monospace" font-weight="700" font-size="20" text-anchor="middle" fill="#F59E0B">B</text>\n</svg>'


def _page(content: str, *, title: str = "Media operations") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · CRBL</title>
<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/app.css"></head><body>{content}</body></html>"""


def login_page(*, error: str | None = None) -> str:
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<main class="panel login"><div class="brand"><div class="mark">C</div><div><h1>Media operations</h1>
<div class="subtitle">CRBL private dashboard</div></div></div>{error_html}
<form method="post" action="/login"><label for="password" class="muted">Dashboard password</label>
<input class="input" id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button class="btn" type="submit">Sign in</button></form></main>""",
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
        f"""<main class="shell"><header class="top"><div class="brand"><div class="mark">C</div><div><h1>Media operations</h1>
<div class="subtitle">Users, requests, and Plex activity</div></div></div><form method="post" action="/logout">
<input type="hidden" name="csrf" value="{html.escape(csrf)}"><button class="btn secondary">Sign out</button></form></header>
{notice_html}<section class="grid"><div class="kpi"><strong>{allowed}</strong><span>Allowed users</span></div>
<div class="kpi"><strong>{admins}</strong><span>Administrators</span></div><div class="kpi"><strong>{blocked}</strong><span>Blocked contacts observed</span></div></section>
<section class="panel"><div class="panel-head"><h2>Users</h2><form class="inline" method="post" action="/users/add">
<input type="hidden" name="csrf" value="{html.escape(csrf)}"><input class="input mono" name="user_id" inputmode="numeric"
placeholder="Telegram user ID" required><button class="btn">Allow user</button></form></div><div class="table-wrap"><table>
<thead><tr><th>User</th><th>Telegram ID</th><th>Role</th><th>Last seen</th><th>Last blocked</th><th></th></tr></thead>
<tbody>{user_rows}</tbody></table></div></section><section class="panel" id="requests"><div class="panel-head"><h2>Requests</h2></div>
<div class="table-wrap"><table><thead><tr><th>Title</th><th>Type</th><th>Seasons</th><th>Requester</th><th>Status</th><th>Destinations</th><th>Created</th></tr></thead>
<tbody>{request_rows}</tbody></table></div>{_pager(requests, section="requests", other=activity)}</section><section class="panel" id="activity"><div class="panel-head"><h2>Activity</h2></div>
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
<input type="hidden" name="user_id" value="{user["user_id"]}"><button class="btn danger">Remove</button></form>"""
    elif user["role"] == Role.BLOCKED.value:
        action = f"""<form method="post" action="/users/add"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<input type="hidden" name="user_id" value="{user["user_id"]}"><button class="btn secondary">Allow</button></form>"""
    return f"""<tr><td><strong>{html.escape(str(display))}</strong><div class="subtitle">{html.escape(str(sub))}</div></td>
<td class="mono">{user["user_id"]}</td><td><span class="badge {html.escape(str(user["role"]))}">{html.escape(str(user["role"]).title())}</span></td>
<td>{_time(user["last_seen"])}</td><td>{_time(user["last_blocked"])}</td><td>{action}</td></tr>"""
