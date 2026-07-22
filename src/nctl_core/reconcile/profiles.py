"""Reconciliation metadata for Ansible deployment profiles (Phase 4 Step 5,
Decision 7).

Lives in the same `vars/deployment_profiles.yml` file
`production/profiles.py` reads, under a sibling top-level key,
`deployment_profile_reconciliation`, keyed by the same profile names as
`deployment_profiles`. It is deliberately a *separate* top-level key rather
than an extra field inside each `deployment_profiles.<name>` entry: that
entry is the frozen production-inventory byte contract
(`production/contract.py::validate_deployment_profiles`, digested into
`deployment_profile_digest`), and adding an unrelated reconciliation-only key
there would either break its closed `_PROFILE_KEYS` check or silently widen
a contract that other schema-1.0 consumers pin byte-for-byte. Reconciliation
metadata has its own validator here instead.

A profile absent from `deployment_profile_reconciliation` entirely -- or
present but declaring neither `action` nor `observe_only` -- is
`unsupported`: "a profile with neither action nor exemption is unsupported,
never silently satisfied" (p4/plan.md Step 5). Declaring an *empty* entry is
treated as a config mistake and rejected at load time (omit the key instead
of writing `{}` if a profile truly has no reconciliation story yet).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

RECONCILIATION_KEY = "deployment_profile_reconciliation"
_KNOWN_ACTION_KINDS = frozenset({"playbook", "dnsmasq_config"})


class ProfileReconciliationError(Exception):
    """The `deployment_profile_reconciliation` section is missing, unparsable, or invalid."""


class ManagedFileSpec(BaseModel):
    """One closed managed-file observation target (fix_sshkey3 Step 4).

    The one metadata-owned source of the deployed path -- `nctl_core.
    observation.render_probe_hints` copies it verbatim into the nodeutils
    probe config, and `ansible_agdev`'s deploy playbook must actuate the
    same path. `path` must be absolute: the deployed managed-file path is
    never resolved relative to anything (a Nautobot-editable relative value
    could otherwise be walked to an unintended location on the target).
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    digest: Literal["sha256"] = "sha256"

    @model_validator(mode="after")
    def _check_absolute_path(self) -> "ManagedFileSpec":
        if not Path(self.path).is_absolute():
            raise ValueError(f"managed file path must be absolute: {self.path!r}")
        return self


class ProfileAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["playbook", "dnsmasq_config"]
    # Exactly one of these two for kind="playbook"; neither for "dnsmasq_config".
    playbook: str | None = None
    playbook_by_os: dict[str, str] = Field(default_factory=dict)
    # Closed managed-file observation targets (fix_sshkey3 Step 4). Only
    # `dnsmasq_config` declares these in this phase -- content convergence
    # for arbitrary playbook profiles is explicitly out of scope.
    managed_files: dict[str, ManagedFileSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_playbook_fields(self) -> "ProfileAction":
        if self.kind == "dnsmasq_config":
            if self.playbook is not None or self.playbook_by_os:
                raise ValueError("dnsmasq_config actions must not set playbook/playbook_by_os")
        else:
            if bool(self.playbook) == bool(self.playbook_by_os):
                raise ValueError("a playbook action needs exactly one of playbook or playbook_by_os")
            if self.managed_files:
                raise ValueError("managed_files is only supported for kind=dnsmasq_config in this phase")
        return self

    def playbook_paths(self) -> list[str]:
        if self.playbook is not None:
            return [self.playbook]
        return sorted(self.playbook_by_os.values())


class ProfileReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ProfileAction | None = None
    observe_only: bool = False
    dependencies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_action_or_exemption(self) -> "ProfileReconciliation":
        if self.action is not None and self.observe_only:
            raise ValueError("a profile cannot declare both an action and observe_only")
        if self.action is None and not self.observe_only:
            raise ValueError("a profile entry must declare an action or observe_only=true")
        return self


def load_profile_reconciliation(
    playbook_dir: Path, profile_names: set[str]
) -> dict[str, ProfileReconciliation]:
    """Parse and validate `deployment_profile_reconciliation` for known profiles.

    `profile_names` is the already-validated `deployment_profiles` key set
    (`production.profiles.load_deployment_profiles`); every reconciliation
    entry and dependency must name a profile from that set, and every
    `action.playbook`/`playbook_by_os` value must resolve inside
    `playbook_dir` (Decision 7's path confinement).
    """

    path = playbook_dir / "vars" / "deployment_profiles.yml"
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise ProfileReconciliationError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProfileReconciliationError(f"cannot parse {path}: {exc}") from exc

    section = (raw or {}).get(RECONCILIATION_KEY, {})
    if not isinstance(section, dict):
        raise ProfileReconciliationError(f"{path}: {RECONCILIATION_KEY} must be an object")

    unknown_profiles = sorted(set(section) - profile_names)
    if unknown_profiles:
        raise ProfileReconciliationError(
            f"{path}: {RECONCILIATION_KEY} names unknown profiles: {', '.join(unknown_profiles)}"
        )

    entries: dict[str, ProfileReconciliation] = {}
    for name in sorted(section):
        try:
            entry = ProfileReconciliation.model_validate(section[name])
        except Exception as exc:  # pydantic ValidationError / our ValueError
            raise ProfileReconciliationError(f"{path}: {RECONCILIATION_KEY}.{name}: {exc}") from exc
        entries[name] = entry

    for name, entry in entries.items():
        unknown_deps = sorted(set(entry.dependencies) - profile_names)
        if unknown_deps:
            raise ProfileReconciliationError(
                f"{path}: {RECONCILIATION_KEY}.{name}.dependencies names unknown profiles: "
                f"{', '.join(unknown_deps)}"
            )
        if entry.action is not None and entry.action.kind == "playbook":
            for rel_path in entry.action.playbook_paths():
                _confine_playbook_path(playbook_dir, rel_path, f"{RECONCILIATION_KEY}.{name}.action")

    _reject_dependency_cycles(entries, path)
    return entries


def resolve_dnsmasq_records_spec(entries: dict[str, ProfileReconciliation]) -> ManagedFileSpec:
    """Return the one validated dnsmasq records `ManagedFileSpec` (fix_sshkey4 Step 3).

    The single metadata-owned source of the deployed dnsmasq destination --
    used identically by nodeutils probe-hint rendering
    (`observation.render_probe_hints`), drift evidence
    (`evaluation_snapshot._content_spec_by_service_id`), and
    `dnsmasq_apply.build_dnsmasq_apply`'s Ansible extra variables, so all
    three can never independently drift. Requires exactly one
    `deployment_profile_reconciliation` entry with `action.kind ==
    "dnsmasq_config"`, and that entry's `managed_files` to be exactly
    `{"records": ManagedFileSpec(...)}` (absolute path and `digest ==
    "sha256"` are already enforced by `ManagedFileSpec` itself at load
    time). Absence or any other shape is a structured configuration error,
    never a fallback default.
    """
    matches = [
        entry for entry in entries.values() if entry.action is not None and entry.action.kind == "dnsmasq_config"
    ]
    if not matches:
        raise ProfileReconciliationError(
            f"no {RECONCILIATION_KEY} entry declares an action.kind == 'dnsmasq_config'"
        )
    if len(matches) > 1:
        raise ProfileReconciliationError(
            f"exactly one dnsmasq_config {RECONCILIATION_KEY} entry is supported in this phase, "
            f"found {len(matches)}"
        )
    action = matches[0].action
    assert action is not None
    if set(action.managed_files) != {"records"}:
        raise ProfileReconciliationError(
            f"a dnsmasq_config action must declare exactly one managed_files entry named 'records', "
            f"got {sorted(action.managed_files)}"
        )
    return action.managed_files["records"]


def is_supported(entries: dict[str, ProfileReconciliation], profile_name: str) -> bool:
    """A profile is reconcile-supported only if it declares an action or observe_only."""

    return profile_name in entries


def _confine_playbook_path(playbook_dir: Path, rel_path: str, context: str) -> None:
    if Path(rel_path).is_absolute():
        raise ProfileReconciliationError(f"{context}: playbook path must be relative: {rel_path!r}")
    resolved_root = playbook_dir.resolve()
    resolved = (playbook_dir / rel_path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ProfileReconciliationError(
            f"{context}: playbook path escapes playbook_dir: {rel_path!r}"
        )


def _reject_dependency_cycles(entries: dict[str, ProfileReconciliation], path: Path) -> None:
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(name: str, chain: tuple[str, ...]) -> None:
        if name in visited or name not in entries:
            return
        if name in in_progress:
            raise ProfileReconciliationError(
                f"{path}: {RECONCILIATION_KEY} dependency cycle: {' -> '.join((*chain, name))}"
            )
        in_progress.add(name)
        for dep in sorted(entries[name].dependencies):
            visit(dep, (*chain, name))
        in_progress.discard(name)
        visited.add(name)

    for name in sorted(entries):
        visit(name, ())
