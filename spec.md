# Aspen — HPC Slack Agent Specification

---

## 1. Overall Architecture

```
Slack (Socket Mode — outbound WebSocket only)
  │
  ▼
aspen-bot.py  (Slack Bolt app, runs as aspen-agent service account)
  │   └── maintains per-thread conversation history (in-memory)
  │   └── enforces per-user rate limits (in-memory)
  │
  ▼  HTTP POST to localhost:8000 with shared secret header
FastAPI tool server  (binds to 127.0.0.1 only)
  │
  ▼
run_python_analysis tool
  │
  ▼
Apptainer container (sandbox, one per execution)
  │
  ├── /projects/                        [read-only mount]
  │     ├── drug_binding/
  │     │    ├── experiment_01/
  │     │    ├── experiment_02/
  │     │    └── metadata.toml
  │     ├── heme/
  │     └── MOF/
  │
  └── /aspen_workspace/                 [writable mount]
        ├── generated_code/             [UUID-named scripts, deleted after execution]
        ├── figures/                    [transient; moved to archive after Slack upload]
        ├── figure_archive/             [cleaned when total size exceeds 2 GB]
        ├── cache/
        └── logs/
```

Each project has its own SQLite database file for metadata and run indexing.

---

## 2. Deployment & Process Management

### Deployment Modes

Aspen supports two deployment modes. Their security properties differ, and the distinction must be understood before deployment.

**Development mode (single developer, personal account).** During initial development the bot runs under the developer's own user account, started in a `screen` or `tmux` session rather than systemd. Apptainer inherits the developer's Unix permissions, so the agent can read everything the developer can read and the orchestration process (bot + FastAPI) runs with the developer's full privileges. This mode is acceptable **only** when the developer is the sole authorized user, enforced by the Slack user allowlist in Section 3, because every analysis request executes LLM-generated code under the developer's identity. It must not be opened to other Slack users while running under a personal account.

**Production mode (service account, systemd).** The bot runs under a dedicated `aspen-agent` service account managed by systemd, as specified below. This is the target deployment and is required before any user other than the developer is allowed to invoke the bot.

To keep the port from development to production cheap, the implementation must:
- Drive every path and identity-specific value from the `.env` file (Section 15). No path may be hardcoded, and no code may derive paths or behaviour from `getpass.getuser()`, `os.environ["USER"]`, `Path.home()`, `~`, or `pwd.getpwuid(os.getuid())`.
- Keep all state (repo, virtualenv, workspace, image, `.env`) on the group data path, never in the developer's home directory.
- Create the workspace with a shared Unix group and the setgid bit, so files created under the developer's account stay manageable after the switch to `aspen-agent`:
  ```bash
  chgrp -R <shared_group> /sdf/data/<group>/aspen_workspace
  chmod -R g+rwX,g+s /sdf/data/<group>/aspen_workspace
  ```

The Slack and Anthropic tokens are bound to the Slack app and Anthropic account, not to the Unix account, so they carry over unchanged. Only `AGENT_INTERNAL_SECRET` should be regenerated at the production cutover.

### Service Account

In production mode, the bot **must** run under a dedicated `aspen-agent` service account, not under any personal user account. This must be set up by the HPC sysadmin before production deployment. (For development mode, see above.)

Required permissions for `aspen-agent`:

| Resource | Permission |
|---|---|
| `/projects/` (all subdirectories) | Read-only |
| `/aspen_workspace/` | Read + Write |
| Slurm / job scheduler | None — no job submission allowed |
| Other users' home directories | None |
| Network (outbound HTTPS, port 443) | Required for Slack WebSocket and Anthropic API |

The `aspen-agent` account must have **no login shell** and **no SSH keys** — it is a service identity only. All secrets are stored in `/opt/aspen-agent/.env`, readable only by `aspen-agent` (chmod 600).

### systemd Service Definition

The bot is managed as a systemd service. This requires sysadmin (root) access to install. The sysadmin must create the following file at `/etc/systemd/system/aspen-agent.service`:

```ini
[Unit]
Description=Aspen HPC Slack Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aspen-agent
Group=aspen-agent
WorkingDirectory=/opt/aspen-agent
ExecStart=/opt/aspen-agent/venv/bin/python aspen-bot.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/opt/aspen-agent/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aspen-agent

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/aspen_workspace

[Install]
WantedBy=multi-user.target
```

Enable and start with:
```bash
sudo systemctl daemon-reload
sudo systemctl enable aspen-agent
sudo systemctl start aspen-agent
```

Check logs with:
```bash
journalctl -u aspen-agent -f
```

**Dependency flag for sysadmin:** The deploying researcher must request the following from their S3DF sysadmin before deployment:
1. Creation of `aspen-agent` service account (no login shell, no SSH keys)
2. Installation and enablement of the systemd service file above on the designated login node
3. Read access for `aspen-agent` to `/sdf/data/<group>/projects/` (replace `<group>` with the actual S3DF group name)
4. Confirmation that the login node permits outbound HTTPS (port 443) for Slack WebSocket and Anthropic API calls

### Filesystem Access

Apptainer inherits the Unix permissions of the user running it — in this case `aspen-agent`. Filesystem access is therefore controlled by standard Unix group permissions, not by any special configuration:

- **Project data** lives at `/sdf/data/<group>/projects/` on S3DF's shared filesystem (Lustre). The `aspen-agent` account must be added to the appropriate Unix group so it can read this path. Write access is not granted and not needed — the Apptainer bind mount enforces read-only at the container level as an additional layer.
- **Workspace** is a directory created by the researcher at `/sdf/data/<group>/aspen_workspace/`, owned by `aspen-agent`. This is preferred over the home directory (`/sdf/home/`) because group data storage has significantly more capacity. The researcher creates this directory before first deployment:

```bash
mkdir -p /sdf/data/<group>/aspen_workspace/{generated_code,figures,figure_archive,cache,db,logs}
chown -R aspen-agent:aspen-agent /sdf/data/<group>/aspen_workspace/
```

All paths in the spec's architecture diagram and environment variables refer to these S3DF paths. The placeholders `/projects/` and `/aspen_workspace/` in Apptainer bind mounts map to:

| Spec path | S3DF path |
|---|---|
| `/projects/` | `/sdf/data/<group>/projects/` |
| `/aspen_workspace/` | `/sdf/data/<group>/aspen_workspace/` |

Replace `<group>` with the actual group name throughout all bind mount commands and the `.env` file before deployment.

