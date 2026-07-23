#!/usr/bin/env python3
"""Verify that PyPI contains the complete local PPBase release byte-for-byte."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


class PyPIReleaseVerificationError(RuntimeError):
    """Raised when the published release is incomplete or does not match."""


def _local_files(directory: Path) -> dict[str, str]:
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )
    if len(files) != 5:
        raise PyPIReleaseVerificationError(
            f"expected five local release files, found {len(files)}"
        )
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def _verify_payload(
    payload: object,
    *,
    expected_version: str,
    local_files: dict[str, str],
) -> None:
    if not isinstance(payload, dict):
        raise PyPIReleaseVerificationError("PyPI returned a non-object response")
    info = payload.get("info")
    urls = payload.get("urls")
    if (
        not isinstance(info, dict)
        or str(info.get("name", "")).lower() != "ppbase"
        or info.get("version") != expected_version
        or not isinstance(urls, list)
    ):
        raise PyPIReleaseVerificationError("PyPI returned the wrong project/version")

    remote_files: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise PyPIReleaseVerificationError("PyPI returned malformed file metadata")
        filename = item.get("filename")
        digests = item.get("digests")
        sha256 = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(filename, str) or not isinstance(sha256, str):
            raise PyPIReleaseVerificationError("PyPI omitted a release file digest")
        if filename in remote_files:
            raise PyPIReleaseVerificationError("PyPI returned a duplicate filename")
        remote_files[filename] = sha256

    if set(remote_files) != set(local_files):
        missing = sorted(set(local_files) - set(remote_files))
        unexpected = sorted(set(remote_files) - set(local_files))
        raise PyPIReleaseVerificationError(
            "PyPI release file set differs"
            + (f"; missing={','.join(missing)}" if missing else "")
            + (f"; unexpected={','.join(unexpected)}" if unexpected else "")
        )
    mismatched = sorted(
        name
        for name, digest in local_files.items()
        if remote_files[name].lower() != digest.lower()
    )
    if mismatched:
        raise PyPIReleaseVerificationError(
            "PyPI SHA-256 differs for: " + ", ".join(mismatched)
        )


def verify(
    directory: Path,
    *,
    version: str,
    retries: int,
    delay: float,
) -> dict[str, str]:
    local_files = _local_files(directory.resolve())
    endpoint = f"https://pypi.org/pypi/ppbase/{version}/json"
    last_error: BaseException | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                endpoint,
                headers={"Accept": "application/json", "User-Agent": "ppbase-release-ci"},
            )
            with urllib.request.urlopen(request, timeout=15.0) as response:
                if response.geturl() != endpoint or int(response.status) != 200:
                    raise PyPIReleaseVerificationError(
                        "PyPI returned a non-authoritative response"
                    )
                body = response.read(5 * 1024 * 1024 + 1)
            if len(body) > 5 * 1024 * 1024:
                raise PyPIReleaseVerificationError("PyPI response was oversized")
            payload = json.loads(body.decode("utf-8"))
            _verify_payload(
                payload,
                expected_version=version,
                local_files=local_files,
            )
            return local_files
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            urllib.error.URLError,
            PyPIReleaseVerificationError,
        ) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(delay)
    raise PyPIReleaseVerificationError(
        f"PyPI release did not converge after {retries} attempts: {last_error}"
    ) from last_error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--retries", type=int, default=12)
    parser.add_argument("--delay", type=float, default=10.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.retries < 1 or args.delay < 0:
        print("error: retries must be positive and delay non-negative")
        return 2
    try:
        version = args.version.removeprefix("v")
        files = verify(
            args.directory,
            version=version,
            retries=args.retries,
            delay=args.delay,
        )
    except PyPIReleaseVerificationError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(files, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
