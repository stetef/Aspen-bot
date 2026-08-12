# Aspen — Threat Model & Security Measures

_Last updated: 2026-08-12. Scope: the system **as built** after the
`security/interim-hardening` pass, the group-DM participant gate (C7), the user
registry + per-user workflows (C8–C11), and **Slurm job submission in beta**
(C12–C14, spec §19), plus the security work still owed (notably the move to a
dedicated service account). Companion to [`spec.md`](spec.md) (design)
and [`probe_isolation.sh`](probe_isolation.sh) (host-fact verification)._

This document records **why** Aspen is locked down the way it is: the context it
runs in, what we're protecting, who from, the controls in place, the risks we
knowingly accept for now, and the security work to do at the service-account
cutover. It is deliberately opinionated about reasoning so the next reader can
tell a deliberate decision from an accident.

---

## 1. Context (what shapes the model)

| Dimension | Reality | Consequence |
|---|---|---|
| Users | The SMB/SSRL research group, via a **SLAC-managed (SSO) Slack** workspace | The user-ID allowlist is a *strong* auth gate; spoofing/external entry is low-risk. Multi-user rooms (group DMs) are gated so the allowlist also bounds *who can read along*, not just who can invoke (C7) |
| Data sensitivity | **Public / publishable** computational chemistry | Confidentiality is low priority; **integrity & availability** matter more |
| Host | A **shared multi-user login node** (`sdfiana*`) | `127.0.0.1` ports and the bot's files are reachable/visible to other lab users |
| Bot identity (interim) | Runs as a **developer's personal Unix account** with SSH keys + munge (job submission) + group data access | If any confinement is bypassed, the blast radius is that whole cluster identity — this is the dominant interim risk, fixed only by the service account (§7). It also **inverts** the cancel boundary: the developer's own hand-submitted jobs are inside the bot's `scancel` range, so C12's checks are the only thing protecting them, not defense in depth |
| Insider trust | Defend against the **careless** allowlisted member, not a malicious one | Favor guardrails, backups, and fenced tools over hard inter-user isolation |

## 2. Assets (ranked)

1. **Secrets on the node** — Slack tokens, `AGENT_INTERNAL_SECRET`, the Claude
   Code login (`~/.claude`), the account's SSH key, the munge credential.
   *(Highest: theft = impersonate Aspen, lateral movement, spend compute.)*
