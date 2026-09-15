#!/usr/bin/env python3
"""Deployment mode 2: build PostgreSQL and Spock from source.

Spock needs core patches, so the order is not negotiable — the PostgreSQL tree
must be patched from `spock/patches/<major>/` *before* it is configured, and
Spock itself is then built with USE_PGXS against the resulting install. A
Spock built against an unpatched server compiles and then fails at load time.

Patroni and etcd are deliberately not built here. Patroni is a Python
application (installed into its own venv, so it cannot break the system
interpreter) and etcd ships official static binaries. Neither benefits from a
source build, and pulling in a Go toolchain to get etcd would double the
prerequisites.
"""

import shlex

from aspects import etcd_management, package_management, platform_detect, service_management

BUILD_ROOT = "/opt/pgedge/build"
INSTALL_ROOT = "/opt/pgedge"
PATRONI_VENV = "/opt/pgedge/patroni-venv"

PG_SOURCE_URL = "https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.bz2"
SPOCK_REPO = "https://github.com/pgEdge/spock.git"
ETCD_RELEASE_URL = (
    "https://github.com/etcd-io/etcd/releases/download/v{version}/"
    "etcd-v{version}-linux-{arch}.tar.gz"
)

DEFAULT_ETCD_VERSION = "3.5.17"


def make_reachable(executor, path, node=None):
    """Let the database user read and run what root just installed.

    Everything here is installed by root, and a hardened image with a 0077
    umask leaves /opt/pgedge and anything created under it unreadable to anyone
    else. Patroni and PostgreSQL both run as the database user, so they fail at
    exec with "bad interpreter: Permission denied".

    Three passes, because one is not enough:
      * every directory from INSTALL_ROOT down needs +x to be traversable —
        `chmod -R a+rX` covers the tree but not the ancestors above it;
      * `-R` skips symlinks, so a venv interpreter that is a link to a file
        inside the tree would keep its mode;
      * these run with `run`, not `try_run`: a chmod that fails silently here
        is the difference between a working deployment and one that dies six
        steps later.
    """
    quoted = shlex.quote(path)
    executor.run(f"chmod a+rx {shlex.quote(INSTALL_ROOT)}", node=node,
                 message=f"make {INSTALL_ROOT} traversable")
    executor.run(f"chmod -R a+rX {quoted}", node=node,
                 message=f"make {path} readable")
    # Directories explicitly: a+rX only adds x to a directory, but an empty
    # tree walked by find is cheap insurance against a stray 0700.
    executor.run(f"find {quoted} -type d -exec chmod a+rx {{}} +", node=node,
                 message=f"make {path} directories traversable")
    return path


def make_venv_reachable(executor, venv, node=None):
    """make_reachable, plus the interpreter the venv's scripts point at.

    A venv's bin/python3 is usually a symlink, and `chmod -R` does not follow
    symlinks. When the target lives inside the install tree it needs the same
    treatment as the rest; when it is the system interpreter it is already
    world-executable and is left alone.
    """
    make_reachable(executor, venv, node=node)
    _, target = executor.try_run(
        f"readlink -f {shlex.quote(venv)}/bin/python3 2>/dev/null", node=node
    )
    target = (target or "").strip().splitlines()
    target = target[-1].strip() if target else ""
    if target.startswith(INSTALL_ROOT):
        executor.try_run(f"chmod a+rx {shlex.quote(target)}", node=node)
    return venv


def install_dir(pg_major):
    return f"{INSTALL_ROOT}/pg{pg_major}"


def bin_dir(pg_major):
    return f"{install_dir(pg_major)}/bin"


def install_build_dependencies(executor, family, node=None):
    """Install the toolchain and headers PostgreSQL's configure looks for."""
    packages = platform_detect.build_dependencies(family)
    installed, failures = package_management.install(
        executor, family, packages, node=node, allow_missing=True
    )
    if failures:
        names = ", ".join(name for name, _ in failures)
        # configure will name whatever is genuinely missing; some of these
        # packages simply do not exist on every image.
        return installed, f"unavailable build packages: {names}"
    return installed, f"{len(installed)} build packages installed"


