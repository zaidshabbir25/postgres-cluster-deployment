#!/usr/bin/env python3
"""Install and point the pgEdge package repository.

The release-RPM/DEB writes a repo file pinned to the `release` channel; the
staging and daily channels are reached by rewriting that channel token in
place, which is how pgEdge's own tooling switches channels.
"""

from aspects import platform_detect
from aspects.prereq_setup import _wait_for_apt_lock

RHEL_REPO_RPM = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
DEB_REPO_DEB = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"

VALID_CHANNELS = ("release", "staging", "daily")

RHEL_REPO_FILE = "/etc/yum.repos.d/pgedge.repo"
DEB_REPO_FILE = "/etc/apt/sources.list.d/pgedge.sources"


def configure(executor, family, channel="release", node=None):
    """Install the pgEdge repo and select a channel. Returns a status message."""
    if channel not in VALID_CHANNELS:
        raise ValueError(
            f"Unknown repository channel {channel!r}; expected one of {VALID_CHANNELS}"
        )

    if family == "rhel":
        return _configure_rhel(executor, channel, node=node)
    return _configure_deb(executor, channel, node=node)


def _configure_rhel(executor, channel, node=None):
    executor.run(
        f"dnf install -y {RHEL_REPO_RPM}",
        node=node,
        message="install pgEdge release RPM",
    )

    if channel != "release":
        executor.run(
            f"sed -i 's|release|{channel}|g' {RHEL_REPO_FILE}",
            node=node,
            message=f"switch repo channel to {channel}",
        )

    executor.try_run("dnf clean expire-cache", node=node)
    executor.run("dnf makecache -q", node=node, message="dnf makecache")
    return f"pgEdge repository configured on rhel (channel={channel})"


def _configure_deb(executor, channel, node=None):
    _wait_for_apt_lock(executor, node=node)
    executor.run(
        f"curl -fsSL {DEB_REPO_DEB} -o /tmp/pgedge-release.deb && "
        f"dpkg -i /tmp/pgedge-release.deb && rm -f /tmp/pgedge-release.deb",
        node=node,
        message="install pgEdge release DEB",
    )

    if channel != "release":
        executor.run(
            f"sed -i 's|release|{channel}|g' {DEB_REPO_FILE}",
            node=node,
            message=f"switch repo channel to {channel}",
        )

    _wait_for_apt_lock(executor, node=node)
    executor.run("apt-get update", node=node, message="apt-get update")
    return f"pgEdge repository configured on deb (channel={channel})"


def available_versions(executor, family, package, node=None):
    """List candidate versions of a package in the configured repo.

    Used by the report layer to record what was on offer, which makes a
    'why did I get 5.0.11?' question answerable after the fact.
    """
    if family == "rhel":
        ok, output = executor.try_run(
            f"dnf --showduplicates list {package} 2>/dev/null", node=node
        )
    else:
        ok, output = executor.try_run(
            f"apt-cache madison {package} 2>/dev/null", node=node
        )
    return output.strip() if ok else ""


def repo_summary(executor, family, node=None):
    """Capture the active repo definition for the deployment report."""
    path = RHEL_REPO_FILE if family == "rhel" else DEB_REPO_FILE
    return executor.fetch_text(path).strip()
