"""Security regressions for persisted HTTP request metadata."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
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


def test_redact_url_query_fails_closed_for_url_userinfo_credentials() -> None:
    redacted = _redact_url_query(
        "https://admin:database-secret@example.test/backups?token=file-secret"
    )

    assert redacted == "[REDACTED URL CREDENTIALS]"
    assert "database-secret" not in redacted
    assert "file-secret" not in redacted


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
