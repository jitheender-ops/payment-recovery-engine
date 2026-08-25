"""
The dashboard's design system: tokens, CSS, chart template, and components.

One module so every page draws from the same palette rather than each view
inventing its own colours — which is how this dashboard ended up with a
flat-UI rainbow (#e74c3c/#f39c12/#3498db) on one page and np.random on another.

On the palette: the categorical hues are not chosen by eye. They are generated
at fixed OKLCH lightness and chroma and then run through the six-check
validator (lightness band, chroma floor, CVD separation, normal-vision floor,
contrast) against this exact ink surface. Changing one means re-running it —
`node scripts/validate_palette.js "<hexes>" --mode dark --surface "#12161C"`.

Ink and brass rather than the slate-and-blue every SaaS dashboard ships:
brass is the colour money wears here, clay is the colour it wears when it
leaks, and the two never swap roles anywhere in the product.

Design language (2026 fintech console):
  Legibility IS the luxury. Layered near-black surfaces with hairline borders
  and an inner top highlight give depth without blur; motion is restricted to
  2-frame lifts on hover/press and collapses entirely under reduced-motion;
  status is always a pill, never a bare word. Colour informs, never alarms.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

IST = ZoneInfo("Asia/Kolkata")

# ── Surfaces and ink ─────────────────────────────────────────────────────
INK = "#12161C"        # page ground
SURFACE = "#1A1F27"    # raised panels, chart plotting area
SURFACE_2 = "#151A21"  # recessed wells inside raised panels
LINE = "#2A313C"       # hairlines and borders — never text
PAPER = "#ECEFF4"      # primary text            15.7:1 on ink
SLATE = "#8A94A6"      # secondary text           5.9:1 on ink
MUTE = "#6B7480"       # rules and borders ONLY — 3.8:1 fails AA for body text

# ── Money, and what it is doing ──────────────────────────────────────────
# Reserved roles. These are semantic, not slots in a rotation: brass always
# means recovered, clay always means at risk. A reader who learns that on the
# overview must not have to relearn it on another page.
BRASS = "#A58108"      # recovered — validated as a chart mark
BRASS_TEXT = "#D9A93A" # the same role in type, where contrast rules differ
CLAY = "#C16139"       # at risk / leaked
CLAY_TEXT = "#E0805F"

# ── State colours (chips and dots) ───────────────────────────────────────
# Indigo says "waiting on the clock" — scheduled, deferred, in flight. Teal
# says "alive and connected". Neither ever means money; money stays brass/clay.
INDIGO_TEXT = "#9BA3E0"
TEAL = "#009592"
TEAL_TEXT = "#3DBDB4"

# ── Categorical series ───────────────────────────────────────────────────
# Fixed order, never cycled. A ninth series folds into "Other" rather than
# getting a generated hue.
SERIES = [BRASS, TEAL, CLAY, "#757DD0", "#5F9752"]

# Magnitude, not identity: one hue, monotone lightness. The dark end is floored
# at 2.5:1 against the ink — the previous ramp started at the hairline colour,
# so the lowest heatmap cells were indistinguishable from empty background and
# "no recoveries" looked identical to "no data".
SEQUENTIAL = ["#6B551B", "#876B1B", "#A58108", "#C39810", "#DFB127"]

FONT_DISPLAY = "'Bricolage Grotesque', 'IBM Plex Sans', sans-serif"
FONT_BODY = "'IBM Plex Sans', system-ui, sans-serif"
FONT_MONO = "'IBM Plex Mono', ui-monospace, monospace"


# ── Money formatting ─────────────────────────────────────────────────────


def inr(paise: float, *, decimals: bool = False) -> str:
    """
    Rupees from paise, grouped the way the audience reads them.

    Indian digit grouping is 12,34,567 — the last three digits, then twos —
    not the Western 1,234,567. Everyone reading this dashboard prices in lakh
    and crore, and a number grouped the other way has to be counted digit by
    digit before it means anything.
    """
    rupees = paise / 100
    neg = rupees < 0
    whole = abs(int(rupees))
    frac = f"{abs(rupees) - whole:.2f}"[1:] if decimals else ""

    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{'-' if neg else ''}₹{s}{frac}"


def compact_inr(paise: float) -> str:
    """₹12.3L / ₹4.6Cr — the units an Indian payments team actually speaks in."""
    rupees = abs(paise) / 100
    sign = "-" if paise < 0 else ""
    if rupees >= 1_00_00_000:
        return f"{sign}₹{rupees / 1_00_00_000:.2f}Cr"
    if rupees >= 1_00_000:
        return f"{sign}₹{rupees / 1_00_000:.2f}L"
    if rupees >= 1_000:
        return f"{sign}₹{rupees / 1_000:.1f}K"
    return f"{sign}₹{rupees:,.0f}"


def fmt_ist(ts: Any) -> str:
    """
    A DB timestamp as the wall clock the team runs on.

    Every timestamp in Postgres is UTC; this audience prices things in IST and
    the guardrail's own blackout window is an IST window, so a naive "2026-08-25
    05:10:44+00:00" is a small lie someone has to undo in their head. Naive
    input (SQLite, test harnesses) is treated as UTC, matching the service's
    own convention. Unparseable input passes through untouched — better an
    ugly string than a fabricated time.
    """
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return "—"
    try:
        parsed: datetime = pd.Timestamp(ts).to_pydatetime()
    except Exception:
        return str(ts)
    if parsed.tzinfo is None:
        from datetime import UTC

        parsed = parsed.replace(tzinfo=UTC)
    local: datetime = parsed.astimezone(IST)
    return local.strftime("%d %b %H:%M IST")


# ── Status semantics ─────────────────────────────────────────────────────
# One mapping for every lifecycle word in the product. A status that renders
# green here and amber there is a status nobody trusts.

_TONES: dict[str, tuple[str, str]] = {
    # word prefix → (text colour, border/background tint)
    "brass": (BRASS_TEXT, "rgba(165,129,8,0.16)"),
    "clay": (CLAY_TEXT, "rgba(193,97,57,0.16)"),
    "indigo": (INDIGO_TEXT, "rgba(117,125,208,0.16)"),
    "teal": (TEAL_TEXT, "rgba(0,149,146,0.16)"),
    "slate": (SLATE, "rgba(138,148,166,0.12)"),
    "mute": (MUTE, "rgba(107,116,128,0.12)"),
}

STATUS_TONE: dict[str, str] = {
    # attempt results
    "success": "brass",
    "failed": "clay",
    "pending": "indigo",
    "scheduled": "indigo",
    "rejected": "clay",
    "cancelled": "mute",
    "skipped": "mute",
    "superseded": "mute",
    # case states
    "open": "indigo",
    "recovered": "brass",
    "exhausted": "slate",
    "abandoned": "slate",
    "expired": "mute",
    "opted_out": "mute",
    # promises
    "kept": "brass",
    "broken": "clay",
    # agents
    "llm": "teal",
    "xgboost": "slate",
    "deterministic": "slate",
    # actions
    "retry_now": "indigo",
    "retry_at": "indigo",
    "switch_rail": "teal",
    "nudge_customer": "brass",
    "abandon": "slate",
}


def chip(text: str, *, tone: str = "slate") -> str:
    """One status pill. Tones come from _TONES; unknown tones fall back."""
    fg, bg = _TONES.get(tone, _TONES["slate"])
    return (
        f"<span style='display:inline-flex;align-items:center;gap:0.32rem;"
        f"padding:0.14rem 0.55rem;border-radius:999px;font-family:{FONT_BODY};"
        f"font-size:0.72rem;font-weight:500;letter-spacing:0.02em;"
        f"color:{fg};background:{bg};"
        f"border:1px solid {fg}33;'>{text}</span>"
    )


# ── Icons ────────────────────────────────────────────────────────────────
# Inline SVG, Phosphor-outline style: stroke follows currentColor so an icon
# inherits its container's tone, and no emoji ever stands in for a glyph.
# Paths are deliberately simple geometry — they must read at 16px.

_ICONS: dict[str, str] = {
    "money":        "<rect x='2.5' y='6' width='19' height='12' rx='2'/><circle cx='12' cy='12' r='2.6'/><path d='M6 9.5v0M18 14.5v0'/>",  # noqa: E501
    "trend-up":     "<path d='M3 17l6-6 4 4 8-8'/><path d='M15 7h6v6'/>",  # noqa: E501
    "pending":      "<path d='M12 3a9 9 0 1 0 9 9'/><path d='M12 7v5l3 3'/>",  # noqa: E501
    "clock":        "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3.5 2.5'/>",  # noqa: E501
    "warning":      "<path d='M12 3L2.5 20h19L12 3z'/><path d='M12 10v4'/><circle cx='12' cy='17' r='0.4'/>",  # noqa: E501
    "envelope":     "<rect x='3' y='5.5' width='18' height='13' rx='2'/><path d='M3.5 7l8.5 6 8.5-6'/>",  # noqa: E501
    "hourglass":    "<path d='M7 3h10M7 21h10M8 3c0 7 8 7 8 11s-8 4-8 7M16 3c0 7-8 7-8 11s8 4 8 7'/>",  # noqa: E501
    "check":        "<path d='M4.5 12.5l5 5L20 6.5'/>",  # noqa: E501
    "cross":        "<path d='M6 6l12 12M18 6L6 18'/>",  # noqa: E501
    "shield":       "<path d='M12 3l8 3v6c0 4.5-3.4 7.9-8 9-4.6-1.1-8-4.5-8-9V6l8-3z'/>",  # noqa: E501
    "open-case":    "<circle cx='12' cy='12' r='9'/><path d='M12 7v5h4'/>",  # noqa: E501
    "exhausted":    "<circle cx='12' cy='12' r='9'/><path d='M8.5 12h7'/>",  # noqa: E501
    "abandoned":    "<circle cx='12' cy='12' r='9'/><path d='M6 6l12 12'/>",  # noqa: E501
    "opted-out":    "<circle cx='12' cy='12' r='9'/><path d='M8 10.5h8M8 13.5h5'/>",  # noqa: E501
    "expired":      "<circle cx='12' cy='12' r='9'/><path d='M9 12h6'/>",  # noqa: E501
    "disconnected": "<path d='M3 3l18 18'/><path d='M8.5 8.5A11 11 0 0 0 5 12M12 4.5c3.5 0 6.7 1.3 9 3.5M11 8.3a8 8 0 0 1 7 2.2'/><circle cx='12' cy='17' r='0.6'/>",  # noqa: E501
}


def icon(name: str, *, size: int = 18) -> str:
    """
    One inline SVG icon by name, stroked in currentColor.

    Unknown names fail LOUD (KeyError): a silently missing icon renders as an
    invisible gap that nobody investigates.
    """
    body = _ICONS[name]
    return (
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none'"
        f" stroke='currentColor' stroke-width='1.8' stroke-linecap='round'"
        f" stroke-linejoin='round' aria-hidden='true'>{body}</svg>"
    )

# Alias: tile()/empty_state() take an `icon=` NAME argument that shadows this
# function inside their bodies; they render through this alias instead.
_icon = icon


def status_chip(status: Any) -> str:
    """A lifecycle word as its reserved pill. Unknown words stay quiet-slate."""
    word = str(status) if status is not None else "—"
    return chip(word.replace("_", " "), tone=STATUS_TONE.get(word, "slate"))


# ── Page furniture ───────────────────────────────────────────────────────


def page_header(eyebrow: str, title: str, note: str | None = None) -> None:
    """Every view opens the same way: eyebrow, headline, optional one-liner."""
    st.markdown(
        f"<div style='font-family:{FONT_MONO};color:{SLATE};font-size:0.74rem;"
        f"letter-spacing:0.12em;margin-bottom:0.2rem;'>{eyebrow}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<h1 style='margin-bottom:{'0.15rem' if note else '0.4rem'};'>{title}</h1>",
        unsafe_allow_html=True,
    )
    if note:
        st.markdown(
            f"<p style='color:{SLATE};font-size:0.88rem;"
            f"margin:0 0 0.4rem 0;'>{note}</p>",
            unsafe_allow_html=True,
        )


def ring(
    pct: float,
    *,
    label: str = "",
    size: int = 74,
    stroke: int = 7,
    colour: str = BRASS_TEXT,
    track: str = LINE,
) -> str:
    """
    A circular progress ring — SVG, no library.

    The rounded cap and the -90° start (12 o'clock) are the two conventions
    every fitness/payment ring shares; the number sits dead centre in mono so
    the eye lands on the value, not on the geometry. pct beyond [0,100] is
    clamped: a progress ring that lies is worse than none.
    """
    p = min(max(pct, 0.0), 100.0)
    r = (size - stroke) / 2
    c = 2 * 3.14159265 * r
    filled = c * p / 100
    return f"""
<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" role="img"
     aria-label="{label}: {p:.1f} percent">
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none"
          stroke="{track}" stroke-width="{stroke}"/>
  <circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none"
          stroke="{colour}" stroke-width="{stroke}"
          stroke-linecap="round"
          stroke-dasharray="{filled:.2f} {c:.2f}"
          transform="rotate(-90 {size/2} {size/2})"/>
  <text x="50%" y="54%" text-anchor="middle" dominant-baseline="middle"
        font-family="{FONT_MONO}" font-size="{size*0.21:.0f}" font-weight="500"
        fill="{PAPER}">{p:.0f}%</text>
</svg>"""


def empty_state(
    title: str,
    body: str,
    *,
    icon: str = "pending",
    action_label: str = "",
    action_code: str = "",
) -> None:
    """
    A zero-data moment that still has a job to do.

    2026 ops-console guidance, applied: headline first, one sentence of
    context, ONE primary action, and no whimsical illustration — this is a
    payments console, not a consumer app. The card keeps the region's real
    proportions (no giant hero panel), so the move from empty to populated
    reads as continuous rather than as a page swap. `icon` is a theme.icon()
    name.
    """
    st.markdown(
        f"""
        <div style="border:1px dashed {LINE}; border-radius:12px;
                    background:rgba(255,255,255,0.012);
                    padding:2rem 2rem 1.7rem 2rem; text-align:center;
                    margin:0.4rem 0 1rem 0;">
          <div style="width:44px;height:44px;border-radius:12px;margin:0 auto 0.8rem auto;
                      background:{SURFACE};border:1px solid {LINE};
                      display:flex;align-items:center;justify-content:center;
                      color:{SLATE};">{_icon(icon, size=22)}</div>
          <div style="font-family:{FONT_DISPLAY};font-weight:600;font-size:1.02rem;
                      color:{PAPER};">{title}</div>
          <div style="color:{SLATE};font-size:0.84rem;margin-top:0.3rem;
                      max-width:52ch;margin-left:auto;margin-right:auto;">
            {body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if action_code:
        st.code(action_code, language="bash")


def tile(
    label: str,
    value: str,
    *,
    icon: str = "",
    foot: str = "",
    tone: str = "paper",
    help: str = "",
) -> None:
    """
    One elevated KPI card — the design system's primary atom.

    Depth comes from frosted glass over the ambient ground, an inner top
    highlight and a tinted drop shadow (never pure black), not from heavy
    blur. The whole card lifts two pixels on hover; under reduced-motion the
    transition collapses. `icon` is a theme.icon() NAME (svg), never an emoji.
    tone: 'paper' (default), 'brass' (money in), 'clay' (needs eyes).
    """
    value_token = {
        "paper": "--rc-paper",
        "brass": "--rc-brass-text",
        "clay": "--rc-clay-text",
    }.get(tone, "--rc-paper")
    ico_html = (
        f"<span style='width:26px;height:26px;border-radius:8px;display:inline-flex;"
        f"align-items:center;justify-content:center;background:rgba(165,129,8,0.10);"
        f"border:1px solid rgba(165,129,8,0.25);color:{BRASS_TEXT};'>"
        f"{_icon(icon, size=15)}</span>"
        if icon
        else ""
    )
    foot_html = (
        f"<div style='color:{MUTE};font-size:0.72rem;margin-top:0.28rem;"
        f"line-height:1.35;' title='{help}'>{foot}</div>"
        if foot
        else ""
    )
    st.markdown(
        f"""
        <div class="rc-tile" title="{help}">
          <div style="display:flex;justify-content:space-between;align-items:center;
                      margin-bottom:0.45rem;">
            <span style="color:{SLATE};text-transform:uppercase;letter-spacing:0.07em;
                         font-size:0.68rem;font-weight:500;">{label}</span>
            {ico_html}
          </div>
          <div style="font-family:{FONT_MONO};font-weight:500;font-size:1.72rem;
                      color:var({{{value_token}}});font-variant-numeric:tabular-nums;
                      line-height:1.1;">{value}</div>
          {foot_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── Chart template ───────────────────────────────────────────────────────


_template_registered = False


def register_plotly_template() -> None:
    """One template every chart inherits: recessive grid, no chart junk."""
    global _template_registered
    if _template_registered:
        return
    pio.templates["recovery"] = go.layout.Template(
        layout=go.Layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"family": "IBM Plex Sans, system-ui, sans-serif", "color": SLATE, "size": 13},
            title={"font": {"family": "Bricolage Grotesque, sans-serif",
                            "color": PAPER, "size": 16}, "x": 0, "xanchor": "left"},
            colorway=SERIES,
            barcornerradius=5,
            # Recessive by design: the grid is a reading aid, not a subject.
            xaxis={"gridcolor": LINE, "zerolinecolor": LINE, "linecolor": LINE,
                   "tickfont": {"color": SLATE, "size": 12}, "showgrid": False,
                   "automargin": True},
            # automargin, not a hand-picked left margin: every horizontal bar
            # chart here has category names on the y-axis, and an 8px margin
            # clipped them to a single character.
            yaxis={"gridcolor": LINE, "zerolinecolor": LINE, "linecolor": "rgba(0,0,0,0)",
                   "tickfont": {"color": SLATE, "size": 12}, "automargin": True},
            legend={"bgcolor": "rgba(0,0,0,0)", "font": {"color": SLATE, "size": 12},
                    "orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0},
            margin={"l": 8, "r": 56, "t": 48, "b": 8},  # r leaves room for outside labels
            hoverlabel={"bgcolor": SURFACE, "bordercolor": LINE,
                        "font": {"color": PAPER, "family": "IBM Plex Sans, sans-serif",
                                 "size": 13}},
            separators=".,",
        )
    )
    pio.templates.default = "recovery"
    _template_registered = True


def soft_fill(fig: Any, colour: str = BRASS, opacity: float = 0.14) -> Any:
    """Fill under a line trace with a fade to transparent — depth, not noise."""
    fig.update_traces(
        fill="tozeroy",
        fillcolor=f"rgba({hex_to_rgb(colour)},{opacity})",
        line={"shape": "spline", "smoothing": 0.75},
    )
    return fig


def hex_to_rgb(hex_colour: str) -> str:
    """'#A58108' -> '165,129,8'. Raises on malformed input — fail loud."""
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"not a hex colour: {hex_colour!r}")
    return ",".join(str(int(h[i : i + 2], 16)) for i in (0, 2, 4))


def _srgb_channel(c: float) -> float:
    """One sRGB channel [0,1] through the WCAG transfer function."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    """
    WCAG relative luminance of a hex colour, 0 (black) to 1 (white).

    The number that decides whether cell text is ink or paper — a threshold
    guess reads wrong on exactly the mid-ramp cells people squint at.
    """
    r, g, b = (int(x, 16) / 255 for x in [hex_to_rgb_part(hex_colour, i) for i in (0, 2, 4)])
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def hex_to_rgb_part(hex_colour: str, start: int) -> str:
    """The two hex digits of one channel."""
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"not a hex colour: {hex_colour!r}")
    return h[start : start + 2]


def ramp_color(t: float) -> str:
    """Interpolate SEQUENTIAL at t∈[0,1] — the exact fill a heat cell gets."""
    stops = SEQUENTIAL
    x = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(x), len(stops) - 2)
    frac = x - i
    c1, c2 = (stops[i].lstrip("#"), stops[i + 1].lstrip("#"))
    mixed = "".join(
        f"{round(int(c1[k:k+2], 16) * (1 - frac) + int(c2[k:k+2], 16) * frac):02x}"
        for k in (0, 2, 4)
    )
    return f"#{mixed}"


def readable_on(fill_hex: str) -> str:
    """Ink or paper — whichever clears WCAG against this fill."""
    return PAPER if luminance(fill_hex) < 0.42 else INK


# One config for every st.plotly_chart call: no plotly logo, modebar appears
# on hover only (restyled dark in CSS), no scroll-zoom hijacking the page
# wheel inside a scrolling console.
PLOTLY_CONFIG = {
    "displaylogo": False,
    "scrollZoom": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}


# ── CSS ──────────────────────────────────────────────────────────────────
# Only what config.toml cannot express. Selectors are data-testid attributes
# rather than generated class names, because the generated ones change between
# Streamlit releases and take the whole design with them when they do.

_CSS = f"""
<style>
:root {{
  /* Semantic design tokens — components reference var(--rc-*), never
     raw hex, so re-theming is a token edit, not a markup hunt. */
  --rc-ink: #12161C;
  --rc-surface: #1A1F27;
  --rc-line: #2A313C;
  --rc-paper: #ECEFF4;
  --rc-slate: #8A94A6;
  --rc-brass-text: #D9A93A;
  --rc-clay-text: #E0805F;
}}

@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {{ font-family: {FONT_BODY}; }}

/* ── Ambient layer ───────────────────────────────────────────────────── */
/* Two fixed radial washes for depth, plus a hairline engineering grid that
   drifts one cell every 90s — perceptible as 'alive', never as motion that
   competes with data. Paints between the app ground and all content
   (z-index:-1), pointer-transparent, and collapses under reduced-motion. */
.stApp {{
    background:
      radial-gradient(1100px 520px at 78% -12%, rgba(165,129,8,0.055), transparent 60%),
      radial-gradient(900px 480px at -8% 108%, rgba(117,125,208,0.045), transparent 55%),
      {INK};
}}
.stApp::before {{
    content: "";
    position: fixed; inset: -56px;
    z-index: -1; pointer-events: none;
    background-image:
      linear-gradient(rgba(138,148,166,0.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(138,148,166,0.045) 1px, transparent 1px);
    background-size: 56px 56px;
    animation: rc-grid-drift 90s linear infinite;
}}
@keyframes rc-grid-drift {{
    to {{ transform: translate(56px, 56px); }}
}}

/* Every full page render rises once — the closest a rerun-model framework
   gets to a view transition, and enough that switching sections feels like
   movement instead of a flash. */
.block-container {{ padding-top: 2.6rem; max-width: 1340px;
                   padding-left: 2.6rem; padding-right: 2.6rem;
                   animation: rc-rise 0.32s cubic-bezier(0.16,1,0.3,1); }}
@keyframes rc-rise {{
    from {{ opacity: 0; transform: translateY(9px); }}
    to   {{ opacity: 1; transform: none; }}
}}

h1, h2, h3 {{ font-family: {FONT_DISPLAY}; letter-spacing: -0.02em; color: {PAPER}; }}
h1 {{ font-weight: 800; font-size: 2.15rem; }}
h2 {{ font-weight: 600; font-size: 1.35rem; margin-top: 0.4rem; }}
h3 {{ font-weight: 600; color: {SLATE};
      text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.78rem; }}

hr {{ border-color: {LINE}; }}

/* ── Tiles: frosted glass over the ambient ground ────────────────────── */
/* True glassmorphism, with a discipline the trend usually lacks: blur is
   capped at 14px so text on the panel stays crisp, the fill stays ≥0.66
   opaque so contrast math survives whatever drifts behind it, and a solid
   fallback serves browsers without backdrop-filter. */
.rc-tile {{
  position: relative;
  background: linear-gradient(180deg,
              rgba(30,37,48,0.78) 0%, rgba(22,27,34,0.72) 100%);
  -webkit-backdrop-filter: blur(14px) saturate(150%);
  backdrop-filter: blur(14px) saturate(150%);
  border: 1px solid rgba(236,239,244,0.09);
  border-radius: 12px;
  padding: 1.05rem 1.15rem 0.95rem 1.15rem;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.09), 0 10px 28px rgba(6,9,14,0.38);
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
}}
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {{
  .rc-tile {{ background: linear-gradient(180deg, #1B212B 0%, #161B22 100%); }}
}}
.rc-tile:hover {{
  transform: translateY(-2px);
  border-color: rgba(217,169,58,0.40);
  /* The hover glow: soft brass bleed, not neon — money keeps its colour. */
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.10),
              0 0 0 1px rgba(217,169,58,0.18),
              0 6px 26px rgba(165,129,8,0.22),
              0 14px 34px rgba(6,9,14,0.46);
}}

[data-testid="stMetric"] {{
  background: linear-gradient(180deg,
              rgba(30,37,48,0.78) 0%, rgba(22,27,34,0.72) 100%);
  border: 1px solid rgba(236,239,244,0.09);
  border-radius: 12px; padding: 1rem 1.1rem;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.09), 0 10px 28px rgba(6,9,14,0.38);
}}
[data-testid="stMetricValue"] {{
    font-family: {FONT_MONO}; font-weight: 500; font-size: 1.75rem;
    color: {PAPER}; font-variant-numeric: tabular-nums;
}}
[data-testid="stMetricLabel"] {{
    color: {SLATE}; text-transform: uppercase;
    letter-spacing: 0.07em; font-size: 0.72rem; font-weight: 500;
}}

/* ── Sidebar: a control surface, not a menu dump ─────────────────────── */
[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, #181D26 0%, {SURFACE_2} 100%);
  border-right: 1px solid {LINE};
}}
[data-testid="stSidebar"] .block-container {{ padding-top: 1.6rem; }}

/* Nav radio rendered as stacked cards with a brass rail on the active one. */
/* ── Touch targets: 44px is the floor a thumb can trust ──────────────── */
[data-testid="stSidebar"] div[role="radiogroup"] label {{
  display: flex; flex-direction: column; gap: 0.05rem;
  min-height: 44px; justify-content: center;
  background: rgba(255,255,255,0.015);
  border: 1px solid transparent;
  border-radius: 10px;
  padding: 0.52rem 0.8rem !important;
  margin: 0.14rem 0 !important;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}}
[data-testid="stSidebar"] div[role="radiogroup"] label:hover {{
  background: rgba(255,255,255,0.045);
  border-color: {LINE};
}}
[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {{
  background: rgba(165,129,8,0.09);
  border-color: rgba(217,169,58,0.38);
  box-shadow: inset 2px 0 0 {BRASS_TEXT},
              /* Active nav carries the glow language: a soft brass bleed
                 that fades in with the border — state you can feel. */
              0 0 14px rgba(165,129,8,0.18);
}}
/* Hide the native radio dot; the rail carries the state. */
[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {{
  display: none;
}}
[data-testid="stSidebar"] div[role="radiogroup"] label div {{
  color: {PAPER}; font-size: 0.9rem; font-weight: 500; line-height: 1.25;
}}
[data-testid="stSidebar"] div[role="radiogroup"] label small {{
  color: {SLATE} !important; font-size: 0.7rem; font-weight: 400;
}}

/* Keyboard nav on custom radios: the native input is visually clipped,
   so its focus ring moves onto the label card itself. */
[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:focus-visible) {{
  outline: 2px solid {BRASS_TEXT};
  outline-offset: 2px;
}}

/* ── Buttons: brass on press, never neon ─────────────────────────────── */
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-primary"] {{
  border-radius: 10px;
  min-height: 40px;
  cursor: pointer;
  transition: transform 0.12s ease, box-shadow 0.12s ease, border-color 0.12s ease;
}}
[data-testid="stBaseButton-primary"]:not([disabled]) {{
  background: linear-gradient(180deg, {BRASS_TEXT} 0%, #C2952F 100%);
  color: #14100A; border: 1px solid #E0BC63; font-weight: 600;
}}
[data-testid="stBaseButton-primary"]:hover:not([disabled]) {{
  box-shadow: 0 6px 20px rgba(165,129,8,0.30);
}}
/* Press feedback: border expands a frame and the glow fades outward —
   instant acknowledgement, gone before it distracts. */
[data-testid="stBaseButton-secondary"]:hover:not([disabled]) {{
  border-color: rgba(217,169,58,0.45);
  box-shadow: 0 0 12px rgba(165,129,8,0.16);
}}
[data-testid="stBaseButton-primary"]:active:not([disabled]),
[data-testid="stBaseButton-secondary"]:active {{
  /* whileTap equivalent: shrink + drop, returning faster than hover entered */
  transform: translateY(1px) scale(0.98);
}}

/* Sidebar nav items cascade in on load — same stagger primitive as tiles. */
[data-testid="stSidebar"] div[role="radiogroup"] label {{
  animation: rc-rise 0.26s cubic-bezier(0.16, 1, 0.3, 1) both;
}}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(1) {{ animation-delay: 40ms }}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(2) {{ animation-delay: 80ms }}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(3) {{ animation-delay: 120ms }}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(4) {{ animation-delay: 160ms }}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(5) {{ animation-delay: 200ms }}
[data-testid="stSidebar"] div[role="radiogroup"] label:nth-child(n+6) {{ animation-delay: 240ms }}

/* ── Tables, expanders, alerts ───────────────────────────────────────── */
[data-testid="stDataFrame"] {{
  border: 1px solid {LINE}; border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 10px 28px rgba(6,9,14,0.30);
}}
[data-testid="stExpander"] {{
  border: 1px solid {LINE} !important; border-radius: 12px !important;
  background: rgba(255,255,255,0.014);
}}
[data-testid="stExpander"] summary:hover {{
  color: {PAPER};
}}
[data-testid="stAlert"] {{
  border: 1px solid {LINE}; border-radius: 12px;
  background-color: {SURFACE};
}}

/* ── Scrollbars: part of the dark, not an afterthought ───────────────── */
::-webkit-scrollbar {{ width: 9px; height: 9px; }}
::-webkit-scrollbar-thumb {{ background: #2C333E; border-radius: 8px;
                             border: 2px solid {INK}; }}
::-webkit-scrollbar-thumb:hover {{ background: #3A424F; }}
::-webkit-scrollbar-track {{ background: transparent; }}

/* ── Plotly chrome: the modebar must belong to this surface ──────────── */
/* Default is a white pill that pops over every chart on hover and reads as
   a second app pasted on top. Transparent ground, hairline buttons, icons
   in slate brightening to paper on hover. */
.modebar {{
  background: rgba(18,22,28,0.85) !important;
  border: 1px solid {LINE};
  border-radius: 8px;
  left: auto !important;
}}
.modebar-btn {{ opacity: 0.75; }}
.modebar-btn:hover {{ opacity: 1; }}
.modebar-btn path {{
  fill: {SLATE} !important;
  transition: fill 0.12s ease;
}}
.modebar-btn:hover path {{ fill: {PAPER} !important; }}
.modebar-group {{ background: transparent !important; }}

/* ── Widgets Streamlit ships unthemed ────────────────────────────────── */
[data-testid="stToggle"] span[aria-checked], [data-testid="stToggle"] {{
  --primary-color: {BRASS_TEXT};
}}
[data-testid="stCodeBlock"] code, [data-testid="stCode"] pre {{
  background: {SURFACE_2} !important;
  border: 1px solid {LINE};
  border-radius: 10px;
}}
[data-testid="stDownloadButton"] button {{
  border-radius: 10px;
}}

::selection {{ background: rgba(217,169,58,0.32); color: {PAPER}; }}

/* The live dot breathes — the one place perpetual motion is earned, because
   'connected and streaming' is exactly what it communicates. */
@keyframes rc-pulse {{
  0%   {{ box-shadow: 0 0 0 0 rgba(61,189,180,0.45); }}
  70%  {{ box-shadow: 0 0 0 7px rgba(61,189,180,0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(61,189,180,0); }}
}}
.rc-pulse {{ animation: rc-pulse 2.4s ease-out infinite; }}

/* ── Motion system ───────────────────────────────────────────────────── */
/* Motion-library principles, expressed in CSS because Streamlit has no React
   runtime: transforms only (never width/left), enter slower than exit,
   spring-flavoured overshoot on hover, staggered children via delay steps,
   and everything collapses under prefers-reduced-motion below. */

.rc-tile {{
  /* Enter: rise + fade, 'both' fill so delayed children start hidden —
     this is the stagger primitive; delays come from the column rules. */
  animation: rc-rise 0.30s cubic-bezier(0.16, 1, 0.3, 1) both;
}}
/* KPI rows are Streamlit columns; each child enters a beat after the last.
   Cap at four — deeper rows just arrive together, which is fine. */
[data-testid="stColumn"]:nth-child(1) .rc-tile {{ animation-delay: 40ms; }}
[data-testid="stColumn"]:nth-child(2) .rc-tile {{ animation-delay: 100ms; }}
[data-testid="stColumn"]:nth-child(3) .rc-tile {{ animation-delay: 160ms; }}
[data-testid="stColumn"]:nth-child(4) .rc-tile {{ animation-delay: 220ms; }}

/* Hover: spring-flavoured overshoot curve — past the target, settle back.
   Press: faster than hover returns (exit-faster-than-enter). */
.rc-tile {{
  transition: transform 0.18s cubic-bezier(0.34, 1.4, 0.5, 1),
              border-color 0.16s ease, box-shadow 0.16s ease;
}}
.rc-tile:hover {{ transform: translateY(-2px); }}
.rc-tile:hover [style*="border-radius:8px"] {{
  /* The little icon chip answers its card's hover — nested-variant feel. */
  transform: scale(1.1);
  transition: transform 0.18s cubic-bezier(0.34, 1.4, 0.5, 1);
}}

/* Decision-feed rows cascade in, capped so a long feed doesn't parade. */
.rc-row {{ animation: rc-rise 0.26s cubic-bezier(0.16, 1, 0.3, 1) both; }}
.rc-row:nth-child(1) {{ animation-delay: 30ms }}
.rc-row:nth-child(2) {{ animation-delay: 70ms }}
.rc-row:nth-child(3) {{ animation-delay: 110ms }}
.rc-row:nth-child(4) {{ animation-delay: 150ms }}
.rc-row:nth-child(5) {{ animation-delay: 190ms }}
.rc-row:nth-child(n+6) {{ animation-delay: 230ms }}

/* Focus must stay visible — this is a console people drive from the keyboard. */
*:focus-visible {{ outline: 2px solid {BRASS_TEXT} !important; outline-offset: 2px; }}

/* ── Responsive: 375 / 768 / 1024 ───────────────────────────────────── */
@media (max-width: 1024px) {{
  .block-container {{ padding-left: 1.6rem; padding-right: 1.6rem; }}
}}
@media (max-width: 768px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem;
                      padding-top: 1.6rem; }}
  h1 {{ font-size: 1.7rem; }}
  .rc-tile {{ animation-delay: 0ms !important; }}
  .stApp::before {{ animation: none; }}   /* ambience rests on small screens */
}}
@media (max-width: 480px) {{
  h1 {{ font-size: 1.5rem; }}
  .block-container {{ max-width: 100%; }}
}}

@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0.001ms !important;
                              transition-duration: 0.001ms !important; }}
}}
</style>
"""


def apply() -> None:
    """Inject the CSS. Chart template is registered lazily by style_fig()."""
    st.markdown(_CSS, unsafe_allow_html=True)


# ── The signature: the recovery ledger band ──────────────────────────────


def ledger_band(at_risk_paise: int, recovered_paise: int, attributed_paise: int) -> str:
    """
    One band answering the only question this product exists to answer.

    The full width is money at risk. The filled portion is money that came
    back. Inside it, the brass portion is what a link WE sent earned; the
    hollow portion is the customer paying on their own. That distinction is the
    engine's central honesty claim — a headline that cannot separate them is
    taking credit for the control group — so it is the thing the page is built
    around rather than a footnote under a stat tile.

    Hand-written SVG, not a chart library: it is one bar with three stops and a
    hatch, and it has to sit exactly on the type grid.
    """
    # 2px at the band's rendered width, expressed in its 0-100 viewBox units.
    gap = 0.22
    at_risk = max(at_risk_paise, 1)
    rec_pct = min(100.0, recovered_paise / at_risk * 100)
    att_pct = min(rec_pct, attributed_paise / at_risk * 100)
    self_pct = max(0.0, rec_pct - att_pct)

    return f"""
