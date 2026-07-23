#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
Local token publication is disabled.

Publish PPBase by publishing a GitHub Release whose tag is exactly `vX.Y.Z`,
where `X.Y.Z` matches pyproject.toml. The "Build and publish PPBase platform
wheels" workflow then builds and verifies all four wheels and authenticates to
PyPI with Trusted Publishing. It also publishes one source guard so unsupported
platforms fail on the current version instead of falling back to an old wheel.
Publishing from one developer machine would create an incomplete platform
release and is intentionally unsupported.
EOF
exit 1
