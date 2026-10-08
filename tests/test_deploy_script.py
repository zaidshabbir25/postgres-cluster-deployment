#!/usr/bin/env python3
"""pg_deploy_cluster.sh: the prompts and flags, without deploying anything.

The script is driven through a stubbed copy: the venv bootstrap is skipped and
the `exec python3 -m deployment.cli ...` hand-offs become `echo` plus an exit,
so a test can assert on the command line the answers would have produced. That
is the script's whole job — everything past the exec is covered by the Python
tests.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "pg_deploy_cluster.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash is required to drive the script")


@pytest.fixture
def script(tmp_path):
    """A stubbed copy of the deploy script."""
    text = SCRIPT.read_text(encoding="utf-8")
    text = text.replace("ensure_venv\n[[", "true\n[[", 1)
    text = text.replace('if ! python3 -m deployment.cli plan "${PLAN_ARGS[@]}"; then',
                        'if ! echo "PLAN: ${PLAN_ARGS[@]}"; then', 1)
    text = text.replace('exec python3 -m deployment.cli deploy "${ARGS[@]}"',
                        'echo "DEPLOY: ${ARGS[@]}"', 1)
    text = text.replace('exec python3 -m deployment.cli node add',
                        'exit_after echo "ADD: node add"', 1)
    text = text.replace('exec python3 -m deployment.cli node remove',
                        'exit_after echo "REMOVE: node remove"', 1)
    text = text.replace('exec python3 -m deployment.cli cleanup',
                        'exit_after echo "CLEANUP: cleanup"', 1)
    # `exec` would have replaced the process; the stub has to stop by itself.
    text = text.replace('say()  { printf', 'exit_after() { "$@"; exit 0; }\nsay()  { printf', 1)
    # The helpers import from the repository, so keep running there.
    text = text.replace('cd "$SCRIPT_DIR"', f'cd "{REPO_ROOT}"', 1)
    path = tmp_path / "deploy.sh"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def inventory(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({
        "defaults": {"data_root": "/var/lib/pgedge"},
        "hosts": [{"name": "local", "host": "localhost", "username": "root",
                   "local": True, "enabled": True}],
    }), encoding="utf-8")
    return path


def run(script, inventory, *args, answers="", facts=None):
    """Run the stubbed script with piped answers. Returns (code, output)."""
    if facts is not None:
        # Both the add-node and remove-node blocks read the facts; stub every
        # occurrence, not the first, or the one under test keeps calling out
        # to a cluster that does not exist.
        text = script.read_text(encoding="utf-8").replace(
            '  FACTS="$(cluster_facts "$CLUSTER_NAME")"', f'  FACTS="{facts}"')
        script.write_text(text, encoding="utf-8")

    env = dict(os.environ, PG_CLUSTER_INVENTORY=str(inventory))
    env.pop("VIRTUAL_ENV", None)
    proc = subprocess.run(["bash", str(script), *args], input=answers, text=True,
                          capture_output=True, env=env, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def line(output, prefix):
    for text in output.splitlines():
        if text.startswith(prefix):
            return text
    raise AssertionError(f"no {prefix!r} line in:\n{output}")


def deployment_answers(hosts="1", method="1", nodes="2", pg_major="17",
                       pg_version=None, standby="n", standby_list=None,
                       sync="n", sync_count=None, sync_strict=None,
                       lolor="n", lolor_mode=None, lolor_ref=None,
                       snowflake="n", snowflake_mode=None, snowflake_ref=None,
                       ace="n", ace_mode=None, ace_ref=None,
                       cluster="", spock_major="", channel_or_branch="",
                       db_name="", db_user="", base_port="", base_restapi="",
                       clean="n", proceed="yes"):
    """The deployment prompts, in the order the script asks them.

    Spelled out because the stream is positional: one answer too few and every
    later question is silently given the wrong reply.
    """
    answers = [hosts, method, nodes, pg_major]
    if pg_version is not None:                  # a source build wants an exact one
        answers.append(pg_version)
    answers.append(standby)
    if standby_list is not None:
        answers.append(standby_list)
        answers.append(sync)                    # asked only when there is one
        if sync == "y":
            if sync_count is not None:          # asked only for several
                answers.append(sync_count)
            answers.append(sync_strict or "n")
    # The general options come first: the channel an extension package is
    # fetched from is one of them.
    answers += [cluster, spock_major, channel_or_branch, db_name, db_user,
                base_port, base_restapi, clean]
    # Then each add-on: one yes/no, and saying yes opens one or two more.
    for wanted, mode, ref in ((lolor, lolor_mode, lolor_ref),
                              (snowflake, snowflake_mode, snowflake_ref),
                              (ace, ace_mode, ace_ref)):
        answers.append(wanted)
        if wanted == "y":
            answers.append(mode or "")
            if mode == "source":
                answers.append(ref or "")
    answers.append(proceed)
    return "\n".join(answers) + "\n"


# ---------------------------------------------------------------------------
# usage and argument handling
# ---------------------------------------------------------------------------


def test_help_documents_every_mode(script, inventory):
    code, output = run(script, inventory, "--help")

    assert code == 0
    for flag in ("--nodes", "--add-node", "--role", "--pg-version",
                 "--spock-major", "--spock-branch", "--cleanup", "--purge"):
        assert flag in output


def test_an_unknown_option_is_refused(script, inventory):
    code, output = run(script, inventory, "--nonsense")

    assert code != 0
    assert "unknown option" in output


def test_a_flag_without_its_value_is_refused(script, inventory):
    code, output = run(script, inventory, "--nodes")

    assert code != 0
    assert "needs a value" in output


# ---------------------------------------------------------------------------
# the deployment prompts
# ---------------------------------------------------------------------------


def test_the_host_question_can_write_a_localhost_inventory(script, tmp_path):
    inventory = tmp_path / "fresh.json"

    code, _ = run(script, inventory, answers=deployment_answers(hosts="3"))

    assert code == 0
    written = json.loads(inventory.read_text(encoding="utf-8"))
    assert written["hosts"][0]["host"] == "localhost"
    assert written["hosts"][0]["local"] is True


def test_entered_hosts_are_written_to_the_inventory(script, tmp_path):
    inventory = tmp_path / "fresh.json"
    # option 2, one machine: address, ssh user, ssh port, key file, name
    entered = "2\n1\nhost-a.example.com\nrocky\n22\n\nnode-a\n"
    rest = deployment_answers().split("\n", 1)[1]        # minus the host choice

    code, _ = run(script, inventory, answers=entered + rest)

    assert code == 0
    written = json.loads(inventory.read_text(encoding="utf-8"))
    assert written["hosts"] == [{
        "name": "node-a", "host": "host-a.example.com", "username": "rocky",
        "enabled": True, "key_file": "", "port": 22,
    }]


def test_defaults_produce_a_two_node_package_deployment(script, inventory):
    code, output = run(script, inventory, answers=deployment_answers())

    assert code == 0
    deploy = line(output, "DEPLOY:")
    assert "--mode packages" in deploy
    assert "--nodes 2" in deploy
    assert "--standby" not in deploy          # standbys are opt-in


def test_standbys_are_asked_for_only_when_wanted(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(standby="y", standby_list="all"))

    assert code == 0
    assert "--standby n1,n2" in line(output, "DEPLOY:")


def test_an_unknown_standby_target_is_re_asked(script, inventory):
    answers = deployment_answers(standby="y", standby_list="n9")
    answers = answers.replace("n9\n", "n9\nn2\n", 1)      # refused, then accepted

    code, output = run(script, inventory, answers=answers)

    assert "Unknown node 'n9'" in output
    assert "--standby n2" in line(output, "DEPLOY:")


def test_a_source_build_defaults_the_branch_from_the_spock_major(script, inventory):
    source = dict(method="2", pg_version="17.11")

    spock50 = run(script, inventory, answers=deployment_answers(**source))[1]
    spock60 = run(script, inventory,
                  answers=deployment_answers(spock_major="60", **source))[1]

    assert "--spock-branch v5_STABLE" in line(spock50, "DEPLOY:")
    assert "--spock-branch main" in line(spock60, "DEPLOY:")


def test_shared_machines_are_asked_which_ports_to_start_from(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(base_port="6432",
                                                  base_restapi="8108"))

    deploy = line(output, "DEPLOY:")
    assert "--base-port 6432" in deploy
    assert "--base-restapi-port 8108" in deploy


def test_a_standby_is_asked_how_it_replicates(script, inventory):
    """Asynchronous by default; the question only appears with a standby."""
    plain = run(script, inventory, answers=deployment_answers())[1]
    async_ = run(script, inventory,
                 answers=deployment_answers(standby="y", standby_list="n1"))[1]
    sync = run(script, inventory,
               answers=deployment_answers(standby="y", standby_list="n1",
                                          sync="y", sync_strict="y"))[1]

    # bash prints a `read -p` prompt only to a terminal, so the question is
    # recognised here by the explanation `say` puts above it.
    assert "A standby can be asynchronous or synchronous" not in plain
    assert "A standby can be asynchronous or synchronous" in async_
    assert "--sync-mode async" in line(async_, "DEPLOY:")
    assert "--sync-mode sync" in line(sync, "DEPLOY:")
    assert "--sync-strict" in line(sync, "DEPLOY:")
    assert "Strict mode refuses writes" in sync


def test_several_standbys_are_asked_how_many_must_confirm(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(standby="y", standby_list="all",
                                                  sync="y", sync_count="2"))

    assert "--sync-count 2" in line(output, "DEPLOY:")


def test_add_standby_is_asked_how_its_leader_replicates(script, inventory):
    code, output = run(script, inventory, "--add-node", "n1s1",
                       answers="2\nn1\ny\nn\n", facts=SOURCE_50)

    add = line(output, "ADD:")
    assert "--role standby" in add
    assert "--sync-mode sync" in add
    assert "--sync-count 1" in add


def test_add_standby_takes_the_mode_from_flags(script, inventory):
    code, output = run(script, inventory, "--add-node", "n1s1", "--role", "standby",
                       "--leader", "n1", "--sync-mode", "sync", "--sync-strict",
                       facts=SOURCE_50)

    add = line(output, "ADD:")
    assert "--sync-mode sync" in add
    assert "--sync-strict" in add


def test_sync_flags_make_no_sense_for_a_spock_node(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3", "--role", "leader",
                       "--sync-mode", "sync", facts=SOURCE_50)

    assert code != 0
    assert "apply with --role standby" in output


def test_answering_no_cancels_without_deploying(script, inventory):
    code, output = run(script, inventory, answers=deployment_answers(proceed="no"))

    assert code == 0
    assert "Cancelled" in output
    assert "DEPLOY:" not in output


# ---------------------------------------------------------------------------
# add-node
# ---------------------------------------------------------------------------


SOURCE_50 = "pgedge|17.11|n1,n2|n1,n2|source|50|v5_STABLE"
PACKAGES_50 = "pgedge|17.11|n1,n2|n1,n2|packages|50|main"
SOURCE_60 = "pgedge|17.11|n1,n2|n1,n2|source|60|main"


def test_add_node_defaults_to_a_spock_node_at_the_clusters_versions(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3",
                       answers="1\n\n\n\n", facts=SOURCE_50)

    add = line(output, "ADD:")
    assert "--name n3" in add
    assert "--role leader" in add
    assert "--pg-version 17.11" in add
    assert "--spock-major 50" in add
    assert "--spock-branch v5_STABLE" in add


def test_add_node_can_take_a_newer_postgres_and_spock(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3",
                       answers="1\n18.6\n60\n\n", facts=SOURCE_50)

    add = line(output, "ADD:")
    assert "--pg-version 18.6" in add
    assert "--spock-major 60" in add
    assert "--spock-branch main" in add       # the branch follows the major
    assert "Mixed-version add" in output


def test_add_node_refuses_an_older_postgres_at_the_prompt(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3",
                       answers="1\n17.4\n17.11\n\n\n", facts=SOURCE_50)

    assert "older than the cluster's 17.11" in output
    assert "--pg-version 17.11" in line(output, "ADD:")


def test_add_node_refuses_an_older_spock(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3",
                       answers="1\n\n50\n\n", facts=SOURCE_60)

    assert code != 0
    assert "may not run an older Spock" in output


def test_add_node_asks_which_leader_a_standby_follows(script, inventory):
    code, output = run(script, inventory, "--add-node", "n1s1",
                       answers="2\nn1\n", facts=SOURCE_50)

    add = line(output, "ADD:")
    assert "--role standby" in add
    assert "--leader n1" in add
    assert "--pg-version" not in add          # a replica copies its leader


def test_add_node_re_asks_for_an_unknown_leader(script, inventory):
    code, output = run(script, inventory, "--add-node", "s1",
                       answers="2\nn9\nn2\n", facts=SOURCE_50)

    assert "is not a Spock node" in output
    assert "--leader n2" in line(output, "ADD:")


def test_add_node_forwards_flags_without_prompting(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3", "--role", "leader",
                       "--pg-version", "18.6", "--spock-major", "60",
                       "--spock-branch", "dev", "--host", "node-d",
                       facts=SOURCE_50)

    add = line(output, "ADD:")
    for fragment in ("--pg-version 18.6", "--spock-major 60",
                     "--spock-branch dev", "--host node-d"):
        assert fragment in add


def test_a_branch_makes_no_sense_for_a_package_cluster(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3", "--role", "leader",
                       "--spock-branch", "main", "--pg-version", "17.11",
                       "--spock-major", "50", facts=PACKAGES_50)

    assert code != 0
    assert "source-built cluster" in output


def test_version_flags_make_no_sense_for_a_standby(script, inventory):
    code, output = run(script, inventory, "--add-node", "s1", "--role", "standby",
                       "--leader", "n1", "--spock-major", "60", facts=SOURCE_50)

    assert code != 0
    assert "do not apply to a standby" in output


def test_an_invalid_node_name_is_refused(script, inventory):
    code, output = run(script, inventory, "--add-node", "bad name")

    assert code != 0
    assert "must start with a letter" in output


def test_add_node_without_a_name_is_refused(script, inventory):
    code, output = run(script, inventory, "--add-node")

    assert code != 0
    assert "needs a node name" in output


def test_add_node_and_cleanup_are_mutually_exclusive(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3", "--cleanup")

    assert code != 0
    assert "pick one" in output


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


def test_cleanup_hands_off_with_the_inventory(script, inventory):
    code, output = run(script, inventory, "--cleanup")

    assert code == 0
    assert "CLEANUP:" in output
    assert "Host cleanup" in output


def test_cleanup_passes_purge_and_yes_through(script, inventory):
    code, output = run(script, inventory, "--cleanup", "--purge", "--yes")

    assert code == 0
    assert "CLEANUP:" in output


# ---------------------------------------------------------------------------
# --remove-node
# ---------------------------------------------------------------------------

WITH_STANDBY = "pgedge|17.11|n1,n2|n1,n2,n1s1|source|50|v5_STABLE"


def test_remove_node_confirms_by_name_before_touching_anything(script, inventory):
    code, output = run(script, inventory, "--remove-node", "n2",
                       answers="no\n", facts=SOURCE_50)

    assert code != 0
    assert "nothing was changed" in output


def test_remove_node_forwards_the_name_once_confirmed(script, inventory):
    code, output = run(script, inventory, "--remove-node", "n2",
                       answers="n2\n", facts=SOURCE_50)

    assert code == 0
    assert "REMOVE: node remove" in output


def test_remove_node_says_which_kind_of_removal_this_is(script, inventory):
    spock = run(script, inventory, "--remove-node", "n2", answers="n2\n",
                facts=WITH_STANDBY)[1]
    standby = run(script, inventory, "--remove-node", "n1s1", answers="n1s1\n",
                  facts=WITH_STANDBY)[1]

    assert "spock.remove_node from branch" in spock
    assert "v5_STABLE" in spock
    assert "synchronous" in standby
    assert "No Spock state changes" in standby


def test_removing_the_last_spock_node_is_refused_before_the_prompt(script, inventory):
    code, output = run(script, inventory, "--remove-node", "n1",
                       facts="pgedge|17.11|n1|n1|source|50|v5_STABLE")

    assert code != 0
    assert "only Spock node" in output
    assert "--cleanup" in output


def test_removing_a_node_that_is_not_in_the_cluster_is_refused(script, inventory):
    code, output = run(script, inventory, "--remove-node", "n9",
                       facts=SOURCE_50)

    assert code != 0
    assert "is not a node in cluster" in output


def test_remove_node_and_add_node_are_mutually_exclusive(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3",
                       "--remove-node", "n2", facts=SOURCE_50)

    assert code != 0
    assert "opposite of each other" in output


def test_yes_skips_the_prompt_and_the_flags_are_forwarded(script, inventory):
    code, output = run(script, inventory, "--remove-node", "n2", "--yes",
                       "--wipe-data", "--force", facts=SOURCE_50)

    assert code == 0
    assert "REMOVE: node remove" in output


# ---------------------------------------------------------------------------
# --pg-extensions
# ---------------------------------------------------------------------------


def test_no_extensions_is_the_default_the_prompts_produce(script, inventory):
    code, output = run(script, inventory, answers=deployment_answers())

    assert code == 0
    assert "--pg-extensions none" in line(output, "DEPLOY:")


def test_the_prompts_offer_each_extension_and_how_to_get_it(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(lolor="y", lolor_mode="packages",
                                                  snowflake="n"))

    assert code == 0
    assert "--pg-extensions pgedge-lolor:packages" in line(output, "DEPLOY:")


def test_choosing_a_source_build_asks_which_ref_to_build(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(lolor="y", lolor_mode="source",
                                                  lolor_ref="v1.2.0",
                                                  snowflake="n"))

    assert code == 0
    assert "--pg-extensions pgedge-lolor:source@v1.2.0" in line(output, "DEPLOY:")


def test_both_extensions_can_be_chosen_with_different_origins(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(lolor="y", lolor_mode="source",
                                                  lolor_ref="main",
                                                  snowflake="y",
                                                  snowflake_mode="packages"))

    assert code == 0
    assert ("--pg-extensions pgedge-lolor:source@main,pgedge-snowflake:packages"
            in line(output, "DEPLOY:"))


def test_the_flag_skips_the_prompts_entirely(script, inventory):
    code, output = run(script, inventory, "--pg-extensions",
                       "pgedge-lolor,pgedge-snowflake")

    assert code == 0
    deploy = line(output, "DEPLOY:")
    assert "--pg-extensions pgedge-lolor,pgedge-snowflake" in deploy


def test_the_flag_needs_a_value(script, inventory):
    code, output = run(script, inventory, "--pg-extensions")

    assert code != 0
    assert "needs a value" in output


def test_ace_is_offered_alongside_the_extensions(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(ace="y", ace_mode="packages"))

    assert code == 0
    assert "--pg-extensions pgedge-ace:packages" in line(output, "DEPLOY:")


def test_building_ace_asks_for_a_ref_and_warns_it_needs_go(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(ace="y", ace_mode="source",
                                                  ace_ref="v1.0.0"))

    assert code == 0
    assert "written in Go" in output
    assert "--pg-extensions pgedge-ace:source@v1.0.0" in line(output, "DEPLOY:")


def test_all_three_can_be_chosen_at_once(script, inventory):
    code, output = run(script, inventory,
                       answers=deployment_answers(
                           lolor="y", lolor_mode="packages",
                           snowflake="y", snowflake_mode="packages",
                           ace="y", ace_mode="packages"))

    assert code == 0
    assert ("--pg-extensions pgedge-lolor:packages,pgedge-snowflake:packages,"
            "pgedge-ace:packages" in line(output, "DEPLOY:"))


def test_the_general_options_are_asked_before_the_add_ons(script, inventory):
    """The channel an extension package comes from is one of them, so asking
    it after the add-on questions was the wrong way round."""
    code, output = run(script, inventory,
                       answers=deployment_answers(channel_or_branch="staging",
                                                  lolor="y",
                                                  lolor_mode="packages"))

    assert code == 0
    assert output.index("6) Anything else") < output.index("7) Any optional")
    assert "from the staging channel" in output
    deploy = line(output, "DEPLOY:")
    assert "--channel staging" in deploy
    assert "--pg-extensions pgedge-lolor:packages" in deploy


def test_the_shell_does_not_make_the_deploy_command_ask_twice(script, inventory):
    """It has just asked "Start the deployment?" itself."""
    code, output = run(script, inventory, answers=deployment_answers())

    assert code == 0
    assert "--yes" in line(output, "DEPLOY:")


def test_a_flag_driven_run_leaves_the_question_to_the_deploy_command(script, inventory):
    """Nothing asked here, so the summary the deploy command prints is the
    first and only chance to stop."""
    code, output = run(script, inventory, "--nodes", "2")

    assert code == 0
    assert "--yes" not in line(output, "DEPLOY:")


# ---------------------------------------------------------------------------
# version prompts
# ---------------------------------------------------------------------------

PRERELEASE = "pgedge|19.0beta3|n1,n2|n1,n2|packages|50|main"


def test_add_node_accepts_the_clusters_own_prerelease_version(script, inventory):
    """A cluster on 19.0beta3 offers it as the default; a pattern that treats
    the minor and the pre-release as alternatives rejects its own default and
    the prompt becomes unanswerable."""
    code, output = run(script, inventory, "--add-node", "n3", "--role", "leader",
                       answers="\n\n", facts=PRERELEASE)

    assert code == 0
    assert "Enter a version like" not in output
    assert "--pg-version 19.0beta3" in line(output, "ADD:")


def test_add_node_accepts_every_shape_a_postgres_version_takes(script, inventory):
    for version in ("19", "19.0", "19beta3", "19.0beta3", "19.0rc1"):
        code, output = run(script, inventory, "--add-node", "n3",
                           "--role", "leader", answers=f"{version}\n\n",
                           facts=PRERELEASE)

        assert code == 0, version
        assert "Enter a version like" not in output, version


def test_add_node_still_refuses_something_that_is_not_a_version(script, inventory):
    code, output = run(script, inventory, "--add-node", "n3", "--role", "leader",
                       answers="19.0xyz\n19.0beta3\n\n", facts=PRERELEASE)

    assert "Enter a version like" in output
    assert "--pg-version 19.0beta3" in line(output, "ADD:")


def _version_pattern(label):
    """Pull a version regex out of the script, so the test checks the real one.

    The source-build prompt also asks the PostgreSQL mirror which versions
    exist, so driving it end to end makes the test depend on what is published
    today. The shape check is a separate question and this asks only that.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    patterns = re.findall(r'=~ (\^[^\s]*?)\s*\]\]', text)
    matches = [p for p in patterns if label in p]
    assert matches, f"no version pattern containing {label!r} in the script"
    return matches[0]


