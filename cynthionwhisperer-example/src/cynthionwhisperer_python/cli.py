import argparse
import sys
import time
from typing import Optional

import cynthionwhisperer


def _hex_bytes(value: str, flag_name: str) -> bytes:
    try:
        parsed = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(f"Invalid {flag_name} value: {error}") from error
    if not parsed:
        raise ValueError(f"{flag_name} must contain at least one byte")
    return parsed


def _int_auto(value: str, flag_name: str) -> int:
    try:
        return int(value, 0)
    except ValueError as error:
        raise ValueError(f"Invalid {flag_name} value: {value}") from error


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture and trigger utilities for cynthionwhisperer"
    )
    subparsers = parser.add_subparsers(dest="command")

    capture_parser = subparsers.add_parser(
        "capture",
        help="Capture until a matching DATA packet payload prefix is found",
    )
    capture_parser.add_argument(
        "--speed",
        default="auto",
        choices=["auto", "high", "full", "low"],
        help="Capture speed selection",
    )
    capture_parser.add_argument(
        "--direction",
        default="in",
        choices=["any", "in", "out"],
        help="Match traffic direction",
    )
    capture_parser.add_argument(
        "--data-pid",
        choices=["data0", "data1", "data2", "mdata"],
        help="Optional DATA PID filter (default: match any DATA PID)",
    )
    capture_parser.add_argument(
        "--pattern-hex",
        default="20",
        help="Payload prefix as hex bytes (e.g. '20' or '20 01')",
    )

    trigger_config_parser = subparsers.add_parser(
        "trigger-config",
        help="Configure one trigger stage and optionally arm it",
    )
    trigger_config_parser.add_argument(
        "--stage-index",
        default="0",
        help="Trigger stage index (default: 0)",
    )
    trigger_config_parser.add_argument(
        "--offset",
        required=True,
        help="Byte offset inside packet to begin matching (decimal or 0x..)",
    )
    trigger_config_parser.add_argument(
        "--pattern-hex",
        required=True,
        help="Pattern bytes to match (e.g. '00 32 52 95 FE')",
    )
    trigger_config_parser.add_argument(
        "--mask-hex",
        help="Optional byte mask; defaults to all FF; must match pattern length",
    )
    trigger_config_parser.add_argument(
        "--length",
        help="Optional match length in bytes; defaults to pattern length",
    )
    trigger_config_parser.add_argument(
        "--stage-count",
        default="1",
        help="Number of sequence stages to use (default: 1)",
    )
    trigger_config_parser.add_argument(
        "--no-enable",
        action="store_true",
        help="Write trigger control with enable=false",
    )
    trigger_config_parser.add_argument(
        "--no-output",
        action="store_true",
        help="Disable trigger output toggling",
    )
    trigger_config_parser.add_argument(
        "--arm",
        action="store_true",
        help="Arm trigger after writing config",
    )

    trigger_status_parser = subparsers.add_parser(
        "trigger-status",
        help="Read trigger status",
    )
    trigger_status_parser.add_argument(
        "--print-caps",
        action="store_true",
        help="Also print trigger capabilities",
    )

    trigger_get_stage_parser = subparsers.add_parser(
        "trigger-get-stage",
        help="Read trigger stage configuration",
    )
    trigger_get_stage_parser.add_argument(
        "--stage-index",
        default="0",
        help="Trigger stage index (default: 0)",
    )

    subparsers.add_parser(
        "trigger-arm",
        help="Arm trigger state machine",
    )
    subparsers.add_parser(
        "trigger-disarm",
        help="Disarm trigger state machine",
    )

    target_power_parser = subparsers.add_parser(
        "target-power",
        help="Read or control target power switching",
    )
    target_power_parser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "on", "off", "cycle"],
        help="Power action (default: status)",
    )
    target_power_parser.add_argument(
        "--source",
        default="target-c",
        help="Power source name (e.g. target-c, control, aux, host)",
    )
    target_power_parser.add_argument(
        "--delay-ms",
        type=int,
        default=250,
        help="Cycle off delay in milliseconds (default: 250)",
    )

    # Backward compatibility: if no subcommand is given, treat as `capture`.
    effective_argv = list(argv if argv is not None else sys.argv[1:])
    if not effective_argv:
        effective_argv = ["capture", *effective_argv]
    elif effective_argv[0] not in ("-h", "--help") and effective_argv[0].startswith("-"):
        effective_argv = ["capture", *effective_argv]

    return parser.parse_args(effective_argv)


