#!/usr/bin/env python3
"""Repair one PPBase wheel and seal its native-payload provenance."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Any


TOOLS = ("pg_dump", "pg_restore", "psql")
PROVENANCE = "ppbase/_vendor/postgresql/PROVENANCE.json"
PLATFORM_LIBRARY_NAMES = ("libpq", "libssl", "libcrypto")


class WheelRepairError(RuntimeError):
    """Raised when wheel repair leaves an incomplete native payload."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _is_shared_library(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return ".so" in name or name.endswith(".dylib")


def _run_repair(
    platform_name: str,
    wheel: Path,
    output: Path,
) -> tuple[Path, dict[str, str]]:
    if platform_name == "linux":
        tool_name = "auditwheel"
        machine = platform.machine().lower()
        architecture = {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "aarch64",
            "arm64": "aarch64",
        }.get(machine)
        if architecture is None:
            raise WheelRepairError(
                f"unsupported Linux wheel repair architecture: {machine}"
            )
        version_command = (sys.executable, "-m", "auditwheel", "--version")
        command = (
            sys.executable,
            "-m",
            "auditwheel",
            "repair",
            "--plat",
            f"manylinux_2_28_{architecture}",
            "--wheel-dir",
            str(output),
            str(wheel),
        )
    else:
        tool_name = "delocate"
        executable = shutil.which("delocate-wheel")
        if executable is None:
            raise WheelRepairError("delocate-wheel is unavailable")
        version_command = (executable, "--version")
        command = (executable, "-v", "-w", str(output), str(wheel))
    version_result = subprocess.run(
        version_command,
        check=True,
        capture_output=True,
        text=True,
    )
    version_output = (version_result.stdout or version_result.stderr).strip()
    if not version_output:
        raise WheelRepairError(f"cannot identify {tool_name} version")
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)
    wheels = sorted(output.glob("ppbase-*.whl"))
    if len(wheels) != 1:
        raise WheelRepairError(
            f"repair produced {len(wheels)} wheels instead of exactly one"
        )
    return wheels[0], {"name": tool_name, "versionOutput": version_output}


def _read_wheel(path: Path) -> tuple[dict[str, bytes], dict[str, zipfile.ZipInfo]]:
    values: dict[str, bytes] = {}
    infos: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(path, "r") as wheel:
        duplicate_names = len(wheel.namelist()) != len(set(wheel.namelist()))
        if duplicate_names:
            raise WheelRepairError("wheel contains duplicate member names")
        failed = wheel.testzip()
        if failed is not None:
            raise WheelRepairError(f"wheel member failed CRC validation: {failed}")
        for info in wheel.infolist():
            if info.is_dir():
                continue
            values[info.filename] = wheel.read(info.filename)
            infos[info.filename] = info
    return values, infos


def _find_dist_info(values: dict[str, bytes]) -> tuple[str, str]:
    wheel_files = [name for name in values if name.endswith(".dist-info/WHEEL")]
    record_files = [name for name in values if name.endswith(".dist-info/RECORD")]
    if len(wheel_files) != 1 or len(record_files) != 1:
        raise WheelRepairError("wheel must contain one WHEEL and one RECORD")
    prefix = wheel_files[0].removesuffix("WHEEL")
    if record_files[0] != prefix + "RECORD":
        raise WheelRepairError("wheel metadata directories are inconsistent")
    return wheel_files[0], record_files[0]


def _validate_tool_member(
    values: dict[str, bytes],
    infos: dict[str, zipfile.ZipInfo],
    name: str,
) -> str:
    path = f"ppbase/_vendor/postgresql/bin/{name}"
    data = values.get(path)
    info = infos.get(path)
    if data is None or info is None or not data:
        raise WheelRepairError(f"wheel is missing PostgreSQL executable {path}")
    mode = info.external_attr >> 16
    if not mode & 0o111:
        raise WheelRepairError(f"wheel member is not executable: {path}")
    return path


