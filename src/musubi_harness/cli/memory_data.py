"""``musubi-memory-data``: a direct HTTP client for the Musubi operations the harness uses.

The harness drives every Musubi call through a ``memory-data`` subprocess
(``<bin> --json --timeout N musubi <command> ...``) and parses its JSON. Until
1.1.0 the only implementation was a private operator tool, so a plugin installed
anywhere else could not capture or recall. This module is a public, stdlib-only
implementation of exactly the subset the harness calls, with the same argv and
the same JSON on stdout, so the capture -> outbox -> drainer -> receipt ->
readback contract is unchanged:

    status | recent | search | get | capture-durable | receipt-lookup

Endpoint and credential come from the process environment the harness passes
to its child (``MUSUBI_API_URL``, ``MUSUBI_TOKEN``). Plugins fill those from
their own settings; users are not asked to export them.

Differences from the operator tool, all deliberately stricter:
- redirects are refused, so the bearer token is never sent to another URL;
- responses are capped at ``MAX_RESPONSE_BYTES``;
- only ``http``/``https`` URLs without credentials, query or fragment are used.

Errors print ``error: <message>`` on stderr and exit 2, like the operator tool.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RECALL_STATES = ["provisional", "matured", "promoted"]
SETTLED_STATES = ["matured", "promoted"]
GET_PLANES = {"episodic", "curated", "concepts", "artifacts"}
RECEIPT_OPERATION = "capture_episodic.bucket=capture"


class CliError(RuntimeError):
    """Expected failure with a user-facing message."""


class MusubiHTTPError(CliError):
    def __init__(self, status_code: int, method: str, path: str, detail: str) -> None:
        super().__init__(f"Musubi HTTP {status_code} {method.upper()} {path}: {detail[:600]}")
        self.status_code = status_code
        try:
            decoded = json.loads(detail)
        except json.JSONDecodeError:
            decoded = None
        self.payload = decoded if isinstance(decoded, dict) else None


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise CliError(
            f"Musubi answered {code} with a redirect; refusing to follow it so the token "
            "is not sent anywhere else. Set the Musubi URL to the final address."
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def base_url() -> str:
    raw = os.environ.get("MUSUBI_API_URL", "").strip().rstrip("/")
    if not raw:
        raise CliError("MUSUBI_API_URL is not configured")
    parts = urllib.parse.urlsplit(raw)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise CliError("MUSUBI_API_URL must be an http(s) URL without credentials, query or fragment")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


def token() -> str:
    value = os.environ.get("MUSUBI_TOKEN", "").strip()
    if not value:
        raise CliError("MUSUBI_TOKEN is not configured")
    return value


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def self_attested_token_claims(bearer: str) -> dict[str, object]:
    """Decode (without verifying) the claims of the token the server just authorized."""
    parts = bearer.split(".")
    if len(parts) != 3:
        raise CliError("Musubi token is not a JWT for receipt self-attestation")
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("Musubi token claims cannot be decoded for receipt self-attestation") from exc
    if not isinstance(payload, dict):
        raise CliError("Musubi token claims are not an object")
    issuer, subject, presence = payload.get("iss"), payload.get("sub"), payload.get("presence")
    scopes = payload.get("scope")
    if isinstance(scopes, str):
        effective = [part for part in scopes.split() if part]
    elif isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
        effective = scopes
    else:
        effective = []
    if not all(isinstance(value, str) and value for value in (issuer, subject, presence)):
        raise CliError("Musubi token lacks receipt observer identity claims")
    if not effective:
        raise CliError("Musubi token lacks receipt observer scopes")
    return {
        "attestation": "self_attested",
        "issuer": issuer,
        "subject": subject,
        "presence": presence,
        "effective_scopes": list(dict.fromkeys(effective)),
    }


def _send(
    method: str,
    path: str,
    *,
    body: bytes | None,
    content_type: str | None,
    query: dict[str, str] | None,
    extra_headers: dict[str, str] | None,
    timeout: float,
    bearer: str | None = None,
) -> dict[str, Any]:
    url = f"{base_url()}/{path.lstrip('/')}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {bearer or token()}"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", errors="replace")
        raise MusubiHTTPError(exc.code, method, path, detail) from exc
    except CliError:
        raise
    except Exception as exc:  # noqa: BLE001 - network failures become one clear message
        raise CliError(f"Musubi request failed {method.upper()} {path}: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CliError(f"Musubi response for {method.upper()} {path} exceeds {MAX_RESPONSE_BYTES} bytes")
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CliError(f"Musubi returned non-JSON for {method.upper()} {path}") from exc
    if not isinstance(decoded, dict):
        raise CliError(f"Musubi returned non-object JSON for {method.upper()} {path}")
    return decoded


def request_json(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    bearer: str | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    return _send(
        method,
        path,
        body=data,
        content_type="application/json" if data is not None else None,
        query=query,
        extra_headers=extra_headers,
        timeout=timeout,
        bearer=bearer,
    )


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _namespace(args: argparse.Namespace) -> str:
    # The harness always passes an explicit, owned namespace with --exact.
    if not args.namespace:
        raise CliError("--namespace is required")
    return str(args.namespace)


def cmd_status(args: argparse.Namespace) -> int:
    print_json(request_json("GET", "/ops/status", timeout=args.timeout))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {"namespace": _namespace(args), "mode": "recent", "limit": args.limit}
    tags = parse_csv(args.tags)
    if tags:
        body["tags"] = tags
    print_json(request_json("POST", "/retrieve", body=body, timeout=args.timeout))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "namespace": _namespace(args),
        "query_text": args.query,
        "mode": args.mode,
        "limit": args.limit,
        "state_filter": SETTLED_STATES if args.settled_only else RECALL_STATES,
    }
    planes = parse_csv(args.planes)
    if planes:
        body["planes"] = planes
    print_json(request_json("POST", "/retrieve", body=body, timeout=args.timeout))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    if args.plane not in GET_PLANES:
        raise CliError(f"unsupported plane for get: {args.plane}")
    payload = request_json(
        "GET",
        f"/{args.plane}/{urllib.parse.quote(args.object_id, safe='')}",
        query={"namespace": args.namespace},
        timeout=args.timeout,
    )
    print_json(payload)
    return 0


def cmd_capture_durable(args: argparse.Namespace) -> int:
    if bool(args.request_file) == bool(args.stdin):
        raise CliError("choose exactly one of --request-file or --stdin")
    if not 1 <= len(args.idempotency_key) <= 256:
        raise CliError("idempotency key must contain 1 to 256 characters")
    try:
        if args.request_file:
            with open(args.request_file, "rb") as handle:
                body = handle.read()
        else:
            body = sys.stdin.buffer.read()
    except OSError as exc:
        raise CliError(f"cannot read durable capture body: {exc}") from exc
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CliError("durable capture body must be a UTF-8 JSON object") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("namespace"), str):
        raise CliError("durable capture body must include namespace")
    if not body:
        raise CliError("durable capture body is empty")
    try:
        # Exact caller bytes: the receipt digest binds Content-Type and body.
        payload = _send(
            "POST",
            "/episodic",
            body=body,
            content_type="application/json",
            query=None,
            extra_headers={"Idempotency-Key": args.idempotency_key, "Idempotency-Receipt": "durable"},
            timeout=args.timeout,
        )
    except MusubiHTTPError as exc:
        error = exc.payload.get("error") if exc.payload is not None else None
        if (
            exc.status_code != 422
            or not isinstance(error, dict)
            or error.get("code") != "CONTENT_TOO_LARGE"
            or not isinstance(error.get("detail"), str)
        ):
            raise
        match = re.fullmatch(r"episodic content is ([1-9][0-9]*) UTF-8 bytes; the limit is ([1-9][0-9]*)", error["detail"])
        if match is None:
            raise CliError("CONTENT_TOO_LARGE response detail is not canonical") from exc
        content_bytes, limit_bytes = int(match.group(1)), int(match.group(2))
        if content_bytes <= limit_bytes:
            raise CliError("CONTENT_TOO_LARGE response byte counts are inconsistent") from exc
        print_json(
            {
                "terminal_rejection": {
                    "response_status": exc.status_code,
                    "error_code": error["code"],
                    "response_detail": error["detail"],
                    "content_bytes_server": content_bytes,
                    "limit_bytes": limit_bytes,
                    "observed_at": utc_timestamp(),
                }
            }
        )
        return 0
    object_id = payload.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        raise CliError("Musubi durable capture response did not include object_id")
    print_json(payload)
    return 0


def cmd_receipt_lookup(args: argparse.Namespace) -> int:
    if not args.namespace.strip():
        raise CliError("namespace is required")
    if not 1 <= len(args.idempotency_key) <= 256:
        raise CliError("idempotency key must contain 1 to 256 characters")
    if re.fullmatch(r"[0-9a-fA-F]{64}", args.request_digest) is None:
        raise CliError("request digest must be exactly 64 ASCII hexadecimal characters")
    bearer = token()
    payload = request_json(
        "POST",
        "/idempotency/receipts/lookup",
        body={
            "namespace": args.namespace,
            "method": "POST",
            "operation_id": args.operation_id,
            "idempotency_key": args.idempotency_key,
            "request_digest": args.request_digest.lower(),
        },
        timeout=args.timeout,
        bearer=bearer,
    )
    payload["receipt_observation"] = {
        "status": payload.get("status"),
        **self_attested_token_claims(bearer),
        "observed_at": utc_timestamp(),
        "namespace": args.namespace,
        "operation_id": args.operation_id,
        "request_digest": args.request_digest.lower(),
    }
    print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musubi-memory-data", description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")
    parser.add_argument("--timeout", type=float, default=10.0, help="Musubi HTTP timeout in seconds")
    areas = parser.add_subparsers(dest="area", required=True)
    musubi = areas.add_parser("musubi", help="Musubi operations")
    sub = musubi.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    recent = sub.add_parser("recent")
    recent.add_argument("--namespace")
    recent.add_argument("--exact", action="store_true")
    recent.add_argument("--limit", type=int, default=5)
    recent.add_argument("--tags")
    recent.set_defaults(func=cmd_recent)

    search = sub.add_parser("search")
    search.add_argument("--namespace")
    search.add_argument("--exact", action="store_true")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--mode", default="deep", choices=["fast", "deep", "blended"])
    search.add_argument("--planes")
    search.add_argument("--settled-only", action="store_true")
    search.set_defaults(func=cmd_search)

    get = sub.add_parser("get")
    get.add_argument("--plane", required=True)
    get.add_argument("--namespace", required=True)
    get.add_argument("--object-id", required=True)
    get.set_defaults(func=cmd_get)

    capture = sub.add_parser("capture-durable")
    capture.add_argument("--idempotency-key", required=True)
    capture.add_argument("--request-file")
    capture.add_argument("--stdin", action="store_true")
    capture.set_defaults(func=cmd_capture_durable)

    lookup = sub.add_parser("receipt-lookup")
    lookup.add_argument("--namespace", required=True)
    lookup.add_argument("--idempotency-key", required=True)
    lookup.add_argument("--request-digest", required=True)
    lookup.add_argument("--operation-id", default=RECEIPT_OPERATION, choices=[RECEIPT_OPERATION])
    lookup.set_defaults(func=cmd_receipt_lookup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
