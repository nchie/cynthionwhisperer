import argparse
import sys

import cynthionwhisperer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture until a matching DATA packet payload starts with a byte prefix"
    )
    parser.add_argument(
        "--speed",
        default="auto",
        choices=["auto", "high", "full", "low"],
        help="Capture speed selection",
    )
    parser.add_argument(
        "--direction",
        default="in",
        choices=["any", "in", "out"],
        help="Match traffic direction",
    )
    parser.add_argument(
        "--data-pid",
        choices=["data0", "data1", "data2", "mdata"],
        help="Optional DATA PID filter (default: match any DATA PID)",
    )
    parser.add_argument(
        "--pattern-hex",
        default="20",
        help="Payload prefix as hex bytes (e.g. '20' or '20 01')",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        pattern = bytes.fromhex(args.pattern_hex)
    except ValueError as error:
        print(f"Invalid --pattern-hex value: {error}", file=sys.stderr)
        return 2
    if not pattern:
        print("--pattern-hex must contain at least one byte", file=sys.stderr)
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


if __name__ == "__main__":
    sys.exit(main())
