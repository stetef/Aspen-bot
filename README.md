# Aspen

Aspen is a Slack research assistant for HPC computational chemistry, built for the
**Structural Molecular Biology (SMB) group at the Stanford Synchrotron Radiation
Lightsource (SSRL)**, part of SLAC National Accelerator Laboratory at Stanford
University. The SMB program studies biomolecular and bioinspired systems at the
atomic-to-micron scale using synchrotron techniques (macromolecular crystallography,
SAXS/WAXS, µXRF, XAS/XES). Aspen helps the group explore and analyze calculation results
without leaving Slack.

## What Aspen does

- **Explore results** — browse the calculations tree and read files (`@Aspen what runs
  are under thermolysin?`).
- **Analyze & plot** — runs LLM-generated Python (numpy/pandas/matplotlib/scipy/py3Dmol)
  in a locked-down sandbox and uploads figures to the thread.
- **Hand over files** — attaches data/structure/log files directly to its reply.
- **Record project notes** — updates each project's top-level `metadata.md`.
- **Investigate jobs** — read-only Slurm queries (`squeue`/`sacct`/…). It does **not**
  submit or cancel jobs.

It responds only to `@Aspen` mentions from allowlisted users, keeps per-thread context,
and shows a native "Aspen is typing…" status while working.

## Architecture at a glance

- **`aspen-bot.py` / `aspen/`** — Slack Bolt front-end running the **Claude Agent SDK**
  (via the Claude Code CLI). Exposes a locked-down tool surface; read-only browsing and
  `write_metadata` run in-process.
- **`tool_server.py`** — a localhost-only FastAPI service that executes analysis code in a
  **bubblewrap (bwrap)** jail: no network, read-only project mount, only `figures/` and
  `cache/` writable, scrubbed environment, and `prlimit` resource caps. It also owns
  caching, metadata parsing, the per-project SQLite index, and audit logging.

Aspen is SDK-only (the older direct Anthropic Messages-API backend was removed), and the
analysis sandbox is bwrap (it replaced Apptainer, which couldn't enforce rootless memory
limits on this cgroups-v1 host).

The only place Aspen can write is each project's `metadata.md` and the sandbox's
`figures/`/`cache/` — all calculation inputs, outputs, and data stay read-only.

See [`spec.md`](spec.md) for the full design, security model, and roadmap.

## Quickstart (development mode)

Requirements: Python ≥ 3.11, `bubblewrap` (and `socat` for the optional Bash OS-sandbox).
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
