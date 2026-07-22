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

from nctl_core.braindump import (
    build_braindump_create,
    build_braindump_delete,
    build_braindump_list,
    build_braindump_review,
    build_braindump_review_delete,
    build_braindump_show,
    build_braindump_update,
    render_braindump_create_text,
    render_braindump_delete_text,
    render_braindump_list_text,
    render_braindump_review_delete_text,
    render_braindump_review_text,
    render_braindump_show_text,
    render_braindump_update_text,
)
from nctl_core.config import Config, ConfigError, ConfigInvalidError, ServeConfig
from nctl_core.dashboard_render import build_dashboard, render_dashboard_text
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
from nctl_core.ops_render import build_ops_list, build_ops_show, render_ops_list_text, render_ops_show_text
from nctl_core.output import emit
from nctl_core.production_render import (
    build_production_render,
    render_production_inventory_text,
    render_production_summary_text,
    write_production_artifacts,
)
from nctl_core.reconcile.executor import render_reconcile_text, run_reconcile
from nctl_core.status import build_status, render_status_text
from nctl_core.serve.runtime import build_serve_startup, render_serve_text, run_server
from nctl_core.ssh_enroll import build_ssh_enroll, render_ssh_enroll_text

app = typer.Typer(help="Unified CLI for pj-clusterintent reconciliation workflows.")
render_app = typer.Typer(help="Deterministic renders of desired state into consumer formats.")
apply_app = typer.Typer(help="Apply rendered desired state through deployment automation.")
ops_app = typer.Typer(help="Inspect past and running operations from the event-log directory.")
braindump_app = typer.Typer(help="Read and update the Braindump/Alignment Review exchange diary.")
ssh_app = typer.Typer(help="Manage the local, alias-keyed SSH trust store nctl uses for actuation.")
app.add_typer(render_app, name="render")
app.add_typer(apply_app, name="apply")
app.add_typer(ops_app, name="ops")
app.add_typer(braindump_app, name="braindump")
app.add_typer(ssh_app, name="ssh")


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


DashboardJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.dashboard.v1 envelope as JSON.")]
DashboardOutOption = Annotated[
    Optional[Path],
    typer.Option("--out", help="Write index.html + drift.json to this directory (default: [dashboard].out_dir)."),
]
DashboardFromOption = Annotated[
    Optional[Path],
    typer.Option("--from", help="Render a saved nctl.drift.v1 envelope instead of computing drift."),
]
NoPushOption = Annotated[
    bool,
    typer.Option("--no-push", help="Generate only; skip writing reconciliation statuses back to nintent."),
]


@app.command()
def dashboard(
    config: ConfigOption = None,
    out: DashboardOutOption = None,
    from_file: DashboardFromOption = None,
    no_push: NoPushOption = False,
    json_output: DashboardJsonOption = False,
) -> None:
    """Generate the static drift dashboard (index.html + drift.json) and push statuses to nintent.

    This is the regeneration entry point: it computes a fresh cluster-wide
    drift internally (`nctl drift` itself stays side-effect free). A failed
    drift run still writes a page that shows the errors.
    """
    cfg = _load_config(config)
    envelope = build_dashboard(cfg, out_dir=out, from_file=from_file, push=not no_push)
    emit(envelope, json_output, render_dashboard_text)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


OutOption = Annotated[
    Optional[Path],
    typer.Option("--out", help="Write the conf to this path instead of stdout (prints a summary instead)."),
]
RenderJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.render.dnsmasq.v1 envelope as JSON.")]


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


ApplyJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.apply.dnsmasq.v1 envelope as JSON.")]
YesOption = Annotated[bool, typer.Option("--yes", help="Apply changes instead of running the default check+diff dry-run.")]
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
    """Render and deploy dnsmasq configuration; dry-run with diff unless --yes is set."""
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


