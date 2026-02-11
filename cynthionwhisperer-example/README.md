# cynthionwhisperer-example

Python example project that uses the `cynthionwhisperer` PyO3 extension built from the sibling Rust workspace.

## Quick start

```bash
cd /Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-example
./scripts/dev_setup.sh
source .venv/bin/activate
cynthionwhisperer-capture --speed auto
```

Match any incoming DATA packet with payload starting `0x20`:

```bash
cynthionwhisperer-capture --speed auto --direction in --pattern-hex 20
```

Match only incoming `DATA1` with payload starting `0x20`:

```bash
cynthionwhisperer-capture --speed auto --direction in --data-pid data1 --pattern-hex 20
```

## Notes

- `scripts/dev_setup.sh` builds and installs the extension from:
  - `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/Cargo.toml`
- The CLI captures until it finds a packet matching direction, optional DATA PID, and payload prefix.
