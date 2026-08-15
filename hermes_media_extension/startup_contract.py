"""Fail-closed external startup/readiness contract for the Hermes override.

Hermes plugin loading intentionally logs registration failures and continues
gateway startup.  That behavior is useful for optional plugins but unsafe for
this trust boundary.  The deployment entrypoint therefore runs this module
after plugin discovery and before ``hermes gateway``.  It verifies ownership of
the native Telegram registry entry, the adapter/callback bindings, the frozen
wrapper inventory, pinned Hermes/native source identity, polling-only config,
and mounted actor/helper configuration.

The checker accepts explicit fake modules/registries/configuration so focused
tests do not need Hermes or python-telegram-bot installed.  The shell entrypoint
uses the default real-module path and exits non-zero on any missing evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .companion_client import (
    TOOL_INVENTORY,
    TOOL_POLICY_AVAILABLE,
)
from .plugin import (
    ADMIN_TOOLSET,
    CALLBACK_PREFIX,
    HERMES_RELEASE_DATE,
    NATIVE_ADAPTER_CLASS,
    NATIVE_ADAPTER_MODULE,
    PLATFORM_NAME,
    PLUGIN_NAME,
    SHARED_TOOLSET,
    TOOL_SCHEMA_SHA256,
    frozen_tool_contract_digest,
    load_frozen_tool_schemas,
)
from .policy_helper_api import PolicyHelperAPI

EXPECTED_HERMES_RELEASE_DATE = HERMES_RELEASE_DATE
EXPECTED_PLUGIN_MANIFEST_NAME = "telegram-platform"
EXPECTED_NATIVE_ADAPTER_SHA256 = (
    "9fb01bf3069abbce0f93ad7ae06d215143be8c308a7cf7017a893d49b36b6641"
)
EXPECTED_PLATFORM_REGISTRY_SHA256 = (
    "22950c20edac9be90840e5ded37f932d29b25fd0f8d20c2b73d6efd86240d1cf"
)
EXPECTED_TOOL_INVENTORY = TOOL_INVENTORY
EXPECTED_CALLBACK_PREFIX = CALLBACK_PREFIX
DEFAULT_PLUGIN_DIR = Path("/opt/hermes/plugins/platforms/media-policy")
DEFAULT_KEY_ENV_NAMES = ("CRBL_ACTOR_SIGNING_KEY_FILE", "ACTOR_SIGNING_KEY_FILE")
DEFAULT_HELPER_KEY_ENV_NAMES = (
    "CRBL_POLICY_HELPER_KEY_FILE",
    "POLICY_HELPER_KEY_FILE",
)
DEFAULT_COMPANION_URL_ENV_NAMES = (
    "CRBL_COMPANION_URL",
    "MEDIA_COMPANION_URL",
    "COMPANION_URL",
)
DEFAULT_HELPER_URL_ENV_NAMES = ("CRBL_POLICY_HELPER_URL", "POLICY_HELPER_URL")
DEFAULT_POLICY_FILE = "/opt/data/.env"
EXPECTED_POLICY_HELPER_ROUTES = (
    "/v1/policy/membership",
    "/v1/policy/current-users",
    "/v1/policy/notify-admin",
    "/v1/policy/blocked-contacts",
    "/v1/policy/resolve-identity",
    "/v1/policy/allowlist/mutate",
    "/v1/policy/status",
)
EXPECTED_NOTIFICATION_METHOD = "send_notification"


class StartupContractError(RuntimeError):
    """Raised when the immutable Hermes integration evidence is incomplete."""

    def __init__(self, report: StartupContractReport) -> None:
        self.report = report
        message = "Hermes media-policy startup contract failed: " + "; ".join(
            report.errors
        )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StartupContractReport:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checks: Mapping[str, bool] = field(default_factory=dict)

    @property
    def failures(self) -> tuple[str, ...]:
        return self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
        }


@dataclass(frozen=True, slots=True)
class ContractInputs:
    """Optional fake injection/overrides for tests and image validation."""

    registry: object | None = None
    native_module: object | None = None
    platform_registry_module: object | None = None
    hermes_module: object | None = None
    tool_names: tuple[str, ...] | None = None
    config: Mapping[str, Any] | None = None
    env: Mapping[str, str] | None = None
    plugin_dir: Path | None = None
    actor_key_file: Path | None = None
    helper_key_file: Path | None = None
    companion_url: str | None = None
    helper_url: str | None = None
    native_source_sha256: str | None = None
    registry_source_sha256: str | None = None
    visibility_patch: bool | None = None


def _module(name: str, supplied: object | None) -> object | None:
    if supplied is not None:
        return supplied
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None


def _discover_platform_plugins() -> bool:
    """Load Hermes plugins before inspecting its last-writer registries.

    Hermes v2026.8.3 defers platform plugin imports until a registry lookup.
    The external preflight must force that discovery in its own process; a
    plain import of ``gateway.platform_registry`` would otherwise inspect an
    empty registry and falsely report that the override is absent.
    """

    try:
        plugins = importlib.import_module("hermes_cli.plugins")
        discover = getattr(plugins, "discover_plugins", None)
        if not callable(discover):
            return False
        discover()
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_attr(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first_env(env: Mapping[str, str], names: Sequence[str]) -> str:
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _contains_nonempty(mapping: object, keys: set[str], *, _depth: int = 0) -> bool:
    if _depth > 8:
        return False
    if isinstance(mapping, Mapping):
        for key, value in mapping.items():
            normalized = str(key).strip().lower()
            if normalized in keys and isinstance(value, str) and value.strip():
                return True
            if isinstance(value, Mapping) and _contains_nonempty(
                value, keys, _depth=_depth + 1
            ):
                return True
            if isinstance(value, list) and _contains_nonempty(
                value, keys, _depth=_depth + 1
            ):
                return True
    elif isinstance(mapping, list):
        return any(
            _contains_nonempty(item, keys, _depth=_depth + 1) for item in mapping
        )
    return False


def _module_source_sha256(module: object) -> str | None:
    declared = getattr(module, "__hermes_source_sha256__", None)
    if isinstance(declared, str) and declared:
        return declared
    source_file = getattr(module, "__file__", None)
    if not isinstance(source_file, str) or not source_file:
        return None
    try:
        digest = hashlib.sha256(Path(source_file).read_bytes()).hexdigest()
    except OSError:
        return None
    return digest


def _entry_registry(
    registry_module: object | None, explicit: object | None
) -> object | None:
    if explicit is not None:
        return explicit
    return _read_attr(registry_module, "platform_registry")


def _entry_for(registry: object, name: str) -> object | None:
    getter = getattr(registry, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:  # noqa: BLE001
            return None
    entries = _read_attr(registry, "_entries", {})
    if isinstance(entries, Mapping):
        return entries.get(name)
    return None


def _tool_inventory_from_registry() -> tuple[str, ...] | None:
    try:
        module = importlib.import_module("tools.registry")
    except Exception:  # noqa: BLE001
        return None
    registry = _read_attr(module, "registry")
    getter = getattr(registry, "get_tool_names_for_toolset", None)
    if callable(getter):
        try:
            shared = getter(SHARED_TOOLSET)
            admin = getter(ADMIN_TOOLSET)
        except Exception:  # noqa: BLE001
            return None
        if (
            isinstance(shared, (list, tuple))
            and isinstance(admin, (list, tuple))
            and all(isinstance(name, str) for name in (*shared, *admin))
        ):
            return tuple(shared) + tuple(admin)
    return None


def _inventory_matches(value: Sequence[str] | None) -> bool:
    """Compare the frozen names independent of Hermes registry sort order."""

    if value is None:
        return False
    names = tuple(value)
    return len(names) == len(set(names)) and tuple(sorted(names)) == tuple(
        sorted(EXPECTED_TOOL_INVENTORY)
    )


def _key_is_safe(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    if info.st_size <= 0 or info.st_size > 16 * 1024:
        return False
    # The actor key is mounted as a private file.  Group/other permissions or
    # a symlink would make the trust boundary ambiguous.
    return not path.is_symlink() and not stat.S_IMODE(info.st_mode) & 0o077


_POLICY_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>.*?)[ \t]*$"
)


def _policy_assignment_present(path: Path, variable: str) -> bool:
    """Check one non-secret assignment without returning its value.

    The pinned image loads ``$HERMES_HOME/.env`` in the gateway process, but
    s6 cont-init and helper probes run before that process has imported dotenv.
    Startup therefore checks the canonical file itself for the token marker;
    no token bytes enter the report, logs, or process environment.
    """

    try:
        info = path.stat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size <= 0
            or info.st_size > 1024 * 1024
        ):
            return False
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError):
        return False
    matches = 0
    present = False
    for line in lines:
        match = _POLICY_ASSIGNMENT_RE.fullmatch(line)
        if match is None or match.group("name") != variable:
            continue
        matches += 1
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] in {"'", '"'}:
            if value[-1] != value[0]:
                return False
            value = value[1:-1].strip()
        if value and not any(ord(character) < 0x20 for character in value):
            present = True
    return matches == 1 and present


def _config_url(value: str | None, *, field: str) -> bool:
    if not value:
        return False
    # Keep the field keyword in the helper's public diagnostic call sites so
    # future checks can report which endpoint failed, while validating the
    # URL uniformly here.
    _ = field
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        # Accessing ``port`` rejects malformed/out-of-range ports that would
        # otherwise remain hidden behind ``urlsplit``'s lazy validation.
        _ = parsed.port
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _pinned_helper_url(value: str | None) -> bool:
    if not _config_url(value, field="policy helper") or value is None:
        return False
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == 8787
            and parsed.path in {"", "/"}
        )
    except ValueError:
        return False


def check_startup_contract(
    inputs: ContractInputs | None = None, **overrides: Any
) -> StartupContractReport:
    """Check all external startup evidence and return a safe report."""

    supplied = inputs or ContractInputs()
    if overrides:
        values = {
            field_name: getattr(supplied, field_name)
            for field_name in supplied.__dataclass_fields__
        }
        values.update({key: value for key, value in overrides.items() if key in values})
        supplied = ContractInputs(**values)
    env = dict(os.environ if supplied.env is None else supplied.env)
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    def check(name: str, condition: bool, message: str) -> None:
        checks[name] = bool(condition)
        if not condition:
            errors.append(message)

    if (
        supplied.registry is None
        and supplied.platform_registry_module is None
        and supplied.native_module is None
    ):
        _discover_platform_plugins()

    native = _module(NATIVE_ADAPTER_MODULE, supplied.native_module)
    check(
        "native_adapter_import",
        native is not None,
        "native Telegram adapter is unavailable",
    )
    native_class = (
        _read_attr(native, NATIVE_ADAPTER_CLASS) if native is not None else None
    )
    check(
        "native_adapter_class",
        isinstance(native_class, type),
        "pinned native Telegram adapter class is unavailable",
    )
    check(
        "native_adapter_module",
        getattr(native_class, "__module__", None) == NATIVE_ADAPTER_MODULE,
        "Telegram adapter module drifted from the pinned native module",
    )

    native_digest = supplied.native_source_sha256 or (
        _module_source_sha256(native) if native is not None else None
    )
    check(
        "native_source",
        native_digest == EXPECTED_NATIVE_ADAPTER_SHA256,
        "pinned native Telegram adapter source drifted",
    )
    registry_module = _module(
        "gateway.platform_registry", supplied.platform_registry_module
    )
    registry_digest = supplied.registry_source_sha256 or (
        _module_source_sha256(registry_module) if registry_module is not None else None
    )
    check(
        "registry_source",
        registry_digest == EXPECTED_PLATFORM_REGISTRY_SHA256,
        "pinned Hermes platform registry source drifted",
    )

    registry = _entry_registry(registry_module, supplied.registry)
    entry = _entry_for(registry, PLATFORM_NAME) if registry is not None else None
    check(
        "platform_registry_entry",
        entry is not None,
        "Telegram platform registry entry is missing",
    )
    check(
        "platform_owner",
        _read_attr(entry, "plugin_name", "") == PLUGIN_NAME,
        "media-policy plugin does not own Telegram registry entry",
    )
    check(
        "platform_source",
        _read_attr(entry, "source", "") == "plugin",
        "Telegram registry entry is not a plugin override",
    )
    factory = _read_attr(entry, "adapter_factory")
    check(
        "platform_factory",
        callable(factory),
        "media-policy Telegram adapter factory is missing",
    )
    check(
        "factory_owner",
        getattr(factory, "__module__", "") == "hermes_media_extension.plugin",
        "Telegram factory is not owned by the media-policy extension",
    )
    check(
        "factory_class",
        getattr(factory, "__name__", "") == "_build_adapter",
        "media-policy Telegram factory binding drifted",
    )

    from .confirmation_callback import ConfirmationCallbackHandler

    check(
        "callback_prefix",
        ConfirmationCallbackHandler.prefix == EXPECTED_CALLBACK_PREFIX,
        "confirmation callback prefix drifted",
    )
    from .plugin import MediaPolicyTelegramAdapter

    check(
        "callback_binding",
        "_handle_callback_query" in MediaPolicyTelegramAdapter.__dict__
        and "handle_confirmation_callback" in MediaPolicyTelegramAdapter.__dict__,
        "native callback binding is missing",
    )
    check(
        "callback_handler",
        callable(getattr(ConfirmationCallbackHandler, "handle_update", None)),
        "confirmation callback handler is missing",
    )

    registered_tools = supplied.tool_names or _tool_inventory_from_registry()
    check(
        "tool_inventory",
        _inventory_matches(registered_tools),
        "typed companion tool inventory is missing or drifted",
    )
    check(
        "tool_policy_module",
        TOOL_POLICY_AVAILABLE,
        "canonical media_companion.tool_policy is unavailable",
    )
    check(
        "policy_helper_routes",
        tuple(PolicyHelperAPI.ROUTES.values()) == EXPECTED_POLICY_HELPER_ROUTES,
        "policy-helper route inventory drifted",
    )
    check(
        "policy_helper_notification_method",
        callable(getattr(PolicyHelperAPI, EXPECTED_NOTIFICATION_METHOD, None)),
        "policy-helper notification method binding drifted",
    )
    schemas_ok = False
    schemas: dict[str, dict[str, Any]] = {}
    try:
        schemas = load_frozen_tool_schemas()
        schemas_ok = (
            set(schemas) == set(EXPECTED_TOOL_INVENTORY)
            and frozen_tool_contract_digest() == "sha256:" + TOOL_SCHEMA_SHA256
            and all(
                isinstance(schema, Mapping)
                and schema.get("type") == "object"
                and isinstance(schema.get("properties"), Mapping)
                and schema.get("additionalProperties") is False
                for schema in schemas.values()
            )
        )
    except Exception:  # noqa: BLE001
        schemas_ok = False
    check(
        "tool_schemas",
        schemas_ok,
        "checked-in per-tool schemas are missing, generic, or drifted",
    )
    candidate_flow_ok = False
    try:
        movie_schema = schemas["request_movie"]
        series_schema = schemas["request_series"]
        movie_properties = movie_schema.get("properties")
        series_properties = series_schema.get("properties")
        movie_required = movie_schema.get("required")
        series_required = series_schema.get("required")
        candidate_flow_ok = (
            isinstance(movie_properties, Mapping)
            and isinstance(series_properties, Mapping)
            and isinstance(movie_required, list)
            and isinstance(series_required, list)
            and movie_required == ["candidate_handle"]
            and set(series_required) == {"candidate_handle", "seasons"}
            and "candidate_handle" in movie_properties
            and "candidate_handle" in series_properties
            and "tmdbId" not in movie_properties
            and "tvdbId" not in series_properties
        )
    except (KeyError, TypeError):
        candidate_flow_ok = False
    check(
        "candidate_handle_flow",
        candidate_flow_ok,
        "request tools must consume only actor-bound candidate handles",
    )
    tool_profile = str(env.get("TOOL_PROFILE", "") or "").strip()
    include_raw = str(env.get("TOOL_INCLUDE", "") or "").strip()
    include = tuple(item.strip() for item in include_raw.split(",") if item.strip())
    check(
        "tool_profile",
        tool_profile == "full",
        "pinned upstream TOOL_PROFILE must remain full",
    )
    check(
        "tool_include",
        include == ("tmdb_get_movie_credits", "tmdb_get_tv_credits"),
        "pinned upstream TOOL_INCLUDE must add the two omitted TMDB credit tools",
    )
    if registered_tools is None:
        warnings.append(
            "tool registry could not be inspected; real entrypoint treats this as a failure"
        )

    visibility_patch = supplied.visibility_patch
    if visibility_patch is None:
        try:
            from .plugin import _native_tool_visibility_patch_installed

            visibility_patch = _native_tool_visibility_patch_installed()
        except Exception:  # noqa: BLE001
            visibility_patch = False
    check(
        "role_aware_tool_visibility",
        visibility_patch is True,
        "Telegram role-aware toolset visibility patch is missing",
    )

    hermes = _module("hermes_cli", supplied.hermes_module)
    release = _read_attr(hermes, "__release_date__") if hermes is not None else None
    check(
        "hermes_release",
        str(release or "") == EXPECTED_HERMES_RELEASE_DATE,
        "Hermes release is not the pinned v2026.8.3 image",
    )

    # Long polling is mandatory.  Check both the native env name and nested
    # config forms used by Hermes setup/config loaders.
    webhook_env = str(env.get("TELEGRAM_WEBHOOK_URL", "") or "").strip()
    config = supplied.config or {}
    webhook_config = _contains_nonempty(config, {"telegram_webhook_url", "webhook_url"})
    check(
        "telegram_polling_only",
        not webhook_env and not webhook_config,
        "Telegram webhook URL must be empty; use native long polling",
    )

    actor_key = supplied.actor_key_file
    if actor_key is None:
        raw_key = _first_env(env, DEFAULT_KEY_ENV_NAMES)
        actor_key = Path(raw_key) if raw_key else None
    check(
        "actor_key_reference",
        actor_key is not None,
        "actor signing key file is not configured",
    )
    check(
        "actor_key_permissions",
        actor_key is not None and _key_is_safe(actor_key),
        "actor signing key file is missing or not private",
    )
    check(
        "no_inline_actor_key",
        not any(
            str(env.get(name, "") or "").strip()
            for name in ("CRBL_ACTOR_SIGNING_KEY", "ACTOR_SIGNING_KEY")
        ),
        "inline actor signing key is not permitted",
    )

    helper_key = supplied.helper_key_file
    if helper_key is None:
        raw_helper_key = _first_env(env, DEFAULT_HELPER_KEY_ENV_NAMES)
        helper_key = Path(raw_helper_key) if raw_helper_key else None
    check(
        "policy_helper_key_reference",
        helper_key is not None,
        "policy-helper signing key file is not configured",
    )
    check(
        "policy_helper_key_permissions",
        helper_key is not None and _key_is_safe(helper_key),
        "policy-helper signing key file is missing or not private",
    )
    check(
        "no_inline_policy_helper_key",
        not any(
            str(env.get(name, "") or "").strip()
            for name in ("CRBL_POLICY_HELPER_KEY", "POLICY_HELPER_KEY")
        ),
        "inline policy-helper signing key is not permitted",
    )

    companion_url = supplied.companion_url or _first_env(
        env, DEFAULT_COMPANION_URL_ENV_NAMES
    )
    helper_url = supplied.helper_url or _first_env(env, DEFAULT_HELPER_URL_ENV_NAMES)
    check(
        "companion_url",
        _config_url(companion_url, field="companion"),
        "typed companion URL is not configured",
    )
    check(
        "policy_helper_url",
        _pinned_helper_url(helper_url),
        "Hermes policy-helper URL must be http://127.0.0.1:8787; only the companion uses the hermes-media DNS route",
    )
    helper_port = str(env.get("CRBL_POLICY_HELPER_PORT", "") or "").strip()
    check(
        "policy_helper_port",
        helper_port == "8787",
        "policy-helper port must remain the pinned private port 8787",
    )
    helper_bind_host = str(env.get("CRBL_POLICY_HELPER_HOST", "") or "").strip()
    check(
        "policy_helper_bind_host",
        helper_bind_host == "0.0.0.0",
        "policy-helper must bind 0.0.0.0 on the private Docker networks",
    )
    policy_file = str(env.get("CRBL_POLICY_FILE", "") or "").strip()
    check(
        "policy_file_reference",
        policy_file == DEFAULT_POLICY_FILE,
        "canonical policy file must be /opt/data/.env",
    )
    check(
        "telegram_token_reference",
        bool(str(env.get("TELEGRAM_BOT_TOKEN", "") or "").strip())
        or _policy_assignment_present(Path(policy_file), "TELEGRAM_BOT_TOKEN"),
        "Telegram bot token is not configured",
    )

    plugin_dir = supplied.plugin_dir or DEFAULT_PLUGIN_DIR
    manifest = plugin_dir / "plugin.yaml"
    shim = plugin_dir / "__init__.py"
    check(
        "plugin_manifest", manifest.is_file(), "media-policy plugin manifest is missing"
    )
    check("plugin_shim", shim.is_file(), "media-policy plugin shim is missing")
    if manifest.is_file():
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            text = ""
        check(
            "plugin_kind",
            any(line.strip() == "kind: platform" for line in text.splitlines()),
            "media-policy manifest is not kind: platform",
        )
        check(
            "plugin_name",
            any(
                line.strip() == f"name: {EXPECTED_PLUGIN_MANIFEST_NAME}"
                for line in text.splitlines()
            ),
            "media-policy manifest platform lookup name drifted",
        )

    return StartupContractReport(
        ok=not errors, errors=tuple(errors), warnings=tuple(warnings), checks=checks
    )


def validate_startup_contract(*args: Any, **kwargs: Any) -> StartupContractReport:
    return check_startup_contract(*args, **kwargs)


def assert_startup_contract(*args: Any, **kwargs: Any) -> StartupContractReport:
    report = check_startup_contract(*args, **kwargs)
    if not report.ok:
        raise StartupContractError(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the Hermes media-policy startup contract"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="emit a machine-readable report",
    )
    args = parser.parse_args(argv)
    report = check_startup_contract()
    if args.as_json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        if report.ok:
            print("Hermes media-policy startup contract: ok")
        else:
            print("Hermes media-policy startup contract: failed", file=sys.stderr)
            for error in report.errors:
                print(f"- {error}", file=sys.stderr)
    return 0 if report.ok else 78


if __name__ == "__main__":  # pragma: no cover - exercised by deployment shell.
    raise SystemExit(main())


__all__ = [
    "CALLBACK_PREFIX",
    "DEFAULT_PLUGIN_DIR",
    "DEFAULT_POLICY_FILE",
    "EXPECTED_HERMES_RELEASE_DATE",
    "EXPECTED_PLUGIN_MANIFEST_NAME",
    "EXPECTED_NATIVE_ADAPTER_SHA256",
    "EXPECTED_NOTIFICATION_METHOD",
    "EXPECTED_PLATFORM_REGISTRY_SHA256",
    "EXPECTED_TOOL_INVENTORY",
    "ContractInputs",
    "StartupContractError",
    "StartupContractReport",
    "assert_startup_contract",
    "check_startup_contract",
    "main",
    "validate_startup_contract",
]
