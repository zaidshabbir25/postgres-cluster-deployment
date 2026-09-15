#!/usr/bin/env bash
#
# pg_deploy_cluster.sh — deploy an n-node PostgreSQL cluster with Patroni + Spock.
#
# With no arguments it asks the questions that decide a deployment (which
# machines to use, packages or source build, how many nodes, which PostgreSQL
# version, which nodes get a standby) and then hands off to deployment/cli.py.
# Pass any flag and it skips the prompts entirely, which is what CI should do.
#
#   ./pg_deploy_cluster.sh
#   ./pg_deploy_cluster.sh --nodes 3 --pg-major 17 --standby n1,n2
#   ./pg_deploy_cluster.sh --mode source --pg-version 17.11 --spock-branch main
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${PG_CLUSTER_VENV:-$SCRIPT_DIR/venv}"
INVENTORY="${PG_CLUSTER_INVENTORY:-$SCRIPT_DIR/configuration/inventory.json}"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; YELLOW=$'\033[33m'
  GREEN=$'\033[32m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; YELLOW=""; GREEN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
info() { printf '%s\n' "${DIM}$*${RESET}"; }
warn() { printf '%s\n' "${YELLOW}WARN  $*${RESET}" >&2; }
die()  { printf '%s\n' "${RED}ERROR $*${RESET}" >&2; exit 1; }
rule() { printf '%s\n' "${DIM}------------------------------------------------------------${RESET}"; }

