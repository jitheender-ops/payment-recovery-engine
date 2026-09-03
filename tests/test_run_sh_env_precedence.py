"""
run.sh must never let .env beat a value the caller exported.

This precedence bug has bitten three times — demo mode, the --sandbox live-key
guard (which "passed" only because .env had already overwritten the exported
test key), and the default branch, which is the one where .env holds the LIVE
credentials. It is now one function, `source_env`, and this test is what stops
it coming back a fourth time.

The function is extracted from run.sh and exercised directly: shelling out to
run.sh itself would build an image and start servers.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"


def _source_env_fn() -> str:
    body = re.search(r"^source_env\(\) \{.*?^\}", RUN_SH.read_text(), re.S | re.M)
    assert body, "source_env() not found in run.sh — did it get inlined again?"
    return body.group(0)


def _run(script: str, tmp_path: Path) -> str:
    (tmp_path / ".env").write_text(
        "RAZORPAY_KEY_ID=rzp_live_DANGER\nONLY_IN_ENV=kept\n"
    )
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{_source_env_fn()}\n{script}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_exported_value_beats_dotenv(tmp_path: Path) -> None:
    """The live key in .env must not silently replace the test key just named."""
    out = _run(
        'export RAZORPAY_KEY_ID=rzp_test_caller\n'
        'source_env ./.env\n'
        'echo "$RAZORPAY_KEY_ID"',
        tmp_path,
    )
    assert out == "rzp_test_caller"


def test_dotenv_only_values_survive(tmp_path: Path) -> None:
    """Restoring the caller's environment must not discard what .env alone set."""
    out = _run('source_env ./.env\necho "$ONLY_IN_ENV"', tmp_path)
    assert out == "kept"


def test_dotenv_applies_when_caller_set_nothing(tmp_path: Path) -> None:
    """.env is still the source of truth when nothing was exported over it."""
    out = _run(
        'unset RAZORPAY_KEY_ID\nsource_env ./.env\necho "$RAZORPAY_KEY_ID"',
        tmp_path,
    )
    assert out == "rzp_live_DANGER"


def test_missing_dotenv_is_not_an_error(tmp_path: Path) -> None:
    """--demo and a fresh checkout both call this with no .env present."""
    out = _run('source_env ./.absent\necho ok', tmp_path)
    assert out == "ok"
