#!/usr/bin/env python3
"""Get a bare host into a state where the rest of the deployment can run.

Two jobs: make the package manager usable (release the apt lock, enable the
RHEL CRB repo, mute PGDG so it cannot shadow pgEdge packages) and install the
handful of tools later steps shell out to.
"""

from aspects import platform_detect


def _wait_for_apt_lock(executor, timeout=180, node=None):
    """Stop background apt jobs and wait for dpkg locks to clear.

    Fresh cloud images run unattended-upgrades on boot, which holds the dpkg
    lock for minutes. Every apt call in a deployment would race it, so we stop
    the timers once and then wait for the locks rather than retrying blindly.
    """
    executor.try_run(
        "systemctl stop unattended-upgrades apt-daily.service "
        "apt-daily-upgrade.service apt-daily.timer apt-daily-upgrade.timer "
        "2>/dev/null; "
        "systemctl disable unattended-upgrades apt-daily.timer "
        "apt-daily-upgrade.timer 2>/dev/null; true",
        node=node,
    )
    wait_cmd = (
        f"i=0; "
        f"while fuser /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend "
        f"/var/lib/apt/lists/lock >/dev/null 2>&1; do "
        f"  i=$((i+3)); "
        f"  if [ $i -ge {timeout} ]; then echo 'apt lock still held'; exit 1; fi; "
        f"  sleep 3; "
        f"done; echo 'apt lock free'"
    )
    ok, output = executor.try_run(wait_cmd, node=node)
    if not ok:
        raise RuntimeError(f"{executor.host}: dpkg lock never released ({output.strip()})")


def prepare_package_manager(executor, family, node=None):
    """Make the package manager ready for pgEdge installs."""
    if family == "deb":
        _wait_for_apt_lock(executor, node=node)
        executor.run("apt-get update", node=node, message="apt-get update")
        return "apt updated"

    # CodeReady Builder carries build-time deps (perl-IPC-Run and friends) that
    # several pgEdge packages need. The repo id differs per distribution.
    for repo_id in ("crb", "powertools", "ol9_codeready_builder", "ol10_codeready_builder"):
        ok, _ = executor.try_run(
            f"dnf config-manager --set-enabled {repo_id} 2>/dev/null", node=node
        )
        if ok:
            break
    executor.try_run("dnf install -y epel-release 2>/dev/null || true", node=node)
    executor.try_run("dnf makecache -q || true", node=node)
    return "dnf caches refreshed"


def disable_pgdg_repositories(executor, family, node=None):
    """Neutralise community PGDG repos.

    PGDG and pgEdge both ship `postgresql<major>-*` packages. If PGDG is
    enabled the resolver can satisfy Spock's server dependency from PGDG,
    producing a cluster whose server and extension were built against
    different trees — it installs cleanly and then fails to load spock.so.
    """
    if family == "rhel":
        executor.try_run(
            "for f in /etc/yum.repos.d/pgdg*.repo; do "
            "  [ -e \"$f\" ] || continue; "
            "  sed -i 's/^enabled *= *1/enabled=0/' \"$f\"; "
            "done; true",
            node=node,
        )
        executor.try_run(
            "dnf -qy module disable postgresql 2>/dev/null || true", node=node
        )
        return "PGDG repos disabled"

    executor.try_run(
        "for f in /etc/apt/sources.list.d/pgdg*.list "
        "/etc/apt/sources.list.d/pgdg*.sources; do "
        "  [ -e \"$f\" ] || continue; mv \"$f\" \"$f.disabled\"; "
        "done; true",
        node=node,
    )
    # Stop postgresql-common from spinning up a cluster during install; this
    # deployment creates every cluster explicitly, with its own GUCs.
    executor.run("mkdir -p /etc/postgresql-common", node=node)
    executor.write_file(
        "/etc/postgresql-common/createcluster.conf",
        "create_main_cluster = false\n",
        node=node,
    )
    return "PGDG repos disabled, apt auto-cluster creation off"


def install_prerequisites(executor, node=None):
    """Full prerequisite pass for one host. Returns (platform_info, message)."""
    info = platform_detect.detect(executor)
    family = info["family"]

    prepare_package_manager(executor, family, node=node)
    disable_pgdg_repositories(executor, family, node=node)

    pm = platform_detect.package_manager(family)
    packages = platform_detect.base_prerequisites(family)
    if family == "deb":
        _wait_for_apt_lock(executor, node=node)

    # Install individually: a single missing name on an unusual image should not
    # take out the whole prerequisite set.
    missing = []
    for package in packages:
        ok, _ = executor.try_run(f"{pm['install']} {package}", node=node)
        if not ok:
            missing.append(package)

    message = f"{info['pretty']} ({info['arch']})"
    if missing:
        message += f"; optional packages unavailable: {', '.join(missing)}"
    return info, message