def _matches(pattern, value, variables=""):
    """Does bash consider `value` to match `pattern`?"""
    script = f'{variables}\n[[ "{value}" =~ {pattern} ]]'
    return subprocess.run(["bash", "-c", script]).returncode == 0


@pytest.mark.parametrize("version,allowed", [
    ("19", True), ("19.0", True), ("19beta3", True),
    ("19.0beta3", True), ("19.0rc1", True),
    ("19.0xyz", False), ("", False), ("beta3", False),
])
def test_the_add_node_version_pattern_accepts_every_real_shape(version, allowed):
    """The minor and the pre-release are independent. Treating them as
    alternatives rejects 19.0beta3 — which is what a cluster on a pre-release
    reports, and therefore what the prompt offers as its own default."""
    pattern = _version_pattern("beta[0-9]+|rc[0-9]+)?$")

    assert _matches(pattern, version) is allowed


@pytest.mark.parametrize("version,allowed", [
    ("19.0", True), ("19beta3", True), ("19.0beta3", True), ("19.0rc1", True),
    ("19", False),          # a source build needs an exact version to fetch
    ("18.5", False),        # a different major
    ("19.0xyz", False),     # anchored, so no trailing junk
])
def test_the_source_build_version_pattern_demands_an_exact_version(version, allowed):
    pattern = _version_pattern("${PG_MAJOR}")

    assert _matches(pattern, version, variables="PG_MAJOR=19") is allowed
