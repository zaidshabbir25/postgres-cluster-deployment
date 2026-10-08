#!/usr/bin/env python3
"""Optional pgEdge add-ons: lolor, snowflake and ACE.

All three are things a cluster may or may not want, and all three can arrive
two ways — from the pgEdge package repository, or built from a branch or tag of
their own GitHub repository. `--pg-extensions` selects them; the default is
none, and a cluster that asks for none never touches any of this.

They are not all the same kind of thing, and the differences are real:

  * **lolor** and **snowflake** are PGXS extensions. They are built into a
    particular PostgreSQL's prefix, so their package name carries the major
    version, and they are created inside a database with CREATE EXTENSION.
  * **ACE** is a Go command-line tool that talks to the cluster from outside
    it. It is version-independent — one `pgedge-ace` package for every
    PostgreSQL and both families — builds with `go build` rather than PGXS,
    installs a binary onto the host rather than into a database, and has
    nothing to create with CREATE EXTENSION.

What makes the two extensions more than "install a package" is that each
carries a GUC naming the node, and in a multi-master cluster those values must
differ:

  * `snowflake.node` (1–1023) is the node part of every generated id. Its
    default is deliberately invalid, so snowflake.nextval() raises until it is
    set, and two nodes sharing a value generate colliding ids with nothing to
    detect it.
  * `lolor.node` (1–2^28) does the same job for large-object OIDs.

So the id is assigned per Spock node, and a standby deliberately inherits its
leader's: a physical replica is a byte-for-byte copy, and when Patroni promotes
it, it has to keep generating ids as the node it replaced rather than as a new
one.

Neither extension needs shared_preload_libraries, so neither costs a restart.
"""

import re
import shlex
from dataclasses import dataclass

from aspects import package_management, pg_server_management, platform_detect

BUILD_ROOT = "/opt/pgedge/build"

MODE_PACKAGES = "packages"
MODE_SOURCE = "source"
VALID_MODES = (MODE_PACKAGES, MODE_SOURCE)

NONE_VALUES = ("", "none", "no", "off", "false")

# What a selection actually is. An extension lives inside a database and is
# tied to one PostgreSQL; a tool is a binary on the host and is tied to none.
KIND_EXTENSION = "extension"
KIND_TOOL = "tool"

# Where a built tool lands, and the Go toolchain used to build one.
TOOL_BIN_DIR = "/usr/local/bin"
GO_ROOT = "/usr/local/go"
GO_VERSION = "1.26.0"
GO_RELEASE_URL = "https://go.dev/dl/go{version}.linux-{arch}.tar.gz"


@dataclass(frozen=True)
class ExtensionSpec:
    """One optional extension: where it comes from and what it needs."""

    name: str                      # the CREATE EXTENSION name, or the binary
    repo: str                      # git remote for a source build
    default_ref: str = "main"
    kind: str = KIND_EXTENSION
    package_name: str = ""         # fixed name, for something version-independent
    build_command: str = ""        # for a tool: what produces the binary
    binary: str = ""               # for a tool: the file it produces
    module: str = ""               # shared library defining the GUC; default: name
    guc: str = ""                  # per-node identity GUC, if it has one
    guc_max: int = 0
    min_pg_major: int = 0
    summary: str = ""

    @property
    def is_tool(self):
        return self.kind == KIND_TOOL

    @property
    def library(self):
        """The shared library to LOAD before its GUC can be set."""
        return self.module or self.name

    def package(self, family, pg_major):
        """The pgEdge package providing this, for one PostgreSQL major.

        An extension is built into a particular server's prefix, so its package
        name carries the major — and the two families spell that differently,
        neither derivable from the other: RHEL appends the major, Debian puts
        the server in the middle. A tool is independent of the server, so it is
        one package name everywhere and `pg_major` does not enter into it.
        """
        if self.package_name:
            return self.package_name
        if family == "rhel":
            return f"pgedge-{self.name}_{pg_major}"
        return f"pgedge-postgresql-{pg_major}-{self.name}"


