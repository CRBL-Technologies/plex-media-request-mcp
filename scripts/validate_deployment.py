#!/usr/bin/env python3
"""Deterministic validation for the immutable media-request deployment.

This module validates checked-in Compose/manifest *templates* after applying a
small, non-shell Compose-style variable substitution.  It deliberately does
not invoke Docker, resolve DNS, read secrets, contact Portainer, or mutate a
container.  Callers can import the helpers from tests or use the CLI before a
release gate.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE = ROOT / "deployment" / "compose.yaml"
DEFAULT_MANIFEST = ROOT / "deployment" / "manifest.yaml"

_VARIABLE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<operator>:-|:\?|\?|-)"  # noqa: E501
    r"(?P<argument>[^}]*))?\}"
)
_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_INLINE_SECRET_NAME = re.compile(
    r"(?:TOKEN|API_KEY|PASSWORD|SECRET|SIGNING_KEY|AUTH_KEY)$", re.IGNORECASE
)
_PLEX_LIFECYCLE_COMMAND = re.compile(
    r"(?is)\b(?:docker(?:[- ]compose)?|compose)\s+"
    r"(?:up|down|start|stop|restart|recreate|rm|remove|kill|replace|deploy|run|exec)\b"
    r"[^\n]{0,256}\bplex(?:[-_.:/\s]|$)"
)
_SERVICE_NAMES = frozenset(
    {
        "media-server-mcp",
        "media-companion",
        "hermes-media",
        "media-request-dashboard",
    }
)
_NETWORK_NAMES = frozenset(
    {
        "media-bot",
        "media-mcp-backend",
        "media-service-egress",
        "media-dashboard-backend",
        "media",
    }
)
_PRIVATE_NETWORKS = frozenset(_NETWORK_NAMES - {"media"})
_MEDIA_BOT_SUBNET = "172.30.40.0/24"
_MEDIA_BOT_GATEWAY = "172.30.40.1"
_EXPECTED_MEMBERSHIPS: dict[str, frozenset[str]] = {
    "hermes-media": frozenset({"media-bot"}),
    "media-companion": frozenset(
        {
            "media-bot",
            "media-mcp-backend",
            "media-service-egress",
            "media-dashboard-backend",
        }
    ),
    "media-server-mcp": frozenset({"media-mcp-backend", "media-service-egress"}),
    "media-request-dashboard": frozenset({"media-dashboard-backend", "media"}),
}
_SECRET_BASENAMES = frozenset(
    {
        ".env",
        "upstream.env",
        "companion.env",
        "actor-signing.key",
        "dashboard-auth.env",
        "dashboard-api.key",
    }
)


class DeploymentValidationError(ValueError):
    """A checked-in deployment contract is invalid."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Stable result object useful to tests and release wrappers."""

    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise DeploymentValidationError("; ".join(self.errors))


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Load a simple dotenv file without expanding or printing its values.

    The validator accepts ordinary ``KEY=value`` release files and ignores
    comments/blank lines.  It does not read any canonical secret file: release
    metadata must be supplied separately and only variable names are surfaced
    in validation errors.
    """

    values: dict[str, str] = {}
    source = Path(path)
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentValidationError(
            "release environment file is unavailable"
        ) from exc
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DeploymentValidationError(
                f"release environment line {line_number} is not KEY=value"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise DeploymentValidationError(
                f"release environment line {line_number} has an invalid key"
            )
        values[key] = value.strip().strip("\"'")
    return values


def render_template(text: str, env: Mapping[str, str]) -> str:
    """Apply the bounded Compose variable forms used by deployment assets.

    Supported forms are ``${NAME}``, ``${NAME:-default}``,
    ``${NAME-default}``, ``${NAME:?message}``, and ``${NAME?message}``.
    Shell command/parameter expansion is intentionally not supported.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        operator = match.group("operator")
        argument = match.group("argument") or ""
        value = env.get(name, "")
        present = bool(value)
        if operator in {":?", "?"}:
            if (operator == ":?" and not present) or (
                operator == "?" and name not in env
            ):
                raise DeploymentValidationError(
                    f"required release variable is missing: {name}"
                )
            return str(value)
        if operator in {":-", "-"} and (
            (operator == ":-" and not present) or (operator == "-" and name not in env)
        ):
            return argument
        return str(value)

    return _VARIABLE.sub(replace, text)


