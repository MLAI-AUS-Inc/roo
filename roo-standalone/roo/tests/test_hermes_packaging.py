import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def shim():
    spec = importlib.util.spec_from_file_location("hermes_acp_shim", ROOT / "hermes/acp_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shim_rejects_image_revision_and_code_drift(shim, tmp_path, monkeypatch):
    source = tmp_path / "server.py"
    source.write_bytes(b"reviewed source")
    marker = tmp_path / "image.json"
    marker.write_text(json.dumps({"version": shim.EXPECTED_VERSION, "revision": shim.EXPECTED_REVISION}))
    monkeypatch.setattr(shim, "EXPECTED_SERVER_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    shim.verify_image(marker, source)
    source.write_bytes(b"different source")
    with pytest.raises(RuntimeError):
        shim.verify_image(marker, source)
    source.write_bytes(b"reviewed source")
    marker.write_text(json.dumps({"version": shim.EXPECTED_VERSION, "revision": "unreviewed"}))
    with pytest.raises(RuntimeError):
        shim.verify_image(marker, source)


def test_shim_preserves_other_capabilities_and_validates_wire_aliases(shim):
    class Capabilities(BaseModel):
        model_config = ConfigDict(populate_by_name=True, extra="forbid")
        load_session: bool = Field(alias="loadSession")
        mcp_capabilities: dict = Field(default_factory=dict, alias="mcpCapabilities")

    class Response(BaseModel):
        model_config = ConfigDict(populate_by_name=True, extra="forbid")
        protocol_version: int = Field(alias="protocolVersion")
        agent_capabilities: Capabilities = Field(alias="agentCapabilities")

    result = shim.advertise_http(Response(protocolVersion=1, agentCapabilities={"loadSession": True}))
    assert result.model_dump(by_alias=True) == {"protocolVersion": 1, "agentCapabilities": {
        "loadSession": True, "mcpCapabilities": {"http": True, "sse": False}}}


def test_iteration_shim_applies_explicit_cap_without_widening_existing_limit(shim):
    bounded = shim.constrain_agent_kwargs({"platform": "acp", "model": "approved"}, {"agent": {"max_turns": 20}})
    assert bounded == {"platform": "acp", "model": "approved", "max_iterations": 20,
                       "skip_memory": True, "skip_background_review": True}
    assert shim.constrain_agent_kwargs({"platform": "acp", "max_iterations": 3}, {"agent": {"max_turns": 20}})["max_iterations"] == 3
    for config in ({}, {"agent": {"max_turns": 0}}, {"agent": {"max_turns": True}}, {"agent": {"max_turns": 101}}):
        with pytest.raises(RuntimeError):
            shim.constrain_agent_kwargs({"platform": "acp"}, config)


def test_infrastructure_exposes_only_internal_fixed_routes():
    import yaml
    manifest = yaml.safe_load((ROOT / "docker-compose.hermes-infrastructure.yml").read_text())
    assert manifest["networks"]["internal"]["internal"] is True
    assert manifest["networks"]["public-web"]["internal"] is True
    for service in manifest["services"].values():
        assert not service.get("ports")
        assert service["read_only"] is True and service["user"] == "10000:10000"
    assert "internal" not in manifest["services"]["hermes-web-egress"]["networks"]
    template = (ROOT / "hermes/broker/nginx.conf.template").read_text()
    assert "proxy_ssl_verify on;" in template
    assert "proxy_pass_request_headers off;" in template
    assert "location / { return 404; }" in template
    routes = [line.strip() for line in template.splitlines() if "proxy_pass https:" in line]
    assert routes == [
        "proxy_pass https://$approved_backend/api/v1/agent-runtime/mcp/;",
        "proxy_pass https://$approved_backend/api/v1/agent-runtime/model/v1/chat/completions;",
    ]


@pytest.mark.parametrize("hostname", ["", "127.0.0.1", "https://backend.example", "backend.example/path",
                                      "backend.example;env", "backend.example\nother.example", "-bad.example"])
def test_broker_rejects_destination_injection_before_render(hostname):
    import os
    import subprocess
    result = subprocess.run(["/bin/sh", str(ROOT / "hermes/broker/entrypoint.sh")],
                            env={"PATH": "/usr/bin:/bin", "ROO_HERMES_BACKEND_HOST": hostname},
                            capture_output=True, timeout=2)
    assert result.returncode != 0
    assert b"must be an explicit DNS hostname" in result.stderr


def test_operator_browser_has_bounded_clean_profile_and_protected_access():
    import yaml
    manifest = yaml.safe_load((ROOT / "docker-compose.hermes-browser.yml").read_text())
    browser = manifest["services"]["browser"]
    assert browser["profiles"] == ["operator-browser"]
    assert not browser.get("ports") and browser["read_only"] is True
    assert any(value.startswith("/profile:") and "size=512m" in value for value in browser["tmpfs"])
    assert all(value["read_only"] for value in browser["volumes"])
    nginx = (ROOT / "hermes/browser/nginx.conf").read_text()
    assert "listen 8443 ssl;" in nginx and "auth_basic_user_file" in nginx


def test_text_browser_toolset_excludes_image_and_authority_tools(shim):
    recorded = {}
    class Toolsets:
        @staticmethod
        def create_custom_toolset(name, description, *, tools):
            recorded.update(name=name, tools=tools)
    shim.register_text_browser(Toolsets)
    assert recorded == {"name": "roo_browser_text", "tools": [
        "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
        "browser_scroll", "browser_back", "browser_press", "browser_get_images", "browser_console"]}
    assert {"toolsets.py", "tools/browser_tool.py", "hermes_cli/tools_config.py", "model_tools.py"} <= set(shim.EXPECTED_LIMIT_SOURCES)
