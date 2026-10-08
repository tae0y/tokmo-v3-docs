"""Create one signed/hashed prelaunch PASS marker (Stage 4 D).

Hermes runs this after committing Stage 4: once with the full-suite result
and once with the independent-review provenance. The marker binds the exact
commit SHA, suite command, test count, creation instant, and provenance
under a canonical SHA-256 content hash, optionally HMAC-signed with a key
taken from the environment (the key value never enters argv, the marker, or
stdout). The 48-hour shadow runner and the deployment doctor verify these
markers — hashes and exact commit, never a plain boolean.

Usage:
  python tools/make_stage4_pass_markers.py \
    --kind full-suite --commit <40-hex sha> \
    --command ".venv/bin/python -m pytest -q" --test-count 2466 \
    --provenance hermes/full-suite \
    --out .hermes/evidence/stage4/full_suite_pass.json \
    [--hmac-key-env TOKMO_MARKER_KEY]
"""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

_TOOL_DIR = Path(__file__).resolve().parent
if str(_TOOL_DIR.parent / "src") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR.parent / "src"))

from tokmo.records.prelaunch_markers import (  # noqa: E402
    MarkerError,
    create_pass_marker,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make-stage4-pass-markers")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--test-count", type=int, required=True)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--hmac-key-env")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    hmac_key = None
    if args.hmac_key_env is not None:
        value = os.environ.get(args.hmac_key_env)
        if not value:
            print(
                f"marker refusal: environment key {args.hmac_key_env} unset",
                file=sys.stderr,
            )
            return 1
        hmac_key = value.encode()
    try:
        marker = create_pass_marker(
            Path(args.out), kind=args.kind, commit_sha=args.commit,
            command=args.command, test_count=args.test_count,
            created_at=datetime.now(timezone.utc),
            provenance=args.provenance, hmac_key=hmac_key,
        )
    except MarkerError as error:
        print(f"marker refusal: {error}", file=sys.stderr)
        return 1
    print(
        f"marker kind={args.kind} commit={args.commit} "
        f"sha256={marker['sha256']} out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
