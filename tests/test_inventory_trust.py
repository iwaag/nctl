from nctl_core.inventory_trust import resolve_route_from_host_vars, validate_inventory_trust_contract
from nctl_core.ssh_trust import build_ansible_ssh_common_args, derive_host_key_alias

NODE_ID = "27818c12-fe15-4c9f-83d0-7949523f6c33"
ALIAS = derive_host_key_alias(NODE_ID)
KNOWN_HOSTS_PATH = "/home/user/.local/state/nctl/ssh/known_hosts"


def test_validate_inventory_trust_contract_accepts_exact_match():
    host_vars = {
        "nintent_desired_node_id": NODE_ID,
        "nctl_ssh_host_key_alias": ALIAS,
        "ansible_ssh_common_args": build_ansible_ssh_common_args(ALIAS, KNOWN_HOSTS_PATH),
    }
    assert validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH) is None


def test_validate_inventory_trust_contract_rejects_missing_node_id():
    error = validate_inventory_trust_contract({}, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "missing_desired_node_id"


def test_validate_inventory_trust_contract_rejects_invalid_node_id():
    error = validate_inventory_trust_contract(
        {"nintent_desired_node_id": "not-a-uuid"}, "agdnsmasq", KNOWN_HOSTS_PATH
    )
    assert error is not None
    assert error.code == "invalid_desired_node_id"


def test_validate_inventory_trust_contract_rejects_alias_mismatch():
    host_vars = {"nintent_desired_node_id": NODE_ID, "nctl_ssh_host_key_alias": "nctl-node-hand-written"}
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ssh_host_key_alias_mismatch"


def test_validate_inventory_trust_contract_rejects_missing_common_args():
    host_vars = {"nintent_desired_node_id": NODE_ID, "nctl_ssh_host_key_alias": ALIAS}
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ansible_ssh_common_args_mismatch"


def test_validate_inventory_trust_contract_rejects_weakened_common_args():
    host_vars = {
        "nintent_desired_node_id": NODE_ID,
        "nctl_ssh_host_key_alias": ALIAS,
        "ansible_ssh_common_args": build_ansible_ssh_common_args(ALIAS, KNOWN_HOSTS_PATH) + " -o StrictHostKeyChecking=no",
    }
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ansible_ssh_common_args_mismatch"


def test_validate_inventory_trust_contract_rejects_different_known_hosts_path():
    host_vars = {
        "nintent_desired_node_id": NODE_ID,
        "nctl_ssh_host_key_alias": ALIAS,
        "ansible_ssh_common_args": build_ansible_ssh_common_args(ALIAS, "/some/other/known_hosts"),
    }
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ansible_ssh_common_args_mismatch"


def _valid_host_vars(**extra):
    host_vars = {
        "nintent_desired_node_id": NODE_ID,
        "nctl_ssh_host_key_alias": ALIAS,
        "ansible_ssh_common_args": build_ansible_ssh_common_args(ALIAS, KNOWN_HOSTS_PATH),
    }
    host_vars.update(extra)
    return host_vars


def test_validate_inventory_trust_contract_rejects_exact_common_args_plus_hostile_ssh_args():
    # ansible_ssh_args is placed before ansible_ssh_common_args by Ansible and
    # OpenSSH keeps the first value for a repeated -o option, so this
    # inventory would otherwise pass the exact-common-args check while the
    # real connection used StrictHostKeyChecking=no and a different alias.
    host_vars = _valid_host_vars(ansible_ssh_args="-o StrictHostKeyChecking=no -o HostKeyAlias=attacker")
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ssh_policy_override_rejected"
    assert "ansible_ssh_args" in str(error)


def test_validate_inventory_trust_contract_rejects_each_forbidden_override_var():
    for var in (
        "ansible_ssh_args",
        "ansible_ssh_extra_args",
        "ansible_scp_extra_args",
        "ansible_sftp_extra_args",
        "ansible_ssh_executable",
        "ansible_host_key_checking",
        "ansible_ssh_host_key_checking",
    ):
        host_vars = _valid_host_vars(**{var: "anything"})
        error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
        assert error is not None, var
        assert error.code == "ssh_policy_override_rejected", var


def test_validate_inventory_trust_contract_rejects_non_ssh_connection():
    host_vars = _valid_host_vars(ansible_connection="local")
    error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
    assert error is not None
    assert error.code == "ansible_connection_invalid"


def test_validate_inventory_trust_contract_accepts_explicit_ssh_connection():
    host_vars = _valid_host_vars(ansible_connection="ssh")
    assert validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH) is None


def test_validate_inventory_trust_contract_accepts_integer_port():
    host_vars = _valid_host_vars(ansible_port=2222)
    assert validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH) is None


def test_validate_inventory_trust_contract_rejects_invalid_ports():
    for bad_port in ("2222", True, False, 0, -1, 65536, 3.5):
        host_vars = _valid_host_vars(ansible_port=bad_port)
        error = validate_inventory_trust_contract(host_vars, "agdnsmasq", KNOWN_HOSTS_PATH)
        assert error is not None, bad_port
        assert error.code == "ansible_port_invalid", bad_port


def test_resolve_route_from_host_vars_uses_bootstrap_ansible_host_verbatim():
    assert resolve_route_from_host_vars({"ansible_host": "agdnsmasq.local"}, "agdnsmasq") == "agdnsmasq.local"


def test_resolve_route_from_host_vars_ignores_unrendered_jinja_ansible_host():
    # Regression: `ansible-inventory --host` reports every production host's
    # ansible_host as inherited, unrendered from group_vars/all's Jinja
    # template -- ansible-inventory does not template variables. That must
    # never be used as a literal route.
    host_vars = {
        "ansible_host": "{{ tailscale_ip | default(local_connection_host, true) }}",
        "connection_path": "local",
        "local_ip": "192.168.0.2",
    }
    assert resolve_route_from_host_vars(host_vars, "agdnsmasq") == "192.168.0.2"


def test_resolve_route_from_host_vars_local_priority_chain():
    assert resolve_route_from_host_vars(
        {"connection_path": "local", "local_ip": "192.168.0.2", "mdns_hostname": "agdnsmasq.local"}, "agdnsmasq"
    ) == "192.168.0.2"
    assert resolve_route_from_host_vars(
        {"connection_path": "local", "mdns_hostname": "agdnsmasq.local"}, "agdnsmasq"
    ) == "agdnsmasq.local"
    assert resolve_route_from_host_vars({"connection_path": "local"}, "agdnsmasq") == "agdnsmasq"


def test_resolve_route_from_host_vars_tailscale():
    assert resolve_route_from_host_vars(
        {"connection_path": "tailscale", "tailscale_ip": "100.64.0.5"}, "agdnsmasq"
    ) == "100.64.0.5"


def test_resolve_route_from_host_vars_tailscale_without_ip_is_unresolved():
    assert resolve_route_from_host_vars({"connection_path": "tailscale"}, "agdnsmasq") is None


def test_resolve_route_from_host_vars_unsupported_connection_path_is_unresolved():
    assert resolve_route_from_host_vars({"connection_path": "vpn-unknown"}, "agdnsmasq") is None


def test_resolve_route_from_host_vars_no_information_at_all_is_unresolved():
    assert resolve_route_from_host_vars({}, "agdnsmasq") is None
