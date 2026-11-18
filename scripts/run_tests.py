"""Run automated tests with configured media tools and write a machine-readable report."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.utils import atomic_json


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--pattern',default='test*.py')
    args=parser.parse_args()
    cfg = load_config()
    os.environ.setdefault("FCF_TEST_FFMPEG", cfg["ffmpeg"])
    os.environ.setdefault("FCF_TEST_FFPROBE", cfg["ffprobe"])
    # Tests must not consume the operator's real API key or Codex profile.
    # Temporary .env fixtures remain readable by configuration tests.
    from core.config import _dotenv
    from core.llm_settings import KEYS
    live_roots={ROOT.resolve(),Path(os.environ.get('FCF_PRIMARY_ROOT') or ROOT).resolve()}
    def test_dotenv(path):
        values=_dotenv(path)
        if Path(path).resolve().parent in live_roots:
            values={k:v for k,v in values.items() if k not in KEYS}
            values['SUBTITLE_REVIEW_ENABLED']='false'
        return values
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"),pattern=args.pattern)
    started = time.monotonic()
    with patch('core.config._dotenv',side_effect=test_dotenv):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
              "success": result.wasSuccessful(), "elapsed_seconds": round(time.monotonic() - started, 2),
              "scope": "Unit tests, mocked API/ASR, synthetic FFmpeg integration, Streamlit AppTest. Not real broadcast/API validation."}
    atomic_json(Path(cfg["output_dir"]) / "test_results.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
