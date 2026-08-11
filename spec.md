# Aspen — HPC Slack Agent: Design & Architecture

Aspen is a Slack research assistant built for the **Structural Molecular Biology (SMB)
group at the Stanford Synchrotron Radiation Lightsource (SSRL)**, part of SLAC National
Accelerator Laboratory at Stanford University. It helps the group explore and analyze
HPC computational-chemistry results from Slack: browsing a calculations tree, plotting
and summarizing data in a sandbox, and recording per-project notes.

This document describes the system **as built**. Work that is designed but not yet
implemented (production service account / systemd, the agent submitting its own Slurm
jobs, per-user calculations roots, metadata moving to a sidecar) is collected in
[§18 Roadmap](#18-roadmap--not-yet-implemented). Magic numbers in
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
  │         list_directory · read_file · search_files · attach_file · write_metadata
  │         read_workflow · write_workflow
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

Two processes, both single-instance:

- **`aspen-bot.py`** (the `aspen` package) — the Slack front-end and the agent. It runs
  the **Claude Agent SDK** against the Claude Code CLI, exposes a locked-down tool
  surface, and keeps a warm SDK session per Slack thread. The read-only browsing tools,
  `write_metadata`, and the workflow tools run **in-process**; `run_python_analysis` is
  the one tool that reaches out to the tool server.
- **`tool_server.py`** — a FastAPI service bound to a **Unix-domain socket** (a file in
  a `0700` directory, not a TCP port — so other users on a shared node can't reach it)
  that executes LLM-generated analysis code inside a **bubblewrap (bwrap)** jail with a
  **seccomp** syscall filter. It owns caching, metadata parsing, the per-project SQLite
  index, figure handling, and audit logging.

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
([§5](#5-user-registry)). Anyone else gets at most one "not authorized" reply that names
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
| `list_directory` | read-only | List a directory under the calculations root |
| `read_file` | read-only | Read a text file under the calculations root (size-capped) |
| `search_files` | read-only | Grep file contents under the calculations root (path-confined, in-process) |
| `attach_file` | read-only | Upload a calculations-root file alongside the Slack reply |
| `write_metadata` | **write** | Create/overwrite **only** a project's top-level `metadata.md` |
| `read_workflow` | read-only | Open a user's workflow file (own, a colleague's, or `_group`) |
| `write_workflow` | **write** | Create/overwrite **only the speaking user's own** workflow |
| `run_python_analysis` | sandboxed | Execute analysis code in the bwrap jail (via the tool server) |

The agent has exactly **two** write surfaces, both enforced in Python and both narrow by
construction.

`write_metadata`: the target must resolve to `<calculations-root>/<project>/metadata.md`
(single path component, no traversal), the project dir must already exist, and nothing
else can be written. All calculation inputs/outputs/data stay read-only.

`write_workflow`: the target is `<workflows-root>/<alias>__<slack-id>/WORKFLOW.md` for the
**user who sent the message**. Note what the tool schema does *not* contain: an owner
parameter. The destination is derived from `context["user_id"]` — the ID Slack itself
attached to the event — so no wording in the conversation can redirect a write to someone
else's file. A model can be talked into passing any argument it is told to; it cannot pass
a value it never receives. The one exception is the shared `_group` workflow, gated on
`uid == ADMIN_USER_ID` in the same function.

Both writes replace the whole file, so the previous version is snapshotted first —
`<workspace>/metadata_history/<project>/<UTC>.md` and
`<workspace>/workflow_history/<slack-id>/<UTC>.md` respectively — making a careless
overwrite recoverable.

The read/search tools (`list_directory`, `read_file`, `search_files`, `attach_file`) are
**path-fenced in Python** to the calculations root (`_safe_path`): they resolve symlinks
and reject anything outside the root, so they cannot read host files such as `~/.ssh` or
`.env`. `search_files` greps file *contents* entirely in-process (it never shells out),
with the same fence.

### Bash

The Bash tool's default allowlist is **read-only Slurm investigation only**:
`squeue`/`sacct`/`sinfo`/`sstat`/`sprio`/`scontrol show`. General file utilities
(`cat`/`grep`/`head`/`tail`/`ls`/…) are deliberately **excluded**: with the OS sandbox
off (the default) the Bash tool runs as the bot's Unix user with no path restriction, so
those would let an allowlisted user read any file that user can (SSH keys, `.env`,
`~/.claude`). Content search is provided instead by the path-fenced `search_files` tool.
The `can_use_tool` backstop denies anything off the allowlist; job-control (`scancel`,
`scontrol update`) is never permitted.

If/when the agent is given a Bash **write/exec** surface, the Claude Code OS sandbox
("Sandbox B") should be enabled **fail-closed** to confine it — see
[`THREAT_MODEL.md`](THREAT_MODEL.md) §7. It is off today because it wraps only the Bash
tool (not the in-process tools) and the Slurm commands are excluded from it anyway, so it
would currently confine nothing.

---

## 5. User Registry

Who may talk to Aspen, and what to call them, lives in a single JSON file —
`ASPEN_USERS_FILE` (default `$ASPEN_STATE_DIR/users.json`, i.e. `~/.aspen/users.json`).

```json
{"version": 1, "users": [
  {"slack_user_id": "U0SAM", "alias": "sam", "display_name": "Sam",
   "role": "admin", "status": "active", "added": "2026-06-01", "added_by": "bootstrap"},
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

**Writes are CLI-only.** `aspen-users` (`python -m aspen.users_cli`) is the only thing
that writes the registry: `init`, `list`, `add`, `rename`, `remove`, `sync`, `whois`. The
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

`main._check_state_locations()` refuses to start if `USERS_FILE` or `WORKFLOWS_ROOT`
resolves inside `WORKSPACE_ROOT` or any `ASPEN_SANDBOX_WRITE_PATHS` entry. Those areas are
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

Each project has a human-readable **`metadata.md`** at its root that the agent reads to
understand the project, and that the agent can update via `write_metadata`. The tool
server parses one machine-relevant field from it — the advisory list of Python libraries
under a heading mentioning "librar" (e.g. "## Python libraries available for analysis").
For backward compatibility `metadata.toml` / `metadata.yaml` are still accepted as a
fallback. If no metadata file exists, the tool server returns a 422 with a `metadata.md`
template.

Metadata is planned to move **out of the project directory** into a mirrored sidecar under
`ASPEN_STATE_DIR`, since the agent must not write into user-owned trees — see
[§18.4](#184-project-metadata-as-a-sidecar).

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
| Tool surface | Locked-down allowlist + `can_use_tool` deny; host settings ignored; Bash = Slurm read-only |
| Read/search tools | Path-fenced in Python to the calculations root; can't read `~/.ssh`, `.env`, etc. |
| Write surface | Only `write_metadata` (one file per project, prior version snapshotted) + the jail's `figures/`,`cache/` |
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
DM that contains any non-allowlisted human (participant gate, fail-closed); write any
project file other than `metadata.md`; submit/cancel/inspect Slurm jobs (read-only investigation
only); reach files outside the calculations root / workspace; make network calls from
inside the analysis jail.

---

## 17. Tests

A hermetic pytest suite runs without a live Slack connection, Claude CLI, or network
(`pytest -q` from the repo root). Highlights:

- **`tests/test_tools.py`** — read-only browsing tools, `write_metadata` (path safety,
  metadata.md-only, existing-project-only), and the tool-server bridge (mocked HTTP).
- **`tests/test_tool_server_bwrap.py`** — the bwrap command builder: prlimit caps, network
  unshared, project read-only, only `figures/`/`cache/` writable, interpreter binds,
  scrubbed env, hook-writable `MPLCONFIGDIR`, `/etc/fonts` bound.
- **`tests/test_sdk_backend.py`** — SDK option lockdown (allowed tools, ignored host
  settings), warm-session reuse, and turn-end reporting (success, real errors, and the
  `error_max_turns` soft-pause path).
- **`tests/test_sessions.py`, `test_ratelimit.py`, `test_admission.py`,
  `test_render.py`, `test_attachments.py`** — session lifecycle, rate limits, the Slack
  admission/typing-status path, Slack-markdown rendering, and the attachment sink.

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

### 18.2 Agent-submitted Slurm/PBS jobs (ORCA → CORVUS pipeline)

Today Aspen's scheduler access is **read-only investigation** only. A future capability
would let it submit and cancel its own jobs via the `orca-pipeline` `submit-batch.py`
(ORCA → CORVUS → postprocess chains, one per `.xyz`). Non-negotiable design principles:

- **No agent-written code touches the pipeline** — it may only invoke `submit-batch.py`
  with a validated, fixed `template_mode` allowlist and path-validated structure/output
  dirs (within the projects root); it never composes shell commands.
- **Every submission is fully logged before `qsub`/`sbatch`** (command, args, user, job
  IDs, timestamp); if logging fails, submission aborts.
- **`--no-submit` dry run first** to validate inputs before any real submission.
- **Scheduler-agnostic in the pipeline, not Aspen.** Cancellation is scoped strictly to
  job IDs in the agent's own SQLite `jobs`/`job_runs` tables (reserved names) — the agent
  never lists scheduler jobs and cancels from that. Cancelling the CORVUS job kills the
  dependent postprocess job via the dependency chain.
- Two endpoints: `submit_orca_batch` and `cancel_orca_batch`. To keep this addable
  without refactoring, the tool server stays structured so new routes/tools drop in
  without modifying existing ones.

Once calculations are per-user ([§18.3](#183-per-user-calculations-roots)), submission is
always **on someone's behalf but under the agent's own identity** — the agent's Unix user
and its own munge credentials, never the requester's:

- **Copy, then edit, then submit.** Nothing in a user's root is modified. Files are staged
  into `$WORKSPACE_ROOT/jobs/<alias>__<uid>/<thread>/` — copied, never symlinked (a symlink
  lets the sandboxed editor write back through it) — with a provenance record of source
  paths, checksums, and the requesting Slack user. The staging tree is the agent's writable
  surface; roots stay outside every sandbox write path.
- **`sbatch` is a structured tool, never a Bash allowlist entry.** A `Bash(sbatch:*)` prefix
  rule would hand the model `--wrap`, i.e. arbitrary code execution as the bot user on a
  compute node, outside the jail. The tool builds the argv itself: script path must resolve
  inside a staging directory, resources come from validated fields, partition/account from
  an allowlist. It runs outside the jail for the same reason the Slurm read clients do
  (munge socket + cluster network).
- **Scrub the job environment.** `load_dotenv` puts `SLACK_BOT_TOKEN` and
  `AGENT_INTERNAL_SECRET` into the bot's `os.environ`, which a naive `sbatch` inherits into
  the job. `--export=NONE` plus an explicit whitelist.
- **Tag for attribution, keyed by Slack ID.** `--job-name aspen-<alias>-<project>` is what
  the group sees in `squeue`; `--comment aspen/v1/<slack-id>/<thread-ts>` is the durable
  machine key. The comment carries the **ID, not the alias**: aliases are renameable and
  lookups everywhere else resolve by `slack_user_id`, so an alias baked into a months-old
  job record stops resolving the first time someone is renamed. Both strings are composed
  only from the Slack event's `user_id` and `thread_ts` — never from conversation text —
  the same rule that keeps `write_workflow` un-forgeable (C9).
  **Verified on s3df (2026-08):** `scontrol show config` reports
  `AccountingStoreFlags = job_comment`, so slurmdbd retains the comment and `sacct -o
  Comment` can recover attribution from Slurm itself, independent of anything Aspen keeps.
  Re-check that flag before relying on it — without it the comment is silently dropped.
- **Accounting is an admin question, not a code one.** Jobs charge the bot's Slurm
  association; either everything runs under one account or the bot is added to each user's,
  which is a cluster-side association change.
- **Gate this on §18.1.** A submitted job runs another user's script, possibly edited by the
  model, as the bot's Unix user with no sandbox on the compute node — so it can read
  `$ASPEN_STATE_DIR` and the repo `.env`. With a single account there is no fix; the
  service-account split is what makes this safe to build.
- **Results stay in the agent's workspace** (world-readable) and are reported/attached from
  there. Writing back into a user's tree is out of scope; if it is ever wanted, the
  mechanism is an opt-in per-user inbox directory whose existence *is* the consent.

**Attribution is two-phase, and the second phase is the one that answers "who used the
compute".** At submit time you know *who and what*; you do not know what it cost — elapsed
time, CPU-hours and exit state exist only after the job ends. A design that logs only at
submission yields job *counts* and nothing about consumption, which is the metric that
matters when an allocation runs low.

1. **Ledger, written once at submit** (the "fully logged before `sbatch`" requirement
   above, given a schema): `job_id`, `slack_user_id`, `alias`, `thread_ts`, `project`,
   staging path, submitted-at, and the requested resources. Rows are immutable.
2. **Reconciler, run later** — joins the ledger against Slurm's own accounting to fill in
   what the job actually consumed:

   ```
   sacct -X -n -P -u aspen-agent -S <since> \
     -o JobID,JobName,Comment,State,Submit,Start,End,Elapsed,TotalCPU,AllocTRES,ExitCode
   ```

   `-X` limits to job allocations (no `.batch`/`.extern` step rows, which would double-count),
   `-P` is parseable output, `-n` drops the header. `TotalCPU`/`AllocTRES` are the
   consumption figures; `Comment` lets a row be re-attributed even if the ledger is lost.

The two copies fail differently and that is the point: the ledger can be corrupted or
deleted, and Slurm's copy survives it; slurmdbd purges on a site-set schedule, and the
ledger survives that. Neither alone is a durable record of who used the allocation.

### 18.3 Per-user calculations roots

Today there is one `CALCULATIONS_ROOT` and it is one person's tree; every user's questions
resolve into it. The target model is **N named roots**, so each user's own work lives where
they keep it and colleagues' work is reachable by name.

**The read boundary stays flat, and stays POSIX's job.** Everyone may read everyone,
exactly as on the shared filesystem — so Aspen models *naming and ownership*, never
permission. A group/ACL layer inside Aspen would be a copy of the real boundary, and a copy
can only ever be wrong in the direction of showing something it shouldn't. If the boundary
ever stops being flat (an embargo, a collaborator's data under someone else's agreement),
the enforcement is Unix groups on the bot's own account and the read simply fails — nothing
to model here. Note the corollary at the §18.1 cutover: the bot currently runs as the
developer and therefore sees everything the developer sees; under `aspen-agent` its group
memberships *become* the effective boundary, so every root needs re-checking then.

- **Where the value lives:** a `calc_root` field on the registry record (§5), beside
  `alias`/`role`, plus `unix_user` (a Slack ID does not name a SLAC account). The registry
  hot-reloads and sits outside `WORKSPACE_ROOT`, so sandboxed code cannot edit where the
  agent is allowed to look — the same requirement that put `USERS_FILE` there.
- **Shared roots** are named entries owned by nobody, for group project data. Needed
  because the existing tree is a mix of personal and shared work: without this tier,
  setting the first personal root permanently relabels group projects as one person's.
- **Migration is a no-op.** `CALCULATIONS_ROOT` remains the fallback for any user with no
  `calc_root`. Day one nobody has one and behavior is identical; roots are then set one
  user at a time, each taking effect on that user's next message.
- **One resolution seam.** `_safe_path(rel)` becomes `_safe_path(rel, owner, context)`:
  empty owner = the caller's own root, an alias = that person's. Every file tool already
  routes through it. **The model passes an alias, never a path** — the registry does
  alias → root on the trusted side, the same property that makes `write_workflow`
  unspoofable. Paths echoed back are qualified (`@arun-asundi/thermolysin/...`), which is
  also the attribution a PI asking across the group actually wants.
- **Scope, not permission, on search.** `search_files` gains own (default) / one alias /
  everyone. The caps in §15 bound a single call; N roots multiply the work, so a
  cross-root sweep needs its own budget.
- **Validation twice.** An `aspen-users set-root` command validates at write time (exists,
  is a directory, readable *as the bot's Unix user*, not inside `STATE_DIR`/`WORKSPACE_ROOT`
  /the repo) and `main._check_state_locations` re-checks at startup, because roots rot.
  **No root may be nested inside another**: `_safe_path` fences by `relative_to`, so a root
  containing another silently encloses it and the boundary is not there.
- **Writes never cross into a root.** `write_metadata` is the only write into the tree
  today and it moves out entirely (§18.4). Anything the agent must modify — a script it is
  preparing to run — is *copied* into its own workspace first; see §18.2.
- **The tool server needs the same treatment.** `PROJECTS_ROOT` is a second copy of the
  fence and feeds the bwrap binds. It already receives `user_id` per call, so it resolves
  the root from the registry server-side and binds *that* read-only — it never accepts a
  root path over the socket.

### 18.4 Project metadata as a sidecar

`write_metadata` (§7) writes `metadata.md` into the project directory. That works only
because the tree belongs to the account the bot runs as. Under per-user roots it is a write
into someone else's directory, and under the §18.1 service account it stops working at all,
including for the developer's own root. Metadata therefore moves **out of the calculations
tree**, into a sidecar that mirrors it:

```
$ASPEN_STATE_DIR/
  users.json
  workflows/<alias>__<uid>/WORKFLOW.md
  metadata/<alias>__<uid>/<project>/metadata.md        ← mirrors <their-root>/<project>/
  metadata_history/<alias>__<uid>/<project>/<ts>.md
```

**The path is the key.** Mirroring derives the location by arithmetic, so there is no
mapping to keep in sync — the same trick as `workflows.dir_for`, and the reason to prefer
it over an index file (a second source of truth that can desync, needs locking, and stores
what was derivable), flat encoded filenames (slug collisions: `a/b` and `a-b`), or
frontmatter declaring its own target (requires a full scan to answer one lookup, and makes
the *model's* declared target authoritative).

**It belongs in `STATE_DIR`, not `WORKSPACE_ROOT`.** The workspace is sandbox-writable, and
metadata is read back into the model's context on later turns — so metadata stored there is
a slow-loop injection path: generated analysis code edits a note that steers a future
session. The line to hold is **`WORKSPACE_ROOT` = what the sandbox produces; `STATE_DIR` =
what steers the agent.** Placing it beside `workflows/` also inherits the existing
`_check_state_locations` guard for free. `metadata_history` moves for the same reason.

Consequences to handle in the same pass:

- **`metadata_history` must be keyed by owner.** It is keyed by project alone today, so the
  moment there are multiple roots, two users with a `thermolysin` project silently share a
  history directory. Latent collision, exposed rather than created by this change.
- **Ownership is the *data's* owner, not the writer.** One canonical metadata file per
  project, written only by the person whose project it is. Per-viewer annotations (a PI
  commenting on someone's work) are a different feature; folding them in here would fork
  metadata into per-reader copies.
- **Fence the join.** The relative path comes from a tool argument and is now joined into a
  *writable* location, so it needs the resolve-and-`relative_to` check `workflows._fenced`
  does. Keep the existing requirement that the source project directory exists — it doubles
  as validation, so metadata cannot exist for a project that does not.
- **Read it through its own tool, not a transparent `read_file` redirect.** Metadata is
  Aspen-authored; project files are the user's. Serving both through one path means the
  agent cannot distinguish its own past notes from ground truth — the same reason
  `read_workflow` is separate and trust-tagged (§6). `list_directory` notes that metadata
  exists; `read_metadata(project, owner)` returns it with authorship and date.
- **The tool server reads it too.** `load_metadata` parses the advisory Python-library list
  out of the project directory (§7) and 422s with a template when absent; it must read the
  sidecar instead, resolved server-side from `user_id`.
- **Migration.** Existing in-tree `metadata.md` files are copied into the sidecar keyed to
  their owner. The originals cannot be removed (read-only tree, and impossible once the bot
  is a separate account), so they linger as ordinary files that `read_file` still returns —
  `read_metadata` prefers the sidecar, and users delete the stale in-tree copy themselves.

### 18.5 Other deferred items

- LLM-assisted metadata/indexing suggestions.
- Persistent conversation history across restarts.
- Migration from SQLite to PostgreSQL.
- Automatic analysis-venv rebuild when a project's library list changes (currently a
  manual edit of `analysis-requirements.txt` + rebuild).
- Async/background figure-archive trimming (synchronous per-request is sufficient now).
- Automated shared-secret rotation (manual today).
- Multi-process scaling (would require moving rate-limit/concurrency state out of process).
