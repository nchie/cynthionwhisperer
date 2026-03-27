#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f Cargo.toml ]] || ! grep -q 'crates/cynthionwhisperer-py' Cargo.toml; then
    echo "error: run this script from the cynthionwhisperer-rs directory" >&2
    exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
MANIFEST_PATH="$PWD/crates/cynthionwhisperer-py/Cargo.toml"

cleanup_wheel_dir=0
if [[ -n "${WHEEL_DIR:-}" ]]; then
    WHEEL_DIR="$WHEEL_DIR"
else
    WHEEL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cynthionwhisperer-wheels.XXXXXX")"
    cleanup_wheel_dir=1
fi

trap 'if [[ "$cleanup_wheel_dir" -eq 1 ]]; then rm -rf "$WHEEL_DIR"; fi' EXIT

echo "Using Python: $("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
echo "Building wheel from: $MANIFEST_PATH"
echo "Wheel output dir: $WHEEL_DIR"

"$PYTHON_BIN" -m pip install --user maturin
"$PYTHON_BIN" -m maturin build --release --manifest-path "$MANIFEST_PATH" -o "$WHEEL_DIR"

shopt -s nullglob
wheels=("$WHEEL_DIR"/cynthionwhisperer-*.whl)
shopt -u nullglob

if [[ "${#wheels[@]}" -ne 1 ]]; then
    echo "error: expected exactly one wheel in $WHEEL_DIR, found ${#wheels[@]}" >&2
    exit 1
fi

"$PYTHON_BIN" -m pip install --user --force-reinstall --no-deps "${wheels[0]}"

"$PYTHON_BIN" - <<'PY'
import cynthionwhisperer
import cynthionwhisperer.cynthionwhisperer as ext

print("package:", cynthionwhisperer.__file__)
print("extension:", ext.__file__)
PY
