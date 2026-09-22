#!/usr/bin/env python3
"""PostgreSQL cluster lifecycle and SQL execution.

Patroni owns the clusters it bootstraps, so the initdb/start/stop helpers here
exist for the pieces Patroni does not cover — probing a host's server version
before deciding on GUCs, and running SQL against any node from anywhere in the
deployment.
"""

import re
import shlex

# ---------------------------------------------------------------------------
# output_plugin_libraries
# ---------------------------------------------------------------------------
# PostgreSQL 16.15 / 17.11 / 18.5 / 19.0beta3 began requiring logical decoding
# output plugins to be allow-listed before the server will load them. Spock's
# spock_output plugin is not in the default list, so on those releases every
# cross-wired Spock subscription fails to start until the GUC is set.
#
# Older point releases do not recognise the GUC at all and refuse to start when
# it is present, so it must be gated on the server's actual version — never set
# unconditionally.

OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION = {
    16: "16.15",
    17: "17.11",
    18: "18.5",
    19: "19.0beta3",
}

DEFAULT_OUTPUT_PLUGIN_LIBRARIES = "pgoutput, test_decoding, spock_output"

# initdb with no --encoding inherits the host locale, and cloud images commonly
# run with LANG unset, which yields SQL_ASCII. Several pgEdge extensions refuse
# to install on a non-UTF8 database, so pin it.
DEFAULT_ENCODING = "UTF8"


def parse_pg_version(version):
    """Parse a version string into a sortable tuple.

    Handles releases ("18.5") and pre-releases ("19.0beta3"), ordering them the
    way PostgreSQL does: 19.0beta1 < 19.0beta3 < 19.0rc1 < 19.0.
    """
    if not version:
        return None
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:(beta|rc)(\d+))?", str(version).strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    stage = match.group(3)
    stage_num = int(match.group(4) or 0)
    stage_rank = {"beta": 0, "rc": 1}.get(stage, 2)
    return (major, minor, stage_rank, stage_num)


def requires_output_plugin_libraries(pg_version):
    """True when this server version needs spock_output allow-listed."""
    parsed = parse_pg_version(pg_version)
    if parsed is None:
        # Unknown version — do not risk emitting a GUC the server may reject.
        return False

    major = parsed[0]
    known = sorted(OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION)
    if major > known[-1]:
        return True  # every major newer than we know about has the change
    if major < known[0]:
        return False

    minimum = OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION.get(major)
    if minimum is None:
        return False
    return parsed >= parse_pg_version(minimum)


def output_plugin_libraries_guc(pg_version, libraries=None, quoted=True):
    """Return {'output_plugin_libraries': value} or {} when not required.

    Safe to merge into any GUC dict unconditionally. `quoted=False` is for
    Patroni YAML, which quotes parameter values itself.
    """
    if not requires_output_plugin_libraries(pg_version):
        return {}
    value = libraries or DEFAULT_OUTPUT_PLUGIN_LIBRARIES
    return {"output_plugin_libraries": f"'{value}'" if quoted else value}


def spock_guc_parameters(pg_version):
    """The GUCs every Spock node in a multi-master cluster needs.

    track_commit_timestamp is what lets Spock resolve conflicts by last-write;
    without it a multi-master cluster cannot decide which side wins.
    """
    params = {
        "wal_level": "logical",
        "shared_preload_libraries": "spock",
        "max_worker_processes": "16",
        "max_replication_slots": "16",
        "max_wal_senders": "16",
        "track_commit_timestamp": "on",
        "hot_standby": "on",
        "hot_standby_feedback": "on",
        "wal_log_hints": "on",  # required by pg_rewind, which Patroni uses
    }
    params.update(output_plugin_libraries_guc(pg_version, quoted=False))
    return params


# ---------------------------------------------------------------------------
# Server version probing
# ---------------------------------------------------------------------------


