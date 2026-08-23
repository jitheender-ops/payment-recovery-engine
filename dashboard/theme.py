"""
The dashboard's design system: tokens, CSS, chart template, and the ledger band.

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
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

# ── Surfaces and ink ─────────────────────────────────────────────────────
INK = "#12161C"        # page ground
SURFACE = "#1A1F27"    # raised panels, chart plotting area
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

# ── Categorical series ───────────────────────────────────────────────────
# Fixed order, never cycled. A ninth series folds into "Other" rather than
# getting a generated hue.
SERIES = [BRASS, "#009592", CLAY, "#757DD0", "#5F9752"]

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


# ── Chart template ───────────────────────────────────────────────────────


def register_plotly_template() -> None:
    """One template every chart inherits: recessive grid, no chart junk."""
    pio.templates["recovery"] = go.layout.Template(
        layout=go.Layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font={"family": "IBM Plex Sans, system-ui, sans-serif", "color": SLATE, "size": 13},
            title={"font": {"family": "Bricolage Grotesque, sans-serif",
                            "color": PAPER, "size": 16}, "x": 0, "xanchor": "left"},
            colorway=SERIES,
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
                        "font": {"color": PAPER, "family": "IBM Plex Sans, sans-serif"}},
            separators=".,",
        )
    )
    pio.templates.default = "recovery"


# ── CSS ──────────────────────────────────────────────────────────────────
# Only what config.toml cannot express. Selectors are data-testid attributes
# rather than generated class names, because the generated ones change between
# Streamlit releases and take the whole design with them when they do.

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {{ font-family: {FONT_BODY}; }}

.block-container {{ padding-top: 2.4rem; max-width: 1280px; }}

h1, h2, h3 {{ font-family: {FONT_DISPLAY}; letter-spacing: -0.02em; color: {PAPER}; }}
h1 {{ font-weight: 800; font-size: 2.1rem; }}
h2 {{ font-weight: 600; font-size: 1.35rem; margin-top: 0.4rem; }}
h3 {{ font-weight: 600; font-size: 1.05rem; color: {SLATE};
      text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.78rem; }}

/* Figures are tabular everywhere. Money that does not align cannot be scanned
   down a column, which is the only way anyone reads a money table. */
[data-testid="stMetricValue"] {{
    font-family: {FONT_MONO}; font-weight: 500; font-size: 1.75rem;
    color: {PAPER}; font-variant-numeric: tabular-nums;
}}
[data-testid="stMetricLabel"] {{
    color: {SLATE}; text-transform: uppercase;
    letter-spacing: 0.07em; font-size: 0.72rem; font-weight: 500;
}}
[data-testid="stMetric"] {{
    background: {SURFACE}; border: 1px solid {LINE};
    border-radius: 10px; padding: 1rem 1.1rem;
}}

[data-testid="stSidebar"] {{ background: {SURFACE}; border-right: 1px solid {LINE}; }}
[data-testid="stSidebar"] .block-container {{ padding-top: 1.5rem; }}

[data-testid="stDataFrame"] {{ border: 1px solid {LINE}; border-radius: 10px; }}

hr {{ border-color: {LINE}; }}

/* Focus must stay visible — this is a console people drive from the keyboard. */
*:focus-visible {{ outline: 2px solid {BRASS_TEXT} !important; outline-offset: 2px; }}

@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0.001ms !important;
                              transition-duration: 0.001ms !important; }}
}}
</style>
"""


def apply() -> None:
    """Inject the CSS and register the chart template. Call once, after set_page_config."""
    st.markdown(_CSS, unsafe_allow_html=True)
    register_plotly_template()


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
    </defs>
    <rect x="0" y="0" width="100" height="6" rx="1" fill="{SURFACE}" stroke="{LINE}"
          stroke-width="0.25"/>
    <rect x="0" y="0" width="{att_pct:.3f}" height="6" rx="1" fill="{BRASS}"/>
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
    fig.update_layout(height=height, template="recovery")
    return fig