<div style="margin: 0.2rem 0 1.6rem 0;">
  <div style="display:flex; justify-content:space-between; align-items:baseline;
              font-family:{FONT_BODY}; margin-bottom:0.55rem;">
    <span style="color:{SLATE}; text-transform:uppercase; letter-spacing:0.07em;
                 font-size:0.72rem; font-weight:500;">Recovery ledger</span>
    <span style="color:{SLATE}; font-size:0.78rem; font-family:{FONT_MONO};">
      {compact_inr(at_risk_paise)} at risk
    </span>
  </div>

  <svg viewBox="0 0 100 6" preserveAspectRatio="none" width="100%" height="30"
       role="img" aria-label="Of {compact_inr(at_risk_paise)} at risk,
       {compact_inr(recovered_paise)} recovered, of which
       {compact_inr(attributed_paise)} is attributed to this engine.">
    <defs>
      <!-- Texture, so the two recovered kinds are separable without colour:
           the CVD case, print, and forced-colors all lose the hue. -->
      <pattern id="selfpay" width="2.2" height="2.2" patternUnits="userSpaceOnUse"
               patternTransform="rotate(45)">
        <rect width="2.2" height="2.2" fill="{SURFACE}"/>
        <line x1="0" y1="0" x2="0" y2="2.2" stroke="{BRASS}" stroke-width="0.9"/>
      </pattern>
      <linearGradient id="attfill" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#8F7008"/>
        <stop offset="1" stop-color="{BRASS}"/>
      </linearGradient>
    </defs>
    <rect x="0" y="0" width="100" height="6" rx="1" fill="{SURFACE}" stroke="{LINE}"
          stroke-width="0.25"/>
    <rect x="0" y="0" width="{att_pct:.3f}" height="6" rx="1" fill="url(#attfill)"/>
    <!-- GAP is a sliver of surface between the two fills. Adjacent segments that
         touch read as one mark, which is the opposite of what this band is for:
         the whole point is that "we earned it" and "they paid anyway" are
         different money. -->
    <rect x="{att_pct + gap:.3f}" y="0" width="{max(0.0, self_pct - gap):.3f}" height="6"
          fill="url(#selfpay)"/>
  </svg>

  <div style="display:flex; gap:1.4rem; margin-top:0.65rem; flex-wrap:wrap;
              font-family:{FONT_BODY}; font-size:0.78rem; color:{SLATE};">
    <span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;
      background:{BRASS};margin-right:0.45rem;"></span>
      Recovered by us <span style="font-family:{FONT_MONO};color:{PAPER};">
      {compact_inr(attributed_paise)}</span> · {att_pct:.1f}%</span>
    <span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;
      background:repeating-linear-gradient(45deg,{BRASS} 0 2px,{SURFACE} 2px 4px);
      margin-right:0.45rem;"></span>
      Customer self-paid <span style="font-family:{FONT_MONO};color:{PAPER};">
      {compact_inr(max(0, recovered_paise - attributed_paise))}</span> · {self_pct:.1f}%</span>
    <span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;
      background:{SURFACE};border:1px solid {LINE};margin-right:0.45rem;"></span>
      Still open <span style="font-family:{FONT_MONO};color:{PAPER};">
      {compact_inr(max(0, at_risk_paise - recovered_paise))}</span></span>
  </div>
</div>
"""


def section(title: str, note: str | None = None) -> None:
    """A section heading with an optional one-line explanation beneath it."""
    st.markdown(
        f"<h3 style='margin-bottom:0.15rem;'>{title}</h3>"
        + (f"<p style='color:{SLATE};font-size:0.85rem;margin:0 0 0.9rem 0;'>{note}</p>"
           if note else "<div style='height:0.6rem;'></div>"),
        unsafe_allow_html=True,
    )


def bar_headroom(fig: Any, values: Any, *, pad: float = 1.18) -> Any:
    """
    Extend a bar chart's value axis so `textposition="outside"` labels fit.

    Without this the longest bar — always the one that matters most — has its
    own label clipped by the plot edge: 320 rendered as "32(". Plotly does not
    reserve room for outside text, so the axis has to.
    """
    top = max(list(values) + [0])
    fig.update_traces(cliponaxis=False)
    fig.update_xaxes(range=[0, top * pad if top else 1])
    return fig


def style_fig(fig: Any, *, height: int = 320) -> Any:
    """Final pass every figure goes through, so no view can drift off-template."""
    register_plotly_template()
    fig.update_layout(height=height, template="recovery")
    return fig
