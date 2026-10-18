# Add a PC as a WFE execution node

A recipe for making a registered PC perform WFE workflow runs, alongside the
coordinator's own worker (project_migration p7). The contract is agdevworld
`workflow_editor/docs/workers.md`. Done once for the first PC, with cagent
`change` performing steps 3–5. Host names, addresses and paths here are
placeholders; real ones belong in desired state and ignored notes.

## What is reusable and what is per PC

| Reusable | Per PC |
| --- | --- |
| WFE's worker protocol, `wfe` on a worker, the executor (`agautolab.wfexec`) | the worker id, label and host facts |
| Deployment profile `wfe_worker`, playbook `services/setup_wfe_worker.yml`, role `wfe_worker` | the placement config: pins, token files, shares, display, Godot version |
| Profiles `nfs_server` / `nfs_client` for `agshare` | the server's client list, the PC's mount |
| `workers.json` on the coordinator | one entry and one token file |

## 1. Inspect the PC

- It must be registered and observed: `nctl observe <pc>` and `nctl drift --host <pc>`.
- Read what it is through the Ansible inventory (the managed key and host-key alias):
  - the OS;
  - a systemd user session with lingering (`loginctl show-user <user> -p Linger`);
  - Node ≥ 24 and `uv`;
  - git;
  - the desktop, if agents should draw on it (`DISPLAY`, the session's Xauthority);
  - the GPU and its current users (`nvidia-smi`);
  - free disk for run copies.
- Decide which harnesses the PC really has. The worker offers every profile in agautolab's
  `agents.toml`; a profile it cannot run fails at launch. Today only `claude_code` is installed by
  the role.

## 2. Prepare the coordinator

- Choose a worker id (`^[a-z][a-z0-9-]{0,31}$`, a logical name, not the host name).
- Write a random token into a file beside the area (for example `<area>/workers/<id>.token`, mode
  0600). List the worker in `<area>/workers.json`:
  `"<id>": {"label": "…", "tokenFile": "workers/<id>.token"}`.
- Make a Gitea token of its own for the PC, with `write:repository` for the managed projects. A file
  holds it beside the worker token; nothing prints it.
- The coordinator's WFE release and the executor commit must be pushed to GitHub: the PC fetches
  them by commit.

## 3. Shared area

- Add the PC's address to the clients of the `agshare` server placement.
- Add an `nfs_client` placement for it (server, export, `/mnt/agshare`, `noresvport`,
  `x-systemd.idle-timeout=300s`).
- The server's export changes by config only, and nctl compares NFS placements by existence: apply
  it with the server's setup playbook.
- Prove list, write, rename and hash of a probe file from the PC, then remove it.

## 4. The worker placement

Add a placement of service `agautolab` on the PC: profile `wfe_worker`, `nctl_managed`. Its config:

- `worker_id`, `label`;
- `coordinator`: the service URL (`http://<coordinator>:8098/`);
- `wfe_version`, `executor_version`: full commits;
- `token_source`, `gitea_url`, `gitea_token_source`: controller-local files;
- `shares`: `["agshare=/mnt/agshare"]`;
- `display`, `xauthority`: for a desktop;
- `godot_version`: when projects need Godot.

Then `nctl reconcile <pc> --yes`. The playbook installs:
- the pinned checkouts and their dependencies;
- Claude Code (native, pinned) and Godot;
- the worker area `~/wfe-worker`: `worker.json`, tokens, `gitea.json`, `shares.json`, the `bin/wfe`
  launcher, and executor settings with intake/notify off;
- the user unit `wfe-worker.service` (`KillMode=process`, so a restart leaves agents running).

The profile's check is the unit file, the role's last artifact. A later change of the config (a new
pin) is not compared by nctl. Render production and run
`ansible-playbook -l <pc> playbooks/services/setup_wfe_worker.yml`.

## 5. Observe it

- `GET http://<coordinator>:8098/api/workers` lists the worker `available`, with its profiles and
  host facts. That heartbeat is its health; nctl proves installation.
- On the PC: `systemctl --user is-active wfe-worker`, `~/.local/bin/claude -p …` answers (the CLI
  must be logged in), `~/wfe-worker/bin/wfe share list` says the share is `ok`.
- `nctl drift --host <pc>` is converged.

## 6. Use the ordinary request path

- Request a run on it: `wfe request <workflow> --node <id> …`, the browser's request route with
  `node`, or `execution.node` in a workflow or `project.yaml`.
- Check, in the central run view (Detail):
  - the node, its last contact and the attempt "on <id>";
  - the agent's log (shipped);
  - its outputs on the share;
  - the worker checkpoint published;
  - the integration.
- Ask something in a run and answer it: the resume is queued for the same PC.
- Run a job on the coordinator's own worker at the same time; both slots are used.
- Stop the PC's unit during an attempt:
  - the attempt stays open and the worker shows contact lost;
  - started again, the executor adopts the live process;
  - the attempt ends once.

## Known limits

- A worker offers profiles it may not be able to run.
- A run waiting on an external result (`wfe run wait`) is not woken by anything; a person's
  `wfe run resume` continues it.
- Only one worker per host; no automatic placement, relocation or failover. A run moves only by
  `wfe run assign`.
