"""nctl CLI: thin Typer wrappers around nctl_core.

Convention: commands parse arguments, call nctl_core, and render the result.
No business logic lives in this module.
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import ValidationError

from nctl_core.actual_render import build_actual, render_actual_text
from nctl_core.agent import (
    AGENT_ABORT_SCHEMA, AGENT_RUN_SCHEMA, AGENT_SEND_SCHEMA, AGENT_SESSIONS_SCHEMA, AGENT_STATUS_SCHEMA,
    AgentError, attach_agent, build_agent_abort, build_agent_run, build_agent_send, build_agent_sessions, build_agent_status,
)
from nctl_core.agent_render import render_agent_abort_text, render_agent_sessions_text, render_agent_status_text, render_agent_task_text
from nctl_core.braindump_render import (
    build_braindump_complete,
    build_braindump_create,
    build_braindump_list,
    build_braindump_purge,
    build_braindump_review,
    build_braindump_review_delete,
    build_braindump_show,
    build_braindump_supersede,
    render_braindump_complete_text,
    render_braindump_create_text,
    render_braindump_list_text,
    render_braindump_purge_text,
    render_braindump_review_delete_text,
    render_braindump_review_text,
    render_braindump_show_text,
    render_braindump_supersede_text,
)
from nctl_core.config import Config, ConfigError
from nctl_core.dnsmasq_apply import build_dnsmasq_apply, render_dnsmasq_apply_text
from nctl_core.dnsmasq_render import build_dnsmasq_render, render_dnsmasq_conf_text, render_dnsmasq_summary_text
from nctl_core.drift_render import build_drift, render_drift_text
from nctl_core.hosts_intent_render import (
    build_hosts_intent_render,
    render_hosts_intent_inventory_text,
    render_hosts_intent_summary_text,
    write_hosts_intent_artifacts,
)
from nctl_core.lifecycle import LIFECYCLE_STATES, build_lifecycle, render_lifecycle_text
from nctl_core.retirement_prune import render_prune_text, run_prune
from nctl_core.desired_apply import apply_document
from nctl_core.desired_write import DesiredWriteError
from nctl_core.ops_render import build_ops_list, build_ops_show, render_ops_list_text, render_ops_show_text
from nctl_core.output import emit
from nctl_core.production_render import (
    build_production_render,
    render_production_inventory_text,
    render_production_summary_text,
    write_production_artifacts,
)
from nctl_core.reconcile.executor import run_reconcile
from nctl_core.reconcile_render import render_reconcile_text
from nctl_core.relations_render import build_relations, render_relations_text
from nctl_core.workspaces_render import build_workspaces, render_workspaces_text
from nctl_core.session import build_session_new, render_session_new_text
from nctl_core.status import build_status, render_status_text
from nctl_core.ssh_enroll import build_ssh_enroll, render_ssh_enroll_text
from nctl_core.workflow_episode import DEFAULT_LIST_STATUSES
from nctl_core.workflow_episode_render import (
    build_workflow_episode_create,
    build_workflow_episode_dismiss,
    build_workflow_episode_list,
    build_workflow_episode_resolve,
    build_workflow_episode_select,
    build_workflow_episode_show,
    build_workflow_episode_write,
    render_workflow_episode_create_text,
    render_workflow_episode_list_text,
    render_workflow_episode_show_text,
    render_workflow_episode_transition_text,
    render_workflow_episode_write_text,
)

app = typer.Typer(help="Unified CLI for pj-clusterintent reconciliation workflows.")
render_app = typer.Typer(help="Deterministic renders of desired state into consumer formats.")
apply_app = typer.Typer(help="Apply rendered desired state through deployment automation.")
ops_app = typer.Typer(help="Inspect past and running operations from the event-log directory.")
braindump_app = typer.Typer(help="Read immutable Braindumps and manage their Alignment Reviews.")
ssh_app = typer.Typer(help="Manage the local, alias-keyed SSH trust store nctl uses for actuation.")
session_app = typer.Typer(help="Create isolated agent-workspace session folders under .local/workspace/.")
desired_app = typer.Typer(help="Preview or atomically apply a desired-state batch document.")
agent_app = typer.Typer(help="Reach loopback-only node agents through managed SSH tunnels.")
workflow_episode_app = typer.Typer(help="Read and manage workflow-improvement episodes.")
app.add_typer(render_app, name="render")
app.add_typer(apply_app, name="apply")
app.add_typer(ops_app, name="ops")
app.add_typer(braindump_app, name="braindump")
app.add_typer(ssh_app, name="ssh")
app.add_typer(session_app, name="session")
app.add_typer(desired_app, name="desired")
app.add_typer(agent_app, name="agent")
app.add_typer(workflow_episode_app, name="workflow-episode")


@app.callback()
def _root() -> None:
    """Keep subcommand names explicit even while only one command exists."""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

ConfigOption = Annotated[
    Optional[Path],
    typer.Option("--config", help="Path to nctl.toml (defaults to $NCTL_CONFIG, ./nctl.toml, repo root)."),
]


def _load_config(config_path: Path | None) -> Config:
    try:
        return Config.load(config_path)
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE)


JsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.status.v1 envelope as JSON.")]


@app.command()
def status(config: ConfigOption = None, json_output: JsonOption = False) -> None:
    """Check Nautobot connectivity, nodeutils dumps freshness, and submodule state."""
    cfg = _load_config(config)
    envelope = build_status(cfg)
    emit(envelope, json_output, render_status_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


AgentHostArgument = Annotated[str, typer.Argument(help="Exact DesiredNode slug.")]
AgentStatusJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.agent.status.v1 envelope as JSON.")]
AgentSessionOption = Annotated[Optional[str], typer.Option("--session", help="Existing OpenCode session ID to resume.")]
AgentPromptOption = Annotated[str, typer.Option("--prompt", help="Prompt to send to the node agent.")]
AgentJsonOption = Annotated[bool, typer.Option("--json", help="Print the command envelope as JSON.")]


@agent_app.command("status")
def agent_status(host: AgentHostArgument, config: ConfigOption = None, json_output: AgentStatusJsonOption = False) -> None:
    """Check a node agent's /doc health endpoint through managed SSH."""
    cfg = _load_config(config)
    envelope = build_agent_status(cfg, host)
    emit(envelope, json_output, render_agent_status_text)
    if any(error.code == "unknown_host" for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


@agent_app.command("attach")
def agent_attach(host: AgentHostArgument, config: ConfigOption = None, session: AgentSessionOption = None) -> None:
    """Open the controller's native OpenCode TUI through a temporary SSH tunnel."""
    cfg = _load_config(config)
    try:
        code = attach_agent(cfg, host, session=session)
    except AgentError as exc:
        typer.echo(f"error [{exc.code}]: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE if exc.code == "unknown_host" else EXIT_FAILURE)
    raise typer.Exit(code)


@agent_app.command("sessions")
def agent_sessions(host: AgentHostArgument, config: ConfigOption = None, json_output: AgentJsonOption = False) -> None:
    """List sessions scoped to the node's configured working directory."""
    envelope = build_agent_sessions(_load_config(config), host)
    emit(envelope, json_output, render_agent_sessions_text)
    raise typer.Exit(EXIT_USAGE if any(error.code == "unknown_host" for error in envelope.errors) else (EXIT_OK if envelope.ok else EXIT_FAILURE))


@agent_app.command("run")
def agent_run(host: AgentHostArgument, prompt: AgentPromptOption, config: ConfigOption = None, json_output: AgentJsonOption = False) -> None:
    """Create a session, send a prompt, and wait for the completed reply."""
    envelope = build_agent_run(_load_config(config), host, prompt)
    emit(envelope, json_output, render_agent_task_text)
    raise typer.Exit(EXIT_USAGE if any(error.code == "unknown_host" for error in envelope.errors) else (EXIT_OK if envelope.ok else EXIT_FAILURE))


@agent_app.command("send")
def agent_send(host: AgentHostArgument, session_id: Annotated[str, typer.Argument(help="OpenCode session ID.")], prompt: AgentPromptOption, config: ConfigOption = None, json_output: AgentJsonOption = False) -> None:
    """Continue an existing node-local session and wait for its reply."""
    envelope = build_agent_send(_load_config(config), host, session_id, prompt)
    emit(envelope, json_output, render_agent_task_text)
    raise typer.Exit(EXIT_USAGE if any(error.code == "unknown_host" for error in envelope.errors) else (EXIT_OK if envelope.ok else EXIT_FAILURE))


@agent_app.command("abort")
def agent_abort(host: AgentHostArgument, session_id: Annotated[str, typer.Argument(help="OpenCode session ID.")], config: ConfigOption = None, json_output: AgentJsonOption = False) -> None:
    """Deliberately interrupt a running OpenCode session."""
    envelope = build_agent_abort(_load_config(config), host, session_id)
    emit(envelope, json_output, render_agent_abort_text)
    raise typer.Exit(EXIT_USAGE if any(error.code == "unknown_host" for error in envelope.errors) else (EXIT_OK if envelope.ok else EXIT_FAILURE))


ActualJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.actual.v1 envelope as JSON.")]


@app.command()
def actual(config: ConfigOption = None, json_output: ActualJsonOption = False) -> None:
    """Read-only typed actual-state diagnostic: observer Device -> Proxmox Cluster -> guests.

    Not drift and has no write path -- it renders only what nctl's typed actual reader
    observed in Nautobot (native Cluster/VirtualMachine/VMInterface fields plus the
    dedicated proxmox_* custom fields). It never infers desired ownership or the future
    desired Cluster slug.
    """
    cfg = _load_config(config)
    envelope = build_actual(cfg)
    emit(envelope, json_output, render_actual_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


DriftJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.drift.v1 envelope as JSON.")]
HostOption = Annotated[Optional[str], typer.Option("--host", help="Filter to a single node by slug.")]
ServiceOption = Annotated[Optional[str], typer.Option("--service", help="Filter to a single service by name.")]


@app.command()
def drift(config: ConfigOption = None, host: HostOption = None, service: ServiceOption = None, json_output: DriftJsonOption = False) -> None:
    """Compute desired-vs-actual drift across nodes and services (converged/drifting/converging/unknown)."""
    cfg = _load_config(config)
    envelope = build_drift(cfg, host=host, service=service)
    emit(envelope, json_output, render_drift_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


RelationsJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.relations.v1 envelope as JSON.")]


@app.command()
def relations(config: ConfigOption = None, host: HostOption = None, service: ServiceOption = None, json_output: RelationsJsonOption = False) -> None:
    """Who depends on what, and is it real: every binding edge with resolved provider, actual state, and gap codes."""
    cfg = _load_config(config)
    envelope = build_relations(cfg, host=host, service=service)
    emit(envelope, json_output, render_relations_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


WorkspacesJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.workspaces.v1 envelope as JSON.")]


@app.command()
def workspaces(config: ConfigOption = None, host: HostOption = None, json_output: WorkspacesJsonOption = False) -> None:
    """One row per declared workspace: node, presence, identity match, activity class, observation freshness."""
    cfg = _load_config(config)
    envelope = build_workspaces(cfg, host=host)
    emit(envelope, json_output, render_workspaces_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


OutOption = Annotated[
    Optional[Path],
    typer.Option("--out", help="Write the conf to this path instead of stdout (prints a summary instead)."),
]
RenderJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.render.dnsmasq.v3 envelope as JSON.")]


@render_app.command("dnsmasq")
def render_dnsmasq(config: ConfigOption = None, out: OutOption = None, json_output: RenderJsonOption = False) -> None:
    """Render the dnsmasq conf from desired endpoints, IP ranges, and intent evaluations."""
    cfg = _load_config(config)
    envelope = build_dnsmasq_render(cfg)

    if json_output:
        print(envelope.to_json())
    elif envelope.ok and out is not None:
        out.write_text(envelope.data.conf)
        print(render_dnsmasq_summary_text(envelope))
    else:
        print(render_dnsmasq_conf_text(envelope))

    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


ProductionOutOption = Annotated[
    Optional[Path],
    typer.Option(
        "--out",
        help=(
            "Write production.yml + production.reports/<id>.json to this directory instead of "
            "stdout. Pass the directory containing the configured ansible.inventory path to "
            "regenerate it in place."
        ),
    ),
]
RenderProductionJsonOption = Annotated[
    bool, typer.Option("--json", help="Print the nctl.render.production.v1 envelope as JSON.")
]


@render_app.command("production")
def render_production(
    config: ConfigOption = None, out: ProductionOutOption = None, json_output: RenderProductionJsonOption = False
) -> None:
    """Compose the production inventory from desired placements and actual facts.

    Without `--out`, the inventory YAML goes to stdout (pipeable, matches
    `render dnsmasq`). With `--out DIR`, writes `DIR/production.yml` and
    `DIR/production.reports/<generation_id>.json` (validated with
    `ansible-inventory --list` first) and prints a summary instead.
    """
    cfg = _load_config(config)
    envelope = build_production_render(cfg)

    if envelope.ok and out is not None:
        write_error = write_production_artifacts(envelope, out)
        if write_error is not None:
            envelope = envelope.model_copy(update={"ok": False, "errors": [write_error]})

    if json_output:
        print(envelope.to_json())
    elif envelope.ok and out is not None:
        print(render_production_summary_text(envelope))
    else:
        print(render_production_inventory_text(envelope))

    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


HostsIntentOutOption = Annotated[
    Optional[Path],
    typer.Option(
        "--out",
        help=(
            "Write hosts_intent.yml + hosts-intent-export.json to this directory instead of "
            "stdout (validated with `ansible-inventory --list` first)."
        ),
    ),
]
RenderHostsIntentJsonOption = Annotated[
    bool, typer.Option("--json", help="Print the nctl.render.hosts_intent.v1 envelope as JSON.")
]


@render_app.command("hosts-intent")
def render_hosts_intent(
    config: ConfigOption = None, out: HostsIntentOutOption = None, json_output: RenderHostsIntentJsonOption = False
) -> None:
    """Render the mDNS bootstrap inventory from desired nodes.

    Without `--out`, the inventory YAML goes to stdout (pipeable, matches the
    other render commands). With `--out DIR`, writes `DIR/hosts_intent.yml`
    (validated with `ansible-inventory --list` against a staged copy, then
    atomically replaced) and `DIR/hosts-intent-export.json`, and prints a
    summary instead.
    """
    cfg = _load_config(config)
    envelope = build_hosts_intent_render(cfg)

    if envelope.ok and out is not None:
        write_error = write_hosts_intent_artifacts(envelope, out)
        if write_error is not None:
            envelope = envelope.model_copy(update={"ok": False, "errors": [write_error]})

    if json_output:
        print(envelope.to_json())
    elif envelope.ok and out is not None:
        print(render_hosts_intent_summary_text(envelope))
    else:
        print(render_hosts_intent_inventory_text(envelope))

    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


OpsListJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.ops.list.v1 envelope as JSON.")]
OpsLimitOption = Annotated[
    Optional[int], typer.Option("--limit", min=1, help="Show at most this many operations (newest first).")
]


@ops_app.command("list")
def ops_list(config: ConfigOption = None, limit: OpsLimitOption = None, json_output: OpsListJsonOption = False) -> None:
    """List operations found in the event-log directory, newest first."""
    cfg = _load_config(config)
    envelope = build_ops_list(cfg, limit=limit)
    emit(envelope, json_output, render_ops_list_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


OperationIdArgument = Annotated[str, typer.Argument(help="Operation ID (ULID) to inspect.")]
OpsShowJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.ops.show.v1 envelope as JSON.")]
AfterSeqOption = Annotated[
    int, typer.Option("--after-seq", help="Only include events with seq greater than this cursor.")
]


@ops_app.command("show")
def ops_show(
    operation_id: OperationIdArgument,
    config: ConfigOption = None,
    after_seq: AfterSeqOption = -1,
    json_output: OpsShowJsonOption = False,
) -> None:
    """Show one operation's record, artifact files, and event tail."""
    cfg = _load_config(config)
    envelope = build_ops_show(cfg, operation_id, after_seq=after_seq)
    emit(envelope, json_output, render_ops_show_text)
    if any(error.code in ("malformed_operation_id", "unknown_operation") for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


ApplyJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.apply.dnsmasq.v2 envelope as JSON.")]
YesOption = Annotated[bool, typer.Option("--yes", help="Execute the planned changes instead of stopping at the default plan.")]
ApplyInventoryOption = Annotated[
    Optional[Path],
    typer.Option(
        "--inventory",
        help=(
            "Override the configured ansible.inventory for this run (e.g. a freshly rendered "
            "hosts_intent.yml for bootstrap-time actuation before any production inventory "
            "exists). No silent fallback -- omit to use the configured production inventory."
        ),
    ),
]


@apply_app.command("dnsmasq")
def apply_dnsmasq(
    config: ConfigOption = None,
    yes: YesOption = False,
    json_output: ApplyJsonOption = False,
    inventory: ApplyInventoryOption = None,
) -> None:
    """Render a dnsmasq target plan, or deploy it with --yes."""
    cfg = _load_config(config)
    envelope = build_dnsmasq_apply(cfg, apply_changes=yes, inventory=inventory)
    emit(envelope, json_output, render_dnsmasq_apply_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


HostArgument = Annotated[
    Optional[str],
    typer.Argument(help="Desired-node slug to scope reconciliation to. Omit for the whole cluster."),
]
ReconcileYesOption = Annotated[
    bool, typer.Option("--yes", help="Execute the plan instead of stopping after a dry plan.")
]
MaxRoundsOption = Annotated[
    Optional[int],
    typer.Option("--max-rounds", min=1, max=10, help="Override [reconcile].max_rounds for this run."),
]
ReconcileJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.reconcile.v2 envelope as JSON.")]
RefreshObservationOption = Annotated[
    bool,
    typer.Option(
        "--refresh-observation",
        "--refresh",
        help="Force one fresh nodeutils collection/ingest for the scoped host, even if drift is converged.",
    ),
]
AllowDestroyOption = Annotated[
    bool,
    typer.Option("--allow-destroy", help="Permit planned LXC/QEMU guest destruction for this --yes reconcile run."),
]


@app.command()
def reconcile(
    host: HostArgument = None,
    config: ConfigOption = None,
    yes: ReconcileYesOption = False,
    allow_destroy: AllowDestroyOption = False,
    refresh_observation: RefreshObservationOption = False,
    max_rounds: MaxRoundsOption = None,
    json_output: ReconcileJsonOption = False,
) -> None:
    """Drift -> plan -> (with --yes) execute -> re-observe -> converge, as one bounded operation.

    Without `--yes`, builds and persists a dry plan without touching the ledger, Ansible, or
    Nautobot Jobs. With `--yes`, executes the plan's actions in dependency order across up to
    `--max-rounds` bounded re-plan rounds, regenerating the full production inventory every round.
    """
    cfg = _load_config(config)
    envelope = run_reconcile(
        cfg,
        host=host,
        apply_changes=yes,
        allow_destroy=allow_destroy,
        refresh_observation=refresh_observation,
        max_rounds=max_rounds,
    )
    emit(envelope, json_output, render_reconcile_text)
    if any(error.code in ("unknown_host", "refresh_observation_requires_host") for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


@app.command()
def prune(
    host: Annotated[str, typer.Argument(help="Exact retired DesiredNode slug to prune after completed guest removal.")],
    config: ConfigOption = None,
    yes: YesOption = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the nctl.prune.v1 envelope as JSON.")] = False,
) -> None:
    """Delete one fully-retired LXC/QEMU guest's retained Actual then Desired ledger records."""
    cfg = _load_config(config)
    envelope = run_prune(cfg, host, apply_changes=yes)
    emit(envelope, json_output, render_prune_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


LifecycleNodeArgument = Annotated[str, typer.Argument(help="Exact DesiredNode slug.")]
LifecycleStateArgument = Annotated[
    str, typer.Argument(help=f"Target lifecycle state: one of {', '.join(LIFECYCLE_STATES)}.")
]
LifecycleJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.lifecycle.v1 envelope as JSON.")]


@app.command()
def lifecycle(
    node: LifecycleNodeArgument,
    state: LifecycleStateArgument,
    config: ConfigOption = None,
    json_output: LifecycleJsonOption = False,
) -> None:
    """Set a desired node's lifecycle directly (planned/approved/active/deprecated/retired).

    A direct setter, not an approval engine and not part of `reconcile --yes`: it sends a
    one-operation batch that changes only the `lifecycle` field, confirms the write through a
    GraphQL refetch, and is idempotent (no write is sent if the node is already in the requested
    state).
    """
    cfg = _load_config(config)
    envelope = build_lifecycle(cfg, node, state)
    emit(envelope, json_output, render_lifecycle_text)
    if any(error.code in ("invalid_lifecycle", "unknown_node") for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


@desired_app.command("apply")
def desired_apply(
    file: Annotated[str, typer.Option("-f", help="Phase 0 batch document path, or - for standard input.")],
    yes: Annotated[bool, typer.Option("--yes", help="Commit instead of the default dry-run.")] = False,
    config: ConfigOption = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the raw batch artifact.")] = False,
) -> None:
    """Preview a batch document, or atomically commit it with --yes."""
    try:
        artifact = apply_document(_load_config(config), file, commit=yes)
    except DesiredWriteError as exc:
        if json_output and exc.artifact:
            import json

            typer.echo(json.dumps(exc.artifact, sort_keys=True))
        else:
            typer.echo(f"error: {exc}", err=True)
            transaction = exc.artifact.get("transaction") if isinstance(exc.artifact, dict) else None
            if isinstance(transaction, dict) and transaction.get("error"):
                typer.echo(f"transaction error: {transaction['error']}", err=True)
            operations = exc.artifact.get("operations") if isinstance(exc.artifact, dict) else None
            if isinstance(operations, list):
                for operation in operations:
                    if not isinstance(operation, dict) or operation.get("action") != "conflict":
                        continue
                    reason = operation.get("reason")
                    if reason:
                        typer.echo(
                            f"operation {operation.get('index', '?')} "
                            f"{operation.get('kind', 'unknown')}: {reason}",
                            err=True,
                        )
        raise typer.Exit(EXIT_FAILURE) from exc
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_FAILURE) from exc
    if json_output:
        import json
        typer.echo(json.dumps(artifact, sort_keys=True))
    else:
        typer.echo(f"{artifact.get('transaction', {}).get('status', 'unknown')}: {artifact.get('totals', {})}")
    raise typer.Exit(EXIT_OK)


class AuthorshipChoice(str, Enum):
    user_direct = "user_direct"
    agent_transcribed = "agent_transcribed"


BRAINDUMP_USAGE_CODES = (
    "invalid_braindump_id",
    "invalid_supersede_old_ids",
    "invalid_authorship",
    "invalid_text",
    "input_conflict",
    "input_file_error",
    "input_file_invalid_utf8",
    "braindump_not_found",
    "braindump_purge_ineligible",
    "braindump_complete_ineligible",
)


def _braindump_exit_code(envelope) -> int:
    if envelope.ok:
        return EXIT_OK
    if any(error.code in BRAINDUMP_USAGE_CODES for error in envelope.errors):
        return EXIT_USAGE
    return EXIT_FAILURE


def _confirm_destructive(prompt: str, *, yes: bool, json_output: bool) -> None:
    """Confirmation gate for `braindump review-delete`.

    `--json` is non-interactive: destructive commands require `--yes` or fail as a usage error
    before contacting Nautobot. In human mode, omitting `--yes` prompts; declining or EOF performs
    no request.
    """
    if json_output:
        if not yes:
            typer.echo("error: --json requires --yes for destructive commands", err=True)
            raise typer.Exit(EXIT_USAGE)
        return
    if yes:
        return
    try:
        confirmed = typer.confirm(prompt)
    except (typer.Abort, EOFError):
        confirmed = False
    if not confirmed:
        typer.echo("aborted: not deleted", err=True)
        raise typer.Exit(EXIT_USAGE)


BraindumpJsonOption = Annotated[
    bool, typer.Option("--json", help="Print the corresponding nctl.braindump.*.v1 envelope as JSON.")
]
BraindumpIdArgument = Annotated[str, typer.Argument(help="Braindump UUID.")]
BraindumpTitleOption = Annotated[str, typer.Option("--title", help="Braindump title.")]
BraindumpAuthorshipOption = Annotated[
    AuthorshipChoice, typer.Option("--authorship", help="user_direct or agent_transcribed.")
]
BraindumpBodyOption = Annotated[Optional[str], typer.Option("--body", help="Literal Braindump body text.")]
BraindumpFileOption = Annotated[
    Optional[Path], typer.Option("--file", help="Read the body from this UTF-8 file instead of --body.")
]
BraindumpSummaryOption = Annotated[
    Optional[str], typer.Option("--summary", help="Literal Alignment Review summary text.")
]
BraindumpSummaryFileOption = Annotated[
    Optional[Path], typer.Option("--file", help="Read the summary from this UTF-8 file instead of --summary.")
]
BraindumpYesOption = Annotated[bool, typer.Option("--yes", help="Execute the requested destructive Braindump action.")]
BraindumpReasonOption = Annotated[str, typer.Option("--reason", help="Why this active Braindump is done (recorded on the row).")]
BraindumpOldOption = Annotated[list[str], typer.Option("--old", help="Active Braindump UUID to supersede; repeat for each old document.")]
BraindumpIncludeSupersededOption = Annotated[bool, typer.Option("--include-superseded", help="Include reference-only superseded Braindumps.")]


@braindump_app.command("list")
def braindump_list(config: ConfigOption = None, include_superseded: BraindumpIncludeSupersededOption = False, json_output: BraindumpJsonOption = False) -> None:
    """List Braindumps with review presence, timestamps, and the attention hint."""
    cfg = _load_config(config)
    envelope = build_braindump_list(cfg, include_superseded=include_superseded)
    emit(envelope, json_output, render_braindump_list_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("show")
def braindump_show(
    braindump_id: BraindumpIdArgument, config: ConfigOption = None, json_output: BraindumpJsonOption = False
) -> None:
    """Show one Braindump and its current Alignment Review."""
    cfg = _load_config(config)
    envelope = build_braindump_show(cfg, braindump_id)
    emit(envelope, json_output, render_braindump_show_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("create")
def braindump_create(
    title: BraindumpTitleOption,
    authorship: BraindumpAuthorshipOption,
    config: ConfigOption = None,
    body: BraindumpBodyOption = None,
    file: BraindumpFileOption = None,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Create a Braindump from literal text (`--body`) or a UTF-8 file (`--file`)."""
    cfg = _load_config(config)
    envelope = build_braindump_create(cfg, title=title, authorship=authorship.value, body=body, body_file=file)
    emit(envelope, json_output, render_braindump_create_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("supersede")
def braindump_supersede(
    old_ids: BraindumpOldOption,
    title: BraindumpTitleOption,
    authorship: BraindumpAuthorshipOption,
    config: ConfigOption = None,
    body: BraindumpBodyOption = None,
    file: BraindumpFileOption = None,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Create an active replacement and atomically supersede exactly the selected active rows."""
    cfg = _load_config(config)
    envelope = build_braindump_supersede(
        cfg, old_ids=old_ids, title=title, authorship=authorship.value, body=body, body_file=file
    )
    emit(envelope, json_output, render_braindump_supersede_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("complete")
def braindump_complete(
    braindump_id: BraindumpIdArgument,
    reason: BraindumpReasonOption,
    config: ConfigOption = None,
    yes: BraindumpYesOption = False,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Directly transition one active Braindump to completed; no replacement row is created."""
    cfg = _load_config(config)
    _confirm_destructive(
        f"Mark Braindump {braindump_id} completed? It becomes reference-only history and eligible for purge.",
        yes=yes,
        json_output=json_output,
    )
    envelope = build_braindump_complete(cfg, braindump_id, reason=reason)
    emit(envelope, json_output, render_braindump_complete_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("review")
def braindump_review(
    braindump_id: BraindumpIdArgument,
    config: ConfigOption = None,
    summary: BraindumpSummaryOption = None,
    file: BraindumpSummaryFileOption = None,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Create or replace the current Alignment Review for a Braindump (at most one current row)."""
    cfg = _load_config(config)
    envelope = build_braindump_review(cfg, braindump_id, summary=summary, summary_file=file)
    emit(envelope, json_output, render_braindump_review_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("review-delete")
def braindump_review_delete(
    braindump_id: BraindumpIdArgument,
    config: ConfigOption = None,
    yes: BraindumpYesOption = False,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Delete only the current review, returning the Braindump to the unreviewed state."""
    cfg = _load_config(config)
    _confirm_destructive(
        f"Delete the current review for Braindump {braindump_id}? "
        "The Braindump itself will remain, but become unreviewed.",
        yes=yes,
        json_output=json_output,
    )
    envelope = build_braindump_review_delete(cfg, braindump_id)
    emit(envelope, json_output, render_braindump_review_delete_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("purge")
def braindump_purge(
    braindump_id: BraindumpIdArgument,
    config: ConfigOption = None,
    yes: BraindumpYesOption = False,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Show a purge plan, or delete one exact superseded Braindump with --yes."""
    cfg = _load_config(config)
    envelope = build_braindump_purge(cfg, braindump_id, apply=yes)
    emit(envelope, json_output, render_braindump_purge_text)
    raise typer.Exit(_braindump_exit_code(envelope))


SshHostArgument = Annotated[str, typer.Argument(help="Desired-node slug to enroll (its mDNS endpoint is used).")]
SshFromKnownHostsOption = Annotated[
    bool,
    typer.Option(
        "--from-known-hosts",
        help="Verify the offered key against an already-trusted entry for the mDNS endpoint.",
    ),
]
SshFingerprintOption = Annotated[
    Optional[list[str]],
    typer.Option(
        "--fingerprint",
        help="Accept an offered key with this exact SHA256:... fingerprint (repeatable).",
    ),
]
SshReplaceOption = Annotated[
    bool, typer.Option("--replace", help="Allow replacing an already-enrolled, different key.")
]
SshYesOption = Annotated[bool, typer.Option("--yes", help="Write the managed known_hosts entry instead of a dry plan.")]
SshJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.ssh.enroll.v1 envelope as JSON.")]

SSH_ENROLL_USAGE_CODES = ("unknown_host", "node_without_mdns")


@ssh_app.command("enroll")
def ssh_enroll(
    host: SshHostArgument,
    config: ConfigOption = None,
    from_known_hosts: SshFromKnownHostsOption = False,
    fingerprint: SshFingerprintOption = None,
    replace: SshReplaceOption = False,
    yes: SshYesOption = False,
    json_output: SshJsonOption = False,
) -> None:
    """Enroll or replace the managed SSH host key for one DesiredNode's stable alias.

    Without `--yes`, only inspects: resolves the node/endpoint/alias, scans currently offered
    keys, and reports the proposed action with no write. Requires `--from-known-hosts` and/or a
    matching `--fingerprint`; an unverified scan is never sufficient to create trust, even with
    `--yes`.
    """
    cfg = _load_config(config)
    envelope = build_ssh_enroll(
        cfg,
        host,
        from_known_hosts=from_known_hosts,
        fingerprints=fingerprint,
        replace=replace,
        apply_changes=yes,
    )
    emit(envelope, json_output, render_ssh_enroll_text)
    if any(error.code in SSH_ENROLL_USAGE_CODES for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


SessionTaskNameArgument = Annotated[
    str, typer.Argument(help="Task name (matches an agentdocs/<task_name>/ manual), e.g. 'brainforge'.")
]
SessionTopicOption = Annotated[
    Optional[str], typer.Option("--topic", help="One-word-ish topic to fold into the slug for readability.")
]
SessionJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.session.new.v1 envelope as JSON.")]

SESSION_USAGE_CODES = ("invalid_task_name",)


@session_app.command("new")
def session_new(
    task_name: SessionTaskNameArgument,
    config: ConfigOption = None,
    topic: SessionTopicOption = None,
    json_output: SessionJsonOption = False,
) -> None:
    """Create a fresh, collision-free session folder under .local/workspace/<task_name>/.

    Prints the created directory's absolute path (or the full envelope with --json). Only the
    session directory itself is created -- subfolders like sources/reviews/evidence stay the
    caller's responsibility to create lazily.
    """
    cfg = _load_config(config)
    envelope = build_session_new(cfg, task_name, topic=topic)
    emit(envelope, json_output, render_session_new_text)
    if any(error.code in SESSION_USAGE_CODES for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


WORKFLOW_EPISODE_USAGE_CODES = (
    "invalid_workflow_episode_id",
    "invalid_status_filter",
    "invalid_text",
    "input_conflict",
    "input_file_error",
    "input_file_invalid_utf8",
    "invalid_json",
    "invalid_namespace_payload",
    "workflow_episode_not_found",
    "workflow_episode_validation_failed",
    "workflow_episode_transition_ineligible",
)


def _workflow_episode_exit_code(envelope) -> int:
    if envelope.ok:
        return EXIT_OK
    if any(error.code in WORKFLOW_EPISODE_USAGE_CODES for error in envelope.errors):
        return EXIT_USAGE
    return EXIT_FAILURE


class WorkflowEpisodeStatusChoice(str, Enum):
    candidate = "candidate"
    selected = "selected"
    resolved = "resolved"
    dismissed = "dismissed"


WorkflowEpisodeJsonOption = Annotated[
    bool, typer.Option("--json", help="Print the corresponding nctl.workflow_episode.*.v1 envelope as JSON.")
]
WorkflowEpisodeIdArgument = Annotated[str, typer.Argument(help="WorkflowEpisode UUID.")]
WorkflowEpisodeStatusOption = Annotated[
    Optional[list[WorkflowEpisodeStatusChoice]],
    typer.Option("--status", help=f"Restrict to this status; repeatable. Default: {', '.join(sorted(DEFAULT_LIST_STATUSES))}."),
]
WorkflowEpisodeAllOption = Annotated[bool, typer.Option("--all", help="Include every status, overriding --status.")]


@workflow_episode_app.command("list")
def workflow_episode_list(
    config: ConfigOption = None,
    status: WorkflowEpisodeStatusOption = None,
    all_statuses: WorkflowEpisodeAllOption = False,
    json_output: WorkflowEpisodeJsonOption = False,
) -> None:
    """List workflow-improvement episodes, defaulting to candidate + selected."""
    cfg = _load_config(config)
    statuses = None if all_statuses else (frozenset(s.value for s in status) if status else DEFAULT_LIST_STATUSES)
    envelope = build_workflow_episode_list(cfg, statuses=statuses)
    emit(envelope, json_output, render_workflow_episode_list_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


@workflow_episode_app.command("show")
def workflow_episode_show(
    episode_id: WorkflowEpisodeIdArgument, config: ConfigOption = None, json_output: WorkflowEpisodeJsonOption = False
) -> None:
    """Show one workflow episode's full raw_data (report/assessment/references/resolution)."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_show(cfg, episode_id)
    emit(envelope, json_output, render_workflow_episode_show_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


class WorkflowEpisodeNamespaceChoice(str, Enum):
    report = "report"
    assessment = "assessment"
    references = "references"
    resolution = "resolution"


WorkflowEpisodeTitleOption = Annotated[str, typer.Option("--title", help="WorkflowEpisode title.")]
WorkflowEpisodeRawDataOption = Annotated[
    Optional[str], typer.Option("--raw-data", help="Literal JSON object for the whole initial raw_data document.")
]
WorkflowEpisodeFileOption = Annotated[
    Optional[Path], typer.Option("--file", help="Read the JSON object from this UTF-8 file instead of the literal option.")
]
WorkflowEpisodeNamespaceArgument = Annotated[
    WorkflowEpisodeNamespaceChoice, typer.Argument(help="raw_data namespace to replace wholesale.")
]
WorkflowEpisodeDataOption = Annotated[
    Optional[str], typer.Option("--data", help="Literal JSON object for this namespace.")
]


@workflow_episode_app.command("create")
def workflow_episode_create(
    title: WorkflowEpisodeTitleOption,
    config: ConfigOption = None,
    raw_data: WorkflowEpisodeRawDataOption = None,
    file: WorkflowEpisodeFileOption = None,
    json_output: WorkflowEpisodeJsonOption = False,
) -> None:
    """Create a workflow episode; status always starts candidate."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_create(cfg, title=title, raw_data=raw_data, raw_data_file=file)
    emit(envelope, json_output, render_workflow_episode_create_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


@workflow_episode_app.command("write")
def workflow_episode_write(
    episode_id: WorkflowEpisodeIdArgument,
    namespace: WorkflowEpisodeNamespaceArgument,
    config: ConfigOption = None,
    data: WorkflowEpisodeDataOption = None,
    file: WorkflowEpisodeFileOption = None,
    json_output: WorkflowEpisodeJsonOption = False,
) -> None:
    """Replace one raw_data namespace wholesale; the other namespaces are untouched."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_write(cfg, episode_id, namespace.value, data=data, data_file=file)
    emit(envelope, json_output, render_workflow_episode_write_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


@workflow_episode_app.command("select")
def workflow_episode_select(
    episode_id: WorkflowEpisodeIdArgument, config: ConfigOption = None, json_output: WorkflowEpisodeJsonOption = False
) -> None:
    """Transition candidate -> selected."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_select(cfg, episode_id)
    emit(envelope, json_output, render_workflow_episode_transition_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


@workflow_episode_app.command("resolve")
def workflow_episode_resolve(
    episode_id: WorkflowEpisodeIdArgument, config: ConfigOption = None, json_output: WorkflowEpisodeJsonOption = False
) -> None:
    """Transition selected -> resolved."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_resolve(cfg, episode_id)
    emit(envelope, json_output, render_workflow_episode_transition_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


@workflow_episode_app.command("dismiss")
def workflow_episode_dismiss(
    episode_id: WorkflowEpisodeIdArgument, config: ConfigOption = None, json_output: WorkflowEpisodeJsonOption = False
) -> None:
    """Transition candidate|selected -> dismissed."""
    cfg = _load_config(config)
    envelope = build_workflow_episode_dismiss(cfg, episode_id)
    emit(envelope, json_output, render_workflow_episode_transition_text)
    raise typer.Exit(_workflow_episode_exit_code(envelope))


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
