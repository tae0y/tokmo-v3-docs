#!/opt/homebrew/bin/python3.12
"""Scheduled stabilization feedback to the NEWS group; --preview never sends."""

from pathlib import Path

PROJECT_ROOT = (
    Path(__file__).resolve().parents[2]
)  # ops/hermes-cron/<this> -> project root
import sys

PROJECT = PROJECT_ROOT
ROOT = PROJECT_ROOT / "var/data"
sys.path[:0] = [str(PROJECT / "src"), str(PROJECT / "tools")]
from report_news_observation import main as run_report


def main(final=False):
    args = ["--root", str(ROOT), "--hermes", str(Path.home() / ".hermes")]
    if final:
        args.append("--final")
    if "--preview" not in sys.argv[1:]:
        args.append("--send")
    return run_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
