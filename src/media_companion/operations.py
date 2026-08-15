"""Application-boundary protocols and safe operation helpers.

The rest of :mod:`media_companion` contains the durable workflows and the
provider adapters.  This module is intentionally the small seam between
those components and the HTTP application.  It owns no transport, discovers
no tools, and never exposes a provider object directly.

The classes here are deliberately duck-typed at the dependency seams.  That
keeps the application usable with the real SQLite/adapters as well as with
small deterministic fakes in contract tests without weakening the production
checks performed by :class:`CompanionRuntime`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import hmac
import inspect
import json
import re
import secrets
import time
from typing import Any, Final, Protocol, TypeAlias, cast

from .auth import (
    ActorAssertionVerifier,
    ActorClaims,
    ConfirmationRecord,
    ConfirmationTokenStore,
    InMemoryConfirmationTokenStore,
    InMemoryNonceReplayStore,
    MAX_CANONICAL_BYTES,
    canonical_argument_hash,
    canonical_json,
)
from .errors import AuthorizationError, DependencyError
from .plex_ingress import WebhookRateLimiter
from .rate_limit import (
    DEFAULT_RATE_LIMIT_POLICY,
    InMemoryRateLimiter,
    RateLimitDecision,
    RateLimitExceeded,
    RateLimitPolicy,
    RateOperation,
)
from .redaction import redact_json, redact_text
from .tool_policy import (
    SHARED_TOOL_SET,
    UPSTREAM_TOOL_SET,
)

try:  # ``safe_views`` is a pure module, but retain an import fallback for tiny deployments.
    from .safe_views import response_dict, serialize_response
except ImportError:  # pragma: no cover - defensive packaging fallback
    response_dict = None  # type: ignore[assignment]
    serialize_response = None  # type: ignore[assignment]


JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
MaybeAwaitable: TypeAlias = object | Awaitable[object]

MAX_OPERATION_ARGUMENT_BYTES: Final[int] = 64 * 1024
MAX_OPERATION_RESPONSE_BYTES: Final[int] = 256 * 1024
MAX_OPERATION_DEPTH: Final[int] = 16
MAX_OPERATION_ITEMS: Final[int] = 10_000
MAX_OPERATION_TEXT_BYTES: Final[int] = 16 * 1024
MAX_PREVIEW_BYTES: Final[int] = 64 * 1024
MAX_DASHBOARD_BODY_BYTES: Final[int] = 64 * 1024
MAX_DASHBOARD_RESPONSE_BYTES: Final[int] = 256 * 1024
MCP_AUDIENCE: Final[str] = "media-companion"
CONFIRMATION_AUDIENCE: Final[str] = "confirmation-callback"
CONFIRMATION_TOOL: Final[str] = "confirmation_callback"
POLICY_VERSION_DEFAULT: Final[str] = "1"

# This is intentionally duplicated as a transport-independent contract so
# production startup can prove that the typed operations provider is complete
# without importing the ASGI module (which would create a cycle).
DASHBOARD_OPERATION_SET: Final[frozenset[str]] = frozenset(
    {
        "health",
        "users",
        "users.resolve",
        "blocked",
        "subscriptions",
        "deliveries",
        "quarantine",
        "oracle",
        "audit",
        "users.add",
        "users.remove",
        "delivery.retry_once",
        "delivery.mark_abandoned",
        "delivery.assume_sent",
        "delivery.resend_once",
    }
)

_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDEMPOTENCY_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "raw",
        "raw_payload",
        "payload",
        "provider_response",
        "webhook_payload",
        "message_text",
        "session",
        "document",
        "environment",
        "env",
        "sql",
        "mcp",
        "exception",
        "traceback",
        "error_text",
        "raw_error",
        "token",
        "secret",
        "password",
        "api_key",
        "access_token",
        "authorization",
        "actor",
        "signed_assertion",
    }
)

# This is a schema vocabulary, not a recursive redaction policy.  Provider
# keys not listed here remain absent even if they happen not to look sensitive.
SAFE_RESULT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "status",
        "state",
        "result",
        "tool",
        "content",
        "structuredContent",
        "structured_content",
        "isError",
        "is_error",
        "confirmation_required",
        "confirmation",
        "confirmation_capability",
        "preview",
        "preview_digest",
        "changed",
        "idempotency_key",
        "fingerprint",
        "user_id",
        "delivery_id",
        "version",
        "operation",
        "expires_at",
        "token_hash",
        "callback_prefix",
        "action",
        "target",
        "target_identity",
        "state_fingerprint",
        "request_id",
        "subscription_id",
        "request_key",
        "created",
        "media_type",
        "provider_id",
        "title",
        "year",
        "seasons",
        "season_number",
        "episode_number",
        "mode",
        "status",
        "error",
        "message",
        "items",
        "as_of",
        "next_cursor",
        "truncated",
        "total",
        "partial_errors",
        "service",
        "progress_percent",
        "eta_seconds",
        "available",
        "quality",
        "plex_url",
        "library_name",
        "show_title",
        "dependencies",
        "services",
        "healthy",
        "ready",
        "fresh",
        "complete",
        "users",
        "blocked",
        "subscriptions",
        "deliveries",
        "quarantine",
        "audit",
        "oracle",
        "residual",
        "accounted",
        "unaccounted",
        "count",
        "attempts",
        "attempt_count",
        "id",
        "user_id",
        "chat_id",
        "delivery_id",
        "record_id",
        "role",
        "access",
        "display_name",
        "username",
        "chat_type",
        "reason",
        "reason_code",
        "outcome",
        "fingerprint",
        "policy_version",
        "notification_class",
        "destination_state",
        "possible_duplicate",
        "resend_generation",
        "version",
        "generation",
        "source",
        "updated_at",
        "created_at",
        "first_seen_at",
        "last_seen_at",
        "first_seen",
        "last_seen",
        "added_at",
        "revoked_at",
        "activation",
        "migration",
        "worker",
        "webhook",
        "reconciliation",
        "details",
        "data",
        "errors",
    }
)


class OperationBoundaryError(AuthorizationError):
    """A request failed a closed application-boundary check."""


class OperationValidationError(OperationBoundaryError):
    """A request or typed result was outside the reviewed bound."""


class OperationDependencyError(DependencyError):
    """A required injected dependency was unavailable."""


class DurableStoreRequiredError(OperationBoundaryError):
    """Production was configured with an in-process replay/capability store."""


class MutationAlreadyClaimed(OperationBoundaryError):
    """A second safe mutation was attempted for one actor/update."""


@dataclass(frozen=True, slots=True)
class ActorPolicy:
    """The narrow policy decision returned by the Hermes helper."""

    user_id: int
    chat_id: int
    allowed: bool
    role: str
    fingerprint: str
    version: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise OperationValidationError("policy user identity is invalid")
        if (
            isinstance(self.chat_id, bool)
            or not isinstance(self.chat_id, int)
            or self.chat_id == 0
        ):
            raise OperationValidationError("policy chat identity is invalid")
        if not isinstance(self.allowed, bool):
            raise OperationValidationError("policy allow decision is invalid")
        if self.role not in {"user", "admin", "unknown"}:
            raise OperationValidationError("policy role is invalid")
        if not isinstance(self.fingerprint, str) or not self.fingerprint:
            raise OperationValidationError("policy fingerprint is invalid")
        if self.version and (
            not isinstance(self.version, str) or len(self.version) > 256
        ):
            raise OperationValidationError("policy version is invalid")

    @property
    def is_admin(self) -> bool:
        return self.allowed and self.role == "admin"


class PolicyProvider(Protocol):
    def membership(self, *, user_id: int, chat_id: int) -> object: ...


class SafeToolHandler(Protocol):
    def __call__(
        self, arguments: Mapping[str, JsonValue], **kwargs: object
    ) -> MaybeAwaitable: ...


class UpstreamToolProxy(Protocol):
    def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue] | None = None
    ) -> object: ...


class ConfirmationExecutor(Protocol):
    def __call__(self, record: ConfirmationRecord) -> MaybeAwaitable: ...


class ConfirmationBridge(Protocol):
    def __call__(self, **kwargs: object) -> MaybeAwaitable: ...


class ConfirmationArgumentsStore(Protocol):
    """Durable binding for the exact arguments behind a capability.

    ``ConfirmationRecord`` intentionally stores only the argument hash.  A
    separate durable adapter keeps the bounded, canonical arguments available
    to the callback executor without putting them in the Telegram token or
    the model-visible confirmation envelope.  Implementations must atomically
    claim/remove the binding on ``consume`` so a replay cannot recover it.
    """

    def put(
        self,
        *,
        token_hash: str,
        tool: str,
        argument_hash: str,
        arguments: Mapping[str, JsonValue],
        expires_at: int,
    ) -> object: ...

    def consume(
        self,
        *,
        token_hash: str,
        tool: str,
        argument_hash: str,
    ) -> Mapping[str, JsonValue] | object | None: ...


class EventInbox(Protocol):
    def persist_event(self, event: object) -> object: ...


class ReadinessProvider(Protocol):
    def __call__(self) -> object: ...


class DashboardIdentityResolver(Protocol):
    """Resolve the signed dashboard actor to a current typed identity.

    The dashboard signature's actor is an opaque display label.  It is never
    an authorization identity by itself.  A production resolver must return
    the fixed ``dashboard-admin`` service principal with ``allowed``,
    ``role``, current allowlist ``fingerprint``, and ``version``.  Numeric
    Telegram IDs are optional adapter details and are not used for dashboard
    authorization or rate limiting.  Keeping this as a protocol prevents the
    HTTP boundary from guessing how Hermes stores its native policy.
    """

    def __call__(self, actor: str) -> object: ...


class DashboardPolicyRechecker(Protocol):
    """Re-read dashboard identity/policy before every operation.

    Production results retain the ``dashboard-admin`` service-principal
    marker and the current allowlist fingerprint/version; a numeric Telegram
    administrator is never substituted for the dashboard session.
    """

    def __call__(
        self,
        *,
        actor: str,
        identity: object,
        operation: str,
        arguments: Mapping[str, JsonValue],
    ) -> object: ...


class DashboardMutationGuard(Protocol):
    """Atomically enforce dashboard CAS/admin-removal policy.

    The guard is a typed authorization/CAS decision only.  It must not perform
    the mutation before the one-time confirmation capability is consumed.
    """

    def __call__(
        self,
        *,
        actor: str,
        identity: object,
        operation: str,
        arguments: Mapping[str, JsonValue],
    ) -> object: ...


class DashboardConfirmationGuard(Protocol):
    """Consume one exact, durable dashboard preview capability.

    Implementations must compare the opaque confirmation against the exact
    server-rendered preview/body/target and consume it atomically.  Returning
    ``False``/``None`` (or a bare boolean) denies the mutation; a non-boolean
    value must be a typed audit/consumption record for the originally bound
    operation/arguments, forwarded to the handler only after fresh policy
    revalidation.
    """

    def __call__(
        self,
        *,
        actor: str,
        identity: object,
        operation: str,
        arguments: Mapping[str, JsonValue],
        preview: str,
        confirmation: str,
    ) -> object: ...


class DashboardConfirmationIssuer(Protocol):
    """Issue one durable dashboard capability for an exact preview."""

    def __call__(
        self,
        *,
        actor: str,
        identity: object,
        operation: str,
        arguments: Mapping[str, JsonValue],
        preview: str,
        preview_digest: str,
        policy: object,
    ) -> object: ...


def _bounded_json(
    value: object, *, depth: int = 0, count: list[int] | None = None
) -> JsonValue:
    """Validate a JSON-shaped value and keep only the typed safe vocabulary."""

    if depth > MAX_OPERATION_DEPTH:
        raise OperationValidationError("result exceeds the nesting bound")
    counter = [0] if count is None else count
    counter[0] += 1
    if counter[0] > MAX_OPERATION_ITEMS:
        raise OperationValidationError("result exceeds the item bound")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > (1 << 53) - 1:
            raise OperationValidationError("result contains an unsafe number")
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise OperationValidationError("result contains an invalid number")
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8", "strict")) > MAX_OPERATION_TEXT_BYTES:
            raise OperationValidationError("result contains oversized text")
        return redact_text(value, max_bytes=MAX_OPERATION_TEXT_BYTES)
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for raw_key, raw_child in value.items():
            if (
                not isinstance(raw_key, str)
                or len(raw_key.encode("utf-8", "strict")) > 256
            ):
                raise OperationValidationError("result contains an invalid field")
            if raw_key in _FORBIDDEN_KEYS or raw_key.lower() in _FORBIDDEN_KEYS:
                continue
            if raw_key not in SAFE_RESULT_KEYS:
                continue
            result[raw_key] = _bounded_json(raw_child, depth=depth + 1, count=counter)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_OPERATION_ITEMS:
            raise OperationValidationError("result contains too many items")
        return [_bounded_json(child, depth=depth + 1, count=counter) for child in value]
    raise OperationValidationError("result contains an unsupported value")


def _json_bytes(
    value: object,
    *,
    maximum: int = MAX_OPERATION_ARGUMENT_BYTES,
    max_bytes: int | None = None,
) -> bytes:
    """Encode one bounded canonical JSON value.

    ``max_bytes`` is retained as an explicit compatibility spelling because
    the surrounding typed adapters use that name.  Keeping the alias here
    avoids an accidental unbounded path when callers pass the adapter's
    vocabulary through this boundary.
    """

    if max_bytes is not None:
        maximum = max_bytes
    try:
        if maximum <= MAX_CANONICAL_BYTES:
            encoded = canonical_json(value, max_bytes=maximum)
        else:
            # The auth canonicalizer intentionally caps assertion-sized JSON
            # at 64 KiB.  Response envelopes have a separate 256 KiB bound,
            # so use deterministic stdlib JSON for a size-only check once the
            # value has already passed the typed/redacted result walk.
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", "strict")
    except (TypeError, ValueError) as exc:
        raise OperationValidationError(
            "JSON value is outside the request bound"
        ) from exc
    if len(encoded) > maximum:
        raise OperationValidationError("JSON value exceeds the byte bound")
    return encoded


def _mapping(
    value: object, message: str = "JSON object is required"
) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise OperationValidationError(message)
    result: dict[str, JsonValue] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise OperationValidationError("JSON object keys must be text")
        result[key] = cast(JsonValue, child)
    _json_bytes(result)
    return result


def _policy_from(value: object, *, user_id: int, chat_id: int) -> ActorPolicy:
    if isinstance(value, ActorPolicy):
        return value
    if isinstance(value, Mapping):
        source = value
        return ActorPolicy(
            user_id=source.get("user_id", user_id),
            chat_id=source.get("chat_id", chat_id),
            allowed=source.get("allowed", source.get("is_authorized", False)),
            role=str(source.get("role", "unknown")),
            fingerprint=str(
                source.get("fingerprint", source.get("allowlist_fingerprint", ""))
            ),
            version=str(source.get("version", source.get("allowlist_version", ""))),
        )
    try:
        allowed_value = getattr(value, "allowed", None)
        if allowed_value is None:
            allowed_value = getattr(value, "is_authorized", False)
        fingerprint_value = getattr(value, "fingerprint", None)
        if fingerprint_value is None:
            fingerprint_value = getattr(value, "allowlist_fingerprint", "")
        version_value = getattr(value, "version", None)
        if version_value is None:
            version_value = getattr(value, "allowlist_version", "")
        allowed = bool(allowed_value)
        role = str(getattr(value, "role", "unknown"))
        fingerprint = str(fingerprint_value)
        version = str(version_value)
    except Exception as exc:  # noqa: BLE001
        raise OperationDependencyError("current policy is unavailable") from exc
    return ActorPolicy(user_id, chat_id, allowed, role, fingerprint, version)


def _safe_actor_arguments(
    arguments: Mapping[str, JsonValue], claims: ActorClaims
) -> dict[str, JsonValue]:
    """Overwrite every recognized requester field with trusted actor values."""

    result = dict(arguments)
    user_id = claims.user_id
    chat_id = claims.chat_id
    username: JsonValue = None
    # A username is display-only; the actor assertion intentionally contains no
    # mutable display label.  Never retain a model-supplied one.
    for key in (
        "requester_user_id",
        "requested_by_user_id",
        "actor_user_id",
        "user_id",
    ):
        if key in result:
            result[key] = user_id
    for key in (
        "requester_chat_id",
        "requested_by_chat_id",
        "actor_chat_id",
        "chat_id",
    ):
        if key in result:
            result[key] = chat_id
    for key in (
        "requester_username",
        "requested_by_username",
        "actor_username",
        "username",
    ):
        if key in result:
            result[key] = username
    for key in ("actor", "_actor", "trusted_actor"):
        result.pop(key, None)
    # Handlers can always consume these explicit, server-owned fields.  Adding
    # them is idempotent and does not alter the assertion's received-args hash.
    result.setdefault("requested_by_user_id", user_id)
    result.setdefault("requested_by_chat_id", chat_id)
    result.setdefault("requested_by_username", username)
    return result


def _target_identity(tool: str, arguments: Mapping[str, JsonValue]) -> str:
    """Build a stable, non-secret target label for confirmation state."""

    for key in (
        "tmdb_id",
        "tmdbId",
        "tvdb_id",
        "tvdbId",
        "provider_id",
        "providerId",
        "id",
    ):
        value = arguments.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return f"{tool}:{key}:{value}"
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            return f"{tool}:{key}:{value}"
    # The hash is intentionally used instead of a title/path or an arbitrary
    # model string.  It remains stable for exact arguments and safe to audit.
    return f"{tool}:args:{canonical_argument_hash(arguments)}"


def _state_fingerprint(
    tool: str, arguments: Mapping[str, JsonValue], runtime: object
) -> str:
    callback = getattr(runtime, "target_state_callback", None)
    if callable(callback):
        try:
            value = callback(tool, arguments)
        except TypeError:
            value = callback(tool=tool, arguments=arguments)
        if isinstance(value, str) and value:
            return value[:512]
    return hashlib.sha256(
        _json_bytes(
            {"tool": tool, "arguments": dict(arguments)},
            max_bytes=MAX_OPERATION_ARGUMENT_BYTES,
        )
    ).hexdigest()


def render_confirmation_preview(
    tool: str,
    arguments: Mapping[str, JsonValue],
    *,
    target_identity: str,
    state_fingerprint: str,
    policy_version: str,
) -> str:
    """Render the exact bounded bytes that Hermes must show to an admin."""

    safe_arguments = redact_json(dict(arguments), max_bytes=MAX_PREVIEW_BYTES)
    try:
        args_text = json.dumps(
            safe_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise OperationValidationError(
            "confirmation arguments are not renderable"
        ) from exc
    preview = (
        "Confirmation required\n"
        f"Tool: {tool}\n"
        f"Target: {redact_text(target_identity, max_bytes=512)}\n"
        f"Policy: {redact_text(policy_version, max_bytes=128)}\n"
        f"State: {redact_text(state_fingerprint, max_bytes=512)}\n"
        f"Arguments: {args_text}"
    )
    if len(preview.encode("utf-8", "strict")) > MAX_PREVIEW_BYTES:
        raise OperationValidationError("confirmation preview exceeds the bound")
    return preview


def _result_mapping(value: object) -> object:
    """Convert known typed result objects without passing provider objects."""

    if value is None:
        return {"ok": True}
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            return value.to_dict()
        except Exception as exc:  # noqa: BLE001
            raise OperationValidationError(
                "typed result could not be serialized"
            ) from exc
    if response_dict is not None:
        try:
            # SafePage/Page are the only generic mapping path accepted by this
            # helper.  Arbitrary mappings intentionally fall through below.
            if value.__class__.__name__ in {"SafePage", "Page"}:
                return response_dict(value)
        except Exception as exc:  # noqa: BLE001
            raise OperationValidationError(
                "typed result could not be serialized"
            ) from exc
    if is_dataclass(value) and not isinstance(value, type):
        name = value.__class__.__name__
        if name == "RequestWorkflowResult":
            raw = asdict(value)
            intent = raw.pop("intent", {})
            if isinstance(intent, Mapping):
                raw.update(
                    {
                        "request_id": intent.get("request_id"),
                        "request_key": intent.get("request_key"),
                        "media_type": intent.get("media_type"),
                        "provider_id": intent.get("provider_id"),
                        "title": intent.get("title"),
                        "year": intent.get("year"),
                        "seasons": intent.get("seasons"),
                        "mode": intent.get("mode"),
                        "status": intent.get("status"),
                    }
                )
            return raw
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return {"result": value}
    raise OperationValidationError("untyped provider result is denied")


def safe_operation_result(value: object, *, tool: str | None = None) -> JsonObject:
    """Return a bounded MCP ``result`` object from a typed handler result."""

    mapped = _result_mapping(value)
    if isinstance(mapped, Mapping) and (
        "content" in mapped
        or "structuredContent" in mapped
        or "structured_content" in mapped
    ):
        content_raw = mapped.get("content", ())
        content: list[JsonValue] = []
        if not isinstance(content_raw, (list, tuple)):
            raise OperationValidationError("typed content must be an array")
        for item in content_raw:
            if (
                not isinstance(item, Mapping)
                or item.get("type") != "text"
                or not isinstance(item.get("text"), str)
            ):
                raise OperationValidationError("typed content item is invalid")
            content.append(
                {
                    "type": "text",
                    "text": redact_text(
                        str(item["text"]), max_bytes=MAX_OPERATION_TEXT_BYTES
                    ),
                }
            )
        structured = mapped.get("structuredContent", mapped.get("structured_content"))
        result: JsonObject = {
            "content": content,
            "isError": bool(mapped.get("isError", mapped.get("is_error", False))),
        }
        if structured is not None:
            result["structuredContent"] = _bounded_json(structured)
        if tool is not None:
            result.setdefault("tool", tool)
        encoded = _json_bytes(result, max_bytes=MAX_OPERATION_RESPONSE_BYTES)
        del encoded
        return result
    safe = _bounded_json(mapped)
    if not isinstance(safe, dict):
        safe = {"result": safe}
    if tool is not None:
        safe.setdefault("tool", tool)
    _json_bytes(safe, max_bytes=MAX_OPERATION_RESPONSE_BYTES)
    return safe


def confirmation_result(
    *,
    tool: str,
    preview: str,
    expires_at: int,
    token_hash: str,
    token: str | None = None,
) -> JsonObject:
    """Build the model-visible portion of a confirmation envelope.

    The transient plaintext capability is returned only to the authenticated
    Hermes boundary, where the native extension immediately consumes it for
    Telegram delivery and bind.  The durable store retains only ``token_hash``.
    """

    if not isinstance(tool, str) or not tool:
        raise OperationValidationError("confirmation tool is invalid")
    if not isinstance(preview, str) or not preview:
        raise OperationValidationError("confirmation preview is invalid")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise OperationValidationError("confirmation expiry is invalid")
    if not isinstance(token_hash, str) or not _SHA256_RE.fullmatch(token_hash):
        raise OperationValidationError("confirmation token hash is invalid")
    if token is not None and (
        not isinstance(token, str) or not _TOKEN_RE.fullmatch(token)
    ):
        raise OperationValidationError("confirmation token is invalid")
    confirmation: JsonObject = {
        "state": "pending_bind",
        "expires_at": expires_at,
        "token_hash": token_hash,
        "callback_prefix": "crblc:",
    }
    if token is not None:
        confirmation["token"] = token
    return {
        "confirmation_required": True,
        "tool": tool,
        "preview": preview,
        "confirmation": confirmation,
    }


def _call_with_supported_arguments(
    function: Callable[..., object], *positional: object, **named: object
) -> object:
    """Call a fake/production handler while preserving a typed seam."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*positional)
    parameters = signature.parameters
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return function(*positional, **named)
    filtered = {key: value for key, value in named.items() if key in parameters}
    if positional:
        return function(*positional, **filtered)
    return function(**filtered)


