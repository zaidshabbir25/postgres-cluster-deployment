#!/usr/bin/env python3
"""Load and validate the host inventory, and the per-version config files.

The inventory names machines; it says nothing about topology. Turning hosts
into a node layout is deployment/topology.py's job — keeping the two apart is
what lets the same inventory serve a 2-node and a 6-node deployment.
"""

import json
import os
import re
import shlex
from pathlib import Path

from aspects.cluster_model import Host

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "configuration"
DEFAULT_INVENTORY = CONFIG_DIR / "inventory.json"
EXAMPLE_INVENTORY = CONFIG_DIR / "inventory.example.json"


class InventoryError(RuntimeError):
    pass


def inventory_path(path=None):
    """Resolve which inventory file to use."""
    if path:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = REPO_ROOT / resolved
        if not resolved.exists():
            raise InventoryError(f"Inventory not found: {resolved}")
        return resolved

    env_path = os.environ.get("PG_CLUSTER_INVENTORY")
    if env_path:
        return inventory_path(env_path)

    if DEFAULT_INVENTORY.exists():
        return DEFAULT_INVENTORY

    raise InventoryError(
        f"No inventory file. Copy the example and fill in your hosts:\n"
        f"  cp {EXAMPLE_INVENTORY.relative_to(REPO_ROOT)} "
        f"{DEFAULT_INVENTORY.relative_to(REPO_ROOT)}"
    )


def load(path=None):
    """Read the inventory. Returns (hosts, defaults).

    hosts is a list of Host; defaults is the inventory's `defaults` block,
    which supplies fallbacks for CLI options the user did not pass.
    """
    resolved = inventory_path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError(f"{resolved} is not valid JSON: {exc}") from exc

    entries = raw.get("hosts")
    if entries is None:
        raise InventoryError(f"{resolved}: missing top-level \"hosts\" array")
    if not isinstance(entries, list):
        raise InventoryError(f"{resolved}: \"hosts\" must be an array")

    hosts, seen_names, problems = [], set(), []
    for index, entry in enumerate(entries):
        if entry.get("enabled") is False:
            continue

        name = (entry.get("name") or "").strip()
        address = (entry.get("host") or entry.get("address") or "").strip()
        if not address:
            problems.append(f"hosts[{index}]: \"host\" is required")
            continue
        if not name:
            name = address
        if name in seen_names:
            problems.append(f"hosts[{index}]: duplicate host name {name!r}")
            continue
        seen_names.add(name)

        key_file = (entry.get("key_file") or "").strip() or None
        if key_file:
            key_path = Path(key_file)
            if not key_path.is_absolute():
                key_path = REPO_ROOT / key_path
            if not key_path.exists():
                problems.append(
                    f"hosts[{index}] ({name}): key_file not found: {key_path}"
                )

        hosts.append(
            Host(
                name=name,
                address=address,
                username=(entry.get("username") or "root").strip(),
                key_file=key_file,
                port=int(entry.get("port") or 22),
                local=bool(entry.get("local", False)),
                description=(entry.get("description") or "").strip(),
            )
        )

    if problems:
        raise InventoryError(
            f"{resolved} has problems:\n  " + "\n  ".join(problems)
        )
    if not hosts:
        raise InventoryError(
            f"{resolved}: no enabled hosts. Set \"enabled\": true on at least one."
        )

    defaults = raw.get("defaults") or {}
    return hosts, defaults


def check_key_permissions(hosts):
    """Warn about private keys other users can read.

    OpenSSH refuses such keys outright; paramiko accepts them, so the failure
    would otherwise only show up later as a confusing auth error.
    """
    warnings = []
    for host in hosts:
        if not host.key_file:
            continue
        key_path = Path(host.key_file)
        if not key_path.is_absolute():
            key_path = REPO_ROOT / key_path
        if not key_path.exists():
            continue
        mode = key_path.stat().st_mode & 0o777
        if mode & 0o077:
            warnings.append(
                f"{host.name}: {key_path} is mode {oct(mode)[2:]} — "
                f"run chmod 600 {key_path}"
            )
    return warnings


# ---------------------------------------------------------------------------
# Version config files (configuration/config<major>.env)
# ---------------------------------------------------------------------------

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_version_config(pg_major, config_dir=None):
    """Parse configuration/config<major>.env into a dict.

    These files pin the versions a deployment expects, so a run can be verified
    against a known-good set rather than 'whatever the repo served today'.
    Missing file is not an error — pinning is optional.
    """
    directory = Path(config_dir) if config_dir else CONFIG_DIR
    path = directory / f"config{pg_major}.env"
    if not path.exists():
        return {}

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_LINE.match(stripped)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        # Strip trailing comments outside quotes, then unquote.
        try:
            parts = shlex.split(raw_value, comments=True)
        except ValueError:
            parts = [raw_value]
        values[key] = parts[0] if parts else ""
    return values


def expected_versions(pg_major, spock_major, config_dir=None):
    """Pull the version pins relevant to this deployment out of the config file."""
    config = load_version_config(pg_major, config_dir)
    return {
        "pg_version": config.get("PG_VERSION", ""),
        "spock": config.get(f"PGEDGE_SPOCK{spock_major}_{pg_major}_VERSION", ""),
        "patroni": config.get("PGEDGE_PATRONI_VERSION", ""),
        "etcd": config.get("PGEDGE_ETCD_VERSION", ""),
        "zodan_sql": config.get(f"ZODAN_SQL_SPOCK{spock_major}", ""),
    }
