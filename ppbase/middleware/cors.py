"""CORS middleware configuration."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def setup_cors(app: FastAPI, origins: list[str] | None = None) -> None:
    """Add CORS middleware to the FastAPI application.

    Args:
        app: The FastAPI application instance.
        origins: Allowed origins. Defaults to ``["*"]``.
    """
    resolved_origins = origins or ["*"]
    allow_credentials = "*" not in resolved_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