async def invoke_handler(
    function: Callable[..., object],
    arguments: Mapping[str, JsonValue],
    *,
    claims: ActorClaims | None = None,
    policy: ActorPolicy | None = None,
) -> object:
    result = _call_with_supported_arguments(
        function,
        arguments,
        claims=claims,
        actor=claims,
        policy=policy,
    )
    if inspect.isawaitable(result):
        return await cast(Awaitable[object], result)
    return result


class SQLiteMutationGuard:
    """Durable one-safe-mutation-per-update guard using the existing ledger."""

    def __init__(self, database: object, *, ttl_seconds: int = 600) -> None:
        self.database = database
        self.ttl_seconds = ttl_seconds

    def claim(self, *, user_id: int, update_id: int) -> bool:
        transaction = getattr(self.database, "transaction", None)
        if not callable(transaction):
            raise OperationDependencyError("durable mutation guard is unavailable")
        now = int(time.time())
        key = f"{user_id}:{update_id}"
        if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise OperationValidationError("mutation identity is invalid")
        try:
            with transaction() as connection:
                connection.execute(
                    "DELETE FROM idempotency_keys WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),),
                )
                result = connection.execute(
                    "INSERT OR IGNORE INTO idempotency_keys(scope, key, status, expires_at) VALUES (?, ?, 'accepted', ?)",
                    (
                        "safe_mutation",
                        key,
                        time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + self.ttl_seconds)
                        ),
                    ),
                )
                return bool(result.rowcount == 1)
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError(
                "durable mutation guard is unavailable"
            ) from exc