def ensure_postgres_user(executor, db_user, family, node=None):
    """Create the postgres OS account when no package has made one."""
    home = platform_detect.pg_home(family)
    executor.try_run(
        f"id {shlex.quote(db_user)} >/dev/null 2>&1 || "
        f"useradd --system --home-dir {shlex.quote(home)} --create-home "
        f"--shell /bin/bash {shlex.quote(db_user)}",
        node=node,
    )
    executor.run(f"mkdir -p {shlex.quote(home)}", node=node)
    executor.run(f"chown -R {db_user}:{db_user} {shlex.quote(home)}", node=node)
    return home


def fetch_postgres_source(executor, pg_version, node=None):
    """Download and unpack the PostgreSQL tarball. Returns the source dir."""
    tarball = f"postgresql-{pg_version}.tar.bz2"
    source_dir = f"{BUILD_ROOT}/postgresql-{pg_version}"
    url = PG_SOURCE_URL.format(version=pg_version)

    executor.run(f"mkdir -p {BUILD_ROOT}", node=node)
    executor.run(
        f"cd {BUILD_ROOT} && "
        f"if [ ! -f {tarball} ]; then curl -fL -o {tarball} {shlex.quote(url)}; fi",
        node=node, message=f"download PostgreSQL {pg_version} source",
    )
    executor.run(
        f"cd {BUILD_ROOT} && rm -rf {shlex.quote(source_dir)} && tar xjf {tarball}",
        node=node, message="extract PostgreSQL source",
    )
    return source_dir


def fetch_spock_source(executor, branch="main", node=None):
    """Clone Spock at a branch or tag. Returns the checkout dir."""
    spock_dir = f"{BUILD_ROOT}/spock"
    executor.run(f"mkdir -p {BUILD_ROOT}", node=node)
    executor.run(f"rm -rf {shlex.quote(spock_dir)}", node=node)
    executor.run(
        f"git clone --branch {shlex.quote(branch)} --depth 1 "
        f"{SPOCK_REPO} {shlex.quote(spock_dir)}",
        node=node, message=f"clone spock@{branch}",
    )
    _, revision = executor.try_run(
        f"git -C {shlex.quote(spock_dir)} rev-parse --short HEAD", node=node
    )
    return spock_dir, revision.strip()


def apply_spock_patches(executor, source_dir, spock_dir, pg_major, node=None):
    """Apply Spock's core patches to the PostgreSQL tree, in numeric order.

    Returns (count, detail). A branch with no patch directory for this major is
    a hard error, not an empty success: it means Spock does not support this
    PostgreSQL major and the resulting build would be silently broken.
    """
    patch_dir = f"{spock_dir}/patches/{pg_major}"
    ok, listing = executor.try_run(
        f"ls -1 {shlex.quote(patch_dir)}/*.patch "
        f"{shlex.quote(patch_dir)}/*.diff 2>/dev/null | sort -V", node=node
    )
    patches = [line.strip() for line in listing.splitlines() if line.strip()]

    if not patches:
        raise RuntimeError(
            f"No Spock patches found in {patch_dir}. This Spock branch does not "
            f"support PostgreSQL {pg_major} — pick a different branch or major."
        )

    for patch in patches:
        executor.run(
            f"cd {shlex.quote(source_dir)} && patch -p1 --forward < {shlex.quote(patch)}",
            node=node, message=f"apply {patch.rsplit('/', 1)[-1]}",
        )

    names = ", ".join(p.rsplit("/", 1)[-1] for p in patches)
    return len(patches), names


