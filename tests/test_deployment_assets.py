from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml

from scripts.validate_deployment import (
    DEFAULT_COMPOSE,
    DEFAULT_MANIFEST,
    render_template,
    validate_assets,
    validate_compose_document,
    validate_manifest_document,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
HASH = "b" * 64
COMMIT = "c" * 40


def _release_env() -> dict[str, str]:
    return {
        "MEDIA_SERVER_MCP_IMAGE": f"ghcr.io/wyattjoh/media-server-mcp@sha256:{DIGEST}",
        "MEDIA_COMPANION_IMAGE": f"ghcr.io/crbl-technologies/media-request-companion@sha256:{DIGEST}",
        "MEDIA_DASHBOARD_IMAGE": f"ghcr.io/crbl-technologies/media-request-dashboard@sha256:{DIGEST}",
        "HERMES_MEDIA_IMAGE": f"ghcr.io/crbl-technologies/hermes-media@sha256:{DIGEST}",
        "HERMES_BASE_IMAGE": f"ghcr.io/crbl-technologies/hermes@sha256:{DIGEST}",
        "MEDIA_COMPANION_COMMIT": COMMIT,
        "MEDIA_DASHBOARD_COMMIT": COMMIT,
        "HERMES_BASE_COMMIT": COMMIT,
        "HERMES_MEDIA_COMMIT": COMMIT,
        "COMPOSE_SHA256": HASH,
        "CONFIG_SHA256": HASH,
        "MIGRATION_CHECKSUMS_SHA256": HASH,
        "UPSTREAM_SBOM_SHA256": HASH,
        "COMPANION_SBOM_SHA256": HASH,
        "DASHBOARD_SBOM_SHA256": HASH,
        "HERMES_SBOM_SHA256": HASH,
        "UPSTREAM_ENV_FILE": "/volume2/docker/hermes-media/secrets/upstream.env",
        "COMPANION_ENV_FILE": "/volume2/docker/hermes-media/secrets/companion.env",
        "HERMES_DATA_DIR": "/volume2/docker/hermes-media/data",
        "COMPANION_STATE_DIR": "/volume2/docker/hermes-media/data/state",
        "ACTOR_SIGNING_KEY_FILE": "/volume2/docker/hermes-media/secrets/actor-signing.key",
        "DASHBOARD_API_KEY_FILE": "/volume2/docker/hermes-media/secrets/dashboard-api.key",
        "DASHBOARD_AUTH_ENV_FILE": "/volume2/docker/hermes-media/secrets/dashboard-auth.env",
        "MEDIA_DASHBOARD_ALLOWED_ORIGINS": "http://localhost:18082",
    }


def test_production_assets_validate_with_resolved_release_metadata() -> None:
    result = validate_assets(DEFAULT_COMPOSE, DEFAULT_MANIFEST, env=_release_env())
    assert result.ok, result.errors


def test_missing_required_digest_fails_closed() -> None:
    env = _release_env()
    env.pop("MEDIA_COMPANION_IMAGE")
    result = validate_assets(DEFAULT_COMPOSE, DEFAULT_MANIFEST, env=env)
    assert not result.ok
    assert any("MEDIA_COMPANION_IMAGE" in error for error in result.errors)


def test_mutable_image_reference_is_rejected() -> None:
    env = _release_env()
    env["MEDIA_DASHBOARD_IMAGE"] = (
        "ghcr.io/crbl-technologies/media-request-dashboard:latest"
    )
    result = validate_assets(DEFAULT_COMPOSE, DEFAULT_MANIFEST, env=env)
    assert not result.ok
    assert any("immutable image digest" in error for error in result.errors)


def test_plex_service_and_non_loopback_webhook_are_explicitly_forbidden() -> None:
    text = DEFAULT_COMPOSE.read_text(encoding="utf-8")
    assert "# There is intentionally no Plex service" in text
    assert "127.0.0.1:18081:18080" in text
    assert "  plex:" not in text
    assert "  plex-" not in text


def test_plex_lifecycle_and_socket_access_are_rejected() -> None:
    env = _release_env()
    compose = yaml.safe_load(
        render_template(DEFAULT_COMPOSE.read_text(encoding="utf-8"), env)
    )
    compose = copy.deepcopy(compose)
    compose["services"]["media-companion"]["command"] = [
        "docker",
        "restart",
        "plex",
    ]
    compose["services"]["media-companion"]["volumes"].append(
        "/var/run/docker.sock:/var/run/docker.sock:ro"
    )
    compose_result = validate_compose_document(compose)
    assert any("lifecycle commands" in error for error in compose_result.errors)
    assert any("Docker socket" in error for error in compose_result.errors)

    manifest = yaml.safe_load(
        render_template(DEFAULT_MANIFEST.read_text(encoding="utf-8"), env)
    )
    manifest = copy.deepcopy(manifest)
    manifest["operator_command"] = "docker compose restart plex"
    manifest["socket"] = "/var/run/docker.sock"
    manifest_result = validate_manifest_document(manifest)
    assert any("lifecycle commands" in error for error in manifest_result.errors)
    assert any("Docker socket" in error for error in manifest_result.errors)


def test_policy_helper_is_hermes_hosted_and_internal_only() -> None:
    text = DEFAULT_COMPOSE.read_text(encoding="utf-8")
    assert "CRBL_POLICY_HELPER_URL: http://hermes-media:8787" in text
    assert "CRBL_POLICY_HELPER_URL: http://media-companion" not in text
    assert '      - "8787"' in text
    assert '"8787:8787"' not in text


def test_companion_selects_provider_urls_without_upstream_env_injection() -> None:
    rendered = render_template(
        DEFAULT_COMPOSE.read_text(encoding="utf-8"), _release_env()
    )
    compose = yaml.safe_load(rendered)
    companion = compose["services"]["media-companion"]
    assert companion["environment"]["MEDIA_COMPANION_PROVIDER_ENV_FILE"] == (
        "/run/media-secrets/upstream.env"
    )
    assert "env_file" not in companion
    assert companion["environment"]["MEDIA_COMPANION_DB_PATH"] == (
        "/opt/data/state/media_requests.sqlite3"
    )
    assert "MEDIA_COMPANION_RADARR_QUALITY_PROFILE_ID" in companion["environment"]
    assert "MEDIA_COMPANION_SONARR_TAG_IDS" in companion["environment"]
    assert any(
        mount.endswith(":/run/media-secrets/companion.env:ro")
        for mount in companion["volumes"]
    )
    assert all("hermes.env" not in mount for mount in companion["volumes"])
    assert "MEDIA_COMPANION_TELEGRAM_BOT_TOKEN_FILE" not in companion["environment"]


def test_upstream_service_selects_the_canonical_102_tool_inventory() -> None:
    rendered = render_template(
        DEFAULT_COMPOSE.read_text(encoding="utf-8"), _release_env()
    )
    compose = yaml.safe_load(rendered)
    upstream = compose["services"]["media-server-mcp"]
    assert upstream["environment"]["TOOL_PROFILE"] == "full"
    assert upstream["environment"]["TOOL_INCLUDE"] == (
        "tmdb_get_movie_credits,tmdb_get_tv_credits"
    )
    assert "env_file" not in upstream
    assert upstream["entrypoint"] == [
        "deno",
        "run",
        "--env-file=/run/media-secrets/upstream.env",
        "--allow-read",
        "--allow-write",
        "--allow-env",
        "--allow-run",
        "--allow-net",
        "packages/media-server-mcp/src/index.ts",
    ]
    assert upstream["command"] == ["--http", "--host", "0.0.0.0", "--port", "3000"]
    assert any(
        mount.endswith(":/run/media-secrets/upstream.env:ro")
        for mount in upstream["volumes"]
    )


def test_dashboard_uses_raw_hash_file_and_environment_origins() -> None:
    rendered = render_template(
        DEFAULT_COMPOSE.read_text(encoding="utf-8"), _release_env()
    )
    compose = yaml.safe_load(rendered)
    dashboard = compose["services"]["media-request-dashboard"]
    assert "env_file" not in dashboard
    assert dashboard["environment"]["MEDIA_DASHBOARD_ALLOWED_ORIGINS"] == (
        "http://localhost:18082"
    )
    assert dashboard["environment"]["MEDIA_DASHBOARD_PASSWORD_HASH_FILE"] == (
        "/run/media-secrets/dashboard-auth.env"
    )
    assert any(
        mount.endswith(":/run/media-secrets/dashboard-auth.env:ro")
        for mount in dashboard["volumes"]
    )


def test_network_topology_keeps_telegram_egress_and_backend_isolation() -> None:
    rendered = render_template(
        DEFAULT_COMPOSE.read_text(encoding="utf-8"), _release_env()
    )
    compose = yaml.safe_load(rendered)
    networks = compose["networks"]
    assert networks["media-bot"].get("internal") is not True
    assert networks["media-service-egress"].get("internal") is not True
    assert networks["media-mcp-backend"]["internal"] is True
    assert networks["media-dashboard-backend"]["internal"] is True
    assert compose["services"]["hermes-media"]["networks"] == {"media-bot": {}}


def test_hermes_preserves_native_s6_pid1_and_worker_remap() -> None:
    rendered = render_template(
        DEFAULT_COMPOSE.read_text(encoding="utf-8"), _release_env()
    )
    compose = yaml.safe_load(rendered)
    hermes = compose["services"]["hermes-media"]
    assert "user" not in hermes
    assert "init" not in hermes
    assert hermes["environment"]["HERMES_UID"] == "1000"
    assert hermes["environment"]["HERMES_GID"] == "1000"
    assert "read_only" not in hermes
    assert "cap_drop" not in hermes
    assert "entrypoint" not in hermes
    assert hermes["command"] == ["gateway", "run"]
    assert any(
        item.startswith("/run:") and ",exec," in item for item in hermes["tmpfs"]
    )
    assert hermes["healthcheck"]["test"] == [
        "CMD",
        "/bin/sh",
        "/opt/hermes/policy-helper-healthcheck.sh",
    ]
    assert hermes["environment"]["TOOL_PROFILE"] == "full"
    assert hermes["environment"]["TOOL_INCLUDE"] == (
        "tmdb_get_movie_credits,tmdb_get_tv_credits"
    )


def test_hermes_derived_dockerfile_is_digest_pinned_and_preserves_dispatcher() -> None:
    text = (ROOT / "deployment" / "Dockerfile.hermes").read_text(encoding="utf-8")
    assert "ARG HERMES_BASE_IMAGE" in text
    assert "FROM ${HERMES_BASE_IMAGE} AS runtime" in text
    assert "re.fullmatch" in text
    assert "@sha256:[0-9a-f]{64}" in text
    assert "COPY hermes_media_extension /opt/hermes/hermes_media_extension" in text
    assert "COPY src/media_companion /app/src/media_companion" in text
    assert (
        "COPY deployment/hermes/plugins/platforms/media-policy "
        "/opt/hermes/plugins/platforms/zzzz-media-policy"
    ) in text
    assert "S6_BEHAVIOUR_IF_STAGE2_FAILS=2" in text
    assert "/etc/cont-init.d/03-media-policy-contract" in text
    assert "COPY deployment/hermes/s6-rc.d/ /etc/s6-overlay/s6-rc.d/" in text
    assert "/opt/hermes/policy-helper-healthcheck.sh" in text
    assert 'CMD ["gateway", "run"]' in text
    # The base image's dispatcher and s6 /init must remain the container
    # entrypoint; a derived shell supervisor or forced USER would bypass it.
    assert not any(line.lstrip().startswith("ENTRYPOINT") for line in text.splitlines())
    assert not any(line.lstrip().startswith("USER ") for line in text.splitlines())
    assert "deployment/hermes/entrypoint.sh" not in text


def test_pinned_tool_contract_has_exact_upstream_projection_and_closed_registration() -> (
    None
):
    contract_path = (
        ROOT
        / "deployment"
        / "hermes"
        / "plugins"
        / "platforms"
        / "media-policy"
        / "tool_contract.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    upstream = contract["upstream_tools"]
    assert len(upstream) == 102
    projected = [
        {
            key: entry[key]
            for key in ("name", "title", "description", "inputSchema", "annotations")
        }
        for entry in upstream
    ]
    digest = hashlib.sha256(
        (
            json.dumps(
                projected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    assert contract["upstream_tool_digest"] == "sha256:" + digest
    assert digest == "31451102af4d424ce516d5515db5839028f17e9363651e0d1a2d64518633f2b1"
    entries = upstream + contract["companion_tools"]
    assert len(entries) == 110
    assert len({entry["name"] for entry in entries}) == 110
    assert all(
        entry["inputSchema"]["type"] == "object"
        and entry["inputSchema"].get("additionalProperties") is not True
        for entry in entries
    )


def test_companion_and_dashboard_dockerfiles_require_digest_bases() -> None:
    expected = (
        ("Dockerfile.companion", "18080", "media_companion.app"),
        ("Dockerfile.dashboard", "18082", "media_dashboard"),
    )
    for filename, port, module in expected:
        text = (ROOT / "deployment" / filename).read_text(encoding="utf-8")
        assert "ARG PYTHON_BASE_IMAGE" in text
        assert "FROM ${PYTHON_BASE_IMAGE} AS runtime" in text
        assert "re.fullmatch" in text
        assert f"EXPOSE {port}" in text
        assert f'ENTRYPOINT ["python", "-m", "{module}"]' in text


def test_template_substitution_has_no_shell_expansion() -> None:
    assert render_template("${VALUE:-safe}", {}) == "safe"
    assert render_template("${VALUE}", {"VALUE": "literal$(id)"}) == "literal$(id)"
