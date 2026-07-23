#!/usr/bin/env python3
"""Smoke-test the installed PPBase platform wheel with an empty PATH."""

from __future__ import annotations

from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


TOOLS = ("pg_dump", "pg_restore", "psql")


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    sys.path = [
        entry
        for entry in sys.path
        if Path(entry or os.getcwd()).resolve() != project_root
    ]
    distribution = metadata.distribution("ppbase")
    if distribution.metadata["Name"].lower() != "ppbase":
        raise RuntimeError("installed distribution is not the ppbase project")
    from ppbase.backup.tools import resolve_postgres_tool

    results: dict[str, str] = {}
    payload_roots: set[Path] = set()
    with tempfile.TemporaryDirectory(prefix="ppbase-empty-path-") as empty_path:
        clean_environment = os.environ.copy()
        clean_environment["PATH"] = empty_path
        os.environ["PATH"] = empty_path
        for name in TOOLS:
            executable = Path(resolve_postgres_tool(name, name))
            if not executable.is_absolute() or not executable.is_file():
                raise RuntimeError(f"{name} did not resolve to an installed wheel file")
            if executable.is_symlink() or not os.access(executable, os.X_OK):
                raise RuntimeError(f"unsafe bundled executable: {executable}")
            expected_suffix = Path("ppbase/_vendor/postgresql/bin") / name
            if not executable.as_posix().endswith(expected_suffix.as_posix()):
                raise RuntimeError(f"{name} unexpectedly resolved outside the wheel payload")
            installed_member = Path(
                distribution.locate_file(expected_suffix.as_posix())
            ).resolve()
            if executable.resolve() != installed_member:
                raise RuntimeError(
                    f"{name} resolved from the build tree instead of the installed wheel"
                )
            payload_roots.add(executable.parents[1])
            completed = subprocess.run(
                (str(executable), "--version"),
                env=clean_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            output = (completed.stdout or completed.stderr).strip()
            if "PostgreSQL" not in output:
                raise RuntimeError(f"unexpected {name} version output: {output}")
            results[name] = output

    if len(payload_roots) != 1:
        raise RuntimeError("PostgreSQL tools did not resolve from one wheel payload")
    payload_root = payload_roots.pop()
    provenance = json.loads(
        (payload_root / "PROVENANCE.json").read_text(encoding="utf-8")
    )
    if provenance.get("repair", {}).get("status") != "complete":
        raise RuntimeError("installed PostgreSQL payload was not sealed after wheel repair")
    client_version = provenance.get("component", {}).get("version")
    if not isinstance(client_version, str) or any(
        client_version not in output for output in results.values()
    ):
        raise RuntimeError("installed PostgreSQL tool versions contradict provenance")
    for license_name in (
        "PostgreSQL-COPYRIGHT.txt",
        "OpenSSL-LICENSE.txt",
        "Zlib-LICENSE.txt",
    ):
        license_path = payload_root / "licenses" / license_name
        if not license_path.is_file() or license_path.stat().st_size == 0:
            raise RuntimeError(f"installed payload is missing {license_name}")

    majors = {
        output.rsplit(" ", 1)[-1].split(".", 1)[0]
        for output in results.values()
    }
    if len(majors) != 1:
        raise RuntimeError(f"bundled PostgreSQL tools have different majors: {results}")
    print(
        json.dumps(
            {
                "project": distribution.metadata["Name"],
                "version": distribution.version,
                "tools": results,
                "pathWasEmpty": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