def build_postgresql(executor, source_dir, pg_major, node=None, jobs=None):
    """configure, make, make install — including contrib for dblink."""
    prefix = install_dir(pg_major)
    jobs_flag = f"-j{jobs}" if jobs else "-j$(nproc)"

    executor.run(
        f"cd {shlex.quote(source_dir)} && ./configure "
        f"--prefix={shlex.quote(prefix)} "
        f"--with-openssl --with-libxml --with-libxslt --with-icu "
        f"--enable-thread-safety",
        node=node, message="configure PostgreSQL", timeout=1800,
    )
    executor.run(
        f"cd {shlex.quote(source_dir)} && make {jobs_flag}",
        node=node, message="make PostgreSQL", timeout=5400,
    )
    executor.run(
        f"cd {shlex.quote(source_dir)} && make install",
        node=node, message="make install PostgreSQL", timeout=1800,
    )
    # zodan's cross-wiring runs through dblink, which lives in contrib.
    executor.run(
        f"cd {shlex.quote(source_dir)}/contrib && make {jobs_flag} && make install",
        node=node, message="build and install contrib", timeout=3600,
    )
    make_reachable(executor, prefix, node=node)
    return prefix


def build_spock(executor, spock_dir, pg_major, node=None, jobs=None):
    """Build Spock against the freshly installed server via PGXS."""
    prefix = install_dir(pg_major)
    jobs_flag = f"-j{jobs}" if jobs else "-j$(nproc)"
    env = f"PATH={prefix}/bin:$PATH USE_PGXS=1"

    executor.try_run(f"cd {shlex.quote(spock_dir)} && {env} make clean", node=node)
    executor.run(
        f"cd {shlex.quote(spock_dir)} && {env} make {jobs_flag}",
        node=node, message="make spock", timeout=3600,
    )
    executor.run(
        f"cd {shlex.quote(spock_dir)} && {env} make install",
        node=node, message="make install spock", timeout=900,
    )

    ok, _ = executor.try_run(
        f"test -f {prefix}/lib/spock.so || test -f {prefix}/lib/postgresql/spock.so",
        node=node,
    )
    if not ok:
        raise RuntimeError(
            f"spock.so is missing under {prefix}/lib after make install — "
            f"the build produced no loadable module"
        )
    make_reachable(executor, prefix, node=node)
    return f"{prefix}/lib/spock.so"


def register_paths(executor, pg_major, node=None):
    """Put the built binaries on PATH and the libraries on the loader path."""
    prefix = install_dir(pg_major)
    executor.write_file(
        "/etc/profile.d/pgedge-source.sh",
        f"# Managed by pg-cluster-deployment\nexport PATH={prefix}/bin:$PATH\n",
        owner="root", mode="644", node=node,
    )
    executor.write_file(
        "/etc/ld.so.conf.d/pgedge-source.conf",
        f"{prefix}/lib\n", owner="root", mode="644", node=node,
    )
    executor.try_run("ldconfig", node=node)
    # Symlink the client tools so psql/pg_isready resolve without a login shell.
    for binary in ("psql", "pg_isready", "pg_config", "pg_basebackup", "pg_ctl",
                   "initdb", "pg_rewind", "pg_controldata"):
        executor.try_run(
            f"ln -sf {prefix}/bin/{binary} /usr/local/bin/{binary}", node=node
        )
    return prefix


def install_patroni_from_pip(executor, family, node=None):
    """Install Patroni into its own venv.

    A venv rather than a system pip install: Debian and RHEL both ship
    externally-managed interpreters now, and installing into them either fails
    outright or risks replacing distro-managed packages.
    """
    venv_package = "python3-venv" if family == "deb" else "python3-pip"
    package_management.install(executor, family, [venv_package], node=node,
                               allow_missing=True)

    executor.run(f"python3 -m venv {PATRONI_VENV}", node=node,
                 message="create patroni venv")
    executor.run(
        f"{PATRONI_VENV}/bin/pip install --upgrade pip wheel",
        node=node, message="upgrade pip", timeout=900,
    )
    executor.run(
        f"{PATRONI_VENV}/bin/pip install 'patroni[etcd3]' psycopg2-binary",
        node=node, message="pip install patroni", timeout=1800,
    )
    for binary in ("patroni", "patronictl"):
        executor.run(
            f"ln -sf {PATRONI_VENV}/bin/{binary} /usr/local/bin/{binary}", node=node
        )
    # Patroni runs as the database user, so the venv — interpreter included —
    # has to be reachable by it, not just by root.
    make_venv_reachable(executor, PATRONI_VENV, node=node)

    _, version = executor.try_run(f"{PATRONI_VENV}/bin/patroni --version", node=node)
    return version.strip() or "patroni installed"


