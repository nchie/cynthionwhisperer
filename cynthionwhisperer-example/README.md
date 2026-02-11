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
cynthionwhisperer-capture capture --speed auto --direction in --pattern-hex 20
```

Match only incoming `DATA1` with payload starting `0x20`:

```bash
cynthionwhisperer-capture capture --speed auto --direction in --data-pid data1 --pattern-hex 20
```

## Trigger commands

Configure stage 0 for a fixed-offset byte pattern, enable trigger output, and arm:

```bash
cynthionwhisperer-capture trigger-config \
  --stage-index 0 \
  --offset 68 \
  --pattern-hex "00 32 52 95 FE" \
  --stage-count 1 \
  --arm
```

Read trigger status:

```bash
cynthionwhisperer-capture trigger-status --print-caps
```

Read back one stage configuration:

```bash
cynthionwhisperer-capture trigger-get-stage --stage-index 0
```

Disarm trigger:

```bash
cynthionwhisperer-capture trigger-disarm
```

## Notes

- `scripts/dev_setup.sh` builds and installs the extension from:
  - `/Users/andre/source/cynthion/cynthionwhisperer/cynthionwhisperer-rs/crates/cynthionwhisperer-py/Cargo.toml`
- `capture` mode accepts the old no-subcommand form for backward compatibility.
