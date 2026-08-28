"""Optional StreamDeck integration for recording session controls."""

from __future__ import annotations

import math
import threading
from typing import Callable

from .instrument_colors import color_for_label, hex_to_rgb

try:
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.ImageHelpers import PILHelper
    from StreamDeck.Devices.StreamDeck import DialEventType
    _HAVE_STREAMDECK = True
except ImportError:
    _HAVE_STREAMDECK = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False


def _skip_hidapi_exit_crash() -> None:
    """python-elgato-streamdeck registers libhidapi's hid_exit() via atexit the
    first time it opens the HID transport (StreamDeck/Transport/LibUSBHIDAPI.py).
    On recent macOS (Apple Silicon, pointer authentication) calling hid_exit()
    during Python's interpreter-shutdown atexit phase reliably crashes with
    SIGTRAP inside IOHIDManagerUnscheduleFromRunLoop — by that point the run
    loop the HID devices were scheduled on is already gone. We already close
    our own device handle in disconnect(); skip the library's redundant
    global teardown call so the process exits cleanly instead (the OS
    reclaims the HID subsystem's handles on process exit regardless)."""
    try:
        import atexit
        from StreamDeck.Transport.LibUSBHIDAPI import LibUSBHIDAPI
        hidapi = LibUSBHIDAPI.Library.HIDAPI_INSTANCE
        if hidapi is not None:
            atexit.unregister(hidapi.hid_exit)
    except Exception:
        pass


def _probe_deck(deck) -> tuple[str, str] | None:
    """Read an already-open deck's stable serial number + model name (the
    (id, label) shape used throughout this module and by list_streamdecks()).
    Serial number isn't available before open() — caller owns open()/close().
    Returns None if either read fails."""
    try:
        serial = deck.get_serial_number()
        deck_type = deck.deck_type()
    except Exception:
        return None
    label = f"{deck_type} ({serial[-4:]})" if len(serial) >= 4 else deck_type
    return serial, label


# HID transports enforce exclusive access, so if a StreamDeckController in
# this same process already has a device open (e.g. the Record tab's
# persistent connection), a second open() from list_streamdecks() below
# would fail and silently drop that device from the settings dropdown —
# exactly the "Stream Deck shows as None" bug. deck.id() (the HID device
# path) is readable straight from enumerate() results with no open() call,
# so it's a safe key for recognizing "this is a device I already have
# open" and serving its cached (serial, label) instead of re-opening it.
_open_by_id: dict[str, tuple[str, str]] = {}
_open_by_id_lock = threading.Lock()


def list_streamdecks() -> list[tuple[str, str]]:
    """Enumerate every currently attached Stream Deck as (serial_number,
    label) pairs, for a settings dropdown. Each device not already held
    open by this process is briefly opened to read its serial (not
    available before open()) and closed again — this is a one-off listing
    call, not a live connection."""
    if not _HAVE_STREAMDECK:
        return []
    try:
        decks = DeviceManager().enumerate()
        _skip_hidapi_exit_crash()
    except Exception:
        return []
    results = []
    for deck in decks:
        try:
            device_key = deck.id()
        except Exception:
            device_key = None
        with _open_by_id_lock:
            cached = _open_by_id.get(device_key) if device_key is not None else None
        if cached is not None:
            results.append(cached)
            continue
        try:
            deck.open()
        except Exception:
            continue
        try:
            probe = _probe_deck(deck)
            if probe is not None:
                results.append(probe)
        finally:
            try:
                deck.close()
            except Exception:
                pass
    return results


# Button tuple: (key_index, icon_name, label, key_char, active_state_name, active_color, dim_color)
# active_state_name=None → always shown in active color.
_INSPIRATION_BUTTONS: list[tuple] = [
    (0, None,     None,   " ", None, None,            None),           # play/pause — rendered by update_inspiration
    (1, "skip",   "Skip", "s", None, (0,  120, 200),  (0,  120, 200)),
    (2, "quit",   "Quit", "q", None, (200,  30,  30), (200, 30,  30)),
]

_INSPIRATION_RESTART_BUTTON: tuple = (3, "prev", "Restart", "b", None, (255, 140, 0), (255, 140, 0))

_INSPIRATION_VOLUME_BUTTONS: list[tuple] = [
    (4, "vol_dn", "Vol -", "l", None, (0,  120, 200), (0,  120, 200)),
    (5, "vol_up", "Vol +", "u", None, (0,  120, 200), (0,  120, 200)),
]

# Shared layout for every recording context (Tk UI, headless `takeloom
# server`, and the CLI) — one table, one set of semantics, so the physical
# deck (and the Tk UI's on-screen emulator — see ui/streamdeck_emulator.py)
# behaves identically no matter which one is driving it. `active_state`
# here is a recording *phase* ("idle"/"waiting"/"recording"); Next and the
# monitor-mode toggle are always available so their active_state is None,
# while Restart only means anything mid-take and is dimmed otherwise.
_RECORDING_TOGGLE: tuple = (0, None, None, "r", None, None, None)  # rendered by update_recording_page
_RECORDING_NEXT: tuple = (2, "skip", "Next", "n", None, (0, 160, 220), (0, 160, 220))
_RECORDING_RESTART: tuple = (1, "prev", "Restart", "b", "recording", (255, 140, 0), (55, 35, 10))
RECORDING_MONITOR_TOGGLE_KEY_INDEX = 3
_RECORDING_MONITOR_TOGGLE: tuple = (
    RECORDING_MONITOR_TOGGLE_KEY_INDEX, None, None, "m", None, None, None,
)  # rendered by update_monitoring_mode
# Always available (like Next) rather than dimmed/enabled by phase — the
# backend itself is what actually decides whether there's anything to
# redraw (the current track must be a setlist "inspiration filter" slot's
# draw; see backend.py's redraw_current_track), so pressing this with
# nothing applicable just logs a message instead of doing anything,
# same as Next with nothing left in the setlist. Unlike Next, which
# advances to the next actual setlist position, this replaces whatever's
# currently loaded with a different random draw from the same filter.
_RECORDING_REDRAW: tuple = (10, "dice", "Redraw", "d", None, (150, 90, 220), (150, 90, 220))
# On a dial deck (e.g. the Stream Deck Plus, KEY_COUNT=8) the volume-button
# block (4-9) is never drawn — dials handle volume instead, see
# use_recording_layout — so key 4 sits free. use_recording_layout swaps
# this in for _RECORDING_REDRAW on dial decks so Redraw actually reaches
# the physical keys instead of being silently dropped by the key_count
# filter (index 10 is out of range for an 8-key deck).
_RECORDING_REDRAW_DIAL: tuple = (4, "dice", "Redraw", "d", None, (150, 90, 220), (150, 90, 220))

