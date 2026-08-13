"""
Configuration — all values from .env, nothing hardcoded.

Other modules read these as ``config.<NAME>`` (at call time) so they stay
overridable and testable. Import-time behavior matches the original single file:
required env vars raise ``KeyError`` if missing, and the Anthropic client is built
here at import.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aspen")

# ---------------------------------------------------------------------------
# Required / core configuration (unchanged from the original)
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN     = os.environ["SLACK_APP_TOKEN"]
# ANTHROPIC_API_KEY is not read here. The agent authenticates the Claude
# Code CLI (via the subscription login, or the key passed to the CLI subprocess
# when ASPEN_SDK_USE_SUBSCRIPTION=false) — see agent.py.
CALCULATIONS_ROOT     = Path(os.environ["CALCULATIONS_ROOT"]).resolve()
MODEL                 = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")


def _flag_early(name: str, default: str) -> bool:
    """``_flag`` is defined further down (with the sandbox settings); this is the
    same thing for the handful of flags that have to be read up here."""
    return os.getenv(name, default).lower() in ("1", "true", "yes")


def _named_paths(name: str) -> dict:
    """``"smb=/data/smb,legacy=/data/old"`` -> ``{"smb": "/data/smb", ...}``."""
    out = {}
    for item in os.getenv(name, "").split(","):
        label, sep, path = item.partition("=")
        label, path = label.strip().lower(), path.strip()
        if not sep or not label or not path:
            if item.strip():
                log.warning("%s: ignoring %r — expected name=/absolute/path", name, item)
            continue
        out[label] = path
    return out


# Calculations roots that belong to nobody in particular — group project data.
# Per-user roots live on the registry record (`calc_root`); CALCULATIONS_ROOT
# above is the fallback for anyone without one, which is what keeps a
# single-root deployment behaving exactly as it did. See roots.py.
SHARED_CALC_ROOTS     = _named_paths("ASPEN_SHARED_CALC_ROOTS")

# ---------------------------------------------------------------------------
# DEMO mode — a walkthrough for people who are not users yet (see demo.py).
#
# Safe to expose to a whole workspace because a demo session is scope-isolated
# (the demo root is the ONLY thing it can read), writes nothing — not even a
# temporary registry entry — and renders the admin request into the thread
# instead of sending it. What it does cost is model time, so it is capped three
# ways: per session, per day across everyone, and by the ordinary rate limiter.
# ---------------------------------------------------------------------------
DEMO_ENABLED          = _flag_early("ASPEN_DEMO_ENABLED", "true")
DEMO_ROOT             = Path(os.getenv(
    "ASPEN_DEMO_ROOT", str(Path(__file__).resolve().parent.parent / "examples" / "demo-calculations")
)).resolve()
# One walkthrough is ~10 turns; the cap is generous enough to wander.
DEMO_MAX_TURNS        = int(os.getenv("ASPEN_DEMO_MAX_TURNS", "30"))
DEMO_MAX_SESSIONS     = int(os.getenv("ASPEN_DEMO_MAX_SESSIONS", "10"))
DEMO_MAX_STARTS_PER_DAY = int(os.getenv("ASPEN_DEMO_MAX_STARTS_PER_DAY", "20"))
DEMO_SESSION_TTL      = int(os.getenv("ASPEN_DEMO_SESSION_TTL_SECONDS", "3600"))
# Actually DM the admin during a demo. Off by default: a demo anyone can trigger
# must not be able to page a human.
DEMO_REAL_ADMIN_DM    = _flag_early("ASPEN_DEMO_REAL_ADMIN_DM", "false")

# ---------------------------------------------------------------------------
# Users and per-user workflows
#
# State that outlives a release but isn't code: the user registry (who may talk
# to Aspen, and their alias) and each user's workflow file. Kept OUTSIDE both the
# repo and WORKSPACE_ROOT on purpose — see the startup guard in main.py. The
# workspace is the analysis sandbox's writable area, so a registry living there
# would be writable by sandboxed code, turning "who is allowed" into something
# the agent could edit.
# ---------------------------------------------------------------------------
STATE_DIR             = Path(os.getenv("ASPEN_STATE_DIR", str(Path.home() / ".aspen"))).resolve()
# The user registry: Slack ID <-> alias/display name, plus the admission
# allowlist. Written only by the `aspen-users` CLI, read (hot-reloaded) by the
# bot. See registry.py.
USERS_FILE            = Path(os.getenv("ASPEN_USERS_FILE", str(STATE_DIR / "users.json"))).resolve()
# Per-user workflow files: <root>/<alias>__<slack-id>/WORKFLOW.md, plus the
# shared _group/ and the _archive/ of removed users. See workflows.py.
WORKFLOWS_ROOT        = Path(os.getenv("ASPEN_WORKFLOWS_ROOT", str(STATE_DIR / "workflows"))).resolve()
# Cap on a single workflow file (they are prose, not data).
MAX_WORKFLOW_BYTES    = int(os.getenv("ASPEN_MAX_WORKFLOW_BYTES", "60000"))
# Project metadata, mirroring each root's layout:
#   <root>/<alias>__<slack-id>/<project>/metadata.md
# It lives HERE rather than under WORKSPACE_ROOT for the reason the registry
# does, and one more: metadata is read back into the model's context on later
# turns, so metadata in a sandbox-writable area is a slow-loop injection path —
# generated analysis code edits a note that steers a future session. The line to
# hold is: WORKSPACE_ROOT is what the sandbox produces, STATE_DIR is what steers
# the agent. See metadata.py.
METADATA_ROOT         = Path(os.getenv("ASPEN_METADATA_ROOT", str(STATE_DIR / "metadata"))).resolve()
METADATA_HISTORY_ROOT = Path(
    os.getenv("ASPEN_METADATA_HISTORY_ROOT", str(STATE_DIR / "metadata_history"))
).resolve()
# Pending asks for the admin: who wants access, who wants a calculations root.
# Aspen can't grant either — it records the ask and DMs the admin the exact
# command. Beside the registry for the same reason: sandboxed code must not be
# able to fabricate a request the operator might act on. See pending.py.
REQUESTS_FILE         = Path(os.getenv("ASPEN_REQUESTS_FILE", str(STATE_DIR / "requests.json"))).resolve()
# Don't re-DM the admin about the same pending ask more often than this.
REQUEST_NOTIFY_COOLDOWN_HOURS = float(os.getenv("ASPEN_REQUEST_NOTIFY_COOLDOWN_HOURS", "12"))
# Bootstrap allowlist, used only until USERS_FILE exists (fresh install) or if it
# can't be parsed and nothing good was ever cached. The registry is the real
# source of truth; this is the operator-controlled floor that prevents lockout.
BOOTSTRAP_USER_IDS    = [u.strip() for u in os.getenv("ASPEN_ALLOWED_SLACK_USER_IDS", "").split(",") if u.strip()]
# Explicit admin override. Empty = the registry decides (first `role: admin`,
# else the first active user — the historical "first ID in the list" rule).
ADMIN_OVERRIDE        = os.getenv("ASPEN_ADMIN_SLACK_USER_ID", "").strip()

RATE_LIMIT_REQUESTS   = int(os.getenv("RATE_LIMIT_REQUESTS", "10"))
RATE_LIMIT_WINDOW     = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300"))
CONTEXT_EXPIRY        = int(os.getenv("CONTEXT_EXPIRY_SECONDS", "14400"))
MAX_CONCURRENT        = int(os.getenv("MAX_CONCURRENT_EXECUTIONS", "5"))
MAX_FILE_BYTES        = int(os.getenv("MAX_FILE_READ_BYTES", "50000"))
# Cap on a single file the agent may attach to a reply via the attach_file tool.
MAX_ATTACHMENT_BYTES  = int(os.getenv("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))
# search_files limits (in-process content search, confined to the calculations root).
SEARCH_MAX_FILES      = int(os.getenv("ASPEN_SEARCH_MAX_FILES", "3000"))
SEARCH_MAX_MATCHES    = int(os.getenv("ASPEN_SEARCH_MAX_MATCHES", "200"))
SEARCH_MAX_FILE_BYTES = int(os.getenv("ASPEN_SEARCH_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
# A cross-root sweep (search_files everyone=true) needs its own budget: the cap
# above was sized for one tree, and N roots multiply the work. Shared across
# roots, so a sweep is bounded however many people are registered — and the tool
# reports which roots it could not reach rather than reading as complete.
SEARCH_MAX_FILES_ALL  = int(os.getenv("ASPEN_SEARCH_MAX_FILES_ALL", str(4 * SEARCH_MAX_FILES)))

# Tool server (only needed when run_python_analysis is used)
AGENT_INTERNAL_SECRET = os.getenv("AGENT_INTERNAL_SECRET", "")
WORKSPACE_ROOT        = Path(os.getenv("WORKSPACE_ROOT", "/aspen_workspace")).resolve()
FIGURE_ARCHIVE_DIR    = WORKSPACE_ROOT / "figure_archive"
# The bot talks to the tool server over a Unix-domain socket (a file), not a TCP
# port — so it can't be reached by other users on a shared node. Keep the path
# short (Linux caps socket paths at ~108 chars). tool_server.py binds this same
# path inside a 0700 directory, which is what keeps other local users out (the
# socket's own mode is 0666 from uvicorn, but a dir they can't enter makes it
# unreachable).
TOOL_SERVER_SOCKET    = os.getenv("ASPEN_TOOL_SERVER_SOCKET", str(WORKSPACE_ROOT / "run" / "tool.sock"))

# ---------------------------------------------------------------------------
# Claude Agent SDK backend
# ---------------------------------------------------------------------------
# Per-turn tool-call (agentic round) cap, passed to the SDK as max_turns.
AGENT_MAX_ROUNDS      = int(os.getenv("AGENT_MAX_ROUNDS", "25"))
# Upper bound on concurrently parked conversation sessions (bounds warm SDK
# CLI subprocesses).
MAX_OPEN_SESSIONS     = int(os.getenv("MAX_OPEN_SESSIONS", "20"))
# Pre-connected spare sessions kept on standby. Connecting an SDK client spawns
# the Claude Code CLI and waits for its init handshake (~1.7 s measured), which
# otherwise lands inside the first message of every new Slack thread. Warming
# spares in the background moves that cost off the user's critical path. Each
# spare is a live CLI subprocess, so this trades memory for latency; 0 disables.
# Counts against MAX_OPEN_SESSIONS.
PREWARM_SESSIONS      = max(0, int(os.getenv("ASPEN_PREWARM_SESSIONS", "1")))
# Path to the Claude Code CLI binary. Empty = auto-discover "claude" on PATH;
# set it when PATH is minimal (e.g. under systemd).
CLAUDE_CLI_PATH       = os.getenv("CLAUDE_CLI_PATH", "")
# Auth: when true, the CLI uses the Claude Code login (subscription) by
# withholding ANTHROPIC_API_KEY from the CLI subprocess. Set false to let the CLI
# use ANTHROPIC_API_KEY (API billing) instead.
ASPEN_SDK_USE_SUBSCRIPTION = os.getenv("ASPEN_SDK_USE_SUBSCRIPTION", "true").lower() in ("1", "true", "yes")

# Built-in Bash tool allowlist (HPC job investigation: squeue, sacct, ...).
# The SDK backend exposes Claude Code's *built-in* Bash tool, but only for the
# command patterns listed here. Entries are Claude Code permission rules — the
# "Bash(cmd:*)" form is a prefix match. The CLI's bash parser checks every
# sub-command of a pipeline and refuses to auto-approve command substitution, so
# a pipeline needs every command in it allowlisted and "squeue $(...)" never
# auto-approves — matching commands run without prompting and everything else is
# denied by the can_use_tool lockdown in agent.py.
#
# SECURITY: the default is Slurm-ONLY. General text utilities (cat/head/tail/ls/
# grep/wc/sort/uniq) are deliberately NOT in the default. With the OS Bash sandbox
# off (SANDBOX_ENABLED=false, the default) the Bash tool runs as the bot's own
# Unix user with no path restriction, so any allowlisted user could have the agent
# read ANY file that user can — SSH private keys, this repo's .env (Slack tokens +
# AGENT_INTERNAL_SECRET), ~/.claude credentials. Calculations-root files stay
# available through the path-scoped read_file tool instead. Excluded for the same
# can't-see-the-flags reason: find (-exec/-delete), awk (system()), sed (w/e).
# Only widen this if you enable the OS sandbox with denyRead on the secret paths
# (ASPEN_SANDBOX_DENY_READ_PATHS), or you accept those commands running unconfined
# as the bot user.
_DEFAULT_BASH_ALLOWLIST = [
    "Bash(squeue:*)",         # job queue
    "Bash(sacct:*)",          # job accounting / history
    "Bash(sinfo:*)",          # partition / node info
    "Bash(sstat:*)",          # running-job stats
    "Bash(sprio:*)",          # job priorities
    "Bash(scontrol show:*)",  # read-only job/node detail (not bare scontrol)
]
BASH_ALLOWLIST = [
    p.strip()
    for p in os.getenv("ASPEN_BASH_ALLOWLIST", ",".join(_DEFAULT_BASH_ALLOWLIST)).split(",")
    if p.strip()
]


# ---------------------------------------------------------------------------
# Bash OS-level sandbox (Claude Code sandbox: bubblewrap on Linux, Seatbelt on
# macOS). When enabled, the agent's Bash commands run inside an OS sandbox whose
# read/write/network boundary is defined HERE by the operator — independent of
# the Unix user the bot runs as. This is how the agent gets *write* access to a
# controlled area without the bot's own account granting it everywhere.
#
# On Linux this needs `bubblewrap` and `socat` installed (apt/dnf). See
# https://code.claude.com/docs/en/sandboxing.
#
# Design (how this composes with BASH_ALLOWLIST above):
#   - Read-only investigation commands (squeue/sacct/...) are EXCLUDED from the
#     sandbox: Slurm clients need cluster network + the munge socket, which the
#     bwrap network jail blocks. Excluded commands run as the bot user but are
#     still auto-approved by BASH_ALLOWLIST (and anything off it is denied by the
#     can_use_tool backstop), so excluding them does not widen access.
#   - Every other command runs INSIDE the jail, auto-approved by the sandbox
#     boundary (autoAllow), able to write only within SANDBOX_WRITE_PATHS.
# ---------------------------------------------------------------------------
def _csv_env(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")

# Verified enforcing 2026-06-24 (CLI 2.1.190 + bubblewrap 0.4.0) when the bot runs
# as a normal top-level process. Gotcha: Claude Code disables its Bash sandbox if
# launched *nested* inside another Claude Code session — so don't start the bot
# from within one. Re-check anytime with ./verify_sandbox.sh from a plain shell.
SANDBOX_ENABLED = _flag("ASPEN_SANDBOX_ENABLED", "false")
# Fail closed: if the sandbox can't start (missing bwrap, unsupported platform),
# refuse to run rather than silently dropping to UNsandboxed execution.
SANDBOX_FAIL_IF_UNAVAILABLE = _flag("ASPEN_SANDBOX_FAIL_IF_UNAVAILABLE", "true")
# Auto-approve Bash commands that successfully run inside the sandbox (the point
# of the jail — the OS boundary contains them, so no per-command prompt).
SANDBOX_AUTO_ALLOW = _flag("ASPEN_SANDBOX_AUTO_ALLOW", "true")
# Allow commands to escape the jail via dangerouslyDisableSandbox. Default false
# (strict): a command either runs sandboxed or is in SANDBOX_EXCLUDED_COMMANDS.
SANDBOX_ALLOW_UNSANDBOXED = _flag("ASPEN_SANDBOX_ALLOW_UNSANDBOXED", "false")
# Session working directory when sandboxed (also writable by default). Keep the
# agent out of the repo/home — point this at a scratch/workspace dir.
SANDBOX_WORKDIR = os.getenv("ASPEN_SANDBOX_WORKDIR", str(WORKSPACE_ROOT))
# Paths the sandboxed agent may WRITE to (beyond cwd + the session temp dir).
# This is the agent's writable surface — operator-controlled, separate from what
# the bot's Unix user could otherwise touch. Prefix rules: "/abs", "~/home", "rel".
SANDBOX_WRITE_PATHS = _csv_env("ASPEN_SANDBOX_WRITE_PATHS")
# Paths to deny reads of inside the jail (e.g. credentials). Empty = read-most.
SANDBOX_DENY_READ_PATHS = _csv_env("ASPEN_SANDBOX_DENY_READ_PATHS")
# Re-allow reads of specific paths inside a denied region.
SANDBOX_ALLOW_READ_PATHS = _csv_env("ASPEN_SANDBOX_ALLOW_READ_PATHS")
# Network domains the sandbox may reach. Empty = no network (safest); a command
# needing an unlisted domain fails rather than hanging on a prompt.
SANDBOX_ALLOWED_DOMAINS = _csv_env("ASPEN_SANDBOX_ALLOWED_DOMAINS")
# Unix socket paths reachable inside the jail (e.g. an SSH agent). Be careful —
# some sockets (docker.sock) are a sandbox escape.
SANDBOX_UNIX_SOCKETS = _csv_env("ASPEN_SANDBOX_UNIX_SOCKETS")
# Commands that run OUTSIDE the jail. Default = the read-only Slurm clients,
# which need cluster network/munge the jail blocks (still gated by BASH_ALLOWLIST).
SANDBOX_EXCLUDED_COMMANDS = _csv_env(
    "ASPEN_SANDBOX_EXCLUDED_COMMANDS",
    "squeue,sacct,sinfo,sstat,sprio,scontrol",
)


# ---------------------------------------------------------------------------
# Slurm job submission (spec §19)
#
# The one capability that spends a *shared* resource, so it is off unless a
# deployment says otherwise: nobody should gain job submission by upgrading.
#
# Two placement rules, both enforced at startup by main._check_state_locations:
#
#   * LEDGER is an AUTHORIZATION INPUT — it decides who may cancel what — so it
#     belongs beside the registry in STATE_DIR, not with the per-project
#     databases under WORKSPACE_ROOT. Same rule as metadata: WORKSPACE_ROOT is
#     what the sandbox produces, STATE_DIR is what steers the agent. A ledger
#     the sandbox can write is a row the agent can forge, and a forged row is a
#     cancel it should not have had.
#   * STAGING_ROOT holds the files a job actually runs from. If sandboxed
#     analysis code could write there, generated Python could plant a script and
#     Aspen would submit it — the cross-tool path that turns two separately
#     fenced tools into one hole.
# ---------------------------------------------------------------------------
JOBS_SUBMIT_ENABLED   = _flag_early("ASPEN_JOBS_SUBMIT_ENABLED", "false")
JOBS_LEDGER           = Path(
    os.getenv("ASPEN_JOBS_LEDGER", str(STATE_DIR / "jobs.sqlite"))
).resolve()
# Job staging: the inputs a job runs from and the outputs it leaves behind.
#
# Under WORKSPACE_ROOT rather than STATE_DIR, and world-readable, because these are
# RESULTS. A run's ORCA output and optimised geometry are the point of submitting
# it, and the first real submission left them somewhere only the bot's account could
# read — so neither the user nor Aspen could get them back.
#
# Safe there, unlike the ledger: the analysis jail bind-mounts only figures/ and
# cache/ read-write, so nothing the agent can run reaches this directory. The
# startup guard still refuses it inside ASPEN_SANDBOX_WRITE_PATHS or the jail's own
# writable binds, which are the paths that would make a planted job script possible.
JOBS_STAGING_ROOT     = Path(
    os.getenv("ASPEN_JOBS_STAGING_ROOT", str(WORKSPACE_ROOT / "jobs"))
).resolve()
# Mode for staging directories. 0755 so colleagues can read and copy results out;
# 0700 if a deployment wants them private to the bot's account.
JOBS_STAGING_MODE     = int(os.getenv("ASPEN_JOBS_STAGING_MODE", "755"), 8)
# The pipeline entry point. A NAME resolved on PATH (or an absolute path), never
# a shell string — the argv is built as a list, so there is no shell to inject
# into. See jobs.build_submit_argv.
JOBS_PIPELINE_BIN     = os.getenv("ASPEN_JOBS_PIPELINE_BIN", "xas-run-batch")
# Directory holding that entry point, prepended to the PATH the orchestrator (and
# therefore every submitted job) runs with. Set this rather than only pinning an
# absolute path above: the pipeline's ORCA script finds `xas-rerun-orca` via
# `command -v`, so pinning just the entry point gives a working submission whose
# failure triage silently never fires. Handled here rather than in start.sh so it
# holds however the bot was launched — including under systemd, where there is no
# start.sh at all.
JOBS_PIPELINE_PATH_DIR = os.getenv("ASPEN_PIPELINE_BIN_DIR", "").strip()
JOBS_SCHEDULER        = os.getenv("ASPEN_JOBS_SCHEDULER", "slurm")
# Caps. The primary threat actor here is the careless allowlisted member and the
# asset is shared compute, so these are Python-enforced, not prompt advice.
JOBS_MAX_STRUCTURES   = int(os.getenv("ASPEN_JOBS_MAX_STRUCTURES", "24"))
JOBS_MAX_ACTIVE_PER_USER = int(os.getenv("ASPEN_JOBS_MAX_ACTIVE_PER_USER", "48"))
JOBS_MAX_ACTIVE_TOTAL = int(os.getenv("ASPEN_JOBS_MAX_ACTIVE_TOTAL", "200"))
JOBS_MAX_SUBMITS_PER_DAY = int(os.getenv("ASPEN_JOBS_MAX_SUBMITS_PER_DAY", "10"))
# Lifetime of a dry-run → confirm token. Single-use, keyed by (thread, user).
JOBS_CONFIRM_TTL      = int(os.getenv("ASPEN_JOBS_CONFIRM_TTL_SECONDS", "900"))
# Wall-clock cap on the orchestrator subprocess itself (it only *submits*; it
# does not wait for jobs to run, so this is generous).
# Raised from 600s after the first real submission, though NOT because submitting
# is slow — it is not; that batch reached the queue in about three minutes. What
# happened is that the orchestrator did not return promptly once it had submitted,
# and the ledger write comes after it returns. The cause is not established, so the
# honest fix is two-sided: a timeout generous enough that a slow-to-exit
# orchestrator is not mistaken for a failure, and `jobs.backfill()` so the job IDs
# are recovered from the pipeline's own log whichever way this goes wrong.
JOBS_SUBMIT_TIMEOUT   = int(os.getenv("ASPEN_JOBS_SUBMIT_TIMEOUT_SECONDS", "3600"))
# Job notifications (aspen/notify.py). A user opts in once and is told when their
# jobs finish — immediately if one fails, since a dependency chain fails late and
# silence for hours is the wrong default. Off means nobody is ever pinged, whatever
# their preference says.
JOBS_NOTIFY_ENABLED   = _flag_early("ASPEN_JOBS_NOTIFY", "true")
# How often the watcher looks, in the two states it can be in. A single interval
# had to be a compromise between "tell me quickly" and "do not poll the accounting
# database every minute forever"; it does not have to be, because the two states
# have entirely different costs. With nothing outstanding a pass is one cheap
# ledger COUNT and no Slurm call at all, so the idle number can stay conservative;
# with jobs in flight the pass is gated by `squeue` (controller memory) and only
# reaches `sacct` (slurmdbd) when something has actually left the queue. See
# jobs.refresh_states.
JOBS_NOTIFY_POLL_SECONDS = int(os.getenv("ASPEN_JOBS_NOTIFY_POLL_SECONDS", "300"))
JOBS_NOTIFY_ACTIVE_POLL_SECONDS = int(
    os.getenv("ASPEN_JOBS_NOTIFY_ACTIVE_POLL_SECONDS", "60"))
# Floor on how often a state refresh may actually talk to Slurm, whoever asks.
# The refresh is now reachable from a conversation (`list_my_jobs` runs one so the
# answer is not simply "as of the last poll"), and a tool the model can call in a
# loop needs a rate limit that does not depend on the model being reasonable.
JOBS_REFRESH_MIN_GAP_SECONDS = int(os.getenv("ASPEN_JOBS_REFRESH_MIN_GAP_SECONDS", "15"))

# Timeout for a single read-only Slurm client call (scontrol/sacct/squeue).
JOBS_SLURM_TIMEOUT    = int(os.getenv("ASPEN_JOBS_SLURM_TIMEOUT_SECONDS", "30"))

# Environment handed to the orchestrator subprocess — and therefore, since
# `sbatch` defaults to --export=ALL, to every compute node.
#
# An ALLOWLIST, not a denylist of secret names: a denylist silently fails to
# cover the next secret someone adds to .env, and the cost of that failure is
# Slack tokens sitting in a job environment on a shared cluster.
#
# NOT --export=NONE, which would be the obvious tightening: the pipeline's ORCA
# job script triages its own failures by calling `xas-rerun-orca`, which it finds
# on an inherited PATH behind a `command -v` guard. Under --export=NONE that guard
# fails and auto-rerun becomes a SILENT no-op — no error, no log line, just runs
# that quietly stop retrying. Scrubbing the parent environment gets the security
# result without paying that. Set ASPEN_JOBS_SBATCH_EXPORT=NONE to override.
JOBS_ENV_BASE         = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG",
                         "LC_ALL", "TERM", "TMPDIR", "TZ")
# Prefixes of site/pipeline variables that must survive scrubbing for the
# pipeline to find ORCA, OpenMPI, its own entry points and its .env.
JOBS_ENV_PREFIXES     = ("PIPELINE_", "XAS_", "SLURM_CONF", "MODULE", "LMOD_")
# Extra names an operator wants passed through (comma-separated). Secrets are
# filtered out regardless of what is listed here — see jobs.submit_env.
JOBS_ENV_PASSTHROUGH  = [
    v.strip() for v in os.getenv("ASPEN_JOBS_ENV_PASSTHROUGH", "").split(",") if v.strip()
]
# Value for SBATCH_EXPORT (defense in depth beside the scrub). Empty = derive it
# from the scrubbed environment's own names.
JOBS_SBATCH_EXPORT    = os.getenv("ASPEN_JOBS_SBATCH_EXPORT", "").strip()

# Names that must NEVER reach a compute node, whatever the allowlist says. Used
# as a belt-and-braces filter in jobs.submit_env and asserted by a contract test.
JOBS_ENV_FORBIDDEN    = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY",
                         "AGENT_INTERNAL_SECRET", "ASPEN_ADMIN_SLACK_USER_ID",
                         "ASPEN_ALLOWED_SLACK_USER_IDS")


# ---------------------------------------------------------------------------
# Runner profiles (aspen/runners.py)
#
# The job scripts a submission runs. Users own their own, exactly like templates:
# Aspen saves one for the person it is talking to after checking it and getting an
# explicit confirmation. The *default* runner (`job_runner` on the registry record)
# stays CLI-only, so an operator's assignment cannot be redirected by a conversation.
# ---------------------------------------------------------------------------
RUNNERS_ROOT          = Path(
    os.getenv("ASPEN_RUNNERS_ROOT", str(STATE_DIR / "runners"))
).resolve()
RUNNERS_HISTORY_ROOT  = Path(
    os.getenv("ASPEN_RUNNERS_HISTORY_ROOT", str(STATE_DIR / "runners_history"))
).resolve()
# Ceilings on what a rendered job script may ask for. A typo in a resource field
# is the "runaway compute" the threat model names.
RUNNER_MAX_NTASKS     = int(os.getenv("ASPEN_RUNNER_MAX_NTASKS", "64"))
RUNNER_MAX_MEM_GB     = int(os.getenv("ASPEN_RUNNER_MAX_MEM_GB", "512"))
RUNNER_MAX_HOURS      = int(os.getenv("ASPEN_RUNNER_MAX_HOURS", "168"))


# ---------------------------------------------------------------------------
# Per-user input templates (aspen/templates.py)
#
# "The way Arun runs a TD-DFT", saved as a reusable artifact. Beside the workflows
# tree and for the same reasons: these are read back later to BUILD A JOB, so a
# sandbox-writable template is the slow-loop injection path §7 describes for
# metadata, with a compute node on the end of it. Startup refuses if either path
# lands somewhere writable.
# ---------------------------------------------------------------------------
TEMPLATES_ROOT         = Path(
    os.getenv("ASPEN_TEMPLATES_ROOT", str(STATE_DIR / "templates"))
).resolve()
TEMPLATES_HISTORY_ROOT = Path(
    os.getenv("ASPEN_TEMPLATES_HISTORY_ROOT", str(STATE_DIR / "templates_history"))
).resolve()
# An input file is data, not prose — but still small.
MAX_TEMPLATE_BYTES     = int(os.getenv("ASPEN_MAX_TEMPLATE_BYTES", "200000"))


# ---------------------------------------------------------------------------
# Input-file validation (aspen/inputs.py)
#
# Aspen may edit a user's quantum-chemistry input freely — functional, basis set,
# geometry, charge, extra blocks. These bound the narrow set of things that turn an
# input from data into a way to run another program, plus two resource numbers
# where a typo becomes a queue-hogging job.
#
# The ORCA block vocabulary is closed (see inputs.py for why) with this as the
# escape hatch: a legitimate block nobody anticipated is a one-line change, not an
# argument. It cannot re-enable a denied block — that check runs afterwards.
# ---------------------------------------------------------------------------
ORCA_EXTRA_BLOCKS     = _csv_env("ASPEN_ORCA_EXTRA_BLOCKS")
ORCA_MAX_NPROCS       = int(os.getenv("ASPEN_ORCA_MAX_NPROCS", "64"))
ORCA_MAX_MAXCORE_MB   = int(os.getenv("ASPEN_ORCA_MAX_MAXCORE_MB", "8000"))
ORCA_MAX_ABS_CHARGE   = int(os.getenv("ASPEN_ORCA_MAX_ABS_CHARGE", "10"))
ORCA_MAX_MULTIPLICITY = int(os.getenv("ASPEN_ORCA_MAX_MULTIPLICITY", "11"))


# ---------------------------------------------------------------------------
# Turn telemetry — one JSON line per turn, so the tool surface and prompt can be
# tuned for the tasks people actually bring. See telemetry.py.
#
# This flag is the operator floor: false means nothing is recorded, whatever the
# state file says. Day-to-day switching (metrics vs. question text, a time-boxed
# collection window, per-person exclusions) lives in TELEMETRY_STATE_FILE and is
# driven by `aspen-users telemetry`, so it hot-reloads without a restart.
#
# Both paths sit under STATE_DIR for the same reason the registry does: inside
# WORKSPACE_ROOT or a sandbox-writable path, generated analysis code could read
# the log or switch off its own recording (guarded in main._check_state_locations).
# ---------------------------------------------------------------------------
# Post the agent's own commentary as it works, instead of holding every word
# until the answer is ready. A turn is mostly tool time, so without this a user
# waits in silence behind a "typing…" indicator and then receives the narration
# retroactively glued to the top of the answer. Set false for a quieter room.
INTERIM_UPDATES       = _flag("ASPEN_INTERIM_UPDATES", "true")

TELEMETRY_ENABLED     = _flag("ASPEN_TELEMETRY", "true")
TELEMETRY_DIR         = Path(os.getenv("ASPEN_TELEMETRY_DIR", str(STATE_DIR / "telemetry"))).resolve()
TELEMETRY_STATE_FILE  = Path(os.getenv("ASPEN_TELEMETRY_STATE_FILE", str(STATE_DIR / "telemetry.json"))).resolve()
# Cap on the recorded question text — a pasted traceback shouldn't bloat the log
# (the true length is kept separately as `chars`).
TELEMETRY_MAX_TEXT    = int(os.getenv("ASPEN_TELEMETRY_MAX_TEXT", "4000"))


# ---------------------------------------------------------------------------
# Registry-backed values (PEP 562 module __getattr__)
#
# ``ALLOWED_USER_IDS`` and ``ADMIN_USER_ID`` used to be module constants computed
# at import. They now resolve through ``registry`` on every read, so a change made
# by ``aspen-users`` takes effect on the offending user's NEXT message instead of
# at the next restart — which is what makes revoking access actually prompt.
#
# A module ``__getattr__`` only fires for names NOT found in the module dict, so:
#   * the three existing call sites (`config.ALLOWED_USER_IDS`, `config.ADMIN_USER_ID`)
#     keep working verbatim — no change needed at the point of use;
#   * ``monkeypatch.setattr(config, "ALLOWED_USER_IDS", ...)`` in the tests sets a
#     real attribute, which shadows this hook, and ``monkeypatch.undo()`` removes
#     it again. The existing test seam is unaffected.
#
# The import is deferred into the function body because ``registry`` imports this
# module at its top level.
# ---------------------------------------------------------------------------
_REGISTRY_BACKED = {
    "ALLOWED_USER_IDS": lambda r: r.allowed_ids(),
    "ADMIN_USER_ID":    lambda r: r.admin_id(),
}


def __getattr__(name):
    getter = _REGISTRY_BACKED.get(name)
    if getter is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import registry
    return getter(registry)
