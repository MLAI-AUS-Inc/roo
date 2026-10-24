"""Pinned Hermes ACP compatibility shim; never widens execution/tool policy.

v0.21.5 implements HTTP MCP registration but omits its capability declaration.
Keep this workaround scoped to the reviewed image/source. Fail closed on drift.
"""

import hashlib
import json
import os
from pathlib import Path
import sys

EXPECTED_VERSION = "0.21.5"
EXPECTED_REVISION = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
EXPECTED_SERVER_SHA256 = "42741bd01361de1ab6c37a9b74c51db36ad9f1fbb0ca28c6307df59a1cadf35a"
EXPECTED_LIMIT_SOURCES = {
    "run_agent.py": "244da863d3c21591a3b5326dc14c2962d4e31131dda52df628502cd9fcbfea33",
    "acp_adapter/session.py": "b4a31319bf384538ada301aabb009241e8704720e9f2d790a62c55f0315a2136",
    'toolsets.py': '48ba8bea0b9bcd5821747f480055a224639fd83565d9b14d6134bbe7d433aa43',
    'tools/browser_tool.py': '7e0f76d8ae26983b726143512fe9d4a2b48f20b74aacdd6fd62cd5613ae89130',
    'hermes_cli/tools_config.py': '113274934cd85734005e04acc2b86216899ccde6db9eb632660eec784ee71fb7',
    'model_tools.py': '5d5a947d84f31f1ba4ef5267e28154b819e8f957a0b378739696f1ac305e1509',
}


TEXT_BROWSER_TOOLS = (
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
    "browser_scroll", "browser_back", "browser_press", "browser_get_images",
    "browser_console",
)


def register_text_browser(toolsets):
    # The broker's model contract is text-only. Navigate returns an accessibility
    # snapshot; browser_vision would inject images or call an auxiliary provider.
    # Explicit selection also excludes vault, arbitrary CDP and browser_exec.
    toolsets.create_custom_toolset("roo_browser_text", "Roo browser with text/DOM results only",
                                  tools=list(TEXT_BROWSER_TOOLS))


def verify_image(provenance: Path = Path("/etc/hermes/image-provenance.json"),
                 server: Path = Path("/opt/hermes/acp_adapter/server.py")) -> None:
    marker = json.loads(provenance.read_text())
    if (marker.get("version") != EXPECTED_VERSION or marker.get("revision") != EXPECTED_REVISION
            or hashlib.sha256(server.read_bytes()).hexdigest() != EXPECTED_SERVER_SHA256):
        raise RuntimeError("Hermes image/source differs from the reviewed ACP contract")


def advertise_http(response):
    # Validate through the installed SDK so wire aliases remain SDK-owned.
    data = response.model_dump(by_alias=True)
    data["agentCapabilities"]["mcpCapabilities"] = {"http": True, "sse": False}
    return type(response).model_validate(data)


def constrain_agent_kwargs(kwargs: dict, config: dict) -> dict:
    """The pinned ACP adapter does not forward agent.max_turns to AIAgent."""
    if kwargs.get("platform") != "acp":
        return kwargs
    cap = config.get("agent", {}).get("max_turns")
    if type(cap) is not int or not 0 < cap <= 100:
        raise RuntimeError("Missing bounded Hermes task iteration limit")
    result = dict(kwargs)
    result["max_iterations"] = min(result.get("max_iterations", cap), cap)
    result["skip_memory"] = True
    result["skip_background_review"] = True
    return result


def main() -> None:
    verify_image()
    for relative, digest in EXPECTED_LIMIT_SOURCES.items():
        if hashlib.sha256((Path("/opt/hermes") / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Hermes reviewed task contract changed")
    import toolsets
    register_text_browser(toolsets)
    from acp_adapter.server import HermesACPAgent
    from acp_adapter.entry import main as hermes_main
    from hermes_cli import __version__
    from run_agent import AIAgent

    if __version__ != EXPECTED_VERSION:
        raise RuntimeError("Unexpected Hermes runtime version")
    original = HermesACPAgent.initialize

    async def initialize(self, *args, **kwargs):
        return advertise_http(await original(self, *args, **kwargs))

    HermesACPAgent.initialize = initialize
    original_init = AIAgent.__init__

    def bounded_init(self, *args, **kwargs):
        if kwargs.get("platform") == "acp":
            # Read the controller's immutable task config, not generated files.
            path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
            data = path.read_bytes()
            if len(data) > 65536:
                raise RuntimeError("Task configuration too large")
            kwargs = constrain_agent_kwargs(kwargs, json.loads(data))
        original_init(self, *args, **kwargs)

    AIAgent.__init__ = bounded_init
    hermes_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Dependency/import failures can include environment data; operator gets
        # a stable failure and can inspect the pinned image independently.
        print("Roo Hermes runtime verification/startup failed", file=sys.stderr)
        raise SystemExit(1)
