<div align="center">

<img src="assets/aspen.jpeg" alt="Aspen" width="200" height="200" />

# Aspen

**A Slack research assistant for HPC computational chemistry.**

*Explore, analyze, and plot your calculation results — without leaving Slack.*

[![Tests](https://github.com/stetef/Aspen-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/stetef/Aspen-bot/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Built with Claude Agent SDK](https://img.shields.io/badge/built%20with-Claude%20Agent%20SDK-d97757.svg)](https://github.com/anthropics/claude-agent-sdk-python)

</div>

---

Aspen is built for the **Structural Molecular Biology (SMB) group at the Stanford
Synchrotron Radiation Lightsource (SSRL)**, part of SLAC National Accelerator Laboratory
at Stanford University. The SMB program studies biomolecular and bioinspired systems at
the atomic-to-micron scale using synchrotron techniques (macromolecular crystallography,
SAXS/WAXS, µXRF, XAS/XES). Aspen brings that group's calculation results into the
conversation — browse the tree, run the analysis, and get the figure back, all in a
thread.

## What Aspen does

- **Explore results** — browse the calculations tree, read files, and search file
  contents (`@Aspen what runs are under thermolysin?`, `@Aspen which runs logged "SCF
  not converged"?`).
- **Analyze & plot** — runs LLM-generated Python (numpy/pandas/matplotlib/scipy/py3Dmol)
  in a locked-down sandbox and uploads figures to the thread.
- **Hand over files** — attaches data/structure/log files directly to its reply.
- **Record project notes** — keeps its own notes per project, stored outside the
  calculations tree (it never writes into anyone's files).
- **Work the way you work** — each user can keep a **workflow file** (their own notes on
  functionals, what they check, what they extract). Aspen reads yours before planning a
  calculation, and can show you a colleague's when you want to borrow their approach.
- **Everyone's calculations, by name** — each person can have their own tree, plus shared
  group trees. Yours is the default; a colleague's is `@arun/thermolysin`. Reading is flat,
  as on the shared filesystem, so "who else has run this?" is a normal question.
- **Ask, without knowing who to ask** — turned away at the door, or want your own
  calculations directory? Aspen files the request and DMs the admin the exact command.
- **Show itself off** — anyone can DM `DEMO` for a guided walkthrough over fabricated
  data, without being added as a user ([below](#try-it-without-being-a-user-demo)).
- **Investigate jobs** — read-only Slurm queries (`squeue`/`sacct`/…) across the whole
  queue, whoever it belongs to.
- **Run calculations** *(beta, off by default)* — submit an ORCA → CORVUS batch and cancel
  it again. Always a dry run first, then an explicit confirmation. It can only cancel jobs
  **it submitted for you** — never your own hand-submitted jobs, never a colleague's
  ([below](#running-calculations-beta)).

It responds to `@Aspen` mentions from allowlisted users, keeps per-thread context, and
shows a live status naming what it's doing right now ("Aspen is reading orca.out…",
"Aspen is running squeue…") as it works. It works in channels, its own
1:1 DM, and **group DMs** — in a group DM, *every* human member must be allowlisted or
Aspen politely declines (the "participant gate"). The first allowlisted ID is treated as
the admin and is named in refusals so users know who to ask to be added. (Slack doesn't
allow a bot inside an existing 1:1 human DM — make a group DM that includes Aspen instead.)

You only need to `@Aspen` to *start* a conversation: in its 1:1 DM every message reaches
it, and in a group-DM thread it began (via a mention) it picks up your plain replies too,
so a back-and-forth doesn't need a mention every turn. Other messages in a channel or
group DM (not in a thread Aspen is already in) still require an `@Aspen`.

## Try it without being a user (`DEMO`)

DM Aspen the single word **DEMO** and it walks you through the whole arc: being
turned away, the request that reaches the admin, being welcomed, saving a workflow,
browsing some calculations, and getting a plot back — against fabricated data in
[`examples/demo-calculations/`](examples/demo-calculations/), without being added
as a user and without touching anything real.

Three properties make it safe to offer to a whole workspace:

- **Scope isolation.** A demo session can read the demo tree and nothing else. Real
  roots aren't listed, aren't addressable by name, and can't be reached by traversal
  or by a cross-root search. This is the one place the "everyone reads everyone" rule
  does *not* apply — a visitor isn't a group member.
- **Nothing is written.** No registry entry, not even a temporary one. The workflow
  and notes you "save" live in memory for the session and are then gone.
- **Nobody is paged.** The admin request is rendered *into the thread* — the card
  your admin would have received, so you can see and approve it — rather than sent.

Slurm commands are switched off inside a demo — they would query the real cluster, and
everything else in the walkthrough is fabricated. The tour says so and describes what
`squeue` would show instead, alongside the other capabilities it didn't have time for.

It still costs model time, so it's capped per session, per day across everyone, and
by the ordinary rate limiter. Switch it off with `ASPEN_DEMO_ENABLED=false`.

## Requesting access

Aspen only answers allowlisted users. If you're not on the list yet, it will reply
with a short refusal and these same steps. To get added:

1. **Copy your Slack member ID.** In Slack, click your name or profile picture →
   **View full profile** → the **⋮ More** button → **Copy member ID**. It looks like
   `U01AB2CD3EF` (not your `@handle`, which can change).
2. **Message Aspen anyway.** Being turned away files the request for you: Aspen DMs
   the admin your name and ID with the command to approve it. It also @-mentions the
   admin in the refusal, so you know who to nudge if nothing happens.

For **group DMs**, *every* human member must be allowlisted — so anyone in the room
who isn't yet approved needs to do the same. Until then, approved users can DM Aspen
directly.

## Managing users (`aspen-users`)

Who may talk to Aspen lives in a **user registry** (`$ASPEN_STATE_DIR/users.json`, default
`~/.aspen/users.json`) that maps each Slack member ID to a human **alias**, so you can
manage people by name instead of by `U01AB2CD3EF`. Changes take effect on the affected
user's **next message — no restart**.

The registry is written *only* by this CLI. Aspen has no tool and no Slack command that
adds or removes a user, so no message — however phrased — can widen the allowlist.

```bash
./aspen-users init                                   # first run: migrate the .env bootstrap
./aspen-users list                                   # who's registered
./aspen-users list --all                             # include removed users
./aspen-users add U01AB2CD3EF --alias arun --name "Arun N."
./aspen-users add U01AB2CD3EF --notes beta           # alias/name looked up from Slack
./aspen-users rename arun --to arun-n                # also renames their folder
./aspen-users remove arun                            # revoke + archive their workflow
./aspen-users remove arun --purge                    # revoke + delete their workflow
./aspen-users sync --apply                           # refresh display names from Slack
./aspen-users whois arun                             # one user's full entry
./aspen-users workflow import arun notes.md          # file a document as their workflow
./aspen-users workflow list                          # every workflow's description
```

| Command | Arguments | Notes |
|---|---|---|
| `init` | — | Turns the `.env` bootstrap into a real registry (names looked up from Slack, first ID kept as admin). Runs automatically on the first `add`/`rename`/`remove`; this just does it deliberately. |
| `list` | `--all` | `--all` includes removed users. Flags column marks admin / workflow. |
| `add` | `<slack_id>` `--alias` `--name` `--role {member,admin}` `--notes` | Alias defaults to a kebab-case slug of their name; the name is looked up from Slack if omitted. Re-running `add` reinstates a removed user. |
| `rename` | `<alias\|id>` `--to <new-alias>` | Aliases are cosmetic — lookups go by Slack ID, so this never breaks anything. |
| `remove` | `<alias\|id>` `--purge` `--purge-history` `--force` `-y/--yes` | Archives the workflow by default. `--force` is required to remove the admin. |
| `sync` | `--apply` | Dry run by default: reports display-name changes and aliases that no longer match. |
| `whois` | `<alias\|id>` | Registry entry plus the workflow path. |
| `workflow` | `import`, `describe`, `list`, `show` | File and maintain workflow documents on someone's behalf — see [Per-user workflows](#per-user-workflows). |
| `set-root` | `<alias\|id>` `<path>` `--clear` `--unix-user` | Point someone at their own calculations tree — see [Whose calculations](#whose-calculations). Validated when you set it. |
| `roots` | — | Every calculations root and whether it is actually readable. |
| `requests` | `--clear` | Who has asked for access or a root, with the command that grants it. |
| `setup` | `<alias\|id>` `--reset <item>` | Who has a workflow / their own root, and who declined. |
| `telemetry` | `status`, `on`, `off`, `content on\|off`, `exclude`, `include`, `prune` | What Aspen records about how it's used — see [What Aspen records](#what-aspen-records-aspen-users-telemetry). |

Global: `--by <name>` records who made the change in `added_by`/`removed_by` (defaults to
your Unix user).

Until `users.json` exists, the allowlist falls back to `ASPEN_ALLOWED_SLACK_USER_IDS` in
`.env` — that bootstrap is what keeps a fresh install (or a corrupt registry) from locking
the admin out. The first write migrates those IDs into the registry (announced, with names
resolved from Slack) and the registry takes over; nobody loses access in the process.

## Whose calculations

Each person can have their own calculations tree, and there can be any number of **shared**
trees for group project data. Anyone without their own reads from `CALCULATIONS_ROOT`, so
until you set a single root, nothing changes.

```bash
./aspen-users roots                                  # every root, and is it readable?
./aspen-users set-root arun /sdf/home/a/arun/calcs   # give someone their own
./aspen-users set-root arun --clear                  # back to the shared default
./aspen-users whois arun                             # shows which root they read from
```

Shared trees are named in `.env`: `ASPEN_SHARED_CALC_ROOTS=smb=/data/smb-shared`.

**Everyone may read everyone.** That mirrors the shared filesystem, where the files are
already world-readable — so asking "how did Arun set up that geometry optimisation?" or
"who else has run this?" just works. What ownership decides is whose files an unqualified
path means, and who may write. In Slack, Aspen reads your own tree by default and reaches
someone else's by name (`@arun/dft/orca.out`), and it says whose files an answer came from.

**Aspen writes nothing into any calculations tree** — not yours, not anyone's. Its one
write is its own project notes, which live in `$ASPEN_STATE_DIR/metadata/`, mirroring each
root's layout. Notes on your own and on shared projects are yours to change; a colleague's
are readable but theirs.

**Your projects need no metadata file.** Aspen reads and analyses a project directory with
nothing in it but calculations. If one of yours has no `README` and no notes on Aspen's
side, it will offer — once, and only if the conversation gives it an opening — to draft a
`README.md` from what it just read there: the real run names, the real filenames. You save
it yourself, because Aspen cannot write into your tree. Plain sentences are fine; there is
no format to get right, and `examples/README.example.md` shows the shape. If you would
rather not keep a file, ask Aspen to hold the same notes on its side instead — and if you
want neither, say so once and it stops offering.

`set-root` validates when you type it: the path must exist, be readable **by the account
Aspen runs as**, and never contain (or sit inside) another root — containment is how the
security fence works, so a nested root would silently enclose someone else's files. Aspen
re-checks at startup and refuses to boot if a root has since gone missing or unreadable.

### Requests, and being asked once

Aspen cannot add a user or set a root — those stay CLI-only so that no message can widen
access. It can, however, do the paperwork: someone turned away at the door has an access
request filed automatically, and Aspen DMs you the exact command. Same for a root, when
someone tells it where their calculations live.

```bash
./aspen-users requests            # who's waiting, and the command for each
./aspen-users setup               # who has a workflow / a root, and who declined
./aspen-users setup pi --reset calc_root   # ask them again after all
```

Nothing in that queue grants anything — running the printed command does.

On the other side, Aspen offers to set these up **once per thread, one item at a time**,
and only when the conversation gives it an opening. If someone says no, it records that
and never asks again. Declining a calculations root also says something true about that
person: they read colleagues' work rather than running their own, so Aspen stops guessing
whose files an unqualified path means and asks instead.

## Per-user workflows

Every user can keep a **workflow file** — their own notes on how they run and interpret
calculations (favored functionals and basis sets, what they check and in what order, what
they pull out of an output). Aspen reads yours before planning work it covers, and can
read a colleague's when you ask how they'd do something.

```
$ASPEN_STATE_DIR/workflows/
  arun__U01AB2CD3EF/WORKFLOW.md    # <alias>__<slack-id> — resolution is by ID
  _group/WORKFLOW.md               # shared house style (admin-writable)
  _archive/priya__U0PRIYA/         # removed users' knowledge is kept, not deleted
```

The easiest way to create one is to tell Aspen: paste your notes into a DM and ask it to
save them as your workflow. It writes the frontmatter, shows you the file, and saves on
your say-so. See [`examples/WORKFLOW.example.md`](examples/WORKFLOW.example.md) for the
format — the one-line `description:` is the important part, since that is what gets
indexed into every conversation.

Aspen can only ever write **your own** workflow: the target comes from the Slack event,
not from anything in the conversation. To adopt a colleague's approach, Aspen reads
theirs, adapts it with you, and saves it to yours with `derived_from` recorded. Someone
else's workflow is delivered to the model marked `trust="reference-only"` — material to
quote and adapt, explicitly not instructions to follow — because a workflow file is
user-authored text and is treated as untrusted, exactly like project data.

Every overwrite snapshots the previous version to
`$WORKSPACE_ROOT/workflow_history/<slack-id>/`.

### Filing one for someone (`aspen-users workflow`)

When someone hands you a document instead of typing it to Aspen, file it for them:

```bash
./aspen-users --by you workflow import arun ArunDFTWorkflow.md   # <alias|id|_group> <file>
./aspen-users workflow import arun notes.md --description "…"    # skip the drafting step
./aspen-users workflow describe arun "…"                         # rewrite just that line
./aspen-users workflow list                                      # every description, plus who has none
./aspen-users workflow show arun                                 # one file in full
```

The **body is filed verbatim** — it becomes that person's standing guidance under their
name, so it is stored, never reformatted. The only authored field is the one-line
`description`, and `import` settles it in that order: `--description` if given, else one
already in the file, else a draft from the Claude CLI that it shows you and files only on
an explicit yes (`--no-draft` skips the call; an unreachable CLI is a warning, not a
failure). Because the description is the whole routing signal, `describe` exists to sharpen
it later without re-importing.

Filing for someone is not writing as them: `owner_id` stays the user, while `--by` lands in
`updated_by`. Overwrites confirm first and snapshot as usual, `--archive-source` renames the
original aside so it can't drift from the filed copy, and the user must already be
registered — there's nowhere to file it otherwise.

## Running calculations (beta)

Off by default. Turn it on with `ASPEN_JOBS_SUBMIT_ENABLED=true` once you have read
[spec §19](spec.md#19-slurm-job-submission-beta) — in particular §19.9, on what the beta
account model does *not* fix.

In Slack it is deliberately two steps:

```
you    @Aspen run the CA-fixed pipeline on thermolysin/structures
Aspen  DRY RUN — 6 structures (a.xyz, b.xyz, …), ca-fixed template. Nothing submitted yet.
       Want me to go ahead?
you    yes
Aspen  Submitted batch 9f2c… — 18 scheduler jobs queued.
```

Cancelling works the same way, with a preview first. The confirmation is enforced in
Python, not requested in the prompt: the first call mints a single-use token bound to that
user and thread, and only a call carrying it acts. A model can be talked out of asking a
question; it cannot mint a token it was never given.

**What it can cancel.** Exactly the jobs Aspen submitted *for the person asking*. Not your
own hand-submitted jobs, not a colleague's, not anything else in the queue. Every job ID is
checked against live Slurm before anything is cancelled, and a job whose working directory
is not inside that user's staging area is refused. That check is why your own research jobs
are safe even though Aspen currently runs under the same Unix account you do — they
structurally cannot pass it. Reading stays flat: Aspen can *see* the whole queue.

**What the model never does.** It doesn't write the job script. It picks a `template_mode`
from a fixed list (`ca-fixed`, `h-only`, `single-point`, `free`, `backbone`, `xtb-free`,
`xtb-constrained`, `quick`, `quick-ca-fixed`) and the pipeline generates everything from its
own templates. Structures are *copied* into a staging tree — nothing is written inside
anyone's calculations directory, as everywhere else in Aspen.

### Operator commands

```bash
./aspen-users jobs list              # active Aspen jobs, all users
./aspen-users jobs list arun         # just one person's
./aspen-users jobs show <batch-id>   # one batch: inputs, argv, its jobs
./aspen-users jobs cancel arun       # cancel someone's, same verification as the agent
./aspen-users jobs panic             # cancel every active Aspen job (still per-ID)
./aspen-users jobs reconcile         # fill in what jobs actually consumed, from sacct
```

`panic` is the 2 a.m. button. It stays per-job-ID rather than using `scancel -u`, so it
cannot take out your own research jobs alongside Aspen's.

`reconcile` is worth running periodically: the ledger knows *who submitted what*, but
elapsed time, CPU-hours and exit state only exist once a job has finished, so
"who used the compute" is unanswerable until you join against `sacct`.

## What Aspen records (`aspen-users telemetry`)

Aspen keeps a **turn log** — one JSON line per message — so its tools, prompt and
workflows can be tuned for the tasks people actually bring it. It lives at
`$ASPEN_STATE_DIR/telemetry/YYYYMMDD.jsonl`, `0600`, readable only by the account Aspen
runs as.

Two things are switched separately, because they have different useful lifetimes:

- **Metrics** — who, when, which tools in what order, latency, outcome, tokens and cost.
  Small and worth keeping: it's what shows which questions hit the per-turn round limit,
  which commands the allowlist is refusing, and what a turn costs.
- **The question text** — what you need to work out *what* people ask. Collect it for a
  few weeks, then switch it off; `--days`/`--until` set a window that closes by itself.

| Command | Effect |
|---|---|
| `telemetry status` | What is being recorded right now, and why |
| `telemetry content on --days 30` | Collect question text for 30 more days, then stop |
| `telemetry content off` | Metrics only |
| `telemetry exclude <alias\|id>` | One person's text is never recorded (metrics stay) |
| `telemetry off` | Record nothing at all |
| `telemetry prune --older-than 90` | Delete old daily logs |

Changes apply on the next message, no restart. Turning text off **narrows** a record
rather than dropping it — the line is still written with `"text": null`, `"redacted":
true` and the character count — so volume, latency and failure-rate series stay unbroken.
The text of people who *aren't* on the allowlist is never recorded at all, whatever the
setting: they haven't agreed to use Aspen. `ASPEN_TELEMETRY=false` in `.env` is a hard
kill switch that overrides all of the above.

The log and its switch sit under `ASPEN_STATE_DIR` for the same reason the registry does:
outside the workspace and every sandbox-writable path, so generated analysis code can
neither read the log nor stop its own recording. Aspen refuses to start if that's violated.

Because Aspen runs on a **subscription seat**, there is no per-turn dollar cost to record.
The spend signals are tokens per user, plus the CLI's own rate-limit meter — `utilization`
(the fraction of the window consumed), its status, and when it resets. That meter is
account-wide, so it says *how much is left*, not *who spent it*; the join is against the
per-user token columns on the same rows.

## Seeing the usage (`aspen-dashboard`)

```
./aspen-dashboard             # against the real log
./aspen-dashboard --demo      # synthetic data, to judge the layout before real traffic
```

Volume and tokens per person over time, the rate-limit meter, a per-person table, the tool
histogram and repeated tool sequences, the failure panel (including the commands the
allowlist refused — a feature backlog), a latency breakdown separating Aspen's own overhead
from model time from tool time, and a searchable list of the questions themselves.

It is **read-only**, gets **its own venv** (a web framework has no place in the dependency
tree of the running Slack service), and is **pinned to `127.0.0.1`** — on a shared login
node Streamlit's default bind would publish colleagues' questions to every account on the
machine, undoing the `0600` on the log file. Reach it over an SSH tunnel; the launcher
prints the exact command. First run builds the venv from `dashboard-requirements.txt`.

## Architecture at a glance

- **`aspen-bot.py` / `aspen/`** — Slack Bolt front-end running the **Claude Agent SDK**
  (via the Claude Code CLI). Exposes a locked-down tool surface; the read/search/browse
  tools and `write_metadata` run in-process and are path-fenced to the calculations root.
- **`tool_server.py`** — a FastAPI service reached over a **Unix-domain socket** (a file
  in a `0700` dir, not a TCP port — unreachable by other users on a shared node) that
  executes analysis code in a **bubblewrap (bwrap)** jail: no network, read-only project
  mount, only `figures/` and `cache/` writable, scrubbed environment, a **seccomp syscall
  denylist**, and `prlimit` resource caps. It also owns caching, metadata parsing, the
  per-project SQLite index, and audit logging.

Aspen is SDK-only (the older direct Anthropic Messages-API backend was removed), and the
analysis sandbox is bwrap (it replaced Apptainer, which couldn't enforce rootless memory
limits on this cgroups-v1 host).

- **`aspen/registry.py` + `aspen/workflows.py`** — the user registry (Slack ID ↔ alias,
  and the admission allowlist, hot-reloaded from `users.json`), the per-user workflow
  store, Aspen's project notes, and the pending-request queue. All live under
  `ASPEN_STATE_DIR`, deliberately outside the repo *and* outside any sandbox-writable
  path; the bot refuses to start if that's violated. Notes get that treatment for a second
  reason: they are read back into the model's context later, so a sandbox-writable copy
  would be a slow-loop injection path.
- **`aspen/telemetry.py`** — the turn log and its switch, under `ASPEN_STATE_DIR` by the
  same rule. Written by the bot, configured only by `aspen-users telemetry`; never
  reachable from a tool, a prompt, or a Slack message.

**Aspen writes nothing inside any calculations root** — not yours, not a colleague's, not
a shared one. Its only writes are its own per-project notes and the speaking user's own
workflow file, both under `ASPEN_STATE_DIR` with prior versions snapshotted, plus the
sandbox's `figures/`/`cache/`. It also cannot grant anything: admission and calculations
roots are set only by `aspen-users`, so the most a conversation can do is file a request
for a human to approve.

See [`spec.md`](spec.md) for the full design, [`THREAT_MODEL.md`](THREAT_MODEL.md)
for the threat model, security measures, and the service-account cutover checklist,
and [`SLACK_SETUP.md`](SLACK_SETUP.md) for the step-by-step Slack app setup (with an
importable [`slack-app-manifest.yaml`](slack-app-manifest.yaml)) — for reinstalling
or cloning Aspen into another workspace.

## Quickstart (development mode)

Requirements: Python ≥ 3.11, `bubblewrap` (and `socat` for the optional Bash OS-sandbox).
The analysis jail's seccomp filter uses `pyseccomp` + `libseccomp` (pulled in by
`requirements.txt`; the jail still runs, unfiltered and logged, if they're absent).
`uv` is used to build the analysis venv if present.

```bash
cp .env.example .env        # fill in Slack tokens, allowlist (your user ID only), paths
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
bash start.sh               # builds the analysis venv, starts the tool server + bot
```

Run from a `screen`/`tmux` session so it survives disconnects. `start.sh` builds the
analysis venv (numpy/pandas/matplotlib/scipy/py3Dmol from `analysis-requirements.txt`) on
first launch — that needs network and takes a few minutes once.

### Configuration

All paths, tokens, limits, and sandbox settings come from `.env` — see `.env.example` for
the annotated list and [`spec.md` §15](spec.md#15-environment-variables-env) for details.
By default the Claude Code CLI authenticates with the Claude Code login; set
`ASPEN_SDK_USE_SUBSCRIPTION=false` to use `ANTHROPIC_API_KEY` instead.

## Tests

```bash
pytest -q
```

A hermetic suite — no live Slack, Claude CLI, or network needed.

## Status

Aspen is implemented and running in **developer mode** (under a personal account). Job
submission is **built and in beta** ([above](#running-calculations-beta)); what remains on
the [roadmap](spec.md#18-roadmap--not-yet-implemented) is the production **service account
+ systemd** deployment. That last item is what makes submission safe to open up beyond a
small trusted group: until it lands, a submitted job runs unjailed as the account Aspen
runs under. Keep the registry small.

---

<div align="center">

Built for the <strong>SMB group</strong> at <strong>SSRL · SLAC · Stanford</strong>

</div>
