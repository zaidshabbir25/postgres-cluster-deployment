#!/usr/bin/env python3
"""Platform detection and the per-family naming that follows from it.

Everything that differs between RHEL-family and Debian-family hosts is resolved
here once, so no other module needs an `if rhel:` branch: package names, binary
paths, the postgres user's home, and the build toolchain.

etcd's config layout also differs by family, but that lives in
aspects/etcd_management.py alongside the code that writes it — a source-mode
deployment overrides the family default, so the two cannot be separated.
"""

RHEL_LIKE = {"rhel", "rocky", "almalinux", "alma", "ol", "oracle", "centos", "fedora"}
DEB_LIKE = {"debian", "ubuntu", "raspbian", "linuxmint"}


class UnsupportedPlatform(RuntimeError):
    pass


def detect(executor):
    """Read /etc/os-release and classify the host.

    Returns a dict: family ('rhel'|'deb'), id, version_id, major, arch, pretty.
    """
    raw = executor.fetch_text("/etc/os-release")
    if not raw.strip():
        raise UnsupportedPlatform(f"{executor.host}: cannot read /etc/os-release")

    fields = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"').strip("'")

    os_id = (fields.get("ID") or "").lower()
    id_like = (fields.get("ID_LIKE") or "").lower().split()
    version_id = fields.get("VERSION_ID") or ""
    major = version_id.split(".")[0] if version_id else ""

    if os_id in RHEL_LIKE or any(x in RHEL_LIKE for x in id_like):
        family = "rhel"
    elif os_id in DEB_LIKE or any(x in DEB_LIKE for x in id_like):
        family = "deb"
    else:
        # Fall back to whichever package manager is present.
        if executor.which("dnf") or executor.which("yum"):
            family = "rhel"
        elif executor.which("apt-get"):
            family = "deb"
        else:
            raise UnsupportedPlatform(
                f"{executor.host}: unsupported distribution "
                f"(ID={os_id!r}, ID_LIKE={id_like!r}); no dnf or apt-get found"
            )

    _, arch = executor.try_run("uname -m")

    return {
        "family": family,
        "id": os_id,
        "version_id": version_id,
        "major": major,
        "arch": arch.strip(),
        "pretty": fields.get("PRETTY_NAME") or f"{os_id} {version_id}",
        "codename": (fields.get("VERSION_CODENAME") or "").lower(),
    }


LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "127.0.0.1", "::1"}


def is_loopback(address):
    address = (address or "").strip().lower()
    return address in LOOPBACK_NAMES or address.startswith("127.")


def primary_address(executor):
    """The address other machines would use to reach this host.

    Patroni refuses a loopback name in connect_address — a peer that reads
    "localhost" from the DCS would dial itself — so a host entered as
    localhost still needs a real address to advertise. Falls back to the
    hostname, and finally to 127.0.0.1 for a machine that genuinely has no
    other address.
    """
    probes = (
        # The source address the kernel would pick for an outbound packet:
        # correct on multi-homed hosts, and it needs no DNS.
        "ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \\([0-9.]*\\).*/\\1/p' | head -n 1",
        "hostname -I 2>/dev/null | awk '{print $1}'",
        "hostname -f 2>/dev/null",
    )
    for probe in probes:
        ok, output = executor.try_run(probe)
        candidate = output.strip().splitlines()[0].strip() if output.strip() else ""
        if ok and candidate and not is_loopback(candidate):
            return candidate
    return "127.0.0.1"


def package_manager(family):
    """Return the package-manager verbs for a family."""
    if family == "rhel":
        return {
            "install": "dnf install -y",
            "remove": "dnf remove -y",
            "refresh": "dnf makecache",
            "query": "rpm -q --queryformat '%{VERSION}-%{RELEASE}'",
            "list_installed": "rpm -qa",
        }
    return {
        "install": "DEBIAN_FRONTEND=noninteractive apt-get install -y",
        "remove": "DEBIAN_FRONTEND=noninteractive apt-get remove -y",
        "refresh": "apt-get update",
        "query": "dpkg-query -W -f='${Version}'",
        "list_installed": "dpkg -l",
    }


def pg_bin_dir(family, pg_major):
    if family == "rhel":
        return f"/usr/pgsql-{pg_major}/bin"
    return f"/usr/lib/postgresql/{pg_major}/bin"


def pg_home(family):
    """Home directory of the postgres OS user — where .pgpass belongs."""
    return "/var/lib/pgsql" if family == "rhel" else "/var/lib/postgresql"


def server_packages(family, pg_major, spock_major):
    """Packages that provide PostgreSQL + Spock + dblink.

    Spock is installed first deliberately: it pulls in the matching pgEdge
    PostgreSQL server as a dependency, which guarantees the two are ABI
    compatible. contrib is named separately because zodan's cross-wiring
    procedures call dblink.
    """
    if family == "rhel":
        return [
            f"pgedge-spock{spock_major}_{pg_major}",
            f"pgedge-postgresql{pg_major}-contrib",
        ]
    return [
        f"pgedge-postgresql-{pg_major}-spock{spock_major}",
        f"pgedge-postgresql-{pg_major}",
    ]


def patroni_packages(family):
    """Patroni plus the etcd DCS it talks to."""
    if family == "rhel":
        return ["pgedge-patroni-etcd", "pgedge-etcd"]
    return ["pgedge-patroni", "pgedge-etcd"]


def devel_packages(family, pg_major):
    """Headers and pg_config, needed when building Spock against a packaged server."""
    if family == "rhel":
        return [f"pgedge-postgresql{pg_major}-devel"]
    return [f"pgedge-postgresql-{pg_major}-dev"]


def build_dependencies(family):
    """Toolchain for a from-source PostgreSQL + Spock build."""
    if family == "rhel":
        return [
            "git", "gcc", "gcc-c++", "make", "flex", "bison",
            "readline-devel", "zlib-devel", "openssl-devel",
            "libxml2-devel", "libxslt-devel", "libicu-devel",
            "krb5-devel", "jansson-devel", "perl-IPC-Run",
            "perl-FindBin", "python3-devel", "bzip2", "curl", "tar",
        ]
    return [
        "git", "gcc", "g++", "make", "flex", "bison",
        "libreadline-dev", "zlib1g-dev", "libssl-dev",
        "libxml2-dev", "libxslt1-dev", "libicu-dev",
        "libkrb5-dev", "libjansson-dev", "libipc-run-perl",
        "python3-dev", "bzip2", "curl", "tar", "pkg-config",
    ]


def base_prerequisites(family):
    """Small tools every deployment step assumes are present."""
    if family == "rhel":
        return ["curl", "wget", "which", "procps-ng", "iproute", "sudo", "python3"]
    return ["curl", "wget", "procps", "iproute2", "sudo", "python3", "ca-certificates"]