CATALOG = {
    "lolor": ExtensionSpec(
        name="lolor",
        repo="https://github.com/pgEdge/lolor.git",
        guc="lolor.node",
        guc_max=2 ** 28,
        min_pg_major=16,
        summary="large objects that replicate — stores them in the lolor "
                "schema instead of the catalog, so Spock can carry them",
    ),
    "snowflake": ExtensionSpec(
        name="snowflake",
        repo="https://github.com/pgEdge/snowflake.git",
        guc="snowflake.node",
        guc_max=1023,
        summary="int8 sequences that embed a node id, so multi-master nodes "
                "cannot generate colliding keys",
    ),
    "ace": ExtensionSpec(
        name="ace",
        repo="https://github.com/pgEdge/ace.git",
        kind=KIND_TOOL,
        package_name="pgedge-ace",
        build_command="go build -o ace ./cmd/ace/",
        binary="ace",
        summary="the Active Consistency Engine — compares nodes and repairs "
                "the differences; a command-line tool, not an in-database "
                "extension",
    ),
}

# What people type. The package is called pgedge-lolor; the extension is lolor.
ALIASES = {}
for _key in CATALOG:
    ALIASES[_key] = _key
    ALIASES[f"pgedge-{_key}"] = _key
    ALIASES[f"pgedge_{_key}"] = _key


def known_names():
    return sorted(f"pgedge-{name}" for name in CATALOG)


def canonical(name):
    """Resolve what the user typed to a catalogue key."""
    key = ALIASES.get(str(name).strip().lower())
    if not key:
        raise ValueError(
            f"unknown extension {name!r}. Available: "
            f"{', '.join(known_names())} (or 'none')"
        )
    return key


# ---------------------------------------------------------------------------
# The --pg-extensions value
# ---------------------------------------------------------------------------

ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9_-]+)"
    r"(?::(?P<mode>[A-Za-z]+))?"
    r"(?:@(?P<ref>[^\s,]+))?$"
)


def parse(value, default_mode=MODE_PACKAGES):
    """Turn a --pg-extensions value into a list of selections.

    Each entry is `name[:mode[@ref]]`:

        pgedge-lolor
        pgedge-lolor:source@v1.2.0,pgedge-snowflake:packages

    The mode defaults to how the cluster itself is being deployed, which is
    almost always what someone means: a source-built cluster has no pgEdge
    repository configured to install a package from.

    Returns [{"name", "mode", "ref"}, ...], ordered and de-duplicated; an empty
    list for "none".
    """
    text = str(value or "").strip()
    if text.lower() in NONE_VALUES:
        return []

    if default_mode not in VALID_MODES:
        raise ValueError(f"unknown mode {default_mode!r}")

    selections = []
    seen = set()
    for raw in re.split(r"[,\s]+", text):
        if not raw:
            continue
        match = ENTRY.match(raw)
        if not match:
            raise ValueError(
                f"cannot read {raw!r} as an extension. Use name, name:mode or "
                f"name:mode@ref — for example pgedge-lolor:source@v1.2.0"
            )
        key = canonical(match.group("name"))
        mode = (match.group("mode") or default_mode).lower()
        if mode == "package":
            mode = MODE_PACKAGES
        if mode not in VALID_MODES:
            raise ValueError(
                f"{raw}: mode must be {' or '.join(VALID_MODES)}, not {mode!r}"
            )
        ref = match.group("ref") or ""
        if mode == MODE_PACKAGES and ref:
            raise ValueError(
                f"{raw}: a ref only applies to a source build — a package "
                f"comes at whatever version the channel offers. Use "
                f"{match.group('name')}:source@{ref}"
            )
        if key in seen:
            raise ValueError(f"{match.group('name')} is listed twice")
        seen.add(key)
        selections.append({"name": key, "mode": mode,
                           "ref": ref or (CATALOG[key].default_ref
                                          if mode == MODE_SOURCE else "")})
    return selections


