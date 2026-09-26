"""Batch drain: deliver several rows per pass, bounded, stopping at the first failure.

One row per pass cannot shrink a backlog (each turn adds one and delivers one).
Measured on a live seat, 2026-09-26: queue depth 11-24 for hours, median
delivery lag 66 minutes. ``Drainer.flush`` repeats the existing ``flush_once``;
these tests pin its stopping rules and the CLI/facade contracts around it.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from musubi_harness.cli import harness as harness_cli
from musubi_harness.core import ContractError, Outbox
from musubi_harness.delivery import DeliveryStore, Drainer
from musubi_harness.plugin_mcp import PluginMcpFacade
from musubi_harness.plugin_runtime import PluginRuntime


def drainer_with(results: list[dict[str, object]], tmp_path: Path) -> tuple[Drainer, list[int]]:
    Outbox(tmp_path / "shadow.db")  # the delivery store requires an initialized outbox
    drainer = Drainer(DeliveryStore(tmp_path / "shadow.db"), client=object(), owner="test")  # type: ignore[arg-type]
    calls: list[int] = []

    def scripted(*, now: float | None = None) -> dict[str, object]:
        calls.append(1)
        return results[len(calls) - 1] if len(calls) <= len(results) else {"state": "idle"}

    drainer.flush_once = scripted  # type: ignore[method-assign]
    return drainer, calls


VERIFIED = {"state": "verified", "object_id": "o"}


def test_stops_when_the_queue_is_idle(tmp_path: Path) -> None:
    drainer, _ = drainer_with([VERIFIED, VERIFIED, {"state": "idle"}], tmp_path)
    assert [r["state"] for r in drainer.flush(max_rows=10, budget_seconds=60)] == ["verified", "verified", "idle"]


def test_stops_at_max_rows(tmp_path: Path) -> None:
    drainer, calls = drainer_with([VERIFIED] * 10, tmp_path)
    assert len(drainer.flush(max_rows=3, budget_seconds=60)) == 3 and len(calls) == 3


def test_stops_at_the_first_row_that_does_not_verify(tmp_path: Path) -> None:
    # A failing backend must cost at most one attempt per pass, as before.
    drainer, calls = drainer_with([VERIFIED, {"state": "pending", "reason": "memory_data_request_failed"}, VERIFIED], tmp_path)
    results = drainer.flush(max_rows=10, budget_seconds=60)
    assert [r["state"] for r in results] == ["verified", "pending"] and len(calls) == 2


def test_stops_starting_rows_once_the_budget_is_spent(tmp_path: Path) -> None:
    drainer, _ = drainer_with([VERIFIED] * 10, tmp_path)
    ticks = iter([0.0, 6.0, 12.0, 18.0])
    results = drainer.flush(max_rows=10, budget_seconds=10, clock=lambda: next(ticks))
    assert len(results) == 2  # 6 s after the first row, 12 s after the second: stop


@pytest.mark.parametrize(("max_rows", "budget"), [(0, 10.0), (3, 0.0)])
def test_nonsense_limits_are_refused(tmp_path: Path, max_rows: int, budget: float) -> None:
    drainer, _ = drainer_with([], tmp_path)
    with pytest.raises(ContractError):
        drainer.flush(max_rows=max_rows, budget_seconds=budget)


def cli(tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    Outbox(tmp_path / "shadow.db")
    out = io.StringIO()
    argv = ["musubi-harness", "--db", str(tmp_path / "shadow.db"), "drain", "--once", "--owner", "t", *extra]
    with patch("sys.argv", argv), redirect_stdout(out):
        code = harness_cli.main()
    return code, json.loads(out.getvalue())


def test_cli_default_output_is_unchanged(tmp_path: Path) -> None:
    code, payload = cli(tmp_path)
    assert code == 0 and payload == {"ok": True, "result": {"state": "idle"}}  # no "results" key


def test_cli_batch_output_keeps_result_and_adds_results(tmp_path: Path) -> None:
    code, payload = cli(tmp_path, "--max", "3", "--budget-seconds", "5")
    assert code == 0 and payload["result"] == {"state": "idle"} and payload["results"] == [{"state": "idle"}]


@pytest.mark.parametrize("bad", ["0", "21"])
def test_cli_refuses_max_out_of_range(tmp_path: Path, bad: str) -> None:
    code, payload = cli(tmp_path, "--max", bad)
    assert code == 2 and "between 1 and 20" in payload["error"]


def test_remember_finds_its_own_event_anywhere_in_the_batch(tmp_path: Path) -> None:
    # Oldest first: with a backlog, this remember is rarely the first row.
    env = {
        "MUSUBI_ACTOR": "alice",
        "MUSUBI_PRESENCE": "alice/laptop",
        "MUSUBI_ZONE": "home",
        "MUSUBI_DELIVERY_MODE": "verified",
        "MUSUBI_HARNESS_BIN": "/opt/fake/musubi-harness",
        "MUSUBI_MEMORY_DATA_BIN": "/opt/fake/memory-data",
    }
    staged: dict[str, str] = {}
    drain_argv: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[3] == "remember":
            staged["id"] = argv[argv.index("--event-id") + 1]
            out = {"result": {"event_id": staged["id"], "state": "pending"}}
        else:
            drain_argv.append(argv)
            older = [{"event_id": f"older-{i}", "state": "verified", "object_id": f"o{i}"} for i in range(2)]
            mine = {"event_id": staged["id"], "state": "verified", "object_id": "mine"}
            out = {"ok": True, "result": older[0], "results": [*older, mine]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

    with patch.dict("os.environ", env, clear=True):
        runtime = PluginRuntime("batch-test", default_data_root=tmp_path)
        config = runtime.runtime_config()
        facade = PluginMcpFacade(runtime, source="claude-code", event_prefix="t", owner_label="t", server_name="t")
        with patch("musubi_harness.plugin_mcp.subprocess.run", fake_run):
            response = facade.call_tool(config, "musubi_remember", {"content": "a fact"})
    payload = json.loads(response["content"][0]["text"])
    assert (payload["status"], payload["object_id"]) == ("verified", "mine")
    assert drain_argv[0][drain_argv[0].index("--max") + 1] == "5"


def test_an_error_after_the_first_row_keeps_the_verified_report(tmp_path: Path) -> None:
    drainer, _ = drainer_with([], tmp_path)
    calls = iter([VERIFIED, OSError("memory-data vanished")])

    def flaky(*, now: float | None = None) -> dict[str, object]:
        step = next(calls)
        if isinstance(step, Exception):
            raise step
        return step

    drainer.flush_once = flaky  # type: ignore[method-assign]
    results = drainer.flush(max_rows=5, budget_seconds=60)
    assert results == [VERIFIED, {"state": "error", "reason": "OSError"}]


def test_an_error_on_the_first_row_still_raises(tmp_path: Path) -> None:
    drainer, _ = drainer_with([], tmp_path)

    def broken(*, now: float | None = None) -> dict[str, object]:
        raise OSError("memory-data vanished")

    drainer.flush_once = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        drainer.flush(max_rows=5, budget_seconds=60)


def test_the_remember_drain_timeout_covers_a_full_row_past_the_budget(tmp_path: Path) -> None:
    # One row is up to 4 memory-data calls at --timeout 5; a row started just
    # before the budget ends must finish before the process is killed.
    env = {
        "MUSUBI_ACTOR": "alice",
        "MUSUBI_PRESENCE": "alice/laptop",
        "MUSUBI_ZONE": "home",
        "MUSUBI_DELIVERY_MODE": "verified",
        "MUSUBI_HARNESS_BIN": "/opt/fake/musubi-harness",
        "MUSUBI_MEMORY_DATA_BIN": "/opt/fake/memory-data",
    }
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[3] == "remember":
            seen["id"] = argv[argv.index("--event-id") + 1]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"event_id": seen["id"], "state": "pending"}}), "")
        seen["budget"] = float(argv[argv.index("--budget-seconds") + 1])
        seen["per_call"] = float(argv[argv.index("--timeout") + 1])
        seen["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": {"state": "idle"}}), "")

    with patch.dict("os.environ", env, clear=True):
        runtime = PluginRuntime("batch-test", default_data_root=tmp_path)
        facade = PluginMcpFacade(runtime, source="claude-code", event_prefix="t", owner_label="t", server_name="t")
        with patch("musubi_harness.plugin_mcp.subprocess.run", fake_run):
            facade.call_tool(runtime.runtime_config(), "musubi_remember", {"content": "a fact"})
    assert seen["timeout"] > seen["budget"] + 4 * seen["per_call"]
