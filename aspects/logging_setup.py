#!/usr/bin/env python3
"""Run-scoped logging for cluster deployments.

Every deployment gets a run id and a directory under logs/:

    logs/<run_id>/deploy.log      full transcript, every command and result
    logs/<run_id>/<node>.log      per-node transcript
    logs/<run_id>/steps.json      machine-readable step timeline

The step timeline is what reports/ and dashboard/ read back, so it is written
incrementally — a deployment that dies half way still leaves a usable record.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_ROOT = REPO_ROOT / "logs"

# Trim command output in the human-readable log; the full text always goes to
# the per-node log so nothing is actually lost.
CONSOLE_OUTPUT_LIMIT = 2000


def new_run_id(cluster_name="cluster"):
    """Build a sortable, filesystem-safe run id."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{cluster_name}"


class RunLogger:
    """Owns the log directory and step timeline for a single deployment run."""

    def __init__(self, run_id, log_root=None, console_level=logging.INFO):
        self.run_id = run_id
        self.root = Path(log_root or LOG_ROOT) / run_id
        self.root.mkdir(parents=True, exist_ok=True)

        self.steps = []
        self._step_stack = []
        self._node_handlers = {}
        self._steps_path = self.root / "steps.json"

        self.log = logging.getLogger(f"pgcluster.{run_id}")
        self.log.setLevel(logging.DEBUG)
        self.log.handlers.clear()
        self.log.propagate = False

        file_handler = logging.FileHandler(self.root / "deploy.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        self.log.addHandler(file_handler)

        console = logging.StreamHandler(sys.stdout)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(message)s"))
        self.log.addHandler(console)

        self.log.info(f"Run {run_id} — logs in {self.root}")

    # ------------------------------------------------------------------
    # Plain logging
    # ------------------------------------------------------------------

    def info(self, message):
        self.log.info(message)

    def warn(self, message):
        self.log.warning(f"WARN  {message}")

    def error(self, message):
        self.log.error(f"ERROR {message}")

    def debug(self, message):
        self.log.debug(message)

    def banner(self, title):
        self.log.info("")
        self.log.info("=" * 78)
        self.log.info(f"  {title}")
        self.log.info("=" * 78)

    # ------------------------------------------------------------------
    # Per-node transcripts
    # ------------------------------------------------------------------

    def node_log_path(self, node_name):
        return self.root / f"{node_name}.log"

    def node(self, node_name, message):
        """Append a line to a node's transcript (and the main log at DEBUG)."""
        path = self.node_log_path(node_name)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
        self.log.debug(f"[{node_name}] {message}")

    def command(self, node_name, command, exit_code, output, duration):
        """Record one remote command execution."""
        self.node(
            node_name,
            f"$ {command}\n  exit={exit_code} in {duration:.2f}s\n"
            + "\n".join(f"  | {line}" for line in output.splitlines()),
        )

    # ------------------------------------------------------------------
    # Step timeline
    # ------------------------------------------------------------------

    def step_start(self, name, detail=""):
        entry = {
            "name": name,
            "detail": detail,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "duration": None,
            "message": "",
        }
        self.steps.append(entry)
        self._step_stack.append((entry, time.time()))
        self.banner(f"{len(self.steps):02d}. {name}")
        if detail:
            self.log.info(f"    {detail}")
        self.flush_steps()
        return entry

    def step_end(self, status="passed", message=""):
        if not self._step_stack:
            return None
        entry, started = self._step_stack.pop()
        entry["status"] = status
        entry["duration"] = round(time.time() - started, 2)
        entry["message"] = message
        symbol = {"passed": "OK", "failed": "FAILED", "skipped": "SKIPPED"}.get(status, status)
        line = f"    -> {symbol} ({entry['duration']}s)"
        if message:
            line += f" — {message}"
        if status == "failed":
            self.log.error(line)
        else:
            self.log.info(line)
        self.flush_steps()
        return entry

    def flush_steps(self):
        payload = {
            "run_id": self.run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "steps": self.steps,
        }
        tmp = self._steps_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._steps_path)

    # ------------------------------------------------------------------
    # Summaries
    # ------------------------------------------------------------------

    def counts(self):
        counts = {"passed": 0, "failed": 0, "skipped": 0, "running": 0}
        for step in self.steps:
            counts[step["status"]] = counts.get(step["status"], 0) + 1
        return counts

    def failed_steps(self):
        return [s for s in self.steps if s["status"] == "failed"]