def unparse(selections):
    """The --pg-extensions string that would reproduce `selections`."""
    if not selections:
        return "none"
    parts = []
    for item in selections:
        text = f"pgedge-{item['name']}:{item['mode']}"
        if item["mode"] == MODE_SOURCE and item.get("ref"):
            text += f"@{item['ref']}"
        parts.append(text)
    return ",".join(parts)


def describe(selections):
    """One line per selection, for a plan preview or a report."""
    lines = []
    for item in selections:
        spec = CATALOG[item["name"]]
        if item["mode"] == MODE_SOURCE:
            origin = f"built from {spec.repo.rsplit('/', 1)[-1]}@{item['ref']}"
        else:
            origin = "from the pgEdge packages"
        lines.append(f"{spec.name}: {origin} — {spec.summary}")
    return lines


def validate(selections, pg_major, node_count=0):
    """Refuse combinations that cannot work, before anything is installed."""
    major = pg_server_management.major_of(str(pg_major))
    try:
        major_number = int(re.sub(r"\D.*$", "", str(major)) or 0)
    except ValueError:
        major_number = 0

    for item in selections:
        spec = CATALOG[item["name"]]
        if spec.min_pg_major and major_number and major_number < spec.min_pg_major:
            raise ValueError(
                f"{spec.name} needs PostgreSQL {spec.min_pg_major} or newer; "
                f"this cluster is on {pg_major}"
            )
        if spec.guc_max and node_count > spec.guc_max:
            raise ValueError(
                f"{spec.name} identifies a node with {spec.guc} in 1.."
                f"{spec.guc_max}, and this cluster has {node_count} nodes"
            )
    return True


# ---------------------------------------------------------------------------
# Per-node identity
# ---------------------------------------------------------------------------


def node_id(plan, node):
    """The value for this node's identity GUCs.

    Taken from the number in the node's name — n3 is 3 — because that is the
    only thing about a node that never moves. Position in the cluster does:
    remove n2 from n1,n2,n3 and n3 slides from third to second, so the next
    node added would be handed 3, which n3 is already generating ids under.
    Two nodes sharing a value is precisely what these GUCs exist to prevent,
    and nothing would detect it until the ids collided.

    Names without a number, or a cluster where two names yield the same one,
    fall back to position — still unique within a single deployment, which is
    where that case arises.

    A standby takes its leader's number: it is a byte-for-byte copy, and when
    Patroni promotes it, it must keep issuing ids as the node it replaced.
    """
    target = node
    if not node.is_spock and node.leader:
        try:
            target = plan.node(node.leader)
        except KeyError:
            target = node

    members = list(plan.spock_nodes)
    if all(n.name != target.name for n in members):
        members = members + [target]

    numbers = {n.name: _number_in(n.name) for n in members}
    usable = [value for value in numbers.values() if value]
    if len(usable) == len(members) and len(set(usable)) == len(members):
        return numbers[target.name]

    for index, member in enumerate(members, start=1):
        if member.name == target.name:
            return index
    return 1


def _number_in(name):
    """The trailing number in a node name, or 0 when it has none."""
    match = re.search(r"(\d+)\s*$", str(name))
    if not match:
        return 0
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


def selections_of(plan):
    """The extensions this cluster asked for, from saved state or a plan."""
    return list(getattr(plan, "pg_extensions", None) or [])


def packages_for(selections, family, pg_major):
    return [CATALOG[item["name"]].package(family, pg_major)
            for item in selections if item["mode"] == MODE_PACKAGES]


def install_packages(executor, family, selections, pg_major, node=None,
                     run_logger=None):
    """Install the packaged extensions on one host. Returns the names."""
    packages = packages_for(selections, family, pg_major)
    if not packages:
        return []
    names, _ = package_management.install(executor, family, packages, node=node)
    if run_logger:
        run_logger.info(f"    {node}: installed {', '.join(packages)}")
    return names


