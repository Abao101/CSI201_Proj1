"""Run each test suite in its own process, avoiding client/server import collisions."""
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    failed = False
    for suite in ('server/tests', 'client/tests'):
        print(f'\nRunning {suite}', flush=True)
        result = subprocess.run([sys.executable, '-W', 'default', '-m', 'unittest',
                                 'discover', '-s', suite, '-v'], cwd=root)
        failed = failed or result.returncode != 0
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
