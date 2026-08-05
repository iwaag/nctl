# `ops`, `upload`, `braindump`

See [`../README.md`](../README.md#usage) for the full command list.

## `ops list` / `ops show`

`nctl ops list [--limit N] [--json]` and `nctl ops show OPERATION_ID [--after-seq N] [--json]` are
a read-only, filesystem-only view over `[events].log_dir` — no live process, Nautobot, or Ansible
access required. `ops list` enumerates every `<operation_id>.jsonl` file, newest first, parsing
just enough of each to report `op`/`state`/`ok`/`result`/timestamps (schema `nctl.ops.list.v1`).
`ops show` additionally returns the full event list (or only events with `seq > --after-seq`) plus
the resolved `artifact_dir` and its artifact list, using the same corrupt-line-tolerant JSONL
reader (schema `nctl.ops.show.v1`; a truncated or partially written final line is reported via
`corrupt_lines`, not raised as an error). `nctl_core.operations_index` is a retained CLI-only
helper: the JSONL event log and operation-artifact directories are durable disk evidence read by
this module and the CLI alone, not published to or consumed by any external subscriber.

## `upload`

`nctl upload PATH [PATH...] [--zip] [--ttl DURATION] [--json]` puts the given file(s) in the
`[storage]` MinIO bucket and prints one time-limited presigned download URL (schema
`nctl.upload.v1`: url, expiry, object key, byte size). A single regular file uploads as-is;
multiple paths, any directory, or an explicit `--zip` bundle everything into one zip, so one
invocation always yields exactly one URL. Object keys are prefixed with a timestamp and short
random suffix (`2026-08-05/143012-a1b2c3/state.json`) so repeated uploads never collide. `--ttl`
takes integer minutes or `30m`/`2h`, bounded to 7 days; default is `storage.default_ttl_minutes`.

There is deliberately no state-specific export command: write state with the existing readers,
then upload the file —

```bash
uv run nctl drift --json > /tmp/state.json && uv run nctl upload /tmp/state.json
```

The `[storage]` section (endpoint, bucket, access key, secret file/env) is optional and only
`upload` needs it; see `example.nctl.toml`. The endpoint host is signed into the URL, so configure
the name recipients actually reach (a LAN name, not `localhost`, when downloads happen on another
machine). The bucket is private; the presigned URL is the only read path, and there is no deletion
or lifecycle handling yet — objects accumulate until removed by hand.

## `braindump`

`nctl braindump {list,show,create,supersede,complete,review,review-delete,purge}` is the deterministic,
typed interface to the exchange diary described in `devdocs/big/braindump/roadmap.md`: a
**Braindump** is the user's free-form wish, and its at-most-one current **Alignment Review** is the
AI agent's latest natural-language reply. Neither is executable input, and this command surface has
no import path into `drift`, `reconcile`, Jobs, nodeutils, or Ansible — reading or
writing the diary never changes convergence status or triggers actuation.

- `list [--include-superseded] [--json]` / `show ID [--json]` read through GraphQL only and never write. `list` returns only active documents by default; `--include-superseded` explicitly includes reference-only history (both `superseded` and `completed` documents). It returns a
  compact `id`/`title`/`authorship`/timestamps/review-presence/attention projection; `show` returns
  the full record including `body` and, if present, the review's `summary`.
- `create --title TITLE --authorship AUTHOR (--body TEXT | --file PATH)` writes through REST
  and always confirm the result via a fresh GraphQL refetch before reporting success; a mismatch is
  a command-scoped `*_confirmation_mismatch` failure, never a fabricated success. `AUTHOR` is
  exactly `user_direct` or `agent_transcribed` — there is no default, so provenance is never
  misstated.
- `supersede --old OLD_ID [--old OLD_ID ...] --title TITLE --authorship AUTHOR (--body TEXT | --file PATH)` creates an active replacement and marks exactly the selected active old documents `superseded`; any validation failure leaves all old rows active and retains no replacement.
- `complete ID --reason TEXT [--yes]` is the other status transition: it moves exactly one `active`
  document straight to `completed`, in place, without creating a replacement row. Use it when a wish
  is resolved (a node retired, a one-off task finished) and there is nothing to supersede it with —
  `supersede` remains the only way to record that one wish replaced another. `--reason` is required
  and stored on the row as `completion_reason`, the same non-negotiable audit trail `supersede`'s
  replacement text provides. A non-`active` target is rejected (409, `braindump_complete_ineligible`)
  and left unchanged. Like `review-delete`, it is destructive-gated: without `--yes` it prompts in
  human mode and fails as a usage error in `--json` mode.
- `--file PATH` reads the file as `Path.read_text(encoding="utf-8", errors="strict")` — the exact
  bytes are stored, with no trailing-newline stripping, line-ending normalization, BOM removal,
  Markdown rendering, variable interpolation, or shell/prompt interpretation. Prefer `--file` over
  `--body` for multiline or shell-sensitive prose, and never embed secrets in either — command-line
  arguments and stored Braindump text both end up in process lists, reports, and Git history.
- `review ID (--summary TEXT | --file PATH)` creates the review when none exists and replaces the
  one current row when it does — it never appends a second row. Replacement always advances
  `last_updated`, even when the new summary text is byte-identical to the old one, because invoking
  `review` records a new evaluation. A rare create/create race (two writers, no existing review) is
  recovered automatically by refetching once and replacing the row the other writer created; any
  other rejection is a genuine validation failure and is reported as such.
- Ordinary Braindump deletion is unavailable. `review-delete ID [--yes]` deletes only the review, returning the Braindump to the unreviewed
  state; deleting an already-unreviewed Braindump's review is an idempotent no-op
  (`deleted: false`), not an error. Without `--yes`, `review-delete` prompts for the exact target
  UUID in human mode; its `--json` mode is non-interactive and requires `--yes` or fails as a usage
  error (exit 2) before contacting Nautobot. `--yes` never broadens the target — there is no bulk,
  title-based, or wildcard delete.
- `purge ID [--yes]` is the narrow exception for a document already marked `superseded` or
  `completed`. Without `--yes` it obtains and prints a read-only server-side plan for that exact
  UUID, including whether its Alignment Review will cascade. With `--yes` it re-checks the UUID and
  status and deletes the document and its one-to-one review in one transaction. An active document
  is rejected; a repeated purge is the successful `already_purged` no-op. Purge never affects
  Desired, Actual, drift, reconcile, operation evidence, or infrastructure.
- Attention is a non-persisted, three-state hint computed only from the two diary timestamps:
  `unreviewed` (no review row), `needs_attention` (the review is older than its Braindump), or
  `review_present` (a review exists and is not older than its Braindump). `review_present` does
  **not** mean aligned, valid, or converged — it says only that a current review row exists.
  Braindump/review timestamps are never compared against desired/actual freshness here; run `nctl
  drift --json` separately and read its evidence before writing a grounded review.

A safe external-agent interaction over this surface: read `braindump list --json` and the relevant
`show --json` calls, read `nctl drift --json` separately for desired/actual evidence, ask the user
about ambiguity or any proposed structured change, write only the user's confirmed words to a
Braindump, publish the agent's own prose with `braindump review`, and use the established
desired-state/`reconcile` commands separately — only after the user has actually granted that
authority, never inferred from Braindump/review text alone.

Each command emits its own frozen `nctl.braindump.<command>.v1` envelope (see
[`output-format.md`](output-format.md)); human output renders **User-originated
Braindump** and **AI Alignment Review** as visually separate sections so AI-derived text is never
mistaken for the user's own words.