def _seal_provenance(
    path: Path,
    *,
    repair_tool: dict[str, str],
) -> None:
    values, infos = _read_wheel(path)
    wheel_metadata, record_path = _find_dist_info(values)
    try:
        provenance = json.loads(values[PROVENANCE].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError) as exc:
        raise WheelRepairError("wheel has invalid PostgreSQL provenance") from exc
    if provenance.get("schemaVersion") != 1:
        raise WheelRepairError("wheel PostgreSQL provenance schema is unsupported")

    tool_paths = [_validate_tool_member(values, infos, name) for name in TOOLS]
    library_paths = sorted(name for name in values if _is_shared_library(name))
    basenames = [PurePosixPath(name).name.lower() for name in library_paths]
    for required in PLATFORM_LIBRARY_NAMES:
        if not any(required in name for name in basenames):
            raise WheelRepairError(
                f"repaired wheel did not bundle required shared library {required}"
            )

    wheel_text = values[wheel_metadata].decode("utf-8", errors="strict")
    tags = re.findall(r"^Tag:\s*(\S+)\s*$", wheel_text, flags=re.MULTILINE)
    if not tags or any(tag.startswith("py3-none-any") for tag in tags):
        raise WheelRepairError("repaired wheel is not platform tagged")

    artifact_paths = [*tool_paths, *library_paths]
    declared_tools = provenance.get("tools")
    if not isinstance(declared_tools, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        for item in declared_tools
    ):
        raise WheelRepairError("wheel provenance has malformed tool declarations")
    repaired_tools = {item["name"]: item for item in declared_tools}
    if set(repaired_tools) != set(TOOLS):
        raise WheelRepairError("wheel provenance does not declare exactly three tools")
    for name, member in zip(TOOLS, tool_paths, strict=True):
        repaired_tools[name].update(
            {
                "path": member.removeprefix("ppbase/_vendor/postgresql/"),
                "sha256": _digest(values[member]),
                "size": len(values[member]),
            }
        )
    provenance["tools"] = [repaired_tools[name] for name in TOOLS]
    provenance["repair"] = {
        "status": "complete",
        "tool": repair_tool,
        "wheelTags": tags,
    }
    provenance["artifacts"] = [
        {
            "path": member,
            "sha256": _digest(values[member]),
            "size": len(values[member]),
        }
        for member in artifact_paths
    ]
    values[PROVENANCE] = (
        json.dumps(provenance, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for member in sorted(values):
        if member == record_path:
            continue
        data = values[member]
        writer.writerow((member, _record_digest(data), str(len(data))))
    writer.writerow((record_path, "", ""))
    values[record_path] = record_buffer.getvalue().encode("utf-8")

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary, "w", allowZip64=True) as output:
        for member in sorted(values):
            info = infos[member]
            output.writestr(info, values[member])
    os.replace(temporary, path)
    with zipfile.ZipFile(path, "r") as verified:
        failed = verified.testzip()
        if failed is not None:
            raise WheelRepairError(f"sealed wheel member failed CRC validation: {failed}")


def repair(args: argparse.Namespace) -> Path:
    wheel = args.wheel.resolve()
    if not wheel.is_file() or wheel.is_symlink() or wheel.suffix != ".whl":
        raise WheelRepairError(f"input wheel is not a regular file: {wheel}")
    dest_dir = args.dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ppbase-wheel-repair-") as temporary:
        repaired, repair_tool = _run_repair(args.platform, wheel, Path(temporary))
        _seal_provenance(
            repaired,
            repair_tool=repair_tool,
        )
        destination = dest_dir / repaired.name
        if destination.exists():
            destination.unlink()
        shutil.move(repaired, destination)
    print(f"repaired and verified {destination}")
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux", "macos"), required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--dest-dir", type=Path, required=True)
    return parser


def main() -> int:
    try:
        repair(_parser().parse_args())
    except (WheelRepairError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
