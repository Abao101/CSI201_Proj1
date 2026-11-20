"""Submission tests entry point. Run from Codes: python3 tests/run_all.py."""
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(subprocess.call([sys.executable, str(root / 'run_tests.py')], cwd=root))