### Pre-Deployment Verification Checklist

The following must be verified on the actual target node, not assumed from the spec. Several have been sources of silent failure:

- **Container runtime present and recent.** `apptainer --version` ≥ 1.1 — required for the symlink-containment guarantee in Section 8. (Confirmed on the current node: `apptainer 1.2.5`, installed as a system binary at `/usr/bin/apptainer`; `singularity` is an alias to the same binary, so no module load is needed.)
- **Symlink containment actually holds:** a symlink created in `/aspen_workspace/` that points into `/projects/` or beyond is not traversable from inside the container.
- **PID namespace isolation active** (`apptainer inspect` on the built image): subprocesses spawned in the container die when it is killed on timeout.
- **`--memory` enforcement actually bites** (Section 8): allocate more than the limit and confirm an OOM kill rather than host-memory consumption. If unenforced, wire up the `ulimit -v` fallback.
- **`--cleanenv` confirmed:** `apptainer exec --cleanenv <image> python -c "import os; print('ANTHROPIC_API_KEY' in os.environ)"` prints `False`.
- **Outbound HTTPS (443)** to Slack and Anthropic. (Confirmed reachable on the current node; note that a `404` from `https://api.anthropic.com/` and a `200` from `https://slack.com/api/api.test` both indicate success — the connection completed.)
- **PyPI reachable** for image builds. (Confirmed: `pypi.org` and `files.pythonhosted.org` return `200`.)
- **Base-image source reachable** if building from a registry (`registry-1.docker.io`, `quay.io`); a `401` from the registry is a pass. If blocked, build the `.sif` on a host with egress and copy it to `APPTAINER_IMAGE`.
- **Filesystem type of `/sdf/data`** determined (`stat -f -c %T`) and the SQLite placement decision made accordingly (Section 6).
- **Node-local scratch** identified and writable, if databases are placed locally.
- **Python ≥ 3.11** for stdlib `tomllib`; otherwise add `tomli` as a dependency for TOML parsing.
- **Login-node pinning (development mode):** record the `hostname` the `screen`/`tmux` session is started on. If logins are load-balanced, the session is reachable only by SSHing back to that exact host, and the bot dies if that node reboots — a reason to move to systemd (production mode) once stable.

---

## 3. Slack Integration — Socket Mode

The bot (named **Aspen**) uses **Slack Socket Mode** exclusively. This means the bot initiates an outbound WebSocket connection to Slack's servers — no inbound ports are opened, no public URL is needed, and no firewall exceptions are required on the cluster.

```
Cluster (behind firewall)              Slack's servers
  aspen-bot.py  ──── outbound WebSocket ───►  api.slack.com
          ◄─── events pushed down ─────
          ────── replies sent up ──────►
```

### Slack App Configuration

The Slack app must be configured with the following settings (set in the Slack developer portal):

- **Socket Mode:** Enabled
- **App-Level Token:** Required (with `connections:write` scope) — stored as `SLACK_APP_TOKEN` in `.env`
- **Bot Token:** Required (with scopes below) — stored as `SLACK_BOT_TOKEN` in `.env`
- **Bot display name:** Aspen
- **Bot default icon:** to be set at deployment time

Required OAuth Bot Token scopes:

| Scope | Purpose |
|---|---|
| `app_mentions:read` | Respond only when @Aspen is mentioned |
| `chat:write` | Post messages and results |
| `files:write` | Upload figures as Slack files |
| `im:history` | Read DM threads for context |
| `channels:history` | Read channel threads for context (only threads Aspen is in) |

**The bot does not have `channels:read` or unrestricted message history access.** It can only read messages in threads it has been mentioned in.

### Interaction Model

- Aspen responds only to `@Aspen` mentions in channels or direct messages.
- Aspen does not read or act on messages that do not @mention it.
- Aspen ignores mentions from any Slack user not on the configured allowlist (see below).
- Aspen posts results (text + figures) as replies within the same Slack thread.

### User Allowlist

Aspen acts only on requests from Slack user IDs in an explicit allowlist, configured as `ASPEN_ALLOWED_SLACK_USER_IDS` in `.env` (comma-separated Slack user IDs). A mention from anyone not on the allowlist is ignored, with at most a single reply: "Sorry, you're not authorized to use Aspen." The allowlist check runs in `aspen-bot.py` **before** rate limiting and before any request is forwarded to FastAPI — it is the first authorization gate.

This is a hard requirement in development mode, where the bot runs under the developer's personal account: the allowlist must contain only the developer's own Slack user ID, since any authorized user's request executes code with the developer's Unix privileges. In production mode the allowlist may be widened, but it remains the first gate.

---

## 4. FastAPI Tool Server — Security

### Binding

The FastAPI server **must** bind exclusively to `127.0.0.1` (localhost), not to `0.0.0.0` or any network interface. It is not reachable from other machines or other users on the cluster.

```python
uvicorn.run(app, host="127.0.0.1", port=8000)
```

### Shared Secret Authentication

All requests from `aspen-bot.py` to the FastAPI server must include a shared secret in the `X-Agent-Secret` HTTP header. The FastAPI server rejects any request missing this header or presenting an incorrect value with HTTP 403.

The secret is a randomly generated 32-byte hex string, generated once at setup:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

It is stored in `/opt/aspen-agent/.env` as `AGENT_INTERNAL_SECRET` and read by both `aspen-bot.py` and the FastAPI server at startup. It is never logged, never included in responses, and never transmitted to Slack or the Anthropic API.

---

## 5. Project Metadata — Required Before Agent Can Assist

### Metadata-First Policy

**Aspen will not attempt to analyze or describe any project directory that does not contain a valid `metadata.toml` or `metadata.yaml` file at its root.**

If a user asks Aspen about a project directory that lacks a metadata file, Aspen must respond with a message explaining that no metadata file was found, and provide the following example template for the user to fill in and place at the project root:

```toml
# metadata.toml — place this file at /projects/<your_project>/metadata.toml

name = "your_project_name"
allowed_libraries = ["numpy", "pandas", "matplotlib"]

[parsers]
energy = "energy.csv"          # primary energy output file name
structure = "structure.pdb"    # primary structure file name (if applicable)
output_files = ["energy.csv", "log.txt"]  # all expected output file names

[datasets]
# List your dataset or run group names here
run_group_1 = ["run_001", "run_002", "run_003"]
```

Or in YAML format (`metadata.yaml`):

