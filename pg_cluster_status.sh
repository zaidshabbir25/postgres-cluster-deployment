#!/usr/bin/env bash
#
# pg_cluster_status.sh — health of a deployed cluster, from the terminal.
#
#   ./pg_cluster_status.sh                          # most recent cluster
#   ./pg_cluster_status.sh --cluster demo
#   ./pg_cluster_status.sh --cluster demo --json     # machine-readable
#   ./pg_cluster_status.sh --cluster demo --report   # also write an HTML report
#   ./pg_cluster_status.sh --cluster demo --repair-failover n1
#   ./pg_cluster_status.sh --list                    # what has been deployed
#
# Exits non-zero when the cluster is not fully healthy, so it works as a check
# in a monitoring loop or CI job.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="${PG_CLUSTER_VENV:-$SCRIPT_DIR/venv}"

if [[ -z "${VIRTUAL_ENV:-}" && -f "$VENV_DIR/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$VENV_DIR/bin/activate"
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

if [[ "${1:-}" == "--list" ]]; then
  exec python3 -m deployment.cli list
fi

exec python3 -m deployment.cli status "$@"
