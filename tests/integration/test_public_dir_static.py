from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase.app import create_app
from ppbase.config import Settings


@pytest.mark.asyncio
async def test_public_dir_serves_root_index_and_files(tmp_path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_text("<html><body><h1>Public Home</h1></body></html>")
    (public_dir / "app.js").write_text("console.log('public-ok')")

    app = create_app(Settings(public_dir=str(public_dir)))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        index_response = await client.get("/")
        js_response = await client.get("/app.js")

    assert index_response.status_code == 200
    assert "text/html" in index_response.headers.get("content-type", "")
    assert "Public Home" in index_response.text

    assert js_response.status_code == 200
    assert js_response.text == "console.log('public-ok')"


@pytest.mark.asyncio
async def test_public_dir_returns_404_without_directory_index(tmp_path) -> None:
    public_dir = tmp_path / "public"
    nested_dir = public_dir / "nested"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "hello.txt").write_text("hello")

    app = create_app(Settings(public_dir=str(public_dir)))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        root_response = await client.get("/")
        directory_response = await client.get("/nested/")
        file_response = await client.get("/nested/hello.txt")

    assert root_response.status_code == 404
    assert root_response.text == ""

    assert directory_response.status_code == 404
    assert "Index of" not in directory_response.text

    assert file_response.status_code == 200
    assert file_response.text == "hello"


@pytest.mark.asyncio
async def test_public_dir_keeps_api_404_shape_for_missing_api_routes(tmp_path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir(parents=True, exist_ok=True)
    (public_dir / "index.html").write_text("<html>ok</html>")

    app = create_app(Settings(public_dir=str(public_dir)))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {
        "status": 404,
        "message": "Not Found",
        "data": {},
    }