@app.command()
def reconcile(
    host: HostArgument = None,
    config: ConfigOption = None,
    yes: ReconcileYesOption = False,
    max_rounds: MaxRoundsOption = None,
    json_output: ReconcileJsonOption = False,
) -> None:
    """Drift -> plan -> (with --yes) execute -> re-observe -> converge, as one bounded operation.

    Without `--yes`, builds and persists a dry plan without touching the ledger, Ansible, or
    Nautobot Jobs. With `--yes`, executes the plan's actions in dependency order across up to
    `--max-rounds` bounded re-plan rounds, regenerates the full production inventory every round,
    and regenerates the dashboard from the same final drift payload it used to decide the result.
    """
    cfg = _load_config(config)
    envelope = run_reconcile(cfg, host=host, apply_changes=yes, max_rounds=max_rounds)
    emit(envelope, json_output, render_reconcile_text)
    if any(error.code in ("unknown_host",) for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
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

    A direct setter, not an approval engine and not part of `reconcile --yes`: it PATCHes only the
    `lifecycle` field, confirms the write through a GraphQL refetch, and is idempotent (no PATCH is
    sent if the node is already in the requested state).
    """
    cfg = _load_config(config)
    envelope = build_lifecycle(cfg, node, state)
    emit(envelope, json_output, render_lifecycle_text)
    if any(error.code in ("invalid_lifecycle", "unknown_node") for error in envelope.errors):
        raise typer.Exit(EXIT_USAGE)
    raise typer.Exit(EXIT_OK if envelope.ok else EXIT_FAILURE)


ServeHostOption = Annotated[Optional[str], typer.Option("--host", help="Override [serve].host for this run.")]
ServePortOption = Annotated[Optional[int], typer.Option("--port", min=1, max=65535, help="Override [serve].port.")]
ServeJsonOption = Annotated[bool, typer.Option("--json", help="Print the nctl.serve.v1 startup envelope as JSON.")]


@app.command()
def serve(
    config: ConfigOption = None,
    host: ServeHostOption = None,
    port: ServePortOption = None,
    json_output: ServeJsonOption = False,
) -> None:
    """Run the foreground HTTP subscriber API."""
    cfg = _load_config(config)
    try:
        serve_values = cfg.serve.model_dump()
        if host is not None:
            serve_values["host"] = host
        if port is not None:
            serve_values["port"] = port
        cfg = cfg.model_copy(update={"serve": ServeConfig.model_validate(serve_values)})
        # Resolve now so startup fails before claiming that the server is listening.
        if cfg.serve.auth == "token" and cfg.serve.resolve_token() is None:
            raise ConfigInvalidError(
                f"serve auth is enabled but no token was found in ${cfg.serve.token_env} or serve.token_file"
            )
    except (ConfigError, ValidationError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_USAGE)

    envelope = build_serve_startup(cfg)
    emit(envelope, json_output, render_serve_text)
    run_server(cfg)


class AuthorshipChoice(str, Enum):
    user_direct = "user_direct"
    agent_transcribed = "agent_transcribed"


BRAINDUMP_USAGE_CODES = (
    "invalid_braindump_id",
    "invalid_authorship",
    "invalid_text",
    "input_conflict",
    "no_update_fields",
    "input_file_error",
    "input_file_invalid_utf8",
    "braindump_not_found",
)


def _braindump_exit_code(envelope) -> int:
    if envelope.ok:
        return EXIT_OK
    if any(error.code in BRAINDUMP_USAGE_CODES for error in envelope.errors):
        return EXIT_USAGE
    return EXIT_FAILURE


def _confirm_destructive(prompt: str, *, yes: bool, json_output: bool) -> None:
    """Shared confirmation gate for `braindump delete`/`review-delete` (plan.md Decision 7).

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
BraindumpUpdateTitleOption = Annotated[Optional[str], typer.Option("--title", help="New title.")]
BraindumpAuthorshipOption = Annotated[
    AuthorshipChoice, typer.Option("--authorship", help="user_direct or agent_transcribed.")
]
BraindumpUpdateAuthorshipOption = Annotated[
    Optional[AuthorshipChoice], typer.Option("--authorship", help="New authorship.")
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
BraindumpYesOption = Annotated[bool, typer.Option("--yes", help="Confirm the delete non-interactively.")]


@braindump_app.command("list")
def braindump_list(config: ConfigOption = None, json_output: BraindumpJsonOption = False) -> None:
    """List Braindumps with review presence, timestamps, and the attention hint."""
    cfg = _load_config(config)
    envelope = build_braindump_list(cfg)
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


@braindump_app.command("update")
def braindump_update(
    braindump_id: BraindumpIdArgument,
    config: ConfigOption = None,
    title: BraindumpUpdateTitleOption = None,
    authorship: BraindumpUpdateAuthorshipOption = None,
    body: BraindumpBodyOption = None,
    file: BraindumpFileOption = None,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Update title, authorship, and/or body; omitted fields are preserved unchanged."""
    cfg = _load_config(config)
    envelope = build_braindump_update(
        cfg,
        braindump_id,
        title=title,
        authorship=authorship.value if authorship is not None else None,
        body=body,
        body_file=file,
    )
    emit(envelope, json_output, render_braindump_update_text)
    raise typer.Exit(_braindump_exit_code(envelope))


@braindump_app.command("delete")
def braindump_delete(
    braindump_id: BraindumpIdArgument,
    config: ConfigOption = None,
    yes: BraindumpYesOption = False,
    json_output: BraindumpJsonOption = False,
) -> None:
    """Delete a Braindump; its current review, if any, is cascade-deleted with it."""
    cfg = _load_config(config)
    _confirm_destructive(
        f"Delete Braindump {braindump_id}? Its current review, if any, will also be deleted.",
        yes=yes,
        json_output=json_output,
    )
    envelope = build_braindump_delete(cfg, braindump_id)
    emit(envelope, json_output, render_braindump_delete_text)
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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
