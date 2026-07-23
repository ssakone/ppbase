#!/usr/bin/env python3
"""Build the pinned PostgreSQL client payload used by PPBase wheels.

The source archive is accepted only after its checked-in SHA-256 lock matches.
The script intentionally keeps the build prefix below ``--work-dir``: wheel
repair tools need it to resolve libpq and its transitive shared libraries, then
copy and rewrite them into the final wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from typing import Any, Iterable
import zlib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "scripts" / "postgresql-source.json"
DEFAULT_DEST = ROOT / "ppbase" / "_vendor" / "postgresql"
DEFAULT_WORK_DIR = ROOT / "build" / "postgresql-vendor"
DEFAULT_CACHE_DIR = ROOT / "build" / "postgresql-source-cache"
ZLIB_LICENSE = ROOT / "scripts" / "licenses" / "Zlib-LICENSE.txt"
SUPPORTED_TARGETS = {
    ("Linux", "x86_64"),
    ("Linux", "aarch64"),
    ("Darwin", "x86_64"),
    ("Darwin", "arm64"),
}


class VendorBuildError(RuntimeError):
    """Raised when the pinned native payload cannot be built safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VendorBuildError(f"cannot read PostgreSQL source lock {path}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise VendorBuildError("PostgreSQL source lock schema must be 1")

    required_strings = ("component", "version", "archive", "url", "sha256", "license")
    for key in required_strings:
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise VendorBuildError(f"PostgreSQL source lock has invalid {key}")
    if not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise VendorBuildError("PostgreSQL source SHA-256 must be lowercase hexadecimal")
    expected_url = (
        f"https://ftp.postgresql.org/pub/source/v{value['version']}/"
        f"{value['archive']}"
    )
    if value["url"] != expected_url:
        raise VendorBuildError("PostgreSQL source URL does not match the pinned version")
    if value["archive"] != f"postgresql-{value['version']}.tar.bz2":
        raise VendorBuildError("PostgreSQL source archive name is inconsistent")
    if value.get("expectedTools") != ["pg_dump", "pg_restore", "psql"]:
        raise VendorBuildError("PostgreSQL source lock must declare exactly three tools")
    configure_args = value.get("configureArgs")
    if (
        not isinstance(configure_args, list)
        or not configure_args
        or any(not isinstance(item, str) or not item.startswith("--") for item in configure_args)
    ):
        raise VendorBuildError("PostgreSQL configure arguments are invalid")
    epoch = value.get("sourceDateEpoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise VendorBuildError("PostgreSQL SOURCE_DATE_EPOCH is invalid")
    return value


def _download_locked_source(
    manifest: dict[str, Any],
    *,
    supplied_archive: Path | None,
    cache_dir: Path,
) -> Path:
    expected = str(manifest["sha256"])
    if supplied_archive is not None:
        archive = supplied_archive.resolve()
        if not archive.is_file() or archive.is_symlink():
            raise VendorBuildError(f"source archive is not a regular file: {archive}")
        actual = _sha256(archive)
        if actual != expected:
            raise VendorBuildError(
                f"source archive SHA-256 mismatch: expected {expected}, got {actual}"
            )
        return archive

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / str(manifest["archive"])
    if archive.exists():
        if archive.is_symlink() or not archive.is_file():
            raise VendorBuildError(f"unsafe source cache entry: {archive}")
        actual = _sha256(archive)
        if actual == expected:
            return archive
        archive.unlink()

    temporary = archive.with_suffix(archive.suffix + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        str(manifest["url"]),
        headers={"User-Agent": "ppbase-wheel-builder/1"},
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("xb") as output,
        ):
            shutil.copyfileobj(response, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        actual = _sha256(temporary)
        if actual != expected:
            raise VendorBuildError(
                f"downloaded source SHA-256 mismatch: expected {expected}, got {actual}"
            )
        os.replace(temporary, archive)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return archive


def _validate_tar_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise VendorBuildError(f"unsafe path in PostgreSQL source archive: {member.name}")
    if member.ischr() or member.isblk() or member.isfifo():
        raise VendorBuildError(f"special file in PostgreSQL source archive: {member.name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or ".." in target.parts:
            raise VendorBuildError(
                f"unsafe link in PostgreSQL source archive: {member.name}"
            )


def _extract_source(archive: Path, work_dir: Path, version: str) -> Path:
    source_root = work_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:bz2") as bundle:
        members = bundle.getmembers()
        for member in members:
            _validate_tar_member(member)
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(source_root, members=members, filter="data")
        else:  # pragma: no cover - Python 3.11 security backports expose data_filter
            bundle.extractall(source_root, members=members)
    source = source_root / f"postgresql-{version}"
    if not (source / "configure").is_file() or not (source / "COPYRIGHT").is_file():
        raise VendorBuildError("PostgreSQL source archive has an unexpected layout")
    return source


def _run(argv: Iterable[str], *, cwd: Path, env: dict[str, str]) -> None:
    command = tuple(str(part) for part in argv)
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _brew_openssl_prefix() -> Path | None:
    brew = shutil.which("brew")
    if brew is None:
        return None
    result = subprocess.run(
        (brew, "--prefix", "openssl@3"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    prefix = Path(result.stdout.strip())
    return prefix if prefix.is_dir() else None


def _build_environment(source: Path, work_dir: Path, epoch: int) -> dict[str, str]:
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    env["ZERO_AR_DATE"] = "1"
    env["CONFIG_SITE"] = "/dev/null"
    mapping = f"-ffile-prefix-map={work_dir}=./postgresql-build"
    env["CFLAGS"] = " ".join(filter(None, (env.get("CFLAGS", ""), "-O2 -g0", mapping)))
    env["CPPFLAGS"] = " ".join(
        filter(None, (env.get("CPPFLAGS", ""), f"-I{source / 'src' / 'include'}"))
    )

    if platform.system() == "Darwin":
        openssl = _brew_openssl_prefix()
        if openssl is None:
            raise VendorBuildError("Homebrew openssl@3 is required for macOS wheels")
        env["CPPFLAGS"] = " ".join(
            filter(None, (env["CPPFLAGS"], f"-I{openssl / 'include'}"))
        )
        env["LDFLAGS"] = " ".join(
            filter(None, (env.get("LDFLAGS", ""), f"-L{openssl / 'lib'}"))
        )
        pkgconfig = openssl / "lib" / "pkgconfig"
        env["PKG_CONFIG_PATH"] = os.pathsep.join(
            filter(None, (str(pkgconfig), env.get("PKG_CONFIG_PATH", "")))
        )
    return env


def _find_openssl_license() -> Path:
    candidates: list[Path] = []
    explicit = os.environ.get("OPENSSL_LICENSE_FILE")
    if explicit:
        candidates.append(Path(explicit))
    brew_prefix = _brew_openssl_prefix()
    if brew_prefix is not None:
        candidates.extend((brew_prefix / "LICENSE.txt", brew_prefix / "LICENSE"))
    for pattern in (
        "/usr/share/licenses/openssl*/LICENSE*",
        "/usr/share/doc/openssl*/copyright",
    ):
        candidates.extend(sorted(Path("/").glob(pattern.lstrip("/"))))
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise VendorBuildError(
        "cannot locate the OpenSSL license; set OPENSSL_LICENSE_FILE explicitly"
    )


def _dependency_versions() -> dict[str, str]:
    openssl_prefix = _brew_openssl_prefix()
    openssl = (
        openssl_prefix / "bin" / "openssl"
        if openssl_prefix is not None
        else Path(shutil.which("openssl") or "")
    )
    if not openssl.is_file():
        raise VendorBuildError("cannot identify the OpenSSL build dependency")
    versions = {
        "openssl": _first_line((str(openssl), "version")),
        "zlib": zlib.ZLIB_VERSION,
    }
    pkg_config = shutil.which("pkg-config")
    if pkg_config is not None:
        result = subprocess.run(
            (pkg_config, "--modversion", "zlib"),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            versions["zlib"] = result.stdout.strip()
    return versions


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _first_line(argv: tuple[str, ...]) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return (result.stdout or result.stderr).splitlines()[0].strip()


def _write_payload(
    *,
    source: Path,
    prefix: Path,
    dest: Path,
    manifest: dict[str, Any],
    configure_args: list[str],
) -> None:
    if dest == Path(dest.anchor) or dest == ROOT:
        raise VendorBuildError(f"refusing unsafe vendor destination {dest}")
    root_readme_path = dest / "README.md"
    licenses_readme_path = dest / "licenses" / "README.md"
    root_readme = (
        root_readme_path.read_text(encoding="utf-8")
        if root_readme_path.is_file() and not root_readme_path.is_symlink()
        else "# Private PostgreSQL wheel payload\n"
    )
    licenses_readme = (
        licenses_readme_path.read_text(encoding="utf-8")
        if licenses_readme_path.is_file() and not licenses_readme_path.is_symlink()
        else "License texts generated for the platform wheel.\n"
    )
    shutil.rmtree(dest, ignore_errors=True)
    (dest / "bin").mkdir(parents=True)
    (dest / "licenses").mkdir()

    tool_records: list[dict[str, Any]] = []
    for name in manifest["expectedTools"]:
        built = prefix / "bin" / name
        if not built.is_file() or built.is_symlink():
            raise VendorBuildError(f"PostgreSQL build did not produce {built}")
        target = dest / "bin" / name
        shutil.copy2(built, target)
        target.chmod(0o755)
        version_output = _first_line((str(target), "--version"))
        if str(manifest["version"]) not in version_output:
            raise VendorBuildError(f"unexpected {name} version output: {version_output}")
        record = _artifact(target, dest)
        record.update({"name": name, "versionOutput": version_output})
        tool_records.append(record)

    postgresql_license = dest / "licenses" / "PostgreSQL-COPYRIGHT.txt"
    openssl_license = dest / "licenses" / "OpenSSL-LICENSE.txt"
    zlib_license = dest / "licenses" / "Zlib-LICENSE.txt"
    shutil.copy2(source / "COPYRIGHT", postgresql_license)
    shutil.copy2(_find_openssl_license(), openssl_license)
    if not ZLIB_LICENSE.is_file() or ZLIB_LICENSE.is_symlink():
        raise VendorBuildError(f"checked-in zlib license is missing: {ZLIB_LICENSE}")
    shutil.copy2(ZLIB_LICENSE, zlib_license)

    compiler = os.environ.get("CC", "cc").split()[0]
    target_details = {
        "system": platform.system(),
        "machine": platform.machine(),
    }
    deployment_target = os.environ.get("MACOSX_DEPLOYMENT_TARGET")
    if deployment_target:
        target_details["macosDeploymentTarget"] = deployment_target
    provenance = {
        "schemaVersion": 1,
        "component": {
            "name": manifest["component"],
            "version": manifest["version"],
            "license": manifest["license"],
        },
        "source": {
            "archive": manifest["archive"],
            "url": manifest["url"],
            "sha256": manifest["sha256"],
        },
        "build": {
            "sourceDateEpoch": manifest["sourceDateEpoch"],
            "target": target_details,
            "compiler": _first_line((compiler, "--version")),
            "configureArgs": configure_args,
            "dependencyVersions": _dependency_versions(),
        },
        "tools": tool_records,
        "licenses": [
            _artifact(postgresql_license, dest),
            _artifact(openssl_license, dest),
            _artifact(zlib_license, dest),
        ],
        "repair": {"status": "pending"},
        "artifacts": [],
    }
    (dest / "PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dest / "README.md").write_text(root_readme, encoding="utf-8")
    (dest / "licenses" / "README.md").write_text(
        licenses_readme,
        encoding="utf-8",
    )


def build(args: argparse.Namespace) -> None:
    manifest_path = args.manifest.resolve()
    manifest = _load_manifest(manifest_path)
    configured_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if configured_epoch is not None and configured_epoch != str(manifest["sourceDateEpoch"]):
        raise VendorBuildError(
            "SOURCE_DATE_EPOCH does not match scripts/postgresql-source.json"
        )
    target = (platform.system(), platform.machine())
    if target not in SUPPORTED_TARGETS:
        raise VendorBuildError(f"unsupported wheel build target: {target[0]} {target[1]}")
    if args.jobs is not None and (
        isinstance(args.jobs, bool) or args.jobs < 1 or args.jobs > 64
    ):
        raise VendorBuildError("--jobs must be between 1 and 64")
    if args.check:
        print(
            json.dumps(
                {
                    "manifest": str(manifest_path),
                    "version": manifest["version"],
                    "sha256": manifest["sha256"],
                    "target": list(target),
                },
                sort_keys=True,
            )
        )
        return

    dest = args.dest.resolve()
    work_dir = args.work_dir.resolve()
    cache_dir = args.cache_dir.resolve()
    build_root = (ROOT / "build").resolve()
    if DEFAULT_DEST.is_symlink():
        raise VendorBuildError("private PPBase vendor destination must not be a symlink")
    if dest != DEFAULT_DEST.resolve():
        raise VendorBuildError(
            f"vendor destination must be the private PPBase payload: {DEFAULT_DEST}"
        )
    if work_dir == build_root or not work_dir.is_relative_to(build_root):
        raise VendorBuildError("work dir must be a child of the repository build directory")
    if cache_dir == build_root or not cache_dir.is_relative_to(build_root):
        raise VendorBuildError("cache dir must be a child of the repository build directory")
    if dest == work_dir or dest.is_relative_to(work_dir):
        raise VendorBuildError("vendor destination must not be inside the disposable work dir")
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True)
    archive = _download_locked_source(
        manifest,
        supplied_archive=args.archive,
        cache_dir=cache_dir,
    )
    source = _extract_source(archive, work_dir, str(manifest["version"]))
    prefix = work_dir / "prefix"
    env = _build_environment(source, work_dir, int(manifest["sourceDateEpoch"]))
    configure_args = [f"--prefix={prefix}", *manifest["configureArgs"]]
    _run((str(source / "configure"), *configure_args), cwd=source, env=env)

    jobs = args.jobs or min(4, max(1, os.cpu_count() or 1))
    make = ("make", f"-j{jobs}")
    for directory in ("src/interfaces/libpq", "src/bin/pg_dump", "src/bin/psql"):
        _run((*make, "-C", directory, "all"), cwd=source, env=env)
    for directory in ("src/interfaces/libpq", "src/bin/pg_dump", "src/bin/psql"):
        _run(("make", "-C", directory, "install"), cwd=source, env=env)

    runtime_env = env.copy()
    library_variable = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
    runtime_env[library_variable] = os.pathsep.join(
        filter(None, (str(prefix / "lib"), runtime_env.get(library_variable, "")))
    )
    for name in manifest["expectedTools"]:
        _run((str(prefix / "bin" / name), "--version"), cwd=source, env=runtime_env)

    _write_payload(
        source=source,
        prefix=prefix,
        dest=dest,
        manifest=manifest,
        configure_args=configure_args,
    )
    print(f"PostgreSQL {manifest['version']} client payload written to {dest}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--jobs", type=int)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the lock and current target without downloading or building",
    )
    return parser


def main() -> int:
    try:
        build(_parser().parse_args())
    except (VendorBuildError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
