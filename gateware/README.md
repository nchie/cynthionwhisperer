# Standalone Cynthion Analyzer Gateware

This directory contains a standalone, analyzer-only gateware package extracted from the Cynthion project, which has been modified to support triggers on sequences of byte-patterns.

## What is included

- `cynthion.gateware.analyzer` (analyzer top + capture/event/speed/fifo modules)
- `cynthion.gateware.platform` (Cynthion board platform definitions used for build/program)
- `cynthion.gateware.vendor.amaranth_boards` (small vendored board resources fallback)
- `cynthion.shared.usb` constants embedded for standalone operation

## Prerequisites

- Python 3.9+
- OSS CAD Suite (Yosys / nextpnr / ecppack) available in shell
- Apollo tools / hardware access for upload/flash

## Install

```bash
pip install -e .
```

## Build / Upload / Flash

```bash
## Add the export below if it complains about the platform
# export LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4

# build only
python -m cynthion.gateware.analyzer.top --dry-run

# if the board is in analyzer/stub mode, hand USB back to Apollo first
apollo force-offline
apollo info

# upload to SRAM
python -m cynthion.gateware.analyzer.top --upload

# flash persistent bitstream
python -m cynthion.gateware.analyzer.top --flash

# write bitstream file only
python -m cynthion.gateware.analyzer.top --output analyzer.bit
```

## Notes

- Platform autodetect uses Apollo (`top_level_cli` + `cynthion.gateware.APOLLO_PLATFORMS`).
- If autodetect fails, set `LUNA_PLATFORM` explicitly, for example:
  - `LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4`
- If upload/flash fails with an Apollo stub-interface error, run `apollo force-offline` and retry.
