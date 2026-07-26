#!/usr/bin/env bash
set -euo pipefail

VERSION=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")

echo "==> Building ppbase v${VERSION}..."
rm -rf dist/ppbase-"${VERSION}"*
python -m build

echo "==> Publishing ppbase v${VERSION} to PyPI..."
if [ -z "${PIPY:-}" ]; then
  echo "Error: PIPY environment variable not set (PyPI API token)" >&2
  exit 1
fi

python -m twine upload dist/ppbase-"${VERSION}"* -u __token__ -p "$PIPY"

echo "==> Done! https://pypi.org/project/ppbase/${VERSION}/"
