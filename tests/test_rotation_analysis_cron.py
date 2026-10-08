"""Exercise the Hermes shell entry without a model, live state, or broker."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("mode,child_rc", [("watch", 0), ("watch", 2), ("close", 0)])
def test_cron_waits_for_completion_and_keeps_child_failure_visible(tmp_path, mode, child_rc):
    supervisor = tmp_path / "completion-safe"
    supervisor.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
a=sys.argv[1:]
manifest=pathlib.Path(a[a.index('--manifest')+1])
manifest.write_text(json.dumps({'child_exit_code':int(os.environ['TEST_CHILD_RC']), 'args':a}))
''')
    supervisor.chmod(0o700)
    env = dict(os.environ, TOKMO_ROTATION_ANALYSIS_RUNTIME=str(tmp_path / "logs"),
               TOKMO_ROTATION_STATE=str(tmp_path / "state"), TOKMO_COMPLETION_SAFE_CLI=str(supervisor),
               TOKMO_ROTATION_PYTHON=sys.executable, TEST_CHILD_RC=str(child_rc))
    name = "tokmo_v3_hyundai_rotation_analysis" + ("_close" if mode == "close" else "") + ".sh"
    result = subprocess.run(["/bin/bash", str(ROOT / "ops/hermes-cron" / name)], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if child_rc == 0 else 1), result.stderr
    manifest = next((tmp_path / "logs").glob("*/manifest.json"))
    a = json.loads(manifest.read_text())["args"]
    # watch: headroom past the latest approval window (10:40 close + 30 min = 11:10), not a business deadline
    assert a[a.index("--timeout") + 1] == ("60" if mode == "close" else "3900")
    assert ("--close-only" if mode == "close" else "--watch") in a
    assert a[a.index("--state-dir") + 1] == str(tmp_path / "state")
    assert not (tmp_path / "state").exists()
    if child_rc:
        assert "NEEDS_ATTENTION" in result.stdout and "child_rc=2" in result.stdout
