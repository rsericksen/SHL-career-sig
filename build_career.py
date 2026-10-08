"""Render sig.career.template.svg + data.json into jagger.career.svg.

This is the career-aggregate build. data.json holds ONE stats object, and for this
build it means career totals to date: update it in place as the career goes on.
Nothing in here sums seasons. Counting figures (GP, G, A, hits, ...) are running
totals; time on ice is total seconds, so the per-game averages come out right on
their own; and the possession and rate figures (CF%, FF%, PDO, the per-60s) are
whatever career-wide values you enter, which must be weighted rather than added.

The palette comes from team.colors in data.json (see the palette section). With
no colours given, the signature keeps its original cyan and pink.

Nothing here draws anything. The template is hand-authored and stays that way;
this only computes the numbers that change and substitutes them into tokens.

The output SVG is written only after the rendered string passes every check in
validate(). A stale signature is harmless. A broken one appears on every post
you make, so the failure mode we optimise for is "refuse to write".
"""
import colorsys
import json
import math
import pathlib
import re
import sys
import xml.etree.ElementTree as ElementTree
from decimal import Decimal, ROUND_HALF_UP

HERE = pathlib.Path(__file__).parent
TEMPLATE_PATH = HERE / "sig.career.template.svg"
DATA_PATH = HERE / "data.json"
LOGO_PATH = HERE / "logo.svg"
OUTPUT_PATH = HERE / "jagger.career.svg"

CANVAS_WIDTH = 620.0
BAR_MAX_TPE = 2000.0

RADAR_CENTRE_X = 338.0
RADAR_CENTRE_Y = 78.0
RADAR_MAX_RADIUS = 54.0
RADAR_SCALE_MAX = 20.0

# Clockwise from the top. Order here is the order the polygon points are emitted,
# so it must match the axis captions baked into the template.
RADAR_AXES = (
    ("SKATING", ("acceleration", "agility", "speed"), -90),
    ("SENSE", ("offensiveRead", "defensiveRead", "positioning"), -30),
    ("PUCK", ("passing", "puckhandling"), 30),
    ("PHYSICAL", ("hitting", "checking", "strength", "fighting"), 90),
    ("STICK", ("stickchecking", "shotBlocking"), 150),
    ("MENTAL", ("determination", "leadership", "temperament", "professionalism"), 210),
)

GHOST_LABEL = "CAREER"  # what the ghost says when data.json has no season span to show

LABEL_PAD = 6.0  # breathing room between a bar label and the zone edge it sits against

# Ticker pacing. Every card holds for the same length of time no matter how many
# are in the rotation, so adding cards lengthens the loop rather than speeding it
# up. A card that flashes past cannot be read at all; a card late in a long loop
# is at least legible to anyone who lingers.
CARD_DWELL_SECONDS = 3.5
CARD_FADE_IN = 0.15   # fraction of a card's turn spent fading in
CARD_FADE_OUT = 0.85  # fraction at which it starts fading out

REGULAR_CARDS = 6
PLAYOFF_CARDS = 4

MIN_OUTPUT_BYTES = 24000  # a signature this small has lost either the crest or the rail
SIZE_TOLERANCE = 0.10


class BuildError(Exception):
    """The signature cannot be rendered, or was rendered wrong. Never write on this."""