```yaml
name: your_project_name
allowed_libraries:
  - numpy
  - pandas
  - matplotlib

parsers:
  energy: energy.csv
  structure: structure.pdb
  output_files:
    - energy.csv
    - log.txt

datasets:
  run_group_1:
    - run_001
    - run_002
    - run_003
```

Aspen's exact response when metadata is missing:

> "I can't find a `metadata.toml` or `metadata.yaml` file in `/projects/<project_name>/`. Please create one so I understand your project's structure. Here's a template to get started: [template above]. Once it's in place, ask me again and I'll be ready to help."

There is no fallback or LLM-assisted indexing. The metadata file is a hard prerequisite.

### Metadata Schema

The metadata file defines:

- `name`: Project identifier (must match directory name)
- `allowed_libraries`: Python libraries the sandbox is permitted to import for this project (enforced at container level)
- `parsers`: File name patterns for known output types (used to guide code generation)
- `datasets`: Named groups of run directories

### Metadata and Project Text Are Untrusted Input

Metadata files, README/blurb text, directory and file names, and file contents all flow into the LLM's context and influence the code it generates. They must be treated as untrusted input: a string in a README — deliberate or accidental — could attempt to steer code generation ("ignore previous instructions and…"). This is low-risk while the project files belong to the sole authorized developer, but it is precisely why the container, not the prompt, is the security boundary (Section 8). The implementation must never let project-derived text relax sandbox restrictions, choose mounts, or alter the import allowlist.

---

## 6. Per-Project Database — SQLite

Each project uses a single SQLite database file stored at `/aspen_workspace/db/<project_name>.sqlite`. There is no PostgreSQL dependency.

SQLite is chosen for simplicity: no server process to manage, no authentication configuration, and the database is a portable file. Migration to PostgreSQL can be considered if concurrent write performance becomes a bottleneck at scale.

**Database file placement and journal mode.** SQLite's locking and shared-memory behaviour are unreliable on networked/parallel filesystems. WAL mode in particular uses an mmap-backed `-shm` file that requires every reader and writer to be on the same host and that does not work correctly over a network filesystem. Aspen is single-host, so the host requirement is met, but mmap semantics on a parallel filesystem (Lustre/Weka/GPFS — confirm which one `/sdf/data` actually is at deployment) remain a corruption risk.

- **Preferred:** place the SQLite databases on node-local disk (`$TMPDIR` or local scratch, via `SQLITE_DB_ROOT`) and keep `PRAGMA journal_mode=WAL`. WAL is safe and correct on local disk and gives the concurrent-read-with-write behaviour the threaded FastAPI server benefits from. The databases are derived indexes, so if the bot moves to another node they are simply rebuilt by re-indexing.
- **Fallback (databases on the group data path):** do **not** use WAL there. The FastAPI server is the only writer; serialize writes behind a single guarded writer connection and use SQLite's default rollback journal, which avoids the `-shm` mmap dependency entirely.

In both cases, set `PRAGMA busy_timeout` (e.g. 5000 ms) on every connection so brief contention retries instead of raising `database is locked`, and verify the chosen configuration on the actual target filesystem before deployment rather than assuming it works. These pragmas must be set in the database connection helper, not left to individual call sites.

### Schema

```sql
CREATE TABLE runs (
    run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT UNIQUE NOT NULL,
    status    TEXT,           -- 'running', 'done', 'failed'
    tags      TEXT,           -- JSON array stored as text, e.g. '["docking","run01"]'
    energy    REAL,
    structure TEXT,
    last_update TEXT          -- ISO 8601 UTC timestamp
);

CREATE TABLE datasets (
    dataset_name TEXT PRIMARY KEY,
    run_ids      TEXT         -- JSON array of run_id integers
);

CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_tags   ON runs(tags);
```

The FastAPI server is the only process that writes to the SQLite databases. The Apptainer container has no access to database files.

---

## 7. Directory Indexing

For each run directory, a `manifest.json` is generated and stored in the project SQLite database (as a JSONB-equivalent TEXT field):

```json
{
  "run_id": "run_01",
  "project": "drug_binding",
  "files": ["energy.csv", "poses.sdf", "log.txt"],
  "tags": ["docking"],
  "parsed_properties": {
    "energy": -5.4,
    "ligand_count": 12
  }
}
```

Manifests are generated by the FastAPI indexing endpoint using the project's `metadata.toml`/`metadata.yaml` parsers. There is no LLM-assisted indexing. If files are not captured by the metadata parsers, they remain unindexed until the metadata file is updated.

---

## 8. Run Python Analysis Tool — Sandboxed Execution

### Code Generation and Passing to Apptainer

1. The LLM generates Python code as a string.
2. The FastAPI server writes this code to a file at `/aspen_workspace/generated_code/<uuid4>.py` where `<uuid4>` is a freshly generated UUID for each execution. This prevents filename collisions and race conditions.
3. The Apptainer container is invoked with that file path as the script to execute.
4. After execution completes (success, failure, or timeout), the generated code file is **deleted immediately** from `/aspen_workspace/generated_code/`.

```python
import uuid, os

script_path = f"/aspen_workspace/generated_code/{uuid.uuid4()}.py"
with open(script_path, "w") as f:
    f.write(generated_code)
try:
    result = run_in_apptainer(script_path, project_name, dataset)
finally:
    os.remove(script_path)  # always clean up, even on failure
```

### Apptainer Container Configuration

**Security model — read this first.** The trust boundary is the Apptainer container itself: no network, read-only project mount, a minimal image, resource caps, and a clean environment. The in-process Python protections described below (the import allowlist, the restricted-builtins layer, and the `exec`/`eval` static check) are **defense-in-depth hints, not a security boundary.** In-process CPython sandboxing is bypassable in general (object-graph gadgets such as `().__class__.__bases__[0].__subclasses__()`, `sys.modules`, builtins recovery, encoded source), so the implementation must assume LLM-generated code can import or call anything present inside the image. Two consequences are mandatory:
- **The image must be minimal.** Install only the libraries analysis needs. Nothing network-capable, no compilers, no credential-bearing tooling. If arbitrary code must not be able to reach it, it must not be in the image.
- **`allowed_libraries` is advisory/UX, not isolation.** It gives the LLM clean errors and steers code generation; it does not provide per-project security separation, because the prebuilt image contains the union of all libraries (Section 16). Do not rely on it as a boundary.

