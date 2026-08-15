from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml
from media_companion import tool_policy


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _uses_values(value: object) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, child in value.items():
            if key == "uses" and isinstance(child, str):
                result.append(child)
            result.extend(_uses_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_uses_values(child))
        return result
    return []


def _workflow(name: str) -> tuple[dict[str, Any], str]:
    path = WORKFLOWS / name
    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed, text


def test_workflow_actions_are_immutable_and_deployment_checks_are_present() -> None:
    for name in ("ci.yml", "publish-image.yml"):
        parsed, text = _workflow(name)
        uses = _uses_values(parsed)
        assert uses
        assert all(ACTION_SHA.fullmatch(value) for value in uses), uses
        for asset in (
            "deployment/Dockerfile.companion",
            "deployment/Dockerfile.dashboard",
            "deployment/Dockerfile.hermes",
            "scripts/validate_deployment.py",
        ):
            assert asset in text
        assert "docker/setup-buildx-action@" in text
        assert "docker/build-push-action@" in text
        assert "python scripts/validate_deployment.py --help" in text
    ci_text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert (
        "nousresearch/hermes-agent@sha256:"
        "16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e"
    ) in ci_text
    assert "example.invalid" not in ci_text
    assert "file: ./Dockerfile" in ci_text
    assert "file: ./Dockerfile.webhook" in ci_text
    publish_text = (WORKFLOWS / "publish-image.yml").read_text(encoding="utf-8")
    assert (
        "inputs.hermes_base_image || 'nousresearch/hermes-agent@sha256:"
        "16788311e2fa3035456bdc1bafb8ec2b1777db64ebf020af9bb7eb73c3712c9e'"
    ) in publish_text
    assert "vars.HERMES_BASE_IMAGE" not in publish_text


def test_publish_workflow_attests_and_reports_immutable_digests_for_all_images() -> (
    None
):
    _parsed, text = _workflow("publish-image.yml")
    assert text.count("sbom: true") >= 3
    assert text.count("provenance: mode=max") >= 3
    assert text.count("push: true") >= 3
    for image in (
        "media-request-companion",
        "media-request-dashboard",
        "hermes-media",
    ):
        assert f"ghcr.io/crbl-technologies/{image}" in text
        assert (
            f"Record {('MCP' if image == 'plex-media-request-mcp' else 'webhook' if image == 'plex-media-request-webhook-bridge' else 'companion' if image == 'media-request-companion' else 'dashboard' if image == 'media-request-dashboard' else 'Hermes')} immutable digest"
            in text
        )
    # Compose consumes the independently pinned upstream media-server-mcp
    # image and does not define the legacy webhook bridge, so publishing those
    # compatibility builds would create misleading release artifacts. CI
    # still builds both legacy Dockerfiles for compatibility coverage.
    assert "plex-media-request-mcp" not in text
    assert "plex-media-request-webhook-bridge" not in text


def test_openapi_documents_actual_companion_routes_and_shared_inventory() -> None:
    document = yaml.safe_load(
        (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    )
    paths = document["paths"]
    assert {
        "/healthz",
        "/readyz",
        "/healthz/ready",
        "/mcp",
        "/private/confirm/bind",
        "/private/confirm/callback",
        "/private/dashboard/{operation}",
        "/private/operations/{operation}",
        "/private/plex/{capability}",
    }.issubset(paths)
    assert not any(path.startswith("/tools/") for path in paths)
    shared = set(document["components"]["schemas"]["SharedToolName"]["enum"])
    assert shared == {
        "search_media",
        "request_movie",
        "request_series",
        "request_status",
        "download_status",
        "browse_library",
        "media_status",
    }
    assert set(document["components"]["schemas"]["DashboardOperation"]["enum"]) == {
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
    assert set(document["components"]["securitySchemes"]) >= {
        "actorAssertion",
        "confirmationHelper",
        "dashboardSignature",
    }
    upstream_contract = document["x-upstream-tool-contract"]
    assert upstream_contract["tool_count"] == 102
    assert upstream_contract["source_revision"] == tool_policy.UPSTREAM_REVISION
    assert upstream_contract["image"] == tool_policy.UPSTREAM_IMAGE
    assert upstream_contract["schema_sha256"] == (
        "31451102af4d424ce516d5515db5839028f17e9363651e0d1a2d64518633f2b1"
    )
    assert upstream_contract["required_include"] == [
        "tmdb_get_movie_credits",
        "tmdb_get_tv_credits",
    ]
    assert paths["/private/plex/{capability}"]["post"]["x-security-boundary"] == {
        "type": "path-capability",
        "parameter": "capability",
        "loopback_only": True,
    }


def test_release_contract_pins_the_canonical_102_tool_inventory() -> None:
    """A release must not silently publish a different upstream tool schema."""

    assert len(tool_policy.UPSTREAM_TOOLS) == 102
    assert len(tool_policy.UPSTREAM_TOOL_SET) == 102
    assert {
        "tmdb_get_movie_credits",
        "tmdb_get_tv_credits",
    }.issubset(tool_policy.UPSTREAM_TOOL_SET)
    assert tool_policy.UPSTREAM_REVISION == "8b469d2b321b27dd1e4f5b89a7236b3ea43c3c72"
    assert tool_policy.UPSTREAM_OCI_DIGEST == (
        "sha256:f83620da1d008ef18df3324b15e44854572ea41b528eff585033e4054b438377"
    )
    assert (
        hashlib.sha256(
            json.dumps(tool_policy.UPSTREAM_TOOLS, separators=(",", ":")).encode()
        ).hexdigest()
        == "02390ae11d07dae8920276460e83503ddd0d115d4ea7f76f19a1f48648f46b24"
    )

    contract = json.loads(
        (
            ROOT
            / "deployment"
            / "hermes"
            / "plugins"
            / "platforms"
            / "media-policy"
            / "tool_contract.json"
        ).read_text(encoding="utf-8")
    )
    upstream = contract["upstream_tools"]
    assert len(upstream) == 102
    assert contract["upstream_source_revision"] == tool_policy.UPSTREAM_REVISION
    schema_digest = "31451102af4d424ce516d5515db5839028f17e9363651e0d1a2d64518633f2b1"
    assert contract["upstream_tool_digest"] == "sha256:" + schema_digest
    projected = [
        {
            key: entry.get(key)
            for key in ("name", "title", "description", "inputSchema", "annotations")
        }
        for entry in upstream
    ]
    canonical = (
        json.dumps(
            projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    assert hashlib.sha256(canonical).hexdigest() == schema_digest
    assert {
        "tmdb_get_movie_credits",
        "tmdb_get_tv_credits",
    }.issubset({entry["name"] for entry in upstream})

    native_contract = json.loads(
        (
            ROOT
            / "deployment"
            / "hermes"
            / "plugins"
            / "platforms"
            / "media-policy"
            / "native_contract.json"
        ).read_text(encoding="utf-8")
    )
    assert native_contract["tool_profile"] == "full"
    assert native_contract["tool_include"] == [
        "tmdb_get_movie_credits",
        "tmdb_get_tv_credits",
    ]
    assert native_contract["upstream_tool_contract_sha256"] == schema_digest
