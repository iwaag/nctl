import subprocess

from nctl_core.ansible import AnsibleRunner, inventory_group_hosts, sanitize_command
from nctl_core.artifacts import OperationArtifacts


def test_runner_parses_failed_hosts_writes_artifacts_and_passes_timeout(tmp_path):
    seen = []

    def command(args, cwd, timeout):
        seen.append((args, cwd, timeout))
        return subprocess.CompletedProcess(
            args,
            2,
            "good : ok=2 changed=1 unreachable=0 failed=0\n"
            "bad : ok=1 changed=0 unreachable=1 failed=0\n"
            "failed : ok=1 changed=0 unreachable=0 failed=1\n",
            "boom",
        )

    artifacts = OperationArtifacts.create(tmp_path / "events", "01JTEST")
    runner = AnsibleRunner(
        tmp_path,
        timeout_seconds=17,
        artifacts=artifacts,
        command_runner=command,
    )
    result = runner.run(
        ["ansible-playbook", "site.yml", "-e", "api_token=secret-value"],
        mode="apply",
        artifact_stem="ansible/site",
    )

    assert seen[0][2] == 17
    assert result.command[-1] == "api_token=<redacted>"
    assert result.unreachable_hosts == ["bad"]
    assert result.failed_hosts == ["failed"]
    assert result.stdout_path.endswith("ansible/site.stdout")
    assert result.stderr_path.endswith("ansible/site.stderr")
    assert (artifacts.root / "ansible/site.stderr").read_text() == "boom"


def test_runner_turns_timeout_into_bounded_result(tmp_path):
    def command(args, cwd, timeout):
        raise subprocess.TimeoutExpired(args, timeout, output="partial", stderr="late")

    result = AnsibleRunner(tmp_path, timeout_seconds=0.5, command_runner=command).run(
        ["ansible-playbook", "site.yml"], mode="apply"
    )

    assert result.exit_code == 124
    assert result.timed_out is True
    assert result.stdout == "partial"
    assert result.stderr == "late"


def test_sanitize_command_redacts_sensitive_extra_vars_and_vault_path():
    assert sanitize_command(
        ["ansible-playbook", "x.yml", "-e", "password=hunter2", "--vault-password-file", "/secret/path"]
    ) == ["ansible-playbook", "x.yml", "-e", "password=<redacted>", "--vault-password-file", "<redacted>"]
    assert sanitize_command(["ansible-playbook", "x.yml", "-e", '{"api_token":"secret"}'])[-1] == "<redacted>"


def test_inventory_group_hosts_handles_cycles_without_recursing_forever():
    payload = {
        "parent": {"children": ["child"]},
        "child": {"hosts": ["host-a"], "children": ["parent"]},
    }
    assert inventory_group_hosts(payload, "parent") == {"host-a"}
