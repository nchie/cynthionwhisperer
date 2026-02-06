import argparse
import sys

import cynthionwhisperer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture packets/events from a Cynthion analyzer via cynthionwhisperer"
    )
    parser.add_argument(
        "--speed",
        default="auto",
        choices=["auto", "high", "full", "low"],
        help="Capture speed selection",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=10,
        help="Maximum number of events to print before stopping",
    )
    return parser.parse_args()


def _format_event(event: object) -> str:
    if hasattr(event, "bytes"):
        payload = event.bytes
        return f"packet ts={event.timestamp_ns} len={len(payload)}"
    return f"event ts={event.timestamp_ns} type={event.event_type}"


def main() -> int:
    args = _parse_args()

    analyzer = cynthionwhisperer.Cynthion.open_first()
    capture = analyzer.start_capture(args.speed)

    printed = 0
    try:
        for event in capture:
            print(_format_event(event))
            printed += 1
            if printed >= args.max_events:
                break
    finally:
        capture.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
