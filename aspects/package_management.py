#!/usr/bin/env python3
"""Install, query and remove packages on either family.

Installs are sequential and individually reported: when a five-package set
fails, the deployment report should name the one package that failed rather
than a single opaque dnf transaction error.
"""

import re

from aspects import platform_detect
from aspects.prereq_setup import _wait_for_apt_lock


def install(executor, family, packages, node=None, allow_missing=False):
    """Install packages one at a time.

    Returns (installed, failures) where failures is a list of (package, output).
    Raises RuntimeError on the first failure unless allow_missing is set.
    """
    if isinstance(packages, str):
        packages = [p.strip() for p in packages.split(",") if p.strip()]

    pm = platform_detect.package_manager(family)
    installed, failures = [], []

    for package in packages:
        if family == "deb":
            _wait_for_apt_lock(executor, node=node)
        ok, output = executor.try_run(f"{pm['install']} {package}", node=node)
        if ok:
            installed.append(package)
        else:
            failures.append((package, output.strip()))
            if not allow_missing:
                raise RuntimeError(
                    f"{executor.host}: failed to install {package}\n{output.strip()}"
                )

    return installed, failures


def remove(executor, family, packages, node=None):
    """Remove packages, tolerating ones that are not installed."""
    if isinstance(packages, str):
        packages = [p.strip() for p in packages.split(",") if p.strip()]

    pm = platform_detect.package_manager(family)
    removed = []
    for package in packages:
        ok, _ = executor.try_run(f"{pm['remove']} {package}", node=node)
        if ok:
            removed.append(package)
    return removed


def installed_version(executor, family, package, node=None):
    """Return the installed version string, or None when absent."""
    pm = platform_detect.package_manager(family)
    ok, output = executor.try_run(f"{pm['query']} {package} 2>/dev/null", node=node)
    if not ok:
        return None
    version = output.strip()
    return version or None


def normalize_version(version):
    """Strip packaging noise so an RPM and a DEB version compare equal.

    Debian encodes pre-releases with a tilde (1.0.0~beta2) where RPM uses a
    hyphen; RPM appends a dist tag (.el9, .rocky9); Debian appends a revision
    (-1.jammy). None of that is part of the upstream version.
    """
    if not version:
        return ""
    text = str(version).strip().lower().replace("~", "-")
    text = re.sub(r"\.(el|rocky|alma|oel|ol|fc)\d+.*$", "", text)
    text = re.sub(r"-\d+(\.[a-z]+)?$", "", text)
    text = re.sub(r"^\d+:", "", text)  # drop a deb epoch
    return text


def verify_version(executor, family, package, expected, node=None):
    """Check an installed version against an expected one.

    Returns (ok, installed, message). A blank expected value means 'whatever
    the repo offered is fine' — it records the version without gating on it.
    """
    actual = installed_version(executor, family, package, node=node)
    if actual is None:
        return False, None, f"{package} is not installed"

    if not expected:
        return True, actual, f"{package} {actual} (no pinned version to check)"

    norm_actual = normalize_version(actual)
    norm_expected = normalize_version(expected)
    if norm_expected in norm_actual:
        return True, actual, f"{package} {actual} matches expected {expected}"
    return (
        False,
        actual,
        f"{package} version mismatch: installed {actual}, expected {expected}",
    )


def list_pgedge_packages(executor, family, node=None):
    """Every installed pgEdge package — the inventory the report prints."""
    if family == "rhel":
        ok, output = executor.try_run(
            "rpm -qa --queryformat '%{NAME} %{VERSION}-%{RELEASE}\\n' "
            "| grep -i '^pgedge' | sort",
            node=node,
        )
    else:
        ok, output = executor.try_run(
            "dpkg-query -W -f='${Package} ${Version}\\n' 'pgedge*' 2>/dev/null | sort",
            node=node,
        )
    if not ok:
        return []
    entries = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append({"package": parts[0], "version": parts[1]})
    return entries
