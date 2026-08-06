from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nctl_core.reconcile.profiles import (
    ProfileReconciliationError,
    load_profile_reconciliation,
    resolve_dnsmasq_records_spec,
)

_REPO_PROFILE_NAMES = {
    "dnsmasq",
    "grafana",
    "manual_toolchain",
    "nomad_client",
    "nomad_server",
    "node_agent",
    "ollama",
    "prometheus",
    "prometheus_node_exporter",
    "swarmui",
    "comfyui",
}


def _write(tmp_path: Path, body: dict) -> Path:
    playbook_dir = tmp_path / "ansible_agdev"
    (playbook_dir / "vars").mkdir(parents=True)
    (playbook_dir / "vars" / "deployment_profiles.yml").write_text(yaml.safe_dump(body))
    (playbook_dir / "playbooks" / "monitoring").mkdir(parents=True)
    (playbook_dir / "playbooks" / "monitoring" / "setup_grafana.yml").write_text("- hosts: all\n")
    return playbook_dir


def test_real_repo_file_validates(tmp_path):
    # The actual checked-in file this Step 5 boundary edits -- a real
    # regression gate, not just a synthetic fixture.
    repo_playbook_dir = Path(__file__).resolve().parents[2] / "ansible_agdev"
    entries = load_profile_reconciliation(repo_playbook_dir, _REPO_PROFILE_NAMES)

    assert entries["dnsmasq"].action.kind == "dnsmasq_config"
    assert entries["dnsmasq"].action.managed_files["records"].path == "/etc/dnsmasq.d/nintent-records.conf"
    assert entries["dnsmasq"].action.managed_files["records"].digest == "sha256"
    assert entries["manual_toolchain"].observe_only is True
    assert entries["swarmui"].observe_only is True
    assert [check.kind for check in entries["swarmui"].checks] == ["http", "file_exists"]
    assert entries["swarmui"].checks[1].path == "~/StabilityMatrix/Packages/SwarmUI"
    assert entries["comfyui"].observe_only is True
    assert [check.kind for check in entries["comfyui"].checks] == ["http", "file_exists"]
    assert entries["comfyui"].checks[1].path == "~/StabilityMatrix/Packages/ComfyUI"
    assert entries["ollama"].checks[0].paths == ["/v1/models", "/api/tags"]
    assert entries["nomad_client"].dependencies == ["nomad_server"]
    assert entries["prometheus_node_exporter"].dependencies == ["prometheus"]
    assert entries["nomad_server"].action.kind == "playbook"
    assert entries["nomad_client"].action.playbook_by_os == {
        "linux": "playbooks/nomad/setup_nomad_client.yml",
        "macos": "playbooks/nomad/setup_nomad_client_macos.yml",
    }
    assert entries["node_agent"].action.playbook == "playbooks/agent/setup_opencode.yml"
    assert entries["node_agent"].action.bindings["llm_provider"].config_file == "~/.config/opencode/opencode.json"
    assert entries["node_agent"].action.bindings["llm_provider"].json_path == "provider.ollama.options.baseURL"
    assert entries["ollama"].observe_only is True


def test_profile_absent_from_reconciliation_is_simply_not_present(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profiles": {},
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"}},
            },
        },
    )

    entries = load_profile_reconciliation(playbook_dir, {"grafana", "prometheus"})

    assert set(entries) == {"grafana"}


def test_unknown_profile_name_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {"deployment_profile_reconciliation": {"nope": {"observe_only": True}}},
    )

    with pytest.raises(ProfileReconciliationError, match="unknown profiles"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_unknown_dependency_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"},
                    "dependencies": ["ghost"],
                }
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="unknown profiles"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_dependency_cycle_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "a": {"action": {"kind": "dnsmasq_config"}, "dependencies": ["b"]},
                "b": {"action": {"kind": "dnsmasq_config"}, "dependencies": ["a"]},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="cycle"):
        load_profile_reconciliation(playbook_dir, {"a", "b"})


def test_action_and_observe_only_are_mutually_exclusive(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {"kind": "dnsmasq_config"},
                    "observe_only": True,
                }
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_entry_with_neither_action_nor_observe_only_is_rejected(tmp_path):
    playbook_dir = _write(tmp_path, {"deployment_profile_reconciliation": {"grafana": {}}})

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_removed_install_path_key_is_rejected(tmp_path):
    # autotask_intent Step 2: install_path migrated into explicit
    # `checks: [{kind: file_exists, ...}]`; the old key must not silently load.
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "swarmui": {"observe_only": True, "install_path": "~/StabilityMatrix/Packages/SwarmUI"}
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"swarmui"})