- **Network access:** Disabled (`--net --network none`)
- **Environment:** Launched with `--cleanenv` so none of the host environment is inherited. `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `AGENT_INTERNAL_SECRET` must never be present in the container environment — the LLM call happens in the FastAPI/bot layer outside the container, so the sandbox needs none of them. With `--cleanenv` in place, code such as `print(os.environ)` inside the sandbox cannot leak secrets, and the stdout redaction in Section 13 becomes a backstop rather than the primary control.
- **Execution timeout:** 120 seconds. On timeout, the container process is sent SIGKILL. The FastAPI server catches the resulting subprocess timeout exception, logs it, and returns a structured error response. The bot posts a user-facing message: "Analysis timed out after 120 seconds. Try a smaller dataset or a simpler query."
- **Memory limit:** 8 GB, enforced via Apptainer's `--memory` flag. **This must be verified to actually take effect at deployment.** Unprivileged `--memory` enforcement requires cgroups v2 delegated to the running user, which is frequently unavailable on HPC nodes; if so, the flag is silently ignored. The deployment checklist must include a test that allocates more than the limit and confirms the container is OOM-killed rather than consuming node memory. If unprivileged cgroup enforcement is unavailable, fall back to `ulimit -v` (address-space limit) applied in the launch wrapper before exec.
- **Mounts (the writable/read-only split is the real enforcement, not policy):**
  - `/projects/<project_name>` → bind-mounted **read-only**.
  - `/aspen_workspace/figures/` and `/aspen_workspace/cache/` → bind-mounted **read-write**. These are the only writable paths.
  - `/aspen_workspace/logs/` → if mounted at all, bind-mounted **read-only**. The container must not be able to modify audit logs.
  - `/aspen_workspace/db/` → **not mounted into the container at all.** The SQLite databases are never reachable from the sandbox; the FastAPI server is the sole writer (Section 6).
  - `/aspen_workspace/generated_code/` → not mounted; the single script is written before launch and passed in by path (or staged into a per-execution scratch dir), then deleted after.
  - Nothing else is mounted. Constraints such as "the container cannot touch the database" are enforced by the mount set above, not by convention in the code.
- **Path validation:** `project_name` and every entry in `dataset` are validated by resolving the real path (`os.path.realpath`) and confirming it is contained within `/projects/` (e.g. via `Path.is_relative_to`) **before** any filesystem access or container launch. Validation must not be a string check for `..` — that misses absolute paths, `....//`, and symlinks. This complements Apptainer's symlink containment (≥ 1.1) rather than replacing it.
- **Allowed Python imports:** Defined per-project in `metadata.toml`/`metadata.yaml` under `allowed_libraries`. The container enforces this via a restricted import hook injected at the top of every generated script. Any attempt to import a library not in the allowlist raises `ImportError` and terminates the script.
- **Blocked operations** (enforced via the import hook and a restricted builtins layer):
  - `subprocess`, `os.system`, `os.popen`, `pty`
  - Any `socket` or network operations
  - File writes outside `/aspen_workspace/figures/` and `/aspen_workspace/cache/`
  - `open()` calls targeting paths outside allowed directories
  - Dynamic import mechanisms: `__import__()`, `importlib.import_module()`, `importlib.__import__()`, and `runpy.run_module()` must all be blocked or overridden — not just the `import` statement. The import hook must intercept these call paths explicitly, as they are common bypass routes.
  - `exec()` and `eval()` on externally derived strings are blocked. If the LLM-generated code contains `exec` or `eval`, the script is rejected before container launch by a pre-execution static check in the FastAPI server.
- **Symlink containment:** The Apptainer version in use must support symlink containment within bind mounts (verify with `apptainer --version` ≥ 1.1). Path traversal via symlinks (e.g., a symlink inside `/aspen_workspace/` pointing to `/projects/` or beyond) must not be possible. This must be confirmed during initial deployment and documented in the deployment checklist.
- **PID namespace isolation:** Apptainer uses Linux PID namespaces by default, which means subprocesses spawned inside the container (e.g., via `multiprocessing.Process`) are terminated when the container is killed on timeout. Confirm this is active with `apptainer inspect` on the built image before deployment.

### FastAPI Endpoint

```python
@app.post("/run_python_analysis/{project_name}")
def run_python_analysis(
    project_name: str,
    code: str,
    dataset: list[str],
    x_agent_secret: str = Header(...)   # validated against AGENT_INTERNAL_SECRET
):
    """
    Execute LLM-generated Python code in a sandboxed Apptainer container.

    Parameters:
    - project_name: Must match a directory under /projects/ with a valid metadata file
    - code: Python code string generated by the LLM
    - dataset: List of run directory names to mount and analyze
    - x_agent_secret: Must match AGENT_INTERNAL_SECRET or request is rejected (HTTP 403)

    Returns:
    - stdout: Captured standard output (truncated to 10,000 characters)
    - stderr: Captured standard error (truncated to 2,000 characters)
    - figures: List of paths to generated PNG files in /aspen_workspace/figures/
    - truncated: Boolean indicating whether output was truncated
    - status: 'success' | 'error' | 'timeout'
    """
```

---

## 9. Output and Figure Handling

### stdout / stderr Size Limits

- **stdout** is truncated to **10,000 characters** before being passed back to the LLM and posted to Slack.
- **stderr** is truncated to **2,000 characters**.
- If truncation occurs, `truncated: true` is set in the response, and the bot appends the following note to its Slack reply:

  > ⚠️ Output was truncated (limit: 10,000 characters for stdout, 2,000 for stderr). Consider narrowing your dataset or printing only summary statistics.

### Figure Size Limit and Resolution Fallback

- **Maximum figure file size: 5 MB per PNG.**
- Before uploading a figure to Slack, the FastAPI server checks file size. If the file exceeds 5 MB:
  1. The LLM is informed via a tool result message: "Figure exceeded the 5 MB upload limit. Please regenerate at lower resolution (e.g., reduce `dpi` from 300 to 100, or reduce figure dimensions)."
  2. The LLM attempts to generate a lower-resolution version.
  3. If the lower-resolution version also exceeds 5 MB, the bot posts a text message to Slack instead: "Figure could not be uploaded (exceeds 5 MB even at reduced resolution). Results are summarized in text above."
- The LLM's system prompt must include the following instruction: "When saving matplotlib figures, default to `dpi=150`. If a figure upload fails due to size, retry with `dpi=72` and halved figure dimensions."

### Figure Archiving and Cleanup

