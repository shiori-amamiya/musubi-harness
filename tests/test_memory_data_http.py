"""musubi-memory-data: the public HTTP client speaks memory-data's argv and JSON.

A fake Musubi server records every request, so each test asserts both what was
sent (method, path, headers, exact bytes) and what the harness will parse.
"""

from __future__ import annotations

import base64
import http.server
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from musubi_harness.cli import memory_data
from musubi_harness.plugin_runtime import PluginRuntime, RuntimeConfigError


def jwt(claims: dict[str, Any]) -> str:
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


TOKEN = jwt({"iss": "musubi", "sub": "alice/laptop", "presence": "alice/laptop", "scope": "alice/**:rw"})


class Fake:
    """A tiny Musubi: routes -> (status, headers, body); records requests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], tuple[int, dict[str, str], bytes]] = {}

    def reply(self, method: str, path: str, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
        self.routes[(method, path)] = (status, headers or {}, raw)


@contextmanager
def serve(fake: Fake) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
            status, headers, raw = fake.routes.get((self.command, path), (404, {}, b'{"error":"nf"}'))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _handle

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def run(url: str, *argv: str, stdin: bytes = b"", token: str = TOKEN) -> tuple[int, Any, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"MUSUBI_API_URL": url, "MUSUBI_TOKEN": token}
    stdin_obj = io.TextIOWrapper(io.BytesIO(stdin))
    with patch.dict("os.environ", env, clear=True), patch("sys.stdin", stdin_obj), redirect_stdout(out), redirect_stderr(err):
        code = memory_data.main(["--json", "--timeout", "3", "musubi", *argv])
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def test_status() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok", "components": {}})
    with serve(fake) as url:
        code, payload, _ = run(url, "status")
    assert (code, payload["status"]) == (0, "ok")
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_url_ending_in_v1_is_not_doubled() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok"})
    with serve(fake) as url:
        code, _, _ = run(url + "/v1/", "status")
    assert code == 0 and fake.requests[0]["path"] == "/v1/ops/status"


def test_recent_and_search_bodies_match_memory_data() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/retrieve", body={"results": []})
    with serve(fake) as url:
        run(url, "recent", "--namespace", "alice/laptop", "--exact", "--limit", "7", "--tags", "a, b")
        run(
            url,
            "search",
            "--namespace",
            "alice/laptop",
            "--exact",
            "--query",
            "tea",
            "--limit",
            "3",
            "--mode",
            "fast",
            "--planes",
            "episodic,curated",
        )
    recent, search = (json.loads(r["body"]) for r in fake.requests)
    assert recent == {"namespace": "alice/laptop", "mode": "recent", "limit": 7, "tags": ["a", "b"]}
    assert search == {
        "namespace": "alice/laptop",
        "query_text": "tea",
        "mode": "fast",
        "limit": 3,
        "state_filter": ["provisional", "matured", "promoted"],
        "planes": ["episodic", "curated"],
    }


def test_get_quotes_the_id_and_scopes_the_namespace() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/episodic/a%2Fb", body={"object_id": "a/b"})
    with serve(fake) as url:
        code, payload, _ = run(url, "get", "--plane", "episodic", "--namespace", "alice/laptop/episodic", "--object-id", "a/b")
    assert code == 0 and payload == {"object_id": "a/b"}
    assert fake.requests[0]["path"] == "/v1/episodic/a%2Fb?namespace=alice%2Flaptop%2Fepisodic"


def test_capture_durable_sends_the_exact_bytes_with_receipt_headers() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/episodic", status=202, body={"object_id": "obj1", "state": "provisional"})
    raw = b'{"namespace":"alice/laptop/episodic","content":"  exact\\r\\n bytes "}'
    with serve(fake) as url:
        code, payload, _ = run(url, "capture-durable", "--idempotency-key", "k1", "--stdin", stdin=raw)
    request = fake.requests[0]
    assert code == 0 and payload["object_id"] == "obj1"
    assert request["body"] == raw  # not re-serialised: the receipt digest binds these bytes
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Idempotency-Key"] == "k1"
    assert request["headers"]["Idempotency-Receipt"] == "durable"


def test_capture_durable_turns_content_too_large_into_a_terminal_rejection() -> None:
    fake = Fake()
    detail = "episodic content is 70000 UTF-8 bytes; the limit is 65536"
    fake.reply("POST", "/v1/episodic", status=422, body={"error": {"code": "CONTENT_TOO_LARGE", "detail": detail}})
    with serve(fake) as url:
        code, payload, _ = run(url, "capture-durable", "--idempotency-key", "k1", "--stdin", stdin=b'{"namespace":"n"}')
    rejection = payload["terminal_rejection"]
    assert code == 0
    assert (rejection["content_bytes_server"], rejection["limit_bytes"], rejection["response_status"]) == (70000, 65536, 422)


def test_receipt_lookup_attaches_self_attested_claims() -> None:
    fake = Fake()
    fake.reply("POST", "/v1/idempotency/receipts/lookup", body={"status": "committed", "object_id": "obj1"})
    digest = "AB" * 32
    with serve(fake) as url:
        code, payload, _ = run(
            url, "receipt-lookup", "--namespace", "alice/laptop/episodic", "--idempotency-key", "k1", "--request-digest", digest
        )
    sent = json.loads(fake.requests[0]["body"])
    observation = payload["receipt_observation"]
    assert code == 0 and sent["request_digest"] == digest.lower() and sent["method"] == "POST"
    assert observation["subject"] == "alice/laptop" and observation["attestation"] == "self_attested"
    assert observation["effective_scopes"] == ["alice/**:rw"] and observation["status"] == "committed"


def test_a_redirect_is_refused_and_the_token_never_leaves() -> None:
    other = Fake()
    other.reply("GET", "/v1/ops/status", body={"status": "stolen"})
    with serve(other) as elsewhere:
        first = Fake()
        first.reply("GET", "/v1/ops/status", status=302, headers={"Location": elsewhere + "/v1/ops/status"})
        with serve(first) as url:
            code, payload, err = run(url, "status")
    assert code == 2 and payload is None and "redirect" in err
    assert other.requests == []


def test_http_errors_exit_2_with_the_status_on_stderr() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", status=401, body={"error": "unauthorized"})
    with serve(fake) as url:
        code, _, err = run(url, "status")
    assert code == 2 and err.startswith("error: Musubi HTTP 401")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"MUSUBI_TOKEN": TOKEN}, "MUSUBI_API_URL is not configured"),
        ({"MUSUBI_API_URL": "http://127.0.0.1:9"}, "MUSUBI_TOKEN is not configured"),
        ({"MUSUBI_API_URL": "http://user:pw@host", "MUSUBI_TOKEN": TOKEN}, "without credentials"),
        ({"MUSUBI_API_URL": "ftp://host", "MUSUBI_TOKEN": TOKEN}, "http(s)"),
    ],
)
def test_bad_configuration_fails_before_any_network(env: dict[str, str], message: str) -> None:
    err = io.StringIO()
    with patch.dict("os.environ", env, clear=True), redirect_stderr(err), redirect_stdout(io.StringIO()):
        code = memory_data.main(["--json", "musubi", "status"])
    assert code == 2 and message in err.getvalue()


def test_runtime_falls_back_to_the_bundled_client_last(tmp_path: Path) -> None:
    runtime = PluginRuntime("harness-test", default_data_root=tmp_path)
    config = type("C", (), {"memory_data_bin": None, "harness_bin": None})()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bundled = bin_dir / "musubi-memory-data"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    with patch.dict("os.environ", {"PATH": str(bin_dir)}, clear=True):
        assert runtime.memory_data_bin(config) == str(bundled)
    # An operator memory-data on PATH still wins.
    operator = bin_dir / "memory-data"
    operator.write_text("#!/bin/sh\n")
    operator.chmod(0o755)
    with patch.dict("os.environ", {"PATH": str(bin_dir)}, clear=True):
        assert runtime.memory_data_bin(config) == str(operator)


def test_nothing_available_is_still_refused(tmp_path: Path) -> None:
    runtime = PluginRuntime("harness-test", default_data_root=tmp_path)
    config = type("C", (), {"memory_data_bin": None, "harness_bin": None})()
    with (
        patch.dict("os.environ", {"PATH": str(tmp_path)}, clear=True),
        patch("musubi_harness.plugin_runtime.sys.executable", str(tmp_path / "python")),
        pytest.raises(RuntimeConfigError, match="memory_data_unavailable"),
    ):
        runtime.memory_data_bin(config)


def test_an_opaque_token_works_as_a_bearer_but_not_for_receipt_lookup() -> None:
    fake = Fake()
    fake.reply("GET", "/v1/ops/status", body={"status": "ok"})
    fake.reply("POST", "/v1/idempotency/receipts/lookup", body={"status": "committed"})
    with serve(fake) as url:
        status_code, _, _ = run(url, "status", token="opaque-token")
        code, payload, err = run(
            url,
            "receipt-lookup",
            "--namespace",
            "alice/laptop/episodic",
            "--idempotency-key",
            "k1",
            "--request-digest",
            "ab" * 32,
            token="opaque-token",
        )
    assert status_code == 0
    assert code == 2 and payload is None and "not a JWT" in err
