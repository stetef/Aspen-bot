# Aspen — HPC Slack Agent: Design & Architecture

Aspen is a Slack research assistant built for the **Structural Molecular Biology (SMB)
group at the Stanford Synchrotron Radiation Lightsource (SSRL)**, part of SLAC National
Accelerator Laboratory at Stanford University. It helps the group explore and analyze
HPC computational-chemistry results from Slack: browsing a calculations tree, plotting
and summarizing data in a sandbox, and recording per-project notes.

This document describes the system **as built**. Work that is designed but not yet
implemented (production service account / systemd, the agent submitting its own Slurm
jobs) is collected in [§16 Roadmap](#16-roadmap--not-yet-implemented). Magic numbers in
this doc are defaults; the authority is `.env` (see [§13](#13-environment-variables-env)).

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
  │         list_directory · read_file · attach_file · write_metadata
  │
  ▼  run_python_analysis → HTTP POST to 127.0.0.1 with a shared-secret header
FastAPI tool server  (tool_server.py, binds to 127.0.0.1 only)
  │
  ▼
bwrap sandbox (bubblewrap, one jail per execution)
  │
  ├── /projects/<name>/                 [read-only bind]
  └── /aspen_workspace/                 [only figures/ and cache/ are writable]
        ├── figures/        cache/       (+ generated_code/, figure_archive/, logs/, db/ on host)
```

Two processes, both single-instance:

- **`aspen-bot.py`** (the `aspen` package) — the Slack front-end and the agent. It runs
  the **Claude Agent SDK** against the Claude Code CLI, exposes a locked-down tool
  surface, and keeps a warm SDK session per Slack thread. The read-only browsing tools
  and `write_metadata` run **in-process**; `run_python_analysis` is the one tool that
  reaches out to the tool server.
- **`tool_server.py`** — a FastAPI service bound to `127.0.0.1` that executes
  LLM-generated analysis code inside a **bubblewrap (bwrap)** jail. It owns caching,
  metadata parsing, the per-project SQLite index, figure handling, and audit logging.

Each project has its own SQLite database file for metadata and run indexing.

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
3. builds the **analysis venv** once (see [§6](#6-analysis-sandbox-bwrap)) — with `uv`
   when available, falling back to `python -m venv` + `pip` — and exports
   `ANALYSIS_PYTHON`,
4. launches `tool_server.py` in the background, then `aspen-bot.py` in the foreground.

In this mode every analysis request executes under the developer's Unix identity, so the
Slack user allowlist ([§3](#3-slack-integration--socket-mode)) **must** contain only the
developer's own user ID. Production deployment under a dedicated service account is
[roadmap](#16-roadmap--not-yet-implemented).

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

Aspen has no `channels:read` or unrestricted history access — it only sees threads it was
mentioned in.

### Interaction model

- Responds only to `@Aspen` mentions in channels or DMs; ignores non-mention messages.
- While working, it shows Slack's native **"Aspen is typing…"** status via
  `assistant.threads.setStatus` (a daemon thread re-asserts it every ~50 s and clears it
  before the reply). On channel @-mentions where `setStatus` doesn't apply, it falls back
  to a posted "_Thinking…_" message.
- Posts results (text + figures) as replies in the same thread.

### User allowlist

Aspen acts only on Slack user IDs in `ASPEN_ALLOWED_SLACK_USER_IDS` (comma-separated).
Anyone else is ignored with at most one "not authorized" reply. This check is the **first
authorization gate**, before rate limiting and before any tool runs. In the current dev
mode the allowlist must contain only the developer's own ID.

---

## 4. Tool Surface

The agent is locked down: `allowed_tools` auto-approves exactly the MCP tools below plus a
read-only Bash allowlist, and a `can_use_tool` backstop denies everything else. Host
settings are ignored (`setting_sources=[]`) so an operator's personal Claude permissions
can't widen the bot.

| Tool | Access | What it does |
|---|---|---|
| `list_directory` | read-only | List a directory under the calculations root |
| `read_file` | read-only | Read a text file under the calculations root (size-capped) |
| `search_files` | read-only | Grep file contents under the calculations root (path-confined, in-process) |
| `attach_file` | read-only | Upload a calculations-root file alongside the Slack reply |
| `write_metadata` | **write** | Create/overwrite **only** a project's top-level `metadata.md` |
| `run_python_analysis` | sandboxed | Execute analysis code in the bwrap jail (via the tool server) |

`write_metadata` is the agent's **only** write surface. It is enforced in Python: the
target must resolve to `<calculations-root>/<project>/metadata.md` (single path
component, no traversal), the project dir must already exist, and nothing else can be
written. All calculation inputs/outputs/data stay read-only.

### Bash

With the OS sandbox enabled, read-only Slurm investigation commands
(`squeue`/`sacct`/`sinfo`/`sstat`/`sprio`/`scontrol show`) run directly against the
cluster (they're excluded from the jail because they need cluster network + munge), and
any other Bash runs inside Claude Code's own bwrap sandbox with an operator-defined write
boundary. Job-control (`scancel`, `scontrol update`) and writes outside the boundary are
blocked. Without the OS sandbox, only the fixed read-only allowlist is permitted.

---

## 5. Project Metadata

Each project has a human-readable **`metadata.md`** at its root that the agent reads to
understand the project, and that the agent can update via `write_metadata`. The tool
server parses one machine-relevant field from it — the advisory list of Python libraries
under a heading mentioning "librar" (e.g. "## Python libraries available for analysis").
For backward compatibility `metadata.toml` / `metadata.yaml` are still accepted as a
fallback. If no metadata file exists, the tool server returns a 422 with a `metadata.md`
template.

**Project text is untrusted input.** Metadata, README/blurb text, file names, and file
contents all flow into the model's context and could attempt prompt injection
("ignore previous instructions…"). The security boundary is therefore the **bwrap jail
and the Python-enforced tool restrictions**, never the prompt. Project-derived text can
never relax sandbox restrictions, choose mounts, or alter the import advisory.

---

## 6. Analysis Sandbox (bwrap)

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

## 7. Output & Figure Handling

- **stdout** truncated to 10,000 chars, **stderr** to 2,000, before returning to the
  model / Slack; `truncated: true` triggers a note in the reply.
- **Figures:** 5 MB per-PNG upload cap. Oversized figures are flagged so the model can
  regenerate at lower dpi/size; if still too big, a text-only reply is posted. The system
  prompt instructs default `dpi=200`, retry at `dpi=72` + halved dimensions on failure.
- **Archiving:** uploaded PNGs move from `figures/` to `figure_archive/`; when the archive
  exceeds 2 GB, oldest files are trimmed to 1.5 GB at the start of each request (no cron).

---

## 8. Conversation Context

Context is held by the **Claude Agent SDK's warm session**, one per Slack thread
(`thread_ts`), parked between turns and reused — the SDK retains conversation state
natively, so the bot does not maintain its own messages array. Sessions are bounded by
`MAX_OPEN_SESSIONS` and expire after `CONTEXT_EXPIRY_SECONDS` (default 4 h). Each turn is
capped at `AGENT_MAX_ROUNDS` agentic tool-call rounds (default 25); hitting it ends the
turn with `error_max_turns`, and Aspen reports a soft pause ("reply *continue*…") while
keeping the thread's context — it is **not** a hard error.

---

## 9. Rate Limiting & Concurrency

Enforced in `aspen-bot.py` before any tool runs, per Slack user ID, in-memory:

| Limit | Default |
|---|---|
| Requests / user / 10-min window | `RATE_LIMIT_REQUESTS` = 5 |
| Concurrent executions / user | 1 |
| Concurrent executions, global | `MAX_CONCURRENT_EXECUTIONS` = 5 |

Over-limit users get an immediate in-thread message; a busy global cap yields a
"busy right now" reply. State resets on restart (no persistent store).

---

## 10. Per-Project Database — SQLite

Each project uses one SQLite file at `<workspace>/db/<project>.sqlite` (no Postgres
dependency). The tool server is the sole writer; the jail has no access to db files.

**Placement & journal mode.** WAL's `-shm` mmap is unreliable on parallel filesystems.
Prefer node-local disk (`SQLITE_DB_ROOT`) with WAL; on the group data path, use the
default rollback journal (no WAL) and serialize writes. Always set `PRAGMA busy_timeout`
(e.g. 5000 ms) in the connection helper. Schema: a `runs` table (path, status, tags,
energy, structure, last_update) and a `datasets` table, with indexes on status/tags.

---

## 11. Caching

Cache key = `SHA-256(question + sorted(dataset_ids) + max(file_mtimes))`, so new data in a
run directory invalidates automatically. Entries are stored at
`cache/<project>/<hash>.json` with stdout/stderr/figure paths; a hit skips execution and
re-uploads the archived figure. A hit verifies each referenced figure still exists (the
archive trimmer may have removed it) and re-executes if any is missing. No time-based
expiry.

---

## 12. Logging, Auditing & Secret Redaction

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

## 13. Environment Variables (`.env`)

See `.env.example` for the full annotated list. Key groups:

```bash
# Slack / auth
SLACK_BOT_TOKEN=xoxb-...      SLACK_APP_TOKEN=xapp-...
ASPEN_ALLOWED_SLACK_USER_IDS=U0XXXXXXXXX        # dev mode: developer's ID only
ANTHROPIC_API_KEY=sk-ant-...                     # only if ASPEN_SDK_USE_SUBSCRIPTION=false
ASPEN_SDK_USE_SUBSCRIPTION=true                  # default: use the Claude Code login
ANTHROPIC_MODEL=claude-opus-4-8
AGENT_INTERNAL_SECRET=<32-byte hex>              # shared secret, bot ↔ tool server

# Paths
CALCULATIONS_ROOT=/.../calculations      # browsing tools + write_metadata
PROJECTS_ROOT=/.../calculations          # analysis (read-only mount)
WORKSPACE_ROOT=/.../aspen_workspace      # figures, cache, logs, db, generated_code
SQLITE_DB_ROOT=/tmp/aspen_db
TOOL_SERVER_URL=http://127.0.0.1:27195

# Analysis sandbox (bwrap)
ANALYSIS_PYTHON=                          # default $WORKSPACE_ROOT/analysis-venv/bin/python
# ANALYSIS_VENV, BWRAP_BIN, ANALYSIS_RO_PATHS — optional, sane defaults
ANALYSIS_AS_LIMIT_BYTES=2147483648        # RLIMIT_AS (memory); 0 disables
ANALYSIS_FSIZE_LIMIT_BYTES=536870912
# ANALYSIS_CPU_LIMIT_SECONDS — defaults to EXECUTION_TIMEOUT_SECONDS

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

## 14. Security Summary

| Layer | Protection |
|---|---|
| Authorization | Slack user-ID allowlist — first gate, before rate limiting |
| Slack connection | Socket Mode — outbound WebSocket only, no open ports |
| Tool surface | Locked-down allowlist + `can_use_tool` deny; host settings ignored |
| Write surface | Only `write_metadata` (one file per project) + the jail's `figures/`,`cache/` |
| Tool server | Binds to `127.0.0.1`; shared secret on every request |
| Analysis jail | bwrap: no network; read-only minimal FS; project read-only; scrubbed env; `prlimit` caps; 120 s timeout. The minimal jail + bind mounts are the boundary |
| Stdout/stderr | Redacted then truncated (10k/2k) before leaving the tool server |
| Figures | 5 MB upload cap; archived, trimmed at 2 GB |
| Rate limiting | 5 req / 10 min / user; 1 concurrent / user; global cap |
| Logging | Structured JSON; secrets redacted |
| Secrets | In `.env` (chmod 600); never logged or sent to Slack/model |

**Hard constraints — Aspen can never:** act for a non-allowlisted user; write any project
file other than `metadata.md`; submit/cancel/inspect Slurm jobs (read-only investigation
only); reach files outside the calculations root / workspace; make network calls from
inside the analysis jail.

---

## 15. Tests

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

## 16. Roadmap / Not Yet Implemented

These are designed or intended but **not** in the current build.

### 16.1 Production deployment (service account + systemd)

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

### 16.2 Agent-submitted Slurm/PBS jobs (ORCA → CORVUS pipeline)

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

### 16.3 Other deferred items

- LLM-assisted metadata/indexing suggestions.
- Persistent conversation history across restarts.
- Migration from SQLite to PostgreSQL.
- Automatic analysis-venv rebuild when a project's library list changes (currently a
  manual edit of `analysis-requirements.txt` + rebuild).
- Async/background figure-archive trimming (synchronous per-request is sufficient now).
- Automated shared-secret rotation (manual today).
- Multi-process scaling (would require moving rate-limit/concurrency state out of process).