def checkout(executor, spec, ref, node=None):
    """Clone one extension at a branch or tag. Returns (dir, revision)."""
    target = f"{BUILD_ROOT}/{spec.name}"
    executor.run(f"mkdir -p {BUILD_ROOT}", node=node)
    executor.run(f"rm -rf {shlex.quote(target)}", node=node)
    executor.run(
        f"git clone --branch {shlex.quote(ref)} --depth 1 "
        f"{spec.repo} {shlex.quote(target)}",
        node=node, message=f"clone {spec.name}@{ref}",
    )
    _, revision = executor.try_run(
        f"git -C {shlex.quote(target)} rev-parse --short HEAD", node=node
    )
    return target, revision.strip()


def build(executor, spec, ref, bin_dir, node=None, jobs=None, arch=""):
    """Build and install one selection from source. Returns a details dict.

    Which build this is depends on what the thing is, and the two have nothing
    in common beyond the clone: an extension is compiled against a particular
    PostgreSQL with PGXS and installed into its prefix, while a tool is
    compiled with the Go toolchain and installed as a binary on the host.
    """
    source_dir, revision = checkout(executor, spec, ref, node=node)
    details = {"extension": spec.name, "kind": spec.kind, "ref": ref,
               "revision": revision, "source_dir": source_dir}
    if spec.is_tool:
        details.update(_build_tool(executor, spec, source_dir, node=node,
                                   arch=arch))
    else:
        details.update(_build_pgxs(executor, spec, source_dir, bin_dir,
                                   node=node, jobs=jobs))
    return details


def _build_pgxs(executor, spec, source_dir, bin_dir, node=None, jobs=None):
    """What both extension repositories document: pg_config on PATH, then
    `make USE_PGXS=1` and `make USE_PGXS=1 install`."""
    jobs_flag = f"-j{jobs}" if jobs else "-j$(nproc)"
    env = f"PATH={shlex.quote(bin_dir)}:$PATH USE_PGXS=1"

    executor.try_run(f"cd {shlex.quote(source_dir)} && {env} make clean",
                     node=node)
    executor.run(
        f"cd {shlex.quote(source_dir)} && {env} make {jobs_flag}",
        node=node, message=f"make {spec.name}", timeout=1800,
    )
    executor.run(
        f"cd {shlex.quote(source_dir)} && {env} make install",
        node=node, message=f"make install {spec.name}", timeout=900,
    )

    # A PGXS install that produced no control file installed nothing CREATE
    # EXTENSION can find, and the failure would otherwise surface much later.
    ok, share_dir = executor.try_run(
        f"{shlex.quote(bin_dir)}/pg_config --sharedir", node=node
    )
    control = f"{share_dir.strip()}/extension/{spec.name}.control"
    found, _ = executor.try_run(f"test -f {shlex.quote(control)}", node=node)
    if ok and not found:
        raise RuntimeError(
            f"{spec.name} built but {control} is missing after make install — "
            f"nothing for CREATE EXTENSION {spec.name} to load"
        )
    return {"control": control}


def _build_tool(executor, spec, source_dir, node=None, arch=""):
    """What ACE's README documents: install Go, then `go build -o ace ./cmd/ace/`
    and put the binary on PATH."""
    go_version = ensure_go(executor, node=node, arch=arch)
    binary = spec.binary or spec.name
    installed = f"{TOOL_BIN_DIR}/{binary}"

    executor.run(
        f"cd {shlex.quote(source_dir)} && "
        f"PATH={GO_ROOT}/bin:$PATH HOME=/root {spec.build_command}",
        node=node, message=f"go build {spec.name}", timeout=1800,
    )
    executor.run(
        f"install -m 0755 {shlex.quote(source_dir)}/{binary} "
        f"{shlex.quote(installed)}",
        node=node, message=f"install {binary}",
    )
    ran, version = executor.try_run(
        f"{shlex.quote(installed)} --version 2>&1 | head -1", node=node
    )
    if not ran:
        raise RuntimeError(
            f"{spec.name} built but {installed} does not run — the binary is "
            f"there and unusable, which nothing later would report"
        )
    return {"binary": installed, "go_version": go_version,
            "tool_version": version.strip()}