def round1(value):
    """Round half away from zero, so 100.95 gives 101.0 rather than banker's 100.9."""
    return float(Decimal(repr(float(value))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def fmt1(value):
    return f"{round1(value):.1f}"


def text_width(text, size=9.0, tracking=1.0):
    """Rough advance width for the .tpeLbl face.

    Verdana Bold digits and caps run near 0.62em and the class adds 1px of
    tracking per character. Only used to decide whether a label fits its zone,
    so an approximation with headroom is enough.
    """
    return len(text) * (size * 0.62 + tracking)


def format_height(raw):
    match = re.fullmatch(r"\s*(\d+)\s*ft\s*(\d+)\s*in\s*", raw)
    if not match:
        raise BuildError(f"cannot parse height {raw!r}; expected a form like '6ft 1in'")
    return f"{match.group(1)}'{match.group(2)}\""


# --------------------------------------------------------------------------
# player name width
#
# The name is set in Arial Narrow, which plenty of machines do not have; the
# fallback is a full-width sans that runs about 25% wider and carries a long name
# across the column divider and into the radar. So the name is given an explicit
# width instead of leaving it to whichever font turns up: SVG's textLength squeezes
# or stretches the glyphs to fit it. The width is what Arial Narrow Bold would
# occupy, capped so the name always clears the divider at x=236.
# --------------------------------------------------------------------------

NAME_X = 26
NAME_FONT_SIZE = 19
NAME_TRACKING = 3
NAME_MAX_WIDTH = 180   # ends at x=206; the divider is at 236
NAME_LIMIT_X = 224     # validate() refuses a name that would end past here
ARIAL_NARROW_RATIO = 0.82  # Arial Narrow's glyphs are 82% of Arial's width

# Arial Bold advance widths, per 1000 em, for capitals and the punctuation names use.
ARIAL_BOLD_WIDTHS = {
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611, " ": 278, "-": 333, "'": 238, ".": 278,
}
ARIAL_BOLD_DEFAULT = 700  # accented capitals and anything unlisted


def name_length(name):
    """Width in px the name is set to: Arial Narrow Bold's, capped at NAME_MAX_WIDTH."""
    em = sum(ARIAL_BOLD_WIDTHS.get(ch, ARIAL_BOLD_DEFAULT) for ch in name) / 1000
    natural = em * ARIAL_NARROW_RATIO * NAME_FONT_SIZE + NAME_TRACKING * len(name)
    return min(natural, NAME_MAX_WIDTH)


def check_name(svg):
    """The rendered name must carry an explicit width that stops short of the divider."""
    match = re.search(r'<text class="nm"[^>]*\btextLength="([\d.]+)"', svg)
    if not match:
        return ["the player name has no textLength, so a wide fallback font could run into the radar"]
    end = NAME_X + float(match.group(1))
    if end > NAME_LIMIT_X:
        return [f"the player name would end at x={end:.0f}, past the divider clearance at x={NAME_LIMIT_X}"]
    return []


def career_span(stats):
    """The seasons these totals run across, for the ghosted label: S89-91, or S90 for one.

    Falls back to the word CAREER when data.json carries no usable first season
    (an older file, or one written by hand).
    """
    first, last = stats.get("firstSeason"), stats.get("season")
    valid = all(isinstance(v, int) and not isinstance(v, bool) for v in (first, last))
    if not valid or first > last:
        return GHOST_LABEL
    return f"S{first}" if first == last else f"S{first}\u2013{last}"


def format_toi(total_seconds, games):
    if games <= 0:
        raise BuildError("cannot average time on ice over zero games played")
    per_game = round(total_seconds / games)
    return f"{per_game // 60}:{per_game % 60:02d}"


def radar_points(attributes):
    """Return [(x, y), ...] for the six grouped averages, plotted on a 0-20 scale."""
    points = []
    for name, keys, angle in RADAR_AXES:
        missing = [k for k in keys if k not in attributes]
        if missing:
            raise BuildError(f"radar axis {name} needs {', '.join(missing)}")
        average = sum(attributes[k] for k in keys) / len(keys)
        radius = RADAR_MAX_RADIUS * average / RADAR_SCALE_MAX
        radians = math.radians(angle)
        points.append(
            (
                RADAR_CENTRE_X + radius * math.cos(radians),
                RADAR_CENTRE_Y + radius * math.sin(radians),
            )
        )
    return points


def bar_geometry(total_tpe, applied_tpe):
    """Widths and label placement for the TPE bar.

    The bar's three zones are the three figures: the solid fill is applied TPE,
    the lighter fill running out to the head is banked, and the head is the
    total. Banked is therefore never labelled; it is the gap between the two.
    """
    if applied_tpe > total_tpe:
        raise BuildError(f"applied TPE {applied_tpe} exceeds total {total_tpe}")
    if total_tpe > BAR_MAX_TPE:
        raise BuildError(
            f"total TPE {total_tpe} overflows the {BAR_MAX_TPE:.0f} bar; the design needs rescaling"
        )

    total_px = round1(CANVAS_WIDTH * total_tpe / BAR_MAX_TPE)
    applied_px = round1(CANVAS_WIDTH * applied_tpe / BAR_MAX_TPE)

    applied_label = str(applied_tpe)
    total_label = f"{total_tpe} TPE"
    scale_label = "2000"

    # If the solid zone is too narrow to hold its own number, the number moves
    # out to the head rather than overrunning into the track. Only reachable
    # below roughly 100 applied TPE, i.e. a brand new player.
    if applied_px < text_width(applied_label) + 2 * LABEL_PAD:
        applied_label = ""
        total_label = f"{total_tpe} TPE  ·  {applied_tpe} APPLIED"

    # The 2000 scale marker is the first thing to go when the track gets short.
    track_room = CANVAS_WIDTH - total_px
    if track_room < text_width(total_label) + text_width(scale_label) + 3 * LABEL_PAD:
        scale_label = ""

    return {
        "TPE_TOTAL_PX": fmt1(total_px),
        "TPE_APPLIED_PX": fmt1(applied_px),
        "TPE_HEAD_X": fmt1(total_px),
        "TPE_APPLIED_LABEL_X": fmt1(applied_px - LABEL_PAD),
        "TPE_APPLIED_LABEL": applied_label,
        "TPE_TOTAL_LABEL_X": fmt1(total_px + LABEL_PAD),
        "TPE_TOTAL_LABEL": total_label,
        "TPE_SCALE_LABEL": scale_label,
    }


def counting_tokens(stats, prefix):
    """Figures that mean the same thing over four games as over sixty-six.

    Goals, hits and blocks are facts about what happened. They are simply small
    when the sample is small, which is honest. Contrast advanced_tokens below.
    """
    games = stats["gamesPlayed"]
    shots = stats["shotsOnGoal"]
    shooting_pct = (stats["goals"] / shots * 100) if shots else 0.0
    return {
        f"{prefix}_GP": str(games),
        f"{prefix}_G": str(stats["goals"]),
        f"{prefix}_A": str(stats["assists"]),
        f"{prefix}_P": str(stats["points"]),
        f"{prefix}_PM": f"{stats['plusMinus']:+d}",
        f"{prefix}_PIM": str(stats["pim"]),
        f"{prefix}_SOG": str(shots),
        f"{prefix}_SHPCT": fmt1(shooting_pct),
        f"{prefix}_HITS": str(stats["hits"]),
        f"{prefix}_BLK": str(stats["shotsBlocked"]),
        f"{prefix}_TK": str(stats["takeaways"]),
        f"{prefix}_GV": str(stats["giveaways"]),
        # Shown instead of raw giveaways. Takeaways and this net recover the
        # giveaway count exactly, so the card loses nothing by carrying it.
        f"{prefix}_TKGV": f"{stats['takeaways'] - stats['giveaways']:+d}",
        f"{prefix}_PPP": str(stats["ppPoints"]),
        f"{prefix}_SHP": str(stats["shPoints"]),
        f"{prefix}_TOIGP": format_toi(stats["timeOnIce"], games),
        f"{prefix}_PPTOI": format_toi(stats["ppTimeOnIce"], games),
        f"{prefix}_SHTOI": format_toi(stats["shTimeOnIce"], games),
    }


def advanced_tokens(stats, prefix):
    """Possession and rate metrics, which estimate true talent rather than record events.

    Deliberately regular-season only. Over a short playoff run these are noise:
    PDO regresses to 100 by construction, so a four-game 113.3 describes luck,
    not the player, and putting it on the sig beside a 66-game number would
    invite exactly the wrong comparison.

    In the career build these are career-wide rates, entered by hand. They are
    not added across seasons, so weight them when updating: CF%, FF% and PDO by
    games (or better, by time on ice), and the per-60 figures by time on ice.
    """
    advanced = stats["advanced"]
    return {
        f"{prefix}_CF": fmt1(advanced["CFPct"]),
        f"{prefix}_FF": fmt1(advanced["FFPct"]),
        f"{prefix}_PDO": fmt1(advanced["PDO"]),
        f"{prefix}_GF60": fmt1(advanced["GF60"]),
        f"{prefix}_GA60": fmt1(advanced["GA60"]),
        f"{prefix}_SF60": fmt1(advanced["SF60"]),
        f"{prefix}_SA60": fmt1(advanced["SA60"]),
    }


def trim(value, places):
    """Shortest CSS-safe form: 21 rather than 21.00, 14.167 rather than 14.16700."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def cycle_timings(card_count):
    """Animation timings for a rail of card_count cards.

    The keyframe percentages are one card's turn expressed against the whole
    loop, so they have to move with the card count. Hardcoding them is what
    silently breaks the rotation when a card is added.
    """
    if card_count < 1:
        raise BuildError("the ticker rail needs at least one card")
    dwell = CARD_DWELL_SECONDS
    total = dwell * card_count
    slot = 100.0 / card_count  # one card's share of the loop, as a percentage

    # Negative delays start each card partway through the loop, so card i comes
    # up at (i-1) * dwell seconds. Card 1 needs none; it leads.
    delays = " ".join(
        f".c{i}{{animation-delay:-{trim(total - (i - 1) * dwell, 2)}s}}"
        for i in range(2, card_count + 1)
    )
    return {
        "CYCLE_SECONDS": trim(total, 2),
        "CYCLE_DELAYS": delays,
        "CYCLE_IN": trim(slot * CARD_FADE_IN, 3),
        "CYCLE_HOLD": trim(slot * CARD_FADE_OUT, 3),
        "CYCLE_OUT": trim(slot, 3),
    }


# --------------------------------------------------------------------------
# club mark
#
# The crest is fetched into logo.svg from the league's sprite stack, so it
# changes on its own when the player is traded. All that is left to decide is how
# large it sits, which cannot be automatic: the leagues draw their marks to
# wildly different proportions, some a compact crest and some a wide lockup.
# --------------------------------------------------------------------------

MARK_CENTRE_X = 535.0
MARK_CENTRE_Y = 76.0

# Fitted by the artwork's longer side. Tampa Bay's lockup carries its own
# wordmark, and 132 is the size at which its smaller line stays legible while
# the whole thing still clears the column divider.
MARK_PRESENTATION = {"Tampa_Bay": {"box": 132.0}}
DEFAULT_PRESENTATION = {"box": 120.0}


def read_mark(markup):
    """Split the fetched crest into its viewBox and its drawable body."""
    opening = re.match(r"<svg\b[^>]*>", markup.strip())
    if not opening:
        raise BuildError("logo.svg does not start with an <svg> element")
    view_box = re.search(r'viewBox="([^"]+)"', opening.group(0))
    if not view_box:
        raise BuildError("logo.svg has no viewBox, so the mark cannot be scaled")
    numbers = [float(n) for n in view_box.group(1).split()]
    if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
        raise BuildError(f"logo.svg viewBox {view_box.group(1)!r} is not a usable box")
    identifier = re.search(r'\sid="([^"]+)"', opening.group(0))
    body = markup.strip()[len(opening.group(0)) : markup.strip().rindex("</svg>")]
    return identifier.group(1) if identifier else "", numbers, body


def mark_geometry(markup):
    """Where the fetched crest sits, and how large."""
    name, view_box, body = read_mark(markup)
    presentation = MARK_PRESENTATION.get(name, DEFAULT_PRESENTATION)
    _, _, width, height = view_box
    scale = presentation["box"] / max(width, height)
    return body, {
        "MARK_TRANSFORM": (
            f"translate({fmt1(MARK_CENTRE_X - width * scale / 2)},"
            f"{fmt1(MARK_CENTRE_Y - height * scale / 2)}) scale({trim(scale, 5)})"
        )
    }


def optional_block(template, name, keep):
    """Keep or drop one marked region, removing its markers either way."""
    region = r"[ \t]*<!--%s_START-->\n(.*?)[ \t]*<!--%s_END-->\n" % (name, name)
    match = re.search(region, template, re.DOTALL)
    if not match:
        raise BuildError(f"template has no <!--{name}_START/END--> block")
    return re.sub(region, match.group(1) if keep else "", template, flags=re.DOTALL)


def prepare_template(template, has_playoffs):
    """Resolve the optional region before any token is substituted.

    The cards live in the template so the design stays in one file; only the
    decision to include them lives here.
    """
    return optional_block(template, "PLAYOFF_CARDS", has_playoffs)


def splice_mark(svg, body):
    """Drop the fetched crest in after token substitution.

    Deliberately last: the crest is markup from another system, and doing this
    after rendering means nothing inside it can ever be mistaken for a token.
    """
    marker = "<!--CLUB_MARK-->"
    if svg.count(marker) != 1:
        raise BuildError(f"template needs exactly one {marker}, found {svg.count(marker)}")
    return svg.replace(marker, body)


def count_cards(template):
    numbers = sorted(int(n) for n in re.findall(r'class="card c(\d+)"', template))
    if numbers != list(range(1, len(numbers) + 1)):
        raise BuildError(f"card classes are not a contiguous run from c1: found {numbers}")
    return len(numbers)


# --------------------------------------------------------------------------
# team palette
#
# team.colors in data.json names the club's colours:
#   primary    required. Becomes --accent, which carries everything structural.
#   secondary  optional. Becomes --accent2 (banked TPE, the ghosted label). When
#              absent it is derived by turning the primary's hue, which is the
#              relationship the original cyan and pink had.
#   base       optional. Only its hue is used, to tint the near-black surfaces.
#              Defaults to the primary. Use it when the club's main colour is
#              a dark one (navy, maroon) that suits the background better than
#              it suits an accent.
#
# A club's printed colours rarely survive a near-black background as they are:
# navy disappears and two reds read as one. So a colour that is too dark is
# lifted (hue and saturation held) until it can be read, and a pair that cannot
# be told apart is refused. The same limits are checked again on the rendered
# SVG in check_palette, so the two cannot drift apart.
# --------------------------------------------------------------------------

# The signature before it took colours from the club. Used untouched when
# team.colors is absent, so an unmodified data.json renders exactly as it did.
ORIGINAL_PALETTE = {
    "PAL_BG": "#12161c", "PAL_PANEL": "#161c24", "PAL_RAIL": "#0b0e13",
    "PAL_RULE": "#28313e", "PAL_GRID": "#222b37", "PAL_WELL": "#0b0e13",
    "PAL_INK": "#eef2f7", "PAL_MUTED": "#78849a",
    "PAL_ACCENT": "#0ad2d2", "PAL_ACCENT2": "#f499c2", "PAL_ON_ACCENT": "#0b0e13",
}

# Surface lightness and saturation measured from that original palette, so a
# tinted set has the same depth and contrast; only the hue moves.
SURFACE_LIGHTNESS = {"BG": 0.090, "PANEL": 0.114, "RAIL": 0.059, "RULE": 0.200, "GRID": 0.175}
SURFACE_SATURATION = 0.22
INK = (0.951, 0.36)    # lightness, saturation
MUTED = (0.537, 0.144)
TINT_FULL_AT = 0.30    # a base less saturated than this tints proportionally less (black or grey: not at all)

MIN_ACCENT_CONTRAST = 4.5     # --accent sets the position line, which is small text
MIN_ACCENT2_CONTRAST = 3.0    # fills and a ghosted label only
MIN_MUTED_CONTRAST = 4.5      # the original was 4.54 on the panel
MIN_ON_ACCENT_CONTRAST = 4.5  # text inside the solid applied-TPE segment
MIN_ACCENT_SEPARATION = 100   # RGB distance; below this the two bar segments read as one

DERIVED_HUE_TURN = 150        # degrees from primary to a derived secondary (cyan to pink)
DERIVED_MIN_LIGHTNESS = 0.72
PALETTE_VARS = ("bg", "panel", "rail", "rule", "grid", "well",
                "ink", "muted", "accent", "accent2", "onAccent",
                "numFace", "numTrim", "numShadow")

# The jersey number is lettered from the club colours, but eased toward the
# background so it sits behind the name instead of competing with it. Each figure
# is how far that layer is faded into the background: 0 is the full colour.
NUMBER_FACE_FADE = 0.10
NUMBER_TRIM_FADE = 0.25
NUMBER_SHADOW_FADE = 0.50

HEX_PATTERN = re.compile(r"#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})")


def parse_hex(value, field):
    match = HEX_PATTERN.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        raise BuildError(f"{field} {value!r} is not a hex colour like '#0ad2d2'")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def to_hls(rgb):
    return colorsys.rgb_to_hls(*(c / 255 for c in rgb))


def from_hls(hue, lightness, saturation):
    r, g, b = colorsys.hls_to_rgb(hue % 1.0, min(1.0, max(0.0, lightness)), min(1.0, max(0.0, saturation)))
    return (round(r * 255), round(g * 255), round(b * 255))


def luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio, 1 to 21."""
    lighter, darker = sorted((luminance(a), luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def distance(a, b):
    return math.dist(a, b)


def fade(rgb, into, amount):
    """rgb moved `amount` (0 to 1) of the way toward `into`."""
    return tuple(round(a * (1 - amount) + b * amount) for a, b in zip(rgb, into))


def number_tones(bg, ink, accent, accent2):
    """The three colours the jersey number is lettered in, as PAL_ tokens."""
    return {
        "PAL_NUM_FACE": to_hex(fade(ink, bg, NUMBER_FACE_FADE)),
        "PAL_NUM_TRIM": to_hex(fade(accent, bg, NUMBER_TRIM_FADE)),
        "PAL_NUM_SHADOW": to_hex(fade(accent2, bg, NUMBER_SHADOW_FADE)),
    }


def lift_until(rgb, against, minimum):
    """Raise lightness a percent at a time, hue and saturation held, until readable."""
    hue, lightness, saturation = to_hls(rgb)
    lifted = rgb
    while contrast(lifted, against) < minimum and lightness < 1.0:
        lightness = min(1.0, lightness + 0.01)
        lifted = from_hls(hue, lightness, saturation)
    return lifted


ORIGINAL_PALETTE.update(number_tones(
    parse_hex(ORIGINAL_PALETTE["PAL_BG"], "bg"), parse_hex(ORIGINAL_PALETTE["PAL_INK"], "ink"),
    parse_hex(ORIGINAL_PALETTE["PAL_ACCENT"], "accent"), parse_hex(ORIGINAL_PALETTE["PAL_ACCENT2"], "accent2"),
))


def derive_palette(colors):
    """Return the PAL_* tokens for team.colors, or the original palette without it."""
    if not colors:
        return dict(ORIGINAL_PALETTE)
    if not isinstance(colors, dict):
        raise BuildError('team.colors must be an object like {"primary": "#0ad2d2"}')
    unknown = sorted(set(colors) - {"primary", "secondary", "base"})
    if unknown:
        raise BuildError(
            f"team.colors has unknown keys: {', '.join(unknown)} (allowed: primary, secondary, base)"
        )
    if "primary" not in colors:
        raise BuildError("team.colors needs a primary colour")

    primary = parse_hex(colors["primary"], "team.colors.primary")
    base = parse_hex(colors.get("base", colors["primary"]), "team.colors.base")
    base_hue, _, base_saturation = to_hls(base)
    tint = min(1.0, base_saturation / TINT_FULL_AT)

    def surface(lightness, saturation=SURFACE_SATURATION):
        return from_hls(base_hue, lightness, saturation * tint)

    bg, panel, rail, rule, grid = (surface(SURFACE_LIGHTNESS[k]) for k in ("BG", "PANEL", "RAIL", "RULE", "GRID"))
    ink = surface(*INK)
    notes = []

    def fit(label, rgb, minimum):
        fitted = lift_until(rgb, panel, minimum)
        if fitted != rgb:
            notes.append(f"{label} {to_hex(rgb)} is too dark for the background; using {to_hex(fitted)}")
        return fitted

    muted = lift_until(surface(*MUTED), panel, MIN_MUTED_CONTRAST)
    accent = fit("primary", primary, MIN_ACCENT_CONTRAST)

    if "secondary" in colors:
        secondary = parse_hex(colors["secondary"], "team.colors.secondary")
    else:
        hue, lightness, saturation = to_hls(accent)
        if saturation < 0.10:
            raise BuildError(
                "the primary colour is grey, so there is nothing to derive a secondary from; "
                "set team.colors.secondary"
            )
        secondary = from_hls(
            hue + DERIVED_HUE_TURN / 360,
            max(lightness, DERIVED_MIN_LIGHTNESS),
            min(max(saturation, 0.45), 0.85),
        )
        notes.append(f"no secondary colour given; derived {to_hex(secondary)} from the primary")
    accent2 = fit("secondary", secondary, MIN_ACCENT2_CONTRAST)

    if distance(accent, accent2) < MIN_ACCENT_SEPARATION:
        raise BuildError(
            f"primary {colors['primary']} and secondary {colors['secondary']} are too close to tell "
            "apart on the TPE bar; choose a more distinct secondary, or leave it out to have one derived"
        )

    on_accent = max((rail, ink), key=lambda candidate: contrast(candidate, accent))

    for note in notes:
        print(f"note: {note}")
    return {
        **number_tones(bg, ink, accent, accent2),
        "PAL_BG": to_hex(bg), "PAL_PANEL": to_hex(panel), "PAL_RAIL": to_hex(rail),
        "PAL_RULE": to_hex(rule), "PAL_GRID": to_hex(grid), "PAL_WELL": to_hex(rail),
        "PAL_INK": to_hex(ink), "PAL_MUTED": to_hex(muted),
        "PAL_ACCENT": to_hex(accent), "PAL_ACCENT2": to_hex(accent2),
        "PAL_ON_ACCENT": to_hex(on_accent),
    }


def check_palette(svg):
    """Read the palette back out of the rendered SVG and hold it to the same limits."""
    root = re.search(r":root\s*\{([^}]*)\}", svg)
    if not root:
        return ["no :root palette block in the output"]
    found = dict(re.findall(r"--(\w+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", root.group(1)))
    missing = [name for name in PALETTE_VARS if name not in found]
    if missing:
        return [f"palette value missing or malformed for: {', '.join('--' + n for n in missing)}"]

    colour = {name: parse_hex(value, f"--{name}") for name, value in found.items()}
    errors = []
    for name, against, minimum in (
        ("accent", "panel", MIN_ACCENT_CONTRAST),
        ("accent2", "panel", MIN_ACCENT2_CONTRAST),
        ("muted", "panel", MIN_MUTED_CONTRAST),
        ("onAccent", "accent", MIN_ON_ACCENT_CONTRAST),
    ):
        ratio = contrast(colour[name], colour[against])
        if ratio < minimum:
            errors.append(f"--{name} on --{against} reads at {ratio:.2f}:1, below the {minimum}:1 floor")
    gap = distance(colour["accent"], colour["accent2"])
    if gap < MIN_ACCENT_SEPARATION:
        errors.append(f"--accent and --accent2 are only {gap:.0f} apart; the TPE bar segments would merge")
    return errors


def build_tokens(data, card_count):
    player = data["player"]
    team = data["team"]
    stats = data["stats"]

    tokens = {
        "ARIA_LABEL": (
            f"{player['name']}, number {player['jerseyNumber']}, "
            f"{player['position']}, {team['name']}, {player['currentLeague']}, career statistics"
        ),
        "NUMBER": str(player["jerseyNumber"]),
        "NAME": player["name"].upper(),
        "NAME_LENGTH": f"{name_length(player['name'].upper()):.1f}",
        "POSITION": player["position"].upper(),
        "BIRTHPLACE": player["birthplace"].upper(),
        "DRAFT_CLASS": f"S{player['draftSeason']}",
        "HEIGHT": format_height(player["height"]),
        "WEIGHT": str(player["weight"]),
        "SHOOTS": player["handedness"].upper(),
        "GHOST": career_span(stats),
    }
    tokens.update(derive_palette(team.get("colors")))
    tokens.update(bar_geometry(player["totalTPE"], player["appliedTPE"]))
    tokens.update(cycle_timings(card_count))
    tokens.update(counting_tokens(stats["regular"], "ST"))
    tokens.update(advanced_tokens(stats["regular"], "ST"))
    if "playoffs" in stats:
        tokens.update(counting_tokens(stats["playoffs"], "PO"))

    points = radar_points(data["attributes"])
    tokens["RADAR_POINTS"] = " ".join(f"{fmt1(x)},{fmt1(y)}" for x, y in points)
    for index, (x, y) in enumerate(points, start=1):
        tokens[f"RADAR_X{index}"] = fmt1(x)
        tokens[f"RADAR_Y{index}"] = fmt1(y)
    return tokens


def render(template, tokens):
    unknown = set(re.findall(r"\{\{(\w+)\}\}", template)) - set(tokens)
    if unknown:
        raise BuildError(f"template uses tokens nothing supplies: {', '.join(sorted(unknown))}")
    rendered = template
    for name, value in tokens.items():
        rendered = rendered.replace("{{" + name + "}}", value)
    return rendered


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _keyframe_width(svg, keyframe):
    match = re.search(
        r"@keyframes\s+" + keyframe + r"\s*\{.*?to\s*\{\s*width:\s*([0-9.]+)px",
        svg,
        re.DOTALL,
    )
    return match.group(1) if match else None


def _reduced_motion_width(svg, css_class):
    match = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?\."
        + css_class
        + r"\s*\{[^}]*width:\s*([0-9.]+)px",
        svg,
        re.DOTALL,
    )
    return match.group(1) if match else None


def _rect_width(svg, css_class):
    match = re.search(r'<rect[^>]*class="' + css_class + r'"[^>]*width="([0-9.]+)"', svg)
    return match.group(1) if match else None


def check_bar_widths(svg):
    """The one thing most likely to end up half-updated.

    Each bar width is written in three independent places: the keyframe that
    animates it, the reduced-motion rule that pins it, and the rect attribute
    that renders it without CSS. Any one of them going stale is invisible until
    someone views the sig in the exact mode that reads the stale copy.
    """
    errors = []
    for keyframe, css_class in (("fillTotal", "tpeTotal"), ("fillApp", "tpeApp")):
        places = {
            f"@keyframes {keyframe}": _keyframe_width(svg, keyframe),
            f"reduced-motion .{css_class}": _reduced_motion_width(svg, css_class),
            f'<rect class="{css_class}">': _rect_width(svg, css_class),
        }
        absent = [where for where, value in places.items() if value is None]
        if absent:
            errors.append(f"{css_class}: width not found in {', '.join(absent)}")
            continue
        if len(set(places.values())) != 1:
            detail = ", ".join(f"{where}={value}" for where, value in places.items())
            errors.append(f"{css_class}: widths disagree across its three places: {detail}")
    return errors


def check_logo(svg, body):
    """The crest must arrive in the output substantially intact.

    Measured against the mark actually fetched rather than a fixed byte count,
    so the check keeps working when the player changes club and the new crest is a
    different size entirely.
    """
    want = sum(len(d) for d in re.findall(r'<path[^>]*\sd="([^"]+)"', body))
    got = sum(len(d) for d in re.findall(r'<path[^>]*\sd="([^"]+)"', svg))
    if not want:
        return ["the fetched club mark has no path data at all"]
    if got < want:
        return [
            f"output carries {got} bytes of path data against {want} in the fetched "
            "mark; the club mark was eaten"
        ]
    return []


def check_labels_fit(svg):
    """A bar label that runs off the canvas would be clipped by the viewBox."""
    errors = []
    for match in re.finditer(r'<text\s([^>]*class="tpeLbl[^"]*"[^>]*)>([^<]*)</text>', svg):
        attributes, text = match.group(1), match.group(2)
        if not text.strip():
            continue
        anchor = re.search(r'\bx="([0-9.]+)"', attributes)
        if not anchor:
            errors.append(f"bar label {text!r} has no usable x attribute")
            continue
        x = float(anchor.group(1))
        width = text_width(text)
        ends_at_x = 'text-anchor="end"' in attributes
        left = x - width if ends_at_x else x
        right = x if ends_at_x else x + width
        if right > CANVAS_WIDTH:
            errors.append(
                f"bar label {text!r} ends at x={right:.1f}, past the {CANVAS_WIDTH:.0f} canvas"
            )
        if left < 0:
            errors.append(f"bar label {text!r} starts at x={left:.1f}, left of the canvas")
    return errors


def validate(svg, template, body):
    """Return a list of reasons this SVG must not be written. Empty means good."""
    errors = []

    try:
        ElementTree.fromstring(svg)
    except ElementTree.ParseError as exc:
        errors.append(f"output is not well-formed XML: {exc}")

    leftover = sorted(set(re.findall(r"\{\{\w*\}?\}?", svg)))
    if leftover:
        errors.append(f"unsubstituted tokens remain: {', '.join(leftover)}")

    errors.extend(check_bar_widths(svg))
    errors.extend(check_palette(svg))
    errors.extend(check_name(svg))

    for required, label in (
        ("@media (prefers-reduced-motion: reduce)", "reduced motion block"),
        ("@keyframes fillTotal", "fillTotal keyframes"),
        ("@keyframes fillApp", "fillApp keyframes"),
        ("@keyframes cycle", "ticker cycle keyframes"),
        ("@keyframes bloom", "radar bloom keyframes"),
    ):
        if required not in svg:
            errors.append(f"{label} did not survive the build ({required!r} missing)")

    errors.extend(check_logo(svg, body))
    errors.extend(check_labels_fit(svg))

    size = len(svg.encode("utf-8"))
    if size < MIN_OUTPUT_BYTES:
        errors.append(f"output is {size} bytes, below the {MIN_OUTPUT_BYTES} floor")

    # The crest is no longer part of the template, so the template alone is no
    # longer the right yardstick. Template plus the mark spliced into it is.
    reference = len(template.encode("utf-8")) + len(body.encode("utf-8"))
    drift = abs(size - reference) / reference
    if drift > SIZE_TOLERANCE:
        errors.append(
            f"output is {size} bytes against an expected {reference} "
            f"(template plus mark), a {drift:.0%} change; over the "
            f"{SIZE_TOLERANCE:.0%} tolerance"
        )
    return errors


def build(template, data, mark_markup):
    """Render and validate. Raises BuildError rather than returning bad markup."""
    has_playoffs = "playoffs" in data["stats"]
    body, mark_tokens = mark_geometry(mark_markup)
    prepared = prepare_template(template, has_playoffs)

    card_count = count_cards(prepared)
    expected = REGULAR_CARDS + (PLAYOFF_CARDS if has_playoffs else 0)
    if card_count != expected:
        raise BuildError(
            f"expected {expected} cards with playoffs "
            f"{'present' if has_playoffs else 'absent'}, template has {card_count}"
        )

    tokens = build_tokens(data, card_count)
    tokens.update(mark_tokens)
    svg = splice_mark(render(prepared, tokens), body)
    errors = validate(svg, prepared, body)
    if errors:
        raise BuildError(f"refusing to write {OUTPUT_PATH.name}:\n  - " + "\n  - ".join(errors))
    return svg


def main():
    try:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        mark = LOGO_PATH.read_text(encoding="utf-8")
        svg = build(template, data, mark)
    except (OSError, json.JSONDecodeError, KeyError, BuildError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1

    OUTPUT_PATH.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_PATH.name}, {len(svg.encode('utf-8'))} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
