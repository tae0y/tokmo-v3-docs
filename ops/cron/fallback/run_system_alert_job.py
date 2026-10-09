#!/usr/bin/env python3
"""Stand-in for Hermes' run_system_alert_job.py: run a command; on failure, send the alert yourself.

Usage (same as the Hermes tool):  run_system_alert_job.py --name "Job name" -- CMD [ARGS...]
Success is silent. On a non-zero exit the tail of stderr is sent through send_system_alert.sh (next to
this file), which posts to Telegram when TELEGRAM_ALERT_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID are set and
otherwise prints to stderr.
"""

import argparse
import subprocess
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--notify-stdout", action="store_true")
    parser.add_argument("--silent-marker", default="[SILENT]")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = (
        args.command[1:] if args.command and args.command[0] == "--" else args.command
    )
    if not command:
        parser.error("command required after --")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0 and not args.notify_stdout:
        return 0
    if result.returncode == 0 and args.silent_marker in result.stdout:
        return 0
    message = (
        f"{args.name}: exit {result.returncode}\n"
        + (result.stderr or result.stdout)[-1500:]
    )
    subprocess.run(
        [str(Path(__file__).with_name("send_system_alert.sh")), message], check=False
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
