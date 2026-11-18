"""Private detached queue worker. Public entry points live in main.py/core.jobs."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import run_worker


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Football Clip Factory queue worker")
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    raise SystemExit(run_worker({"database": args.database}, isolate=True))