def ensure_go(executor, node=None, arch="", version=GO_VERSION):
    """Install the Go toolchain if the host has none new enough.

    ACE needs Go 1.26 or newer, which no distribution ships yet, so the
    official release tarball is the only way to get one. A host that already
    has a new enough Go keeps it.
    """
    wanted = _go_number(version)
    _, current = executor.try_run("go version 2>/dev/null", node=node)
    match = re.search(r"go(\d+\.\d+(?:\.\d+)?)", current or "")
    if match and _go_number(match.group(1)) >= wanted:
        return match.group(1)

    _, existing = executor.try_run(f"{GO_ROOT}/bin/go version 2>/dev/null",
                                   node=node)
    match = re.search(r"go(\d+\.\d+(?:\.\d+)?)", existing or "")
    if match and _go_number(match.group(1)) >= wanted:
        return match.group(1)

    go_arch = "arm64" if str(arch) in ("aarch64", "arm64") else "amd64"
    url = GO_RELEASE_URL.format(version=version, arch=go_arch)
    tarball = f"/tmp/go{version}.linux-{go_arch}.tar.gz"
    executor.run(f"curl -fL -o {shlex.quote(tarball)} {shlex.quote(url)}",
                 node=node, message=f"download Go {version} ({go_arch})",
                 timeout=900)
    executor.run(
        f"rm -rf {GO_ROOT} && tar -C /usr/local -xzf {shlex.quote(tarball)} && "
        f"rm -f {shlex.quote(tarball)}",
        node=node, message=f"install Go {version}",
    )
    executor.write_file(
        "/etc/profile.d/pgedge-go.sh",
        f"# Managed by pg-cluster-deployment\nexport PATH={GO_ROOT}/bin:$PATH\n",
        owner="root", mode="644", node=node,
    )
    return version


def _go_number(version):
    """1.26.0 -> (1, 26, 0), for comparing without parsing twice."""
    parts = re.findall(r"\d+", str(version))[:3]
    return tuple(int(p) for p in parts) + (0,) * (3 - len(parts))


def build_on_host(executor, plan, host, selections, run_logger=None):
    """Build every source-mode extension on one host. Returns details."""
    from aspects import source_build

    wanted = [item for item in selections if item["mode"] == MODE_SOURCE]
    if not wanted:
        return {}

    # What the build needs depends on what is being built. An extension is
    # compiled against PostgreSQL, so it wants the C toolchain and that
    # server's headers — which a source-built cluster already has from building
    # PostgreSQL itself. A tool compiles against nothing of PostgreSQL's, so
    # putting flex, bison and libicu-devel on a packaged host to produce a Go
    # binary would be a lot of installing for no reason.
    needs_pg_headers = any(not CATALOG[item["name"]].is_tool for item in wanted)
    if needs_pg_headers and plan.deploy_mode != MODE_SOURCE:
        source_build.install_build_dependencies(executor, host.family,
                                                node=host.name)
        devel = platform_detect.devel_packages(host.family, plan.pg_major)
        package_management.install(executor, host.family, devel,
                                   node=host.name, allow_missing=True)
    elif not needs_pg_headers:
        # Only git and the means to fetch a tarball; Go brings its own.
        package_management.install(executor, host.family,
                                   ["git", "curl", "tar"], node=host.name,
                                   allow_missing=True)

    details = {}
    jobs = (plan.source_build or {}).get("jobs")
    arch = (getattr(host, "platform", None) or {}).get("arch", "")
    for item in wanted:
        spec = CATALOG[item["name"]]
        ref = item.get("ref") or spec.default_ref
        if run_logger:
            run_logger.info(f"    {host.name}: building {spec.name}@{ref}")
        details[spec.name] = build(executor, spec, ref, host.bin_dir,
                                   node=host.name, jobs=jobs, arch=arch)
        if run_logger:
            run_logger.info(
                f"    {host.name}: {spec.name} {details[spec.name]['revision']} "
                f"installed"
            )
    return details


