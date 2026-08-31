#!/usr/bin/env bash
#
# pg_cluster_ctl.sh — operate a deployed cluster.
#
# Deployment is pg_deploy_cluster.sh; this is everything you do afterwards.
# It is a thin wrapper over `python3 -m deployment.cli`, so every command here
# is equally scriptable.
#
#   ./pg_cluster_ctl.sh node list
#   ./pg_cluster_ctl.sh node add --host node-d
#   ./pg_cluster_ctl.sh node remove n3 --wipe-data
#   ./pg_cluster_ctl.sh node command 'df -h /var' --on all
#   ./pg_cluster_ctl.sh node command 'SELECT count(*) FROM orders' --sql --compare
#   ./pg_cluster_ctl.sh node ssh n2
#   ./pg_cluster_ctl.sh node psql n1
#
#   ./pg_cluster_ctl.sh spock sub-show-status
#   ./pg_cluster_ctl.sh spock replication-begin
#   ./pg_cluster_ctl.sh spock no-primary-key
#   ./pg_cluster_ctl.sh spock lag
#
#   ./pg_cluster_ctl.sh db guc-show --pattern 'spock%'
#   ./pg_cluster_ctl.sh db guc-set max_connections 200
#   ./pg_cluster_ctl.sh db set-readonly on --all-nodes
#   ./pg_cluster_ctl.sh db test-io
#
#   ./pg_cluster_ctl.sh service status
#   ./pg_cluster_ctl.sh service switchover --node n1
#   ./pg_cluster_ctl.sh service restart --node n1s1 --component postgres
#
#   ./pg_cluster_ctl.sh package list
#   ./pg_cluster_ctl.sh package upgrade
#
#   ./pg_cluster_ctl.sh app install --app pgbench --scale 10
#   ./pg_cluster_ctl.sh app counts
#
#   ./pg_cluster_ctl.sh diff all
#   ./pg_cluster_ctl.sh diff table public.orders
#   ./pg_cluster_ctl.sh diff repair public.orders --source n1
#
# Add --cluster NAME to target a specific cluster; the most recently deployed
# one is used by default. Add --json to any read-only command for machine
# output. Exit codes: 0 success, 1 problem found, 2 bad usage.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${PG_CLUSTER_VENV:-$SCRIPT_DIR/venv}"

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  echo
  echo "Run './pg_cluster_ctl.sh <group> --help' for a group's commands."
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

exec python3 -m deployment.cli "$@"
