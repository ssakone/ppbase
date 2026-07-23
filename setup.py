"""Setuptools hooks for PPBase's platform wheel payload.

PPBase itself is pure Python, but its supported PyPI wheels contain native
PostgreSQL client executables.  Marking those wheels as platform-specific is
therefore required for pip to select the correct file automatically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from setuptools import setup
from wheel.bdist_wheel import bdist_wheel


_ROOT = Path(__file__).resolve().parent
_VENDOR_ROOT = _ROOT / "ppbase" / "_vendor" / "postgresql"
_TOOLS = ("pg_dump", "pg_restore", "psql")


def _validate_platform_payload() -> None:
    if os.environ.get("PPBASE_BUILD_PLATFORM_WHEEL") != "1":
        raise RuntimeError(
            "No prebuilt ppbase wheel supports this platform. Supported "
            "targets are glibc 2.28+ Linux x86_64/aarch64 and macOS 15+ "
            "x86_64/arm64. PPBase does not fall back to an older universal "
            "release. Maintainers must build platform wheels with "
            "cibuildwheel and PPBASE_BUILD_PLATFORM_WHEEL=1."
        )

    for name in _TOOLS:
        executable = _VENDOR_ROOT / "bin" / name
        if (
            not executable.is_file()
            or executable.is_symlink()
            or not os.access(executable, os.X_OK)
        ):
            raise RuntimeError(
                f"verified PostgreSQL payload is missing executable {executable}"
            )

    provenance_path = _VENDOR_ROOT / "PROVENANCE.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"verified PostgreSQL payload is missing {provenance_path}"
        ) from exc
    if provenance.get("schemaVersion") != 1:
        raise RuntimeError("PostgreSQL payload provenance schema is unsupported")

    licenses = [
        path
        for path in (_VENDOR_ROOT / "licenses").glob("*")
        if path.is_file() and path.name != "README.md"
    ]
    if len(licenses) < 3:
        raise RuntimeError(
            "PostgreSQL payload must include PostgreSQL, OpenSSL, and zlib "
            "license texts"
        )


class PlatformBdistWheel(bdist_wheel):
    """Build one Python-independent wheel with the native platform tag."""

    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def run(self) -> None:
        _validate_platform_payload()
        super().run()

    def get_tag(self) -> tuple[str, str, str]:
        _python, _abi, platform_tag = super().get_tag()
        return "py3", "none", platform_tag


setup(cmdclass={"bdist_wheel": PlatformBdistWheel})
