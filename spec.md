# Aspen — HPC Slack Agent: Design & Architecture

Aspen is a Slack research assistant built for the **Structural Molecular Biology (SMB)
group at the Stanford Synchrotron Radiation Lightsource (SSRL)**, part of SLAC National
Accelerator Laboratory at Stanford University. It helps the group explore and analyze
HPC computational-chemistry results from Slack: browsing a calculations tree, plotting
and summarizing data in a sandbox, and recording per-project notes.

This document describes the system **as built**. Work that is designed but not yet
implemented (chiefly the production service account / systemd) is collected in
[§18 Roadmap](#18-roadmap--not-yet-implemented). Slurm job submission is built but runs
**under the developer's own account for a beta group**, which changes what several of its
controls are load-bearing *for* — [§19](#19-slurm-job-submission-beta). Magic numbers in
this doc are defaults; the authority is `.env` (see [§15](#15-environment-variables-env)).

---

## 1. Overall Architecture

```
Slack (Socket Mode — outbound WebSocket only)
  │
  ▼
aspen-bot.py / aspen.* package   (Slack Bolt app)
  │   └── warm Claude Agent SDK session per Slack thread (SDK retains context)
  │   └── per-user rate limits + global concurrency cap (in-memory)
  │   └── tool calls served in-process as MCP tools:
  │         list_directory · read_file · search_files · attach_file
  │         read_metadata · write_metadata · read_workflow · write_workflow
  │         submit_orca_batch · cancel_orca_batch · list_my_jobs
  │         (every path-taking tool takes an `owner` — whose root to read;
  │          the job tools deliberately do not — they act on the speaker)
  │   └── Slurm: read-only via Bash (squeue/sacct/…); submit + cancel via the
  │         structured tools above, gated on Aspen's own job ledger (§19)
  │
  ▼  run_python_analysis → HTTP POST over a Unix-domain socket + shared-secret header
FastAPI tool server  (tool_server.py, binds a Unix socket in a 0700 dir)
  │
  ▼
bwrap sandbox (bubblewrap + seccomp filter, one jail per execution)
  │
  ├── /projects/<name>/                 [read-only bind]
  └── /aspen_workspace/                 [only figures/ and cache/ are writable]
        ├── figures/        cache/       (+ generated_code/, figure_archive/, logs/, db/ on host)
```

Two long-running processes, both single-instance, plus a read-only viewer an operator
starts on demand:

- **`aspen-bot.py`** (the `aspen` package) — the Slack front-end and the agent. It runs
  the **Claude Agent SDK** against the Claude Code CLI, exposes a locked-down tool
  surface, and keeps a warm SDK session per Slack thread. The read-only browsing tools,
  the metadata tools, and the workflow tools run **in-process**; `run_python_analysis` is
  the one tool that reaches out to the tool server. Calculations are split by owner
  ([§5.1](#51-calculations-roots-aspenrootspy)) and nothing the agent can do writes inside
  any of those roots ([§7](#7-project-metadata)).
- **`tool_server.py`** — a FastAPI service bound to a **Unix-domain socket** (a file in
  a `0700` directory, not a TCP port — so other users on a shared node can't reach it)
  that executes LLM-generated analysis code inside a **bubblewrap (bwrap)** jail with a
  **seccomp** syscall filter. It owns caching, metadata parsing, the per-project SQLite
  index, figure handling, and audit logging.

- **`aspen-dashboard`** (`dashboard/`) — a Streamlit view of the turn log (§14): volume and
  tokens **per person** over time, the rate-limit and quota meters, the tool histogram and
  repeated tool sequences, the failure panel (including commands the Bash allowlist
  refused, which reads as a feature backlog), a latency breakdown separating Aspen's own
  overhead from model time from tool time, and the questions themselves. Three properties
  make it safe to have at all: it is **read-only**, it gets **its own venv** (a web
  framework has no place in the dependency tree of the running Slack service), and its bind
  address is **pinned to `127.0.0.1` in the launcher, not configurable** — Streamlit's
  default listens on every interface, which on a shared login node would publish
  colleagues' questions to every account on the machine and undo the `0600` on the log.
  Reach it over an SSH tunnel; the launcher prints the command.

Each project has its own SQLite database file for metadata and run indexing.

> **Security note.** A consolidated threat model — context, controls, deliberately
> accepted interim risks, and the checklist of security work owed at the service-account
> cutover — lives in [`THREAT_MODEL.md`](THREAT_MODEL.md).

> **Backend note.** Aspen is SDK-only. An earlier direct Anthropic Messages-API backend
> was removed; all model interaction now goes through the Claude Agent SDK / Claude Code
> CLI. By default the CLI authenticates with the Claude Code login (subscription); set
> `ASPEN_SDK_USE_SUBSCRIPTION=false` to use `ANTHROPIC_API_KEY` instead.

---

## 2. Process Model & Deployment

### Current mode — developer / dev account

Today Aspen runs under a **personal user account**, started by `start.sh` in a `screen`
or `tmux` session (not systemd). `start.sh`:

1. activates the bot virtualenv,
2. bootstraps `socat` into `~/.local/bin` if the Bash OS-sandbox is enabled (needs
   `bubblewrap` + `socat`),
3. builds the **analysis venv** once (see [§8](#8-analysis-sandbox-bwrap)) — with `uv`
   when available, falling back to `python -m venv` + `pip` — and exports
   `ANALYSIS_PYTHON`,
4. launches `tool_server.py` in the background, then `aspen-bot.py` in the foreground.

In this mode every analysis request executes under the developer's Unix identity, so the
Slack user allowlist ([§3](#3-slack-integration--socket-mode)) **must** contain only the
developer's own user ID. Production deployment under a dedicated service account is
[roadmap](#18-roadmap--not-yet-implemented).

### Single-process requirement

Rate-limit, per-user concurrency, and global-semaphore state all live in one process, so
the bot must run as a single `aspen-bot.py` instance and the tool server single-process
(`uvicorn ... --workers 1`). Multiple workers would split that state and silently break
all three guarantees.

---

## 3. Slack Integration — Socket Mode

Aspen uses **Slack Socket Mode** exclusively: it opens an outbound WebSocket to Slack —
no inbound ports, no public URL, no firewall exceptions on the cluster.

### App configuration

> **Setup runbook.** The click-by-click app creation, scopes, events, install, and
> reinstall steps — plus an importable manifest — live in
> [`SLACK_SETUP.md`](SLACK_SETUP.md) / [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
> This section is the summary.

- **Socket Mode:** enabled
- **App-Level Token** (`connections:write`) → `SLACK_APP_TOKEN`
- **Bot Token** (scopes below) → `SLACK_BOT_TOKEN`
- **Display name:** Aspen

| Scope | Purpose |
|---|---|
| `app_mentions:read` | Respond only when @Aspen is mentioned |
| `chat:write` | Post messages/results; also drives the native "Aspen is typing…" status (`assistant.threads.setStatus`) |
| `files:write` | Upload figures and attached files |
| `im:history` | Read DM threads for context |
| `channels:history` | Read channel threads Aspen is in, for context |
| `mpim:history` | Read group-DM (multi-person DM) threads Aspen is in, for context |
| `mpim:read` | List a group DM's members — used by the participant gate (below) and to classify `app_mention`s as group DMs |
| `users:read` | Resolve member IDs → display names and identify app/bot members, for the participant gate's membership check and its reply |
| `im:write` | Open a DM to the admin to relay access / calculations-root requests (optional — without it Aspen posts to the admin's user ID, which works once a DM exists) |

Aspen has no `channels:read` or unrestricted history access — it only sees conversations it
was mentioned in (or DMed in). Adding/removing scopes requires reinstalling the app.

> **Note — 1:1 human DMs can't include a bot.** Slack does not allow adding an app to an
> existing direct message between two people. To message Aspen alongside another person,
> create a **group DM** (a multi-person DM) that includes Aspen, then @-mention it there.

### Interaction model

- An `@Aspen` mention starts a conversation in channels, group DMs, or its own 1:1
  DM. Follow-ups don't always need one:
  - **1:1 DM** (`message.im`): every message is handled — no mention needed.
  - **Group DM** (`message.mpim`): a plain reply in a thread Aspen already has a live
    session for is handled too, so a back-and-forth doesn't need a mention every turn.
    A message that mentions Aspen is left to `app_mention` (so it isn't handled twice);
    a group-DM message that is *not* a reply in an Aspen thread is ignored.
  - **Channels**: `message.channels` is not subscribed, so Aspen stays silent unless
    explicitly `@`-tagged.
- **`DEMO`** in a DM starts a walkthrough for someone who is not a user yet, ahead of
  the allowlist gate ([§5.2](#52-demo-mode-aspendemopy)). The trigger is the whole
  message and nothing else, so "can you show me a demo of the thermolysin runs?" is a
  question, not a trigger.
- While working, it shows a **live progress indicator** naming the tool the agent is
  running right now — "Aspen is reading orca.out…", "Aspen is running squeue…" — via
  `assistant.threads.setStatus`. It opens as "Aspen is typing…", then each tool call the
  agent makes updates it (`SdkSession.send` reports tool uses through the
  `context["on_progress"]` hook). A daemon thread owns every Slack call, re-asserting the
  status every ~50 s (Slack expires it after ~2 min), coalescing bursts to at most one
  push per `_STATUS_MIN_INTERVAL`, and clearing it before the reply. On channel
  @-mentions, where `setStatus` doesn't apply, it falls back to posting a "_Thinking…_"
  message and editing that in place instead, deleting it once the reply is ready.
- Posts results (text + figures) as replies in the same thread.

### User allowlist

Aspen acts only on Slack user IDs marked `status: active` in the **user registry**
([§5](#5-user-registry--calculations-roots)). Anyone else gets at most one "not authorized" reply that names
the admin to contact. This check is the **first authorization gate**, before rate limiting
and before any tool runs. Until the registry file exists, the allowlist falls back to the
`ASPEN_ALLOWED_SLACK_USER_IDS` bootstrap.

**Admin.** Aspen's admin is `ASPEN_ADMIN_SLACK_USER_ID` if set, else the registry's first
`role: admin` user, else the first active user (preserving the historical "first ID in the
list" rule). They are @-mentioned in the "not authorized" and group-DM refusals so users
know who to ask to be added, and they are the only user who may edit the shared `_group`
workflow.

### Participant gate (group DMs)

In a **group DM**, the per-mentioner allowlist isn't sufficient: everyone in the room can
read Aspen's answers and every message in the thread flows into the model's context. So
before running a turn in a group DM, Aspen requires that **every human member be
allowlisted**. If any aren't, it declines and posts a message naming them and pointing to
the admin (allowlisted users can still DM Aspen directly). The check (in `_handle_event`):

1. Classify the conversation — `channel_type == "mpim"`, or `conversations.info`'s
   `is_mpim` for `app_mention`s (which omit `channel_type`). Non-group conversations skip
   the gate.
2. List members (`conversations.members`), drop Aspen itself and any **app/bot** members
   (only humans must be allowlisted), and check the rest against the allowlist.
3. **Fail closed** — if membership can't be verified (e.g. a missing scope), Aspen
   declines rather than answering in a room it can't vet.

The gate runs **after** the mentioner allowlist check and **before** rate limiting, so a
blocked group DM consumes no rate-limit slot. It applies to group DMs only; channels keep
the per-mentioner behavior (requiring every member of a public channel to be allowlisted
isn't practical, and broad channel rollout waits for the service account — [§18.1](#181-production-deployment-service-account--systemd)).

---

## 4. Tool Surface

The agent is locked down in layers. `tools=["Bash"]` is the *availability* gate: Bash is
the only built-in Claude Code tool the model is even shown, so Read/Write/Edit/Glob/Grep/
Task/WebSearch/WebFetch/Skill never enter its context — which both keeps the surface
closed and cuts the per-turn prompt from ~20.2k to ~7.2k tokens (no round trips wasted
attempting a tool that would only be denied). `strict_mcp_config=True` and `skills=[]`
keep inherited MCP servers and skill listings out too. On top of that, `allowed_tools`
auto-approves exactly the MCP tools below plus a read-only Bash allowlist, and a
`can_use_tool` backstop denies everything else. Host settings are ignored
(`setting_sources=[]`) so an operator's personal Claude permissions can't widen the bot.

| Tool | Access | What it does |
|---|---|---|
| `list_directory` | read-only | List a directory in someone's calculations (`owner` = whose) |
| `read_file` | read-only | Read a text file in someone's calculations (size-capped) |
| `search_files` | read-only | Grep file contents — one root, or `everyone=true` across all |
| `attach_file` | read-only | Upload a calculations file alongside the Slack reply |
| `read_metadata` | read-only | Open Aspen's own notes on a project (attributed, from the sidecar) |
| `write_metadata` | **write** | Record those notes — in Aspen's storage, never in a calculations tree |
| `read_workflow` | read-only | Open a user's workflow file (own, a colleague's, or `_group`) |
| `write_workflow` | **write** | Create/overwrite **only the speaking user's own** workflow |
| `request_calc_root` | request | Ask an admin to set the speaker's calculations root — grants nothing |
| `decline_setup` | preference | Record that the speaker doesn't want a workflow / a root of their own |
| `demo_request_card` | demo-only | Render the admin request into the thread (never sends) — [§5.2](#52-demo-mode-aspendemopy) |
| `demo_approve` | demo-only | Advance the demo walkthrough; grants nothing to anyone |
| `run_python_analysis` | sandboxed | Execute analysis code in the bwrap jail (via the tool server) |
| `submit_orca_batch` | **submit** | Stage structures and run the ORCA → CORVUS pipeline for the speaker — [§19](#19-slurm-job-submission-beta) |
| `cancel_orca_batch` | **cancel** | Cancel the speaker's *own* Aspen-submitted jobs, after per-ID verification — [§19.5](#195-verify-before-cancel) |
| `list_my_jobs` | read-only | The speaker's ledger rows, with live Slurm state |

Every path-taking tool takes an **`owner`** — a *name* (alias, Slack ID, or shared-root
name), never a path. Reading is flat: any root may be read by anyone ([§5.1](#51-calculations-roots-aspenrootspy)).

**No tool writes inside any calculations root.** That is now absolute, not a narrow
exception: every write surface lands in Aspen's own storage — including job staging, which
*copies* structures out of a root and never writes back into one
([§19.3](#193-staging-the-model-never-authors-a-job)).

`submit_orca_batch` / `cancel_orca_batch` are the one pair that spends a shared resource
rather than producing text, and they follow the same discipline as `write_workflow`: neither
takes an `owner`, both act on `context["user_id"]` alone, and cancellation is enumerated
from Aspen's own ledger rather than from the scheduler. Their full argument is
[§19](#19-slurm-job-submission-beta).

`write_metadata`: the target is derived entirely from (owner, project) and lands under
`METADATA_ROOT` ([§7](#7-project-metadata)). The project directory must exist, the project
name must be a single component, and the join is fenced — it is a *writable* location, so a
traversal there would be worse than one into the read-only tree. Ownership decides who may
write: your own projects and shared group ones, never a colleague's.

`write_workflow`: the target is `<workflows-root>/<alias>__<slack-id>/WORKFLOW.md` for the
**user who sent the message**. Note what the tool schema does *not* contain: an owner
parameter. The destination is derived from `context["user_id"]` — the ID Slack itself
attached to the event — so no wording in the conversation can redirect a write to someone
else's file. A model can be talked into passing any argument it is told to; it cannot pass
a value it never receives. The one exception is the shared `_group` workflow, gated on
`uid == ADMIN_USER_ID` in the same function.

`request_calc_root` and `decline_setup` are the same discipline applied to *asking*
([§5.0](#50-getting-set-up-and-asking-for-things-aspensetuppy-aspenpendingpy)): both act
on the speaker only, neither takes an owner, and neither grants anything — the first files
a request for a human to approve, the second can only make Aspen quieter.

Both writes replace the whole file, so the previous version is snapshotted first —
`$ASPEN_METADATA_HISTORY_ROOT/<alias>__<slack-id>/<project>/<UTC>.md` and
`<workspace>/workflow_history/<slack-id>/<UTC>.md` respectively — making a careless
overwrite recoverable.

The read/search tools (`list_directory`, `read_file`, `search_files`, `attach_file`) are
**path-fenced in Python** to the resolved root (`roots.resolve`): they resolve symlinks and
reject anything outside *that* root, so they cannot read host files such as `~/.ssh` or
`.env`, and cannot hop between roots. `search_files` greps file *contents* entirely
in-process (it never shells out), with the same fence per root.

### Bash

The Bash tool's default allowlist is **read-only Slurm investigation only**:
`squeue`/`sacct`/`sinfo`/`sstat`/`sprio`/`scontrol show`. General file utilities
(`cat`/`grep`/`head`/`tail`/`ls`/…) are deliberately **excluded**: with the OS sandbox
off (the default) the Bash tool runs as the bot's Unix user with no path restriction, so
those would let an allowlisted user read any file that user can (SSH keys, `.env`,
`~/.claude`). Content search is provided instead by the path-fenced `search_files` tool.
The `can_use_tool` backstop denies anything off the allowlist; job-control (`scancel`,
`sbatch`, `scontrol update`) is **never** permitted through Bash — not even now that Aspen
can submit and cancel jobs. That capability is a pair of structured tools
([§19.2](#192-the-two-tools)) precisely so the argv stays in Python: `Bash(sbatch:*)` would
hand the model `--wrap` (arbitrary code execution as the bot's Unix user on a compute node,
outside every jail) and `Bash(scancel:*)` would hand it `-u` (the entire queue, in one
word). A prefix rule cannot express "only this user's own jobs"; a Python function can.

If/when the agent is given a Bash **write/exec** surface, the Claude Code OS sandbox
("Sandbox B") should be enabled **fail-closed** to confine it — see
[`THREAT_MODEL.md`](THREAT_MODEL.md) §7. It is off today because it wraps only the Bash
tool (not the in-process tools) and the Slurm commands are excluded from it anyway, so it
would currently confine nothing.

---

## 5. User Registry & Calculations Roots

Who may talk to Aspen, what to call them, and **whose files are whose** live in a single
JSON file — `ASPEN_USERS_FILE` (default `$ASPEN_STATE_DIR/users.json`, i.e.
`~/.aspen/users.json`).

```json
{"version": 1, "users": [
  {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
   "role": "admin", "status": "active", "added": "2026-06-01", "added_by": "bootstrap",
   "calc_root": "/sdf/home/s/sam/calculations", "unix_user": "sam"},
  {"slack_user_id": "U01ARUN", "alias": "arun", "display_name": "Arun N.",
   "role": "member", "status": "active", "added": "2026-08-07",
   "added_by": "cli:sam", "notes": "beta"}
]}
```

**Resolve by ID, display by alias.** `slack_user_id` is the durable key and the *only*
thing that authorizes; `alias` is a kebab-case label for folder names and CLI arguments.
An alias can be renamed at any time without breaking a lookup, and looking a user up by
alias only ever *finds* them — admission is always `slack_user_id in ALLOWED_USER_IDS`
against the ID Slack itself put on the event. Aliases match `^[a-z0-9]+(-[a-z0-9]+)*$`
(≤32 chars), which structurally cannot produce the reserved `_group` / `_archive`
directory names.

**Hot reload.** `config.ALLOWED_USER_IDS` and `config.ADMIN_USER_ID` are no longer import-
time constants: a PEP 562 module `__getattr__` routes them through `registry`, which
re-reads the file whenever its mtime/size changes. Revoking access therefore takes effect
on the offending user's **next message**, not at the next restart. The hook only fires for
names absent from the module dict, so both existing call sites are unchanged and
`monkeypatch.setattr` in the tests still shadows them as before.

**Failure behavior — never widen.** A malformed file keeps the last good copy in memory
(access is unchanged, the error is logged). Only if nothing good was ever loaded do we
fall back to the `ASPEN_ALLOWED_SLACK_USER_IDS` bootstrap — operator-controlled, so it
cannot grant more than was already configured, and it keeps a typo from locking the admin
out. Individual malformed entries are dropped rather than failing the whole file.

### 5.0 Getting set up, and asking for things (`aspen/setup.py`, `aspen/pending.py`)

Each person can have two optional things: a **workflow** (§6) and a **calculations root**
(§5.1). The mix is the point — a PI reading across the group may want neither, a student
both, most people one before the other — so each is a three-state question: *has it*
(derived from reality), *declined* (stored), *neither* (the only case worth raising).

Only the decline is stored; recording "done" would duplicate state that can desync.

- **The agent may record a decline** (`decline_setup`), because that field can only make
  Aspen *quieter*. The worst a confused or injected agent achieves is fewer offers — a
  reduction in behavior, not an escalation. Everything that *grants* stays CLI-only.
- **The agent may not set a root.** The read surface rests on *the model passes a name,
  never a path*; a tool that wrote a path into the registry would reinstate exactly the
  surface that removes, and validation cannot save it — pointing someone at a colleague's
  tree passes every check while silently relabelling whose work is whose. Instead
  `request_calc_root` records the ask.
- **Declining a root changes resolution, not just volume.** Someone with no tree of their
  own would otherwise fall back to `CALCULATIONS_ROOT` — somebody else's work served to
  them as "your files". Once declined, an unqualified path is an error naming the
  alternatives, which is the correct model of a reader who owns nothing.
- **Nudges are rationed**: at most one item, on a thread's first turn only. The preamble
  previously carried the workflow offer in *every* turn, forever.

`pending.py` is the queue behind both asks. Being turned away at the allowlist gate **is**
an access request: it is recorded, and the admin is DM'd the exact command
(`./aspen-users add U0… --alias …`). Nothing is granted — the human still runs it — but
nobody has to work out who to ask or what to type. Notifications are de-duplicated and
rate-limited (`ASPEN_REQUEST_NOTIFY_COOLDOWN_HOURS`, default 12) so repeat messages are
one request rather than a pager, a failed DM is logged rather than raised (it must never
break the refusal the user is waiting on), and the refusal text promises only what
actually happened. `aspen-users requests` is the same queue on the terminal, and it drops
asks that reality has already answered.

### 5.2 DEMO mode (`aspen/demo.py`)

``DEMO`` in a DM runs a walkthrough for someone who is not a user: refusal, the
request card, approval, a workflow, browsing, and a plot — over fabricated data in
`examples/demo-calculations/` (regenerate with `python examples/build_demo_tree.py`).

It sits **ahead of the allowlist gate**, which is the point: it has to be usable by
someone who would be refused. That is not a hole in admission, because of three
properties, each asserted in `tests/test_demo.py` against the real tool surface:

- **Scope isolation.** The demo root is the only thing a session can read. This is the
  single place in the codebase that restricts reads *by identity* — and it restricts them
  to strictly less than a registered user sees. A visitor is not a group member, so the
  flat-read rule of §5.1 does not extend to them. Enforced in `roots.resolve` **and**
  separately in `tools._distinct_scopes`, because the cross-root sweep reads the roster
  directly and would otherwise walk around the fence every other read goes through.
- **Nothing is written.** No registry entry, not even a temporary one: the admission story
  is "no message can widen the allowlist", and a demo that writes to `users.json` would
  crack the property everything else rests on. The workflow and notes a visitor saves live
  in `demo.py`'s memory for the session.
- **Nobody is paged.** The admin request is rendered into the thread rather than sent — a
  demo anyone can trigger must not be able to page a human. `ASPEN_DEMO_REAL_ADMIN_DM=true`
  opts into the real thing.

**Bash is off in a demo.** The Slurm clients read the *real* cluster — job names carry
project names, the queue says who is running what — and a demo visitor may not even have a
cluster account. A demo thread therefore builds its own SDK session with the Bash patterns
left out of `allowed_tools`, so a `squeue` call lands on the `can_use_tool` deny instead of
being pre-approved. It is enforced by omission rather than by instruction because a
pre-approved command never reaches that callback: a prompt-level rule there would be advice,
not a control. The cost is that a demo thread cannot adopt a pre-warmed spare (those carry
the ordinary options), which is one turn of warm-start latency.

The `capabilities` beat then covers what the tour did not reach — asking for a calculations
root (rendering the second card, with the `aspen-users set-root` command), reading
colleagues' work by name, Slurm status and *why* it is off here, and attachments.

Model time is the one real cost, so demo turns are rate-limited like any other and capped
per session, per day across everyone, and by a session TTL. The walkthrough advances
through `demo.STAGES`, with per-stage guidance injected in place of the usual `<aspen_context>`
block; the agent does the talking.

### 5.1 Calculations roots (`aspen/roots.py`)

There is no single calculations directory. Each user may have their own (`calc_root`),
there may be any number of **shared** roots for group project data
(`ASPEN_SHARED_CALC_ROOTS=smb=/path,legacy=/other`), and `CALCULATIONS_ROOT` is the
fallback for anyone without one — so a deployment that sets no roots behaves exactly as
the single-root build did.

**The read boundary is flat, and it is POSIX's job.** Everyone may read every root, as on
the shared filesystem. Aspen therefore models *naming and ownership*, never permission: an
ACL layer here would be a copy of the real boundary, and a copy can only be wrong in the
direction of showing something it shouldn't. If the boundary ever stops being flat, the
enforcement is Unix groups on the bot's own account and the read simply fails.

What ownership *does* decide: which root a bare path means, the attribution prefix on every
path handed back, and who may write metadata.

**Names, not paths.** Every path-taking tool takes an `owner` — an alias, a Slack ID, or a
shared root's name — and `roots.resolve` turns it into a directory. `@alias/rest/of/path`
is the same thing spelled inline; passing both and disagreeing is an error rather than a
silent precedence rule. This is the property that makes the surface safe: a model can be
talked into passing any value it is told to, but it cannot pass a value it never receives.
The tool server enforces it independently — it reads the registry itself and accepts only
a name over the socket (§8).

**Fencing** is per root: resolve, then `relative_to`, so neither `..` nor a symlink leaves
the tree it started in. Which is why **roots may not nest** — containment *is* the fence, so
a root inside another root would silently enclose it. `aspen-users set-root` refuses that at
write time (along with unreadable paths, and any overlap with `STATE_DIR`, `WORKSPACE_ROOT`,
or a sandbox-writable path), and `main._check_calculations_roots` refuses to start if it is
ever true anyway. Readability is checked **as the bot's own Unix user** — the check that
will bite at the §18.1 cutover, which is exactly when it should.

**Discovery.** Since tools take names, the model cannot find other roots by browsing; the
per-turn `<aspen_context>` block names them (`roots.preamble_lines`).

**Search scope.** `search_files` takes `everyone=true` for a cross-root sweep, with its own
budget (`ASPEN_SEARCH_MAX_FILES_ALL`) because N roots multiply the work the per-call caps
were sized for. Roots are de-duplicated (users sharing the fallback are one directory), and
a sweep that runs out of budget **says which roots it did not reach** rather than reading as
complete.

**Writes are CLI-only.** `aspen-users` (`python -m aspen.users_cli`) is the only thing
that writes the registry: `init`, `list`, `add`, `rename`, `remove`, `sync`, `whois`,
`set-root`, `roots`, `workflow`. The
first write migrates the bootstrap IDs into the registry — announced, with names resolved
from Slack and the first ID kept as admin — so nobody silently gains or loses access. There is
deliberately **no Slack command and no agent tool** that changes admission, so no message
— however phrased — can widen the allowlist. See the README for the full argument
reference.

---

## 6. Per-User Workflows

Each user can keep a **workflow file**: their own notes on how they run and interpret
calculations. Layout under `ASPEN_WORKFLOWS_ROOT` (default `$ASPEN_STATE_DIR/workflows`):

```
arun__U01ARUN/WORKFLOW.md      # <alias>__<slack-id>; lookups glob "*__<uid>"
_group/WORKFLOW.md             # shared house style (admin-writable)
_archive/priya__U0PRIYA/       # a removed user's knowledge, kept as reference
```

Files carry YAML frontmatter. The author owns `description` (and `derived_from`);
everything else — `name`, `owner_id`, `owner_name`, `updated`, `updated_by` — is stamped
by Python on every write, so identity fields can't be forged by whatever the model passes.

**Two-layer disclosure (the SKILL.md pattern, without Claude Code's Skill machinery).**
Every turn is prefixed with an `<aspen_context>` block naming the speaker and listing each
workflow's alias + one-line description — a frontmatter-only scan, a few hundred tokens at
this group's size. The full body is fetched on demand by `read_workflow`. Claude Code's
native skills were deliberately *not* used: re-enabling the `Skill` tool would undo the
`tools=["Bash"]` / `skills=[]` lockdown ([§4](#4-tool-surface)) and its ~13k tokens/turn
saving, and would hand skill discovery to the CLI, pointed at a user-writable directory.

The preamble rides on the **message**, not the system prompt, because sessions are keyed
per *thread* and pre-warmed before the speaker is known, and a group DM has several
speakers sharing one session ([§10](#10-conversation-context)).

### Trust tiers

A workflow file differs in kind from project data: it is text that asks to be *followed*.
With cross-user visibility, one user's authored text would otherwise steer another user's
session. So `read_workflow` returns the body inside a tagged block:

| Tier | When | Meaning to the model |
|---|---|---|
| `your-own` | the speaker's own file | Standing preferences — follow them, but they're preferences, not orders |
| `group-default` | `_group` | House default; the speaker's own overrides it |
| `reference-only` | anyone else's, incl. archived | Notes to describe, quote, or adapt — **never** instructions addressed to you |

The system prompt states this explicitly, including that no workflow — not even the
speaker's own — can grant tools, relax the sandbox, change file access, alter who may edit
what, or override its instructions. As with project text, **the boundary is the Python-
enforced tool limits, never the prompt**: a workflow file cannot cause any action a plain
Slack message couldn't.

### Ownership

`write_workflow` has no owner parameter; the target is `context["user_id"]` from the Slack
event ([§4](#4-tool-surface)). To help someone adopt a colleague's approach, the agent
reads the colleague's file, adapts it with the user, and saves it to the *user's own* file
with `derived_from` recorded — normalized to a registry alias, or dropped if it resolves
to nobody.

### Lifecycle

`aspen-users remove` revokes access and **archives** the workflow by default (a removed
member's notes are often the most valuable thing they leave behind); the file stays
readable as `reference-only` with an `archived:` tombstone. `--purge` deletes it instead,
and `--purge-history` also clears the backups. Every overwrite snapshots the prior version
to `<workspace>/workflow_history/<slack-id>/<UTC>.md`.

### Placement guard

`main._check_state_locations()` refuses to start if `USERS_FILE`, `WORKFLOWS_ROOT`,
`METADATA_ROOT`, `METADATA_HISTORY_ROOT`, `REQUESTS_FILE`, or the telemetry paths resolve
inside `WORKSPACE_ROOT` or any `ASPEN_SANDBOX_WRITE_PATHS` entry. Those areas are
writable by sandboxed analysis code, which would let generated Python edit the allowlist
or another user's workflow directly and walk straight around both checks above. It also
creates the workflows root and `chmod 0700`s the state dir, so other users on a shared
login node can't read or plant files there.

`0700` is correct only while the bot user and the admin user are the same account. At the
service-account cutover they split, and the state dir moves to group space with a
read/write split — `0750` for the registry (writes need the service identity, so admission
stays privileged) and `2770` for the workflows tree (users may edit their own file). That
also needs `ensure_private_dir()` to take a configurable mode. Details and the traps are in
[`THREAT_MODEL.md`](THREAT_MODEL.md) §7.

---

## 7. Project Metadata

Aspen's own notes about a project — status, conventions, and the advisory list of Python
libraries for analysing it. They live **outside the calculations tree**, in a sidecar that
mirrors it (`aspen/metadata.py`):

```
$ASPEN_METADATA_ROOT/<alias>__<slack-id>/<project>/metadata.md    # a user's root
$ASPEN_METADATA_ROOT/_shared__<name>/<project>/metadata.md        # a shared root
$ASPEN_METADATA_HISTORY_ROOT/<same>/<project>/<UTC>.md            # every overwrite
```

**Why out of the tree.** `write_metadata` used to write `metadata.md` into the project
directory, which worked only while the tree belonged to the account the bot runs as. With a
root per user (§5.1) it would be a write into someone else's directory, and under the §18.1
service account it stops being possible at all. Moving it makes the invariant absolute:
**the agent writes nothing, anywhere, inside any calculations root.**

**Why `STATE_DIR` and not `WORKSPACE_ROOT`.** The workspace is sandbox-writable, and
metadata is read back into the model's context on later turns — metadata stored there would
be a slow-loop injection path, where generated analysis code edits a note that steers a
future session. The line: **`WORKSPACE_ROOT` is what the sandbox produces; `STATE_DIR` is
what steers the agent.** It also inherits the `_check_state_locations` guard, which now
covers both metadata paths.

**The path is the key.** Mirroring derives the location by arithmetic, so there is no
mapping to keep in sync — the same trick as `workflows.dir_for`, and the reason to prefer it
over an index file (a second source of truth that can desync), flat encoded filenames
(`a/b` and `a-b` collide), or frontmatter naming its own target (a full scan per lookup, and
the *model's* declared target becomes authoritative). User directories are `alias__slack-id`
and are found by ID, so a rename cannot orphan anyone's notes.

**History is keyed by owner as well as project.** It was keyed by project alone, which
silently collided the moment two people had a `thermolysin`.

**Ownership decides writes.** Your own projects and shared group projects are yours to
annotate; someone else's are readable by everyone and writable only by them. Annotating a
colleague's work is a different feature (per-viewer notes) and folding it in here would fork
metadata into per-reader copies. The project directory must exist — that check doubles as
validation, so metadata can never describe a project that does not.

**It is not served through `read_file`.** Metadata is Aspen-authored; project files are the
user's. One read path for both would leave the agent unable to tell its own past notes from
ground truth, so `read_metadata` returns it wrapped and attributed, exactly as a workflow is
(§6). A directory listing mentions that metadata exists, since it is no longer visible as a
file in the tree.

The tool server reads the sidecar for the library advisory, resolved from the caller's
scope; an in-tree `README` / `metadata.md` / `.toml` / `.yaml` is a **read-only fallback** so
projects keep working before their notes are migrated.

**Notes are not required.** If nothing exists, `run_python_analysis` proceeds on the default
advisory (`numpy, pandas, matplotlib, scipy`). It used to return 422 with a template reading
"create `metadata.md` in the project root" — an instruction that stopped being possible the
moment metadata left the tree, and which blocked analysis of every project nobody had
documented yet. A file that exists but is *malformed* still 422s: that is a broken file its
owner should hear about, which is not the same as no file.

### 7.1 Encouraging a description

A project with nothing describing it gets an **offer, on the listing where it is noticed** —
`list_directory` on the top level of one of the speaker's own projects (`metadata.nudge_text`
via `setup.project_notes_nudge`). Aspen drafts a README *from the directory it just read* —
real run names, real filenames — and asks the user to save it themselves; it cannot write it,
since every root is read-only to it.

**Why a README in the tree, and not the sidecar.** The two documents have different authors.
The sidecar is Aspen's inference, returned tagged as "not evidence" (above); a README is the
scientist's own ground truth, and it belongs with the data — versioned with it, visible to
colleagues, backups, and every tool that is not Aspen. Collapsing them would re-create exactly
the confusion the sidecar exists to prevent. Anyone who would rather not keep a file is
offered `write_metadata` instead: same content, no file, no markdown.

**No markdown needed.** Only the library heading is parsed (a heading containing `librar`);
the rest is read as prose. `examples/README.example.md` is the annotated example.

**Rationed like every other nudge** (§6): the speaker's own projects only, never one that any
source already describes, and once per project per process. `decline_setup(item="project_notes")`
silences it for good — the ration itself is in-memory, because it is a politeness counter and
not a grant, and a per-project row does not belong in the registry.

**Project text is untrusted input.** Metadata, README/blurb text, file names, and file
contents all flow into the model's context and could attempt prompt injection
("ignore previous instructions…"). The security boundary is therefore the **bwrap jail
and the Python-enforced tool restrictions**, never the prompt. Project-derived text can
never relax sandbox restrictions, choose mounts, or alter the import advisory.

---

## 8. Analysis Sandbox (bwrap)

`run_python_analysis` runs LLM-generated Python inside a **bubblewrap** jail. (bwrap
replaced Apptainer: rootless Apptainer `--memory` requires cgroups v2, and the target
host is cgroups v1, so container creation failed; bwrap needs no cgroups.)

### Flow

1. The model produces Python code as a string.
2. The tool server runs a **static AST check** rejecting `exec()` / `eval()`, then
   prepends a defense-in-depth import hook, and writes the script to
   `<workspace>/generated_code/<uuid4>.py`.
3. The script runs in the jail; the generated file is deleted afterward (always).

### Jail configuration (the OS boundary is the real enforcement)

The command is `prlimit … -- bwrap … <ANALYSIS_PYTHON> /aspen_script.py`, with:

- **No network** — `--unshare-all` unshares the network namespace (replaces Apptainer's
  `--net --network none`).
- **Read-only filesystem, minimal allow-list** — `--ro-bind` of `/usr`, `/lib*`, `/bin`,
  `/sbin`, the dynamic-loader config, `/etc/fonts`, and the analysis interpreter's venv +
  base-CPython prefixes; the project at `/projects/<name>` read-only; `--proc`, `--dev`,
  and a `--tmpfs /tmp`. `/home`, other users, other projects, and `/etc` secrets are not
  mounted, so they're invisible to the code.
- **Only outputs writable** — `--bind` of `figures/` and `cache/` (the sole writable host
  paths). The matplotlib cache lives under `cache/mpl` (a hook-permitted, persistent
  location).
- **Scrubbed environment** — bwrap 0.4.0 has no `--clearenv`, so the subprocess is handed
  only a minimal `SANDBOX_ENV` (no secrets); `MPLBACKEND=Agg`, small BLAS/OpenMP thread
  pools, and `MPLCONFIGDIR` pointed at the writable cache.
- **Hardening** — `--die-with-parent`, `--new-session`.
- **seccomp syscall filter** — `--seccomp` loads a compiled BPF denylist (built once at
  startup via `pyseccomp`, applied per run). It blocks the obscure, never-needed syscalls
  that are the usual road to kernel privilege-escalation — namespace/mount-escape,
  kernel keyrings, `ptrace`, module load, `bpf`, `io_uring`, `userfaultfd`,
  `perf_event_open`, `clone3`, … — while leaving everything numeric Python needs. It is
  the one lever on the kernel→root path on this pinned 4.18 kernel. Default-ALLOW with a
  denylist (a strict allowlist is too brittle for arbitrary numeric code); toggle with
  `ANALYSIS_SECCOMP`. If `pyseccomp` is unavailable the jail still runs (logged), as the
  bind-mount/namespace boundary is unaffected. Only the BPF is *exported* — never
  `load()`ed — so the tool server process itself is not filtered.
- **Resource caps via `prlimit`** — bwrap has no cgroup limits, so per-task caps use
  `RLIMIT_AS` (virtual memory, default 2 GB — generous, since BLAS over-reserves),
  `RLIMIT_CPU` (defaults to the timeout), and `RLIMIT_FSIZE`. The **wall-clock timeout**
  (`EXECUTION_TIMEOUT_SECONDS`, default 120 s; SIGKILL on expiry) is the primary backstop.

### In-process protections (defense-in-depth, not the boundary)

The injected import hook restricts `open()` writes to `figures/`/`cache/`, and the AST
check blocks `exec`/`eval`. These are hints/UX, not isolation — in-process CPython
sandboxing is bypassable in general, so the implementation assumes generated code can
import or call anything present in the jail. That's why the jail is minimal and the
writable set is enforced by bind mounts. The per-project library list is **advisory**
(clean errors + code steering), not a security boundary.

### Analysis environment

The analysis libraries (numpy, pandas, matplotlib, scipy, py3Dmol) live in a dedicated
**venv** — not the bot's venv — listed in `analysis-requirements.txt` and built by
`start.sh` (via `uv`, falling back to `python -m venv`). `ANALYSIS_PYTHON` points the tool
server at it; the interpreter's prefixes are discovered at runtime and bound read-only
(handles uv's split `base_prefix`/`base_exec_prefix` layout). To change the library set,
edit `analysis-requirements.txt` and rebuild the venv.

---

## 9. Output & Figure Handling

- **stdout** truncated to 10,000 chars, **stderr** to 2,000, before returning to the
  model / Slack; `truncated: true` triggers a note in the reply.
- **Figures:** 5 MB per-PNG upload cap. Oversized figures are flagged so the model can
  regenerate at lower dpi/size; if still too big, a text-only reply is posted. The system
  prompt instructs default `dpi=200`, retry at `dpi=72` + halved dimensions on failure.
- **Archiving:** uploaded PNGs move from `figures/` to `figure_archive/`; when the archive
  exceeds 2 GB, oldest files are trimmed to 1.5 GB at the start of each request (no cron).

---

## 10. Conversation Context

Context is held by the **Claude Agent SDK's warm session**, one per Slack thread
(`thread_ts`), parked between turns and reused — the SDK retains conversation state
natively, so the bot does not maintain its own messages array. Sessions are bounded by
`MAX_OPEN_SESSIONS` and expire after `CONTEXT_EXPIRY_SECONDS` (default 4 h).

Connecting a session spawns the Claude Code CLI and waits on its init handshake (~1.7 s),
which would otherwise land inside the first message of every new thread. The manager keeps
`ASPEN_PREWARM_SESSIONS` already-connected spares on standby (default 1, counted against
`MAX_OPEN_SESSIONS`, 0 to disable) and tops the pool back up in the background whenever one
is adopted. Separately, `main.py` imports `claude_agent_sdk` on a boot thread — the lazy
import in `agent.py` costs ~4 s (mostly `mcp`/`pydantic`, worse on a network filesystem)
and would otherwise be paid by the first user after a restart.

Each turn is
capped at `AGENT_MAX_ROUNDS` agentic tool-call rounds (default 25); hitting it ends the
turn with `error_max_turns`, and Aspen reports a soft pause ("reply *continue*…") while
keeping the thread's context — it is **not** a hard error.

---

## 11. Rate Limiting & Concurrency

Enforced in `aspen-bot.py` before any tool runs, per Slack user ID, in-memory:

| Limit | Default |
|---|---|
| Requests / user / 10-min window | `RATE_LIMIT_REQUESTS` = 5 |
| Concurrent executions / user | 1 |
| Concurrent executions, global | `MAX_CONCURRENT_EXECUTIONS` = 5 |

Over-limit users get an immediate in-thread message; a busy global cap yields a
"busy right now" reply. State resets on restart (no persistent store).

---

## 12. Per-Project Database — SQLite

Each project uses one SQLite file at `<workspace>/db/<project>.sqlite` (no Postgres
dependency). The tool server is the sole writer; the jail has no access to db files.

**Placement & journal mode.** WAL's `-shm` mmap is unreliable on parallel filesystems.
Prefer node-local disk (`SQLITE_DB_ROOT`) with WAL; on the group data path, use the
default rollback journal (no WAL) and serialize writes. Always set `PRAGMA busy_timeout`
(e.g. 5000 ms) in the connection helper. Schema: a `runs` table (path, status, tags,
energy, structure, last_update) and a `datasets` table, with indexes on status/tags.

---

## 13. Caching

Cache key = `SHA-256(question + sorted(dataset_ids) + max(file_mtimes))`, so new data in a
run directory invalidates automatically. Entries are stored at
`cache/<project>/<hash>.json` with stdout/stderr/figure paths; a hit skips execution and
re-uploads the archived figure. A hit verifies each referenced figure still exists (the
archive trimmer may have removed it) and re-executes if any is missing. No time-based
expiry.

---

## 14. Logging, Auditing & Secret Redaction

The tool server writes structured JSON logs to `<workspace>/logs/<project>/<date>.jsonl`
after each execution (timestamp, user, thread, project, question, dataset, generated code,
figures, status, errors, duration, cache_hit).

- **Redaction:** stdout/stderr pass through a filter that replaces any line matching
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`/`ANTHROPIC_API_KEY`/`AGENT_INTERNAL_SECRET` or
  `xoxb-`/`xapp-`/`sk-ant-` with `[REDACTED BY ASPEN LOG FILTER]`, before truncation and
  before disk/return. Because `SANDBOX_ENV` carries no secrets, this is a backstop, not
  the primary control.
- Logs never contain Slack tokens, the internal secret, API keys, or HPC credentials.

---

## 15. Environment Variables (`.env`)

See `.env.example` for the full annotated list. Key groups:

```bash
# Slack / auth
SLACK_BOT_TOKEN=xoxb-...      SLACK_APP_TOKEN=xapp-...
ASPEN_ALLOWED_SLACK_USER_IDS=U0XXXXXXXXX        # BOOTSTRAP only — the registry (§5) rules
ASPEN_ADMIN_SLACK_USER_ID=                       # optional; default = registry role:admin
ANTHROPIC_API_KEY=sk-ant-...                     # only if ASPEN_SDK_USE_SUBSCRIPTION=false
ASPEN_SDK_USE_SUBSCRIPTION=true                  # default: use the Claude Code login
ANTHROPIC_MODEL=claude-opus-4-8
AGENT_INTERNAL_SECRET=<32-byte hex>              # shared secret, bot ↔ tool server

# Paths
CALCULATIONS_ROOT=/.../calculations      # browsing tools + write_metadata
PROJECTS_ROOT=/.../calculations          # analysis (read-only mount)
WORKSPACE_ROOT=/.../aspen_workspace      # figures, cache, logs, db, generated_code, metadata_history
SQLITE_DB_ROOT=/tmp/aspen_db
ASPEN_TOOL_SERVER_SOCKET=                 # default $WORKSPACE_ROOT/run/tool.sock (Unix socket, 0700 dir)

# Users + workflows (§5, §6). MUST be outside WORKSPACE_ROOT and the sandbox's
# writable paths — startup refuses otherwise.
ASPEN_STATE_DIR=                          # default ~/.aspen (0700)
ASPEN_USERS_FILE=                         # default $ASPEN_STATE_DIR/users.json
ASPEN_WORKFLOWS_ROOT=                     # default $ASPEN_STATE_DIR/workflows
ASPEN_MAX_WORKFLOW_BYTES=60000

# Bash allowlist (default: Slurm read-only only — no general file readers; see §4)
# ASPEN_BASH_ALLOWLIST=

# Slurm job submission (§19). OFF by default — a deployment that hasn't thought
# about compute budgets shouldn't gain job submission just by upgrading.
ASPEN_JOBS_SUBMIT_ENABLED=false
ASPEN_JOBS_LEDGER=                        # default $ASPEN_STATE_DIR/jobs.sqlite
ASPEN_JOBS_STAGING_ROOT=                  # default $ASPEN_STATE_DIR/jobs-staging
                                          # MUST be outside WORKSPACE_ROOT + sandbox
                                          # write paths — startup refuses otherwise
ASPEN_JOBS_PIPELINE_BIN=xas-run-batch     # the pipeline entry point (on PATH)
ASPEN_JOBS_SCHEDULER=slurm
ASPEN_JOBS_MAX_STRUCTURES=24              # per submission
ASPEN_JOBS_MAX_ACTIVE_PER_USER=48         # concurrent non-terminal jobs
ASPEN_JOBS_MAX_ACTIVE_TOTAL=200
ASPEN_JOBS_MAX_SUBMITS_PER_DAY=10         # per user
ASPEN_JOBS_CONFIRM_TTL_SECONDS=900        # dry-run → confirm token lifetime
ASPEN_JOBS_SUBMIT_TIMEOUT_SECONDS=600     # cap on the orchestrator subprocess
# Extra env names to pass through to the orchestrator subprocess (and thus to
# jobs). The base allowlist is PATH/HOME/USER/LANG/TERM/TMPDIR + PIPELINE_*/XAS_*;
# secrets are never included, whatever is listed here (§19.6).
# ASPEN_JOBS_ENV_PASSTHROUGH=
# ASPEN_JOBS_SBATCH_EXPORT=               # default: the same allowlist; 'NONE' to
                                          # tighten (disables pipeline auto-rerun)

# Analysis sandbox (bwrap)
ANALYSIS_PYTHON=                          # default $WORKSPACE_ROOT/analysis-venv/bin/python
# ANALYSIS_VENV, BWRAP_BIN, ANALYSIS_RO_PATHS — optional, sane defaults
ANALYSIS_SECCOMP=true                     # seccomp syscall denylist on the jail; false to disable
ANALYSIS_AS_LIMIT_BYTES=2147483648        # RLIMIT_AS (memory); 0 disables
ANALYSIS_FSIZE_LIMIT_BYTES=536870912
# ANALYSIS_CPU_LIMIT_SECONDS — defaults to EXECUTION_TIMEOUT_SECONDS
# ASPEN_SEARCH_MAX_FILES / _MATCHES / _FILE_BYTES — search_files caps (sane defaults)

# Tuning
AGENT_MAX_ROUNDS=25            MAX_OPEN_SESSIONS=20
RATE_LIMIT_REQUESTS=5          RATE_LIMIT_WINDOW_SECONDS=600
MAX_CONCURRENT_EXECUTIONS=5    CONTEXT_EXPIRY_SECONDS=14400
EXECUTION_TIMEOUT_SECONDS=120
MAX_STDOUT_CHARS=10000         MAX_STDERR_CHARS=2000
MAX_FIGURE_BYTES=5242880       FIGURE_ARCHIVE_MAX_BYTES=2147483648   FIGURE_ARCHIVE_TRIM_BYTES=1610612736
```

Paths and identity-specific values are driven entirely from `.env` (no hardcoded paths, no
`getpass.getuser()`/`Path.home()`), so the port to a service account is cheap.

---

## 16. Security Summary

| Layer | Protection |
|---|---|
| Authorization | Slack user-ID allowlist — first gate, before rate limiting. In group DMs, a **participant gate** additionally requires *every* human member to be allowlisted (fail-closed) |
| Slack connection | Socket Mode — outbound WebSocket only, no open ports |
| Tool surface | Locked-down allowlist + `can_use_tool` deny; host settings ignored; Bash = Slurm read-only (`sbatch`/`scancel` are structured tools, never Bash rules) |
| Slurm submission | Off by default. Model supplies a `template_mode` from a fixed enum, never a script; inputs copied into a staging tree outside every writable area; orchestrator env scrubbed to an allowlist so no token reaches a compute node; per-user and global job caps; dry run + single-use confirmation token ([§19](#19-slurm-job-submission-beta)) |
| Slurm cancellation | Enumerated **only** from Aspen's own ledger, filtered to the Slack event's `user_id`, then each ID verified against live Slurm (`WorkDir` inside *that* requester's staging dir) before an explicit-ID `scancel`. No filter flags, ever ([§19.5](#195-verify-before-cancel)) |
| Read/search tools | Path-fenced in Python to the *resolved* root; can't read `~/.ssh`, `.env`, or hop between roots |
| Calculations roots | One per user + shared; reads are flat by design, and the tool surface takes a **name**, never a path. Roots may not nest (startup refuses) |
| Write surface | **Nothing inside any calculations root.** Metadata and workflows land in `$ASPEN_STATE_DIR`; the jail gets `figures/`,`cache/`. Prior versions snapshotted |
| Granting | Admission and calculations roots are CLI-only. The agent can *request* (DM to the admin) and *decline*, never grant |
| DEMO | Runs ahead of the allowlist gate for non-users, so the boundary is **scope**: the demo root only, enforced in two places; writes nothing; renders the admin request instead of sending it; capped per session and per day |
| Tool server | Binds a **Unix socket in a `0700` dir** (not a TCP port); shared secret on every request |
| Analysis jail | bwrap: no network; read-only minimal FS; project read-only; scrubbed env; **seccomp syscall denylist**; `prlimit` caps; 120 s timeout. The jail + bind mounts + seccomp are the boundary |
| Stdout/stderr | Redacted then truncated (10k/2k) before leaving the tool server |
| Figures | 5 MB upload cap; archived, trimmed at 2 GB |
| Rate limiting | 5 req / 10 min / user; 1 concurrent / user; global cap |
| Logging | Structured JSON; secrets redacted |
| Secrets | In `.env` (chmod 600); never logged or sent to Slack/model |

A consolidated threat model (assets, actors, accepted interim risks, and the
service-account cutover checklist) is in [`THREAT_MODEL.md`](THREAT_MODEL.md).

**Hard constraints — Aspen can never:** act for a non-allowlisted user; engage in a group
DM that contains any non-allowlisted human (participant gate, fail-closed); write *any*
file inside *any* calculations root; grant itself or anyone else access or a calculations
root; write another user's workflow or metadata; **cancel a Slurm job it did not submit, or
one submitted for a different Slack user** ([§19.5](#195-verify-before-cancel));
**author the body of a job it submits** ([§19.3](#193-staging-the-model-never-authors-a-job));
run `sbatch`/`scancel` through Bash; reach files outside the configured roots / workspace /
state dir / staging tree; make network calls from inside the analysis jail.

---

## 17. Tests

A hermetic pytest suite runs without a live Slack connection, Claude CLI, or network
(`pytest -q` from the repo root). Highlights:

- **`tests/test_tools.py`** — read-only browsing tools, `write_metadata` (path safety,
  existing-project-only, and that it adds nothing to the calculations tree), and the
  tool-server bridge (mocked HTTP).
- **`tests/test_roots.py`, `test_multiuser_tools.py`, `test_tool_server_roots.py`** —
  per-user roots: name-not-path resolution, the `@alias/` grammar, the per-root fence,
  nesting refusal, cross-root search with an honest truncation notice, and the tool
  server's independent registry lookup (a path sent as `owner` resolves to nothing).
- **`tests/test_demo.py`** — the demo boundary, asserted against the real tool
  surface: a visitor cannot reach a real root by name, by `@alias/` path, by
  traversal or by a cross-root sweep; nothing is written (no registry entry, no
  workflow file, no sidecar, no queued request); nothing is sent to the admin.
  Plus the walkthrough over the fabricated ORCA output, including that the scan
  really does have its minimum where the demo says it does.
- **`tests/test_startup.py`** — the boot guards: state refused inside any
  sandbox-writable area, and roots refused when nested, missing or unreadable.
- **`tests/test_setup.py`, `test_pending.py`** — the three-state setup model, one-nudge-per-
  thread rationing, that a declined root turns unqualified paths into an error rather than
  someone else's tree, and that requests are recorded and DM'd but never granted.
- **`tests/test_tool_server_bwrap.py`** — the bwrap command builder: prlimit caps, network
  unshared, project read-only, only `figures/`/`cache/` writable, interpreter binds,
  scrubbed env, hook-writable `MPLCONFIGDIR`, `/etc/fonts` bound.
- **`tests/test_sdk_backend.py`** — SDK option lockdown (allowed tools, ignored host
  settings), warm-session reuse, and turn-end reporting (success, real errors, and the
  `error_max_turns` soft-pause path).
- **`tests/test_sessions.py`, `test_ratelimit.py`, `test_admission.py`,
  `test_render.py`, `test_attachments.py`** — session lifecycle, rate limits, the Slack
  admission/typing-status path, Slack-markdown rendering, and the attachment sink.

- **`tests/test_jobs.py`, `test_jobs_cancel.py`, `test_staging.py`** — the Slurm surface
  ([§19](#19-slurm-job-submission-beta)), which is where the contract tests are densest
  because it is the one capability that spends a shared resource. The properties asserted
  against the real tool surface: a cancel tool that grows an `owner` parameter fails the
  suite; one user cannot cancel another's job by ID, by batch, by project name, or by any
  wording; a job whose `WorkDir` lies outside the requester's staging tree is refused even
  when its ledger row says otherwise (the recycled-ID case); `scancel` argv never contains
  a filter flag (`-u`/`-n`/`-A`/`-p`/`-q`/`-t`/`--me`); an unparseable or erroring
  `scontrol` fails closed; the orchestrator env contains no secret; `template_mode` off the
  enum is refused; no tool accepts a script path or script body; the ledger write precedes
  submission and a failed write aborts it; caps and the confirmation token are enforced in
  Python rather than by prompt.

`tests/conftest.py` provides a facade mapping the legacy flat names onto the `aspen.*`
package and neutralizes import-time side effects.

---

## 18. Roadmap / Not Yet Implemented

These are designed or intended but **not** in the current build.

### 18.1 Production deployment (service account + systemd)

Move from the dev-account model to a dedicated **`aspen-agent` service account** managed
by **systemd**, required before opening Aspen to users beyond the developer. Outline:

- Service account with no login shell / no SSH keys; secrets in `/opt/aspen-agent/.env`
  (chmod 600).
- Read-only access to the group projects path; read-write to the workspace (created with
  a shared group + setgid so dev-created files stay manageable after the cutover).
- A `systemd` unit (`Type=simple`, `User=aspen-agent`, `EnvironmentFile`, `Restart`,
  hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
  `ReadWritePaths=<workspace>`).
- Regenerate `AGENT_INTERNAL_SECRET` at cutover (Slack/model tokens carry over).
- Pre-deployment checks on the real node: `bubblewrap`/`socat` present and the OS sandbox
  enforcing (`./verify_sandbox.sh` from a non-nested shell), outbound HTTPS to Slack and
  the model endpoint, the analysis venv building, and the SQLite placement/journal choice
  validated on the actual filesystem.

### 18.2 Agent-submitted Slurm jobs — built in beta form

Submission and cancellation are **implemented** and running under the developer's own
account for a small beta group. The as-built design, and the ways it differs from the
sketch that used to live here, are in [§19](#19-slurm-job-submission-beta).

What remains roadmap is the part this section always said it depended on: the
**service-account split** ([§18.1](#181-production-deployment-service-account--systemd)).
Until it lands, a submitted job runs as the developer's Unix user with no sandbox on the
compute node, so it can read `$ASPEN_STATE_DIR` and the repo `.env`. That risk is now
*accepted and recorded* ([THREAT_MODEL](THREAT_MODEL.md) §8) rather than blocking, on the
grounds that the beta registry is a handful of trusted colleagues and the model never
authors the job body ([§19.3](#193-staging-the-model-never-authors-a-job)). The
consequences for the *cancel* boundary are the subject of
[§19.1](#191-why-the-ledger-and-the-workdir-tag-are-load-bearing) — they are the reason
this feature could be built early at all.

The design principles below were written before the build and are **unchanged and still
binding**. Where the implementation had to depart from the mechanism this section
originally proposed, the reason is recorded in §19 and cross-referenced here.

- **No agent-written code touches the pipeline** — it may only invoke the pipeline's own
  entry point with a validated, fixed `template_mode` allowlist and path-validated
  structure/output dirs; it never composes shell commands.
  ([§19.3](#193-staging-the-model-never-authors-a-job); the entry point is
  `xas-run-batch`, not the `submit-batch.py` this section used to name — the pipeline was
  reorganised into the installable `xas_pipeline` package.)
- **Every submission is fully logged before `sbatch`** (command, args, user, job IDs,
  timestamp); if logging fails, submission aborts.
  ([§19.4](#194-the-ledger))
- **`--no-submit` dry run first** to validate inputs before any real submission.
  ([§19.7](#197-caps-dry-runs-and-the-kill-switch))
- **Cancellation is scoped strictly to job IDs in the agent's own ledger** — the agent
  never lists scheduler jobs and cancels from that. Cancelling the CORVUS job kills the
  dependent postprocess job via the dependency chain.
  ([§19.5](#195-verify-before-cancel))
- **Copy, then edit, then submit.** Nothing in a user's calculations root is modified;
  inputs are copied (never symlinked — a symlink lets a writer reach back through it) into
  a staging tree, with provenance. ([§19.3](#193-staging-the-model-never-authors-a-job))
- **`sbatch` is never a Bash allowlist entry.** A `Bash(sbatch:*)` prefix rule would hand
  the model `--wrap`, i.e. arbitrary code execution as the bot user on a compute node,
  outside every jail. ([§19.2](#192-the-two-tools))
- **Scrub the job environment.** `load_dotenv` puts `SLACK_BOT_TOKEN` and
  `AGENT_INTERNAL_SECRET` into the bot's `os.environ`, which a naive `sbatch` inherits into
  the job. ([§19.6](#196-environment-scrubbing) — implemented by scrubbing the
  *orchestrator subprocess*, not by `--export=NONE`, for a reason worth reading)
- **Accounting is an admin question, not a code one.** Jobs charge the submitting account's
  Slurm association. During the beta that is the developer's, via the pipeline templates'
  `--account=ssrl:SMBXAS`.
- **Results stay in the agent's workspace** and are reported/attached from there. Writing
  back into a user's tree is out of scope; if it is ever wanted, the mechanism is an opt-in
  per-user inbox directory whose existence *is* the consent.

Two mechanisms this section proposed did **not** survive contact with the cluster, and the
replacements are load-bearing rather than cosmetic:

- **`--comment aspen/v1/<slack-id>/<thread-ts>` cannot be set by Aspen.** The pipeline runs
  `sbatch` itself, and **there is no `SBATCH_COMMENT` environment variable** (verified
  against the s3df `sbatch` man page, 2026-08) — `--comment` is settable only on the
  command line or in a script's `#SBATCH` header, neither of which Aspen owns.
  `--wckey` is not an alternative either: s3df reports `TrackWCKey = no`, so it is
  dropped. The tag Aspen uses instead is **`WorkDir`**, which Slurm records itself —
  see [§19.1](#191-why-the-ledger-and-the-workdir-tag-are-load-bearing). The
  `AccountingStoreFlags = job_comment` finding still holds and still matters: it is what
  makes the comment worth plumbing through the pipeline as a *second* check later
  ([§19.9](#199-what-the-beta-does-not-fix)).
- **Two endpoints on the tool server** is not where this went. `tool_server.py` is
  deliberately standalone — it never imports the `aspen` package and keeps its own
  registry reader. Putting the cancel-ownership check there would mean a **second
  implementation** of it, which is the precise shape of both scope escapes recorded in
  [THREAT_MODEL](THREAT_MODEL.md) §3. The Slurm surface therefore lives in the bot
  process (`aspen/jobs.py`), which is also where the Slurm read clients already run.

### 18.3 Shipped since this list was written

Per-user calculations roots and the metadata sidecar are **built** — see
[§5.1](#51-calculations-roots-aspenrootspy) and [§7](#7-project-metadata). Staged job
submission on a user's behalf is **built in beta form** —
see [§19](#19-slurm-job-submission-beta). What is still outstanding is
[§18.1](#181-production-deployment-service-account--systemd) itself.


### 18.4 Other deferred items

- LLM-assisted metadata/indexing suggestions.
- Persistent conversation history across restarts.
- Migration from SQLite to PostgreSQL.
- Automatic analysis-venv rebuild when a project's library list changes (currently a
  manual edit of `analysis-requirements.txt` + rebuild).
- Async/background figure-archive trimming (synchronous per-request is sufficient now).
- Automated shared-secret rotation (manual today).
- Multi-process scaling (would require moving rate-limit/concurrency state out of process).

---

## 19. Slurm Job Submission (beta)

Aspen can submit and cancel ORCA → CORVUS pipeline batches on a user's behalf
(`aspen/jobs.py`, `aspen/staging.py`). This is the one capability that **spends a shared
resource** and the one whose mistakes are visible to the whole group, so it is built with
more enforcement per line than anything else in the system.

It runs during the beta under the **developer's own Unix account**, ahead of the
service-account split ([§18.1](#181-production-deployment-service-account--systemd)). That
ordering was chosen deliberately — the alternative was shipping it for the first time
directly into a systemd service with no room to iterate — and it changes the *role* of
several controls rather than merely weakening them. §19.1 is that argument; it is the
section to read before changing anything here.

### 19.1 Why the ledger and the `WorkDir` tag are load-bearing

Under a dedicated `aspen-agent` account, Unix and Slurm ownership are a **free outer
layer**: the service account simply cannot `scancel` a job belonging to anyone else, and
every Aspen-side check is defense in depth on top of that.

Running as the developer removes that layer **and inverts it**. Every job the developer has
ever submitted by hand — months of real research jobs — is inside `scancel` range of the
bot's own credentials. So the Aspen-side checks stop being redundancy and become the only
thing standing between a confused or injected model and someone's queue.

The substitute for Unix ownership is a **tag Slurm maintains itself**, checked immediately
before every cancel:

| Layer | What it stops | Status during beta |
|---|---|---|
| Slurm/POSIX ownership | Cancelling another *person's* hand-submitted jobs | **Absent** — the bot is the developer |
| Ledger membership: the job ID must be a row in Aspen's own `jobs` table | Cancelling anything Aspen did not submit — including a hallucinated ID, or one a user typed in chat | Enforced ([§19.4](#194-the-ledger)) |
| Ledger *ownership*: the row's `slack_user_id` must equal the Slack event's `user_id` | **Cancelling another Slack user's Aspen job.** Slurm cannot distinguish these — they are all one Unix user | Enforced ([§19.5](#195-verify-before-cancel)) |
| `WorkDir` verification against live Slurm state | A stale ledger row pointing at a **recycled** job ID | Enforced ([§19.5](#195-verify-before-cancel)) |

**Why `WorkDir` and not `--comment`.** The original design ([§18.2](#182-agent-submitted-slurm-jobs--built-in-beta-form))
keyed this on `--comment aspen/v1/<slack-id>/<thread-ts>`. Aspen cannot set it: the
pipeline invokes `sbatch` itself, and Slurm offers no `SBATCH_COMMENT` environment variable
to inject one through (`--wckey` is dropped too — `TrackWCKey = no`). What Slurm *does*
record without being asked is the submission directory, and the pipeline's job scripts run
with `--chdir=.` from their staging run directory. So `WorkDir` is:

- **Set by Slurm, not by Aspen** — it cannot be forged by conversation text, which is the
  same property the `--comment` design was reaching for (C9).
- **Per-user by construction** — staging is
  `$ASPEN_JOBS_STAGING_ROOT/<alias>__<slack-id>/<thread-ts>/…`, so "is this job's `WorkDir`
  inside *this requester's* staging directory" reuses the path fence Aspen already
  enforces everywhere else, rather than introducing a second notion of ownership.
- **Structurally absent from hand-submitted jobs** — a job the developer launched from
  their own tree has a `WorkDir` outside the staging root, so it is not merely *disallowed*
  from being cancelled, it fails the check by construction. This is what restores the
  protection that Unix ownership would otherwise provide.

Job **IDs are reused** — s3df reports `MaxJobId = 67043328`, and the counter also resets on
a controller rebuild — which is why this check is against live Slurm state at cancel time
rather than trusting the ledger alone.

### 19.2 The two tools

| Tool | Access | What it does |
|---|---|---|
| `submit_orca_batch` | **submit** | Stage a structure set and run the ORCA → CORVUS pipeline for the speaker |
| `cancel_orca_batch` | **cancel** | Cancel the speaker's own Aspen-submitted jobs, after verification |
| `list_my_jobs` | read-only | The speaker's ledger rows, with live Slurm state |

Neither write tool takes an **`owner`** parameter, and that omission is the control, not an
oversight. The requester is `context["user_id"]` — the ID Slack itself attached to the
event — exactly as with `write_workflow` (C9). A model can be argued into passing any
argument it is handed; it cannot pass a value it never receives. There is deliberately no
way to submit or cancel *on behalf of* someone else, not even for the admin through the
agent: the admin's escape hatch is the CLI ([§19.8](#198-the-cli-escape-hatch)), outside
the model's reach, like every other granting action in the system.

`sbatch` and `scancel` are **never** added to the Bash allowlist ([§4](#4-tool-surface)),
which stays read-only. `Bash(sbatch:*)` would hand the model `--wrap` — arbitrary code
execution as the bot's Unix user on a compute node, outside every jail — and
`Bash(scancel:*)` would hand it `-u`, which is the whole queue in one word.

### 19.3 Staging: the model never authors a job

`submit_orca_batch` takes a `template_mode` from a **fixed enum** (`ca-fixed`, `h-only`,
`single-point`, `free`, `backbone`, `xtb-free`, `xtb-constrained`, `quick`,
`quick-ca-fixed` — mirroring the pipeline's mutually-exclusive mode flags), a structure
path resolved through `roots.resolve` like every other path-taking tool, and nothing else
that reaches a command line.

It takes **no script path and no script content.** This is the load-bearing difference from
the §18.2 sketch, which said only that a script path must resolve inside a staging
directory — necessary but not sufficient, because a tool that accepts a path the model
chose is one writable staging directory away from "the model wrote the job body". Instead
the pipeline renders its own job scripts from its own packaged templates, and Aspen's
contribution is a validated mode name. What lands on a compute node is therefore *pipeline
code plus the user's `.xyz` data* — never model output.

Inputs are **copied** into `$ASPEN_JOBS_STAGING_ROOT/<alias>__<slack-id>/<thread-ts>/`,
never symlinked, alongside a `provenance.json` recording source paths, SHA-256 of each
copied structure, the requesting Slack ID, and the resolved mode. Nothing in any
calculations root is written — the invariant from [§7](#7-project-metadata) is unchanged by
this feature.

The staging root must sit **outside** `WORKSPACE_ROOT` and outside every
`ASPEN_SANDBOX_WRITE_PATHS` entry, enforced at startup by the same guard that protects the
registry ([§6 placement guard](#6-per-user-workflows)). If staging were writable by
sandboxed analysis code, generated Python could plant a script and Aspen would submit it —
the cross-tool path that makes an otherwise-fenced feature exploitable.

### 19.4 The ledger

One SQLite file, `$ASPEN_STATE_DIR/jobs.sqlite`, written only by the bot:

- **`batches`** — one row per `submit_orca_batch`: `batch_id`, `slack_user_id`, `alias`,
  `thread_ts`, `project`, `template_mode`, staging path, `submitted_at`, and the argv
  actually run.
- **`jobs`** — one row per scheduler job: `job_id`, `batch_id`, `kind` (`orca` / `corvus` /
  `postprocess`), `job_name`, `work_dir`, `submitted_at`, plus the reconciler's columns
  (`state`, `elapsed`, `total_cpu`, `alloc_tres`, `exit_code`, `reconciled_at`).

Submit rows are **immutable**; only the reconciler's columns are ever updated
([§19.9](#199-what-the-beta-does-not-fix)). The write happens **before** the pipeline is
invoked and a failure to write **aborts the submission** — the §18.2 requirement, kept
literally, because a job running with no ledger row is a job nobody can cancel through
Aspen and nobody can attribute.

**Why `STATE_DIR` and not `<workspace>/db/`.** The per-project SQLite databases of
[§12](#12-per-project-database--sqlite) live under `WORKSPACE_ROOT`, and putting this one
beside them would be the obvious move. It would also be wrong: this ledger is an
**authorization input** — it decides who may cancel what — and the rule from
[§7](#7-project-metadata) applies unchanged, *`WORKSPACE_ROOT` is what the sandbox
produces; `STATE_DIR` is what steers the agent*. A ledger the sandbox could write is a
ledger the agent can forge a row into, and a forged row is a cancel it should not have.

### 19.5 Verify before cancel

`cancel_orca_batch` resolves what it may touch through **one** function,
`jobs.resolve_cancellable(event_user_id, selector)` — the single chokepoint, deliberately
not duplicated into the tool server ([§18.2](#182-agent-submitted-slurm-jobs--built-in-beta-form)):

1. **Enumerate from the ledger**, never from the scheduler: rows whose `slack_user_id`
   equals the Slack event's `user_id` and whose state is not terminal. A `selector` may
   narrow this (a batch, a project, a job ID) but can never widen it — an ID that is not
   already in the requester's own rows resolves to nothing, whatever the conversation says.
2. **Verify each ID against live Slurm** — `scontrol show job <id> -o`, requiring
   `UserId` to be the account Aspen runs as, `WorkDir` to resolve inside *that requester's*
   staging directory, and (when present) `Comment` to match. Fail **closed**: a
   `scontrol` error, a missing field, or a `WorkDir` outside the fence drops that ID and
   says so. It never falls through to cancelling.
3. **Cancel explicit IDs only** — `scancel <id> <id> …`, argv built in Python.

`scancel` **filter flags are never used.** `-u`, `-n/--name`, `-A`, `-p`, `-q`, `-t`,
`--me` all delegate enumeration to Slurm, which contradicts step 1 and makes step 2
impossible — you cannot verify the `WorkDir` of a job you never enumerated. A contract test
fails if any of them ever appears in a built argv. "Cancel all of my jobs" needs none of
them: `scancel` accepts many IDs at once, so the ledger supplies the set and each one is
still verified individually.

**Job names are for humans, not for authorization.** Jobs carry the pipeline's own
`--job-name` (`orca-<basename>`, `corvus-<id>-xas`, …) so the group can read `squeue`; a
future pipeline change can prefix them with `aspen-<alias>`. Names are never an
authorization key: aliases are renameable, names are not unique, and a name is something a
human can type by hand.

### 19.6 Environment scrubbing

`config.py` calls `load_dotenv()` at import, so `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and
`AGENT_INTERNAL_SECRET` are in the bot process's `os.environ`. `sbatch` defaults to
`--export=ALL`, which snapshots the submitting environment into the job — so without care
every compute node would receive Aspen's Slack tokens.

The control is to **scrub the environment of the orchestrator subprocess**, so
`--export=ALL` has nothing secret to copy. `jobs.submit_env()` builds an explicit
allowlist (`PATH`, `HOME`, `USER`, `LANG`, `TERM`, `TMPDIR`, plus `PIPELINE_*` / `XAS_*`
site variables) rather than a denylist, for the reason the seccomp filter goes the other
way: a denylist of secret names silently fails to cover the next secret added to `.env`. A
contract test asserts no allowlisted name matches the secret patterns.

**`--export=NONE` is deliberately *not* used**, and this is a real trade rather than
laziness. The pipeline's ORCA job script triages its own failures by calling
`xas-rerun-orca`, which it finds on `PATH` "inherited via SLURM `--export=ALL` from the
submitting venv" — and the call is guarded by `command -v`, so under `--export=NONE` the
auto-rerun feature becomes a **silent no-op**: no error, no log line, just runs that quietly
stop retrying. Scrubbing the parent environment achieves the security goal without breaking
a feature whose failure mode is invisible. `SBATCH_EXPORT` is set to the same explicit
allowlist as a second layer, and `ASPEN_JOBS_SBATCH_EXPORT` can tighten it to `NONE` for a
deployment that does not want auto-rerun.

### 19.7 Caps, dry runs, and the kill switch

Compute is the asset that rises sharply with this feature
([THREAT_MODEL](THREAT_MODEL.md) §2), and the primary threat actor is the careless
allowlisted member, so the caps are Python-enforced rather than advisory:

| Limit | Default | Env |
|---|---|---|
| Structures per submission | 24 | `ASPEN_JOBS_MAX_STRUCTURES` |
| Concurrent Aspen jobs, per user | 48 | `ASPEN_JOBS_MAX_ACTIVE_PER_USER` |
| Concurrent Aspen jobs, global | 200 | `ASPEN_JOBS_MAX_ACTIVE_TOTAL` |
| Submissions / user / day | 10 | `ASPEN_JOBS_MAX_SUBMITS_PER_DAY` |
| Feature enabled at all | **off** | `ASPEN_JOBS_SUBMIT_ENABLED` |

The feature is **off by default**: a deployment that has not thought about compute
budgets does not get job submission because it upgraded.

**Dry run by default.** `submit_orca_batch` runs the pipeline with `--no-submit` first and
reports what *would* be submitted; committing requires a second call carrying a
confirmation token. The token is held in-process, keyed by `(thread_ts, user_id)` with a
short TTL, and is single-use.

**The confirmation is a Python control, not a prompt instruction.** Cancels work the same
way: a preview call returns the job list and a token, and only a redeeming call cancels.
Asking the model in the system prompt to "confirm first" would be advice — the pattern
here is the same one `pending.py` uses, and it survives a model that has been talked into
skipping the question.

### 19.8 The CLI escape hatch

`aspen-users jobs` — `list`, `show`, `cancel`, `reconcile`, and `panic` (cancel every
non-terminal ledger job) — is the operator's path in, outside the agent and outside Slack.
It exists because the beta's failure mode is a runaway batch at 2 a.m., and debugging that
through a Slack thread is the wrong tool. Like every other granting/administrative action
([§5.1](#51-calculations-roots-aspenrootspy)), it is CLI-only and has no agent-facing
equivalent.

### 19.9 What the beta does not fix

Recorded here rather than left implicit, because the point of the beta is to iterate with
the risks visible:

- **A submitted job runs unjailed as the developer** and can read `$ASPEN_STATE_DIR`, the
  repo `.env`, and `~/.ssh`. There is no fix under a single account; what bounds it is that
  the job body is pipeline code ([§19.3](#193-staging-the-model-never-authors-a-job)) and
  that the beta registry is a handful of trusted colleagues. Resolved by
  [§18.1](#181-production-deployment-service-account--systemd).
- **Jobs charge the developer's Slurm association** (`ssrl:SMBXAS` via the pipeline
  templates), so per-user compute accounting is a ledger fact, not a cluster fact.
- **`--comment` is not yet plumbed through the pipeline.** Adding `--comment` (and a
  `--job-name-prefix`) to `xas-run-batch` would give a second independent tag beside
  `WorkDir`, recoverable from `sacct -o Comment` even if the ledger is lost — worth doing,
  since `AccountingStoreFlags = job_comment` is confirmed on s3df. Deliberately **not** a
  generic `--sbatch-extra-args` pass-through, which would be `--wrap` by another name.
- **The reconciler is manual** (`aspen-users jobs reconcile`), not a cron job.
  Attribution is two-phase and the second phase is the one that answers "who used the
  compute": at submit time you know *who and what*, but elapsed time, CPU-hours and exit
  state exist only after a job ends, so a design that logged only at submission would yield
  job counts and nothing about consumption. The join is:

  ```
  sacct -X -n -P -u <bot-user> -S <since> \
    -o JobID,JobName,Comment,State,Submit,Start,End,Elapsed,TotalCPU,AllocTRES,ExitCode
  ```

  `-X` limits to job allocations (no `.batch`/`.extern` step rows, which would
  double-count), `-P` is parseable, `-n` drops the header. The two copies fail differently
  and that is the point: the ledger can be corrupted or deleted and Slurm's copy survives
  it; slurmdbd purges on a site-set schedule and the ledger survives that. Neither alone is
  a durable record of who used the allocation.
