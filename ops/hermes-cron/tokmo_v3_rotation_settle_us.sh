#!/bin/bash
# PAUSED ARTIFACT — install only when a market=US experiment exists.
# US settlement at 21:40 KST (before the 22:30/23:30 KST regular open).
export TOKMO_ROTATION_MARKET=US
exec /bin/bash "$(dirname "$0")/tokmo_v3_rotation_settle.sh"
