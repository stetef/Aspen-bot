"""
Chart palette and Altair chrome.

The categorical order is fixed and validated (adjacent-pair CVD ΔE 9.1 light /
8.4 dark; normal-vision 19.6 / 19.3). Slots are assigned **by entity, not by
rank** — the caller builds a stable domain from every alias in the dataset, so
filtering the view never repaints the series that survive.

Three light-mode slots sit below 3:1 against the surface; the relief for that is
the per-user table rendered beside the stacked charts, which carries the same
numbers without relying on hue.
"""

CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
              "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark":  ["#3987e5", "#d95926", "#199e70", "#c98500",
              "#d55181", "#008300", "#9085e9", "#e66767"],
}

# Single-hue (blue) for one-measure charts; the sequential ramp's mid steps.
PRIMARY = {"light": "#2a78d6", "dark": "#3987e5"}

# Reserved: only ever for state, never for "series N", and always beside a label.
STATUS = {"good": "#0ca30c", "warning": "#fab219",
          "serious": "#ec835a", "critical": "#d03b3b"}

INK = {
    "light": {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
              "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"},
    "dark":  {"surface": "#1a1a19", "primary": "#ffffff", "secondary": "#c3c2b7",
              "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835"},
}

# Beyond this many series the palette stops being separable — the tail folds
# into one "Other" band rather than inventing a ninth hue.
MAX_SERIES = 8
OTHER = "Other"


def chrome(chart, mode: str):
    """Recede the grid and axes so the data is the only assertive thing."""
    ink = INK[mode]
    return (
        chart
        .configure_view(strokeWidth=0, fill=ink["surface"])
        .configure_axis(
            gridColor=ink["grid"], gridWidth=1, domainColor=ink["axis"],
            tickColor=ink["axis"], labelColor=ink["muted"],
            titleColor=ink["secondary"], labelFontSize=11, titleFontSize=11,
            titleFontWeight="normal",
        )
        .configure_legend(labelColor=ink["secondary"], titleColor=ink["muted"],
                          labelFontSize=11, titleFontSize=11, symbolType="square")
        .configure_title(color=ink["primary"], fontSize=13, fontWeight=600, anchor="start")
    )


def series_scale(alt, domain: list[str], mode: str):
    """A colour scale pinned to a stable domain, so filtering never repaints."""
    palette = CATEGORICAL[mode]
    return alt.Scale(domain=domain, range=palette[: len(domain)])


def fold_to_other(names, keep: list[str]):
    """Everything outside the kept set becomes one band, never a ninth hue."""
    return [n if n in keep else OTHER for n in names]