usage() {
  cat <<'USAGE'
Usage: ./pg_deploy_cluster.sh [options]

Run with no options for the interactive prompts.

Topology
  --nodes N                 number of Spock (multi-master) nodes           [2]
  --standby n1[,n2]         Spock nodes that get a Patroni standby
  --cluster NAME            cluster name, used for Patroni scopes          [pgedge]

Deployment method
  --mode packages|source    native pgEdge packages, or build from source   [packages]
  --channel release|staging|daily
                            pgEdge repository channel (packages mode)      [release]
  --spock-branch BRANCH     Spock git branch (source mode)                 [main]
  --etcd-version V          etcd release to install (source mode)          [3.5.17]
  --jobs N                  make -j value (source mode)

Versions
  --pg-major N              PostgreSQL major version                       [17]
  --pg-version X.Y          exact version; required for --mode source
  --spock-major 50|60       Spock major version                            [50]

Database
  --db-name NAME                                                           [postgres]
  --db-user NAME                                                           [postgres]
  --db-password PASS        default: $PG_CLUSTER_DB_PASSWORD, else postgres

Placement
  --base-port N             first PostgreSQL port on each host             [5432]
  --base-restapi-port N     first Patroni REST port on each host           [8008]
  --data-root PATH          parent of every node's data directory          [/var/lib/pgedge]
  --hba-cidr CIDR           extra network to allow in pg_hba (repeatable)

Behaviour
  --inventory PATH          host inventory                 [configuration/inventory.json]
  --clean                   wipe any previous deployment on these hosts first
  --skip-verify             skip the replication verification step
  --dry-run                 print the planned topology and exit
  -h, --help                this message

Growing a running cluster
  --add-node NAME           add one node to a deployed cluster and exit. Asks
                            for its role and PostgreSQL version unless the
                            flags below supply them.
  --role leader|standby     leader: a Spock node, cross-wired to every peer and
                            leading a scope of its own. standby: a physical
                            replica of one existing node.              [leader]
  --leader NODE             with --role standby, the node it follows
  --pg-version X.Y          PostgreSQL for the new node; must match the cluster
                            or be newer. Not valid for a standby, which copies
                            its leader.                    [the cluster's]
  --spock-major 50|60       Spock major for the new node   [the cluster's]
  --spock-branch BRANCH     Spock git branch or tag to build, for a source-built
                            cluster                        [the cluster's]
  --host NAME               host from the inventory to place it on
                            [a host with no Spock node yet]
  --source NODE             existing node to join through                [n1]

Cleanup
  --cleanup                 scrub every host in the inventory and exit: Patroni
                            units, etcd state, data directories, cluster config
                            and saved state. Works after a failed deployment,
                            when there is no cluster state to remove.
  --purge                   with --cleanup, also uninstall the pgEdge packages
                            and repository
  --yes                     with --cleanup, skip the confirmation prompt

Other commands
  ./pg_cluster_ctl.sh       operate a deployed cluster:
                              node    list / add / remove / command / ssh / psql
                              spock   repsets, subscriptions, DDL, sequences
                              db      databases, GUCs, read-only, IO test
                              service Patroni / PostgreSQL / etcd control
                              package native package inventory and upgrades
                              app     pgbench and sample workloads
                              diff    do the nodes actually agree?
  ./pg_cluster_status.sh    health of a deployed cluster
  ./pg_dashboard.sh         live web dashboard
USAGE
}

# ---------------------------------------------------------------------------
# Virtualenv
# ---------------------------------------------------------------------------

ensure_venv() {
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    info "Using the already-active virtualenv: $VIRTUAL_ENV"
  elif [[ -f "$VENV_DIR/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    info "Activated $VENV_DIR"
  else
    command -v python3 >/dev/null 2>&1 || die "python3 is required but not installed."
    say "Creating a virtualenv in $VENV_DIR (first run only)..."
    python3 -m venv "$VENV_DIR" || die "could not create a virtualenv at $VENV_DIR"
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$SCRIPT_DIR/requirements.txt" \
      || die "could not install dependencies from requirements.txt"
    say "${GREEN}Dependencies installed.${RESET}"
  fi

  python3 - <<'PY' || die "dependencies are missing — run: pip install -r requirements.txt"
import importlib.util, sys
missing = [m for m in ("paramiko", "yaml") if not importlib.util.find_spec(m)]
if missing:
    print("missing modules: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
}

# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

check_inventory() {
  # Non-interactive runs take the inventory as given; the prompts can build one.
  if [[ ! -f "$INVENTORY" ]]; then
    say ""
    warn "No inventory at $INVENTORY"
    say "The deployment needs to know which machines to use. Either run this"
    say "script with no flags and answer the host questions, or copy the"
    say "example and fill it in yourself:"
    say ""
    say "  cp configuration/inventory.example.json configuration/inventory.json"
    say "  \$EDITOR configuration/inventory.json"
    say ""
    exit 1
  fi
}

host_count() {
  python3 - "$INVENTORY" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(0); sys.exit(0)
hosts = [h for h in data.get("hosts", []) if h.get("enabled") is not False]
print(len(hosts))
PY
}

show_hosts() {
  python3 - "$INVENTORY" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  (could not read the inventory: {exc})"); sys.exit(0)
hosts = [h for h in data.get("hosts", []) if h.get("enabled") is not False]
if not hosts:
    print("  (no enabled hosts)"); sys.exit(0)
for h in hosts:
    name = h.get("name") or h.get("host")
    desc = h.get("description") or ""
    print(f"  {name:<18} {h.get('host'):<20} {h.get('username','root'):<10} {desc}")
PY
}

distinct_machine_count() {
  # Hosts that are really the same box (same address) count once: ports, not
  # machines, are what separate nodes sharing one address.
  python3 - "$INVENTORY" <<'PYEOF'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print(0); sys.exit(0)
seen = {
    (h.get("host") or h.get("address") or "").strip().lower()
    for h in data.get("hosts", [])
    if h.get("enabled") is not False
}
seen.discard("")
print(len(seen))
PYEOF
}

write_inventory() {
  # write_inventory <host-spec>...  where each spec is
  #   name|address|username|ssh_port|key_file|local
  # The defaults block is carried over from the existing inventory when there
  # is one, so answering the host questions again does not reset the rest.
  python3 - "$INVENTORY" "$SCRIPT_DIR/configuration/inventory.example.json" "$@" <<'PYEOF'
import json, os, shutil, sys

target, example = sys.argv[1], sys.argv[2]

defaults = {}
for source in (target, example):
    if not os.path.exists(source):
        continue
    try:
        defaults = json.load(open(source)).get("defaults") or {}
    except Exception:
        defaults = {}
    if defaults:
        break

hosts = []
for spec in sys.argv[3:]:
    name, address, username, ssh_port, key_file, local = spec.split("|")
    entry = {
        "name": name,
        "host": address,
        "username": username,
        "enabled": True,
    }
    if local == "yes":
        entry["local"] = True
    else:
        entry["key_file"] = key_file
        entry["port"] = int(ssh_port or 22)
    hosts.append(entry)

if os.path.exists(target):
    shutil.copy2(target, target + ".bak")

payload = {
    "_comment": "Written by ./pg_deploy_cluster.sh. Edit it by hand or answer "
                "the host questions again; the previous file is kept as "
                "inventory.json.bak.",
    "defaults": defaults,
    "hosts": hosts,
}
os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
    handle.write("\n")
PYEOF
}

cluster_facts() {
  # cluster_facts [name] ->
  #   "<cluster>|<pg version>|<spock nodes>|<all nodes>|<mode>|<spock major>|<branch>"
  # Empty when nothing is deployed yet.
  python3 - "${1:-}" <<'PYEOF'
import sys
from aspects import state
try:
    name = sys.argv[1] or state.latest_cluster()
    if not name:
        raise SystemExit(0)
    plan, _ = state.load(name)
except Exception:
    raise SystemExit(0)
print("|".join([
    name,
    plan.pg_version or plan.pg_major,
    ",".join(n.name for n in plan.spock_nodes),
    ",".join(n.name for n in plan.nodes),
    plan.deploy_mode,
    str(plan.spock_major),
    (plan.source_build or {}).get("spock_branch", "main"),
]))
PYEOF
}

version_at_least() {
  # version_at_least <candidate> <minimum>
  #   0 = candidate is the same or newer
  #   1 = candidate is older
  #   2 = could not compare here — say nothing and let the deployment decide,
  #       which is the only place the rule has to hold
  # Shares pg_server_management's comparison so the prompt and the deployment
  # agree on what counts as newer.
  python3 - "$1" "$2" <<'PYEOF'
import sys
try:
    from aspects.pg_server_management import is_at_least
except Exception:
    sys.exit(2)
sys.exit(0 if is_at_least(sys.argv[1], sys.argv[2]) else 1)
PYEOF
}

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

ask() {
  # ask <prompt> <default> -> echoes the answer on stdout.
  # bash sends `read -p` prompts to stderr, so command substitution around this
  # captures the answer alone. Reading from stdin (not /dev/tty) keeps the
  # prompts scriptable: answers can be piped in for a rehearsal or a test.
  local prompt="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " reply || true
    printf '%s' "${reply:-$default}"
  else
    read -r -p "$prompt: " reply || true
    printf '%s' "$reply"
  fi
}

ask_choice() {
  # ask_choice <prompt> <default> <valid...> -> echoes a validated answer
  local prompt="$1" default="$2"; shift 2
  local valid=("$@") reply
  while true; do
    reply="$(ask "$prompt" "$default")"
    for option in "${valid[@]}"; do
      [[ "$reply" == "$option" ]] && { printf '%s' "$reply"; return 0; }
    done
    warn "Enter one of: ${valid[*]}"
  done
}

ask_int() {
  # ask_int <prompt> <default> <min> <max>
  local prompt="$1" default="$2" min="$3" max="$4" reply
  while true; do
    reply="$(ask "$prompt" "$default")"
    if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= min && reply <= max )); then
      printf '%s' "$reply"; return 0
    fi
    warn "Enter a whole number between $min and $max."
  done
}

# ---------------------------------------------------------------------------
# Argument parsing — any flag turns off the prompts
# ---------------------------------------------------------------------------

INTERACTIVE=true
ARGS=()
DRY_RUN=false
CLEANUP=false
CLEANUP_ARGS=()
ADD_NODE=""
NODE_ARGS=()
NODE_ROLE=""
NODE_LEADER=""
NODE_PG_VERSION=""
NODE_SPOCK_MAJOR=""
NODE_SPOCK_BRANCH=""
CLUSTER_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --inventory) INVENTORY="$2"; ARGS+=("--inventory" "$2"); INTERACTIVE=false; shift 2 ;;
    --dry-run) DRY_RUN=true; ARGS+=("--dry-run"); INTERACTIVE=false; shift ;;
    --cleanup) CLEANUP=true; INTERACTIVE=false; shift ;;
    --purge|--yes) CLEANUP_ARGS+=("$1"); shift ;;
    --add-node)
      [[ $# -ge 2 ]] || die "--add-node needs a node name (e.g. --add-node n3)"
      ADD_NODE="$2"; INTERACTIVE=false; shift 2 ;;
    --host|--source|--role|--leader)
      [[ $# -ge 2 ]] || die "$1 needs a value"
      [[ "$1" == "--role" ]]   && NODE_ROLE="$2"
      [[ "$1" == "--leader" ]] && NODE_LEADER="$2"
      NODE_ARGS+=("$1" "$2"); shift 2 ;;
    --clean|--skip-verify|--json)
      ARGS+=("$1"); INTERACTIVE=false; shift ;;
    --nodes|--standby|--cluster|--mode|--channel|--spock-branch|--etcd-version|\
    --jobs|--pg-major|--pg-version|--spock-major|--db-name|--db-user|--db-password|\
    --base-port|--base-restapi-port|--data-root|--hba-cidr|--zodan-sql)
      [[ $# -ge 2 ]] || die "$1 needs a value"
      [[ "$1" == "--cluster" ]] && CLUSTER_NAME="$2"
      [[ "$1" == "--pg-version" ]] && NODE_PG_VERSION="$2"
      [[ "$1" == "--spock-major" ]] && NODE_SPOCK_MAJOR="$2"
      [[ "$1" == "--spock-branch" ]] && NODE_SPOCK_BRANCH="$2"
      ARGS+=("$1" "$2"); INTERACTIVE=false; shift 2 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

ensure_venv
[[ "$INTERACTIVE" == true ]] || check_inventory

# ---------------------------------------------------------------------------
# Cleanup — a mode of its own, not a deployment
# ---------------------------------------------------------------------------

if [[ -n "$ADD_NODE" ]]; then
  [[ "$ADD_NODE" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]] \
    || die "node name '$ADD_NODE' must start with a letter and contain only letters, digits or underscores"
  [[ "$CLEANUP" == false ]] || die "--add-node and --cleanup do the opposite of each other; pick one"
  FACTS="$(cluster_facts "$CLUSTER_NAME")"
  [[ -n "$FACTS" ]] || die "no deployed cluster found — deploy one before adding a node"
  IFS='|' read -r FACT_CLUSTER FACT_PG FACT_SPOCK FACT_ALL FACT_MODE \
    FACT_SPOCK_MAJOR FACT_BRANCH <<<"$FACTS"

  say ""
  say "${BOLD}Adding node $ADD_NODE to cluster '$FACT_CLUSTER'${RESET}"
  rule
  say "   PostgreSQL : $FACT_PG"
  say "   Spock      : spock${FACT_SPOCK_MAJOR}${FACT_MODE:+ ($FACT_MODE)}"
  say "   Spock nodes: ${FACT_SPOCK:-none}"
  say "   All nodes  : ${FACT_ALL:-none}"
  rule
  say ""

  # --- role ----------------------------------------------------------
  if [[ -z "$NODE_ROLE" ]]; then
    say "${BOLD}1) What should $ADD_NODE be?${RESET}"
    say "   1) Spock node  — a multi-master peer, cross-wired to every other node,"
    say "                    leading a Patroni scope of its own"
    say "   2) Standby     — a physical replica of one existing node, which Patroni"
    say "                    can promote if that node fails"
    say ""
    if [[ "$(ask_choice "   Choose 1 or 2" "1" 1 2)" == "2" ]]; then
      NODE_ROLE="standby"
    else
      NODE_ROLE="leader"
    fi
    NODE_ARGS+=(--role "$NODE_ROLE")
    say ""
  fi

  # --- the leader a standby follows -----------------------------------
  if [[ "$NODE_ROLE" == "standby" && -z "$NODE_LEADER" ]]; then
    [[ -n "$FACT_SPOCK" ]] || die "this cluster has no Spock node for a standby to follow"
    say "${BOLD}2) Which node should $ADD_NODE stand by for?${RESET}"
    say "   Spock nodes: $FACT_SPOCK"
    say "   ${DIM}A standby is a copy of one node only; it can never be promoted"
    say "   into a different node's data.${RESET}"
    say ""
    DEFAULT_LEADER="${FACT_SPOCK%%,*}"
    # Bounded, so a closed stdin ends the run instead of spinning on the default.
    for _ in 1 2 3 4 5; do
      NODE_LEADER="$(ask "   Leader node" "$DEFAULT_LEADER")"
      [[ ",$FACT_SPOCK," == *",$NODE_LEADER,"* ]] && break
      warn "'$NODE_LEADER' is not a Spock node in this cluster ($FACT_SPOCK)."
      NODE_LEADER=""
    done
    [[ -n "$NODE_LEADER" ]] || die "no leader chosen for the standby"
    NODE_ARGS+=(--leader "$NODE_LEADER")
    say ""
  fi

  # --- PostgreSQL version ---------------------------------------------
  # A standby is a byte-for-byte copy of its leader, so its version is not a
  # choice; only a Spock node gets asked.
  if [[ "$NODE_ROLE" != "standby" ]]; then
    if [[ -z "$NODE_PG_VERSION" ]]; then
      say "${BOLD}3) Which PostgreSQL version should $ADD_NODE run?${RESET}"
      say "   ${DIM}The cluster runs $FACT_PG. A new node may match it or be newer —"
      say "   an older one cannot replay what its peers produce.${RESET}"
      say ""
      for _ in 1 2 3 4 5; do
        NODE_PG_VERSION="$(ask "   PostgreSQL version" "$FACT_PG")"
        if [[ ! "$NODE_PG_VERSION" =~ ^[0-9]+(\.[0-9]+|beta[0-9]+|rc[0-9]+)?$ ]]; then
          warn "Enter a version like $FACT_PG."
          NODE_PG_VERSION=""
          continue
        fi
        version_at_least "$NODE_PG_VERSION" "$FACT_PG" && break
        [[ $? -eq 2 ]] && break   # comparison unavailable; the deployment checks it
        warn "PostgreSQL $NODE_PG_VERSION is older than the cluster's $FACT_PG — enter $FACT_PG or newer."
        NODE_PG_VERSION=""
      done
      [[ -n "$NODE_PG_VERSION" ]] || die "no usable PostgreSQL version given"
      say ""
    else
      version_at_least "$NODE_PG_VERSION" "$FACT_PG" || [[ $? -eq 2 ]] \
        || die "PostgreSQL $NODE_PG_VERSION is older than the cluster's $FACT_PG; a new node must match it or be newer"
    fi
    # Appended here, not at the prompt: --pg-version may also have arrived as a
    # flag, which the deploy parser captured.
    NODE_ARGS+=(--pg-version "$NODE_PG_VERSION")
  elif [[ -n "$NODE_PG_VERSION" ]]; then
    die "--pg-version does not apply to a standby: it runs the same version as its leader"
  fi

  # --- Spock version and branch ---------------------------------------
  # A standby copies its leader byte for byte, so it runs the leader's Spock.
  if [[ "$NODE_ROLE" != "standby" ]]; then
    if [[ -z "$NODE_SPOCK_MAJOR" ]]; then
      say "${BOLD}4) Which Spock version should $ADD_NODE run?${RESET}"
      say "   ${DIM}The cluster runs spock${FACT_SPOCK_MAJOR}. The two majors differ in the"
      say "   replication API, so a mixed mesh is a migration step, not a"
      say "   resting state.${RESET}"
      say ""
      NODE_SPOCK_MAJOR="$(ask_choice "   Spock major version (50 or 60)" "$FACT_SPOCK_MAJOR" 50 60)"
      say ""
    fi
    NODE_ARGS+=(--spock-major "$NODE_SPOCK_MAJOR")

    if [[ "$FACT_MODE" == "source" ]]; then
      if [[ -z "$NODE_SPOCK_BRANCH" ]]; then
        say "${BOLD}5) Which Spock branch should $ADD_NODE build from?${RESET}"
        say "   ${DIM}This cluster is built from source, so Spock is compiled from a"
        say "   git branch or tag of github.com/pgEdge/spock.${RESET}"
        say ""
        NODE_SPOCK_BRANCH="$(ask "   Spock branch or tag" "${FACT_BRANCH:-main}")"
        say ""
      fi
      NODE_ARGS+=(--spock-branch "$NODE_SPOCK_BRANCH")
    elif [[ -n "$NODE_SPOCK_BRANCH" ]]; then
      die "--spock-branch applies to a source-built cluster; this one installs packages"
    fi
  elif [[ -n "$NODE_SPOCK_MAJOR" || -n "$NODE_SPOCK_BRANCH" ]]; then
    die "--spock-major and --spock-branch do not apply to a standby: it runs exactly what its leader runs"
  fi

  say "${DIM}The node is prepared, then bootstrapped and joined to the cluster."
  say "Existing nodes stay up throughout.${RESET}"
  say ""
  exec python3 -m deployment.cli node add --name "$ADD_NODE" \
    --inventory "$INVENTORY" \
    ${CLUSTER_NAME:+--cluster "$CLUSTER_NAME"} \
    "${NODE_ARGS[@]+"${NODE_ARGS[@]}"}"
fi

if [[ "$CLEANUP" == true ]]; then
  say ""
  say "${BOLD}Host cleanup${RESET}"
  rule
  say "Hosts in $(basename "$INVENTORY") ($(host_count)):"
  show_hosts
  rule
  say ""
  exec python3 -m deployment.cli cleanup --inventory "$INVENTORY" \
    "${CLEANUP_ARGS[@]+"${CLEANUP_ARGS[@]}"}"
fi

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

if [[ "$INTERACTIVE" == true ]]; then
  say ""
  say "${BOLD}PostgreSQL cluster deployment — Patroni + Spock${RESET}"
  rule
  say ""

  # --- 1. hosts ------------------------------------------------------
  say "${BOLD}1) Which machines should the cluster run on?${RESET}"
  HOSTS="$( [[ -f "$INVENTORY" ]] && host_count || echo 0 )"
  if (( HOSTS > 0 )); then
    say "   $(basename "$INVENTORY") currently lists $HOSTS host(s):"
    show_hosts
    say ""
    say "   1) Use these hosts"
    say "   2) Enter host details now (rewrites $(basename "$INVENTORY"))"
    say "   3) This machine only — localhost, no SSH"
    say ""
    HOST_CHOICE="$(ask_choice "   Choose 1, 2 or 3" "1" 1 2 3)"
  else
    say "   ${DIM}No inventory yet, so the hosts have to be entered here.${RESET}"
    say ""
    say "   2) Enter host details now"
    say "   3) This machine only — localhost, no SSH"
    say ""
    HOST_CHOICE="$(ask_choice "   Choose 2 or 3" "2" 2 3)"
  fi

  if [[ "$HOST_CHOICE" == "3" ]]; then
    write_inventory "local|localhost|$(id -un)|22||yes"
    say "   ${GREEN}Everything will run on this machine; nodes are separated by port.${RESET}"
  elif [[ "$HOST_CHOICE" == "2" ]]; then
    say ""
    say "   ${DIM}Each machine needs an address reachable over SSH and a user"
    say "   with passwordless sudo. Enter localhost for this machine.${RESET}"
    MACHINES="$(ask_int "   How many machines" "2" 1 32)"
    HOST_SPECS=()
    for ((m = 1; m <= MACHINES; m++)); do
      say ""
      say "   ${BOLD}Machine $m${RESET}"
      HOST_ADDR=""
      while [[ -z "$HOST_ADDR" ]]; do
        HOST_ADDR="$(ask "     Hostname or IP" "")"
        [[ -n "$HOST_ADDR" ]] || warn "An address is required."
      done
      HOST_IS_LOCAL=no
      case "$HOST_ADDR" in
        localhost|127.0.0.1|::1) HOST_IS_LOCAL=yes ;;
      esac
      if [[ "$HOST_IS_LOCAL" == yes ]]; then
        HOST_USER="$(ask "     User to run as" "$(id -un)")"
        HOST_SSH_PORT=22
        HOST_KEY=""
        say "     ${DIM}localhost — commands run directly, no SSH.${RESET}"
      else
        HOST_USER="$(ask "     SSH username" "root")"
        HOST_SSH_PORT="$(ask_int "     SSH port" "22" 1 65535)"
        HOST_KEY="$(ask "     SSH private key file (empty to use your ssh-agent)" "")"
      fi
      HOST_NAME="$(ask "     Name for this machine" "$HOST_ADDR")"
      HOST_SPECS+=("$HOST_NAME|$HOST_ADDR|$HOST_USER|$HOST_SSH_PORT|$HOST_KEY|$HOST_IS_LOCAL")
    done
    write_inventory "${HOST_SPECS[@]}"
    say ""
    say "   ${GREEN}Saved to $INVENTORY${RESET}"
  fi

  check_inventory
  HOSTS="$(host_count)"
  [[ "$HOSTS" -gt 0 ]] || die "no enabled hosts in $INVENTORY"
  MACHINE_COUNT="$(distinct_machine_count)"
  if (( MACHINE_COUNT < HOSTS )); then
    warn "$HOSTS host entries resolve to $MACHINE_COUNT machine(s); the nodes"
    warn "sharing a machine get separate ports, but no host-failure protection."
  fi
  say ""

  # --- 2. deployment method ------------------------------------------
  say "${BOLD}2) How should PostgreSQL and Spock be installed?${RESET}"
  say "   1) Native packages  — install from the pgEdge repository (fast, recommended)"
  say "   2) Manual build     — build PostgreSQL and Spock from source (slow, ~20-40 min/host)"
  say ""
  METHOD="$(ask_choice "   Choose 1 or 2" "1" 1 2)"
  if [[ "$METHOD" == "2" ]]; then
    MODE="source"
  else
    MODE="packages"
  fi
  say ""

  # --- 3. node count -------------------------------------------------
  say "${BOLD}3) How many Spock nodes should the cluster have?${RESET}"
  say "   Every node is a multi-master peer, cross-wired to all the others."
  if (( MACHINE_COUNT == 1 )); then
    say "   ${DIM}One machine is available, so every node shares it on separate"
    say "   ports (5432, 5433, ...) — fine for testing, not for HA.${RESET}"
  else
    say "   ${DIM}$MACHINE_COUNT machines available: the first $MACHINE_COUNT nodes each get"
    say "   their own; any beyond that share a machine on separate ports.${RESET}"
  fi
  say ""
  NODES="$(ask_int "   Number of nodes" "2" 1 32)"
  say ""

  # --- 4. PostgreSQL version -----------------------------------------
  say "${BOLD}4) Which PostgreSQL version?${RESET}"
  PG_MAJOR="$(ask_choice "   Major version (16, 17, 18 or 19)" "17" 16 17 18 19)"
  PG_VERSION=""
  if [[ "$MODE" == "source" ]]; then
    say "   ${DIM}A source build downloads an exact release tarball.${RESET}"
    while [[ -z "$PG_VERSION" ]]; do
      PG_VERSION="$(ask "   Exact version to build (e.g. ${PG_MAJOR}.11)" "")"
      if [[ ! "$PG_VERSION" =~ ^${PG_MAJOR}(\.[0-9]+|beta[0-9]+|rc[0-9]+) ]]; then
        warn "That does not look like a $PG_MAJOR release (expected ${PG_MAJOR}.x)."
        PG_VERSION=""
      fi
    done
  fi
  say ""

  # --- 5. standbys ---------------------------------------------------
  # Standbys are off by default: they double the instance count and are only
  # worth their cost when a node has to survive its own failure. The list is
  # asked for only after someone says they want one.
  say "${BOLD}5) Should any node get a Patroni standby?${RESET}"
  NODE_LIST=""
  for ((i = 1; i <= NODES; i++)); do
    NODE_LIST="${NODE_LIST}${NODE_LIST:+, }n$i"
  done
  say "   A standby is a physical replica Patroni can promote if its node fails."
  say "   ${DIM}Optional — without one, a failed node stays down until you fix it,"
  say "   and the other Spock nodes keep serving writes.${RESET}"
  say ""
  STANDBY=""
  WANT_STANDBY="$(ask_choice "   Add standbys? (y/n)" "n" y n)"
  if [[ "$WANT_STANDBY" == "y" ]]; then
    say ""
    say "   Nodes in this cluster: $NODE_LIST"
    say "   ${DIM}Comma-separated, or 'all' for every node.${RESET}"
    while [[ -z "$STANDBY" ]]; do
      STANDBY="$(ask "   Standby for which node(s)" "n1")"
      if [[ "$STANDBY" == "all" ]]; then
        STANDBY="$(printf '%s' "$NODE_LIST" | tr -d ' ')"
      fi
      for entry in ${STANDBY//,/ }; do
        if [[ ! "$entry" =~ ^n[0-9]+$ ]] || (( ${entry#n} < 1 || ${entry#n} > NODES )); then
          warn "Unknown node '$entry' — this cluster has $NODE_LIST."
          STANDBY=""
          break
        fi
      done
    done
  fi
  say ""

  # --- 6. remaining options ------------------------------------------
  say "${BOLD}6) Anything else${RESET} ${DIM}(press Enter to accept each default)${RESET}"
  CLUSTER="$(ask "   Cluster name" "pgedge")"
  SPOCK_MAJOR="$(ask_choice "   Spock major version (50 or 60)" "50" 50 60)"
  if [[ "$MODE" == "packages" ]]; then
    CHANNEL="$(ask_choice "   Repository channel (release, staging, daily)" "release" release staging daily)"
    SPOCK_BRANCH=""
  else
    CHANNEL=""
    SPOCK_BRANCH="$(ask "   Spock git branch to build" "main")"
  fi
  DB_NAME="$(ask "   Database name" "postgres")"
  DB_USER="$(ask "   Database superuser" "postgres")"

  # Instances sharing a machine are told apart by port, so it is worth being
  # able to move the first one clear of an existing PostgreSQL.
  BASE_PORT=""
  BASE_RESTAPI_PORT=""
  INSTANCES="$NODES"
  if [[ -n "$STANDBY" ]]; then
    IFS=',' read -r -a STANDBY_LIST <<<"$STANDBY"
    INSTANCES=$(( NODES + ${#STANDBY_LIST[@]} ))
  fi
  if (( INSTANCES > MACHINE_COUNT )); then
    say "   ${DIM}Several instances share a machine; each one takes the next"
    say "   port up from these (5432, 5433, ... and 8008, 8009, ...).${RESET}"
    BASE_PORT="$(ask_int "   First PostgreSQL port on each machine" "5432" 1024 65000)"
    BASE_RESTAPI_PORT="$(ask_int "   First Patroni REST port on each machine" "8008" 1024 65000)"
  fi
  CLEAN="$(ask_choice "   Wipe any previous deployment on these hosts first? (y/n)" "n" y n)"
  say ""

  # --- assemble ------------------------------------------------------
  # PLAN_ARGS holds only the flags the `plan` subcommand accepts; ARGS adds the
  # ones that are meaningful only to an actual deployment.
  PLAN_ARGS=(--mode "$MODE" --nodes "$NODES" --pg-major "$PG_MAJOR"
             --cluster "$CLUSTER" --spock-major "$SPOCK_MAJOR"
             --db-name "$DB_NAME" --db-user "$DB_USER"
             --inventory "$INVENTORY")
  [[ -n "$PG_VERSION" ]] && PLAN_ARGS+=(--pg-version "$PG_VERSION")
  [[ -n "$STANDBY" ]]    && PLAN_ARGS+=(--standby "$STANDBY")
  [[ -n "$CHANNEL" ]]    && PLAN_ARGS+=(--channel "$CHANNEL")
  [[ -n "$BASE_PORT" ]]  && PLAN_ARGS+=(--base-port "$BASE_PORT")
  [[ -n "$BASE_RESTAPI_PORT" ]] && PLAN_ARGS+=(--base-restapi-port "$BASE_RESTAPI_PORT")

  ARGS=("${PLAN_ARGS[@]}")
  [[ -n "$SPOCK_BRANCH" ]] && ARGS+=(--spock-branch "$SPOCK_BRANCH")
  [[ "$CLEAN" == "y" ]]    && ARGS+=(--clean)

  # --- confirm -------------------------------------------------------
  rule
  say "${BOLD}Planned deployment${RESET}"
  rule
  if ! python3 -m deployment.cli plan "${PLAN_ARGS[@]}"; then
    die "the requested topology is not valid — nothing was changed"
  fi
  rule
  say ""
  if [[ "$MODE" == "source" ]]; then
    say "${YELLOW}A source build compiles PostgreSQL on every host; expect 20-40 minutes each.${RESET}"
  fi
  PROCEED="$(ask_choice "Start the deployment? (yes/no)" "yes" yes no)"
  if [[ "$PROCEED" != "yes" ]]; then
    say "Cancelled — nothing was changed."
    exit 0
  fi
  say ""
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

exec python3 -m deployment.cli deploy "${ARGS[@]}"