After a figure is successfully uploaded to Slack:
1. The PNG is **moved** (not copied) from `/aspen_workspace/figures/` to `/aspen_workspace/figure_archive/`.
2. Files in `/aspen_workspace/figures/` are thus transient — they exist only between generation and upload.

**Archive cleanup policy:** When the total size of `/aspen_workspace/figure_archive/` exceeds **2 GB**, the oldest files (by file modification time) are deleted until total size is below 1.5 GB. This cleanup runs as a check at the start of each FastAPI request, not as a separate cron job, to avoid scheduling complexity. The cleanup is logged in the structured audit log.

---

## 10. Multi-Turn Conversation Context

Aspen maintains conversation history on a per-Slack-thread basis, keyed by the Slack `thread_ts` (thread timestamp), which is unique per thread.

### Rules

- History is stored **in-memory** in `aspen-bot.py`. It is not persisted to disk. Restarting the bot clears all conversation history.
- Each history entry contains the role (`user` or `assistant`) and content of the message.
- History is capped at the **last 20 turns** (10 user + 10 assistant) per thread. Older turns are dropped from the front of the list.
- Threads with no new messages for **4 hours** are considered expired. On the next message in an expired thread, history is reset and the bot treats it as a new conversation. The bot does not notify the user when history is reset.
- The full thread history (up to the cap) is included in every API call to the LLM as the `messages` array.

### Thread Identity

- Messages in a Slack channel thread: keyed on `thread_ts` of the parent message.
- Direct messages (DMs): keyed on the DM channel ID + message timestamp, treated as single-turn unless the user replies in a thread.

---

## 11. Rate Limiting

Rate limits are enforced in `aspen-bot.py` before any request is forwarded to FastAPI. They are per-Slack user ID, tracked in-memory.

| Limit | Value |
|---|---|
| Requests per user per 10-minute window | 5 |
| Concurrent executions per user | 1 |
| Concurrent executions across all users (global) | 2 |

If a user exceeds the rate limit, Aspen replies immediately in the same thread:

> "You've sent 5 requests in the last 10 minutes. Please wait a moment before asking again."

If a user already has an analysis running and submits a new request, Aspen replies:

> "I'm still working on your previous request. I'll post results here when it's done."

Rate limit state is in-memory and resets when the bot restarts. There is no persistent rate limit database for v1.

### Global Concurrency Cap

In addition to the per-user limit, a single global semaphore caps the total number of Apptainer containers running concurrently across all users (`MAX_CONCURRENT_EXECUTIONS`, default 2). This bounds total resource use on the shared node: without it, N authorized users each running one execution could launch N simultaneous 8 GB / 120 s containers. When the global cap is reached, a new request waits briefly and, if still blocked, receives: "Aspen is busy running other analyses right now — please try again in a moment."

### Single-Process Requirement

The rate-limit and concurrency guarantees depend on all state living in one process. The bot must run as a single `aspen-bot.py` instance, and the FastAPI server must run single-process (`uvicorn ... --workers 1`). Running multiple workers would split the in-memory rate-limit, per-user concurrency, and global-semaphore state across processes and silently break all three guarantees. Multi-process operation is out of scope for v1.

---

## 12. Caching Layer

Cache key:
```
SHA-256( question_text + sorted(dataset_ids) + max(file_modification_timestamps) )
```

The inclusion of `max(file_modification_timestamps)` ensures that if new data arrives in a run directory, the cache is invalidated automatically.

Cache is stored in `/aspen_workspace/cache/<project_name>/<hash>.json`. Cache files contain the stdout, stderr, and figure paths from a prior execution. Cache hits skip Apptainer execution entirely and re-upload the archived figure.

Because archived figures can be removed by the cleanup policy (Section 9) while a cache entry has no expiry, a cache hit must verify that each referenced figure still exists on disk. If any referenced figure is missing, the cache entry is treated as a miss and the analysis is re-executed (and re-cached), rather than returning a broken or figure-less result.

Cache entries have no automatic expiry in v1 — they are invalidated only by data changes (via the timestamp component of the cache key).

---

## 13. Logging and Auditing

Structured JSON logs are written to `/aspen_workspace/logs/<project_name>/` by the FastAPI server after each execution.

### Log Schema

```json
{
  "timestamp": "2025-01-15T14:32:01Z",
  "user_id": "U01234ABCDE",
  "username": "slack_display_name",
  "thread_ts": "1234567890.123456",
  "project": "drug_binding",
  "question": "Plot binding energies for run01 and run02",
  "dataset": ["run_001", "run_002"],
  "generated_code": "import pandas as pd\n...",
  "figures": ["/aspen_workspace/figure_archive/abc123.png"],
  "stdout_truncated": false,
  "status": "success",
  "errors": "",
  "duration_seconds": 14.3,
  "cache_hit": false
}
```

### Access Rules

- Log files are written by the FastAPI server (running as `aspen-agent`).
- Log files are **read-only for the Apptainer container** — the sandbox cannot write to or modify logs.
- Admin access to logs is by direct filesystem access under the `aspen-agent` account or a designated admin group.
- Logs must never contain: Slack tokens, the `AGENT_INTERNAL_SECRET`, Anthropic API keys, or any HPC credentials.

### stdout Filtering Before Logging

With `--cleanenv` in place (Section 8), the container environment holds no secrets, so the primary protection against leaks is that there is nothing sensitive to print. The filtering below is a defense-in-depth backstop — it must still be implemented, but it is not the sole control and must not be relied on as one (it is defeated by code that transforms a secret before printing, e.g. base64 or character-by-character).

Before the `stdout` field is written to the structured log, it must be passed through a filtering step that redacts environment variable dumps. Specifically:

- If stdout contains patterns matching `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ANTHROPIC_API_KEY`, `AGENT_INTERNAL_SECRET`, or any string matching `xoxb-`, `xapp-`, or `sk-ant-`, those lines must be replaced with `[REDACTED BY ASPEN LOG FILTER]`.
- This filter runs on stdout **before** truncation and **before** writing to disk or returning to the LLM.
- The same filter applies to stderr.

This prevents LLM-generated code that calls `print(os.environ)` or similar from leaking secrets into audit logs.

### Exception Scrubbing

Python exception tracebacks logged by `aspen-bot.py` or the FastAPI server must not include raw environment variable values. Exception handlers must catch and log only the exception type and message — not the full traceback if it could contain environment-derived strings. Use a scrubbing wrapper around all top-level exception handlers.

