#!/usr/bin/env python3
"""The prompt that stands between the summary and touching anything.

The summary is only useful if there is still a decision to make when it has
been read. Running it with a flag-driven command used to print the plan and
start installing in the same breath.
"""

import argparse

import pytest

from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK
from deployment import cli, deploy_cluster
from tests.conftest import RecordingLogger


def plan_with():
    host = Host(name="h0", address="10.0.1.10")
    host.family = "rhel"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", hosts=[host])
    plan.nodes = [Node(name="n1", role=ROLE_SPOCK, host="h0",
                       address="10.0.1.10", pg_port=5432, restapi_port=8008,
                       data_dir="/d", scope="s", config_file="c",
                       pgpass_file="p")]
    return plan


def deployer(answer, log=None):
    return deploy_cluster.ClusterDeployer(
        plan_with(), run_logger=log or RecordingLogger(),
        confirm=(lambda plan: answer),
    )


def test_declining_stops_before_a_single_host_is_touched():
    log = RecordingLogger()

    result = deployer(False, log).run()

    assert result["outcome"] == "cancelled"
    assert result["steps"] == []
    assert log.said("Cancelled")


def test_the_summary_is_printed_before_the_question_is_asked():
    """Answering it from memory of a flag is not the point."""
    log = RecordingLogger()
    seen = {}

    def confirm(plan):
        seen["lines"] = list(log.infos)
        return False

    deploy_cluster.ClusterDeployer(plan_with(), run_logger=log,
                                   confirm=confirm).run()

    assert any("COMPONENT" in line for line in seen["lines"])
    assert any("Cluster        : pgedge" in line for line in seen["lines"])


def test_no_confirm_hook_means_no_prompt():
    """A scripted run has nobody to ask, and a prompt there would hang it."""
    log = RecordingLogger()
    deployment = deploy_cluster.ClusterDeployer(plan_with(), run_logger=log)

    assert deployment.confirm is None


# ---------------------------------------------------------------------------
# when the CLI decides to ask
# ---------------------------------------------------------------------------


def args_with(**kwargs):
    return argparse.Namespace(**{"yes": False, **kwargs})


def test_yes_skips_the_question(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    assert cli._confirm_for(args_with(yes=True)) is None


def test_a_piped_stdin_skips_the_question(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli._confirm_for(args_with()) is None


def test_a_terminal_gets_asked(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    confirm = cli._confirm_for(args_with())

    assert callable(confirm)


@pytest.mark.parametrize("answer,expected", [
    ("yes", True), ("y", True), ("YES", True),
    ("no", False), ("n", False), ("", False), ("maybe", False),
])
def test_only_a_yes_starts_the_deployment(monkeypatch, answer, expected):
    """Anything else is a no: an ambiguous answer must not install a cluster."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli._confirm_for(args_with())(plan_with()) is expected
