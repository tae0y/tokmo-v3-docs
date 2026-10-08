#!/bin/bash
# Separate 10:40 close check; no model invocation, shared daily lock/receipt.
exec /bin/bash "$(cd "$(dirname "$0")" && pwd)/tokmo_v3_hyundai_rotation_analysis.sh" --close-only