---

## 14. Security Summary

| Layer | Protection |
|---|---|
| Process identity | Dedicated `aspen-agent` service account, no login shell |
| Process lifecycle | systemd service with `NoNewPrivileges`, `ProtectSystem`, `PrivateTmp` |
| Authorization | Slack user-ID allowlist — only allowlisted users can invoke Aspen; first gate, before rate limiting |
| Slack connection | Socket Mode — outbound WebSocket only, no open ports |
| FastAPI | Binds to `127.0.0.1` only; shared secret on every request |
| Project data | Read-only mount inside Apptainer container |
| Agent workspace | Writable, but isolated from project directories |
| Generated code | UUID filenames; deleted immediately after execution |
| Container | No network; clean environment (`--cleanenv`, no secrets inside); 120s timeout with SIGKILL; 8 GB memory cap (verified enforced); minimal image is the real boundary |
| Stdout/stderr | Truncated to 10,000 / 2,000 characters before leaving FastAPI |
| Figures | 5 MB upload cap; archived then cleaned at 2 GB threshold |
| Rate limiting | 5 requests / 10 min / user; 1 concurrent execution / user; global cap on concurrent executions |
| Logging | Structured JSON; read-only for container; no secrets in logs |
| Secrets | All in `.env` file, chmod 600, owned by `aspen-agent` |

⚠️ **Hard constraints — the agent can never:**
- Act on a request from a Slack user not on the configured allowlist
- Write to any directory under `/projects/`
- Submit, cancel, or inspect Slurm jobs
- Access files outside its workspace or allowed project mount
- Make network calls from inside the Apptainer container
- Operate without a valid `metadata.toml` or `metadata.yaml` in the project directory

---

## 15. Environment Variables (`.env`)

```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# Authorization
# Comma-separated Slack user IDs allowed to invoke Aspen.
# In development mode this must contain ONLY the developer's own user ID.
ASPEN_ALLOWED_SLACK_USER_IDS=U0XXXXXXXXX

# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Internal security
AGENT_INTERNAL_SECRET=<32-byte hex string generated at setup>

# Paths
# In development mode these point under the developer's group data path,
# e.g. /sdf/data/<group>/projects and /sdf/data/<group>/aspen_workspace.
PROJECTS_ROOT=/projects
WORKSPACE_ROOT=/aspen_workspace
APPTAINER_IMAGE=/opt/aspen-agent/aspen.sif
# SQLite databases: prefer node-local disk (see Section 6). Only fall back to
# WORKSPACE_ROOT/db with the default rollback journal, never WAL, on a parallel FS.
SQLITE_DB_ROOT=/tmp/aspen_db

# Tuning
MAX_STDOUT_CHARS=10000
MAX_STDERR_CHARS=2000
MAX_FIGURE_BYTES=5242880       # 5 MB
FIGURE_ARCHIVE_MAX_BYTES=2147483648   # 2 GB
FIGURE_ARCHIVE_TRIM_BYTES=1610612736  # 1.5 GB
RATE_LIMIT_REQUESTS=5
RATE_LIMIT_WINDOW_SECONDS=600
MAX_CONCURRENT_EXECUTIONS=2    # global cap across all users
CONTEXT_MAX_TURNS=20
CONTEXT_EXPIRY_SECONDS=14400   # 4 hours
EXECUTION_TIMEOUT_SECONDS=120
```

All values that appear in this spec as magic numbers must reference the corresponding environment variable — they must not be hardcoded in source code.

---

## 16. Apptainer Image Management

