"""Transport credentials reach only the processes that talk to Musubi.

``local_tool_environment`` is ``tool_environment`` minus ``MUSUBI_API_URL`` and
``MUSUBI_TOKEN``. The facade's local outbox write (``remember``) uses it; the
drain and memory-data reads keep the full environment.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from musubi_harness.plugin_mcp import PluginMcpFacade
from musubi_harness.plugin_runtime import TRANSPORT_ENV, PluginRuntime, RuntimeConfig

IDENTITY = {
    "MUSUBI_ACTOR": "alice",
    "MUSUBI_PRESENCE": "alice/laptop",
    "MUSUBI_ZONE": "home",
    "MUSUBI_HARNESS_BIN": "/opt/fake/musubi-harness",
    "MUSUBI_MEMORY_DATA_BIN": "/opt/fake/memory-data",
}
CREDENTIALS = {"MUSUBI_API_URL": "https://musubi.example", "MUSUBI_TOKEN": "a.b.c"}


def config_for(tmp_path: Path, **extra: str) -> tuple[PluginRuntime, RuntimeConfig]:
    runtime = PluginRuntime("harness-test", default_data_root=tmp_path)
    with patch.dict("os.environ", {**IDENTITY, **extra}, clear=True):
        return runtime, runtime.runtime_config()


def test_local_environment_drops_only_the_transport_credentials(tmp_path: Path) -> None:
    runtime, config = config_for(tmp_path)
    with patch.dict("os.environ", {**IDENTITY, **CREDENTIALS, "OTHER": "kept"}, clear=True):
        full = runtime.tool_environment(config)
        local = runtime.local_tool_environment(config)
    assert set(TRANSPORT_ENV) == {"MUSUBI_API_URL", "MUSUBI_TOKEN"}
    assert {k: full[k] for k in TRANSPORT_ENV} == CREDENTIALS  # unchanged for network calls
    assert not set(TRANSPORT_ENV) & set(local)
    assert local == {k: v for k, v in full.items() if k not in TRANSPORT_ENV}
    assert local["OTHER"] == "kept" and local["FLEET_IDENTITY"] == "alice"


def test_an_adapter_override_of_tool_environment_still_applies(tmp_path: Path) -> None:
    class Adapter(PluginRuntime):
        @staticmethod
        def tool_environment(config: RuntimeConfig) -> dict[str, str]:
            return {"ADAPTER": "yes", **CREDENTIALS}

    config = config_for(tmp_path)[1]
    assert Adapter("harness-test", default_data_root=tmp_path).local_tool_environment(config) == {"ADAPTER": "yes"}


def test_remember_gets_no_credentials_and_the_drain_gets_them(tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    staged: dict[str, str] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = argv[3]  # <bin> --db <path> <command> ...
        seen.append((command, dict(kwargs["env"])))
        if command == "remember":
            staged["event_id"] = argv[argv.index("--event-id") + 1]
            result = {"event_id": staged["event_id"], "state": "pending"}
        else:  # drain
            result = {"event_id": staged["event_id"], "state": "verified", "object_id": "obj-1"}
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")

    runtime, config = config_for(tmp_path, MUSUBI_DELIVERY_MODE="verified")
    facade = PluginMcpFacade(runtime, source="claude-code", event_prefix="t", owner_label="t", server_name="t")
    with (
        patch.dict("os.environ", {**IDENTITY, **CREDENTIALS, "MUSUBI_DELIVERY_MODE": "verified"}, clear=True),
        patch("musubi_harness.plugin_mcp.subprocess.run", fake_run),
    ):
        response = facade.call_tool(config, "musubi_remember", {"content": "a fact"})

    payload = json.loads(response["content"][0]["text"])
    assert (payload["status"], payload["object_id"]) == ("verified", "obj-1")
    assert [command for command, _ in seen] == ["remember", "drain"]
    remember_env, drain_env = seen[0][1], seen[1][1]
    assert not set(TRANSPORT_ENV) & set(remember_env)
    assert {k: drain_env[k] for k in TRANSPORT_ENV} == CREDENTIALS