class SQLiteRateLimiter:
    """Atomic SQLite-backed sliding-window limiter for the companion boundary.

    The existing ``idempotency_keys`` table is a durable, migration-owned
    ledger with a composite primary key.  Each accepted budget unit is a
    short-lived row in a reviewed rate scope; the write transaction prunes
    expired rows, counts every required scope, and inserts all units before it
    commits.  No process-local counter is used for production traffic.
    """

    def __init__(
        self,
        database: object,
        *,
        policy: RateLimitPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if database is None or not callable(getattr(database, "transaction", None)):
            raise OperationDependencyError(
                "durable rate limiter database is unavailable"
            )
        if policy is not None and not isinstance(policy, RateLimitPolicy):
            raise TypeError("rate limiter policy is invalid")
        if not callable(clock):
            raise TypeError("rate limiter clock is invalid")
        self.database = database
        self.policy = DEFAULT_RATE_LIMIT_POLICY if policy is None else policy
        self.clock = clock

    @staticmethod
    def _operation(operation: str | RateOperation) -> RateOperation:
        if isinstance(operation, RateOperation):
            return operation
        if not isinstance(operation, str):
            raise ValueError("rate-limited operation is invalid")
        try:
            return RateOperation(operation.strip().lower().replace("-", "_"))
        except ValueError as exc:
            raise ValueError("rate-limited operation is invalid") from exc

    @staticmethod
    def _ids(
        *,
        user_id: int | None,
        chat_id: int | None,
        actor_user_id: int | None,
        actor_chat_id: int | None,
    ) -> tuple[int | None, int | None]:
        if user_id is None:
            user_id = actor_user_id
        elif actor_user_id is not None and user_id != actor_user_id:
            raise ValueError("conflicting rate limiter user IDs")
        if chat_id is None:
            chat_id = actor_chat_id
        elif actor_chat_id is not None and chat_id != actor_chat_id:
            raise ValueError("conflicting rate limiter chat IDs")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        ):
            raise ValueError("rate limiter user ID is invalid")
        if chat_id is not None and (
            isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0
        ):
            raise ValueError("rate limiter chat ID is invalid")
        return user_id, chat_id

    @staticmethod
    def _timestamp(value: float) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("rate limiter clock is invalid")
        current = int(value)
        if current < 0:
            raise ValueError("rate limiter clock is invalid")
        return current

    @staticmethod
    def _retry_after(earliest: str | None, *, now: int, fallback: int) -> float:
        """Convert the oldest ledger expiry into a bounded retry delay.

        SQLite stores ledger timestamps as canonical UTC text.  Deriving the
        delay from that value keeps a rejected request from being told to wait
        a full window when its oldest budget unit is about to expire.
        Malformed rows are treated conservatively as a full window; the
        malformed value itself never crosses the HTTP boundary.
        """

        if not earliest:
            return float(fallback)
        try:
            from datetime import datetime, timezone

            text = earliest[:-1] + "+00:00" if earliest.endswith("Z") else earliest
            expires = datetime.fromisoformat(text)
            if expires.tzinfo is None or expires.utcoffset() is None:
                return float(fallback)
            delay = expires.astimezone(timezone.utc).timestamp() - now
            return max(0.0, min(float(fallback), delay))
        except (TypeError, ValueError, OverflowError):
            return float(fallback)

    @staticmethod
    def _iso(value: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))

    def _decision(
        self,
        operation: RateOperation,
        *,
        user_id: int | None,
        chat_id: int | None,
        now: int,
        consume: bool,
    ) -> RateLimitDecision:
        limits = self.policy.limits(operation)
        scopes: list[tuple[str, str, int, int]] = []
        for scope, (limit, window) in limits.items():
            if scope == "user":
                if user_id is None:
                    raise ValueError("rate limiter user ID is required")
                value = str(user_id)
            elif scope == "chat":
                if chat_id is None:
                    raise ValueError("rate limiter chat ID is required")
                value = str(chat_id)
            else:
                value = "global"
            scopes.append((scope, value, limit, window))

        expiration = self._iso(now)
        try:
            with cast(Any, self.database).transaction() as connection:
                connection.execute(
                    "DELETE FROM idempotency_keys WHERE scope LIKE 'rate:%' "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (expiration,),
                )
                counts: dict[str, tuple[int, str | None]] = {}
                blocked: tuple[str, int, int, float] | None = None
                for scope, value, limit, window in scopes:
                    ledger_scope = f"rate:{operation.value}:{scope}:{value}"
                    row = connection.execute(
                        "SELECT COUNT(*), MIN(expires_at) FROM idempotency_keys "
                        "WHERE scope = ? AND status = 'accepted'",
                        (ledger_scope,),
                    ).fetchone()
                    count = int(row[0]) if row is not None else 0
                    earliest = None if row is None or row[1] is None else str(row[1])
                    counts[scope] = (count, earliest)
                    if count >= limit and blocked is None:
                        blocked = (
                            scope,
                            limit,
                            window,
                            self._retry_after(earliest, now=now, fallback=window),
                        )
                if blocked is not None:
                    scope, limit, window, retry_after = blocked
                    return RateLimitDecision(
                        False,
                        operation,
                        0,
                        retry_after,
                        scope,
                        limit,
                        window,
                    )
                if consume:
                    for scope, value, _limit, window in scopes:
                        ledger_scope = f"rate:{operation.value}:{scope}:{value}"
                        connection.execute(
                            "INSERT INTO idempotency_keys(scope, key, status, expires_at) "
                            "VALUES (?, ?, 'accepted', ?)",
                            (
                                ledger_scope,
                                secrets.token_urlsafe(18),
                                self._iso(now + window),
                            ),
                        )
                remaining = min(
                    limit - counts[scope][0] - (1 if consume else 0)
                    for scope, _value, limit, _window in scopes
                )
                global_limit, global_window = limits.get(
                    "global", next(iter(limits.values()))
                )
                return RateLimitDecision(
                    True,
                    operation,
                    max(0, remaining),
                    0.0,
                    None,
                    global_limit,
                    global_window,
                )
        except OperationDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError(
                "durable rate limiter is unavailable"
            ) from exc

    def consume(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        normalized = self._operation(operation)
        user, chat = self._ids(
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
        )
        current = self._timestamp(self.clock() if now is None else now)
        return self._decision(
            normalized,
            user_id=user,
            chat_id=chat,
            now=current,
            consume=True,
        )

    def check(
        self,
        operation: str | RateOperation,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        now: float | None = None,
    ) -> RateLimitDecision:
        normalized = self._operation(operation)
        user, chat = self._ids(
            user_id=user_id,
            chat_id=chat_id,
            actor_user_id=actor_user_id,
            actor_chat_id=actor_chat_id,
        )
        current = self._timestamp(self.clock() if now is None else now)
        return self._decision(
            normalized,
            user_id=user,
            chat_id=chat,
            now=current,
            consume=False,
        )

    def enforce(
        self, operation: str | RateOperation, **kwargs: object
    ) -> RateLimitDecision:
        decision = self.consume(operation, **cast(Any, kwargs))
        if not decision.allowed:
            raise RateLimitExceeded(
                operation=decision.operation,
                scope=decision.blocked_scope,
                retry_after=decision.retry_after,
            )
        return decision

    def allow(self, operation: str | RateOperation, **kwargs: object) -> bool:
        return self.consume(operation, **cast(Any, kwargs)).allowed

    try_consume = allow

    def cleanup(self, *, now: float | None = None) -> int:
        current = self._timestamp(self.clock() if now is None else now)
        try:
            with cast(Any, self.database).transaction() as connection:
                result = connection.execute(
                    "DELETE FROM idempotency_keys WHERE scope LIKE 'rate:%' "
                    "AND expires_at IS NOT NULL AND expires_at <= ?",
                    (self._iso(current),),
                )
                return int(result.rowcount)
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError(
                "durable rate limiter is unavailable"
            ) from exc


class CompanionRuntime:
    """Dependency-injected runtime consumed by :func:`media_companion.app.create_app`.

    ``production=True`` is the secure default.  It rejects the two in-memory
    stores shipped for unit tests and requires an injected policy and durable
    replay path.  Tests can use :meth:`for_testing` or pass ``production=False``
    explicitly; that choice is never inferred from the environment.
    """

    def __init__(
        self,
        *,
        actor_verifier: ActorAssertionVerifier | None = None,
        verifier: ActorAssertionVerifier | None = None,
        confirmation_store: ConfirmationTokenStore | None = None,
        nonce_store: object | None = None,
        policy: object | None = None,
        policy_helper: object | None = None,
        safe_handlers: Mapping[str, Callable[..., object]] | None = None,
        shared_handlers: Mapping[str, Callable[..., object]] | None = None,
        upstream: object | None = None,
        confirmation_executor: Callable[..., object] | None = None,
        confirmation_bridge: Callable[..., object] | None = None,
        confirmation_delivery_owner: str | None = None,
        confirmation_arguments_store: object | None = None,
        confirmation_arguments_resolver: Callable[..., object] | None = None,
        bind_authorizer: Callable[..., object] | None = None,
        helper_key: bytes | str | None = None,
        confirmation_key: bytes | str | None = None,
        dashboard_api_key: bytes | str | None = None,
        dashboard_handlers: Mapping[str, Callable[..., object]] | None = None,
        dashboard_identity_resolver: Callable[..., object] | None = None,
        dashboard_identity: Callable[..., object] | None = None,
        dashboard_policy_recheck: Callable[..., object] | None = None,
        dashboard_policy: Callable[..., object] | None = None,
        dashboard_mutation_guard: Callable[..., object] | None = None,
        dashboard_cas_guard: Callable[..., object] | None = None,
        dashboard_confirmation_guard: Callable[..., object] | None = None,
        dashboard_confirmation_validator: Callable[..., object] | None = None,
        dashboard_confirmation_issuer: Callable[..., object] | None = None,
        dashboard_confirmation_minter: Callable[..., object] | None = None,
        operations: object | None = None,
        database: object | None = None,
        mutation_guard: object | None = None,
        rate_limiter: object | None = None,
        request_rate_limiter: object | None = None,
        target_state_callback: Callable[..., object] | None = None,
        event_inbox: object | None = None,
        persist_event: Callable[..., object] | None = None,
        plex_capability: object | None = None,
        plex_webhook_capability: object | None = None,
        loopback_checker: Callable[..., object] | None = None,
        require_loopback_client: bool = True,
        expected_server_uuid: str | None = None,
        allowed_server_uuids: Sequence[str] = (),
        allowed_library_ids: Sequence[str] = (),
        allowed_library_names: Sequence[str] = (),
        plex_rate_limiter: object | None = None,
        plex_limiter: object | None = None,
        trust_capability_bound_ingress: bool | None = None,
        trusted_ingress_peers: Sequence[str] = (),
        revalidate_confirmation: Callable[..., object] | None = None,
        migrations_ready: Callable[..., object] | bool | None = None,
        worker: object | None = None,
        worker_lifecycle: Callable[..., object] | None = None,
        readiness: Callable[..., object] | None = None,
        production: bool = True,
        policy_version: str = POLICY_VERSION_DEFAULT,
    ) -> None:
        self.actor_verifier = actor_verifier or verifier
        self.confirmation_store = confirmation_store
        self.nonce_store = nonce_store
        self.policy = policy or policy_helper
        self.safe_handlers = dict(safe_handlers or shared_handlers or {})
        self.upstream = upstream
        self.confirmation_executor = confirmation_executor
        self.confirmation_bridge = confirmation_bridge
        # Hermes owns native Telegram preview delivery in production.  A
        # companion-owned bridge remains available only as an explicitly
        # selected test/deployment variant; the boundary never invokes both.
        self.confirmation_delivery_owner = confirmation_delivery_owner or (
            "hermes" if production else "companion"
        )
        self.confirmation_arguments_store = confirmation_arguments_store
        self.confirmation_arguments_resolver = confirmation_arguments_resolver
        self.bind_authorizer = bind_authorizer
        self.helper_key = helper_key or confirmation_key
        self.dashboard_api_key = dashboard_api_key
        self.dashboard_handlers = dict(dashboard_handlers or {})
        self.dashboard_identity_resolver = (
            dashboard_identity_resolver or dashboard_identity
        )
        self.dashboard_policy_recheck = dashboard_policy_recheck or dashboard_policy
        self.dashboard_mutation_guard = dashboard_mutation_guard or dashboard_cas_guard
        self.dashboard_confirmation_guard = (
            dashboard_confirmation_guard or dashboard_confirmation_validator
        )
        self.dashboard_confirmation_issuer = (
            dashboard_confirmation_issuer or dashboard_confirmation_minter
        )
        self.operations = operations
        self.database = database
        self.mutation_guard = mutation_guard or (
            SQLiteMutationGuard(database) if database is not None else None
        )
        if rate_limiter is None:
            rate_limiter = request_rate_limiter
        if rate_limiter is None and not production:
            rate_limiter = InMemoryRateLimiter()
        self.rate_limiter = rate_limiter
        self.target_state_callback = target_state_callback
        self.event_inbox = event_inbox
        self.persist_event_callback = persist_event
        self.plex_capability = plex_capability or plex_webhook_capability
        self.loopback_checker = loopback_checker
        self.require_loopback_client = bool(require_loopback_client)
        self.expected_server_uuid = expected_server_uuid
        self.allowed_server_uuids = tuple(allowed_server_uuids)
        self.allowed_library_ids = tuple(allowed_library_ids)
        self.allowed_library_names = tuple(allowed_library_names)
        supplied_plex_limiter = plex_rate_limiter or plex_limiter
        self.plex_rate_limiter = (
            supplied_plex_limiter
            if supplied_plex_limiter is not None
            else WebhookRateLimiter()
            if not production
            else None
        )
        self.trust_capability_bound_ingress = (
            False
            if trust_capability_bound_ingress is None
            else bool(trust_capability_bound_ingress)
        )
        self.trusted_ingress_peers = tuple(trusted_ingress_peers)
        self.revalidate_confirmation = revalidate_confirmation
        self.migrations_ready = migrations_ready
        self.worker = worker
        self.worker_lifecycle = worker_lifecycle
        self._worker_lifespan_users = 0
        self._worker_started = False
        self._worker_stop_callback: Callable[..., object] | None = None
        self._worker_task: object | None = None
        self.readiness_callback = readiness
        self.production = bool(production)
        self.policy_version = policy_version
        self._migrations_checked = False
        if self.nonce_store is None and self.actor_verifier is not None:
            self.nonce_store = getattr(self.actor_verifier, "nonce_store", None)
        if self.confirmation_store is None:
            self.confirmation_store = getattr(self, "confirmation_store", None)
        if self.production:
            self.validate_production()

    @classmethod
    def for_testing(cls, **kwargs: object) -> "CompanionRuntime":
        kwargs["production"] = False
        return cls(**cast(dict[str, Any], kwargs))

    def validate_production(self) -> None:
        if self.actor_verifier is None or not isinstance(
            self.actor_verifier, ActorAssertionVerifier
        ):
            raise DurableStoreRequiredError("actor verifier is required")
        actor_keys = getattr(self.actor_verifier, "keys", None)
        if (
            not isinstance(actor_keys, Mapping)
            or not actor_keys
            or any(_secret_key(value) is None for value in actor_keys.values())
        ):
            raise OperationValidationError("actor signing key is invalid")
        nonce = self.nonce_store or getattr(self.actor_verifier, "nonce_store", None)
        if nonce is None or isinstance(nonce, InMemoryNonceReplayStore):
            raise DurableStoreRequiredError("production requires a durable nonce store")
        if self.confirmation_store is None or isinstance(
            self.confirmation_store, InMemoryConfirmationTokenStore
        ):
            raise DurableStoreRequiredError(
                "production requires a durable confirmation store"
            )
        if self.policy is None:
            raise OperationDependencyError("current policy helper is required")
        authorize = getattr(self.policy, "authorize", None)
        membership = getattr(self.policy, "membership", None)
        if not callable(authorize) and not callable(membership):
            raise OperationDependencyError("current policy helper is unavailable")

        # Production startup is intentionally strict.  The in-memory auth
        # stores are useful for unit tests, but all cross-request state in a
        # live companion must have SQLite durability and an applied migration
        # set before the process can be considered ready.
        database = self.database
        if database is None:
            raise DurableStoreRequiredError(
                "production requires a durable SQLite database"
            )
        database_name = getattr(database, "database", getattr(database, "path", None))
        if isinstance(database_name, str) and (
            database_name == ":memory:"
            or database_name.startswith("file::memory:")
            or "mode=memory" in database_name
        ):
            raise DurableStoreRequiredError(
                "production rejects an in-memory SQLite database"
            )
        if not callable(getattr(database, "transaction", None)) or not callable(
            getattr(database, "connect", None)
        ):
            raise OperationDependencyError("durable SQLite database is unavailable")
        if not self._migrations_checked:
            self._check_migrations(database)

        if set(self.safe_handlers) != set(SHARED_TOOL_SET) or any(
            not callable(handler) for handler in self.safe_handlers.values()
        ):
            raise OperationDependencyError(
                "exact shared tool dispatcher is unavailable"
            )
        proxy = self.upstream
        call_tool = getattr(proxy, "call_tool", None) if proxy is not None else None
        if not callable(call_tool):
            call_tool = getattr(proxy, "invoke", None) if proxy is not None else None
        if not callable(call_tool):
            raise OperationDependencyError("pinned upstream broker is unavailable")
        inventory = None
        if proxy is not None:
            inventory = getattr(proxy, "registered_tools", None)
            if inventory is None:
                inventory = getattr(proxy, "tool_names", None)
        if inventory is None:
            list_tools = (
                getattr(proxy, "list_tools", None) if proxy is not None else None
            )
            if callable(list_tools):
                inventory = _call_with_supported_arguments(
                    cast(Callable[..., object], list_tools)
                )
        if (
            inspect.isawaitable(inventory)
            or inventory is None
            or isinstance(inventory, (str, bytes))
        ):
            raise OperationDependencyError(
                "upstream broker inventory is not the pinned 102-tool set"
            )
        try:
            inventory_set = set(cast(Iterable[object], inventory))
        except TypeError as exc:
            raise OperationDependencyError(
                "upstream broker inventory is not the pinned 102-tool set"
            ) from exc
        if inventory_set != set(UPSTREAM_TOOL_SET):
            raise OperationDependencyError(
                "upstream broker inventory is not the pinned 102-tool set"
            )

        guard = self.mutation_guard
        if (
            guard is None
            or not callable(getattr(guard, "claim", None))
            and not callable(getattr(guard, "claim_once", None))
        ):
            raise OperationDependencyError("durable safe-mutation guard is unavailable")
        if self.confirmation_delivery_owner not in {"hermes", "companion"}:
            raise OperationValidationError("confirmation delivery owner is invalid")
        if self.confirmation_delivery_owner == "companion" and (
            self.confirmation_bridge is None or not callable(self.confirmation_bridge)
        ):
            raise OperationDependencyError("private confirmation bridge is unavailable")
        argument_store = self.confirmation_arguments_store
        if argument_store is None and (
            self.production or not callable(self.confirmation_arguments_resolver)
        ):
            raise OperationDependencyError(
                "durable confirmation argument binding is unavailable"
            )
        if argument_store is not None:
            put = getattr(argument_store, "put", None)
            consume = getattr(argument_store, "consume", None)
            if not callable(put) or not callable(consume):
                raise OperationDependencyError(
                    "durable confirmation argument binding is unavailable"
                )
            # A production resolver/store must expose a durable backing
            # object.  This rejects dicts and ad-hoc process-local fakes while
            # allowing the checked-in SQLite adapter and reviewed wrappers.
            backing = getattr(argument_store, "database", None)
            if backing is None:
                backing = getattr(argument_store, "connection", None)
            if backing is None:
                raise DurableStoreRequiredError(
                    "production confirmation arguments must be durable"
                )
        if self.confirmation_executor is None and not callable(
            getattr(self.operations, "execute_confirmation", None)
        ):
            raise OperationDependencyError(
                "private confirmation executor is unavailable"
            )
        if not callable(self.revalidate_confirmation):
            raise OperationDependencyError("confirmation state recheck is unavailable")
        if _secret_key(self.helper_key or b"") is None:
            raise OperationValidationError("private confirmation helper key is invalid")
        dashboard_key = _secret_key(self.dashboard_api_key or b"")
        if dashboard_key is None:
            raise OperationValidationError("dashboard API key is invalid")
        if any(
            hmac.compare_digest(dashboard_key, cast(bytes, _secret_key(value)))
            for value in actor_keys.values()
        ):
            raise OperationValidationError("dashboard API key must be separate")
        self._validate_dashboard_handlers()
        if not callable(self.dashboard_identity_resolver):
            raise OperationDependencyError("dashboard identity resolver is unavailable")
        if not callable(self.dashboard_policy_recheck):
            raise OperationDependencyError("dashboard policy recheck is unavailable")
        if not callable(self.dashboard_mutation_guard):
            raise OperationDependencyError("dashboard CAS/admin guard is unavailable")
        if not callable(self.dashboard_confirmation_guard):
            raise OperationDependencyError(
                "dashboard confirmation guard is unavailable"
            )
        if not callable(self.dashboard_confirmation_issuer):
            raise OperationDependencyError(
                "dashboard confirmation issuer is unavailable"
            )
        if not callable(self.target_state_callback):
            raise OperationDependencyError(
                "confirmation target state provider is unavailable"
            )
        limiter = self.rate_limiter
        if limiter is None or isinstance(limiter, InMemoryRateLimiter):
            raise DurableStoreRequiredError(
                "production requires a durable rate limiter"
            )
        limiter_methods = [
            getattr(limiter, name, None)
            for name in ("enforce", "consume", "allow", "try_consume")
        ]
        limiter_methods = [method for method in limiter_methods if callable(method)]
        operation_aware = False
        for method in limiter_methods:
            try:
                parameters = inspect.signature(
                    cast(Callable[..., object], method)
                ).parameters.values()
            except (TypeError, ValueError):
                continue
            if any(parameter.name == "operation" for parameter in parameters) or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                operation_aware = True
                break
        if not operation_aware:
            raise OperationDependencyError("rate limiter is unavailable")

        capability = self.plex_capability
        if isinstance(capability, bytes):
            try:
                capability = capability.decode("ascii", "strict")
            except UnicodeDecodeError as exc:
                raise OperationValidationError(
                    "Plex ingress capability is invalid"
                ) from exc
        if not isinstance(capability, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", capability
        ):
            raise OperationDependencyError("Plex ingress capability is unavailable")
        if not (self.expected_server_uuid or self.allowed_server_uuids):
            raise OperationDependencyError("Plex server allowlist is unavailable")
        if not (self.allowed_library_ids or self.allowed_library_names):
            raise OperationDependencyError("Plex library allowlist is unavailable")
        if self.expected_server_uuid is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.expected_server_uuid
        ):
            raise OperationValidationError("Plex server identity is invalid")
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
            for value in self.allowed_server_uuids
        ) or len(set(self.allowed_server_uuids)) != len(self.allowed_server_uuids):
            raise OperationValidationError("Plex server allowlist is invalid")
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value)
            for value in self.allowed_library_ids
        ) or len(set(self.allowed_library_ids)) != len(self.allowed_library_ids):
            raise OperationValidationError("Plex library allowlist is invalid")
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8", "strict")) > 256
            or any(ord(character) < 0x20 for character in value)
            for value in self.allowed_library_names
        ) or len(set(self.allowed_library_names)) != len(self.allowed_library_names):
            raise OperationValidationError("Plex library-name allowlist is invalid")
        if (
            self.persist_event_callback is None
            and self.event_inbox is None
            and not callable(getattr(self.database, "transaction", None))
        ):
            raise OperationDependencyError(
                "durable Plex event persistence is unavailable"
            )
        if not callable(
            getattr(self.plex_rate_limiter, "allow", None)
        ) and not callable(getattr(self.plex_rate_limiter, "consume", None)):
            raise OperationDependencyError("Plex ingress rate limiter is unavailable")
        # The path capability is an application credential, not a substitute
        # for the host-loopback/network boundary.  A production listener must
        # either have an explicit trusted-peer list (for the Docker DNAT
        # gateway) or an injected checker that validates the dedicated proxy
        # contract.  Without one, any container on the bridge could reach the
        # route if it guessed or obtained the capability.
        if self.loopback_checker is None and not self.trusted_ingress_peers:
            raise OperationDependencyError(
                "Plex ingress network boundary is unavailable"
            )
        if self.trusted_ingress_peers:
            import ipaddress

            try:
                peers = tuple(
                    str(ipaddress.ip_address(peer))
                    for peer in self.trusted_ingress_peers
                )
            except (TypeError, ValueError) as exc:
                raise OperationDependencyError(
                    "Plex ingress peer allowlist is invalid"
                ) from exc
            if peers != self.trusted_ingress_peers or len(set(peers)) != len(peers):
                raise OperationDependencyError("Plex ingress peer allowlist is invalid")
        if not self._worker_configured():
            raise OperationDependencyError("one worker lifecycle is not configured")

    def _check_migrations(self, database: object) -> None:
        """Apply/check the configured ordered migration set exactly at startup."""

        configured = self.migrations_ready
        if configured is not None:
            result = configured() if callable(configured) else configured
            if inspect.isawaitable(result) or result is False:
                raise OperationDependencyError("SQLite migrations are not ready")
            self._migrations_checked = True
            return
        migrate = getattr(database, "migrate", None)
        if not callable(migrate):
            raise OperationDependencyError("SQLite migration runner is unavailable")
        try:
            report = _call_with_supported_arguments(
                cast(Callable[..., object], migrate)
            )
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError("SQLite migrations are not ready") from exc
        if inspect.isawaitable(report) or report is False:
            raise OperationDependencyError("SQLite migrations are not ready")
        self._migrations_checked = True

    def _validate_dashboard_handlers(self) -> None:
        for operation in DASHBOARD_OPERATION_SET:
            handler = self.dashboard_handlers.get(operation)
            if handler is None and self.operations is not None:
                handler = getattr(self.operations, operation.replace(".", "_"), None)
                if handler is None:
                    handler = getattr(self.operations, operation, None)
            if not callable(handler):
                raise OperationDependencyError(
                    "typed dashboard operations are incomplete"
                )

    def _worker_configured(self) -> bool:
        if callable(self.worker_lifecycle):
            return True
        worker = self.worker
        if worker is None:
            return False
        start = callable(getattr(worker, "start", None))
        stop = any(
            callable(getattr(worker, name, None))
            for name in ("stop", "shutdown", "close")
        )
        run = any(
            callable(getattr(worker, name, None))
            for name in ("run", "run_forever", "serve")
        )
        return (start and stop) or (run and stop)

    def worker_ready(self) -> bool:
        worker = self.worker
        if worker is None:
            return callable(self.worker_lifecycle)
        task = self._worker_task
        if task is not None and bool(getattr(task, "done", lambda: False)()):
            return False
        checker = getattr(worker, "ready", None)
        if checker is None:
            checker = getattr(worker, "is_ready", None)
        if checker is None:
            return True
        try:
            result = (
                _call_with_supported_arguments(cast(Callable[..., object], checker))
                if callable(checker)
                else checker
            )
        except Exception:
            return False
        return not inspect.isawaitable(result) and bool(result)

    async def start_worker(self) -> None:
        """Start the single injected worker at most once per process.

        The same ASGI object may be mounted on the MCP and loopback listeners,
        so lifespan startup can be observed twice.  A small reference count
        keeps those listeners from creating duplicate delivery workers.
        """

        self._worker_lifespan_users += 1
        if self._worker_started:
            return
        target = (
            getattr(self.worker, "start", None) if self.worker is not None else None
        )
        if not callable(target):
            target = self.worker_lifecycle
        run_target: object | None = None
        if not callable(target) and self.worker is not None:
            for name in ("run", "run_forever", "serve"):
                candidate = getattr(self.worker, name, None)
                if callable(candidate):
                    run_target = candidate
                    break
        if not callable(target) and run_target is None:
            self._worker_lifespan_users -= 1
            raise OperationDependencyError("one worker lifecycle is not configured")
        try:
            if run_target is not None:
                import asyncio

                async def run_worker() -> None:
                    result = _call_with_supported_arguments(
                        cast(Callable[..., object], run_target)
                    )
                    if inspect.isawaitable(result):
                        await cast(Awaitable[object], result)

                self._worker_task = asyncio.create_task(run_worker())
            else:
                result = _call_with_supported_arguments(
                    cast(Callable[..., object], target)
                )
                if inspect.isawaitable(result):
                    result = await cast(Awaitable[object], result)
                if callable(result):
                    self._worker_stop_callback = cast(Callable[..., object], result)
            self._worker_started = True
        except Exception:
            self._worker_lifespan_users -= 1
            raise

    async def stop_worker(self) -> None:
        if self._worker_lifespan_users <= 0:
            return
        self._worker_lifespan_users -= 1
        if self._worker_lifespan_users != 0 or not self._worker_started:
            return
        target = self._worker_stop_callback
        if target is None and self.worker is not None:
            for name in ("stop", "shutdown", "close"):
                candidate = getattr(self.worker, name, None)
                if callable(candidate):
                    target = cast(Callable[..., object], candidate)
                    break
        try:
            if target is not None:
                result = _call_with_supported_arguments(target)
                if inspect.isawaitable(result):
                    await cast(Awaitable[object], result)
            task = self._worker_task
            if task is not None:
                import asyncio

                try:
                    await asyncio.wait_for(cast(Any, task), timeout=5.0)
                except asyncio.TimeoutError:
                    cast(Any, task).cancel()
                    await asyncio.gather(cast(Any, task), return_exceptions=True)
                finally:
                    self._worker_task = None
        finally:
            self._worker_started = False
            self._worker_stop_callback = None

    def nonce_replay_store(self) -> object:
        value = self.nonce_store or getattr(self.actor_verifier, "nonce_store", None)
        if value is None:
            raise OperationDependencyError("nonce replay store is unavailable")
        return value

    def current_policy(
        self, claims: ActorClaims, *, require_admin: bool
    ) -> ActorPolicy:
        provider = self.policy
        if provider is None:
            raise OperationDependencyError("current policy is unavailable")
        try:
            authorize = getattr(provider, "authorize", None)
            if callable(authorize):
                value = _call_with_supported_arguments(
                    authorize,
                    user_id=claims.user_id,
                    chat_id=claims.chat_id,
                    require_admin=require_admin,
                )
            else:
                membership = getattr(provider, "membership", None)
                if not callable(membership):
                    raise OperationDependencyError("current policy is unavailable")
                value = _call_with_supported_arguments(
                    membership,
                    user_id=claims.user_id,
                    chat_id=claims.chat_id,
                )
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError("current policy is unavailable") from exc
        policy = _policy_from(value, user_id=claims.user_id, chat_id=claims.chat_id)
        if policy.user_id != claims.user_id or policy.chat_id != claims.chat_id:
            raise OperationBoundaryError("actor identity is not current")
        if not policy.allowed or policy.role not in {"user", "admin"}:
            raise OperationBoundaryError("actor is not allowed")
        if require_admin and not policy.is_admin:
            raise OperationBoundaryError("administrator role is required")
        if claims.role != policy.role:
            raise OperationBoundaryError("actor role is stale")
        if claims.allowlist_fingerprint is None or not hmac.compare_digest(
            claims.allowlist_fingerprint, policy.fingerprint
        ):
            raise OperationBoundaryError("actor policy is stale")
        if (
            claims.allowlist_version is not None
            and policy.version
            and claims.allowlist_version != policy.version
        ):
            raise OperationBoundaryError("actor policy version is stale")
        if require_admin and (
            claims.chat_type != "private" or claims.update_type != "message"
        ):
            raise OperationBoundaryError(
                "administrator actions require a private message"
            )
        if (
            not require_admin
            and claims.tool in {"request_movie", "request_series"}
            and claims.update_type != "message"
        ):
            raise OperationBoundaryError("request mutations require a normal message")
        return policy

    def claim_safe_mutation(self, claims: ActorClaims) -> None:
        guard = self.mutation_guard
        if guard is None:
            raise OperationDependencyError("durable mutation guard is unavailable")
        claim = getattr(guard, "claim", None)
        if not callable(claim):
            claim = getattr(guard, "claim_once", None)
        if not callable(claim):
            raise OperationDependencyError("durable mutation guard is unavailable")
        try:
            accepted = _call_with_supported_arguments(
                claim, user_id=claims.user_id, update_id=claims.update_id
            )
        except Exception as exc:  # noqa: BLE001
            raise OperationDependencyError(
                "durable mutation guard is unavailable"
            ) from exc
        if inspect.isawaitable(accepted):
            raise OperationDependencyError(
                "async mutation guards require an application adapter"
            )
        if accepted is False:
            raise MutationAlreadyClaimed(
                "safe mutation already accepted for this update"
            )

    def target_state_fingerprint(
        self, tool: str, arguments: Mapping[str, JsonValue]
    ) -> str:
        return _state_fingerprint(tool, arguments, self)

    def persist_event(self, event: object) -> bool:
        callback = self.persist_event_callback
        if callback is not None:
            result = _call_with_supported_arguments(callback, event)
            if inspect.isawaitable(result):
                raise OperationDependencyError(
                    "event persistence must complete before acknowledgement"
                )
            return result is not False
        inbox = self.event_inbox
        if inbox is not None:
            for name in (
                "persist_event",
                "insert_event",
                "record_event",
                "store_event",
            ):
                callback = getattr(inbox, name, None)
                if callable(callback):
                    result = _call_with_supported_arguments(callback, event)
                    if inspect.isawaitable(result):
                        raise OperationDependencyError(
                            "event persistence must complete before acknowledgement"
                        )
                    return result is not False
        if self.database is not None and hasattr(event, "to_record"):
            record = event.to_record()
            transaction = getattr(self.database, "transaction", None)
            if not callable(transaction):
                return False
            try:
                with transaction() as connection:
                    connection.execute(
                        """INSERT OR IGNORE INTO event_inbox
                           (event_key, source, event_type, server_uuid, library_uuid,
                            rating_key, tombstone_generation, payload_hash,
                            sanitized_payload_json)
                           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                        (
                            record["event_key"],
                            record["source"],
                            record["event_type"],
                            record["server_uuid"],
                            record["library_uuid"],
                            record["rating_key"],
                            record["payload_hash"],
                            record["sanitized_payload_json"],
                        ),
                    )
                return True
            except Exception:
                return False
        return False

    def ready(self) -> bool:
        if self.production:
            try:
                self.validate_production()
            except Exception:
                return False
            if not self.worker_ready():
                return False
        callback = self.readiness_callback
        if callback is not None:
            try:
                result = _call_with_supported_arguments(callback)
                if isinstance(result, Mapping):
                    return bool(result.get("ready", False))
                return bool(result)
            except Exception:
                return False
        return True


def _secret_key(value: bytes | str | object) -> bytes | None:
    if isinstance(value, str):
        value = value.encode("utf-8", "strict")
    if not isinstance(value, bytes) or len(value) < 32:
        return None
    return value


def is_durable_runtime(runtime: CompanionRuntime) -> bool:
    """Return whether both replay/capability stores are durable adapters."""

    nonce = runtime.nonce_store or getattr(runtime.actor_verifier, "nonce_store", None)
    return (
        nonce is not None
        and not isinstance(nonce, InMemoryNonceReplayStore)
        and runtime.confirmation_store is not None
        and not isinstance(runtime.confirmation_store, InMemoryConfirmationTokenStore)
    )


__all__ = [
    "ActorPolicy",
    "CompanionRuntime",
    "CONFIRMATION_AUDIENCE",
    "CONFIRMATION_TOOL",
    "ConfirmationArgumentsStore",
    "DASHBOARD_OPERATION_SET",
    "DashboardConfirmationGuard",
    "DashboardConfirmationIssuer",
    "DashboardIdentityResolver",
    "DashboardMutationGuard",
    "DashboardPolicyRechecker",
    "DurableStoreRequiredError",
    "EventInbox",
    "JsonObject",
    "JsonValue",
    "MCP_AUDIENCE",
    "MAX_DASHBOARD_BODY_BYTES",
    "MAX_DASHBOARD_RESPONSE_BYTES",
    "MAX_OPERATION_ARGUMENT_BYTES",
    "MAX_OPERATION_RESPONSE_BYTES",
    "MAX_PREVIEW_BYTES",
    "MutationAlreadyClaimed",
    "OperationBoundaryError",
    "OperationDependencyError",
    "OperationValidationError",
    "SAFE_RESULT_KEYS",
    "SQLiteMutationGuard",
    "SQLiteRateLimiter",
    "confirmation_result",
    "invoke_handler",
    "is_durable_runtime",
    "render_confirmation_preview",
    "safe_operation_result",
]