2. **Integrity & availability of calculation data** (public, so not secrecy —
   don't corrupt or destroy it).
3. **Cluster compute / fair-share** — now a **live** asset, not a conditional one:
   the agent submits jobs (spec §19). Ranked here rather than higher because a
   runaway batch is recoverable and visible; note it is the only asset whose
   damage is *shared with the whole group* rather than confined to Aspen.
4. **Bot availability & answer correctness.**
5. Data confidentiality — **low** (public), with a mild pre-publication caveat.

## 3. Trust boundaries

- **Slack → bot** — Socket Mode, outbound WebSocket only; auth = SSO-backed
  user-ID allowlist. Strong. In a **group DM**, the mentioner check is not enough
  (every member reads the replies and the thread context feeds the model), so a
  **participant gate** additionally requires *every human member* to be allowlisted,
  fail-closed (C7). 1:1 human DMs can't contain a bot, so there's no equivalent
  exposure there.
- **Bot → tool server** — a **Unix-domain socket** in a `0700` directory (+ a
  shared-secret header). On a shared node this replaces a loopback TCP port that
  every local user could reach.
- **Tool server → analysis code** — the **bwrap jail + seccomp filter**: the real
  isolation boundary for untrusted, LLM-generated Python.
- **Bot's Unix account → rest of cluster** — *not a boundary Aspen enforces*; it
  is the blast radius. Shrinking it is the service account's job (§7).
- **Untrusted project text → model context** — prompt-injection boundary. Project
  metadata/files are untrusted input; the security boundary is always the jail and
  the Python-enforced tool limits, **never** the prompt.
- **Non-user → the agent (DEMO)** — the one path that deliberately runs *before*
  the allowlist gate, so the boundary cannot be identity; it is **scope**. A demo
  session resolves to the demo root and nothing else, enforced in **three**
  places: `roots.resolve`, `tools._distinct_scopes` (the cross-root sweep reads
  the roster directly and escaped the first fence during development), and
  `tool_server.resolve_scope` (a separate process with no view of demo sessions,
  which resolved a visitor to the real `PROJECTS_ROOT` — on the one path that
  *executes* code — until the bot began marking the request `demo: true` from
  its own session state).
  **Both escapes were the same shape:** a code path that reads the roster or the
  registry directly instead of going through the fence. Worth checking for
  explicitly when adding the next one — scope isolation is not a single
  chokepoint, and nothing tells you when one was missed.
  It writes nothing, not even a temporary registry entry, so admission stays "no
  message can widen the allowlist". Reviewing that boundary means reviewing
  `tests/test_demo.py` and the DEMO section of `tests/test_tool_server_roots.py`:
  every negative case is asserted against the real tool surface rather than
  against `demo.py` alone.
- **One user's authored text → another user's session** — per-user workflow files
  are user-written text *intended to be followed*, and are readable across users
  for knowledge transfer. Someone else's file is delivered `trust="reference-only"`
  (C10), but as above the enforcing boundary is the tool surface: workflow text
  cannot reach any action a plain Slack message couldn't.

## 4. Threat actors

- **Careless allowlisted member** *(primary)* — accidental destructive request,
  wrong-project metadata overwrite, runaway compute.
- **Other unprivileged users on the shared node** — could read secrets if file
  perms are loose; could reach a loopback service; could fill shared disk.
- **Hijacked Slack account** — mitigated by SSO, but a live session acts as that
  user.
- **Prompt-injection via project data** — content in the calculations tree trying
  to steer the agent. Bounded by the jail + fenced tools.
- **Prompt-injection via a colleague's workflow file** — a member (or anyone who
  could plant a file in the workflows tree) writing directives aimed at other
  users' sessions. Bounded by C9–C11 and the same fenced tools; the `0700` state
  dir keeps non-members from planting files out-of-band.
- **Unauthenticated workspace member (DEMO)** *(new surface)* — anyone in the
  Slack workspace can reach the agent by DMing `DEMO`, ahead of the allowlist
  gate. Bounded by demo scope isolation (§5), and note what it is *not* bounded
  by: they are not on the allowlist, so nothing about their identity restricts
  them — only the code path does.
- **External attacker** — minimal direct surface (no inbound ports); realistic
  path is secret theft or supply chain.
- **Admins / root / backups** — trusted, out of scope (but note they *can* read
  everything, including `.env`).

## 5. Architecture & enforcement points

Two single-instance processes:

- **bot** (`aspen/`) — Slack front-end + Claude Agent SDK. Serves the read/search
  /metadata/workflow tools **in-process**; the only outbound tool call is the
  analysis bridge to the tool server. Also appends the per-turn telemetry log
  (`aspen/telemetry.py`), which no tool can read, write, or switch off — it is
  configured only by `aspen-users telemetry`, from outside the agent, like admission.
- **tool server** (`tool_server.py`) — runs LLM-generated analysis code in the
  bwrap jail; owns caching, metadata parsing, the SQLite index, audit logging.

**The agent's tool surface, and how each is bounded:**

