"""Run external protocol tests against an installed wheel, without source masking."""
import argparse
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, required=True, help='Python in the wheel installation environment.')
    parser.add_argument('--opencode', type=Path, required=True, help='Actual OpenCode protocol executable.')
    args = parser.parse_args()
    python, executable = args.python.absolute(), args.opencode.absolute()
    repository = Path(__file__).resolve().parent.parent
    check = subprocess.run([str(python), '-I', '-c',
        ('import pathlib,sys,preprocessing; p=pathlib.Path(preprocessing.__file__).resolve(); '
         'assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()); print(sys.prefix)')],
        cwd=repository.parent, capture_output=True, text=True, timeout=10, check=True)
    installed_root = check.stdout.strip()
    environment = {**os.environ, 'PREPROCESSING_TEST_OPENCODE': str(executable),
                   'PREPROCESSING_TEST_INSTALLED_ROOT': installed_root}
    environment.pop('PYTHONPATH', None)
    # -I excludes the source checkout, cwd, user site, and PYTHONPATH. importlib
    # mode loads test modules without adding their parents to package resolution.
    result = subprocess.run([str(python), '-I', '-m', 'pytest', '--import-mode=importlib', '-q',
        str(repository / 'tests/preprocessing/test_engine_http_real.py')],
        cwd=repository.parent, env=environment, timeout=180, check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
