# Slack App Setup — Aspen

How to create (or recreate) the Slack app that Aspen connects to. This is the
**operator runbook**: do this once per workspace, and again only if the app is
deleted, the tokens are rotated, or someone forks the code for their own agent.

Aspen talks to Slack over **Socket Mode** (an outbound WebSocket — no public URL,
no inbound ports). The bot needs two tokens and a small set of scopes/events; once
installed you paste the tokens into `.env` and run `start.sh`.

> The design rationale for these choices lives in [`spec.md` §3](spec.md#3-slack-integration--socket-mode);
> the security reasoning is in [`THREAT_MODEL.md`](THREAT_MODEL.md). This file is
> the *how-to*.

---

## Fast path — create from the manifest

Slack can build the whole app config from a manifest in one step:

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick the workspace, choose **YAML**, and paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
3. Create the app, then jump to [**Tokens & install**](#tokens--install) below.

The manifest captures the display name, bot scopes, subscribed events, and Socket
Mode. What it can't set — all covered below — is minting the tokens, enabling the
App Home **Messages** tab so people can DM Aspen, and (optionally) turning on the
**Agent experience** toggle for the native "is typing…" status.

---

## Manual path — what the manifest encodes

If you'd rather click through it (or audit what the manifest sets), here is the
full configuration.

### 1. Socket Mode

**Settings → Socket Mode → Enable Socket Mode.** This is what lets the bot run on a
cluster login node with no inbound firewall holes.

### 2. App-Level Token (`xapp-…`)

Enabling Socket Mode prompts for an app-level token. Create one named e.g.
`socket` with the **`connections:write`** scope. This is `SLACK_APP_TOKEN` in `.env`.

### 3. Bot Token Scopes (`xoxb-…`)

**OAuth & Permissions → Scopes → Bot Token Scopes:**

| Scope | Why Aspen needs it |
|---|---|
| `app_mentions:read` | Receive `@Aspen` mentions (the only thing it acts on) |
| `chat:write` | Post replies; also drives the native "is typing…" status |
| `files:write` | Upload figures and attached files |
| `im:history` | Read its own 1:1 DM threads for context |
| `channels:history` | Read public-channel threads it's in, for context |
| `mpim:history` | Read **group-DM** threads it's in, for context |
| `mpim:read` | List a group DM's members — for the participant gate and to classify `app_mention`s as group DMs |
| `users:read` | Resolve member IDs → display names and spot app/bot members, for the participant gate's check and its reply |

No `channels:read` and no broad history scopes: Aspen only sees conversations it's
been mentioned/DMed in.

### 4. Event Subscriptions

**Event Subscriptions → Enable Events** (no Request URL needed — Socket Mode
delivers them over the WebSocket). Under **Subscribe to bot events** add:

| Bot event | Delivers |
|---|---|
| `app_mention` | `@Aspen` in channels, group DMs, and 1:1 DMs — the primary trigger |
| `message.im` | Messages in Aspen's own 1:1 DM (so you can DM it without `@`) |
| `message.mpim` | Follow-up messages in a group DM Aspen has already joined — so a thread can continue without re-`@`-mentioning it |

`message.mpim` is subscribed but used narrowly: an `@Aspen` mention still *starts* a
group-DM thread, and once Aspen has replied there, `message.mpim` lets later messages
in that same thread reach it without another mention. Group-DM messages outside a
thread Aspen has joined are ignored (see [`slack_app.py`](aspen/slack_app.py)).

You do **not** need `message.channels`: in public channels Aspen responds only to
`app_mention`, so subscribing it would just deliver events the bot drops.

### 5. (Optional) Agent experience — the native "Aspen is typing…" status

The polished in-thread "Aspen is typing…" indicator uses
`assistant.threads.setStatus`, which requires the app's agent features to be on:
**Features → Agents → turn on "Agent experience"** (a single toggle; this replaced
the older "Agents & AI Apps / Assistant" panel). It's optional — without it, the
first `setStatus` call fails and Aspen falls back to posting a plain `_Thinking…_`
message, which the code handles gracefully, so nothing breaks.

### 6. App Home — display name and DM access

**Features → App Home:**

- Set the bot display name to **Aspen** so the status shows as "Aspen is typing…".
- Scroll to **Show Tabs → Messages Tab** and enable **"Allow users to send Slash
  commands and messages from the messages tab."** Without this checkbox the Messages
  tab is read-only: users can't DM the app, so the `message.im` event never fires and
  1:1 DMs silently don't work.

---

## Tokens & install

1. **Install App → Install to Workspace**, approve the scopes.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) → `SLACK_BOT_TOKEN` in `.env`.
3. Copy the **App-Level Token** (`xapp-…`, from step 2 above) → `SLACK_APP_TOKEN`.
4. Set `ASPEN_ALLOWED_SLACK_USER_IDS` to the allowed Slack user IDs. **The first ID
   is treated as the admin** (named in "not authorized" / group-DM refusals);
   override with `ASPEN_ADMIN_SLACK_USER_ID` if needed. See [`.env.example`](.env.example).
5. `bash start.sh`.

Find a user's ID: Slack → click their name → **View full profile → … → Copy member ID**.

---

## Adding Aspen to conversations

- **Public channel:** `/invite @Aspen`, then `@Aspen …`.
- **Group DM (multi-person DM):** start/open a group DM, type `@Aspen`, and confirm
  Slack's "Add to conversation?" prompt. **Every human member must be on the
  allowlist** or Aspen declines (the participant gate — [`spec.md` §3](spec.md#participant-gate-group-dms)).
  Once Aspen has replied in a thread, you can keep the conversation going with plain
  replies in that thread — no re-`@Aspen` needed (this uses the `message.mpim` event).
- **1:1 with Aspen:** just open a DM with the app and message it (no `@` needed).
- **1:1 between two people:** *not possible* — Slack does not allow adding a bot to
  an existing direct message between two humans. Use a group DM that includes Aspen.

---

## Reinstalling / changing scopes

- **Any scope or event change requires a reinstall** (**OAuth & Permissions →
  Reinstall to Workspace**). The `xoxb-`/`xapp-` tokens stay the same across a
  reinstall, so `.env` doesn't change.
- **Rotating tokens** (e.g. at the service-account cutover — [`THREAT_MODEL.md` §7](THREAT_MODEL.md)):
  regenerate the bot token under OAuth & Permissions and the app-level token under
  Basic Information → App-Level Tokens, then update `.env` and restart.

---

## Recreate-from-scratch checklist

1. Create app from [`slack-app-manifest.yaml`](slack-app-manifest.yaml) (or the manual steps above).
2. Generate the app-level token (`connections:write`).
3. Enable the App Home **Messages** tab (App Home → Show Tabs → "Allow users to send
   Slash commands and messages from the messages tab") so people can DM Aspen.
4. (Optional) turn on **Agents → Agent experience** for the native typing status.
5. Install to workspace; copy both tokens into `.env`.
6. Set `ASPEN_ALLOWED_SLACK_USER_IDS` (first = admin).
7. `bash start.sh`; in Slack, `@Aspen hello` in a DM to smoke-test.
