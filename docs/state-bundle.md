# State bundle (`nctl.bundle.v1`): the cluster's state as one downloadable file

A **state bundle** is the well-defined answer to "stateをファイルにまとめて
出力して": one zip containing the existing versioned per-command envelopes,
one file per view, plus a small manifest. The bundle is a container — it adds
no new state semantics. Desired state is the one view with a round-trip file
format (`nctl desired export`, the canonical batch document `desired apply
-f` accepts); actual state is observed, not declared, so its `--json`
envelope already is its file representation.

Bundling is a documented composition, not an `nctl bundle` command: the
composer runs the four read commands, writes `manifest.json` from their
envelope headers, zips, and uploads. (If chronic manifest mistakes make this
painful, that is the Easier Next Time signal to promote it to a command —
record the pain, don't improvise a second format.)

## Canonical layout

```
cluster-state-<UTC timestamp>.zip
├── manifest.json    # nctl.bundle.v1 (this convention)
├── desired.yaml     # nctl desired export   (raw canonical batch document)
├── drift.json       # nctl drift --json     (nctl.drift.v1 envelope)
├── actual.json      # nctl actual --json    (nctl.actual.v1 envelope)
└── relations.json   # nctl relations --json (nctl.relations.v1 envelope)
```

## `manifest.json` fields

| field | meaning |
|---|---|
| `schema` | literally `nctl.bundle.v1` |
| `generated_at` | UTC ISO-8601 time the manifest was written |
| `nctl_git_sha` | `git -C nctl rev-parse HEAD` of the nctl that produced the views |
| `contents` | one entry per payload file: `path` (relative name inside the zip), `schema` (the envelope schema inside that file, or `nctl.desired.export.v1` for `desired.yaml`), `generated_at` (that file's own timestamp) |

Per-file `generated_at` comes from each JSON file's own envelope header. The
raw `desired.yaml` document deliberately carries no timestamp (it is
byte-deterministic for unchanged state), so its entry records the moment the
export command ran. The views are fetched sequentially in one sitting — close
but not one atomic snapshot; the per-file `generated_at` values are the
honest record of that skew. Do not build snapshot pinning around this.

## Recipe

From the superproject root:

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DIR=.local/tmp/cluster-state-$STAMP   # dir basename becomes the zip name; keep it
mkdir -p "$DIR"                       # inside the repo (.local/ is git-ignored) so
                                      # workdir-scoped agent file tools can read it

uv run --project nctl nctl desired export > "$DIR/desired.yaml"   # exit 0 required
uv run --project nctl nctl drift --json     > "$DIR/drift.json"
uv run --project nctl nctl actual --json    > "$DIR/actual.json"
uv run --project nctl nctl relations --json > "$DIR/relations.json"

# Write $DIR/manifest.json yourself: schema/generated_at/nctl_git_sha as
# above, and one contents entry per file — schema and generated_at for the
# three .json files read from their own envelope headers ("schema",
# "generated_at"); desired.yaml uses schema nctl.desired.export.v1 and the
# export run time.

uv run --project nctl nctl upload "$DIR" --zip --ttl 2h
```

`nctl upload` prints one presigned download URL; quote it and its expiry
exactly. Rules:

- `nctl desired export` failing (non-zero exit, named errors instead of a
  document) is a **stop**: fix or report the named issue; never bundle a
  partial or improvised desired-state file.
- Any view's envelope with `"ok": false` should be reported to the
  requester, not silently bundled as if healthy (bundling it *with* that
  caveat is fine — the envelope itself records the errors).
- Every command above is read-only; composing a bundle never mutates cluster
  or desired state.

## Verifying a bundle

Unzip and check: `manifest.json` parses, `schema` is `nctl.bundle.v1`, and
every `contents` entry names an existing file whose inner `schema` matches
(for the JSON views, compare with the file's own envelope header;
`desired.yaml` must parse as the batch envelope — top-level keys exactly
`dry_run` and `operations`). The definitive desired-state check remains
`nctl desired apply -f desired.yaml` previewing all-unchanged against the
same database state.