def _print_trigger_status(analyzer: cynthionwhisperer.Cynthion) -> None:
    enable, armed, output_enable, output_state, sequence_stage, fire_count, stage_count = (
        analyzer.trigger_status()
    )
    print(
        "Trigger status: "
        f"enable={enable} armed={armed} output_enable={output_enable} "
        f"output_state={output_state} sequence_stage={sequence_stage} "
        f"fire_count={fire_count} stage_count={stage_count}"
    )


def _canonical_source_name(name: str) -> str:
    cleaned = name.strip().upper().replace("_", "-")
    if cleaned in ("TARGETC", "TARGET-C"):
        return "TARGET-C"
    if cleaned in ("CONTROL",):
        return "CONTROL"
    if cleaned in ("AUX",):
        return "AUX"
    if cleaned in ("HOST",):
        return "HOST"
    return cleaned


def _resolve_power_source_index(requested: str, sources: list[str]) -> Optional[int]:
    canonical_sources = [_canonical_source_name(source) for source in sources]
    requested_name = _canonical_source_name(requested)

    if requested_name in canonical_sources:
        return canonical_sources.index(requested_name)

    # Old firmware versions may expose HOST instead of CONTROL.
    if requested_name == "CONTROL" and "HOST" in canonical_sources:
        return canonical_sources.index("HOST")
    if requested_name == "HOST" and "CONTROL" in canonical_sources:
        return canonical_sources.index("CONTROL")

    return None


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        pattern = _hex_bytes(args.pattern_hex, "--pattern-hex")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    analyzer = cynthionwhisperer.Cynthion.open_first()
    capture = analyzer.start_capture(args.speed)

    try:
        packet = capture.capture_until(args.direction, pattern, args.data_pid)
    finally:
        capture.stop()

    if packet is None:
        pid_text = args.data_pid if args.data_pid else "any DATA PID"
        print(
            f"No matching {args.direction} packet found "
            f"(pid={pid_text}, payload_prefix={pattern.hex()})."
        )
        return 1

    raw = packet.bytes
    payload = raw[1:-2] if len(raw) >= 3 else b""
    pid_text = args.data_pid if args.data_pid else "any DATA PID"
    print(
        f"Matched {args.direction} packet at {packet.timestamp_ns} ns "
        f"(pid={pid_text}, payload_prefix={pattern.hex()})"
    )
    print(f"Payload ({len(payload)} bytes): {payload.hex()}")
    return 0


def _cmd_trigger_config(args: argparse.Namespace) -> int:
    try:
        stage_index = _int_auto(args.stage_index, "--stage-index")
        offset = _int_auto(args.offset, "--offset")
        stage_count = _int_auto(args.stage_count, "--stage-count")
        pattern = _hex_bytes(args.pattern_hex, "--pattern-hex")
        if args.mask_hex is not None:
            mask = _hex_bytes(args.mask_hex, "--mask-hex")
        else:
            mask = bytes([0xFF] * len(pattern))
        if len(mask) != len(pattern):
            raise ValueError("--mask-hex length must match --pattern-hex length")
        if args.length is None:
            length = len(pattern)
        else:
            length = _int_auto(args.length, "--length")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if stage_index < 0 or stage_index > 255:
        print("--stage-index must be in range 0..255", file=sys.stderr)
        return 2
    if offset < 0 or offset > 0xFFFF:
        print("--offset must be in range 0..65535", file=sys.stderr)
        return 2
    if stage_count < 0 or stage_count > 255:
        print("--stage-count must be in range 0..255", file=sys.stderr)
        return 2
    if length < 0 or length > 255:
        print("--length must be in range 0..255", file=sys.stderr)
        return 2

    analyzer = cynthionwhisperer.Cynthion.open_first()
    max_stages, max_pattern_len, stage_payload_len = analyzer.trigger_caps()
    print(
        f"Trigger caps: max_stages={max_stages} "
        f"max_pattern_len={max_pattern_len} stage_payload_len={stage_payload_len}"
    )

    if stage_index >= max_stages:
        print(
            f"--stage-index {stage_index} exceeds max_stages {max_stages}",
            file=sys.stderr,
        )
        return 2
    if length > len(pattern):
        print("--length cannot exceed pattern byte count", file=sys.stderr)
        return 2
    if length > max_pattern_len:
        print(
            f"--length {length} exceeds max_pattern_len {max_pattern_len}",
            file=sys.stderr,
        )
        return 2
    if stage_count > max_stages:
        print(
            f"--stage-count {stage_count} exceeds max_stages {max_stages}",
            file=sys.stderr,
        )
        return 2

    analyzer.set_trigger_control(
        enable=not args.no_enable,
        stage_count=stage_count,
        output_enable=not args.no_output,
    )
    analyzer.set_trigger_stage(
        stage_index=stage_index,
        offset=offset,
        pattern=pattern,
        mask=mask,
        length=length,
    )
    if args.arm:
        analyzer.arm_trigger()
        print("Trigger armed.")
    else:
        print("Trigger configured (not armed).")

    _print_trigger_status(analyzer)
    return 0