def test_playbook_path_must_be_relative(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "/etc/passwd"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="must be relative"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_playbook_path_cannot_escape_playbook_dir(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "../../etc/passwd"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="escapes"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_dnsmasq_config_action_forbids_playbook_field(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {"action": {"kind": "dnsmasq_config", "playbook": "playbooks/x.yml"}},
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"dnsmasq"})


def test_playbook_action_needs_exactly_one_of_playbook_or_playbook_by_os(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {"deployment_profile_reconciliation": {"grafana": {"action": {"kind": "playbook"}}}},
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_managed_files_relative_path_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "managed_files": {"records": {"path": "relative/records.conf"}},
                    }
                },
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="absolute"):
        load_profile_reconciliation(playbook_dir, {"dnsmasq"})


def test_managed_files_defaults_digest_to_sha256(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "managed_files": {"records": {"path": "/etc/dnsmasq.d/nintent-records.conf"}},
                    }
                },
            }
        },
    )

    entries = load_profile_reconciliation(playbook_dir, {"dnsmasq"})

    assert entries["dnsmasq"].action.managed_files["records"].digest == "sha256"


def test_managed_files_forbidden_on_playbook_actions(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {
                        "kind": "playbook",
                        "playbook": "playbooks/monitoring/setup_grafana.yml",
                        "managed_files": {"x": {"path": "/etc/x.conf"}},
                    }
                },
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"grafana"})


# --- bindings (service_relation Phase 3) -------------------------------------


def test_bindings_forbidden_on_dnsmasq_config_actions(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "bindings": {"x": {"config_file": "/etc/x.json", "json_path": "a.b"}},
                    }
                },
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"dnsmasq"})


def test_binding_config_file_must_be_absolute_or_home_relative(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {
                        "kind": "playbook",
                        "playbook": "playbooks/monitoring/setup_grafana.yml",
                        "bindings": {"x": {"config_file": "relative/x.json", "json_path": "a.b"}},
                    }
                },
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="absolute or home-relative"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_binding_json_path_must_not_be_empty(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {
                        "kind": "playbook",
                        "playbook": "playbooks/monitoring/setup_grafana.yml",
                        "bindings": {"x": {"config_file": "~/x.json", "json_path": ""}},
                    }
                },
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="json_path"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_binding_config_file_accepts_home_relative_path(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {
                        "kind": "playbook",
                        "playbook": "playbooks/monitoring/setup_grafana.yml",
                        "bindings": {"llm_provider": {"config_file": "~/.config/opencode/opencode.json", "json_path": "a.b"}},
                    }
                },
            }
        },
    )

    entries = load_profile_reconciliation(playbook_dir, {"grafana"})

    assert entries["grafana"].action.bindings["llm_provider"].config_file == "~/.config/opencode/opencode.json"


# --- resolve_dnsmasq_records_spec (fix_sshkey4 Step 3) -----------------------


def test_resolve_dnsmasq_records_spec_returns_the_one_spec(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "managed_files": {"records": {"path": "/etc/dnsmasq.d/nintent-records.conf"}},
                    }
                },
                "grafana": {"action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"}},
            }
        },
    )
    entries = load_profile_reconciliation(playbook_dir, {"dnsmasq", "grafana"})

    spec = resolve_dnsmasq_records_spec(entries)

    assert spec.path == "/etc/dnsmasq.d/nintent-records.conf"
    assert spec.digest == "sha256"


def test_resolve_dnsmasq_records_spec_missing_is_an_error(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {"action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"}},
            }
        },
    )
    entries = load_profile_reconciliation(playbook_dir, {"grafana"})

    with pytest.raises(ProfileReconciliationError):
        resolve_dnsmasq_records_spec(entries)


def test_resolve_dnsmasq_records_spec_rejects_more_than_one_dnsmasq_profile(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "dnsmasq": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "managed_files": {"records": {"path": "/etc/dnsmasq.d/nintent-records.conf"}},
                    }
                },
                "dnsmasq2": {
                    "action": {
                        "kind": "dnsmasq_config",
                        "managed_files": {"records": {"path": "/etc/dnsmasq.d/other-records.conf"}},
                    }
                },
            }
        },
    )
    entries = load_profile_reconciliation(playbook_dir, {"dnsmasq", "dnsmasq2"})

    with pytest.raises(ProfileReconciliationError):
        resolve_dnsmasq_records_spec(entries)


