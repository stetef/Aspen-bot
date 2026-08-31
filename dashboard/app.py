"""
Aspen usage dashboard — read-only, localhost-only.

Run it through ``./aspen-dashboard`` rather than ``streamlit run``: the launcher
pins the bind address to 127.0.0.1. On a shared login node the default bind would
publish colleagues' questions to every account on s3df, which is precisely what
the 0600 log file mode exists to prevent.

Nothing here writes. It reads the JSONL turn log (see dashboard/data.py) and
renders. ``--demo`` substitutes synthetic rows so the layout can be judged before
real traffic accumulates; demo mode never touches the real log.

The Conversations panel reads a second source — the CLI transcripts, via
dashboard/transcripts.py — because the turn log deliberately keeps no reply text.
Those transcripts hold everything both sides said regardless of the telemetry
content switch, so what may be *shown* is gated on the turn log's own per-turn
decision rather than on what happens to be on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as dataio          # noqa: E402
import theme                   # noqa: E402
import transcripts as convo    # noqa: E402

st.set_page_config(page_title="Aspen usage", page_icon="🐺", layout="wide")

DEMO = "--demo" in sys.argv


def _mode() -> str:
    """Follow the viewer's Streamlit theme; assume light if it can't be read."""
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


MODE = _mode()
INK = theme.INK[MODE]
PRIMARY = theme.PRIMARY[MODE]


@st.cache_data(ttl=60, show_spinner=False)
def _load(log_dir: str, demo: bool, _stamp: float) -> pd.DataFrame:
    return dataio.demo_turns() if demo else dataio.read_turns(Path(log_dir))


@st.cache_data(ttl=60, show_spinner=False)
def _load_conversations(projects_dir: str, _stamp: float) -> list[dict]:
    """Parsing every transcript is seconds of work; the mtime stamp caches it."""
    return convo.read_all(Path(projects_dir))


def _stamp(log_dir: Path) -> float:
    """Newest mtime in the log dir — makes the cache refresh when the bot writes."""
    try:
        return max((p.stat().st_mtime for p in log_dir.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- #
# Chart builders
# --------------------------------------------------------------------------- #
def _bars_by_user(df: pd.DataFrame, y: str, title: str, y_title: str, domain: list[str]):
    """Daily stacked bars, one band per person.

    A 2px surface stroke separates the segments so adjacent bands never merge
    into one shape, and the legend is always present — identity is never carried
    by hue alone.
    """
    return (
        alt.Chart(df, title=title)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                  stroke=INK["surface"], strokeWidth=2)
        .encode(
            x=alt.X("day:T", title=None, axis=alt.Axis(grid=False, format="%b %d")),
            y=alt.Y(f"{y}:Q", title=y_title, stack="zero"),
            color=alt.Color("who:N", title="person",
                            scale=theme.series_scale(alt, domain, MODE),
                            legend=alt.Legend(orient="bottom", columns=4)),
            tooltip=[alt.Tooltip("day:T", title="day", format="%a %d %b"),
                     alt.Tooltip("who:N", title="person"),
                     alt.Tooltip(f"{y}:Q", title=y_title, format=",.0f")],
        )
        .properties(height=220)
    )


def _utilization_line(df: pd.DataFrame):
    """Peak share of the rate-limit window consumed, per day.

    Its own chart rather than a second axis on the volume chart: two measures on
    one plot with two scales invite a comparison the geometry doesn't support.
    Shares the day axis with the chart above it, which is what makes them
    readable together.
    """
    meter = df.dropna(subset=["quota_utilization"])
    if meter.empty:
        return None
    daily = (meter.groupby("day", as_index=False)["quota_utilization"].max())
    base = alt.Chart(daily, title="Peak rate-limit utilization (account-wide)")
    threshold = (
        alt.Chart(pd.DataFrame({"y": [0.8]}))
        .mark_rule(strokeDash=[4, 4], color=INK["muted"], strokeWidth=1)
        .encode(y="y:Q")
    )
    line = base.mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=60, filled=True)).encode(
        x=alt.X("day:T", title=None, axis=alt.Axis(grid=False, format="%b %d")),
        y=alt.Y("quota_utilization:Q", title="window used",
                axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        color=alt.value(PRIMARY),        # single series — no legend needed
        tooltip=[alt.Tooltip("day:T", title="day", format="%a %d %b"),
                 alt.Tooltip("quota_utilization:Q", title="used", format=".0%")],
    )
    return (threshold + line).properties(height=180)


def _hbars(df: pd.DataFrame, cat: str, val: str, title: str, cat_title: str):
    """One measure across categories — single hue; the axis label carries identity."""
    return (
        alt.Chart(df, title=title)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4,
                  color=PRIMARY, stroke=INK["surface"], strokeWidth=2)
        .encode(
            x=alt.X(f"{val}:Q", title=None),
            y=alt.Y(f"{cat}:N", sort="-x", title=None,
                    axis=alt.Axis(grid=False, labelLimit=220)),
            tooltip=[alt.Tooltip(f"{cat}:N", title=cat_title),
                     alt.Tooltip(f"{val}:Q", title="count", format=",.0f")],
        )
        .properties(height=max(120, 26 * len(df)))
    )


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
st.sidebar.title("Aspen usage")
if DEMO:
    st.sidebar.warning("Demo mode — synthetic data, not your real log.")
log_dir = Path(st.sidebar.text_input("Log directory", str(dataio.DEFAULT_LOG_DIR)))

turns = _load(str(log_dir), DEMO, _stamp(log_dir))

if turns.empty:
    st.title("Aspen usage")
    st.info(
        f"No turns recorded yet in `{log_dir}`.\n\n"
        "The bot writes one line per turn once it is running with telemetry on "
        "(`./aspen-users telemetry status`). To see the layout meanwhile, "
        "relaunch with `./aspen-dashboard --demo`."
    )
    st.stop()

days = sorted(turns["day"].dt.date.unique())
lo, hi = st.sidebar.select_slider(
    "Date range", options=days, value=(days[0], days[-1]),
    format_func=lambda d: d.strftime("%d %b"))
everyone = sorted(turns["who"].unique())
picked = st.sidebar.multiselect("People", everyone, default=everyone)

view = turns[(turns["day"].dt.date >= lo) & (turns["day"].dt.date <= hi)
             & (turns["who"].isin(picked))]

# The colour domain comes from the FULL dataset, not the filtered view, so
# deselecting someone never repaints the people who remain.
top = list(turns["who"].value_counts().head(theme.MAX_SERIES).index)
stable_domain = sorted(top) + ([theme.OTHER] if len(everyone) > len(top) else [])
view = view.assign(who=theme.fold_to_other(view["who"], top))

st.sidebar.caption(
    f"{len(view):,} turns · {view['who'].nunique()} people · "
    f"{len(dataio.denied_commands(view))} distinct blocked commands"
)

# --------------------------------------------------------------------------- #
# Headline
# --------------------------------------------------------------------------- #
st.title("Aspen usage")

ok = view[~view["failed"]]
cols = st.columns(6)
cols[0].metric("Turns", f"{len(view):,}")
cols[1].metric("People", f"{view['who'].nunique()}")
cols[2].metric("Median answer", f"{ok['latency_ms'].median() / 1000:,.0f}s"
               if ok["latency_ms"].notna().any() else "—")
cols[3].metric("p90 answer", f"{ok['latency_ms'].quantile(0.9) / 1000:,.0f}s"
               if ok["latency_ms"].notna().any() else "—")
cols[4].metric("Tokens", f"{view['tokens'].sum():,.0f}")
cols[5].metric("Failed turns", f"{view['failed'].mean():.0%}",
               help="Turns where the user did not get an answer: errors, "
                    "timeouts, rate limits, and refusals.")

st.caption(
    "Under a subscription seat there is no per-turn dollar cost — tokens and the "
    "rate-limit meter below are the spend signals. The meter is account-wide; "
    "per-user attribution comes from the token columns."
)

# --------------------------------------------------------------------------- #
# Usage over time + the quota meter
# --------------------------------------------------------------------------- #
st.subheader("Usage over time")
daily = view.groupby(["day", "who"], as_index=False).agg(
    turns=("outcome", "size"), tokens=("tokens", "sum"))

left, right = st.columns(2)
with left:
    st.altair_chart(
        theme.chrome(_bars_by_user(daily, "turns", "Turns per day", "turns",
                                   stable_domain), MODE),
        width="stretch")
with right:
    st.altair_chart(
        theme.chrome(_bars_by_user(daily, "tokens", "Tokens per day", "tokens",
                                   stable_domain), MODE),
        width="stretch")

meter = _utilization_line(view)
if meter is not None:
    st.altair_chart(theme.chrome(meter, MODE), width="stretch")
    st.caption(
        "Dashed line marks 80%. The CLI reports this only when the limit state "
        "changes, so days without a reading are days nothing moved — read it "
        "against the token chart above to see whose work drove a climb."
    )
else:
    st.caption(
        "No rate-limit readings in this range — the CLI emits the meter only "
        "when the limit state changes, so a quiet period reports nothing."
    )

# --------------------------------------------------------------------------- #
# Per person
# --------------------------------------------------------------------------- #
st.subheader("Per person")
st.dataframe(dataio.per_user(view), width="stretch")

# --------------------------------------------------------------------------- #
# What people actually ask for
# --------------------------------------------------------------------------- #
st.subheader("What Aspen is doing")
left, right = st.columns(2)
with left:
    tools = (view["tools"].explode().dropna().value_counts()
             .reset_index(name="times").rename(columns={"index": "tool"}))
    tools.columns = ["tool", "times"]
    if tools.empty:
        st.caption("No tool calls recorded in this range.")
    else:
        st.altair_chart(theme.chrome(
            _hbars(tools, "tool", "times", "Tool calls", "tool"), MODE),
            width="stretch")
with right:
    seqs = dataio.tool_sequences(view)
    st.markdown("**Repeated tool sequences**")
    if seqs.empty:
        st.caption("Nothing repeats yet — needs more turns.")
    else:
        st.dataframe(seqs, width="stretch", hide_index=True)
        st.caption("A sequence that keeps recurring is a task worth one "
                   "purpose-built tool instead of five hand-assembled steps.")

# --------------------------------------------------------------------------- #
# Where it goes wrong
# --------------------------------------------------------------------------- #
st.subheader("Where it goes wrong")
left, right = st.columns(2)
with left:
    outcomes = view["outcome"].value_counts().reset_index()
    outcomes.columns = ["outcome", "times"]
    st.altair_chart(theme.chrome(
        _hbars(outcomes, "outcome", "times", "Turn outcomes", "outcome"), MODE),
        width="stretch")
    maxed = (view["result_subtype"] == "error_max_turns").sum()
    if maxed:
        st.caption(f"{maxed} turn(s) hit the per-turn round limit "
                   "(AGENT_MAX_ROUNDS) — tasks Aspen can't finish in one turn.")
with right:
    st.markdown("**Commands the allowlist refused**")
    denied = dataio.denied_commands(view)
    if denied.empty:
        st.caption("Nothing was blocked in this range.")
    else:
        st.dataframe(denied, width="stretch", hide_index=True)
        st.caption("This is a feature backlog: what people wanted Aspen to run.")

# --------------------------------------------------------------------------- #
# Latency decomposition
# --------------------------------------------------------------------------- #
with st.expander("Where the time goes"):
    split = pd.DataFrame({
        "part": ["Aspen overhead", "model", "tools"],
        "median seconds": [
            (view["overhead_ms"].median() or 0) / 1000,
            (view["api_ms"].median() or 0) / 1000,
            (view["tool_ms"].median() or 0) / 1000,
        ],
    }).dropna()
    st.altair_chart(theme.chrome(
        _hbars(split, "part", "median seconds", "Median time per turn", "part"), MODE),
        width="stretch")
    st.caption(
        "Three different problems. Overhead is session waits and pre-warm misses "
        "(ours); model time responds to effort and prompt size; tool time is the "
        "cluster and the analysis sandbox."
    )

# --------------------------------------------------------------------------- #
# The questions themselves
# --------------------------------------------------------------------------- #
st.subheader("Questions")
asked = view[view["text"].notna()] if "text" in view else view.iloc[0:0]
redacted = len(view) - len(asked)
if asked.empty:
    st.caption(f"No question text in this range ({redacted} turn(s) recorded "
               "without it — content collection is off or the window closed).")
else:
    needle = st.text_input("Filter", placeholder="e.g. corvus, failed, spectra")
    shown = asked[asked["text"].str.contains(needle, case=False, na=False)] if needle else asked
    st.dataframe(
        shown[["ts", "who", "outcome", "text", "latency_ms", "tool_count"]]
        .rename(columns={"ts": "when", "who": "person", "latency_ms": "answer (ms)",
                         "tool_count": "tools"})
        .sort_values("when", ascending=False),
        width="stretch", hide_index=True)
    if redacted:
        st.caption(f"{redacted} turn(s) in this range were recorded without text.")

# --------------------------------------------------------------------------- #
# Conversations — the turn log says how it went, this says what was said
# --------------------------------------------------------------------------- #
st.subheader("Conversations")
if DEMO:
    st.caption("Not available in demo mode — there are no transcripts to read.")
else:
    sessions = _load_conversations(str(convo.DEFAULT_PROJECTS_DIR),
                                   convo.stamp(convo.DEFAULT_PROJECTS_DIR))
    listing = convo.index(sessions)
    if listing.empty:
        st.caption(
            f"No transcripts found under `{convo.DEFAULT_PROJECTS_DIR}`. The CLI "
            "writes one per Slack thread; set CLAUDE_PROJECTS_DIR if the bot runs "
            "as another user."
        )
    else:
        excluded = convo.excluded_users(log_dir.parent / "telemetry.json")
        people = sorted(listing["who"].unique())
        left, right = st.columns([1, 2])
        who = left.selectbox("Person", people)
        mine = listing[listing["who"] == who]
        pick = right.selectbox(
            "Conversation", list(mine["session_id"]),
            format_func=lambda sid: (
                f"{mine.loc[mine['session_id'] == sid, 'last'].iloc[0]:%d %b %H:%M}"
                f"  ·  {mine.loc[mine['session_id'] == sid, 'title'].iloc[0]}"),
        )
        session = next(s for s in sessions if s["session_id"] == pick)
        # Everything recorded before session_id was logged has no row to consult.
        # Those turns are shown, because collection defaults to on and "no record"
        # is not a prohibition — but the switch is here to read them strictly.
        strict = not st.toggle(
            "Show turns recorded before the session id was logged", value=True,
            help="These predate the join key, so their consent decision cannot be "
                 "looked up. Collection defaults to on, so they are shown; turn "
                 "this off to see only turns whose record can be verified.")
        # Joined against the WHOLE log, not the filtered view: the sidebar range
        # scopes the charts, and a turn falling outside it is not a turn whose
        # consent decision is unknown.
        convo_turns = convo.visible_turns(session, turns, excluded,
                                          show_unjoined=not strict)

        withheld = [t for t in convo_turns if not t["shown"]]
        opted_out = [t for t in withheld if not t["unjoined"]]
        if opted_out:
            st.caption(f"{len(opted_out)} turn(s) hidden — recorded without text, "
                       "or the person has opted out of content collection.")

        st.caption(f"`{pick}` · {len(session['turns'])} turns · "
                   f"{session['turns'][0]['ts'][:10]} → {session['turns'][-1]['ts'][:10]}")
        for turn in convo_turns:
            visible = turn["shown"]
            when = turn["ts"][11:19]
            if not visible:
                st.markdown(f"**{turn['who']}** · {when} — _withheld ({turn['why']})_")
                st.divider()
                continue
            st.markdown(f"**{turn['who']}** · {when}")
            st.info(turn["question"])
            if turn["tools"]:
                with st.expander(f"{len(turn['tools'])} tool call(s)"):
                    st.code("\n".join(turn["tools"]), language=None)
            st.markdown("\n\n".join(turn["reply"]) if turn["reply"]
                        else "_(no text reply — the turn errored or is still running)_")
            st.divider()

# --------------------------------------------------------------------------- #
# Jobs — lights up when the agent starts submitting
# --------------------------------------------------------------------------- #
st.subheader("Cluster jobs")
st.info(
    "Not yet available — Aspen's scheduler access is read-only today. When "
    "agent-submitted jobs land (spec.md §18.2), this panel joins the submission "
    "ledger against `sacct` to show jobs and CPU-hours per person. That is a "
    "separate budget from the model tokens above."
)
