"""Small server-rendered views for the operations dashboard.

The dashboard intentionally ships no JavaScript, fonts, images, analytics, or
other external assets.  Keeping the HTML here as escaped fragments makes the
content-security policy auditable and prevents companion data from becoming an
HTML injection path.
"""

from __future__ import annotations

from collections.abc import Mapping
import html
import json
from typing import Any

from media_companion.redaction import redact_json


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json_for_pre(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except (TypeError, ValueError):
        text = "{}"
    return _escape(text)


def page(title: str, content: str, *, status_text: str | None = None) -> str:
    """Render a complete document under the dashboard's strict CSP."""

    status = (
        "" if status_text is None else f'<p class="status">{_escape(status_text)}</p>'
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_escape(title)}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "</head><body>"
        f"<header><h1>{_escape(title)}</h1>{status}</header>"
        f"{content}</body></html>"
    )


def login(*, error: str | None = None) -> str:
    content = (
        '<main class="card"><form method="post" action="/login" autocomplete="off">'
        '<label for="password">Dashboard password</label>'
        '<input id="password" name="password" type="password" required '
        'maxlength="1024" autocomplete="current-password">'
        '<p><button type="submit">Sign in</button></p></form></main>'
    )
    return page("Media operations", content, status_text=error)


def dashboard(*, actor: str = "dashboard admin", csrf_token: str | None = None) -> str:
    logout_token = ""
    if csrf_token:
        logout_token = (
            f'<input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">'
        )
    csrf_field = (
        f'<input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">'
        if csrf_token
        else ""
    )
    content = (
        f"<p>Signed in as {_escape(actor)}. Operational data is retrieved through the "
        "typed private companion API.</p>"
        '<nav aria-label="Operations">'
        '<a href="/health">Health</a> '
        '<a href="/users">Users</a> '
        '<a href="/blocked">Blocked contacts</a> '
        '<a href="/subscriptions">Subscriptions</a> '
        '<a href="/deliveries">Deliveries</a> '
        '<a href="/quarantine">Quarantine</a> '
        '<a href="/oracle">No-loss oracle</a> '
        '<a href="/audit">Audit</a>'
        "</nav>"
        "<main><h2>Allowlist preview</h2>"
        "<p>Submitting either form requests a fresh, exact preview. The companion must return a one-time capability before an edit can execute.</p>"
        '<form method="post" action="/users/add" autocomplete="off">'
        "<fieldset><legend>Add regular user</legend>"
        f"{csrf_field}"
        '<label>User ID <input name="user_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Identity fingerprint <input name="fingerprint" maxlength="256" required></label>'
        '<label>Allowlist version <input name="version" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview add</button></fieldset></form>'
        '<form method="post" action="/users/remove" autocomplete="off">'
        "<fieldset><legend>Remove regular user</legend>"
        f"{csrf_field}"
        '<label>User ID <input name="user_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Identity fingerprint <input name="fingerprint" maxlength="256" required></label>'
        '<label>Allowlist version <input name="version" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview remove</button></fieldset></form>'
        "<h2>Delivery recovery preview</h2>"
        '<form method="post" action="/deliveries/retry-once" autocomplete="off">'
        "<fieldset><legend>Retry once</legend>"
        f"{csrf_field}"
        '<label>Delivery ID <input name="delivery_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview retry</button></fieldset></form>'
        '<form method="post" action="/deliveries/mark-abandoned" autocomplete="off">'
        "<fieldset><legend>Mark abandoned</legend>"
        f"{csrf_field}"
        '<label>Delivery ID <input name="delivery_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview mark abandoned</button></fieldset></form>'
        '<form method="post" action="/deliveries/assume-sent" autocomplete="off">'
        "<fieldset><legend>Assume sent</legend>"
        f"{csrf_field}"
        '<label>Delivery ID <input name="delivery_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview assume sent</button></fieldset></form>'
        '<form method="post" action="/deliveries/resend-once" autocomplete="off">'
        "<fieldset><legend>Resend once</legend>"
        f"{csrf_field}"
        '<label>Delivery ID <input name="delivery_id" inputmode="numeric" pattern="[0-9]+" required></label>'
        '<label>Idempotency key <input name="idempotency_key" maxlength="256" required></label>'
        '<button type="submit">Preview resend</button></fieldset></form></main>'
        '<form method="post" action="/logout">'
        f'{logout_token}<button type="submit">Sign out</button></form>'
    )
    return page("Media operations", content)


def operation_result(
    operation: str,
    data: Mapping[str, Any],
    *,
    actor: str | None = None,
    csrf_token: str | None = None,
    action: str | None = None,
    submitted: Mapping[str, Any] | None = None,
) -> str:
    """Render a bounded typed operation response for a no-script browser."""

    heading = f"{operation.replace('_', ' ').title()}"
    actor_text = "" if actor is None else f"<p>Signed in as {_escape(actor)}.</p>"
    try:
        safe_data = redact_json(data)
    except (TypeError, ValueError):
        safe_data = {}
    exact_preview = ""
    if isinstance(safe_data, Mapping) and isinstance(safe_data.get("preview"), str):
        exact_preview = (
            '<section aria-label="Exact confirmation preview"><h2>Exact preview</h2>'
            f"<pre>{_escape(safe_data['preview'])}</pre></section>"
        )
    followup = ""
    if (
        action
        and csrf_token
        and submitted
        and data.get("confirmation_required") is True
        and isinstance(data.get("confirmation_capability"), str)
        and isinstance(data.get("preview_digest"), str)
    ):
        hidden = []
        for key, value in submitted.items():
            hidden.append(
                f'<input type="hidden" name="{_escape(key)}" value="{_escape(value)}">'
            )
        hidden.extend(
            (
                f'<input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">',
                f'<input type="hidden" name="confirmation" value="{_escape(data["confirmation_capability"])}">',
                f'<input type="hidden" name="preview_digest" value="{_escape(data["preview_digest"])}">',
            )
        )
        followup = (
            '<form method="post" action="'
            + _escape(action)
            + '"><p>This exact preview is bound to this session and expires shortly.</p>'
            + "".join(hidden)
            + '<button type="submit">Execute exact preview</button></form>'
        )
    return page(
        heading,
        f'{actor_text}<main class="card">{exact_preview}<pre>{_json_for_pre(safe_data)}</pre>'
        f"{followup}"
        '<p><a href="/">Back to dashboard</a></p></main>',
    )


def error_page(status_text: str = "The request could not be completed.") -> str:
    return page(
        "Media operations",
        '<main class="card"><p>Try again later.</p></main>',
        status_text=status_text,
    )


__all__ = ["dashboard", "error_page", "login", "operation_result", "page"]
