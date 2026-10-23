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


def test_every_module_parses_on_the_oldest_supported_python():
    """CI runs 3.9 because that is what Rocky 9 ships and what most of these
    deployments are driven from. Syntax that needs a newer one — a nested
    same-quote f-string, say — compiles fine on a developer's machine and
    fails only there."""
    import shutil
    import subprocess

    oldest = shutil.which("python3.9") or "/usr/bin/python3"
    probe = subprocess.run(
        [oldest, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "3.9":
        pytest.skip("no python3.9 available to parse with")

    result = subprocess.run(
        [oldest, "-m", "compileall", "-q",
         *[str(REPO_ROOT / name) for name in PACKAGES
           if (REPO_ROOT / name).exists()]],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stdout + result.stderr
