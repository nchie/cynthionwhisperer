# Breaking out Packetry API

## 2026-02-05
- Added backend modules to `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer/src/backend` extracted from Packetry:
- - `packetry/src/backend/mod.rs` -> `.../cynthionwhisperer/src/backend/mod.rs` (removed iCE40 backend, updated module layout).
- - `packetry/src/backend/cynthion.rs` -> `.../cynthionwhisperer/src/backend/cynthion.rs`.
- - `packetry/src/backend/transfer_queue.rs` -> `.../cynthionwhisperer/src/backend/transfer_queue.rs`.
- Copied Packetry `event.rs` -> `.../cynthionwhisperer/src/event.rs`.
- Extracted minimal supporting types into `.../cynthionwhisperer/src`:
- - `CaptureMetadata` from `packetry/src/capture.rs` -> `.../cynthionwhisperer/src/capture.rs`.
- - `Speed`, `PID`, `crc5`, `validate_packet` (and CRC16 helper), plus `Speed::description()` from `packetry/src/usb.rs` and `packetry/src/ui/mod.rs` -> `.../cynthionwhisperer/src/usb.rs`.
- - `handle_thread_panic` from `packetry/src/util/mod.rs` -> `.../cynthionwhisperer/src/util.rs`.
- Implemented Rust wrapper API in `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer/src/lib.rs`.
- Implemented PyO3 bindings in `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/src/lib.rs`.
- Updated workspace members and dependencies in `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/Cargo.toml` and `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer/Cargo.toml`.
- Updated `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/Cargo.toml` to enable `pyo3` extension-module support, add `futures-lite`, and set the library name to `cynthionwhisperer`.
- Fixed duplicate re-exports in `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer/src/lib.rs` after running `cargo check`.
- Updated PyO3 bindings for pyo3 0.28 API changes (Bound types, unsendable classes, `Py<PyAny>` returns) to compile in `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/src/lib.rs`.
- Merged the intermediate backend crate into `cynthionwhisperer` and removed the extra crate, keeping only `cynthionwhisperer` and `cynthionwhisperer-py` in the workspace.
- Added `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/build.rs` to force `-Wl,-undefined,dynamic_lookup` on macOS so Python symbols resolve at runtime.
- Added sibling Python project `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-example` with:
- - `pyproject.toml` and package scaffolding under `src/cynthionwhisperer_python`.
- - `scripts/dev_setup.sh` to create a venv, install `maturin`, build/install the local PyO3 extension, and install the app.
- - CLI entrypoint `cynthionwhisperer-capture` that opens the analyzer and prints captured packets/events.
- Renamed the Python example project directory from `cynthionwhisperer-python` to `cynthionwhisperer-example` and updated internal references in `README.md`, `pyproject.toml`, and `scripts/dev_setup.sh`.
- Updated root `.gitignore` to ignore Python virtualenv/cache/build artifacts generically across subprojects.