# ---------------------------------------------------------------------------
# Creating, in the database
# ---------------------------------------------------------------------------


def is_created(executor, plan, node, spec):
    """Is this extension already registered in the node's database?"""
    found = pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT 1 FROM pg_extension WHERE extname = '{spec.name}';",
        dbname=plan.db_name, node=node.name, default="",
    )
    return bool((found or "").strip())


def create_on_node(executor, plan, node, selections=None, run_logger=None,
                   strict=True):
    """Create each extension and give this node its identity. Returns names.

    The order is forced by how PostgreSQL handles a GUC that an extension
    defines. `lolor.node` does not exist until lolor's shared library has run
    its _PG_init, and ALTER SYSTEM validates against the GUCs the *current
    session* knows — so setting it first fails with "unrecognized configuration
    parameter", and creating the extension is not enough either, because
    CREATE EXTENSION does not load the library into the session.

    So: create the extension, then LOAD the library and ALTER SYSTEM in one
    session, where the second statement can see what the first defined. The
    value lands in postgresql.auto.conf, which every later backend reads as a
    custom placeholder whether or not the library is loaded.

    `strict` is False for a node joining an existing cluster. There, zodan's
    structure sync may already have copied the extension's own tables across
    before this runs, and CREATE EXTENSION would collide with them — the node
    still needs its identity GUC, which is the part that cannot be recovered
    by hand later without knowing what every other node holds.
    """
    selections = selections if selections is not None else selections_of(plan)
    if not selections:
        return []

    identity = node_id(plan, node)
    created = []
    for item in selections:
        spec = CATALOG[item["name"]]
        if spec.is_tool:
            # A tool runs beside the cluster, not inside it. There is nothing
            # to create in a database and no node identity to give it.
            continue

        if not is_created(executor, plan, node, spec):
            statement = f"CREATE EXTENSION IF NOT EXISTS {spec.name};"
            if strict:
                pg_server_management.psql(
                    executor, node.bin_dir, node.pg_port, plan.db_user,
                    statement, dbname=plan.db_name, node=node.name,
                )
            else:
                code, output = pg_server_management.psql(
                    executor, node.bin_dir, node.pg_port, plan.db_user,
                    statement, dbname=plan.db_name, node=node.name, check=False,
                )
                if code != 0:
                    if run_logger:
                        run_logger.warn(
                            f"{node.name}: could not create {spec.name} "
                            f"({output.strip()[-200:]}). Its tables may have "
                            f"arrived through replication already; the node "
                            f"identity is still being set."
                        )
        elif run_logger:
            run_logger.node(node.name,
                            f"{spec.name} is already present — leaving it")

        if spec.guc:
            set_node_identity(executor, plan, node, spec, identity,
                              run_logger=run_logger)

        created.append(spec.name)
        if run_logger:
            run_logger.node(
                node.name,
                f"{spec.name} created"
                + (f", {spec.guc} = {identity}" if spec.guc else "")
            )
    return created