| Tool | Bound by |
|---|---|
| `list_directory`, `read_file`, `search_files`, `attach_file` | In-process Python, **path-fenced to the calculations root** (`_safe_path`); cannot read outside it |
| `write_metadata` | One file, in Aspen's own sidecar (`$ASPEN_METADATA_ROOT/<scope>/<project>/metadata.md`) and never in a calculations tree; prior version snapshotted first |
| `read_workflow` | In-process, path-fenced to the workflows root; another user's file is tagged `trust="reference-only"` (C10) |
| `write_workflow` | Only the **speaking user's own** file — the destination is the Slack event's `user_id`, and the tool has no owner parameter (C9); `_group` gated on the admin; prior version snapshotted |
| `run_python_analysis` | The **bwrap jail + seccomp** (no network, read-only project mount, only `figures/`+`cache/` writable, `prlimit` caps) |
| `Bash` | A **Slurm read-only allowlist** + `can_use_tool` deny; everything else refused. `sbatch`/`scancel` are **not** on it and never will be — a prefix rule cannot express "only this user's own jobs", and `Bash(sbatch:*)` grants `--wrap` |
| `submit_orca_batch` | A fixed `template_mode` enum + a root-resolved structure path — **no script path or body** (C13); inputs copied into staging outside every writable area; env scrubbed (C14); per-user/global caps; dry-run + single-use confirm token |
| `cancel_orca_batch` | Ledger enumeration filtered to the Slack event's `user_id`, then per-ID `WorkDir` verification against live Slurm, then explicit-ID `scancel` (C12) |

**The "two sandboxes" distinction** (a common confusion):

- **bwrap analysis jail** — *our* code; wraps `run_python_analysis` only. Always on.
- **Claude Code Bash OS sandbox ("Sandbox B")** — would wrap the agent's `Bash`
  tool only (not the in-process tools). Currently **off** — see §8 for why, and
  §7 for when it should be turned on.

## 6. Controls implemented (the `security/interim-hardening` pass)

