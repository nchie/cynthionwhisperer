#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PY_APP_DIR="$ROOT_DIR/cynthionwhisperer-example"
RUST_BINDINGS_MANIFEST="$ROOT_DIR/cynthionwhisperer-rs/crates/cynthionwhisperer-py/Cargo.toml"

cd "$PY_APP_DIR"

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install maturin

maturin develop --manifest-path "$RUST_BINDINGS_MANIFEST"
python -m pip install -e .

echo "Environment ready. Activate with: source $PY_APP_DIR/.venv/bin/activate"