def install_etcd_from_release(executor, arch, version=DEFAULT_ETCD_VERSION, node=None):
    """Install the official etcd static binaries and a systemd unit."""
    etcd_arch = "arm64" if arch in ("aarch64", "arm64") else "amd64"
    url = ETCD_RELEASE_URL.format(version=version, arch=etcd_arch)
    tarball = f"/tmp/etcd-v{version}.tar.gz"

    executor.run(f"curl -fL -o {tarball} {shlex.quote(url)}", node=node,
                 message=f"download etcd {version} ({etcd_arch})", timeout=900)
    executor.run(
        f"tar xzf {tarball} -C /tmp && "
        f"install -m 0755 /tmp/etcd-v{version}-linux-{etcd_arch}/etcd /usr/bin/etcd && "
        f"install -m 0755 /tmp/etcd-v{version}-linux-{etcd_arch}/etcdctl /usr/bin/etcdctl && "
        f"rm -rf /tmp/etcd-v{version}-linux-{etcd_arch} {tarball}",
        node=node, message="install etcd binaries",
    )
    executor.try_run(
        "id etcd >/dev/null 2>&1 || "
        "useradd --system --home-dir /var/lib/etcd --shell /sbin/nologin etcd",
        node=node,
    )

    # Source mode always uses etcd's YAML config, on both families: this unit is
    # ours, so there is no packaged EnvironmentFile convention to match.
    config_path = etcd_management.config_path_for("yaml")
    unit = f"""# Managed by pg-cluster-deployment
[Unit]
Description=etcd key-value store
Documentation=https://etcd.io/docs/
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=etcd
Group=etcd
ExecStart=/usr/bin/etcd --config-file {config_path}
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    service_management.write_unit(executor, "etcd", unit, node=node)

    _, version_output = executor.try_run("etcd --version | head -1", node=node)
    return version_output.strip()


def build_host(executor, host, plan, run_logger=None):
    """Run the whole source build on one host. Returns a details dict."""
    family = host.family
    details = {"host": host.name}

    spec = plan.source_build or {}
    pg_version = spec.get("pg_version") or plan.pg_version
    spock_branch = spec.get("spock_branch", "main")
    etcd_version = spec.get("etcd_version", DEFAULT_ETCD_VERSION)
    jobs = spec.get("jobs")

    if not pg_version or "." not in str(pg_version):
        raise ValueError(
            f"A source build needs a full PostgreSQL version (e.g. 17.11), got "
            f"{pg_version!r}. Set source_build.pg_version or pass --pg-version."
        )

    _, message = install_build_dependencies(executor, family, node=host.name)
    details["dependencies"] = message
    if run_logger:
        run_logger.info(f"    {host.name}: {message}")

    ensure_postgres_user(executor, plan.db_user, family, node=host.name)

    source_dir = fetch_postgres_source(executor, pg_version, node=host.name)
    spock_dir, revision = fetch_spock_source(executor, spock_branch, node=host.name)
    details["spock_revision"] = revision

    count, names = apply_spock_patches(
        executor, source_dir, spock_dir, plan.pg_major, node=host.name
    )
    details["patches"] = names
    if run_logger:
        run_logger.info(f"    {host.name}: applied {count} Spock patch(es)")

    prefix = build_postgresql(executor, source_dir, plan.pg_major,
                              node=host.name, jobs=jobs)
    details["prefix"] = prefix
    if run_logger:
        run_logger.info(f"    {host.name}: PostgreSQL {pg_version} installed to {prefix}")

    details["spock_module"] = build_spock(executor, spock_dir, plan.pg_major,
                                          node=host.name, jobs=jobs)
    register_paths(executor, plan.pg_major, node=host.name)

    details["patroni"] = install_patroni_from_pip(executor, family, node=host.name)
    details["etcd"] = install_etcd_from_release(
        executor, host.platform.get("arch", "x86_64"), etcd_version, node=host.name
    )
    return details
