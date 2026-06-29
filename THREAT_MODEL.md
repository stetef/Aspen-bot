# Aspen — Threat Model & Security Measures

_Last updated: 2026-06-25. Scope: the system **as built** after the
`security/interim-hardening` pass, plus the security work still owed (notably the
move to a dedicated service account). Companion to [`spec.md`](spec.md) (design)
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
| Users | The SMB/SSRL research group, via a **SLAC-managed (SSO) Slack** workspace | The user-ID allowlist is a *strong* auth gate; spoofing/external entry is low-risk |
| Data sensitivity | **Public / publishable** computational chemistry | Confidentiality is low priority; **integrity & availability** matter more |
| Host | A **shared multi-user login node** (`sdfiana*`) | `127.0.0.1` ports and the bot's files are reachable/visible to other lab users |
| Bot identity (interim) | Runs as a **developer's personal Unix account** with SSH keys + munge (job submission) + group data access | If any confinement is bypassed, the blast radius is that whole cluster identity — this is the dominant interim risk, fixed only by the service account (§7) |
| Insider trust | Defend against the **careless** allowlisted member, not a malicious one | Favor guardrails, backups, and fenced tools over hard inter-user isolation |

## 2. Assets (ranked)

1. **Secrets on the node** — Slack tokens, `AGENT_INTERNAL_SECRET`, the Claude
   Code login (`~/.claude`), the account's SSH key, the munge credential.
   *(Highest: theft = impersonate Aspen, lateral movement, spend compute.)*
2. **Integrity & availability of calculation data** (public, so not secrecy —
   don't corrupt or destroy it).
3. **Cluster compute / fair-share** (rises sharply if/when the agent can submit
   Slurm jobs — see §7).
4. **Bot availability & answer correctness.**
5. Data confidentiality — **low** (public), with a mild pre-publication caveat.

## 3. Trust boundaries

- **Slack → bot** — Socket Mode, outbound WebSocket only; auth = SSO-backed
  user-ID allowlist. Strong.
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

## 4. Threat actors

- **Careless allowlisted member** *(primary)* — accidental destructive request,
  wrong-project metadata overwrite, runaway compute.
- **Other unprivileged users on the shared node** — could read secrets if file
  perms are loose; could reach a loopback service; could fill shared disk.
- **Hijacked Slack account** — mitigated by SSO, but a live session acts as that
  user.
- **Prompt-injection via project data** — content in the calculations tree trying
  to steer the agent. Bounded by the jail + fenced tools.
- **External attacker** — minimal direct surface (no inbound ports); realistic
  path is secret theft or supply chain.
- **Admins / root / backups** — trusted, out of scope (but note they *can* read
  everything, including `.env`).

## 5. Architecture & enforcement points

Two single-instance processes:

- **bot** (`aspen/`) — Slack front-end + Claude Agent SDK. Serves the read/search
  /metadata tools **in-process**; the only outbound tool call is the analysis
  bridge to the tool server.
- **tool server** (`tool_server.py`) — runs LLM-generated analysis code in the
  bwrap jail; owns caching, metadata parsing, the SQLite index, audit logging.

**The agent's tool surface, and how each is bounded:**

| Tool | Bound by |
|---|---|
| `list_directory`, `read_file`, `search_files`, `attach_file` | In-process Python, **path-fenced to the calculations root** (`_safe_path`); cannot read outside it |
| `write_metadata` | The agent's only write surface; one file (`<project>/metadata.md`), prior version snapshotted first |
| `run_python_analysis` | The **bwrap jail + seccomp** (no network, read-only project mount, only `figures/`+`cache/` writable, `prlimit` caps) |
| `Bash` | A **Slurm read-only allowlist** + `can_use_tool` deny; everything else refused |

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
| C4 | **`metadata.md` versioned backups** | `write_metadata` is a whole-file replace; careless overwrite is the top integrity risk. Prior version saved to `<workspace>/metadata_history/<project>/<UTC>.md` | `tools.py` |
| C5 | **Bot ↔ tool server over a Unix socket** (was `127.0.0.1` TCP) | A loopback port is connectable by any local user; a socket in a `0700` dir is not | `tool_server.py`, `tools.py`, `config.py` |
| C6 | **seccomp syscall denylist on the analysis jail** | The one lever on the kernel→root path from inside the jail on this old kernel; blocks namespace/mount/keyring/ptrace/module/bpf/io_uring/userfaultfd/etc. | `tool_server.py` |

All controls are covered by the hermetic test suite (`pytest -q`, 95 tests),
including contract tests that fail if a file-reader re-enters the allowlist or the
seccomp denylist loses a key entry.

## 7. ⭐ Security work owed at the service-account cutover

The single largest residual risk is that the bot runs as a **personal, fully
privileged cluster account**. A dedicated `aspen-agent` service account (created
by SDF admins — minting a UID needs root) is the fix. This is blocked on admin
availability (~3–6 months from 2026-06-25). **Until it lands, keep the Slack
allowlist restricted to the developer only.**

When the service account exists, do all of the following:

- [ ] **Run the bot as `aspen-agent`** — a service account with **no login shell,
      no SSH keys, and no munge credential** unless/until Slurm submission is
      enabled. This is what actually shrinks the blast radius.
- [ ] **Scope its filesystem access**: read-only to the group projects path,
      read-write only to the workspace (shared group + setgid so dev-created files
      stay manageable after cutover).
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
- [ ] **Re-evaluate the user allowlist.** Only after the above, widen
      `ASPEN_ALLOWED_SLACK_USER_IDS` beyond the developer (the dominant blast-radius
      risk is gone). For the careful-insider model, also confirm the calculations
      tree's `metadata.md` files are backed up / version-controlled (C4 protects
      against in-bot overwrite; off-host backup protects against everything else).

**If/when the agent is given a Bash *write or exec* surface** (e.g. the ORCA→CORVUS
Slurm-submission roadmap), additionally:

- [ ] **Turn on Sandbox B (the Claude Code Bash OS sandbox) — fail-closed.** Add a
      startup self-test (the `verify_sandbox.sh` logic) that **refuses to start** if
      the sandbox isn't actually enforcing, so its silent fail-open-when-nested
      behavior can never apply unnoticed. Set `denyRead` on the secret paths as a
      backstop; keep the fenced in-process tools as the primary read boundary.
- [ ] **Bound submissions**: per-user submission budget, a hard cap on concurrent
      agent-submitted jobs, dry-run + confirmation by default, and a fixed
      `template_mode` allowlist with path validation. Project/user text must never
      reach the `qsub`/`sbatch` argv. Cancel only the agent's own job IDs.

## 8. Accepted risks (deliberate, for the interim)

- **Bot runs as a personal privileged account** (SSH keys + munge). Accepted only
  because the allowlist is **developer-only** until §7. Do not widen users first.
- **Secrets not rotated** despite the brief world-readable `.env` window — short
  exposure, usage is monitored, rotation folded into the §7 cutover.
- **No per-project / per-user authorization** among allowlisted users — acceptable
  while data is public and members are trusted-but-careless. Revisit if either
  changes.
- **Sandbox B is off** — correct for now: it wraps only the `Bash` tool (not the
  in-process tools), and the only allowlisted Bash commands (Slurm) are *excluded*
  from it by necessity (they need cluster network/munge), so it would currently
  confine nothing. Enable it (fail-closed) when Bash gains a write/exec surface — §7.
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