# The idle-family layouts — three sub-states, all only ever shown while
# Backend's own recording phase is "idle" (see update_recording_page's
# `identify_state` argument for how RecordingDeckDriver picks between
# them):
#
# - "idle": two explicit "how do you want to start" choices instead of a
#   single Start button plus a separate settings-tab checkbox, so
#   streaming-vs-not is decided at the moment a session actually starts.
# - "identifying": entered the instant either idle button is pressed —
#   auto-detect is listening in the background (RecordingDeckDriver kicks
#   it off right then, not automatically on every idle transition the way
#   headless server mode briefly did) and there's nothing to press yet but
#   Re-identify, in case the scan needs restarting.
# - "ready": auto-detect has committed to an instrument — Play (what was
#   remembered as "Start Local" or "Start Streaming" back in "idle")
#   finally opens the session; Re-identify stays available in case the
#   wrong instrument got picked.
#
# Key indices 0-2 are reused by the active layout below once a session
# actually opens (0 becomes the Start/Unpause/Stop toggle, 1 becomes
# Restart, 2 becomes Next) — update_recording_page swaps the whole button
# set the moment phase crosses the idle boundary in either direction, or
# identify_state changes while still idle, so none of these ever coexist
# on the deck.
_RECORDING_START_LOCAL: tuple = (0, "record", "Start Local", "r", None, (0, 200, 0), (0, 200, 0))
_RECORDING_START_STREAMING: tuple = (1, "record", "Start Streaming", "s", None, (230, 0, 120), (230, 0, 120))
_RECORDING_REIDENTIFY: tuple = (2, "refresh", "Re-identify", "i", None, (90, 90, 210), (90, 90, 210))
_RECORDING_PLAY: tuple = (0, "play", "Play", "p", None, (0, 200, 0), (0, 200, 0))
RECORDING_IDLE_BUTTONS: list[tuple] = [_RECORDING_START_LOCAL, _RECORDING_START_STREAMING]
RECORDING_IDENTIFYING_BUTTONS: list[tuple] = [_RECORDING_REIDENTIFY]
RECORDING_IDENTIFIED_BUTTONS: list[tuple] = [_RECORDING_PLAY, _RECORDING_REIDENTIFY]

# Colors for the monitor-mode toggle — see update_monitoring_mode(). Live
# Monitor reuses Restart's "hot/active" orange (it's the same zero-latency
# hardware direct monitor path used while actually laying down a take);
# Production reuses the old Video Check button's blue.
LIVE_MONITOR_COLOR = (255, 140, 0)
PRODUCTION_MONITOR_COLOR = (0, 160, 200)

# RECORDING_BUTTONS/RECORDING_VOLUME_BUTTONS (the active, in-session
# layout) are public (no leading underscore), same as RECORDING_IDLE_
# BUTTONS above: the Tk UI's on-screen emulator (ui/streamdeck_emulator.py)
# draws the exact same button set as the physical device from these tables,
# always using _RECORDING_REDRAW's key index 10 since the emulator has
# room for it regardless of what physical deck (if any) is connected.
# On the real device, key 10 is past the volume block below (4-9) so it
# doesn't shift any of them — decks too small to have a key 10 at all
# (e.g. the 6-key Mini) just drop it via use_recording_layout's key_count
# filtering, same as any other button that doesn't fit; dial decks (e.g.
# the Stream Deck Plus) instead get it remapped to key 4 — see
# _RECORDING_REDRAW_DIAL above.
RECORDING_BUTTONS: list[tuple] = [
    _RECORDING_TOGGLE, _RECORDING_NEXT, _RECORDING_RESTART, _RECORDING_MONITOR_TOGGLE, _RECORDING_REDRAW,
]

RECORDING_VOLUME_BUTTONS: list[tuple] = [
    (4, "vol_dn",   "Vol -",   "l", None, (0,   120, 200), (0,   120, 200)),
    (5, "vol_up",   "Vol +",   "u", None, (0,   120, 200), (0,   120, 200)),
    (6, "takes_dn", "Takes -", "[", None, (120,   0, 200), (120,   0, 200)),
    (7, "takes_up", "Takes +", "]", None, (120,   0, 200), (120,   0, 200)),
    (8, "vol_dn",   "Instr -", ",", None, (0,   160, 90),  (0,   160, 90)),
    (9, "vol_up",   "Instr +", ".", None, (0,   160, 90),  (0,   160, 90)),
]

_SESSION_DIAL_MAP: dict[int, tuple[str, str, str]] = {
    0: ("l", "u", "Backing\nVol"),
    1: ("[", "]", "Takes\nVol"),
    2: (",", ".", "Instr\nVol"),
}

_INSPIRATION_DIAL_MAP: dict[int, tuple[str, str, str]] = {
    0: ("l", "u", "Volume"),
}