def _cmd_trigger_status(args: argparse.Namespace) -> int:
    analyzer = cynthionwhisperer.Cynthion.open_first()
    if args.print_caps:
        max_stages, max_pattern_len, stage_payload_len = analyzer.trigger_caps()
        print(
            f"Trigger caps: max_stages={max_stages} "
            f"max_pattern_len={max_pattern_len} stage_payload_len={stage_payload_len}"
        )
    _print_trigger_status(analyzer)
    return 0


def _cmd_trigger_get_stage(args: argparse.Namespace) -> int:
    try:
        stage_index = _int_auto(args.stage_index, "--stage-index")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    if stage_index < 0 or stage_index > 255:
        print("--stage-index must be in range 0..255", file=sys.stderr)
        return 2

    analyzer = cynthionwhisperer.Cynthion.open_first()
    offset, length, pattern, mask = analyzer.get_trigger_stage(stage_index)
    print(
        f"Stage {stage_index}: offset={offset} length={length} "
        f"pattern={pattern.hex()} mask={mask.hex()}"
    )
    return 0


def _cmd_trigger_arm() -> int:
    analyzer = cynthionwhisperer.Cynthion.open_first()
    analyzer.arm_trigger()
    print("Trigger armed.")
    _print_trigger_status(analyzer)
    return 0


def _cmd_trigger_disarm() -> int:
    analyzer = cynthionwhisperer.Cynthion.open_first()
    analyzer.disarm_trigger()
    print("Trigger disarmed.")
    _print_trigger_status(analyzer)
    return 0


def _cmd_target_power(args: argparse.Namespace) -> int:
    analyzer = cynthionwhisperer.Cynthion.open_first()
    sources = analyzer.power_sources()
    if not sources:
        print("Power control is not supported by this gateware/device.", file=sys.stderr)
        return 1

    config = analyzer.power_config()
    if config is None:
        print("Power configuration unavailable.", file=sys.stderr)
        return 1

    source_index, on_now, start_on, stop_off = config

    if args.action == "status":
        source_name = sources[source_index] if source_index < len(sources) else f"index-{source_index}"
        print(
            f"Target power: on_now={on_now} source={source_name} "
            f"start_on={start_on} stop_off={stop_off} "
            f"available_sources={','.join(sources)}"
        )
        return 0

    selected_source_index = _resolve_power_source_index(args.source, list(sources))
    if selected_source_index is None:
        print(
            f"Unknown --source '{args.source}'. Available sources: {', '.join(sources)}",
            file=sys.stderr,
        )
        return 2

    if args.delay_ms < 0:
        print("--delay-ms must be >= 0", file=sys.stderr)
        return 2

    if args.action == "on":
        analyzer.set_power_config(selected_source_index, True, start_on, stop_off)
        print(f"Target power ON via {sources[selected_source_index]}")
    elif args.action == "off":
        analyzer.set_power_config(selected_source_index, False, start_on, stop_off)
        print("Target power OFF")
    elif args.action == "cycle":
        analyzer.set_power_config(selected_source_index, True, start_on, stop_off)
        time.sleep(args.delay_ms / 1000.0)
        analyzer.set_power_config(selected_source_index, False, start_on, stop_off)
        print(
            f"Target power cycled via {sources[selected_source_index]} "
            f"(on for {args.delay_ms} ms)"
        )

    updated = analyzer.power_config()
    if updated is not None:
        updated_source_index, updated_on_now, updated_start_on, updated_stop_off = updated
        updated_source_name = (
            sources[updated_source_index]
            if updated_source_index < len(sources)
            else f"index-{updated_source_index}"
        )
        print(
            f"Target power status: on_now={updated_on_now} source={updated_source_name} "
            f"start_on={updated_start_on} stop_off={updated_stop_off}"
        )
    return 0


def main() -> int:
    args = _parse_args()

    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "trigger-config":
        return _cmd_trigger_config(args)
    if args.command == "trigger-status":
        return _cmd_trigger_status(args)
    if args.command == "trigger-get-stage":
        return _cmd_trigger_get_stage(args)
    if args.command == "trigger-arm":
        return _cmd_trigger_arm()
    if args.command == "trigger-disarm":
        return _cmd_trigger_disarm()
    if args.command == "target-power":
        return _cmd_target_power(args)

    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
