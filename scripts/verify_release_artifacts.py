#!/usr/bin/env python3
"""Verify PPBase's four wheels and unsupported-platform source guard."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tomllib
import zipfile


EXPECTED_PLATFORMS = {
    "linux-x86_64": "manylinux_2_28_x86_64",
    "linux-aarch64": "manylinux_2_28_aarch64",
    "macos-x86_64": "macosx_15_0_x86_64",
    "macos-arm64": "macosx_15_0_arm64",
}
EXPECTED_BUILD_TARGETS = {
    "linux-x86_64": ("Linux", "x86_64"),
    "linux-aarch64": ("Linux", "aarch64"),
    "macos-x86_64": ("Darwin", "x86_64"),
    "macos-arm64": ("Darwin", "arm64"),
}
TOOLS = ("pg_dump", "pg_restore", "psql")
PROVENANCE = "ppbase/_vendor/postgresql/PROVENANCE.json"


class ArtifactVerificationError(RuntimeError):
    """Raised when a release could publish an incomplete or ambiguous set."""


def _project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    if project.get("name") != "ppbase":
        raise ArtifactVerificationError("pyproject project name must remain ppbase")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ArtifactVerificationError("pyproject version is invalid")
    return version


def _platform_key(platform_tag: str) -> str:
    matches = [
        name
        for name, expected_tag in EXPECTED_PLATFORMS.items()
        if platform_tag == expected_tag
    ]
    if len(matches) == 1:
        return matches[0]
    raise ArtifactVerificationError(
        f"wheel platform tag is unsupported or ambiguous: {platform_tag}"
    )


def _metadata_value(text: str, key: str) -> str:
    prefix = key + ":"
    matches = [line[len(prefix) :].strip() for line in text.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise ArtifactVerificationError(f"wheel metadata must contain one {key}")
    return matches[0]


def _verify_wheel(path: Path, expected_version: str) -> str:
    match = re.fullmatch(
        rf"ppbase-{re.escape(expected_version)}-py3-none-(.+)\.whl",
        path.name,
    )
    if match is None:
        raise ArtifactVerificationError(f"unexpected release filename: {path.name}")
    platform_key = _platform_key(match.group(1))

    with zipfile.ZipFile(path, "r") as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ArtifactVerificationError(f"duplicate wheel members in {path.name}")
        failed = wheel.testzip()
        if failed is not None:
            raise ArtifactVerificationError(f"CRC failure in {path.name}: {failed}")
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        wheel_files = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_files) != 1 or len(wheel_files) != 1:
            raise ArtifactVerificationError(f"invalid metadata layout in {path.name}")
        metadata = wheel.read(metadata_files[0]).decode("utf-8")
        wheel_metadata = wheel.read(wheel_files[0]).decode("utf-8")
        wheel_tags = re.findall(
            r"^Tag:\s*(\S+)\s*$",
            wheel_metadata,
            flags=re.MULTILINE,
        )
        parsed_tags = [tag.split("-", 2) for tag in wheel_tags]
        if any(
            len(parts) != 3 or parts[0] != "py3" or parts[1] != "none"
            for parts in parsed_tags
        ):
            raise ArtifactVerificationError(
                f"wheel metadata must use only py3-none platform tags in {path.name}"
            )
        filename_platforms = set(match.group(1).split("."))
        metadata_platforms = {
            parts[2]
            for parts in parsed_tags
        }
        if not wheel_tags or metadata_platforms != filename_platforms:
            raise ArtifactVerificationError(
                f"wheel filename and WHEEL tags disagree in {path.name}"
            )
        if _metadata_value(wheel_metadata, "Root-Is-Purelib").lower() != "false":
            raise ArtifactVerificationError(
                f"platform wheel is incorrectly marked pure in {path.name}"
            )
        if _metadata_value(metadata, "Name").lower() != "ppbase":
            raise ArtifactVerificationError(f"wrong project name in {path.name}")
        if _metadata_value(metadata, "Version") != expected_version:
            raise ArtifactVerificationError(f"wrong project version in {path.name}")
        if "ppbase/admin/dist/index.html" not in names or not any(
            name.startswith("ppbase/admin/dist/assets/") and not name.endswith("/")
            for name in names
        ):
            raise ArtifactVerificationError(
                f"{path.name} is missing the built PPBase Admin UI"
            )

        for tool in TOOLS:
            member = f"ppbase/_vendor/postgresql/bin/{tool}"
            try:
                info = wheel.getinfo(member)
            except KeyError as exc:
                raise ArtifactVerificationError(f"{path.name} is missing {member}") from exc
            if not info.external_attr >> 16 & 0o111:
                raise ArtifactVerificationError(f"{member} is not executable in {path.name}")

        try:
            provenance = json.loads(wheel.read(PROVENANCE))
        except (KeyError, ValueError) as exc:
            raise ArtifactVerificationError(
                f"{path.name} has invalid PostgreSQL provenance"
            ) from exc
        repair = provenance.get("repair", {})
        if repair.get("status") != "complete":
            raise ArtifactVerificationError(f"{path.name} was not sealed after repair")
        repair_tool = repair.get("tool")
        expected_repair_tool = (
            "auditwheel" if platform_key.startswith("linux-") else "delocate"
        )
        if (
            not isinstance(repair_tool, dict)
            or repair_tool.get("name") != expected_repair_tool
            or not isinstance(repair_tool.get("versionOutput"), str)
            or not repair_tool["versionOutput"]
        ):
            raise ArtifactVerificationError(
                f"{path.name} has invalid wheel-repair provenance"
            )
        if repair.get("wheelTags") != wheel_tags:
            raise ArtifactVerificationError(
                f"{path.name} wheel tags contradict repair provenance"
            )
        dependency_versions = provenance.get("build", {}).get("dependencyVersions")
        if not isinstance(dependency_versions, dict) or not all(
            isinstance(dependency_versions.get(name), str)
            and dependency_versions[name]
            for name in ("openssl", "zlib")
        ):
            raise ArtifactVerificationError(
                f"{path.name} is missing OpenSSL/zlib build provenance"
            )
        build_target = provenance.get("build", {}).get("target")
        expected_system, expected_machine = EXPECTED_BUILD_TARGETS[platform_key]
        if (
            not isinstance(build_target, dict)
            or build_target.get("system") != expected_system
            or build_target.get("machine") != expected_machine
        ):
            raise ArtifactVerificationError(
                f"{path.name} build target contradicts its platform tag"
            )
        if platform_key.startswith("macos-"):
            if build_target.get("macosDeploymentTarget") != "15.0":
                raise ArtifactVerificationError(
                    f"{path.name} has an unexpected macOS deployment target"
                )
        elif "macosDeploymentTarget" in build_target:
            raise ArtifactVerificationError(
                f"{path.name} contains macOS provenance for a Linux wheel"
            )
        tool_items = provenance.get("tools")
        if not isinstance(tool_items, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in tool_items
        ):
            raise ArtifactVerificationError(f"{path.name} has malformed tool provenance")
        tools_by_name = {item["name"]: item for item in tool_items}
        if set(tools_by_name) != set(TOOLS):
            raise ArtifactVerificationError(
                f"{path.name} does not declare exactly pg_dump, pg_restore, and psql"
            )
        for tool in TOOLS:
            member = f"ppbase/_vendor/postgresql/bin/{tool}"
            data = wheel.read(member)
            item = tools_by_name[tool]
            if item.get("path") != f"bin/{tool}":
                raise ArtifactVerificationError(
                    f"{path.name} has an invalid provenance path for {tool}"
                )
            if item.get("size") != len(data) or item.get(
                "sha256"
            ) != hashlib.sha256(data).hexdigest():
                raise ArtifactVerificationError(
                    f"{path.name} has stale post-repair provenance for {tool}"
                )
        source_lock_path = Path(__file__).with_name("postgresql-source.json")
        source_lock = json.loads(source_lock_path.read_text(encoding="utf-8"))
        if provenance.get("component", {}).get("version") != source_lock["version"]:
            raise ArtifactVerificationError(
                f"{path.name} PostgreSQL version does not match the source lock"
            )
        if provenance.get("source") != {
            "archive": source_lock["archive"],
            "url": source_lock["url"],
            "sha256": source_lock["sha256"],
        }:
            raise ArtifactVerificationError(
                f"{path.name} provenance does not match the checked-in source lock"
            )
        artifacts = provenance.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) < 6:
            raise ArtifactVerificationError(f"{path.name} has incomplete artifact provenance")
        artifact_paths = [
            item.get("path", "") for item in artifacts if isinstance(item, dict)
        ]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ArtifactVerificationError(f"{path.name} has duplicate artifact provenance")
        for item in artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ArtifactVerificationError(f"{path.name} has malformed artifact provenance")
            member = item["path"]
            try:
                data = wheel.read(member)
            except KeyError as exc:
                raise ArtifactVerificationError(
                    f"{path.name} provenance references missing {member}"
                ) from exc
            if item.get("size") != len(data) or item.get(
                "sha256"
            ) != hashlib.sha256(data).hexdigest():
                raise ArtifactVerificationError(
                    f"{path.name} provenance hash mismatch for {member}"
                )

        expected_licenses = {
            "licenses/PostgreSQL-COPYRIGHT.txt",
            "licenses/OpenSSL-LICENSE.txt",
            "licenses/Zlib-LICENSE.txt",
        }
        license_items = provenance.get("licenses")
        if not isinstance(license_items, list):
            raise ArtifactVerificationError(f"{path.name} has no license provenance")
        observed_licenses: set[str] = set()
        for item in license_items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ArtifactVerificationError(f"{path.name} has malformed license provenance")
            relative = item["path"]
            observed_licenses.add(relative)
            member = "ppbase/_vendor/postgresql/" + relative
            try:
                data = wheel.read(member)
            except KeyError as exc:
                raise ArtifactVerificationError(
                    f"{path.name} is missing bundled license {member}"
                ) from exc
            if item.get("size") != len(data) or item.get(
                "sha256"
            ) != hashlib.sha256(data).hexdigest():
                raise ArtifactVerificationError(
                    f"{path.name} license hash mismatch for {member}"
                )
        if observed_licenses != expected_licenses:
            raise ArtifactVerificationError(
                f"{path.name} does not contain the exact required license set"
            )

        library_names = [
            PurePosixPath(name).name.lower()
            for name in artifact_paths
            if ".so" in name.lower() or name.lower().endswith(".dylib")
        ]
        for library in ("libpq", "libssl", "libcrypto"):
            if not any(library in name for name in library_names):
                raise ArtifactVerificationError(
                    f"{path.name} provenance is missing {library}"
                )
    return platform_key


def _verify_sdist(path: Path, expected_version: str) -> None:
    expected_name = f"ppbase-{expected_version}.tar.gz"
    if path.name != expected_name:
        raise ArtifactVerificationError(
            f"unexpected source distribution filename: {path.name}"
        )
    prefix = f"ppbase-{expected_version}/"
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ArtifactVerificationError("source distribution has duplicate members")
        if any(
            name != prefix.rstrip("/")
            and not name.startswith(prefix)
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            for name in names
        ):
            raise ArtifactVerificationError("source distribution has an unsafe layout")

        def read(relative: str) -> str:
            member = archive.extractfile(prefix + relative)
            if member is None:
                raise ArtifactVerificationError(
                    f"source distribution is missing {relative}"
                )
            return member.read().decode("utf-8")

        metadata = read("PKG-INFO")
        setup_source = read("setup.py")
        read("pyproject.toml")
        if _metadata_value(metadata, "Name").lower() != "ppbase":
            raise ArtifactVerificationError("source distribution has the wrong project name")
        if _metadata_value(metadata, "Version") != expected_version:
            raise ArtifactVerificationError("source distribution has the wrong version")
        if (
            "PPBASE_BUILD_PLATFORM_WHEEL" not in setup_source
            or "No prebuilt ppbase wheel supports this platform" not in setup_source
        ):
            raise ArtifactVerificationError(
                "source distribution does not contain the unsupported-platform guard"
            )
        forbidden_prefix = prefix + "ppbase/_vendor/postgresql/bin/"
        forbidden_members = {
            prefix + "ppbase/_vendor/postgresql/PROVENANCE.json",
        }
        if any(name.startswith(forbidden_prefix) for name in names) or any(
            name in forbidden_members for name in names
        ):
            raise ArtifactVerificationError(
                "source distribution must not contain a host-native PostgreSQL payload"
            )


def verify(args: argparse.Namespace) -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    expected_version = _project_version(root)
    if args.release_tag is not None and args.release_tag != f"v{expected_version}":
        raise ArtifactVerificationError(
            f"release tag {args.release_tag!r} must be v{expected_version}"
        )

    directory = args.directory.resolve()
    wheels = sorted(directory.glob("*.whl"))
    source_distributions = sorted(directory.glob("*.tar.gz"))
    expected_files = {path.name for path in (*wheels, *source_distributions)}
    other_files = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name not in expected_files
    )
    if other_files:
        raise ArtifactVerificationError(
            "only ppbase platform wheels and the source guard may be published: "
            + ", ".join(other_files)
        )
    if len(wheels) != len(EXPECTED_PLATFORMS):
        raise ArtifactVerificationError(
            f"expected four ppbase wheels, found {len(wheels)}"
        )
    if len(source_distributions) != 1:
        raise ArtifactVerificationError(
            f"expected one ppbase source distribution, found {len(source_distributions)}"
        )
    _verify_sdist(source_distributions[0], expected_version)

    observed: dict[str, str] = {}
    for wheel in wheels:
        key = _verify_wheel(wheel, expected_version)
        if key in observed:
            raise ArtifactVerificationError(f"duplicate wheel for {key}")
        observed[key] = wheel.name
    missing = sorted(set(EXPECTED_PLATFORMS) - set(observed))
    if missing:
        raise ArtifactVerificationError("missing release wheels: " + ", ".join(missing))
    observed["source"] = source_distributions[0].name
    return observed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--release-tag")
    return parser


def main() -> int:
    try:
        observed = verify(_parser().parse_args())
    except (
        ArtifactVerificationError,
        OSError,
        KeyError,
        tarfile.TarError,
        UnicodeDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(observed, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
