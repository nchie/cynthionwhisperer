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

## Debug Outputs

When the analyzer is enabled, the gateware exposes a few useful status signals on
the LEDs and PMOD A header:

- `LED0`: capture sync heartbeat, blinking with a 1-minute cycle while capture is active
- `LED1`: USB bulk stream valid
- `LED2`: analyzer overrun indicator
- `LED3`: UTMI `session_valid`
- `LED4`: UTMI `rx_active`
- `LED5`: UTMI `rx_error`
- `PMOD A1`: trigger output pulse
- `PMOD A2`: capture sync square wave, held low while idle, driven high on the exact capture-start event, and toggled every 30 seconds for external logic-analyzer synchronization

## Notes

- Platform autodetect uses Apollo (`top_level_cli` + `cynthion.gateware.APOLLO_PLATFORMS`).
- If autodetect fails, set `LUNA_PLATFORM` explicitly, for example:
  - `LUNA_PLATFORM=cynthion.gateware.platform:CynthionPlatformRev1D4`
- If upload/flash fails with an Apollo stub-interface error, run `apollo force-offline` and retry.
