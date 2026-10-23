#!/usr/bin/env python3
"""Turn on PostgreSQL's own logging across a cluster that is already running.

A cluster deployed before this existed reports every logging setting as
`source = default` and `logging_collector = off`: Patroni never set them, so
the server writes to stderr and systemd swallows it. New deployments get these
from the bootstrap block; this is for the ones already up.

Two things make it more than writing a config file:

  * Patroni renders postgresql.conf itself, so the settings have to go into the
    DCS — one `patronictl edit-config` per scope, not per node. Every member of
    a scope reads the same block, so a leader and its standbys are configured
    together.
  * `logging_collector` is read only at server startup. Everything else here
    takes effect on reload, but until each node restarts it will still not be
    writing files. That restart is the operator's call, and it is done one node
    at a time.
"""

import time

from aspects import patroni_management, state
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor


class LoggingError(RuntimeError):
    pass


def _executor_for(plan, host_name, cache, run_logger):
    if host_name not in cache:
        host = plan.host(host_name)
        executor = build_executor(
            {
                "name": host.name,
                "host": host.address,
                "username": host.username,
                "key_file": host.key_file,
                "port": host.port,
                "local": host.local,
            },
            run_logger=run_logger,
        )
        executor.connect()
        cache[host_name] = executor
    return cache[host_name]


def apply(cluster_name, db_password=None, restart=False, overrides=None,
          run_logger=None, inventory_path=None):
    """Configure logging on every scope. Returns a result dict."""

    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-logging"))
    executors = {}
    started = time.time()
    warnings = []
    configured, restarted, pending = [], [], []

    try:
        # Inside the try: a cluster with no saved state is an ordinary
        # refusal with a message, not a traceback out of a function that
        # promises to return a result dict.
        plan, metadata = state.load(cluster_name, db_password=db_password,
                                   inventory_path=inventory_path)
        plan.db_password = state.resolve_password(plan, explicit=db_password)

        parameters = patroni_management.logging_parameters(overrides)
        log.banner(f"Server logging for '{cluster_name}'")
        log.info(f"    scopes   : {len(plan.scopes())}")
        log.info(f"    files    : <data_dir>/{patroni_management.LOG_DIRECTORY}"
                 f"/{parameters['log_filename']}")
        log.info(f"    rotation : {parameters['log_rotation_age']}, "
                 f"truncating — one file per weekday")
        log.info("")

        # --- 1. one edit-config per scope -----------------------------
        log.step_start("Configure logging",
                       f"patronictl edit-config on {len(plan.scopes())} scope(s)")
        for scope, members in plan.scopes().items():
            leader = next((m for m in members if m.is_spock), members[0])
            executor = _executor_for(plan, leader.host, executors, log)
            ok, output = patroni_management.apply_logging(
                executor, plan, leader, overrides=overrides
            )
            if ok:
                configured.append(scope)
                log.info(f"    {scope}: {len(parameters)} setting(s) written "
                         f"to the DCS")
            else:
                warnings.append(f"{scope}: {output[:200]}")
                log.warn(warnings[-1])
        if not configured:
            log.step_end("failed", "no scope accepted the settings")
            raise LoggingError(
                "could not write the logging settings to any scope — "
                + ("; ".join(warnings) or "no detail")
            )
        log.step_end("passed", f"{len(configured)} scope(s) configured")

        # --- 2. who still needs a restart -----------------------------
        log.step_start("Check what is in force",
                       "logging_collector is read only at startup")
        for node in plan.nodes:
            executor = _executor_for(plan, node.host, executors, log)
            waiting = patroni_management.pending_restart(executor, plan, node)
            if waiting:
                pending.append(node.name)
                log.info(f"    {node.name}: pending restart for "
                         f"{', '.join(waiting)}")
            else:
                log.info(f"    {node.name}: in force")
        log.step_end("passed",
                     f"{len(pending)} node(s) need a restart"
                     if pending else "every node already has it in force")

        # --- 3. restart, one at a time, only when asked ---------------
        if pending and restart:
            log.step_start("Restart the nodes that need it",
                           "one at a time, so the cluster keeps serving")
            for name in pending:
                node = plan.node(name)
                executor = _executor_for(plan, node.host, executors, log)
                binary = patroni_management.patronictl_binary(executor,
                                                              node=node.name)
                ok, output = executor.try_run(
                    f"{binary} -c {node.config_file} restart {node.scope} "
                    f"{node.name} --force",
                    node=node.name, timeout=600,
                )
                if ok:
                    restarted.append(name)
                    log.info(f"    {name}: restarted")
                else:
                    warnings.append(f"{name}: restart failed — {output[:200]}")
                    log.warn(warnings[-1])
            log.step_end(
                "passed" if len(restarted) == len(pending) else "failed",
                f"{len(restarted)}/{len(pending)} restarted",
            )
        elif pending:
            log.info("")
            log.info("  Not restarted. Until each node restarts, it is "
                     "configured to collect logs but still will not:")
            for name in pending:
                node = plan.node(name)
                log.info(f"    patronictl -c {node.config_file} restart "
                         f"{node.scope} {name} --force")
            log.info("  or re-run this with --restart to do it one at a time.")

        plan.log_parameters = dict(overrides or {})
        state.save(plan, extra={**metadata,
                                "last_change": "configured server logging",
                                "run_id": log.run_id})

        duration = round(time.time() - started, 1)
        log.banner(f"Server logging configured in {duration}s")
        for node in plan.nodes:
            log.info(f"  {node.name}: "
                     f"{patroni_management.log_directory_for(node)}/"
                     f"{parameters['log_filename']}")

        return {
            "outcome": "succeeded",
            "cluster": cluster_name,
            "scopes": configured,
            "pending_restart": [n for n in pending if n not in restarted],
            "restarted": restarted,
            "parameters": parameters,
            "duration": duration,
            "steps": log.steps,
            "warnings": warnings,
            "log_dir": str(log.root),
        }

    except Exception as exc:
        log.error(f"Configuring logging failed — {exc}")
        return {
            "outcome": "failed",
            "cluster": cluster_name,
            "failure": str(exc),
            "steps": log.steps,
            "warnings": warnings,
            "log_dir": str(log.root),
        }
    finally:
        for executor in executors.values():
            try:
                executor.close()
            except Exception:
                pass