# --- autotask_intent Step 1: explicit `checks` schema ---


def test_checks_parse_on_observe_only_profile(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "cron_task": {
                    "observe_only": True,
                    "checks": [{"kind": "file_exists", "path_from_config": "script_path"}],
                },
                "ollama": {
                    "observe_only": True,
                    "checks": [{"kind": "http", "paths": ["/v1/models", "/api/tags"]}],
                },
            }
        },
    )

    entries = load_profile_reconciliation(playbook_dir, {"cron_task", "ollama"})

    assert entries["cron_task"].checks[0].kind == "file_exists"
    assert entries["cron_task"].checks[0].path_from_config == "script_path"
    assert entries["ollama"].checks[0].kind == "http"
    assert entries["ollama"].checks[0].paths == ["/v1/models", "/api/tags"]


def test_checks_on_action_profile_are_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "grafana": {
                    "action": {"kind": "playbook", "playbook": "playbooks/monitoring/setup_grafana.yml"},
                    "checks": [{"kind": "file_exists", "path": "/opt/grafana"}],
                }
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="observe_only"):
        load_profile_reconciliation(playbook_dir, {"grafana"})


def test_file_exists_check_needs_exactly_one_path_source(tmp_path):
    for bad in (
        {"kind": "file_exists"},
        {"kind": "file_exists", "path": "/a", "path_from_config": "b"},
    ):
        playbook_dir = _write(
            tmp_path / str(bool(bad.get("path"))),
            {"deployment_profile_reconciliation": {"cron_task": {"observe_only": True, "checks": [bad]}}},
        )
        with pytest.raises(ProfileReconciliationError, match="exactly one of path or path_from_config"):
            load_profile_reconciliation(playbook_dir, {"cron_task"})


def test_file_exists_literal_path_must_be_absolute_or_home_relative(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "cron_task": {"observe_only": True, "checks": [{"kind": "file_exists", "path": "relative/path"}]}
            }
        },
    )

    with pytest.raises(ProfileReconciliationError, match="absolute or home-relative"):
        load_profile_reconciliation(playbook_dir, {"cron_task"})


def test_http_check_paths_must_be_non_empty_and_rooted(tmp_path):
    for bad_paths, match in (([], "at least one path"), (["v1/models"], "must start with"),):
        playbook_dir = _write(
            tmp_path / str(len(bad_paths)),
            {
                "deployment_profile_reconciliation": {
                    "ollama": {"observe_only": True, "checks": [{"kind": "http", "paths": bad_paths}]}
                }
            },
        )
        with pytest.raises(ProfileReconciliationError, match=match):
            load_profile_reconciliation(playbook_dir, {"ollama"})


def test_unknown_check_kind_is_rejected(tmp_path):
    playbook_dir = _write(
        tmp_path,
        {
            "deployment_profile_reconciliation": {
                "cron_task": {"observe_only": True, "checks": [{"kind": "cron_registered"}]}
            }
        },
    )

    with pytest.raises(ProfileReconciliationError):
        load_profile_reconciliation(playbook_dir, {"cron_task"})


def test_resolve_check_hints_substitutes_path_from_config(tmp_path):
    from nctl_core.reconcile.profiles import CheckResolutionError, ProfileReconciliation, resolve_check_hints

    entry = ProfileReconciliation.model_validate(
        {"observe_only": True, "checks": [{"kind": "file_exists", "path_from_config": "script_path"}]}
    )

    assert resolve_check_hints(entry, {"script_path": "/home/eiji/mycron/heartbeat.sh"}, "ctx") == [
        {"kind": "file_exists", "path": "/home/eiji/mycron/heartbeat.sh"}
    ]
    assert resolve_check_hints(
        ProfileReconciliation.model_validate(
            {"observe_only": True, "checks": [{"kind": "http", "paths": ["/v1/models"]}]}
        ),
        {},
        "ctx",
    ) == [{"kind": "http", "paths": ["/v1/models"]}]

    for bad_config in ({}, {"script_path": ""}, {"script_path": 3}, {"script_path": "relative/x.sh"}):
        with pytest.raises(CheckResolutionError, match="ctx"):
            resolve_check_hints(entry, bad_config, "ctx")
