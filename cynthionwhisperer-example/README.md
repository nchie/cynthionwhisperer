# cynthionwhisperer-example

Python example project that uses the `cynthionwhisperer` PyO3 extension built from the sibling Rust workspace.

## Quick start

```bash
cd /Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-example
./scripts/dev_setup.sh
source .venv/bin/activate
cynthionwhisperer-capture --speed auto --max-events 10
```

## Notes

- `scripts/dev_setup.sh` builds and installs the extension from:
  - `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/Cargo.toml`
- The CLI prints packet and event summaries and then stops capture.
