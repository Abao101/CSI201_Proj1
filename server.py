"""Coursework-compatible entry point; implementation lives in server/."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    implementation = Path(__file__).resolve().parent / 'server'
    sys.path.insert(0, str(implementation))
    runpy.run_path(str(implementation / 'server.py'), run_name='__main__')