def version_key(version):
    """Sortable key for a PostgreSQL version string.

    Handles '17', '17.11' and pre-releases ('18beta1', '18rc2'), which sort
    below the release they lead up to: 18beta1 < 18rc1 < 18.0.
    """
    text = str(version or "").strip()
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:(beta|rc)(\d+))?", text)
    if not match:
        return (0, 0, 0, 0)
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    stage = {"beta": -2, "rc": -1}.get(match.group(3) or "", 0)
    stage_number = int(match.group(4) or 0)
    return (major, stage, stage_number, minor) if stage else (major, 0, 0, minor)


def is_at_least(candidate, minimum):
    """Is `candidate` the same PostgreSQL version as `minimum`, or newer?"""
    return version_key(candidate) >= version_key(minimum)


def is_exact_version(version):
    """Does this name a single release, rather than just a major?

    A source build fetches one tarball, so "17" is not enough — but "19beta3"
    is: a major that has not reached release has only pre-releases, and they
    carry no minor number.
    """
    return bool(re.match(r"^\d+(\.\d+|beta\d+|rc\d+)", str(version or "").strip()))


def major_of(version):
    """'17.11' -> '17'."""
    match = re.match(r"^(\d+)", str(version or "").strip())
    return match.group(1) if match else ""


def server_version(executor, bin_dir, node=None):
    """Read the installed server's full version, e.g. '17.11'."""
    ok, output = executor.try_run(f"{bin_dir}/postgres --version", node=node)
    if not ok:
        return None
    match = re.search(r"(\d+\.\d+(?:beta\d+|rc\d+)?|\d+(?:beta\d+|rc\d+))", output)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------


def psql(executor, bin_dir, port, user, sql, dbname="postgres",
         host="127.0.0.1", node=None, tuples_only=False, check=True,
         timeout=300):
    """Run one SQL statement with psql. Returns (exit_code, output).

    Connects over TCP rather than the socket so the same call works whether the
    node is Patroni-managed (socket dir varies) or not, and relies on the
    .pgpass written by auth_setup for a password-free connection.
    """
    flags = "-X -q -v ON_ERROR_STOP=1"
    if tuples_only:
        flags += " -At"
    command = (
        f"{bin_dir}/psql {flags} -h {host} -p {port} -U {user} -d {dbname} "
        f"-c {shlex.quote(sql)}"
    )
    if check:
        output = executor.run(command, user=user, node=node, timeout=timeout,
                              message=f"psql :{port} {sql[:80]}")
        return 0, output
    return executor.exec_run(command, user=user, node=node, timeout=timeout)


def psql_file(executor, bin_dir, port, user, remote_path, dbname="postgres",
              host="127.0.0.1", node=None, check=True, timeout=1800):
    """Run a SQL file with psql."""
    command = (
        f"{bin_dir}/psql -X -q -h {host} -p {port} -U {user} -d {dbname} "
        f"-f {shlex.quote(remote_path)}"
    )
    if check:
        output = executor.run(command, user=user, node=node, timeout=timeout,
                              message=f"psql -f {remote_path}")
        return 0, output
    return executor.exec_run(command, user=user, node=node, timeout=timeout)


def scalar(executor, bin_dir, port, user, sql, dbname="postgres", node=None,
           default=None):
    """Run a query and return the first column of the first row as text."""
    code, output = psql(
        executor, bin_dir, port, user, sql, dbname=dbname, node=node,
        tuples_only=True, check=False,
    )
    if code != 0:
        return default
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[0].strip() if lines else default


def rows(executor, bin_dir, port, user, sql, dbname="postgres", node=None,
         separator="\x1f"):
    """Run a query and return its rows as a list of lists of strings.

    Uses an ASCII unit separator rather than a printable delimiter, so column
    values containing pipes or commas — table definitions, DSNs, SQL snippets —
    survive the round trip intact.
    """
    command = (
        f"{bin_dir}/psql -X -q -At -F {shlex.quote(separator)} "
        f"-h 127.0.0.1 -p {port} -U {user} -d {dbname} -c {shlex.quote(sql)}"
    )
    code, output = executor.exec_run(command, user=user, node=node)
    if code != 0:
        return None
    result = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        result.append(line.split(separator))
    return result


