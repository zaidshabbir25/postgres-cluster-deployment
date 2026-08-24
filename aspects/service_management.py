#!/usr/bin/env python3
"""systemd helpers that degrade gracefully.

Some targets (containers, minimal images) have no working systemd. Every
function here reports whether it could act rather than raising, so callers can
fall back to launching a process directly.
"""

import shlex


def has_systemd(executor, node=None):
    ok, output = executor.try_run(
        "systemctl is-system-running 2>/dev/null || true", node=node
    )
    return ok and "offline" not in output and "unknown" not in output.lower() \
        and bool(executor.which("systemctl"))


def unit_exists(executor, unit, node=None):
    ok, output = executor.try_run(
        f"systemctl list-unit-files {shlex.quote(unit)}.service 2>/dev/null", node=node
    )
    return ok and f"{unit}.service" in output


def start(executor, unit, node=None, enable=True):
    """Start (and by default enable) a unit. Returns (ok, output)."""
    verb = "enable --now" if enable else "start"
    return executor.try_run(f"systemctl {verb} {shlex.quote(unit)}", node=node)


def stop(executor, unit, node=None):
    return executor.try_run(f"systemctl stop {shlex.quote(unit)}", node=node)


def restart(executor, unit, node=None):
    return executor.try_run(f"systemctl restart {shlex.quote(unit)}", node=node)


def disable(executor, unit, node=None):
    return executor.try_run(
        f"systemctl disable --now {shlex.quote(unit)} 2>/dev/null || true", node=node
    )


def is_active(executor, unit, node=None):
    ok, output = executor.try_run(
        f"systemctl is-active {shlex.quote(unit)} 2>/dev/null", node=node
    )
    return ok and output.strip() == "active"


def status_text(executor, unit, lines=20, node=None):
    _, output = executor.try_run(
        f"systemctl status {shlex.quote(unit)} --no-pager -l -n {lines} 2>&1 || true",
        node=node,
    )
    return output.strip()


def journal(executor, unit, lines=50, node=None):
    _, output = executor.try_run(
        f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager 2>&1 || true",
        node=node,
    )
    return output.strip()


def write_unit(executor, unit, content, node=None):
    """Install a systemd unit file and reload the daemon."""
    executor.write_file(f"/etc/systemd/system/{unit}.service", content,
                        owner="root", mode="644", node=node)
    executor.try_run("systemctl daemon-reload", node=node)
    return f"/etc/systemd/system/{unit}.service"
