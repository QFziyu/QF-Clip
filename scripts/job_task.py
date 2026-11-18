"""Private single-task process; owned and cancellable by the queue worker."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.jobs import run_task

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    raise SystemExit(run_task(args.database, args.job, args.result))
