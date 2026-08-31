"""
Rendering coverage for dashboard/views/voice.py — none existed before this.

Not a data-correctness suite (query_db is faked here); the point is a
regression net that catches "this page throws" or "the empty state
disappeared", the two failure modes a Streamlit page has no other net for.

ATTEMPTS_SQL (the "Language used" panel) filters retry_attempts on
channel = 'voice' — a condition no code path in this repo ever sets (voice
calls are queued against the ORIGINAL nudge_customer attempt, not written
as their own attempt row with that channel; see the customer-recovery-page
fix for the same root cause). The populated-state test below feeds it a
non-empty frame directly, which is honest about testing the RENDERING, not
a claim that this query returns rows in production today.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dashboard.views import voice as voice_view

# AppTest.from_function only sees names inside the function body itself, not
# theme/query_db pulled from the enclosing module — a thin support script
# that does a real `import` is what lets those resolve normally.
_SCRIPT = Path(__file__).parent / "_voice_dashboard_script.py"


def _fake_query_db(frames: dict[str, pd.DataFrame]) -> Any:
    def _query(query: str, params: dict[str, Any] | None = None) -> pd.DataFrame | None:
        for needle, frame in frames.items():
            if needle in query:
                return frame
        return pd.DataFrame()

    return _query


def test_the_empty_state_renders_when_nothing_is_queued(monkeypatch: Any) -> None:
    monkeypatch.setattr(voice_view, "query_db", _fake_query_db({
        "AS total,": pd.DataFrame(
            [{"total": 0, "queued": 0, "claimed": 0, "done": 0, "stuck": 0, "amount": 0}]
        ),
    }))
    at = AppTest.from_file(str(_SCRIPT))
    at.run()
    assert not at.exception
    assert any("idle" in m.value.lower() for m in at.markdown)


def test_a_populated_queue_renders_its_tiles(monkeypatch: Any) -> None:
    queue_row = pd.DataFrame([{
        "total": 12, "queued": 3, "claimed": 2, "done": 7, "stuck": 1, "amount": 500_000,
    }])
    by_type = pd.DataFrame([
        {"risk_type": "mandate_failure", "state": "done", "n": 5},
        {"risk_type": "subscription_failure", "state": "queued", "n": 2},
    ])
    recent = pd.DataFrame([{
        "Queued": pd.Timestamp.now(tz="UTC"), "Case": "mandate_1",
        "Type": "mandate_failure", "Amount": 99900, "State": "done",
        "Claimed by": "worker-1", "Result": "done",
    }])
    promises = pd.DataFrame([{"made": 4, "kept": 3, "broken": 1, "amount": 300_000}])
    langs = pd.DataFrame([{"language": "hinglish", "n": 6}])

    monkeypatch.setattr(voice_view, "query_db", _fake_query_db({
        "AS total,": queue_row,
        "GROUP BY 1, 2": by_type,
        "LEFT JOIN recovery_cases": recent,
        "FROM promises_to_pay": promises,
        "FROM retry_attempts": langs,
    }))
    at = AppTest.from_file(str(_SCRIPT))
    at.run()
    assert not at.exception


@pytest.mark.parametrize("frame", [None, pd.DataFrame()])
def test_a_broken_or_empty_query_falls_back_to_the_empty_state(
    monkeypatch: Any, frame: pd.DataFrame | None,
) -> None:
    """query_db returns None on a DB error — same honest empty state, not a crash."""
    monkeypatch.setattr(voice_view, "query_db", lambda *a, **k: frame)
    at = AppTest.from_file(str(_SCRIPT))
    at.run()
    assert not at.exception
