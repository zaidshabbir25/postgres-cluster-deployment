#!/usr/bin/env python3
"""A name used but never imported is not caught by any other test here.

Every module under test is driven through fakes, so a module-level mistake
surfaces only on the code path that reaches it — and `add_node` reached
`pg_extensions` for the first time on a real host, several minutes into a
deployment. pyflakes answers that question without running anything.

Only undefined names are treated as failures. Unused imports are a tidiness
question, and failing the suite over one would make this a nuisance rather than
a guard.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("aspects", "deployment", "dashboard", "tests")

pyflakes = pytest.importorskip("pyflakes",
                               reason="pip install -r requirements-dev.txt")


def _findings():
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes",
         *[str(REPO_ROOT / name) for name in PACKAGES if (REPO_ROOT / name).exists()]],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_no_module_uses_a_name_it_never_imported():
    undefined = [line for line in _findings() if "undefined name" in line]

    assert not undefined, "\n".join(undefined)


def test_no_module_fails_to_parse():
    broken = [line for line in _findings()
              if "invalid syntax" in line or "unexpected" in line.lower()]

    assert not broken, "\n".join(broken)