def dict_rows(executor, bin_dir, port, user, sql, columns, dbname="postgres",
              node=None):
    """Same as rows(), labelled with the column names the caller selected."""
    raw = rows(executor, bin_dir, port, user, sql, dbname=dbname, node=node)
    if raw is None:
        return None
    return [
        {name: (row[i] if i < len(row) else None) for i, name in enumerate(columns)}
        for row in raw
    ]


def wait_for_ready(executor, bin_dir, port, user, timeout=180, interval=3,
                   node=None):
    """Block until the server answers queries. Returns (ok, message)."""
    attempts = max(1, timeout // interval)
    last = ""
    for _ in range(attempts):
        ok, output = executor.try_run(
            f"{bin_dir}/pg_isready -h 127.0.0.1 -p {port} -U {user}",
            user=user, node=node,
        )
        if ok:
            return True, output.strip()
        last = output.strip()
        executor.try_run(f"sleep {interval}", node=node)
    return False, f"port {port} not accepting connections after {timeout}s: {last}"


# ---------------------------------------------------------------------------
# Cluster lifecycle (non-Patroni paths: source builds, standalone nodes)
# ---------------------------------------------------------------------------


def init_cluster(executor, bin_dir, data_dir, pg_user, guc_parameters=None,
                 encoding=DEFAULT_ENCODING, node=None):
    """initdb a cluster and append GUCs to postgresql.conf."""
    executor.run(f"rm -rf {shlex.quote(data_dir)}", node=node)
    executor.run(f"mkdir -p {shlex.quote(data_dir)}", node=node)
    executor.run(f"chown {pg_user}:{pg_user} {shlex.quote(data_dir)}", node=node)
    executor.run(f"chmod 700 {shlex.quote(data_dir)}", node=node)

    cmd = f"{bin_dir}/initdb -D {shlex.quote(data_dir)} --data-checksums"
    if encoding:
        # --no-locale keeps the C collation while still pinning the encoding;
        # a non-C locale whose codeset disagrees with --encoding is rejected.
        cmd += f" --encoding={encoding} --no-locale"
    executor.run(cmd, user=pg_user, node=node, message=f"initdb {data_dir}")

    if guc_parameters:
        lines = ["", "# --- pg-cluster-deployment managed parameters ---"]
        for key, value in guc_parameters.items():
            rendered = value if str(value).startswith("'") else f"'{value}'"
            lines.append(f"{key} = {rendered}")
        executor.run(
            f"printf '%s\\n' {' '.join(shlex.quote(line) for line in lines)} "
            f">> {shlex.quote(data_dir)}/postgresql.conf",
            user=pg_user, node=node, message="append GUCs",
        )

    return f"cluster initialised at {data_dir}"


def start_server(executor, bin_dir, data_dir, port, pg_user, node=None):
    """Start a cluster with pg_ctl."""
    executor.run(
        f"{bin_dir}/pg_ctl -D {shlex.quote(data_dir)} -o '-p {port}' "
        f"-l {shlex.quote(data_dir)}/logfile -w start",
        user=pg_user, node=node, message=f"pg_ctl start :{port}",
    )
    return f"server started on port {port}"


def stop_server(executor, bin_dir, data_dir, pg_user, mode="fast", node=None):
    """Stop a cluster with pg_ctl, tolerating an already-stopped server."""
    ok, output = executor.try_run(
        f"{bin_dir}/pg_ctl -D {shlex.quote(data_dir)} -m {mode} -w stop",
        user=pg_user, node=node,
    )
    return ok, output.strip()


def reload_conf(executor, bin_dir, port, user, node=None):
    """Apply pending postgresql.conf changes without a restart."""
    return psql(executor, bin_dir, port, user, "SELECT pg_reload_conf();",
                node=node, check=False)
