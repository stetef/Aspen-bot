"""
Reading the turn log.

**This is the one place outside the bot that knows the log format.** It reads the
JSONL directly rather than importing ``aspen.telemetry``: that module imports
``aspen.config``, which requires SLACK_BOT_TOKEN and friends at import time, so
importing it would tie a read-only web app to the bot's whole runtime and its
secrets. The format is append-only and every field is read with ``.get``, so a
new field added by the bot shows up here as a new column and an old log missing
one reads as null.

Nothing in this module writes.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DEFAULT_LOG_DIR = Path(
    os.getenv("ASPEN_TELEMETRY_DIR")
    or (Path(os.getenv("ASPEN_STATE_DIR", str(Path.home() / ".aspen"))) / "telemetry")
)

# Outcomes that mean the user did not get an answer. `empty` is not a failure —
# it's someone saying hello.
FAILURE_OUTCOMES = {"error", "timeout", "rate_limited", "busy",
                    "not_authorized", "group_gate"}


def read_turns(log_dir: Path) -> pd.DataFrame:
    """Every recorded turn, oldest first. Malformed lines are skipped."""
    rows: list[dict] = []
    try:
        files = sorted(p for p in Path(log_dir).glob("*.jsonl") if p.is_file())
    except OSError:
        files = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue        # a torn final write; skip the line
        except OSError:
            continue
    return _frame(rows)


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts"]).sort_values("ts")
    df["day"] = df["ts"].dt.floor("D")
    # An alias is friendlier than a Slack ID but only exists for registered
    # users; fall back so an unregistered sender is still countable.
    df["who"] = df.get("alias", pd.Series(dtype=str)).replace("", pd.NA)
    df["who"] = df["who"].fillna(df["uid"]).fillna("unknown")
    for col in ("latency_ms", "first_output_ms", "input_tokens", "output_tokens",
                "agent_ms", "api_ms", "num_turns", "quota_utilization", "reply_chars"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = pd.NA
    if "tools" not in df:
        df["tools"] = [[] for _ in range(len(df))]
    df["tools"] = df["tools"].apply(lambda v: v if isinstance(v, list) else [])
    df["tool_count"] = df["tools"].apply(len)
    df["failed"] = df["outcome"].isin(FAILURE_OUTCOMES)
    df["tokens"] = df["input_tokens"].fillna(0) + df["output_tokens"].fillna(0)
    # The share of a turn that was neither model time nor tool time: session
    # wait, a pre-warm miss, preamble building. Ours to fix, unlike the rest.
    df["overhead_ms"] = df["latency_ms"] - df["agent_ms"]
    df["tool_ms"] = df["agent_ms"] - df["api_ms"]
    return df


def denied_commands(df: pd.DataFrame) -> pd.DataFrame:
    """Commands the allowlist or sandbox refused — a feature backlog, ranked."""
    if df.empty or "denials" not in df:
        return pd.DataFrame(columns=["tool", "command", "times"])
    rows = []
    # A turn logged before the field existed — or one with nothing denied — reads
    # as NaN here, which is truthy, so the type check is what does the guarding.
    for denials in df["denials"]:
        if not isinstance(denials, list):
            continue
        for d in denials:
            if isinstance(d, dict):
                rows.append({"tool": d.get("tool") or "?",
                             "command": (d.get("command") or "").strip()})
    if not rows:
        return pd.DataFrame(columns=["tool", "command", "times"])
    out = pd.DataFrame(rows).value_counts(["tool", "command"]).reset_index(name="times")
    return out.sort_values("times", ascending=False)


def tool_sequences(df: pd.DataFrame, length: int = 3, top: int = 12) -> pd.DataFrame:
    """Repeated n-grams of tool calls.

    A sequence that keeps recurring is a multi-step task the agent is assembling
    by hand every time — the strongest signal for what deserves one purpose-built
    tool instead.
    """
    if df.empty:
        return pd.DataFrame(columns=["sequence", "times"])
    counts: dict[tuple, int] = {}
    for tools in df["tools"]:
        for i in range(len(tools) - length + 1):
            key = tuple(tools[i:i + length])
            counts[key] = counts.get(key, 0) + 1
    rows = [{"sequence": " → ".join(k), "times": v}
            for k, v in counts.items() if v > 1]
    if not rows:
        return pd.DataFrame(columns=["sequence", "times"])
    return (pd.DataFrame(rows).sort_values("times", ascending=False)
            .head(top).reset_index(drop=True))


def per_user(df: pd.DataFrame) -> pd.DataFrame:
    """The table that answers 'who is using this, and what does it cost'."""
    if df.empty:
        return pd.DataFrame()
    g = df.groupby("who")
    out = pd.DataFrame({
        "turns": g.size(),
        "input tokens": g["input_tokens"].sum(min_count=1),
        "output tokens": g["output_tokens"].sum(min_count=1),
        "median answer (s)": g["latency_ms"].median() / 1000,
        "p90 answer (s)": g["latency_ms"].quantile(0.9) / 1000,
        "tools / turn": g["tool_count"].mean(),
        "failure rate": g["failed"].mean(),
    })
    return out.sort_values("turns", ascending=False).round(2)


# --------------------------------------------------------------------------- #
# Demo data — for developing the dashboard before real traffic accumulates
# --------------------------------------------------------------------------- #
_DEMO_USERS = ["arun", "priya", "sam", "wei", "marta"]
_DEMO_TOOLS = ["list_directory", "read_file", "search_files",
               "run_python_analysis", "Bash", "write_metadata", "read_workflow"]
_DEMO_QUESTIONS = [
    "where are my ORCA jobs for the Zn cluster?",
    "did the CORVUS run for 5a3w finish?",
    "plot the XAS spectra for the two clusters side by side",
    "why did job 4417238 fail?",
    "what functional did I use for the thermolysin run?",
    "compare the combined-xas output against separate mode",
    "how much queue time is left on my jobs?",
    "summarize what changed in the metadata for CSD",
]


def demo_turns(days: int = 21, seed: int = 7) -> pd.DataFrame:
    """A plausible three weeks, so the layout can be judged before real rows exist.

    Deliberately uneven: one heavy user, a bad afternoon of timeouts, and a quota
    window that climbs to a warning — the shapes the dashboard exists to surface.
    """
    rng = random.Random(seed)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    for d in range(days):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            volume = rng.randint(0, 4)          # quiet weekends
        else:
            volume = rng.randint(6, 22)
        heavy_day = d in (11, 12)               # someone running a big campaign
        for _ in range(volume):
            who = rng.choices(_DEMO_USERS, weights=[5, 3, 3, 2, 1])[0]
            if heavy_day:
                who = "arun" if rng.random() < 0.7 else who
            ts = day + timedelta(hours=rng.randint(8, 19), minutes=rng.randint(0, 59))
            n_tools = rng.choices([0, 1, 2, 3, 5, 9], weights=[1, 4, 5, 4, 2, 1])[0]
            tools = [rng.choice(_DEMO_TOOLS) for _ in range(n_tools)]
            roll = rng.random()
            if roll < 0.04:
                outcome, subtype = "error", "error_during_execution"
            elif heavy_day and roll < 0.20:
                # The failure mode worth seeing on screen: long turns running out
                # of rounds, clustered on the days someone pushed hard.
                outcome, subtype = "timeout", "error_max_turns"
            elif roll < 0.09:
                outcome, subtype = "rate_limited", None
            else:
                outcome, subtype = "ok", "success"
            api_ms = rng.randint(1500, 20000) + n_tools * rng.randint(200, 900)
            tool_ms = n_tools * rng.randint(300, 2500)
            overhead = rng.randint(50, 2200)
            rows.append({
                "ts": ts.isoformat(), "uid": f"U{who.upper()}", "alias": who,
                "outcome": outcome, "channel": rng.choice(["im", "im", "mpim"]),
                "thread": f"C{rng.randint(1, 40)}:{d}",
                "latency_ms": api_ms + tool_ms + overhead,
                "first_output_ms": overhead + rng.randint(400, 2600),
                "agent_ms": api_ms + tool_ms, "api_ms": api_ms,
                "tools": tools, "chars": rng.randint(20, 220),
                "reply_chars": rng.randint(120, 2400),
                "attachments": 1 if rng.random() < 0.12 else 0,
                "text": rng.choice(_DEMO_QUESTIONS),
                "result_subtype": subtype, "num_turns": max(1, n_tools),
                "input_tokens": rng.randint(6000, 30000) + n_tools * 1500,
                "output_tokens": rng.randint(200, 2500),
                "denials": ([{"tool": "Bash", "command": rng.choice(
                    ["scancel 4417238", "sbatch run.slurm", "cat /etc/passwd",
                     "tail -f orca.out"])}] if rng.random() < 0.06 else None),
            })
    frame = _frame(rows)
    return _add_demo_quota(frame, rng)


def _add_demo_quota(df: pd.DataFrame, rng: random.Random) -> pd.DataFrame:
    """Sprinkle the meter on a few turns, the way the CLI actually emits it —
    only when the state changes, so most turns carry nothing."""
    if df.empty:
        return df
    df = df.copy()
    df["quota_utilization"] = pd.NA
    df["quota_status"] = pd.NA
    for day, chunk in df.groupby("day"):
        marks = chunk.sample(min(3, len(chunk)), random_state=rng.randint(0, 9999))
        used = sorted(rng.uniform(0.15, 0.95) for _ in range(len(marks)))
        for (idx, _), value in zip(marks.iterrows(), used):
            df.loc[idx, "quota_utilization"] = round(value, 3)
            df.loc[idx, "quota_status"] = (
                "allowed_warning" if value > 0.8 else "allowed")
    df["quota_utilization"] = pd.to_numeric(df["quota_utilization"], errors="coerce")
    return df