| # | Control | Why | Where |
|---|---|---|---|
| C1 | **`.env` set to `0600`** (was world-readable) | Other local users could read all bot secrets | filesystem (run-time) |
| C2 | **Bash allowlist → Slurm-only** (dropped `cat/grep/head/tail/ls/wc/sort/uniq`) | With Sandbox B off, those run as the bot user with no path limit — an allowlisted user could `cat ~/.ssh/id_ed25519` or `.env`. Demonstrated live. | `config.py`, `.env(.example)` |
| C3 | **`search_files`** — a path-fenced, in-process grep | Restores content search **without** an unfenced reader: allowlist fence > Sandbox B's denylist, and it's our code | `tools.py` |
| C4 | **`metadata.md` versioned backups** | `write_metadata` is a whole-file replace; careless overwrite is the top integrity risk. Prior version saved to `$ASPEN_METADATA_HISTORY_ROOT/<scope>/<project>/<UTC>.md` (keyed by owner as well as project, so two users' same-named projects cannot overwrite each other) | `metadata.py` |
| C5 | **Bot ↔ tool server over a Unix socket** (was `127.0.0.1` TCP) | A loopback port is connectable by any local user; a socket in a `0700` dir is not | `tool_server.py`, `tools.py`, `config.py` |
| C6 | **seccomp syscall denylist on the analysis jail** | The one lever on the kernel→root path from inside the jail on this old kernel; blocks namespace/mount/keyring/ptrace/module/bpf/io_uring/userfaultfd/etc. | `tool_server.py` |
| C7 | **Group-DM participant gate** | In a group DM, the per-mentioner allowlist leaks Aspen's answers and the read thread context to *every* member — including people who could never DM it (a new prompt-injection + disclosure surface). Require **every human member** allowlisted; **fail closed** if membership can't be verified. App/bot members are exempt (only humans need approval). | `slack_app.py`, `config.py` |

| C8 | **Admission out of the agent's reach** | The user registry is written *only* by the `aspen-users` CLI — no Slack command, no tool. Nothing the model can be told to do adds or removes a user. Reads are hot-reloaded, so a revocation applies on the target's next message rather than at the next restart. Failure never widens: a corrupt file keeps the last good copy. | `registry.py`, `config.py`, `users_cli.py` |
| C9 | **Workflow ownership taken from the Slack event** | `write_workflow` has no owner parameter; the destination is `context["user_id"]`. A model can be argued into passing any argument it is given — it cannot pass a value it never receives. `_group` edits gate on `ADMIN_USER_ID`. Prior versions snapshotted like C4. | `tools.py`, `workflows.py` |
| C10 | **Trust tiers on cross-user workflow text** | A workflow is user-authored text meant to be *followed*, so cross-user reading is a lateral prompt-injection path. Another user's file is delivered tagged `trust="reference-only"` with an explicit prompt rule that directives inside it are not addressed to the model, and that no workflow can grant tools, relax the sandbox, or change file access. Defense in depth only — the real limit stays the Python-enforced tool surface. | `workflows.py`, `prompts.py` |
| C11 | **State-location guard at startup** | C8/C9 hold only if the registry and workflow tree aren't reachable through a writable path. Startup refuses if either resolves inside `WORKSPACE_ROOT` or `ASPEN_SANDBOX_WRITE_PATHS`, where sandboxed analysis code could edit them directly; the state dir is `chmod 0700` against other users on the shared node. Now also covers the **job ledger and the staging tree** (C12/C13) — a writable ledger is a forgeable cancel, and writable staging is a job body the model can plant. | `main.py`, `config.py` |
| C12 | **Cancel scoped by ledger, then verified against live Slurm** | The Slurm surface (spec §19) is the one capability that spends a shared resource, and under the beta account model Unix ownership protects nothing — the bot *is* the developer, so every hand-submitted research job is in `scancel` range. Three checks replace it: the ID must be a row in Aspen's own ledger; the row's `slack_user_id` must equal the Slack event's `user_id` (the tool has no `owner` parameter, as C9); and `scontrol show job` must report a `WorkDir` inside *that requester's* staging directory. Fail-closed at every step. `scancel` filter flags (`-u`/`-n`/`-A`/`-p`/`-q`/`-t`/`--me`) are never built — they delegate enumeration to Slurm, which is precisely what makes per-job verification impossible. | `jobs.py`, `tools.py` |
| C13 | **The model never authors a job body** | A submitted job runs unjailed on a compute node as the bot's Unix user, so *what* runs there is the whole security question. `submit_orca_batch` takes a `template_mode` from a fixed enum and a root-resolved structure path — no script path, no script content. The pipeline renders its own job scripts from its own packaged templates. What reaches a node is pipeline code plus the user's `.xyz` data. Inputs are copied, never symlinked, so nothing writes back into a calculations root. | `jobs.py`, `staging.py` |
| C14 | **Orchestrator environment scrubbed to an allowlist** | `load_dotenv` puts `SLACK_BOT_TOKEN` and `AGENT_INTERNAL_SECRET` in the bot's `os.environ`, and `sbatch` defaults to `--export=ALL` — so a naive submission copies Aspen's Slack tokens onto every compute node. The subprocess gets an explicit allowlist instead; a denylist of secret *names* would silently miss the next secret added to `.env`. Deliberately **not** `--export=NONE`: the pipeline's ORCA script finds `xas-rerun-orca` on an inherited `PATH` behind a `command -v` guard, so `NONE` turns auto-rerun triage into a silent no-op — a security change whose cost would have been invisible. | `jobs.py` |

All controls are covered by the hermetic test suite (`pytest -q`), including
contract tests that fail if a file-reader re-enters the allowlist, the seccomp
denylist loses a key entry, `write_workflow` grows an owner parameter, a `scancel`
argv gains a filter flag, or a job tool starts accepting a script path.

## 7. ⭐ Security work owed at the service-account cutover

The single largest residual risk is that the bot runs as a **personal, fully
privileged cluster account**. A dedicated `aspen-agent` service account (created
by SDF admins — minting a UID needs root) is the fix. This depends on admin
protocols. **Until it lands, keep the registry small and trusted** — the
developer plus a few named beta testers, not the whole group. (Access is now
managed with `./aspen-users`, not `ASPEN_ALLOWED_SLACK_USER_IDS`; see §5 of
[`spec.md`](spec.md). Widening it does not change the blast radius, which stays
the developer's full cluster identity until the service account lands.)

When the service account exists, do all of the following:

- [ ] **Run the bot as `aspen-agent`** — a service account with **no login shell,
      no SSH keys, and no munge credential** unless/until Slurm submission is
      enabled. This is what actually shrinks the blast radius.
- [ ] **Scope its filesystem access**: read-only to the group projects path,
      read-write only to the workspace (shared group + setgid so dev-created files
      stay manageable after cutover).
- [ ] **Relocate the state dir out of a personal home, and pick its mode
      deliberately.** Today `ASPEN_STATE_DIR` defaults to `~/.aspen` at `0700`,
      which works only because the bot user and the admin user are the same person.
      At cutover they split, and `0700` in *either* home breaks the other half:
      the bot can't read a registry in the admin's home, and the admin can't run
      `aspen-users` against one in the service account's home. Move it to the
      group space (`/sdf/data/ssrl/smb/dft/aspen/`, group `sdf-ssrl-dft`, setgid)
      and split the two paths, which is why they are separate env vars:

      | Path | Mode | Rationale |
      |---|---|---|
      | `ASPEN_USERS_FILE` dir | `0750`, owner `aspen-agent` | Group can `aspen-users list` / audit; **writes require being the service account**, so admission stays a privileged act (C8). |
      | `ASPEN_WORKFLOWS_ROOT` | `2770`, group `sdf-ssrl-dft` | Users may reasonably edit their own `WORKFLOW.md` in `$EDITOR`; setgid keeps new files group-owned. |
      | `ASPEN_TELEMETRY_DIR` + state file | `0700`, owner `aspen-agent` | The turn log holds users' questions verbatim while a collection window is open. The bot appends; only the service account reads. **Not** group-readable — a colleague's questions are not group business. |

      Two traps. (1) The parent `/sdf/data/ssrl/smb/dft` is already `drwxrwsr-x`,
      so a plain `mkdir` **inherits group-write** and silently gives every account
      in `sdf-ssrl-dft` the ability to grant itself Aspen access — `chmod 0750`
      the registry dir explicitly. (2) `registry.ensure_private_dir()` currently
      hard-codes `0700` and would clamp the `2770` workflows tree back down on the
      next write — **it needs a configurable mode before this layout will hold.**
      Keep both outside `WORKSPACE_ROOT`, or `main._check_state_locations()` will
      refuse to start (C11). Copy the existing `users.json` and `workflows/` over
      as part of the move; nothing regenerates them.
- [ ] **systemd unit** (`Type=simple`, `User=aspen-agent`, `EnvironmentFile`,
      `Restart=on-failure`) with hardening: `NoNewPrivileges=yes`,
      `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
      `ReadWritePaths=<workspace>`, `SystemCallFilter=@system-service`.
      **Do NOT set `DynamicUser=` or `PrivateUsers=yes`** — both put the service in
      its own user namespace, which would nest the analysis-jail bwrap one level
      too deep and break it on this kernel (verified: nested userns needs a
      mapping; systemd's private-users mode collides with bwrap's own).
- [ ] **Rotate every secret at cutover** — Slack bot+app tokens,
      `AGENT_INTERNAL_SECRET`, and move off the personal Claude login to a
      dedicated lab/service Claude API key (`ASPEN_SDK_USE_SUBSCRIPTION=false`).
      (Rotation was deferred from the interim pass to this step.)
- [ ] **Lock down secret files** under the service account: `.env`, the API-key
      store, any credentials — all `0600`, owner-only.
- [ ] **Re-verify host enforcement as the service account**, in a plain shell
      (not nested in a Claude session): run [`probe_isolation.sh`](probe_isolation.sh)
      and `verify_sandbox.sh`; confirm the bwrap jail starts, the seccomp filter
      compiles, and the UDS binds in a `0700` dir.
- [ ] **Re-evaluate the user registry.** Only after the above, widen it beyond the
      beta group with `./aspen-users add` (the dominant blast-radius risk is gone).
      Drop the `ASPEN_ALLOWED_SLACK_USER_IDS` bootstrap from the new `.env` once the
      registry is in place, so there is exactly one source of truth. For the
      careful-insider model, also confirm `$ASPEN_METADATA_ROOT` is backed up /
      version-controlled (C4 protects against in-bot overwrite; off-host backup
      protects against everything else). Nothing in a calculations tree is at risk
      from `write_metadata` — it has not written there since the sidecar migration,
      and a project README stays entirely in its owner's hands.

**Slurm submission — what this section asked for, and where it landed.** The
ORCA→CORVUS capability is now built (spec §19), and it deliberately did **not** take
the Bash route this section anticipated:

- [x] **Bound submissions** — done as C12–C14: per-user daily submission budget,
      hard caps on concurrent agent-submitted jobs (per-user and global), dry-run +
      single-use confirmation token, a fixed `template_mode` enum with path
      validation, and cancellation restricted to the agent's own ledger rows filtered
      by the requesting Slack ID. No project or user text reaches the argv; the model
      never supplies a script.
- [~] **Sandbox B is not the answer here, and stays off.** Its checkbox was
      conditioned on the agent gaining a Bash *write/exec* surface. Building
      submission as **structured tools** instead means Bash gained nothing — it is
      still the read-only Slurm allowlist — so there is no new Bash surface for
      Sandbox B to confine. It would still confine nothing (§8). If anyone ever adds
      `Bash(sbatch:*)`, this checkbox comes back and so does the fail-closed
      startup self-test.
- [ ] **The residual is the account, not the controls.** A submitted job runs
      unjailed on a compute node as the bot's Unix user and can read
      `$ASPEN_STATE_DIR`, the repo `.env` and `~/.ssh`. No Aspen-side control fixes
      that; the service-account split above is the fix. Accepted for the beta (§8).
- [ ] **Give `aspen-agent` a munge credential at cutover** — the first item in this
      section says "no munge credential unless/until Slurm submission is enabled".
      It is now enabled, so the service account needs one, plus a Slurm association.
      Decide then whether jobs charge one shared account or each user's (an admin
      question, not a code one).
- [ ] **Plumb `--comment` through the pipeline** so attribution has a second,
      Slurm-maintained key beside `WorkDir` (`AccountingStoreFlags = job_comment` is
      confirmed on s3df). Strictly `--comment` and a job-name prefix — **not** a
      generic `--sbatch-extra-args`, which is `--wrap` by another name.

## 8. Accepted risks (deliberate, for the interim)

- **Bot runs as a personal privileged account** (SSH keys + munge). Accepted only
  because the allowlist is small and trusted until §7. Now carries two extra
  consequences, both from job submission:
  - **A submitted job runs unjailed as the developer** on a compute node, so it can
    read `$ASPEN_STATE_DIR`, the repo `.env`, and `~/.ssh`. Bounded by C13 (the job
    body is pipeline code, never model output) and by the beta registry being a
    handful of trusted colleagues — *not* by any confinement. This is the single
    biggest reason to keep the beta small.
  - **The developer's own hand-submitted jobs are inside the bot's `scancel`
    range.** C12's `WorkDir` check is what keeps them safe, because a job launched
    from a personal tree cannot have a `WorkDir` inside Aspen's staging root. Note
    the asymmetry: under the service account this would be enforced by Slurm itself
    and C12 would be redundancy; today C12 *is* the control.
- **Secrets not rotated** despite the brief world-readable `.env` window — short
  exposure, usage is monitored, rotation folded into the §7 cutover.
- **No per-project / per-user authorization** among allowlisted users — acceptable
  while data is public and members are trusted-but-careless. Revisit if either
  changes.
- **Sandbox B is off** — still correct, and job submission did not change it: it
  wraps only the `Bash` tool (not the in-process tools), and the only allowlisted Bash
  commands (Slurm reads) are *excluded* from it by necessity (they need cluster
  network/munge), so it would confine nothing. Submission is a structured tool, not a
  Bash command, so it adds no Bash surface. Enable it (fail-closed) if Bash ever
  gains a write/exec surface — §7.

- **Compute is spent on a shared allocation with per-user accounting kept only by
  Aspen.** Jobs charge the developer's Slurm association (`ssrl:SMBXAS`), so "who used
  the compute" is a ledger fact, not a cluster fact, until the service account gets
  its own association. The ledger is reconciled against `sacct` manually
  (`aspen-users jobs reconcile`), not on a cron, so consumption figures are as fresh
  as the last time someone ran it. Bounded by the caps in C12; watch it during the
  beta rather than trusting it.
- **DEMO spends model budget for people who are not users.** Anyone in the
  workspace can trigger a walkthrough, and it runs the real agent. Accepted
  because it is capped three ways (per session, per day across everyone, and by
  the ordinary rate limiter), reads only fabricated data, and writes nothing —
  and because the alternative, a scripted slideshow, does not demonstrate the
  thing being demonstrated. `ASPEN_DEMO_ENABLED=false` turns it off outright.
  Watch the demo rows in `aspen-dashboard` if the workspace is large.
- **UDS socket mode is `0666`** (uvicorn fixes it and offers no override hook); the
  enclosing **`0700` directory** is the actual access control and is sufficient.
- **Kernel→root via an unpatched kernel bug** — not fully eliminable on the pinned
  4.18 kernel by an unprivileged user; the seccomp filter (C6) shrinks the
  reachable surface, but patching is the admins'.

## 9. Notable decisions & rationale

- **Fenced `search_files` instead of allowlisting `grep`** — an allowlist fence
  (confined to the calc root) beats Sandbox B's `denyRead` denylist, is enforced by
  our own code, and has no fail-open mode. See §6 C2/C3.
- **Unix socket instead of loopback TCP** — removes "any local user can connect";
  also faster (skips the TCP/IP stack). `httpx` chosen as the client because UDS is
  a first-class feature there and it's the FastAPI/uvicorn stack's own HTTP client.
- **Denylist seccomp, not allowlist** — a strict syscall allowlist is too brittle
  for arbitrary numeric Python (numpy/scipy/matplotlib); the denylist blocks the
  known escalation primitives and was verified transparent to the analysis stack.
- **We own the security-critical boundaries** (bwrap jail, fenced tools) rather than
  delegating them to the Claude Code CLI's sandbox feature, which is a third-party
  black box with a fail-open-when-nested quirk.

## 10. Host facts (validated on `sdfiana005`, 2026-06-25)

Established with `probe_isolation.sh` (re-run to re-verify after any host change):
kernel 4.18 (RHEL 8), **cgroups v1** (so `prlimit` caps, not cgroup memory limits);
unprivileged user namespaces work and **nest** (with a uid map); **bwrap 0.4.0**
supports `--seccomp`; `AF_UNIX` + `SO_PEERCRED` available; no `/etc/subuid` range
(a separate UID needs admin); SSH key and Claude credentials are `0600`.

## 11. Operational checklist (activating this pass)

1. **Restart both processes** (`start.sh`) — C2/C5/C6 and the new tools only take
   effect on restart. Confirm the tool-server log shows the seccomp filter compiled
   and the socket bound in a `0700` dir.
2. **Smoke-test as the developer**: one analysis (exercises UDS + seccomp live), one
   metadata edit (confirms a snapshot lands in `metadata_history/`), one search.
3. **Keep the allowlist developer-only** until the service account (§7).
4. Re-run `probe_isolation.sh` after any host/kernel change.
