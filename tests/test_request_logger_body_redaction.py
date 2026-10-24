"""Request bodies stored by the request logger never hold a password or token.

The PocketBase 0.22 admin password reset routes read urlencoded bodies
(``token``, ``password``), which the logger used to store as raw text, like any
JSON body it could not parse.  A body beyond Python's integer or nesting limits
used to turn the already handled request into a 500.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, Request

import ppbase.middleware.request_logger as request_logger_module
from ppbase.middleware.request_logger import RequestLoggerMiddleware

SECRET = "synthetic-secret-value-123"


async def _logged(
    body: bytes, content_type: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, dict]:
    captured: dict = {}
    persisted = asyncio.Event()

    async def capture_log(_app_state, entry: dict) -> None:
        captured.update(entry)
        persisted.set()

    monkeypatch.setattr(request_logger_module, "_write_log", capture_log)
    app = FastAPI()

    @app.post("/probe")
    async def probe(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_middleware(RequestLoggerMiddleware)
    sent: list[dict] = []
    chunks = [body]

    async def receive() -> dict:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(), "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", content_type.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "state": {},
        },
        receive,
        send,
    )
    await asyncio.wait_for(persisted.wait(), timeout=1.0)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    return status, captured


@pytest.mark.asyncio
async def test_urlencoded_bodies_are_redacted_key_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"password": SECRET, "email": "a@b.co"})
    body = (
        f"token={SECRET}&password={SECRET}&pa%C5%BF%C5%BFword={SECRET}&passwordConfirm={SECRET}"
        f"&x=1;y={SECRET}&email=nobody%40c11.test&%40jsonPayload={payload}"
    ).encode()
    status, entry = await _logged(body, "application/x-www-form-urlencoded", monkeypatch)
    assert status == 200
    stored = entry["request_body"]
    assert SECRET not in stored
    assert "email=nobody%40c11.test" in stored
    assert "token=%5BREDACTED%5D" in stored


@pytest.mark.asyncio
async def test_malformed_json_is_never_stored_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    status, entry = await _logged(
        b'{"password": "' + SECRET.encode() + b'", ', "application/json", monkeypatch
    )
    assert status == 200
    assert entry["request_body"] == "[REDACTED MALFORMED BODY]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"x": ' + b"9" * 5000 + b', "password": "' + SECRET.encode() + b'"}',
        b'{"x": ' + b"[" * 3000 + b"]" * 3000 + b', "password": "' + SECRET.encode() + b'"}',
    ],
    ids=["digits-over-python-limit", "deep-nesting"],
)
async def test_unloggable_json_keeps_the_response(
    body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, entry = await _logged(body, "application/json", monkeypatch)
    assert status == 200
    assert SECRET not in json.dumps(entry["request_body"])


@pytest.mark.asyncio
async def test_long_s_keys_are_redacted_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"paſſword": SECRET, "toKen": SECRET, "email": "a@b.co"}).encode()
    status, entry = await _logged(body, "application/json", monkeypatch)
    assert status == 200
    assert entry["request_body"] == {
        "paſſword": "[REDACTED]",
        "toKen": "[REDACTED]",
        "email": "a@b.co",
    }