- The Apptainer image (`aspen.sif`) is built from a definition file version-controlled alongside the bot source code.
- The image is built by the `aspen-agent` account (or an admin) and stored at the path specified in `APPTAINER_IMAGE`.
- When project `allowed_libraries` change (i.e., a new library is added to a project's metadata), the image must be rebuilt and the systemd service restarted. This is an explicit manual step — there is no automatic image rebuilding.
- The image build definition file must be included in the repository as `aspen.def`.
- The image must contain **only** the libraries analysis requires (the union of all projects' `allowed_libraries`) plus their dependencies — nothing network-capable, no compilers, no extra shells. The installed set, not the per-project allowlist, is the real capability boundary for sandboxed code (Section 8), so anything in the image is something arbitrary generated code can use.

---

## 17. Out of Scope for v1

The following are explicitly deferred and must not be implemented by the coding agent:

- LLM-assisted metadata/indexing suggestions
- Automatic Slurm job submission or monitoring
- Multi-user access controls beyond the Slack user allowlist (Section 3) and rate limiting — e.g. per-user or per-project permissions, where different users get different project access
- Persistent conversation history (survives bot restart)
- Web UI or non-Slack interface
- Migration from SQLite to PostgreSQL
- Automatic Apptainer image rebuilding on metadata changes
- Async/background thread for figure archive trimming (synchronous cleanup per request is sufficient for v1 load)
- Shared secret rotation mechanism (the secret is generated once at setup; rotation is a manual process documented in the ops runbook, not automated in v1)

---

## 18. Required Test Suite

The test suite is a **required deliverable** of v1, not optional. The coding agent must implement all tests described below alongside the main implementation. Tests must be runnable without a live Slack connection or Anthropic API key (use mocks for both). Tests must be runnable from the repository root with a single command (e.g., `pytest tests/`).

---

### 18.1 Unit Tests

**FastAPI authentication**
- Request with correct `X-Agent-Secret` header → HTTP 200
- Request with incorrect secret → HTTP 403
- Request with missing secret header → HTTP 403

**FastAPI input validation**
- `project_name` referencing a directory with no metadata file → structured error response, not a 500
- `dataset` containing a run directory that does not exist → structured error response
- `code` field that is an empty string → structured error response

**Cache key generation**
- Same question + same dataset + same timestamps → identical cache key
- Same question + same dataset + different timestamps → different cache key
- Same question + different dataset order (e.g., `["run_01", "run_02"]` vs `["run_02", "run_01"]`) → identical cache key (dataset is sorted before hashing)

**Metadata parsing**
- Valid `metadata.toml` → correctly parsed into expected Python structure
- Valid `metadata.yaml` → correctly parsed into expected Python structure
- Missing metadata file → returns the correct user-facing error message with template
- Malformed TOML → structured error, not an unhandled exception
- Malformed YAML → structured error, not an unhandled exception

**stdout/stderr filtering**
- stdout containing `SLACK_BOT_TOKEN=xoxb-abc123` → filtered to `[REDACTED BY ASPEN LOG FILTER]`
- stdout containing `sk-ant-` anywhere in a line → that line redacted
- stdout with no sensitive content → returned unchanged
- Filtering applied before truncation (verify order of operations)

**Rate limiting**
- 5 requests within 10 minutes from same user ID → 6th request returns rate limit message without calling FastAPI
- 5 requests from user A and 5 from user B simultaneously → both permitted (limits are per-user)
- Rate limit counter resets after window expires

**Conversation history**
- Thread with 25 turns → only last 20 retained
- Thread inactive for 4+ hours → history cleared on next message
- Two different `thread_ts` values → independent histories, no cross-contamination

---

### 18.2 Sandbox Execution Tests

These tests run actual Apptainer container executions against the test image and are marked `@pytest.mark.sandbox`. They require Apptainer to be installed on the test machine and the `aspen.sif` image to be built.

**Import allowlist enforcement**
- Code importing a library in `allowed_libraries` → executes successfully
- Code importing `os` (when not in allowlist) → raises `ImportError`, execution fails cleanly
- Code importing `socket` → raises `ImportError`
- Code importing `subprocess` → raises `ImportError`
- Code using `__import__('os')` → blocked
- Code using `importlib.import_module('os')` → blocked
- Code using `eval("__import__('os')")` → rejected at pre-execution static check before container launch
- Code using `exec("import os")` → rejected at pre-execution static check

**Filesystem access enforcement**
- Code writing to `/aspen_workspace/figures/` → succeeds
- Code writing to `/aspen_workspace/cache/` → succeeds
- Code attempting to write to `/projects/` → fails with permission error
- Code attempting to write to `/tmp/` outside workspace → fails
- Code attempting to read `/projects/<project>/` → succeeds (read-only mount)

**Timeout enforcement**
- Code containing `import time; time.sleep(200)` → container killed after 120s, FastAPI returns timeout error response, no zombie process remains

**Output size limits**
- Code that prints 50,000 characters to stdout → stdout in response truncated to 10,000 characters, `truncated: true` set
- Truncation message appended to Slack reply

---

### 18.3 Concurrency Tests

**SQLite concurrency**
- 10 simultaneous FastAPI requests against the same project database → no `database is locked` errors, all complete successfully
- Verify the configured pragmas are set on every new connection: `PRAGMA busy_timeout` is non-zero, and `PRAGMA journal_mode` matches the deployment choice (`wal` when the database is on node-local disk; the default rollback journal when on the group data path). Inspect via `PRAGMA journal_mode;` / `PRAGMA busy_timeout;` in test setup.

**Concurrent user requests**
- Two users submitting requests simultaneously → both handled independently, rate limits tracked separately
- One user with an in-flight request submitting a second → second request returns "still working" message, first completes normally

---

### 18.4 Security Tests

**Malicious code containment**
- Code containing `__import__('os').system('rm -rf /aspen_workspace/')` → blocked at static check or import hook; workspace contents unchanged after execution attempt
- Code containing `open('/projects/drug_binding/experiment_01/energy.csv', 'w')` → fails with permission error; file unmodified
- Code containing `print(os.environ)` → executes (os not blocked if in allowlist) but stdout filtered before logging; no secrets appear in log file

**Log secrets audit**
- After executing a successful analysis, read the written log file and assert it contains none of: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ANTHROPIC_API_KEY`, `AGENT_INTERNAL_SECRET`, `xoxb-`, `xapp-`, `sk-ant-`
- Run same assertion after a failed/timeout execution

**Exception scrubbing**
- Simulate an exception that includes an environment variable value in its message → assert that the logged exception does not contain the raw value

---

### 18.5 End-to-End Tests

These tests use a mock Slack client and a mock Anthropic API (returning pre-written code strings) to test the full pipeline without live external services. They are marked `@pytest.mark.e2e`.

**Successful analysis with figure**
- Mock LLM returns valid matplotlib code for a known test dataset
- Code executes in sandbox, produces a PNG in `/aspen_workspace/figures/`
- PNG is within size limit → mock Slack upload called with correct file path
- PNG moved to `/aspen_workspace/figure_archive/`
- `/aspen_workspace/figures/` is empty after upload
- Log file written with `status: success` and correct fields

**Figure over size limit — retry**
- Mock LLM returns code that produces a PNG > 5 MB
- FastAPI returns size-limit error to LLM
- Mock LLM returns lower-resolution code on retry
- Lower-resolution PNG uploaded successfully

**Figure over size limit — both attempts fail**
- Both initial and retry figures exceed 5 MB
- Bot posts text-only message to Slack, no file upload attempted
- Log reflects failure

**Archive cleanup trigger**
- Pre-populate `/aspen_workspace/figure_archive/` with dummy files totalling > 2 GB
- Submit any successful analysis request
- After request completes, assert archive size is below 1.5 GB
- Assert oldest files were removed first

---

### 18.6 Edge Case Tests

- **Empty dataset list** (`dataset: []`) → structured error response, no container launched
- **Missing run directory** (run listed in dataset does not exist on filesystem) → structured error before container launch
- **Corrupted CSV** (analysis code tries to parse a malformed CSV) → stderr captured, structured error returned to user, no crash in FastAPI
- **Large figure file** (> 5 MB, both attempts) → text-only Slack response, no unhandled exception
- **Metadata file present but missing required fields** (e.g., no `allowed_libraries`) → structured error with clear message identifying the missing field
- **Project name with path traversal attempt** (e.g., `project_name = "../other_project"`) → rejected with HTTP 400 before any filesystem access

---

### 18.7 Test Infrastructure Requirements

- All tests must be runnable with `pytest tests/` from the repository root
- Sandbox tests (`@pytest.mark.sandbox`) must be skippable on machines without Apptainer: `pytest tests/ -m "not sandbox"`
- End-to-end tests (`@pytest.mark.e2e`) must be skippable similarly: `pytest tests/ -m "not e2e"`
- A `tests/fixtures/` directory must contain:
  - A minimal valid `metadata.toml` and `metadata.yaml` for a test project
  - A small test dataset (synthetic CSVs, < 1 MB total) that the sandbox tests can read
  - Pre-written LLM response strings (mock code) for end-to-end tests
- Test coverage must be measured and reported. Minimum acceptable line coverage for `aspen-bot.py` and the FastAPI server: **80%**

---

## 19. V2 Placeholder — Job Submission (ORCA/CORVUS Pipeline)

This section reserves the design space for job submission in v2. It must not be implemented in v1. The coding agent implementing v1 must be aware of this section so that architectural decisions do not preclude adding it later.

The pipeline being integrated is `submit-batch.py` from the `orca-pipeline` repository. It submits an ORCA → CORVUS → postprocess workflow as a chain of PBS-dependent jobs, one chain per `.xyz` structure file.

---

### 19.1 Design Principles

Job submission in v2 must adhere to the following principles, which are non-negotiable regardless of implementation details decided later:

- **No agent-written code touches the pipeline.** The agent may only invoke `submit-batch.py` with explicitly validated CLI arguments. It cannot modify pipeline scripts, generate new ones, or compose shell commands arbitrarily.
- **Every submission is fully logged before execution.** The exact command invoked, all CLI arguments, the submitting user, all returned job IDs, and the timestamp must be written to the audit log before `qsub` is called. If logging fails, the submission must not proceed.
- **Human-readable audit trail is required for reproducibility.** The log entry must contain enough information to reproduce the exact submission independently of the agent. The pipeline already writes `batch-jobs.log` and a `pipeline-state-<name>.json` with every job ID — these are the primary records. The SQLite `jobs` table (see Section 19.5) is a secondary index for the agent's own lookup, not a replacement.
- **`--no-submit` dry run is always called first.** Before any real submission, the FastAPI endpoint must invoke `submit-batch.py --no-submit` with the same arguments and verify it succeeds. If the dry run fails, the real submission is aborted and the error is returned to the user. This catches bad paths, missing XYZ files, and missing templates before any jobs are queued.
- **Scheduler abstraction is handled in the pipeline, not in Aspen.** The `orca-pipeline` will be made scheduler-agnostic (PBS/Slurm) before v2 is implemented. Aspen's FastAPI endpoint calls `submit-batch.py` as a subprocess and does not need to know which scheduler is in use — that is entirely the pipeline's concern. Cancellation is scoped strictly to job IDs recorded in the agent's own SQLite `jobs` table — the agent never queries the scheduler for a list of running jobs and cancels from that.

---

### 19.2 Intended Interface

The v2 job submission tool exposes two FastAPI endpoints: one to submit a batch, one to cancel it.

#### Submit

```python
@app.post("/submit_orca_batch")
def submit_orca_batch(
    structures_path: str,        # path relative to /projects/<project>/
                                 # must be a directory of .xyz files or a single .xyz file
    template_mode: str,          # one of: "ca-fixed", "h-only", "single-point",
                                 # "no-constraints", "backbone", "xtb-free", "xtb-constrained"
    out_dir: str | None = None,  # optional; relative to /projects/<project>/
                                 # defaults to parent of structures_path per pipeline behaviour
    dry_run: bool = False,       # if True, passes --no-submit; no jobs queued
    x_agent_secret: str = Header(...)
):
    """
    Submit an ORCA->CORVUS->postprocess batch via submit-batch.py.
    Always runs --no-submit dry run first to validate inputs.
    Returns: batch_id, list of (run_id, orca_job_id, corvus_job_id), postprocess_job_id,
             path to pipeline-state JSON, path to batch-jobs.log.
    """
```

`template_mode` is validated against the fixed allowlist above — the agent cannot pass an arbitrary string. The `structures_path` and `out_dir` are validated to be within `/projects/<project>/` before any subprocess call — path traversal attempts are rejected with HTTP 400.

#### Cancel

```python
@app.post("/cancel_orca_batch")
def cancel_orca_batch(
    batch_id: str,               # UUID assigned at submission time, stored in SQLite
    x_agent_secret: str = Header(...)
):
    """
    Cancel all cancellable jobs for a batch submitted by Aspen.
    Only cancels jobs whose IDs appear in the agent's own jobs table.
    Cancels CORVUS job IDs only — the dependent postprocess job is automatically
    killed by the scheduler's dependency chain. ORCA jobs are typically already
    complete by the time cancellation is requested and are skipped if so.
    """
```

---

### 19.3 Cancellation Logic

Cancelling a batch does not require cancelling every job ID individually. The pipeline's PBS/Slurm dependency chain means:

- Cancelling a CORVUS job automatically kills the dependent postprocess job via `afterok` dependency deletion
- ORCA jobs are typically already complete by the time a user asks to cancel — the endpoint checks job status before attempting `qdel`/`scancel` and skips jobs that are no longer running
- The postprocess job ID is stored for reference but does not need to be explicitly cancelled

The agent posts a summary to Slack listing which job IDs were cancelled, which were already complete, and which (if any) failed to cancel.

---

### 19.4 SQLite `jobs` Table

Add the following table to the per-project SQLite database in v2:

```sql
CREATE TABLE jobs (
    batch_id         TEXT PRIMARY KEY,   -- UUID generated at submission time
    submitted_utc    TEXT NOT NULL,       -- ISO 8601
    submitted_by     TEXT NOT NULL,       -- Slack user ID
    structures_path  TEXT NOT NULL,
    template_mode    TEXT NOT NULL,
    out_dir          TEXT,
    state_file_path  TEXT NOT NULL,       -- path to pipeline-state-<name>.json
    batch_log_path   TEXT NOT NULL,       -- path to batch-jobs.log
    postprocess_job_id TEXT,
    status           TEXT NOT NULL        -- 'submitted', 'cancelled', 'completed'
);

CREATE TABLE job_runs (
    batch_id         TEXT NOT NULL REFERENCES jobs(batch_id),
    run_id           TEXT NOT NULL,
    orca_job_id      TEXT NOT NULL,
    corvus_job_id    TEXT NOT NULL,
    PRIMARY KEY (batch_id, run_id)
);
```

The pipeline's own `pipeline-state-<name>.json` and `batch-jobs.log` are the authoritative records. These tables exist solely so the agent can look up its own submissions efficiently without parsing JSON files.

---

### 19.5 What to Avoid in V1 Architecture

The coding agent implementing v1 must not make the following decisions, as they would make v2 harder to add:

- Do not hardcode the sandbox as the only execution path — the FastAPI server must be structured so new route files can be added without modifying existing ones
- Do not couple `aspen-bot.py`'s tool dispatch so tightly to the sandbox that adding `submit_orca_batch` requires significant refactoring
- The SQLite schema introduced in v1 must not conflict with the `jobs` and `job_runs` tables above — reserve those table names

