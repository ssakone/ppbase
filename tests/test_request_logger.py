"""Security regressions for persisted HTTP request metadata."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest

from ppbase.middleware import request_logger as request_logger_module
from ppbase.middleware.request_logger import RequestLoggerMiddleware, _redact_url_query


@pytest.mark.parametrize(
    ("malformed_url", "secret"),
    [
        ("https://[broken.example/backups?token=ipv6-secret", "ipv6-secret"),
        (
            "https://example.test/backups?page=\udcff&token=unicode-secret",
            "unicode-secret",
        ),
    ],
)
def test_redact_url_query_fails_closed_for_malformed_urls(
    malformed_url: str,
    secret: str,
) -> None:
    redacted = _redact_url_query(malformed_url)

    assert redacted == "[REDACTED MALFORMED URL]"
    assert secret not in redacted


def test_redact_url_query_never_persists_fragment_hidden_query_fields() -> None:
    redacted = _redact_url_query(
        "https://example.test/backups?page=2#fragment&token=file-token-secret"
    )

    assert "file-token-secret" not in redacted
    assert urlsplit(redacted).fragment == ""


def test_redact_url_query_snapshots_str_subclasses_before_parsing() -> None:
    class MisleadingURL(str):
        def __str__(self) -> str:
            return "https://safe.example/no-query"

        def lstrip(self, *_args: object, **_kwargs: object) -> str:
            return "https://safe.example/no-query"

    redacted = _redact_url_query(
        MisleadingURL(
            "https://example.test/backups?page=2&token=subclass-secret"
        )
    )

    assert type(redacted) is str
    assert "subclass-secret" not in redacted
    assert parse_qs(urlsplit(redacted).query, keep_blank_values=True) == {
        "page": ["2"],
        "token": ["[REDACTED]"],
    }


def test_redact_url_query_fails_closed_for_url_userinfo_credentials() -> None:
    redacted = _redact_url_query(
        "https://admin:database-secret@example.test/backups?token=file-secret"
    )

    assert redacted == "[REDACTED URL CREDENTIALS]"
    assert "database-secret" not in redacted
    assert "file-secret" not in redacted


@pytest.mark.asyncio
async def test_request_logger_redacts_token_hidden_after_raw_query_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-query-file-token-secret"
    captured: dict[str, object] = {}
    persisted = asyncio.Event()
    seen_token: str | None = None

    async def capture_log(_app_state, entry: dict) -> None:
        captured.update(entry)
        persisted.set()

    monkeypatch.setattr(request_logger_module, "_write_log", capture_log)

    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, bool]:
        nonlocal seen_token
        seen_token = request.query_params.get("token")
        return {"ok": True}

    app.add_middleware(RequestLoggerMiddleware)

    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        return None

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": f"page=2#fragment&token={secret}".encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "state": {},
        },
        receive,
        send,
    )

    await asyncio.wait_for(persisted.wait(), timeout=1.0)
    assert seen_token == secret
    assert secret not in str(captured["url"])


@pytest.mark.asyncio
async def test_request_logger_does_not_fail_on_malformed_referer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    persisted = asyncio.Event()

    async def capture_log(_app_state, entry: dict) -> None:
        captured.update(entry)
        persisted.set()

    monkeypatch.setattr(request_logger_module, "_write_log", capture_log)

    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(RequestLoggerMiddleware)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/probe",
            headers={
                "referer": "https://[broken.example/backups?token=referer-secret"
            },
        )

    assert response.status_code == 200
    await asyncio.wait_for(persisted.wait(), timeout=1.0)
    assert captured["referer"] == "[REDACTED MALFORMED URL]"


@pytest.mark.asyncio
async def test_request_logger_redacts_sensitive_query_values_from_persisted_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    persisted = asyncio.Event()

    async def capture_log(_app_state, entry: dict) -> None:
        captured.update(entry)
        persisted.set()

    monkeypatch.setattr(request_logger_module, "_write_log", capture_log)

    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(RequestLoggerMiddleware)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/probe",
            params=[
                ("page", "2"),
                ("token", "header.payload.signature"),
                ("access_token", "oauth-secret"),
            ],
            headers={
                "referer": "https://dashboard.test/backups?token=referer-secret"
            },
        )

    assert response.status_code == 200
    await asyncio.wait_for(persisted.wait(), timeout=1.0)

    persisted_url = str(captured["url"])
    assert "header.payload.signature" not in persisted_url
    assert "oauth-secret" not in persisted_url
    assert "referer-secret" not in str(captured["referer"])
    query = parse_qs(urlsplit(persisted_url).query, keep_blank_values=True)
    assert query == {
        "page": ["2"],
        "token": ["[REDACTED]"],
        "access_token": ["[REDACTED]"],
    }
