<div align="center">

<img src="assets/aspen.jpeg" alt="Aspen" width="200" height="200" />

# Aspen

**A Slack research assistant for HPC computational chemistry.**

*Explore, analyze, and plot your calculation results — without leaving Slack.*

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
- **Record project notes** — updates each project's top-level `metadata.md`.
- **Investigate jobs** — read-only Slurm queries (`squeue`/`sacct`/…). It does **not**
  submit or cancel jobs.

It responds to `@Aspen` mentions from allowlisted users, keeps per-thread context, and
shows a native "Aspen is typing…" status while working. It works in channels, its own
1:1 DM, and **group DMs** — in a group DM, *every* human member must be allowlisted or
Aspen politely declines (the "participant gate"). The first allowlisted ID is treated as
the admin and is named in refusals so users know who to ask to be added. (Slack doesn't
allow a bot inside an existing 1:1 human DM — make a group DM that includes Aspen instead.)

You only need to `@Aspen` to *start* a conversation: in its 1:1 DM every message reaches
it, and in a group-DM thread it began (via a mention) it picks up your plain replies too,
so a back-and-forth doesn't need a mention every turn. Other messages in a channel or
group DM (not in a thread Aspen is already in) still require an `@Aspen`.

## Requesting access

Aspen only answers allowlisted users. If you're not on the list yet, it will reply
with a short refusal and these same steps. To get added:

1. **Copy your Slack member ID.** In Slack, click your name or profile picture →
   **View full profile** → the **⋮ More** button → **Copy member ID**. It looks like
   `U01AB2CD3EF` (not your `@handle`, which can change).
2. **Send that ID to the admin** and ask to be added to the approved-users list. The
   admin is the first ID in `ASPEN_ALLOWED_SLACK_USER_IDS` (or `ASPEN_ADMIN_SLACK_USER_ID`
   if set), and Aspen @-mentions them in every refusal so you know who to ask.

For **group DMs**, *every* human member must be allowlisted — so anyone in the room
who isn't yet approved needs to do the same. Until then, approved users can DM Aspen
directly.

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

The only place Aspen can write is each project's `metadata.md` (the prior version is
snapshotted first) and the sandbox's `figures/`/`cache/` — all calculation inputs,
outputs, and data stay read-only.

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
the annotated list and [`spec.md` §13](spec.md#13-environment-variables-env) for details.
By default the Claude Code CLI authenticates with the Claude Code login; set
`ASPEN_SDK_USE_SUBSCRIPTION=false` to use `ANTHROPIC_API_KEY` instead.

## Tests

```bash
pytest -q
```

A hermetic suite — no live Slack, Claude CLI, or network needed.

## Status

Aspen is implemented and running in **developer mode** (under a personal account). Two
things remain on the [roadmap](spec.md#16-roadmap--not-yet-implemented): a production
service account + systemd deployment, and letting the agent submit/manage its own
Slurm/PBS jobs (the ORCA → CORVUS pipeline). Today its scheduler access is read-only.

---

<div align="center">

Built for the <strong>SMB group</strong> at <strong>SSRL · SLAC · Stanford</strong>

</div>
