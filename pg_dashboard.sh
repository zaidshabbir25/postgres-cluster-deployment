#!/usr/bin/env bash
#
# pg_dashboard.sh — start the live cluster dashboard.
#
#   ./pg_dashboard.sh                                  # http://127.0.0.1:8080
#   ./pg_dashboard.sh --port 9000 --cluster demo
#   ./pg_dashboard.sh --host 0.0.0.0 --interval 10
#
# The page shows every node's Patroni role, whether it accepts writes, its
# Spock subscription state and replication slots, plus etcd and host health.
# With --allow-changes it also serves /add-node, a form that adds a Spock node
# or a standby to the running cluster.
# A background poller refreshes on --interval seconds; the browser only ever
# reads the cached snapshot.
#
# Binding to 0.0.0.0 exposes cluster topology and health to anyone who can
# reach the port. There is no authentication — put it behind something, or
# leave it on localhost and use an SSH tunnel.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${PG_CLUSTER_VENV:-$SCRIPT_DIR/venv}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  echo
  echo "Options:"
  echo "  --host ADDR       bind address                      [127.0.0.1]"
  echo "  --port N          port                              [8080]"
  echo "  --interval N      seconds between health polls       [20]"
  echo "  --cluster NAME    cluster to show first"
  echo "  --allow-changes   enable the add-node page (loopback binds only)"
  echo "  --inventory PATH  inventory to offer new hosts from"
  echo "  --debug           Flask debug mode"
  exit 0
fi

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ -f "$VENV_DIR/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
  else
    echo "No virtualenv found. Run ./pg_deploy_cluster.sh once to create it, or:" >&2
    echo "  python3 -m venv venv && source venv/bin/activate" >&2
    echo "  pip install -r requirements.txt" >&2
    exit 1
  fi
fi

if ! python3 -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("flask") else 1)'; then
  echo "Flask is not installed. Run: pip install -r requirements.txt" >&2
  exit 1
fi

exec python3 dashboard/app.py "$@"
