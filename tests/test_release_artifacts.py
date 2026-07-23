from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path
import zipfile

import pytest

from scripts import verify_release_artifacts as verifier
from scripts import verify_pypi_release as pypi_verifier


VERSION = "0.3.0"
PLATFORMS = {
    "manylinux_2_28_x86_64": ("Linux", "x86_64", "auditwheel"),
    "manylinux_2_28_aarch64": ("Linux", "aarch64", "auditwheel"),
    "macosx_15_0_x86_64": ("Darwin", "x86_64", "delocate"),
    "macosx_15_0_arm64": ("Darwin", "arm64", "delocate"),
}


def _record(path: str, data: bytes) -> dict[str, object]:
    return {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _write_member(
    wheel: zipfile.ZipFile,
    path: str,
    data: bytes,
    *,
    executable: bool = False,
) -> None:
    info = zipfile.ZipInfo(path)
    info.create_system = 3
    info.external_attr = (
        stat.S_IFREG | (0o755 if executable else 0o644)
    ) << 16
    wheel.writestr(info, data)


def _build_wheel(
    directory: Path,
    platform_tag: str,
    *,
    target_override: tuple[str, str] | None = None,
) -> Path:
    system, machine, repair_tool = PLATFORMS[platform_tag]
    if target_override is not None:
        system, machine = target_override
    wheel_tag = f"py3-none-{platform_tag}"
    filename = f"ppbase-{VERSION}-{wheel_tag}.whl"
    destination = directory / filename
    prefix = f"ppbase-{VERSION}.dist-info"

    tools = {
        name: f"synthetic-{name}-{platform_tag}".encode()
        for name in verifier.TOOLS
    }
    library_suffix = ".dylib" if system == "Darwin" else ".so.5"
    libraries = {
        f"ppbase.libs/libpq{library_suffix}": b"synthetic-libpq",
        f"ppbase.libs/libssl{library_suffix}": b"synthetic-libssl",
        f"ppbase.libs/libcrypto{library_suffix}": b"synthetic-libcrypto",
    }
    artifacts = {
        **{
            f"ppbase/_vendor/postgresql/bin/{name}": data
            for name, data in tools.items()
        },
        **libraries,
    }
    licenses = {
        "licenses/PostgreSQL-COPYRIGHT.txt": b"PostgreSQL license",
        "licenses/OpenSSL-LICENSE.txt": b"OpenSSL license",
        "licenses/Zlib-LICENSE.txt": b"zlib license",
    }
    source_lock = json.loads(
        (Path(__file__).resolve().parents[1] / "scripts" / "postgresql-source.json").read_text(
            encoding="utf-8"
        )
    )
    target = {"system": system, "machine": machine}
    if platform_tag.startswith("macosx_"):
        target["macosDeploymentTarget"] = "15.0"
    provenance = {
        "schemaVersion": 1,
        "component": {"version": source_lock["version"]},
        "source": {
            "archive": source_lock["archive"],
            "url": source_lock["url"],
            "sha256": source_lock["sha256"],
        },
        "build": {
            "target": target,
            "dependencyVersions": {"openssl": "3.0", "zlib": "1.3"},
        },
        "tools": [
            {
                "name": name,
                "path": f"bin/{name}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            for name, data in tools.items()
        ],
        "licenses": [
            _record(relative, data)
            for relative, data in licenses.items()
        ],
        "repair": {
            "status": "complete",
            "tool": {"name": repair_tool, "versionOutput": f"{repair_tool} test"},
            "wheelTags": [wheel_tag],
        },
        "artifacts": [
            _record(path, data)
            for path, data in artifacts.items()
        ],
    }

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        _write_member(
            wheel,
            f"{prefix}/METADATA",
            f"Metadata-Version: 2.4\nName: ppbase\nVersion: {VERSION}\n".encode(),
        )
        _write_member(
            wheel,
            f"{prefix}/WHEEL",
            (
                "Wheel-Version: 1.0\n"
                "Generator: synthetic-test\n"
                "Root-Is-Purelib: false\n"
                f"Tag: {wheel_tag}\n"
            ).encode(),
        )
        _write_member(wheel, "ppbase/admin/dist/index.html", b"<html></html>")
        _write_member(wheel, "ppbase/admin/dist/assets/app.js", b"export {}")
        for name, data in tools.items():
            _write_member(
                wheel,
                f"ppbase/_vendor/postgresql/bin/{name}",
                data,
                executable=True,
            )
        for path, data in libraries.items():
            _write_member(wheel, path, data)
        for relative, data in licenses.items():
            _write_member(
                wheel,
                f"ppbase/_vendor/postgresql/{relative}",
                data,
            )
        _write_member(
            wheel,
            verifier.PROVENANCE,
            (json.dumps(provenance, sort_keys=True) + "\n").encode(),
        )
    return destination


def _build_release(directory: Path) -> None:
    for platform_tag in PLATFORMS:
        _build_wheel(directory, platform_tag)
    _build_sdist(directory)


def _build_sdist(directory: Path, *, guarded: bool = True) -> Path:
    destination = directory / f"ppbase-{VERSION}.tar.gz"
    prefix = f"ppbase-{VERSION}"
    setup_source = (
        "PPBASE_BUILD_PLATFORM_WHEEL = '1'\n"
        "MESSAGE = 'No prebuilt ppbase wheel supports this platform'\n"
        if guarded
        else "from setuptools import setup\nsetup()\n"
    )
    files = {
        "PKG-INFO": f"Metadata-Version: 2.4\nName: ppbase\nVersion: {VERSION}\n",
        "setup.py": setup_source,
        "pyproject.toml": "[project]\nname = 'ppbase'\n",
    }
    with tarfile.open(destination, "w:gz") as archive:
        for relative, value in files.items():
            data = value.encode()
            info = tarfile.TarInfo(f"{prefix}/{relative}")
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return destination


def test_verify_accepts_one_ppbase_version_with_four_platform_wheels(
    tmp_path: Path,
) -> None:
    _build_release(tmp_path)

    observed = verifier.verify(
        argparse.Namespace(directory=tmp_path, release_tag=f"v{VERSION}")
    )

    assert set(observed) == {*verifier.EXPECTED_PLATFORMS, "source"}
    assert len(set(observed.values())) == 5
    assert all(
        observed[key].startswith(f"ppbase-{VERSION}-py3-none-")
        for key in verifier.EXPECTED_PLATFORMS
    )


def test_verify_rejects_release_tag_that_differs_from_project_version(
    tmp_path: Path,
) -> None:
    _build_release(tmp_path)

    with pytest.raises(verifier.ArtifactVerificationError, match="must be v0.3.0"):
        verifier.verify(
            argparse.Namespace(directory=tmp_path, release_tag="v0.3.1")
        )


def test_verify_rejects_provenance_that_contradicts_platform_tag(
    tmp_path: Path,
) -> None:
    for platform_tag in PLATFORMS:
        _build_wheel(
            tmp_path,
            platform_tag,
            target_override=("Darwin", "arm64")
            if platform_tag == "manylinux_2_28_x86_64"
            else None,
        )
    _build_sdist(tmp_path)

    with pytest.raises(
        verifier.ArtifactVerificationError,
        match="build target contradicts its platform tag",
    ):
        verifier.verify(argparse.Namespace(directory=tmp_path, release_tag=None))


@pytest.mark.parametrize(
    "platform_tag",
    (
        "manylinux_2_34_x86_64",
        "macosx_10_9_x86_64",
        "manylinux_2_28_x86_64.musllinux_1_2_x86_64",
        "win_amd64",
    ),
)
def test_verify_rejects_platform_tags_outside_the_release_contract(
    platform_tag: str,
) -> None:
    with pytest.raises(
        verifier.ArtifactVerificationError,
        match="unsupported or ambiguous",
    ):
        verifier._platform_key(platform_tag)


def test_verify_rejects_source_distribution_without_platform_guard(
    tmp_path: Path,
) -> None:
    for platform_tag in PLATFORMS:
        _build_wheel(tmp_path, platform_tag)
    _build_sdist(tmp_path, guarded=False)

    with pytest.raises(
        verifier.ArtifactVerificationError,
        match="unsupported-platform guard",
    ):
        verifier.verify(argparse.Namespace(directory=tmp_path, release_tag=None))


def test_pypi_verifier_requires_exact_remote_file_hashes() -> None:
    local_files = {"ppbase-0.3.0.tar.gz": "a" * 64}
    matching = {
        "info": {"name": "ppbase", "version": VERSION},
        "urls": [
            {
                "filename": "ppbase-0.3.0.tar.gz",
                "digests": {"sha256": "a" * 64},
            }
        ],
    }
    pypi_verifier._verify_payload(
        matching,
        expected_version=VERSION,
        local_files=local_files,
    )

    matching["urls"][0]["digests"]["sha256"] = "b" * 64
    with pytest.raises(
        pypi_verifier.PyPIReleaseVerificationError,
        match="SHA-256 differs",
    ):
        pypi_verifier._verify_payload(
            matching,
            expected_version=VERSION,
            local_files=local_files,
        )