_FONT_PATHS = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _load_font(size: int):
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _format_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _draw_instrument_badge(draw: "ImageDraw.ImageDraw", canvas_w: int, cy: int, text: str) -> None:
    """A small colored "pill" for `text` (an instrument label, or a
    status placeholder like "Detecting…") centered horizontally at cy —
    the touchscreen equivalent of instrument_colors.py's tk.Label
    badges used everywhere else this same label shows up, so it reads as
    the same color at a glance whether you're looking at the deck or the
    Completed Takes tab. color_for_label falls back to a flat gray for
    anything that isn't actually one of INSTRUMENT_LABELS (a status
    placeholder, or a bare full_name when no label was set) rather than
    guessing — see that function's own docstring."""
    font = _load_font(13)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 10, 5
    box_w, box_h = text_w + pad_x * 2, text_h + pad_y * 2
    x0 = (canvas_w - box_w) // 2
    y0 = cy - box_h // 2
    color = hex_to_rgb(color_for_label(text.lower()))
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=box_h // 3, fill=color)
    draw.text((canvas_w // 2, cy), text, anchor="mm", font=font, fill="white")


def _draw_progress_bar(draw: "ImageDraw.ImageDraw", canvas_w: int, cy: int, position: float, duration: float) -> None:
    """Elapsed time / a filled bar / time remaining, all on one line —
    "how long since the song started, and how long until it ends", in
    one bar, at cy. Only called when duration > 0 (see _update_touchscreen)
    — a 0/0 bar would be meaningless."""
    font = _load_font(12)
    elapsed_text = _format_mmss(position)
    remaining_text = "-" + _format_mmss(max(0.0, duration - position))
    margin = 56  # room for the time labels at each end, outside the bar itself
    bar_left, bar_right = margin, canvas_w - margin
    bar_half_h = 4
    draw.rounded_rectangle(
        [bar_left, cy - bar_half_h, bar_right, cy + bar_half_h], radius=bar_half_h, fill=(70, 70, 70),
    )
    frac = max(0.0, min(1.0, position / duration)) if duration > 0 else 0.0
    filled_right = bar_left + int((bar_right - bar_left) * frac)
    if filled_right > bar_left:
        draw.rounded_rectangle(
            [bar_left, cy - bar_half_h, filled_right, cy + bar_half_h], radius=bar_half_h, fill=(0, 200, 120),
        )
    draw.text((8, cy), elapsed_text, anchor="lm", font=font, fill=(200, 200, 200))
    draw.text((canvas_w - 8, cy), remaining_text, anchor="rm", font=font, fill=(200, 200, 200))


# Cents within this of dead-center reads as "in tune" (green needle/zone)
# — roughly a real hardware tuner's own tolerance. Beyond _TUNER_WARN_
# CENTS the zone/needle goes red rather than amber — meaningfully off,
# not just "a little flat". Cents beyond _TUNER_DISPLAY_RANGE_CENTS
# either way just pin the needle at the scale's edge rather than trying
# to show exactly how far off — nobody needs a precise number when a
# string is a semitone-plus flat, just "a lot, that way".
_TUNER_IN_TUNE_CENTS = 5.0
_TUNER_WARN_CENTS = 25.0
_TUNER_DISPLAY_RANGE_CENTS = 50.0
_TUNER_GREEN = (0, 210, 130)
_TUNER_AMBER = (230, 160, 0)
_TUNER_RED = (215, 70, 60)


def _tuner_needle_color(cents: float) -> tuple:
    if abs(cents) <= _TUNER_IN_TUNE_CENTS:
        return _TUNER_GREEN
    if abs(cents) <= _TUNER_WARN_CENTS:
        return _TUNER_AMBER
    return _TUNER_RED


def _draw_tuner_needle(draw: "ImageDraw.ImageDraw", canvas_w: int, cy: int, note: str, cents: float) -> None:
    """The live tuner: `note` (e.g. "E2", already resolved against the
    playing instrument's configured string tuning — see audio/pitch.py's
    nearest_target) at the left, and a horizontal ±_TUNER_DISPLAY_RANGE_
    CENTS scale — a dark background track (so the needle reads against
    something, not bare black touchscreen), red/amber/green zone bands
    inside it (same at-a-glance idea as _draw_progress_bar's fill color,
    just for pitch instead of playback position), a clearly-marked center
    (dead in tune) and both ends (±_TUNER_DISPLAY_RANGE_CENTS), and a
    needle marking how sharp/flat `cents` currently is. `cents` is
    expected to already be smoothed by the caller (see RecordingDeckDriver
    .tuner_cents/audio/pitch.py's TunerSmoother) — this just draws
    whatever it's given."""
    font_note = _load_font(22)
    color = _tuner_needle_color(cents)

    draw.text((16, cy), note, anchor="lm", font=font_note, fill=color)

    scale_left, scale_right = 90, canvas_w - 16
    scale_mid = (scale_left + scale_right) // 2
    half_width = scale_right - scale_mid

    def x_at(cents_value: float) -> int:
        frac = max(-1.0, min(1.0, cents_value / _TUNER_DISPLAY_RANGE_CENTS))
        return scale_mid + int(frac * half_width)

    track_half_h = 12
    draw.rounded_rectangle(
        [scale_left, cy - track_half_h, scale_right, cy + track_half_h], radius=6, fill=(32, 32, 32),
    )

    band_half_h = 8
    green_l, green_r = x_at(-_TUNER_IN_TUNE_CENTS), x_at(_TUNER_IN_TUNE_CENTS)
    warn_l, warn_r = x_at(-_TUNER_WARN_CENTS), x_at(_TUNER_WARN_CENTS)
    draw.rectangle([scale_left, cy - band_half_h, warn_l, cy + band_half_h], fill=(70, 35, 30))
    draw.rectangle([warn_l, cy - band_half_h, green_l, cy + band_half_h], fill=(70, 55, 15))
    draw.rectangle([green_l, cy - band_half_h, green_r, cy + band_half_h], fill=(15, 65, 40))
    draw.rectangle([green_r, cy - band_half_h, warn_r, cy + band_half_h], fill=(70, 55, 15))
    draw.rectangle([warn_r, cy - band_half_h, scale_right, cy + band_half_h], fill=(70, 35, 30))

    # End markers (the ±_TUNER_DISPLAY_RANGE_CENTS boundary) and the
    # center mark (dead in tune) — the center drawn taller/brighter so
    # it's unmistakably the "aim for here" reference, not just another
    # tick.
    draw.line([scale_left, cy - track_half_h, scale_left, cy + track_half_h], fill=(140, 140, 140), width=2)
    draw.line([scale_right, cy - track_half_h, scale_right, cy + track_half_h], fill=(140, 140, 140), width=2)
    draw.line(
        [scale_mid, cy - track_half_h - 5, scale_mid, cy + track_half_h + 5], fill=(235, 235, 235), width=3,
    )

    needle_x = x_at(cents)
    draw.line([needle_x, cy - track_half_h - 7, needle_x, cy + track_half_h + 7], fill=color, width=4)


def _draw_icon(draw: "ImageDraw.ImageDraw", icon: str, cx: int, cy: int, size: int) -> None:
    """Draw a white icon centered at (cx, cy) within a size×size bounding box."""
    r = size // 2
    q = size // 4
    lw = max(2, size // 10)
    f = "white"

    if icon == "record":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=f)

    elif icon == "play":
        draw.polygon([(cx - r, cy - r), (cx + r, cy), (cx - r, cy + r)], fill=f)

    elif icon == "pause":
        bw = max(3, r // 2)
        draw.rectangle([cx - bw - 2, cy - r, cx - 2, cy + r], fill=f)
        draw.rectangle([cx + 2, cy - r, cx + bw + 2, cy + r], fill=f)

    elif icon == "stop":
        draw.rectangle([cx - r, cy - r, cx + r, cy + r], fill=f)

    elif icon == "prev":       # |◀  (back to start / restart)
        bw = max(2, r // 3)
        draw.rectangle([cx - r, cy - r, cx - r + bw, cy + r], fill=f)
        draw.polygon([(cx + r, cy - r), (cx - r + bw * 2, cy), (cx + r, cy + r)], fill=f)

    elif icon == "skip":       # ▶|  (skip / next track)
        bw = max(2, r // 3)
        draw.polygon([(cx - r, cy - r), (cx + r - bw * 2, cy), (cx - r, cy + r)], fill=f)
        draw.rectangle([cx + r - bw, cy - r, cx + r, cy + r], fill=f)

    elif icon == "quit":       # ✕
        draw.line([cx - r, cy - r, cx + r, cy + r], fill=f, width=lw)
        draw.line([cx + r, cy - r, cx - r, cy + r], fill=f, width=lw)

    elif icon == "dice":       # die face: rounded square, 3 diagonal pips
        draw.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=max(2, r // 3), outline=f, width=lw)
        pip_r = max(2, size // 10)
        for dx, dy in ((-r // 2, -r // 2), (0, 0), (r // 2, r // 2)):
            draw.ellipse([cx + dx - pip_r, cy + dy - pip_r, cx + dx + pip_r, cy + dy + pip_r], fill=f)

    elif icon == "headphones":
        # Headband arc over two ear cups
        band_top = cy - r
        draw.arc([cx - r, band_top, cx + r, band_top + size], start=180, end=360, fill=f, width=lw)
        cup_w, cup_h = max(3, size // 5), max(4, size // 3)
        draw.rounded_rectangle(
            [cx - r - cup_w // 2, cy - cup_h // 2, cx - r + cup_w // 2, cy + cup_h // 2], radius=cup_w // 2, fill=f,
        )
        draw.rounded_rectangle(
            [cx + r - cup_w // 2, cy - cup_h // 2, cx + r + cup_w // 2, cy + cup_h // 2], radius=cup_w // 2, fill=f,
        )

    elif icon in ("vol_dn", "vol_up"):
        # Speaker box + flared cone, then −/+
        bx = cx - r + r // 4      # right edge of box
        draw.rectangle([cx - r, cy - q, bx, cy + q], fill=f)
        draw.polygon([(bx, cy - q), (cx + q, cy - r), (cx + q, cy + r), (bx, cy + q)], fill=f)
        # − or + to the right of the cone
        sx1, sx2 = cx + q + 2, cx + r
        sy = cy
        draw.line([sx1, sy, sx2, sy], fill=f, width=lw)
        if icon == "vol_up":
            mx = (sx1 + sx2) // 2
            draw.line([mx, sy - q // 2, mx, sy + q // 2], fill=f, width=lw)

    elif icon == "refresh":    # circular re-scan arrow, arrowhead at the gap
        start_deg, end_deg = 40, 320
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=start_deg, end=end_deg, fill=f, width=lw)
        head = max(3, size // 6)
        hx = cx + r * math.cos(math.radians(start_deg))
        hy = cy - r * math.sin(math.radians(start_deg))
        draw.polygon(
            [(hx, hy), (hx - head, hy - head // 2), (hx - head // 3, hy + head)], fill=f,
        )

    elif icon in ("takes_dn", "takes_up"):
        # Three stacked horizontal bars (like track lanes in a DAW)
        bh = max(2, size // 10)
        for y_off, w in [(-q, r), (0, r * 3 // 4), (q, r // 2)]:
            draw.rectangle([cx - w, cy + y_off - bh, cx + w, cy + y_off + bh], fill=f)
        # − or + below the bars
        by = cy + r - bh
        bx1, bx2 = cx - r // 2, cx + r // 2
        draw.line([bx1, by, bx2, by], fill=f, width=lw)
        if icon == "takes_up":
            draw.line([cx, by - lw * 2, cx, by + lw * 2], fill=f, width=lw)


def _paint_key_face(draw: "ImageDraw.ImageDraw", size: tuple[int, int], icon: str | None, label: str | None) -> None:
    """Paint one key's icon + label onto an already-created image of the
    given (w, h) — the part of key rendering that's identical whether the
    destination is a real deck's native image format (_make_key_image) or
    a plain PIL image for the Tk UI's on-screen emulator (render_button_image)."""
    w, h = size
    if icon:
        icon_size = int(h * 0.42)
        icon_cy = int(h * 0.38)
        _draw_icon(draw, icon, w // 2, icon_cy, icon_size)
    if label:
        label_y = h - int(h * 0.14)
        draw.text((w // 2, label_y), label, anchor="mm", font=_load_font(11), fill="white")


def render_button_image(icon: str | None, label: str | None, color: tuple, size: int = 72) -> "Image.Image":
    """Render one key's face as a plain, deck-independent PIL image — used
    by the Tk UI's on-screen Stream Deck emulator (ui/streamdeck_emulator.py)
    so it draws pixel-identical buttons to the physical device without a
    connected deck. Compare _make_key_image, the hardware-native-format
    equivalent used for an actually-connected deck."""
    img = Image.new("RGB", (size, size), color)
    _paint_key_face(ImageDraw.Draw(img), (size, size), icon, label)
    return img


def recording_toggle_visual(phase: str, video_check_phase: str) -> tuple[str, str, tuple]:
    """(icon, label, color) for the session toggle (key 0) — shared by the
    physical deck (update_recording_page) and the Tk UI's on-screen
    emulator so the two always render identically. All recording is a
    session now (local or over Remote — the backend folds session open/
    close into start/stop itself), so the wording is the same everywhere:
    start the session, play the cued track, stop the session."""
    icons = {"idle": "record", "waiting": "play", "recording": "stop"}
    colors = {"idle": (0, 200, 0), "waiting": (230, 160, 0), "recording": (230, 60, 60)}
    labels = {"idle": "Start Session", "waiting": "Start Recording", "recording": "Stop Session"}
    icon, label, color = icons[phase], labels[phase], colors[phase]
    if video_check_phase == "recording":
        color = (40, 40, 40)
    return icon, label, color


def monitor_toggle_visual(mode: str) -> tuple[str, str, tuple]:
    """(icon, label, color) for the monitor-mode toggle key — see
    update_monitoring_mode()."""
    label = "Live\nMonitor" if mode == "recording" else "Production\nMonitor"
    color = LIVE_MONITOR_COLOR if mode == "recording" else PRODUCTION_MONITOR_COLOR
    return "headphones", label, color


def button_visual(btn: tuple, phase: str) -> tuple[str, str, tuple]:
    """(icon, label, color) for any other recording-layout button, dimmed
    or lit according to whether its active_state matches the current phase
    — see the _RECORDING_* table comment above for the tuple shape."""
    _idx, icon, label, _key, active_state, active_color, dim_color = btn
    color = active_color if (active_state is None or active_state == phase) else dim_color
    return icon, label, color


class StreamDeckController:
    """Manages an Elgato Stream Deck for recording session button display."""

    def __init__(self) -> None:
        self._deck = None
        self._device_key: str | None = None
        self._has_dials = False
        self._buttons: list[tuple] = []
        # Tracks which button table is currently painted: "idle"
        # (RECORDING_IDLE_BUTTONS), "identifying" (RECORDING_IDENTIFYING_
        # BUTTONS), "ready" (RECORDING_IDENTIFIED_BUTTONS), or "active"
        # (_active_recording_buttons()) — see update_recording_page.
        self._layout_state = "idle"
        self._dial_map: dict[int, tuple[str, str, str]] = dict(_SESSION_DIAL_MAP)
        self._lock = threading.Lock()
        # (icon, label, color) last painted on each key — every real key
        # draw goes through _paint_key() to keep this current, so
        # acknowledge_press() can flash a key on contact and then put its
        # actual face back. See _on_key_change.
        self._key_faces: dict[int, tuple] = {}
        # Bumped by every notify() call; a pending _revert_touchscreen only
        # fires if it's still the latest, so two overlapping messages don't
        # cut each other short.
        self._notify_gen = 0
        # Touchscreen content, cached here rather than passed fresh on
        # every redraw — update_playback_position() (RecordingDeckDriver's
        # once-a-second polling ticker, see that module) only ever changes
        # position_seconds/duration_seconds, and shouldn't need to also
        # know/re-pass whatever track_name/instrument_text last was just
        # to redraw the whole touchscreen image (a Stream Deck touchscreen
        # has no partial-region update, so every change repaints it whole
        # regardless). See _update_touchscreen.
        self._touchscreen_track_name: str | None = None
        self._touchscreen_instrument_text: str | None = None
        self._touchscreen_position: float = 0.0
        self._touchscreen_duration: float = 0.0
        # The tuner needle (see _draw_tuner_needle) — set only while
        # RecordingDeckDriver's identify_state is "identifying" and a
        # confident pitch has actually come in (see its own tuner_note/
        # tuner_cents); None the rest of the time, in which case the
        # progress bar takes this same touchscreen row instead (the two
        # never have anything to show at once in practice — there's no
        # track loaded yet during "identifying" — but showing whichever
        # one actually has something is simpler than reasoning about
        # phase here too). See update_recording_page's tuner_note/
        # tuner_cents parameters.
        self._touchscreen_tuner_note: str | None = None
        self._touchscreen_tuner_cents: float = 0.0
        # Only set when a device was actually found but failed to open/
        # configure — "no device plugged in" (the common case for anyone
        # without a Stream Deck) deliberately leaves this unset, so callers
        # can log a real failure without nagging every user who's never
        # owned one.
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._deck is not None

    def connect(self, key_callback: Callable[[str], None], device_id: str = "") -> bool:
        """Open the Stream Deck whose serial number matches device_id (from
        list_streamdecks(), stored as StudioConfig.streamdeck_id). Returns
        False immediately with no hardware probing at all if device_id is
        empty — the app never auto-connects to "whichever Stream Deck
        happens to be plugged in"; the user selects one explicitly in
        Recording Devices settings."""
        self.last_error = None
        if not device_id:
            return False
        if not _HAVE_STREAMDECK or not _HAVE_PIL:
            return False
        try:
            decks = DeviceManager().enumerate()
            _skip_hidapi_exit_crash()
            target = None
            target_probe = None
            for deck in decks:
                try:
                    deck.open()
                except Exception:
                    continue
                probe = _probe_deck(deck)
                if probe is not None and probe[0] == device_id:
                    target = deck
                    target_probe = probe
                    break
                try:
                    deck.close()
                except Exception:
                    pass
            if target is None:
                # Unlike "no device_id configured" above, the user *did*
                # select a specific Stream Deck — not finding it now is
                # worth surfacing (e.g. it's unplugged), not staying silent.
                self.last_error = f"configured Stream Deck (serial ending {device_id[-4:]}) not found among connected devices"
                return False

            self._deck = target
            try:
                self._device_key = target.id()
            except Exception:
                self._device_key = None
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id[self._device_key] = target_probe
            self._deck.reset()
            self._deck.set_brightness(70)
            self._key_callback = key_callback

            self._has_dials = getattr(self._deck, 'DIAL_COUNT', 0) > 0
            # No layout drawn yet — every caller picks one (use_recording_
            # layout()/use_inspiration_layout()) immediately after connect()
            # succeeds.
            self._buttons = []

            self._deck.set_key_callback(self._on_key_change)
            if self._has_dials:
                self._deck.set_dial_callback(self._on_dial_change)

            return True
        except Exception as e:
            self._deck = None
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id.pop(self._device_key, None)
            self._device_key = None
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def use_inspiration_layout(self, recording: bool = False) -> None:
        """Switch to inspiration mode button layout and dial map."""
        self._buttons = list(_INSPIRATION_BUTTONS)
        if recording:
            self._buttons.append(_INSPIRATION_RESTART_BUTTON)
        if not self._has_dials:
            self._buttons += _INSPIRATION_VOLUME_BUTTONS
        self._dial_map = dict(_INSPIRATION_DIAL_MAP)

    def use_recording_layout(self) -> None:
        """Enter the shared recording context — used identically by the Tk
        UI, headless `takeloom server`, and the CLI. Starts on the idle
        two-button layout (RECORDING_IDLE_BUTTONS: "Start Local" / "Start
        Streaming"); update_recording_page swaps to the full in-session
        layout — Record/Unpause/Stop toggle, Next Track (always available —
        advances to the next untaken track, discarding an in-progress take
        first if one's active), Restart (dimmed unless actually recording),
        the Live/Production monitor toggle, and volume controls (dials if
        available, else buttons) — once a session actually opens, and back
        again once it ends. Buttons whose index doesn't fit the connected
        deck are dropped rather than drawn out of range (e.g. the 6-key
        Mini)."""
        self._dial_map = dict(_SESSION_DIAL_MAP)
        self._layout_state = "idle"
        self._apply_layout(list(RECORDING_IDLE_BUTTONS), skip_indices=frozenset())

    def _active_recording_buttons(self) -> list[tuple]:
        buttons = list(RECORDING_BUTTONS)
        if self._has_dials:
            return [_RECORDING_REDRAW_DIAL if btn[0] == 10 else btn for btn in buttons]
        return buttons + RECORDING_VOLUME_BUTTONS

    def _paint_key(self, idx: int, icon: str | None, label: str | None, color: tuple) -> None:
        """The single path every real key draw goes through, so
        self._key_faces always reflects what's actually on each key —
        acknowledge_press() flashes a key the instant it's touched and
        needs its true face to put back afterwards. Callers hold
        self._lock (every current one already does)."""
        self._key_faces[idx] = (icon, label, color)
        self._deck.set_key_image(idx, self._make_key_image(icon, label, color))

    def acknowledge_press(self, key_index: int) -> None:
        """Flash `key_index` bright the instant its press registers, then
        let it fall back to its real face a fraction of a second later —
        so the deck visibly reacts even when the press kicks off something
        slow (a network call, hardware spin-up) that won't repaint the key
        itself for a while. Runs on the Stream Deck's own key-event
        thread and returns immediately; a redraw triggered by the press
        just paints over the flash early, which is fine."""
        if not self.connected:
            return
        face = self._key_faces.get(key_index)
        if face is None:
            return
        icon, label, base_color = face
        flash = tuple(min(255, c + 110) for c in base_color)
        try:
            with self._lock:
                self._deck.set_key_image(key_index, self._make_key_image(icon, label, flash))
        except Exception:
            return
        threading.Timer(0.18, self._restore_key, args=(key_index,)).start()

    def _restore_key(self, key_index: int) -> None:
        face = self._key_faces.get(key_index)
        if face is None or not self.connected:
            return
        try:
            with self._lock:
                self._deck.set_key_image(key_index, self._make_key_image(*face))
        except Exception:
            pass

    def notify(self, text: str, revert_after: float = 3.0) -> None:
        """Flash a one-line message across the touchscreen (dial decks
        only — a no-op otherwise, the caller logs the same text
        regardless), then restore the normal touchscreen after
        `revert_after` seconds. For telling the performer *why* the deck
        just went quiet — a slow query, an error — rather than leaving
        them staring at an unchanged screen."""
        if not self.connected or not self._has_dials:
            return
        self._notify_gen += 1
        gen = self._notify_gen
        try:
            with self._lock:
                img = PILHelper.create_touchscreen_image(self._deck, background="black")
                draw = ImageDraw.Draw(img)
                w, h = img.size
                draw.text((w // 2, h // 2), text, anchor="mm", font=_load_font(20), fill="white")
                self._deck.set_touchscreen_image(
                    PILHelper.to_native_touchscreen_format(self._deck, img),
                    x_pos=0, y_pos=0, width=w, height=h,
                )
        except Exception:
            return
        threading.Timer(max(0.5, revert_after), self._revert_touchscreen, args=(gen,)).start()

    def _revert_touchscreen(self, gen: int) -> None:
        if gen != self._notify_gen or not self.connected or not self._has_dials:
            return
        with self._lock:
            self._update_touchscreen()

    def _apply_layout(self, buttons: list[tuple], skip_indices: frozenset) -> None:
        """Swap in a new key layout: blank every key the deck isn't using —
        on a dial deck (e.g. the Stream Deck Plus) that's most of them, and
        left untouched they'd keep showing whatever the previous layout (or
        the device's own default/branded image) put there instead of
        looking deliberately off — then paint every used key except the
        ones the caller is about to paint itself with phase-specific
        content (skip_indices: the session toggle, and in the active
        layout, the monitor toggle)."""
        self._buttons = buttons
        if not self.connected:
            return
        key_count = getattr(self._deck, "KEY_COUNT", 0)
        self._buttons = [btn for btn in self._buttons if btn[0] < key_count]
        with self._lock:
            used_indices = {btn[0] for btn in self._buttons}
            for idx in range(key_count):
                if idx not in used_indices:
                    self._paint_key(idx, None, None, (0, 0, 0))
            for btn in self._buttons:
                idx, icon, _label, _key, _active_state, _active_color, _dim_color = btn
                if idx in skip_indices or icon is None:
                    continue
                # Freshly applying the layout with no phase known yet — treat
                # as "idle" (dimmed) until the first update_recording_page().
                icon, label, color = button_visual(btn, "idle")
                self._paint_key(idx, icon, label, color)
            if self._has_dials:
                self._update_touchscreen()

    def update_recording_page(
        self, phase: str, video_check_phase: str = "idle", track_name: str | None = None,
        instrument_text: str | None = None, identify_state: str = "idle",
        tuner_note: str | None = None, tuner_cents: float = 0.0,
    ) -> None:
        """Refresh the session toggle and dim/light Next/Restart/volume for
        the current phase, swapping the whole button layout the moment
        phase crosses the idle boundary in either direction, or (while
        still idle) `identify_state` changes. While `phase` is "idle",
        `identify_state` picks among the three idle-family layouts (see
        the RECORDING_IDLE_BUTTONS/RECORDING_IDENTIFYING_BUTTONS/RECORDING_
        IDENTIFIED_BUTTONS table comment above): "idle" (Start Local/Start
        Streaming), "identifying" (Re-identify only — auto-detect is
        listening, kicked off by RecordingDeckDriver the instant one of the
        idle buttons was pressed), or "ready" (Play + Re-identify — auto-
        detect has committed to an instrument). Any non-"idle" `phase`
        shows the full in-session layout (_active_recording_buttons())
        regardless of identify_state. The toggle is dimmed while a Video
        Check — triggered from the Tk UI or a Remote client; there's no
        Stream Deck button for it — holds the audio/camera hardware, since
        the two are mutually exclusive at the backend level. `phase` is one
        of "idle"/"waiting"/"recording"; `video_check_phase` is "idle"/
        "recording". `track_name` and `instrument_text`, on a dial deck,
        are shown on the touchscreen above the dial labels (see
        _update_touchscreen) — same touchscreen area update_inspiration()
        uses `track_name` for, just fed from RecordingDeckDriver's own idea
        of "currently loaded/playing backing track" instead of the
        inspiration filter's, and (for instrument_text) "what auto-detect
        last found, or is currently listening for" (see RecordingDeckDriver.
        detected_instrument) — shown whether idle or mid-session, so it
        stays visible confirmation of what a take is actually being filed
        under the whole time, not just before it starts. `tuner_note`/
        `tuner_cents` are the live tuner needle (see _draw_tuner_needle) —
        RecordingDeckDriver only ever passes a real tuner_note while its
        identify_state is "identifying" and TunerTracker has actually
        heard something confident; None the rest of the time, in which
        case the same touchscreen row falls back to the progress bar
        instead (see _update_touchscreen). Shared verbatim by the Tk UI,
        headless server, and CLI drivers (and mirrored on-screen — buttons
        only, no touchscreen of its own — by the Tk UI's emulator via the
        same recording_toggle_visual()/button_visual() helpers). The
        monitor-mode toggle key (index 3) is refreshed separately — see
        update_monitoring_mode() — and only exists in the active layout."""
        if not self.connected:
            return
        if track_name != self._touchscreen_track_name:
            # A different (or no) track just got loaded — the previous
            # one's progress bar position/duration no longer mean
            # anything, so let update_playback_position's next tick start
            # clean instead of briefly showing the old song's progress
            # under the new song's title.
            self._touchscreen_position = 0.0
            self._touchscreen_duration = 0.0
        self._touchscreen_track_name = track_name
        self._touchscreen_instrument_text = instrument_text
        self._touchscreen_tuner_note = tuner_note
        self._touchscreen_tuner_cents = tuner_cents
        is_idle = phase == "idle"
        if is_idle and identify_state in ("idle", "identifying", "ready"):
            desired_state = identify_state
        elif is_idle:
            desired_state = "idle"
        else:
            desired_state = "active"
        if desired_state != self._layout_state:
            self._layout_state = desired_state
            if desired_state == "idle":
                self._apply_layout(list(RECORDING_IDLE_BUTTONS), skip_indices=frozenset())
            elif desired_state == "identifying":
                self._apply_layout(list(RECORDING_IDENTIFYING_BUTTONS), skip_indices=frozenset())
            elif desired_state == "ready":
                self._apply_layout(list(RECORDING_IDENTIFIED_BUTTONS), skip_indices=frozenset())
            else:
                self._apply_layout(
                    self._active_recording_buttons(),
                    skip_indices=frozenset({0, RECORDING_MONITOR_TOGGLE_KEY_INDEX}),
                )
        if is_idle:
            if self._has_dials:
                with self._lock:
                    self._update_touchscreen()
            return
        record_icon, record_label, record_color = recording_toggle_visual(phase, video_check_phase)
        with self._lock:
            self._paint_key(0, record_icon, record_label, record_color)
            for btn in self._buttons:
                idx, icon, _label, _key, _active_state, _active_color, _dim_color = btn
                if idx in (0, RECORDING_MONITOR_TOGGLE_KEY_INDEX) or icon is None:
                    continue
                icon, label, color = button_visual(btn, phase)
                self._paint_key(idx, icon, label, color)
            if self._has_dials:
                self._update_touchscreen()

    def update_playback_position(self, position_seconds: float, duration_seconds: float) -> None:
        """Refresh just the touchscreen's progress bar — called roughly
        once a second by RecordingDeckDriver's polling ticker while a
        track is actually playing (see Backend.get_playback_position).
        Keeps whatever track_name/instrument_text update_recording_page
        last set; only position/duration change here. No-op if there's no
        dial deck connected, same guard update_recording_page itself
        applies before ever calling this indirectly."""
        if not self.connected or not self._has_dials:
            return
        self._touchscreen_position = position_seconds
        self._touchscreen_duration = duration_seconds
        with self._lock:
            self._update_touchscreen()

    def update_monitoring_mode(self, mode: str) -> None:
        """Redraw the monitor-mode toggle key (index 3) to reflect the
        current Production/Recording monitoring mode (see
        Backend.get_monitoring_mode/set_monitoring_mode) — "recording" is
        labeled "Live Monitor" here, since on the deck it reads as "the
        zero-latency direct-hardware path used while actually laying down a
        take" rather than the backend's internal name for it. Called on
        connect (with whatever the current mode already is) and again
        whenever it changes, from any client — see RecordingDeckDriver."""
        if not self.connected:
            return
        if not any(btn[0] == RECORDING_MONITOR_TOGGLE_KEY_INDEX for btn in self._buttons):
            return
        icon, label, color = monitor_toggle_visual(mode)
        with self._lock:
            self._paint_key(RECORDING_MONITOR_TOGGLE_KEY_INDEX, icon, label, color)

    def _on_key_change(self, deck, key_index: int, pressed: bool) -> None:
        if not pressed:
            return
        for idx, _icon, _label, key_char, *_ in self._buttons:
            if idx == key_index:
                # Flash the key right here on the HID event thread, before
                # the callback runs — so the press is acknowledged
                # instantly even if what it triggers takes a moment.
                self.acknowledge_press(key_index)
                self._key_callback(key_char)
                return

    def _on_dial_change(self, deck, dial_index: int, event, value) -> None:
        if not _HAVE_STREAMDECK:
            return
        if event != DialEventType.TURN:
            return
        mapping = self._dial_map.get(dial_index)
        if mapping is None:
            return
        ccw_key, cw_key, _ = mapping
        key = cw_key if value > 0 else ccw_key
        for _ in range(abs(value)):
            self._key_callback(key)

    def update_inspiration(self, is_playing: bool, track_name: str | None = None) -> None:
        """Refresh buttons and touchscreen for the current inspiration mode state."""
        if not self.connected:
            return
        self._touchscreen_track_name = track_name
        self._touchscreen_instrument_text = None  # no such concept while just browsing inspiration
        self._touchscreen_position = 0.0
        self._touchscreen_duration = 0.0
        with self._lock:
            icon = "pause" if is_playing else "play"
            label = "Pause" if is_playing else "Play"
            color = (200, 130, 0) if is_playing else (0, 180, 0)
            self._paint_key(0, icon, label, color)
            for btn in self._buttons:
                idx, icon, label, _key, _state, active_color, _dim = btn
                if idx == 0 or icon is None:
                    continue
                self._paint_key(idx, icon, label, active_color)
            if self._has_dials:
                self._update_touchscreen()

    def disconnect(self) -> None:
        if self._deck:
            if self._device_key is not None:
                with _open_by_id_lock:
                    _open_by_id.pop(self._device_key, None)
            try:
                self._deck.reset()
                self._deck.close()
            except Exception:
                pass
            self._deck = None
            self._device_key = None
            self._key_faces.clear()

    def _make_key_image(self, icon: str | None, label: str | None, color: tuple) -> bytes:
        img = PILHelper.create_image(self._deck, background=color)
        _paint_key_face(ImageDraw.Draw(img), img.size, icon, label)
        return PILHelper.to_native_format(self._deck, img)

    def _update_touchscreen(self) -> None:
        """Redraws the whole touchscreen from cached self._touchscreen_*
        state (see update_recording_page/update_playback_position/
        update_inspiration, the only three setters) — a Stream Deck
        touchscreen has no partial-region update, so every change repaints
        it whole regardless of which single field actually changed.

        Fixed row positions regardless of which pieces are actually
        present (a row with nothing to show is just left blank) — simpler
        and visually stable than a layout that reflows depending on what's
        currently known, at the cost of some blank space while idle:

        - song title (track_name): the most prominent thing on screen —
          it's what a performer is actually here to look at while playing.
        - instrument label: a small colored badge (see instrument_colors.py
          — same colors as the Sessions/Completed Takes/Studio Setup
          badges) rather than plain text, and deliberately smaller/less
          prominent than the title now — see RecordingDeckDriver.
          detected_instrument for what it's showing and why it's still
          worth a glance, just not the star of the screen the way it was
          when this touchscreen first got auto-detect wired into it.
        - playback progress bar / tuner needle: whichever one currently
          has something to show, in the same row — the progress bar for
          elapsed/remaining time on whatever's currently loaded (blank
          when duration_seconds is 0, e.g. before a take starts playing),
          or the tuner needle (see _draw_tuner_needle) while Recording
          DeckDriver's identify_state is "identifying" and a confident
          pitch has come in. The two never actually have something to
          show at the same time in practice (no track is loaded yet
          during "identifying"), so tuner_note simply takes priority
          when set rather than the two needing to coordinate on whose
          turn it is.
        - dial labels: shown always *except* while the tuner needle is —
          the volume dials they'd be labeling do nothing until a session
          is actually open (there's nothing to adjust yet), so during
          tuning that row is just clutter behind/below the needle; see
          _draw_tuner_needle.
        """
        try:
            img = PILHelper.create_touchscreen_image(self._deck, background="black")
            draw = ImageDraw.Draw(img)
            w, h = img.size  # 800×100
            section_w = w // 4

            if self._touchscreen_track_name:
                draw.text((w // 2, int(h * 0.20)), self._touchscreen_track_name, anchor="mm",
                          font=_load_font(22), fill="white")
            if self._touchscreen_instrument_text:
                _draw_instrument_badge(draw, w, int(h * 0.44), self._touchscreen_instrument_text)
            tuning = self._touchscreen_tuner_note is not None
            if tuning:
                _draw_tuner_needle(draw, w, int(h * 0.66), self._touchscreen_tuner_note, self._touchscreen_tuner_cents)
            elif self._touchscreen_duration > 0:
                _draw_progress_bar(draw, w, int(h * 0.66), self._touchscreen_position, self._touchscreen_duration)

            if not tuning:
                label_y = int(h * 0.88)
                for dial_idx, (_ccw, _cw, label) in self._dial_map.items():
                    x = section_w * dial_idx + section_w // 2
                    draw.text((x, label_y), label, anchor="mm",
                              font=_load_font(14), fill=(160, 160, 160))
            img_bytes = PILHelper.to_native_touchscreen_format(self._deck, img)
            self._deck.set_touchscreen_image(img_bytes, x_pos=0, y_pos=0, width=w, height=h)
        except Exception:
            import traceback
            traceback.print_exc()
