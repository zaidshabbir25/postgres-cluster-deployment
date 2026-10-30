#!/usr/bin/env python3
"""Named cluster shapes, read from configuration/topologies.json.

The wizard asks a dozen questions and most of the answers are the same every
time. A topology is those answers under a name, so a shape that has been agreed
once can be deployed again without anyone retyping it — and reviewed as a diff
rather than recalled from a terminal transcript.

A topology is deliberately *not* a list of machines. Those live in
inventory.json, which means the same shape deploys to staging and to production
without editing it. What it holds is the shape and the versions: how many Spock
nodes, which of them get a standby, what PostgreSQL and Spock to run, which
add-ons, and the ports and names to use.

A definition is written in the groups the wizard asks its questions in —
`hosts`, `install`, `cluster`, `components`, `availability` — because a flat
block of twenty keys gives no clue which of them belong together, and the
wizard's order is the one people already have in their heads. The groups are
presentation only: `get()` flattens them, so everything downstream sees one
flat dictionary and a key is spelled the same wherever it appears.

Those flattened key names match inventory.json's `defaults` block and the
fields of ClusterPlan, so there is one vocabulary across all three rather than
a third spelling of `node_count`.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY_FILE = REPO_ROOT / "configuration" / "topologies.json"


class TopologyError(RuntimeError):
    """The topologies file, or a name in it, cannot be used."""


#: Every key a topology may set, and the flag it becomes. Order matters only
#: for readability of the generated command line.
FLAGS = (
    ("inventory_file", "--inventory"),
    ("deploy_mode", "--mode"),
    ("node_count", "--nodes"),
    ("cluster_name", "--cluster"),
    ("pg_major", "--pg-major"),
    ("pg_version", "--pg-version"),
    ("spock_major", "--spock-major"),
    ("spock_branch", "--spock-branch"),
    ("repo_channel", "--channel"),
    ("db_name", "--db-name"),
    ("db_user", "--db-user"),
    ("base_port", "--base-port"),
    ("base_restapi_port", "--base-restapi-port"),
    ("data_root", "--data-root"),
    ("pg_extensions", "--pg-extensions"),
    ("synchronous_mode", "--sync-mode"),
    ("synchronous_node_count", "--sync-count"),
)

#: Repeatable flags: one occurrence per item.
LIST_FLAGS = (
    ("standby_of", "--standby"),
    ("hba_cidrs", "--hba-cidr"),
)

#: Present-or-absent flags.
BOOL_FLAGS = (
    ("synchronous_mode_strict", "--sync-strict"),
)

#: Keys that describe rather than configure. `host_source` is acted on by the
#: shell before any flag is built, so it is not one either.
IGNORED = ("description", "host_source")

#: The groups a definition is written in, and which keys live in each. They
#: mirror the wizard's questions, in its order. A key in the wrong group is an
#: error naming the right one: silently accepting it anywhere would make the
#: grouping decorative.
GROUPS = (
    ("hosts", ("host_source", "inventory_file")),
    ("install", ("deploy_mode", "repo_channel", "pg_major", "pg_version",
                 "spock_major", "spock_branch")),
    ("cluster", ("node_count", "cluster_name", "db_name", "db_user",
                 "base_port", "base_restapi_port", "data_root", "hba_cidrs")),
    ("components", ()),          # its own shape; see _flatten_components
    ("availability", ("standby_of", "synchronous_mode",
                      "synchronous_node_count", "synchronous_mode_strict")),
)

GROUP_NAMES = tuple(name for name, _ in GROUPS)
GROUP_OF = {key: group for group, keys in GROUPS for key in keys}

VALID_HOST_SOURCES = ("inventory", "localhost")

#: How a component arrives. "none" is spelled out rather than left implicit, so
#: a topology can list every component and say which ones it does not want —
#: the list then reads as a complete answer instead of a partial one.
COMPONENT_NONE = "none"

VALID_MODES = ("packages", "source")
VALID_CHANNELS = ("release", "staging", "daily")
VALID_SYNC = ("off", "on", "quorum", "async", "sync")


def _known_keys():
    return ({key for key, _ in FLAGS}
            | {key for key, _ in LIST_FLAGS}
            | {key for key, _ in BOOL_FLAGS}
            | set(IGNORED))


def load(path=None):
    """Read the topologies file. Returns {name: definition}."""
    path = Path(path or TOPOLOGY_FILE)
    if not path.exists():
        raise TopologyError(f"no topologies file at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TopologyError(f"{path} is not valid JSON: {exc}") from exc

    topologies = payload.get("topologies")
    if not isinstance(topologies, dict) or not topologies:
        raise TopologyError(
            f"{path} has no 'topologies' object. It should map a name to the "
            f"answers for that shape."
        )
    return topologies


def _flatten_components(name, components):
    """Turn the `components` group into a --pg-extensions value.

    Written as one entry per component rather than a packed string, because
    `pgedge-lolor:source@v1.2.0,pgedge-snowflake:packages` is exactly the kind
    of line that gets edited wrongly in a hurry:

        "components": {
          "lolor":     {"install": "packages"},
          "snowflake": {"install": "source", "ref": "v2.0"},
          "ace":       {"install": "none"}
        }

    The packed string is still accepted, as `pg_extensions`, for anyone who
    prefers it or is copying a command line.
    """
    if not isinstance(components, dict):
        raise TopologyError(
            f"topology {name!r}: 'components' must be an object naming each "
            f"component, not {components!r}"
        )

    if "pg_extensions" in components:
        extra = sorted(set(components) - {"pg_extensions"})
        if extra:
            raise TopologyError(
                f"topology {name!r}: 'components' gives both pg_extensions and "
                f"{', '.join(extra)}. Use one form or the other — a packed "
                f"string and per-component entries would disagree."
            )
        return str(components["pg_extensions"])

    entries = []
    for component, spec in components.items():
        if isinstance(spec, str):
            spec = {"install": spec}
        if not isinstance(spec, dict):
            raise TopologyError(
                f"topology {name!r}: component {component!r} must be an object "
                f'like {{"install": "packages"}}, not {spec!r}'
            )
        unknown = sorted(set(spec) - {"install", "ref"})
        if unknown:
            raise TopologyError(
                f"topology {name!r}: component {component!r} has unknown "
                f"key(s) {', '.join(unknown)}. A component says how it arrives "
                f"(install) and, for a source build, what to build (ref)."
            )
        install = str(spec.get("install", "")).strip().lower()
        ref = str(spec.get("ref", "")).strip()
        if install in ("", COMPONENT_NONE, "no", "off", "false"):
            if ref:
                raise TopologyError(
                    f"topology {name!r}: component {component!r} is not "
                    f"installed, so a ref of {ref!r} would be built by nobody"
                )
            continue
        entry = f"{component}:{install}"
        if ref:
            entry += f"@{ref}"
        entries.append(entry)
    return ",".join(entries) or COMPONENT_NONE


def flatten(name, definition):
    """One flat dictionary from a grouped definition.

    Groups are how the file reads; flat keys are what everything else speaks.
    A definition already written flat passes through unchanged, so a file
    predating the groups still loads.
    """
    if not isinstance(definition, dict):
        raise TopologyError(f"topology {name!r} is not an object")

    flat = {}
    for key, value in definition.items():
        if key in GROUP_NAMES:
            if key == "components":
                flat["pg_extensions"] = _flatten_components(name, value)
                continue
            if not isinstance(value, dict):
                raise TopologyError(
                    f"topology {name!r}: group {key!r} must be an object "
                    f"holding its settings, not {value!r}"
                )
            for inner, inner_value in value.items():
                belongs = GROUP_OF.get(inner)
                if belongs and belongs != key:
                    raise TopologyError(
                        f"topology {name!r}: {inner!r} is in group {key!r} but "
                        f"belongs in {belongs!r}"
                    )
                flat[inner] = inner_value
            continue
        flat[key] = value
    return flat


def names(path=None):
    """Topology names, in the order the file lists them."""
    return list(load(path).keys())


def get(name, path=None):
    """One topology by name, validated. Returns its definition."""
    topologies = load(path)
    if name not in topologies:
        raise TopologyError(
            f"unknown topology {name!r}. Available: "
            f"{', '.join(topologies) or '(none)'}"
        )
    definition = flatten(name, topologies[name])
    validate(name, definition)
    return definition


def validate(name, definition):
    """Refuse a topology that cannot produce a working cluster.

    Checked here rather than left to the deployment, because the whole point of
    a named shape is that it was agreed once — a typo in it should surface when
    it is read, not twenty minutes into a source build.
    """
    definition = flatten(name, definition)
    problems = []

    unknown = sorted(set(definition) - _known_keys())
    if unknown:
        known = "; ".join(
            f"{group}: {', '.join(keys)}" for group, keys in GROUPS if keys
        )
        problems.append(
            f"unknown key(s): {', '.join(unknown)}. Known, by group — "
            f"{known}; components: one entry per component"
        )

    source = str(definition.get("host_source", "inventory"))
    if source not in VALID_HOST_SOURCES:
        problems.append(f"host_source must be one of "
                        f"{', '.join(VALID_HOST_SOURCES)}, not {source!r}")
    elif source == "localhost" and definition.get("inventory_file"):
        problems.append("host_source 'localhost' writes an inventory of its "
                        "own, so inventory_file would be ignored")

    mode = definition.get("deploy_mode", "packages")
    if mode not in VALID_MODES:
        problems.append(f"deploy_mode must be one of {', '.join(VALID_MODES)}, "
                        f"not {mode!r}")

    count = definition.get("node_count", 2)
    if not isinstance(count, int) or count < 1:
        problems.append(f"node_count must be a whole number of at least 1, "
                        f"not {count!r}")

    channel = definition.get("repo_channel", "release")
    if channel and channel not in VALID_CHANNELS:
        problems.append(f"repo_channel must be one of "
                        f"{', '.join(VALID_CHANNELS)}, not {channel!r}")

    spock_major = str(definition.get("spock_major", "50"))
    if spock_major not in ("50", "60"):
        problems.append(f"spock_major must be 50 or 60, not {spock_major!r}")

    sync = str(definition.get("synchronous_mode", "off"))
    if sync not in VALID_SYNC:
        problems.append(f"synchronous_mode must be one of "
                        f"{', '.join(VALID_SYNC)}, not {sync!r}")

    # A source build has to fetch one exact tarball; a major alone is not
    # enough, and discovering that after the hosts are prepared is a waste.
    if mode == "source" and not definition.get("pg_version"):
        problems.append("a source build needs an exact pg_version, such as "
                        "17.11 or 19.0beta3")

    if mode == "packages" and definition.get("spock_branch"):
        problems.append("spock_branch applies to a source build; this topology "
                        "installs packages")

    standbys = definition.get("standby_of") or []
    if not isinstance(standbys, list):
        problems.append(f"standby_of must be a list of node names, not "
                        f"{standbys!r}")
    elif isinstance(count, int) and count >= 1:
        # Node names are generated n1..nN, so a standby for n4 in a two-node
        # shape names a node that will never exist. Skipped when node_count is
        # itself wrong: one complaint about that is clearer than two.
        valid = {f"n{index}" for index in range(1, count + 1)}
        unknown_nodes = [n for n in standbys if n not in valid]
        if unknown_nodes:
            problems.append(
                f"standby_of names {', '.join(unknown_nodes)}, but a "
                f"{count}-node cluster has {', '.join(sorted(valid))}"
            )

    if definition.get("pg_extensions"):
        from aspects import pg_extensions

        try:
            pg_extensions.parse(definition["pg_extensions"], default_mode=mode)
        except ValueError as exc:
            problems.append(f"pg_extensions: {exc}")

    if problems:
        raise TopologyError(
            f"topology {name!r} in {TOPOLOGY_FILE.name} cannot be used:\n  "
            + "\n  ".join(problems)
        )
    return True


def to_flags(definition):
    """The command-line flags this topology is equivalent to.

    Returned rather than applied, so the shell can show them and a caller can
    override any of them by appending its own — the last occurrence of a flag
    is what argparse keeps.
    """
    flags = []
    for key, flag in FLAGS:
        value = definition.get(key)
        if value is None or value == "":
            continue
        flags += [flag, str(value)]
    for key, flag in LIST_FLAGS:
        for item in definition.get(key) or []:
            flags += [flag, str(item)]
    for key, flag in BOOL_FLAGS:
        if definition.get(key):
            flags.append(flag)
    return flags


def describe(name, definition):
    """A few lines saying what this shape is, for the preview."""
    standbys = definition.get("standby_of") or []
    mode = definition.get("deploy_mode", "packages")
    source = definition.get("host_source", "inventory")
    where = ("this machine only (localhost, no SSH)" if source == "localhost"
             else definition.get("inventory_file")
             or "the hosts in configuration/inventory.json")
    lines = [
        f"{name}: {definition.get('description', '').strip()}",
        "",
        f"   Machines    : {where}",
        f"   Spock nodes : {definition.get('node_count', 2)}",
        f"   Standbys    : {', '.join(standbys) if standbys else 'none'}",
        f"   PostgreSQL  : {definition.get('pg_major', '17')}"
        + (f" ({definition['pg_version']})" if definition.get("pg_version")
           else ""),
        f"   Spock       : spock{definition.get('spock_major', '50')}"
        + (f" @{definition['spock_branch']}"
           if definition.get("spock_branch") else ""),
        f"   Install     : {mode}"
        + (f" ({definition.get('repo_channel', 'release')} channel)"
           if mode == "packages" else ""),
        f"   Add-ons     : {definition.get('pg_extensions') or 'none'}",
    ]
    return lines
