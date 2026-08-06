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
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

RECONCILIATION_KEY = "deployment_profile_reconciliation"
_KNOWN_ACTION_KINDS = frozenset({"playbook", "dnsmasq_config"})


class ProfileReconciliationError(Exception):
    """The `deployment_profile_reconciliation` section is missing, unparsable, or invalid."""


class CheckResolutionError(Exception):
    """A profile check could not be resolved against a placement's config.

    Raised at probe-hint render time (autotask_intent Step 1): a
    `path_from_config` check whose placement config lacks the named key, or
    holds an unusable value, is a validation error for that placement --
    never a silent skip that would let an unexercised check read as
    converged (README_DEV lesson 1).
    """


class FileExistsCheckSpec(BaseModel):
    """One closed existence-proof check: a file must exist on the target node.

    autotask_intent Step 1: the explicit, parameterized replacement for
    check knowledge that used to be implied by field presence or hard-coded
    by service name. Exactly one of `path` (a literal, absolute or
    home-relative) or `path_from_config` (the name of a placement `config`
    key holding the path) must be set. Resolution against the placement
    config happens at probe-hint render time (`resolve_check_hints`), so
    nodeutils only ever sees a fully-resolved path.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["file_exists"] = "file_exists"
    path: str | None = None
    path_from_config: str | None = None

    @model_validator(mode="after")
    def _check_exactly_one_source(self) -> "FileExistsCheckSpec":
        if bool(self.path) == bool(self.path_from_config):
            raise ValueError("a file_exists check needs exactly one of path or path_from_config")
        if self.path is not None and not (self.path.startswith("~/") or Path(self.path).is_absolute()):
            raise ValueError(
                f"file_exists path must be absolute or home-relative (~/...): {self.path!r}"
            )
        return self

    def resolve_path(self, placement_config: dict[str, Any], context: str) -> str:
        if self.path is not None:
            return self.path
        assert self.path_from_config is not None
        value = placement_config.get(self.path_from_config)
        if not isinstance(value, str) or not value.strip():
            raise CheckResolutionError(
                f"{context}: file_exists check requires placement config key "
                f"{self.path_from_config!r} to be a non-empty string, got {value!r}"
            )
        if not (value.startswith("~/") or Path(value).is_absolute()):
            raise CheckResolutionError(
                f"{context}: file_exists path from config key {self.path_from_config!r} "
                f"must be absolute or home-relative (~/...): {value!r}"
            )
        return value


class HttpCheckSpec(BaseModel):
    """One closed HTTP liveness check against the placement's declared endpoint.

    autotask_intent Step 1: absorbs the paths that nodeutils'
    `HTTP_PROBE_SPECS` used to hard-code by service name. nodeutils probes
    `<endpoint><path>` for each path in order and reports the first bounded
    HTTP status; the endpoint itself still comes from the placement's
    declared `DesiredEndpoint` hint, never from this spec.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["http"] = "http"
    paths: list[str]

    @model_validator(mode="after")
    def _check_paths(self) -> "HttpCheckSpec":
        if not self.paths:
            raise ValueError("an http check needs at least one path")
        for path in self.paths:
            if not path.startswith("/"):
                raise ValueError(f"http check paths must start with '/': {path!r}")
        return self


ProfileCheckSpec = FileExistsCheckSpec | HttpCheckSpec


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


class BindingSlotSpec(BaseModel):
    """One closed consumer-side binding observation target (service_relation Phase 3).

    Declares where a bound provider endpoint is written on the consumer node
    and what to read back. `config_file` intentionally allows a `~`-relative
    path (unlike `ManagedFileSpec.path`): the OpenCode config lives under the
    login user's home and nodeutils runs as that user, so a deliberate
    `Path.expanduser()` is the one documented deviation from the
    managed-files "path must be absolute" rule. `json_path` is a dot-notation
    path into the parsed JSON config (e.g. `provider.ollama.options.baseURL`).
    Copied verbatim into the nodeutils probe config by
    `nctl_core.observation.render_probe_hints`; nodeutils never re-derives it.
    """

    model_config = ConfigDict(extra="forbid")

    config_file: str
    json_path: str

    @model_validator(mode="after")
    def _check_config_file(self) -> "BindingSlotSpec":
        if not (self.config_file.startswith("~/") or Path(self.config_file).is_absolute()):
            raise ValueError(
                f"binding config_file must be absolute or home-relative (~/...): {self.config_file!r}"
            )
        if not self.json_path:
            raise ValueError("binding json_path must not be empty")
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
    # Closed binding-slot observation targets (service_relation Phase 3).
    # Only `playbook` profiles declare these -- a consumer-side config slot
    # that binds to another service, not a dnsmasq-style managed file.
    bindings: dict[str, BindingSlotSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_playbook_fields(self) -> "ProfileAction":
        if self.kind == "dnsmasq_config":
            if self.playbook is not None or self.playbook_by_os:
                raise ValueError("dnsmasq_config actions must not set playbook/playbook_by_os")
            if self.bindings:
                raise ValueError("bindings is only supported for kind=playbook in this phase")
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
    # autotask_intent Step 1: the explicit, closed, parameterized check list
    # replacing implicit field-presence check knowledge. Only kinds with a
    # current consumer exist (`file_exists`, `http`). Restricted to
    # observe_only profiles in this phase -- every consumer is existence
    # proof, and action profiles keep their managed_files/bindings contracts.
    checks: list[Annotated[ProfileCheckSpec, Field(discriminator="kind")]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_action_or_exemption(self) -> "ProfileReconciliation":
        if self.action is not None and self.observe_only:
            raise ValueError("a profile cannot declare both an action and observe_only")
        if self.action is None and not self.observe_only:
            raise ValueError("a profile entry must declare an action or observe_only=true")
        if self.checks and not self.observe_only:
            raise ValueError("checks are only supported for observe_only profiles in this phase")
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


def resolve_check_hints(
    entry: ProfileReconciliation, placement_config: dict[str, Any], context: str
) -> list[dict[str, Any]]:
    """Resolve a profile's `checks` against one placement's config into hint rows.

    The profile layer owns check semantics; the returned rows are fully
    resolved (`path_from_config` already substituted), so nodeutils and the
    drift evaluator consume them verbatim and never read placement config.
    Raises `CheckResolutionError` when a `path_from_config` key is missing,
    empty, or not an absolute/home-relative path.
    """

    hint_rows: list[dict[str, Any]] = []
    for check in entry.checks:
        if isinstance(check, FileExistsCheckSpec):
            hint_rows.append({"kind": "file_exists", "path": check.resolve_path(placement_config, context)})
        else:
            hint_rows.append({"kind": "http", "paths": list(check.paths)})
    return hint_rows


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