def set_node_identity(executor, plan, node, spec, identity, run_logger=None):
    """LOAD the library, then ALTER SYSTEM its node GUC, in one session.

    Falls back to ALTER SYSTEM alone when the LOAD is refused — a packaged
    build may name its library something other than the extension, and the GUC
    is recognised anyway if the library happens to be preloaded.
    """
    statements = [
        f"LOAD '{spec.library}';",
        f"ALTER SYSTEM SET {spec.guc} = {identity};",
        "SELECT pg_reload_conf();",
    ]
    code, output = pg_server_management.psql_session(
        executor, node.bin_dir, node.pg_port, plan.db_user, statements,
        dbname=plan.db_name, node=node.name, check=False,
    )
    if code == 0:
        return True

    if run_logger:
        run_logger.warn(
            f"{node.name}: could not set {spec.guc} after LOAD "
            f"'{spec.library}' ({output.strip()[-200:]}); trying without it"
        )
    pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"ALTER SYSTEM SET {spec.guc} = {identity};",
        dbname=plan.db_name, node=node.name,
    )
    pg_server_management.psql(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SELECT pg_reload_conf();", dbname=plan.db_name, node=node.name,
    )
    return True


# Tables lolor creates and Spock must carry, or large objects replicate
# nowhere — which is the entire reason lolor exists. Both READMEs say to add
# them to a replication set by hand; the deployment does it once, after
# cross-wiring, because a repset only exists from then on.
REPLICATED_TABLES = {
    "lolor": ("lolor.pg_largeobject", "lolor.pg_largeobject_metadata"),
}


def replicate_tables(executor, plan, node, selections=None, repset="default",
                     run_logger=None):
    """Add each extension's own tables to a Spock replication set.

    Idempotent: spock.repset_add_table on a table already in the set reports
    that and changes nothing, so a re-run or a later node costs nothing.
    Returns (added, problems).
    """
    from aspects import spock_operations

    selections = selections if selections is not None else selections_of(plan)
    added, problems = [], []
    for item in selections:
        for table in REPLICATED_TABLES.get(item["name"], ()):
            ok, output = spock_operations.repset_add_table(
                executor, plan, node, repset, table, sync_data=False
            )
            if ok:
                added.append(table)
            elif "already" in output.lower():
                added.append(table)
            else:
                problems.append(f"{node.name}: {table}: {output.strip()[:160]}")
    if run_logger and added:
        run_logger.node(node.name,
                        f"{', '.join(added)} added to repset {repset}")
    return added, problems


def installed_versions(executor, plan, node, selections=None):
    """What is actually installed on a node, as {extension: version}."""
    selections = selections if selections is not None else selections_of(plan)
    found = {}
    for item in selections:
        spec = CATALOG[item["name"]]
        if spec.is_tool:
            continue
        version = pg_server_management.scalar(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"SELECT extversion FROM pg_extension WHERE extname = "
            f"'{spec.name}';",
            dbname=plan.db_name, node=node.name, default="",
        )
        found[spec.name] = (version or "").strip()
    return found


def tool_versions(executor, selections=None, plan=None, node=None):
    """What the installed tools report, as {name: version line}.

    Separate from installed_versions because a tool leaves no row in
    pg_extension — the only evidence it is there is the binary answering.
    """
    selections = (selections if selections is not None
                  else selections_of(plan) if plan is not None else [])
    found = {}
    for item in selections:
        spec = CATALOG[item["name"]]
        if not spec.is_tool:
            continue
        binary = spec.binary or spec.name
        ok, output = executor.try_run(
            f"command -v {shlex.quote(binary)} >/dev/null 2>&1 && "
            f"{shlex.quote(binary)} --version 2>&1 | head -1", node=node
        )
        found[spec.name] = output.strip() if ok else ""
    return found


def node_identities(executor, plan, node, selections=None):
    """The identity GUCs as the server reports them, for a health report."""
    selections = selections if selections is not None else selections_of(plan)
    values = {}
    for item in selections:
        spec = CATALOG[item["name"]]
        if spec.is_tool or not spec.guc:
            continue
        values[spec.guc] = (pg_server_management.scalar(
            executor, node.bin_dir, node.pg_port, plan.db_user,
            f"SHOW {spec.guc};", dbname=plan.db_name, node=node.name,
            default="",
        ) or "").strip()
    return values
