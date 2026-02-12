# cynthionwhisperer

`cynthionwhisperer` is an experimental fork/spinoff around Packetry-style Cynthion capture, focused on:

- a Python-first API and CLI
- configurable hardware triggers in analyzer gateware
- PMOD trigger output for external equipment integration (e.g. Chipwhisperer)

## What this is

This repo combines three pieces:

- `cynthionwhisperer-rs`:
  Rust backend and protocol handling for Cynthion analyzer access.
- `cynthionwhisperer-example`:
  Python package + CLI (`cynthionwhisperer-capture`) on top of the Rust extension.
- `gateware`:
  Standalone analyzer gateware tree used for trigger-related FPGA changes.

## Current Status

This project is early and mostly built quickly for a specific workflow. I likely won't ever polish it to a state which I'm proud of (a lot of it is vibe-coded), but hopefully it can be useful for someone else at some point.

- It is not comprehensively tested.
- Some behavior is based on practical iteration rather than polished design.
- Treat it as a useful prototype, not production-hardened tooling.

That said: it has worked well for the intended use case (capture + trigger + external sync output).

## Where To Start

- Python/CLI usage:
  `cynthionwhisperer-example/README.md`
- Gateware build/flash notes:
  `gateware/README.md`

## Notes

- Trigger timing is implemented in FPGA gateware.
- Power control and trigger configuration are exposed via the Python CLI.
- If using auto speed detection, starting capture before target enumeration may still be required in some scenarios.
