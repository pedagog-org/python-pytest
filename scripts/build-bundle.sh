#!/bin/sh
# Builds the offline install bundle: this package plus pinned wheels of all its
# dependencies (all pure Python). Built on the oldest supported Python so
# version-conditional backports (exceptiongroup, tomli) are included.
#   scripts/build-bundle.sh [OUT_DIR]   -> OUT_DIR/pedagog-pytest-<version>-bundle.tar.gz
set -eu
out="${1:-dist}"
version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])' 2>/dev/null \
  || python -c 'import re; print(re.search(r"^version = \"([^\"]+)\"", open("pyproject.toml").read(), re.M).group(1))')
work=$(mktemp -d)
pip wheel --quiet --only-binary=:all: --wheel-dir "$work/wheels" .
mkdir -p "$out"
tar -czf "$out/pedagog-pytest-$version-bundle.tar.gz" -C "$work" wheels
rm -rf "$work"
echo "$out/pedagog-pytest-$version-bundle.tar.gz"
