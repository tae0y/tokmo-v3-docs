#!/bin/bash
# PAUSED ARTIFACT — not installed. US-market variant of the rotation cycle:
# same wrapper, market pinned to US. Register only when a US experiment
# exists in experiments.json; schedule 42 23 * * 1-5 (Asia/Seoul), which is
# 09:42 ET in winter and 10:42 ET in summer — inside the regular session
# either way. The calendar store gate handles US holidays.
export TOKMO_ROTATION_MARKET=US
exec /bin/bash "$(dirname "$0")/tokmo_v3_hyundai_rotation.sh"