def _load_yaml(
    path: Path, env: Mapping[str, str]
) -> tuple[dict[str, Any] | None, list[str], str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, [f"deployment file is unavailable: {path.name}"], ""
    try:
        rendered = render_template(raw, env)
    except DeploymentValidationError as exc:
        return None, [str(exc)], ""
    try:
        document = yaml.safe_load(rendered)
    except yaml.YAMLError:
        return None, [f"deployment YAML is invalid: {path.name}"], rendered
    if not isinstance(document, dict):
        return None, [f"deployment YAML root must be an object: {path.name}"], rendered
    return document, [], rendered


def _string_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _network_names(value: object) -> frozenset[str]:
    if isinstance(value, Mapping):
        return frozenset(str(name) for name in value)
    return frozenset(_string_list(value))


def _walk_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        result: list[str] = []
        for key, item in value.items():
            result.extend(_walk_strings(key))
            result.extend(_walk_strings(item))
        return tuple(result)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result = []
        for item in value:
            result.extend(_walk_strings(item))
        return tuple(result)
    return ()


def _contains_plex_lifecycle_command(value: object) -> bool:
    """Detect a rendered Docker/Compose command that targets Plex.

    Joining scalar strings also catches YAML list-form commands such as
    ``[docker, restart, plex]`` while leaving ordinary documentation and
    secret-file variable names alone.
    """

    return bool(_PLEX_LIFECYCLE_COMMAND.search(" ".join(_walk_strings(value))))


def _port_strings(service: Mapping[str, Any]) -> tuple[str, ...]:
    values = service.get("ports", ())
    return _string_list(values)


def _mounts(service: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Split Linux Compose short-syntax mounts into source/target/mode."""

    result: list[tuple[str, str, str]] = []
    for item in _string_list(service.get("volumes")):
        parts = item.rsplit(":", 2)
        if len(parts) == 2:
            source, target = parts
            mode = ""
        elif len(parts) == 3:
            source, target, mode = parts
        else:
            continue
        result.append((source, target, mode))
    return tuple(result)


def _require_mount(
    service_name: str,
    service: Mapping[str, Any],
    *,
    target: str,
    mode: str,
    source_basename: str | None,
    errors: list[str],
) -> None:
    matches = [mount for mount in _mounts(service) if mount[1] == target]
    if len(matches) != 1:
        errors.append(f"service {service_name} must mount exactly one {target}")
        return
    source, _target, actual_mode = matches[0]
    if actual_mode != mode:
        errors.append(f"service {service_name} mount {target} must use mode {mode}")
    if source_basename is not None and Path(source).name != source_basename:
        errors.append(
            f"service {service_name} mount {target} must come from {source_basename}"
        )


def _has_healthcheck(service: Mapping[str, Any]) -> bool:
    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, Mapping):
        return False
    test = healthcheck.get("test")
    if isinstance(test, str):
        return test.strip().upper() != "NONE"
    return bool(_string_list(test)) and all(
        isinstance(part, str) and part.strip() for part in _string_list(test)
    )


def validate_compose_document(document: Mapping[str, Any]) -> ValidationResult:
    """Validate service hardening, exposure, and network membership."""

    errors: list[str] = []
    document_strings = _walk_strings(document)
    if any("docker.sock" in item.lower() for item in document_strings):
        errors.append("Docker socket mounts are forbidden")
    if _contains_plex_lifecycle_command(document):
        errors.append("Plex container lifecycle commands are forbidden in Compose")
    services = document.get("services")
    if not isinstance(services, Mapping):
        return ValidationResult(("Compose services must be an object",))
    names = frozenset(str(name) for name in services)
    if names != _SERVICE_NAMES:
        missing = sorted(_SERVICE_NAMES - names)
        extra = sorted(names - _SERVICE_NAMES)
        if missing:
            errors.append("Compose is missing required services: " + ",".join(missing))
        if extra:
            errors.append("Compose has unreviewed services: " + ",".join(extra))
    if any("plex" in name.lower() for name in names):
        errors.append("Compose must not define a Plex service")

    networks = document.get("networks")
    if (
        not isinstance(networks, Mapping)
        or frozenset(str(name) for name in networks) != _NETWORK_NAMES
    ):
        errors.append(
            "Compose must define exactly the four private networks and external media"
        )
    else:
        for name in _PRIVATE_NETWORKS:
            config = networks.get(name)
            if not isinstance(config, Mapping) or config.get("driver") != "bridge":
                errors.append(f"network {name} must use the bridge driver")
            if (
                name in {"media-mcp-backend", "media-dashboard-backend"}
                and config.get("internal") is not True
            ):
                errors.append(f"network {name} must be internal")
            if (
                name in {"media-bot", "media-service-egress"}
                and config.get("internal") is True
            ):
                errors.append(f"{name} must permit configured outbound APIs")
            if name == "media-bot":
                ipam = config.get("ipam")
                ipam_configs = ipam.get("config") if isinstance(ipam, Mapping) else None
                expected_ipam = [
                    {"subnet": _MEDIA_BOT_SUBNET, "gateway": _MEDIA_BOT_GATEWAY}
                ]
                if ipam_configs != expected_ipam:
                    errors.append(
                        "media-bot must use the reviewed fixed subnet/gateway"
                    )
        media = networks.get("media")
        if not isinstance(media, Mapping) or media.get("external") is not True:
            errors.append("media must be the existing external Docker network")

    for service_name in _SERVICE_NAMES:
        service = services.get(service_name)
        if not isinstance(service, Mapping):
            errors.append(f"service {service_name} must be an object")
            continue
        image = service.get("image")
        if not isinstance(image, str) or not _DIGEST_IMAGE.fullmatch(image):
            errors.append(f"service {service_name} must use an immutable image digest")
        if service_name == "hermes-media":
            if "user" in service or service.get("init") is not None:
                errors.append(
                    "Hermes must preserve its native s6 PID 1 without Compose user/init wrappers"
                )
            if "entrypoint" in service:
                errors.append(
                    "Hermes must preserve the derived image's native entrypoint dispatcher"
                )
            if service.get("command") != ["gateway", "run"]:
                errors.append(
                    "Hermes must run the native gateway command as the /init main program"
                )
        elif service.get("user") != "1000:1000":
            errors.append(f"service {service_name} must run as UID/GID 1000")
        if service.get("read_only") is not True:
            errors.append(
                f"service {service_name} must use a read-only root filesystem"
            )
        environment = service.get("environment")
        if (
            not isinstance(environment, Mapping)
            or str(environment.get("UMASK", "")) != "0077"
        ):
            errors.append(f"service {service_name} must set restrictive UMASK=0077")
        elif any(
            isinstance(key, str)
            and _INLINE_SECRET_NAME.search(key) is not None
            and not key.endswith("_FILE")
            and str(value).strip()
            for key, value in environment.items()
        ):
            errors.append(
                f"service {service_name} must not contain inline secret values"
            )
        capabilities = {item.upper() for item in _string_list(service.get("cap_drop"))}
        if "ALL" not in capabilities:
            errors.append(f"service {service_name} must drop all capabilities")
        security = set(_string_list(service.get("security_opt")))
        if "no-new-privileges:true" not in security:
            errors.append(f"service {service_name} must set no-new-privileges")
        tmpfs = _string_list(service.get("tmpfs"))
        if not tmpfs or not any("/tmp" in item for item in tmpfs):
            errors.append(f"service {service_name} must provide a bounded /tmp tmpfs")
        if not _has_healthcheck(service):
            errors.append(f"service {service_name} must define a healthcheck")
        all_strings = _walk_strings(service)
        if service.get("privileged") is True:
            errors.append(f"service {service_name} must not run privileged")
        if service.get("network_mode") in {"host", "none"}:
            errors.append(f"service {service_name} must use declared bridge networks")
        if any("docker.sock" in item.lower() for item in all_strings):
            errors.append("Docker socket mounts are forbidden")
        if _contains_plex_lifecycle_command(service):
            errors.append("Plex container lifecycle commands are forbidden in Compose")
        if any("9119" in item for item in all_strings):
            errors.append("Hermes dashboard port 9119 is forbidden")
        if any("plex" in item.lower() and "://" in item for item in all_strings):
            # Configured Plex URLs are allowed in a secret env file, but never
            # inline in this stack definition.
            errors.append(
                "Plex endpoint values must stay in canonical secret/config files"
            )

        membership = _network_names(service.get("networks"))
        expected = _EXPECTED_MEMBERSHIPS[service_name]
        if membership != expected:
            errors.append(
                f"service {service_name} network membership must be "
                + ",".join(sorted(expected))
            )

        ports = _port_strings(service)
        if service_name == "media-companion":
            if (
                not isinstance(environment, Mapping)
                or environment.get("MEDIA_COMPANION_PLEX_TRUSTED_PEERS")
                != _MEDIA_BOT_GATEWAY
            ):
                errors.append(
                    "companion must pin the Plex ingress peer to the media-bot gateway"
                )
            if ports != ("127.0.0.1:18081:18080",):
                errors.append("companion must publish only loopback port 18081")
        elif service_name == "media-request-dashboard":
            if ports != ("18082:18082",):
                errors.append("dashboard must publish only port 18082")
        elif ports:
            errors.append(f"service {service_name} must not publish host ports")

        env_file = _string_list(service.get("env_file"))
        expected_env_file = {
            "media-server-mcp": "upstream.env",
            "media-companion": "companion.env",
            "media-request-dashboard": "dashboard-auth.env",
        }.get(service_name)
        if expected_env_file is None:
            if env_file:
                errors.append(
                    "Hermes must load its native policy file from /opt/data/.env"
                )
        elif len(env_file) != 1 or Path(env_file[0]).name != expected_env_file:
            errors.append(
                f"service {service_name} must use only canonical {expected_env_file}"
            )

        if service_name == "media-server-mcp" and _mounts(service):
            errors.append(
                "upstream must not mount companion/Hermes state or credentials"
            )
        elif service_name == "media-companion":
            if (
                not isinstance(environment, Mapping)
                or environment.get("MEDIA_COMPANION_PROVIDER_ENV_FILE")
                != "/run/media-secrets/upstream.env"
            ):
                errors.append(
                    "companion must select provider URLs from its mounted upstream.env"
                )
            _require_mount(
                service_name,
                service,
                target="/opt/data/state",
                mode="rw",
                source_basename="state",
                errors=errors,
            )
            for target, basename in (
                ("/run/media-secrets/upstream.env", "upstream.env"),
                ("/run/media-secrets/companion.env", "companion.env"),
                ("/run/media-secrets/actor-signing.key", "actor-signing.key"),
                ("/run/media-secrets/dashboard-api.key", "dashboard-api.key"),
            ):
                _require_mount(
                    service_name,
                    service,
                    target=target,
                    mode="ro",
                    source_basename=basename,
                    errors=errors,
                )
            if any(
                target == "/run/media-secrets/hermes.env" or Path(source).name == ".env"
                for source, target, _ in _mounts(service)
            ):
                errors.append("companion must never mount Hermes .env files")
        elif service_name == "hermes-media":
            _require_mount(
                service_name,
                service,
                target="/opt/data",
                mode="rw",
                source_basename="data",
                errors=errors,
            )
            _require_mount(
                service_name,
                service,
                target="/run/media-secrets/actor-signing.key",
                mode="ro",
                source_basename="actor-signing.key",
                errors=errors,
            )
        elif service_name == "media-request-dashboard":
            _require_mount(
                service_name,
                service,
                target="/run/media-secrets/dashboard-api.key",
                mode="ro",
                source_basename="dashboard-api.key",
                errors=errors,
            )
            if any(
                target == "/run/media-secrets/hermes.env" or Path(source).name == ".env"
                for source, target, _ in _mounts(service)
            ):
                errors.append("dashboard must never mount Hermes .env files")

    # Hermes' dashboard is disabled both as an explicit environment setting and
    # as a topology invariant (there is no 9119 expose/port).
    hermes = services.get("hermes-media")
    if isinstance(hermes, Mapping):
        environment = hermes.get("environment")
        if (
            not isinstance(environment, Mapping)
            or str(environment.get("HERMES_DASHBOARD", "")).lower() != "false"
        ):
            errors.append("Hermes must set HERMES_DASHBOARD=false")
        if any("9119" in item for item in _walk_strings(hermes)):
            errors.append("Hermes must not expose/listen on port 9119")
        if _string_list(hermes.get("expose")) != ("8787",):
            errors.append("Hermes policy helper must expose only internal port 8787")
        if not any(
            item.split(":", 1)[0] == "/run"
            for item in _string_list(hermes.get("tmpfs"))
        ):
            errors.append("Hermes must provide a bounded /run tmpfs for s6 state")
        if (
            not isinstance(environment, Mapping)
            or str(environment.get("HERMES_UID", "")) != "1000"
            or str(environment.get("HERMES_GID", "")) != "1000"
        ):
            errors.append("Hermes must remap its native workers to UID/GID 1000")
        healthcheck_text = " ".join(_walk_strings(hermes.get("healthcheck", {})))
        if "policy-helper-healthcheck.sh" not in healthcheck_text:
            errors.append(
                "Hermes healthcheck must verify the native s6 helper/gateway contract"
            )

    return ValidationResult(tuple(dict.fromkeys(errors)))


def _validate_digest(value: object, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _DIGEST_IMAGE.fullmatch(value):
        errors.append(f"manifest {field} must be an immutable image digest")


def _validate_hash(value: object, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        errors.append(f"manifest {field} must be a lowercase SHA-256 digest")


def _validate_commit(value: object, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        errors.append(f"manifest {field} must be a full immutable commit")


def validate_manifest_document(document: Mapping[str, Any]) -> ValidationResult:
    """Validate immutable provenance and explicit no-Plex-change invariants."""

    errors: list[str] = []
    if any("docker.sock" in item.lower() for item in _walk_strings(document)):
        errors.append("Docker socket access is forbidden in the release manifest")
    if _contains_plex_lifecycle_command(document):
        errors.append(
            "Plex container lifecycle commands are forbidden in the release manifest"
        )
    if document.get("schema_version") != 1:
        errors.append("manifest schema_version must be 1")
    release = document.get("release")
    if not isinstance(release, Mapping) or release.get("immutable") is not True:
        errors.append("manifest must mark the release immutable")
    else:
        for key in ("compose_sha256", "config_sha256", "migration_checksums_sha256"):
            _validate_hash(release.get(key), f"release.{key}", errors)

    images = document.get("images")
    if not isinstance(images, Mapping) or frozenset(
        str(name) for name in images
    ) != frozenset(_SERVICE_NAMES):
        errors.append("manifest image inventory must match the four Compose services")
    else:
        for name in _SERVICE_NAMES:
            record = images.get(name)
            if not isinstance(record, Mapping):
                errors.append(f"manifest images.{name} must be an object")
                continue
            _validate_digest(record.get("image"), f"images.{name}.image", errors)
            _validate_hash(
                record.get("sbom_sha256"), f"images.{name}.sbom_sha256", errors
            )
            if name == "media-server-mcp":
                if (
                    record.get("source_revision")
                    != "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
                ):
                    errors.append(
                        "manifest upstream revision is not the reviewed v2.3.0 revision"
                    )
            elif name != "hermes-media":
                _validate_commit(
                    record.get("source_revision"),
                    f"images.{name}.source_revision",
                    errors,
                )
            if name == "hermes-media":
                if record.get("dockerfile") != "deployment/Dockerfile.hermes":
                    errors.append(
                        "manifest Hermes image must be built from deployment/Dockerfile.hermes"
                    )
                _validate_digest(
                    record.get("base_image"), "images.hermes-media.base_image", errors
                )
                _validate_commit(
                    record.get("base_revision"),
                    "images.hermes-media.base_revision",
                    errors,
                )
                _validate_commit(
                    record.get("policy_extension_revision"),
                    "images.hermes-media.policy_extension_revision",
                    errors,
                )

    runtime = document.get("runtime")
    if not isinstance(runtime, Mapping):
        errors.append("manifest runtime section is required")
    else:
        if runtime.get("uid") != 1000 or runtime.get("gid") != 1000:
            errors.append("manifest runtime UID/GID must be 1000:1000")
        if any(
            runtime.get(key) != expected
            for key, expected in (
                ("umask", "0077"),
                ("secret_directory_mode", "0700"),
                ("state_directory_mode", "0700"),
                ("secret_file_mode", "0600"),
                ("database_file_mode", "0600"),
            )
        ):
            errors.append("manifest host permission contract is incomplete")
        if (
            runtime.get("hermes_uid") != 1000
            or runtime.get("hermes_gid") != 1000
            or runtime.get("hermes_native_s6_pid1") is not True
        ):
            errors.append("manifest Hermes PID1/remap contract is incomplete")
        if (
            runtime.get("hermes_dashboard") is not False
            or runtime.get("hermes_dashboard_port") is not None
        ):
            errors.append("manifest must disable the Hermes dashboard and port")
        if runtime.get("docker_socket") is not False:
            errors.append("manifest must explicitly forbid the Docker socket")
        if (
            runtime.get("plex_service_in_stack") is not False
            or runtime.get("plex_container_mutation_allowed") is not False
        ):
            errors.append(
                "manifest must explicitly preserve the existing Plex container"
            )
        published = runtime.get("published_ports")
        if (
            not isinstance(published, Mapping)
            or published.get("dashboard") != "18082:18082"
            or published.get("plex_webhook") != "127.0.0.1:18081:18080"
        ):
            errors.append("manifest published port contract is incorrect")
        internal_ports = runtime.get("internal_ports")
        if (
            not isinstance(internal_ports, Mapping)
            or internal_ports.get("hermes_policy_helper") != "8787"
        ):
            errors.append("manifest Hermes policy helper port contract is incorrect")

    networks = document.get("networks")
    if not isinstance(networks, Mapping):
        errors.append("manifest network section is required")
    else:
        if frozenset(networks.get("private", ())) != _PRIVATE_NETWORKS:
            errors.append("manifest must list exactly the four private networks")
        if tuple(networks.get("external", ())) != ("media",):
            errors.append("manifest external network must be media")
        memberships = networks.get("memberships")
        if not isinstance(memberships, Mapping):
            errors.append("manifest network memberships are required")
        else:
            for service, expected in _EXPECTED_MEMBERSHIPS.items():
                actual = frozenset(memberships.get(service, ()))
                if actual != expected:
                    errors.append(f"manifest network membership drifted for {service}")

    files = document.get("canonical_files")
    if not isinstance(files, Mapping):
        errors.append("manifest canonical_files section is required")
    else:
        seen_paths: set[str] = set()
        for name, record in files.items():
            if not isinstance(record, Mapping):
                errors.append(f"manifest canonical file {name} must be an object")
                continue
            path = record.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or Path(path).name not in _SECRET_BASENAMES
            ):
                errors.append(f"manifest canonical file {name} has an invalid path")
            elif path in seen_paths:
                errors.append(
                    f"manifest canonical file path is duplicated: {Path(path).name}"
                )
            else:
                seen_paths.add(path)
            if record.get("mode") != "0600":
                errors.append(f"manifest canonical file {name} must have mode 0600")
            consumers = record.get("consumers")
            if (
                not isinstance(consumers, Sequence)
                or isinstance(consumers, (str, bytes))
                or not consumers
            ):
                errors.append(f"manifest canonical file {name} has no consumer list")

    invariants = document.get("invariants")
    required_invariants = {
        "no_plex_service",
        "no_plex_restart_or_recreate",
        "no_hermes_dashboard_listener",
        "no_port_9119",
        "no_docker_socket_mount",
        "dashboard_requires_authenticated_session",
        "dashboard_healthz_only_anonymous",
        "webhook_loopback_only",
        "mutable_tags_rejected",
    }
    if not isinstance(invariants, Mapping) or any(
        invariants.get(key) is not True for key in required_invariants
    ):
        errors.append("manifest deployment invariants are incomplete")

    # A release manifest is not a place for placeholders, mutable tags, or
    # accidental secret-looking values after interpolation.
    for value in _walk_strings(document):
        lowered = value.lower()
        if (
            "replace_with" in lowered
            or "placeholder" in lowered
            or ":latest" in lowered
        ):
            errors.append("manifest contains a placeholder or mutable image tag")
    return ValidationResult(tuple(dict.fromkeys(errors)))


def validate_assets(
    compose_path: str | os.PathLike[str] = DEFAULT_COMPOSE,
    manifest_path: str | os.PathLike[str] = DEFAULT_MANIFEST,
    *,
    env: Mapping[str, str] | None = None,
) -> ValidationResult:
    """Render and validate both checked-in deployment assets."""

    values = dict(os.environ if env is None else env)
    compose, compose_errors, _ = _load_yaml(Path(compose_path), values)
    manifest, manifest_errors, _ = _load_yaml(Path(manifest_path), values)
    errors = list(compose_errors) + list(manifest_errors)
    if compose is not None:
        errors.extend(validate_compose_document(compose).errors)
    if manifest is not None:
        errors.extend(validate_manifest_document(manifest).errors)
    return ValidationResult(tuple(dict.fromkeys(errors)))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="release metadata env file (values are never printed)",
    )
    args = parser.parse_args(argv)
    values = dict(os.environ)
    try:
        if args.env_file is not None:
            values.update(load_env_file(args.env_file))
        result = validate_assets(args.compose, args.manifest, env=values)
    except DeploymentValidationError as exc:
        print(f"deployment validation failed: {exc}", file=sys.stderr)
        return 2
    if not result.ok:
        print("deployment validation failed:", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
        return 2
    print(
        "deployment validation ok: "
        f"compose_sha256={_sha256(args.compose)} manifest={args.manifest.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DeploymentValidationError",
    "ValidationResult",
    "load_env_file",
    "main",
    "render_template",
    "validate_assets",
    "validate_compose_document",
    "validate_manifest_document",
]
