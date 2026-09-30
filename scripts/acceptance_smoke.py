#!/usr/bin/env python3
"""Offline acceptance smoke for grading / pronunciation / invite helpers (no MySQL required)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.classes import invite_code  # noqa: E402
from backend.homework import grade_task  # noqa: E402
from backend.parents import make_invite  # noqa: E402
from backend.pronunciation import assess_audio, assess_from_transcript, provider_status  # noqa: E402


def main() -> int:
    score, ok, _ = grade_task(
        "choice",
        {"question_ids": [1, 2]},
        {"answers": {"1": "B", "2": "A"}},
        {1: {"answer": "B"}, 2: {"answer": "A"}},
    )
    assert score == 100 and ok == 1

    perfect = assess_from_transcript("Please read this sentence carefully", "Please read this sentence carefully", 3.0)
    partial = assess_from_transcript("Please read this sentence carefully", "Please read carefully", 2.0)
    assert perfect["overall_score"] > partial["overall_score"]
    assert perfect["provider"] == "browser-asr"

    wav = Path(tempfile.gettempdir()) / "aienglish_acceptance.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 2500)
    browser = assess_audio(wav, "Hello world", 2.0, transcript="Hello world")
    assert browser["provider"] == "browser-asr"

    status = provider_status()
    assert status["supports_browser_asr"] is True or status["azure_configured"] is True
    assert len(invite_code()) >= 6
    assert len(make_invite()) >= 6

    print("acceptance_smoke: PASS")
    print("pronunciation_provider:", status["provider"])
    print("browser_asr_score:", browser["overall_score"])
    print("NOTE: Full /learn.html E2E needs MySQL + Docker Desktop running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
