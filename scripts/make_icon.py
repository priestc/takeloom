"""Generate a janky, hand-drawn-looking takeloom icon.

Concept: a crooked wooden loom frame with vertical warp threads, and a red
audio waveform being "woven" through them like weft yarn. Everything is drawn
with jittered strokes, uneven widths, and coloring that doesn't quite stay in
the lines — deliberately homemade.

Also draws the Takeloom Camera app's icon (camera_app/): the same loom, with
a hand-drawn camera inside the frame in place of the waveform, written out as
the iOS app icon and the Android adaptive-icon foreground.
"""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # canvas size
rng = random.Random(7)


def wobble_line(draw, p0, p1, color, width, jitter=6, segs=14, passes=1):
    """Draw a line as jittered segments, optionally retraced like a marker."""
    for p in range(passes):
        ox = rng.uniform(-3, 3) if p else 0
        oy = rng.uniform(-3, 3) if p else 0
        pts = []
        for i in range(segs + 1):
            t = i / segs
            x = p0[0] + (p1[0] - p0[0]) * t
            y = p0[1] + (p1[1] - p0[1]) * t
            if 0 < i < segs:
                x += rng.uniform(-jitter, jitter)
                y += rng.uniform(-jitter, jitter)
            pts.append((x + ox, y + oy))
        w = max(2, int(width + rng.uniform(-width * 0.25, width * 0.25)))
        draw.line(pts, fill=color, width=w, joint="curve")
        # round the ends a bit
        for pt in (pts[0], pts[-1]):
            r = w / 2
            draw.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=color)


def wobble_poly(draw, pts, color, width, jitter=6, segs=8, close=True, passes=1):
    n = len(pts)
    for i in range(n if close else n - 1):
        wobble_line(draw, pts[i], pts[(i + 1) % n], color, width, jitter, segs, passes)


def blob(draw, cx, cy, rx, ry, color, jitter=14, n=28):
    """An uneven filled ellipse-ish blob (careless coloring)."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r_x = rx + rng.uniform(-jitter, jitter)
        r_y = ry + rng.uniform(-jitter, jitter)
        pts.append((cx + r_x * math.cos(a), cy + r_y * math.sin(a)))
    draw.polygon(pts, fill=color)


# ---------------------------------------------------------------- palette
PAPER = (247, 240, 224)        # cream paper
PAPER_EDGE = (60, 48, 38)      # dark outline
WOOD = (176, 124, 74)          # frame fill
WOOD_DARK = (94, 62, 34)       # frame outline
THREAD = (58, 122, 128)        # teal warp threads
WAVE = (206, 66, 48)           # red waveform weft
WAVE_2 = (232, 150, 40)        # orange second pass

def rot(p, cx, cy, ang):
    s, c = math.sin(ang), math.cos(ang)
    x, y = p[0] - cx, p[1] - cy
    return (cx + x * c - y * s, cy + x * s + y * c)


CAM_BODY = (52, 50, 58)        # camera body, charcoal
CAM_LENS = (36, 42, 52)        # lens glass


def draw_camera(d, inner_corners):
    """A chunky hand-drawn camcorder sitting inside the loom frame, drawn
    over the warp threads like it's been woven in."""
    # Nudged left of center: the lens hood sticks out the right side.
    cx = sum(p[0] for p in inner_corners) / 4 - 40
    cy = sum(p[1] for p in inner_corners) / 4
    ang = math.radians(-3.5)  # same tilt as the frame

    def place(x, y):
        return rot((cx + x, cy + y), cx, cy, ang)

    # body
    bw, bh = 170, 112
    body = [place(-bw, -bh + 10), place(bw - 20, -bh), place(bw - 14, bh), place(-bw + 6, bh - 8)]
    d.polygon([(x + rng.uniform(-7, 7), y + rng.uniform(-7, 7)) for x, y in body], fill=CAM_BODY)
    # the viewfinder hump on top, and the lens hood sticking out the right side
    hump = [place(-110, -bh - 42), place(-20, -bh - 46), place(-12, -bh + 4), place(-118, -bh + 8)]
    d.polygon([(x + rng.uniform(-5, 5), y + rng.uniform(-5, 5)) for x, y in hump], fill=CAM_BODY)
    hood = [place(bw - 26, -62), place(bw + 72, -96), place(bw + 76, 96), place(bw - 22, 66)]
    d.polygon([(x + rng.uniform(-5, 5), y + rng.uniform(-5, 5)) for x, y in hood], fill=CAM_BODY)
    wobble_poly(d, body, PAPER_EDGE, 13, jitter=5, segs=8, passes=2)
    wobble_poly(d, hump, PAPER_EDGE, 11, jitter=4, segs=6)
    wobble_poly(d, hood, PAPER_EDGE, 11, jitter=4, segs=6)

    # the lens: teal ring (a warp-thread color), dark glass, a lazy highlight
    lx, ly = place(20, 4)
    blob(d, lx, ly, 92, 92, THREAD, jitter=6, n=36)
    blob(d, lx, ly, 64, 64, CAM_LENS, jitter=5, n=30)
    blob(d, lx - 22, ly - 24, 17, 13, PAPER, jitter=3, n=14)
    ring = []
    for i in range(61):
        a = 2 * math.pi * i / 60
        r = 94 + rng.uniform(-3, 3)
        ring.append((lx + r * math.cos(a), ly + r * math.sin(a)))
    d.line(ring, fill=PAPER_EDGE, width=11, joint="curve")

    # red record light, orange-ish halo colored outside the lines
    rx, ry = place(-128, -66)
    blob(d, rx, ry, 26, 24, WAVE_2, jitter=4, n=16)
    blob(d, rx + 2, ry + 1, 18, 17, WAVE, jitter=3, n=16)


def draw_icon(camera: bool = False) -> Image.Image:
    rng.seed(7)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- sticker-ish paper background blob, uneven edge, thick crooked outline
    blob(d, S / 2, S / 2, S * 0.455, S * 0.455, PAPER, jitter=10, n=64)
    # hand-drawn circle: slow low-frequency drift + fine tremor, drawn as one path
    edge_pts = []
    n = 160
    drift_phase = rng.uniform(0, 6)
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        r = S * 0.455
        r += 10 * math.sin(a * 3 + drift_phase)   # slow lopsidedness
        r += rng.uniform(-3.5, 3.5)               # fine tremor
        edge_pts.append((S / 2 + r * math.cos(a), S / 2 + r * math.sin(a)))
    d.line(edge_pts, fill=PAPER_EDGE, width=15, joint="curve")
    # a lazy second retrace over part of the circle
    d.line([(x + 5, y + 4) for x, y in edge_pts[10:70]], fill=PAPER_EDGE, width=9, joint="curve")

    # --- loom frame: slightly rotated crooked rectangle

    ang = math.radians(-3.5)
    fx0, fy0, fx1, fy1 = 195, 190, 829, 754
    corners = [(fx0, fy0), (fx1, fy0 + 18), (fx1 - 10, fy1), (fx0 + 6, fy1 - 12)]
    corners = [rot(p, S / 2, S / 2, ang) for p in corners]

    # fill the frame bars as offset quads (coloring outside the lines a little)
    bar = 46
    def offset_quad(a, b, w):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        nx, ny = -dy / L * w, dx / L * w
        return [a, b, (b[0] + nx, b[1] + ny), (a[0] + nx, a[1] + ny)]

    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        q = offset_quad(a, b, bar)
        q = [(x + rng.uniform(-6, 6), y + rng.uniform(-6, 6)) for x, y in q]
        d.polygon(q, fill=WOOD)

    # frame outlines, retraced twice like a shaky marker
    inner = [(x + bar * (1 if x < S / 2 else -1) * 0.0, y) for x, y in corners]
    wobble_poly(d, corners, WOOD_DARK, 16, jitter=5, segs=10, passes=2)
    inner_corners = []
    cx = sum(p[0] for p in corners) / 4
    cy = sum(p[1] for p in corners) / 4
    for x, y in corners:
        t = bar * 1.35 / math.hypot(x - cx, y - cy)
        inner_corners.append((x + (cx - x) * t * 2.2, y + (cy - y) * t * 2.2))
    wobble_poly(d, inner_corners, WOOD_DARK, 11, jitter=5, segs=8)

    # --- warp threads: vertical teal threads inside the frame
    top_a, top_b = inner_corners[0], inner_corners[1]
    bot_a, bot_b = inner_corners[3], inner_corners[2]
    n_threads = 6
    thread_xs = []
    for i in range(n_threads):
        t = (i + 0.5) / n_threads
        p_top = (top_a[0] + (top_b[0] - top_a[0]) * t, top_a[1] + (top_b[1] - top_a[1]) * t)
        p_bot = (bot_a[0] + (bot_b[0] - bot_a[0]) * t, bot_a[1] + (bot_b[1] - bot_a[1]) * t)
        wobble_line(d, p_top, p_bot, THREAD, 9, jitter=7, segs=16)
        thread_xs.append((p_top, p_bot))

    if camera:
        draw_camera(d, inner_corners)
    else:
        # --- the weft: a red audio waveform woven horizontally through the middle
        mid_y = (inner_corners[0][1] + inner_corners[2][1]) / 2
        x_left = min(inner_corners[0][0], inner_corners[3][0]) + 8
        x_right = max(inner_corners[1][0], inner_corners[2][0]) - 8

        # waveform amplitude envelope like a real take: quiet-loud-quiet
        def wave_pts(y_base, amp_scale, phase):
            pts = []
            n = 90
            for i in range(n + 1):
                t = i / n
                x = x_left + (x_right - x_left) * t
                env = math.sin(t * math.pi) ** 0.7
                y = y_base + math.sin(t * 34 + phase) * 52 * env * amp_scale
                y += rng.uniform(-4, 4)
                pts.append((x, y))
            return pts

        d.line(wave_pts(mid_y, 1.0, 0.0), fill=WAVE, width=13, joint="curve")
        d.line(wave_pts(mid_y + 8, 0.8, 1.3), fill=WAVE_2, width=8, joint="curve")

        # weave illusion: redraw short bits of every OTHER thread over the wave
        for i, (p_top, p_bot) in enumerate(thread_xs):
            if i % 2 == 0:
                continue
            t0, t1 = 0.40, 0.60
            seg_a = (p_top[0] + (p_bot[0] - p_top[0]) * t0, p_top[1] + (p_bot[1] - p_top[1]) * t0)
            seg_b = (p_top[0] + (p_bot[0] - p_top[0]) * t1, p_top[1] + (p_bot[1] - p_top[1]) * t1)
            wobble_line(d, seg_a, seg_b, THREAD, 10, jitter=4, segs=4)

    # --- fringe: warp thread ends hanging below the bottom bar, uneven lengths
    bot_bar_a, bot_bar_b = corners[3], corners[2]
    n_fringe = 9
    for i in range(n_fringe):
        t = (i + 0.5) / n_fringe
        x0 = bot_bar_a[0] + (bot_bar_b[0] - bot_bar_a[0]) * t + rng.uniform(-5, 5)
        y0 = bot_bar_a[1] + (bot_bar_b[1] - bot_bar_a[1]) * t
        length = rng.uniform(40, 95)
        sway = rng.uniform(-18, 18)
        wobble_line(d, (x0, y0), (x0 + sway, y0 + length), THREAD, 8, jitter=5, segs=6)

    # --- a couple of "nails" in the frame corners (dots, off-center)
    for x, y in corners:
        r = rng.uniform(7, 11)
        ox, oy = rng.uniform(-6, 6), rng.uniform(-6, 6)
        d.ellipse([x + ox - r, y + oy - r, x + ox + r, y + oy + r], fill=PAPER_EDGE)

    return img


def write_icons() -> None:
    root = Path(__file__).resolve().parent.parent

    out = root / "takeloom" / "data" / "icon.png"
    draw_icon().resize((512, 512), Image.LANCZOS).save(out)
    print(f"wrote {out}")

    sticker = draw_icon(camera=True)

    # iOS: one opaque 1024 square (the system rounds the corners itself),
    # the sticker sitting on a slightly darker paper.
    ios = Image.new("RGB", (S, S), IOS_BACKGROUND)
    ios.paste(sticker, (0, 0), sticker)
    out = root / "camera_app" / "ios" / "TakeloomCamera" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    ios.save(out)
    print(f"wrote {out}")

    # Android adaptive icon foreground: 108dp canvas, and only the middle
    # 66dp circle is guaranteed visible under every launcher's mask — so the
    # sticker gets scaled into that, on a transparent canvas (the background
    # color layer is res/values/ic_launcher_background.xml).
    fg = Image.new("RGBA", (432, 432), (0, 0, 0, 0))
    inner = round(432 * 66 / 108)
    small = sticker.resize((inner, inner), Image.LANCZOS)
    fg.paste(small, ((432 - inner) // 2, (432 - inner) // 2), small)
    out = root / "camera_app" / "android" / "app" / "src" / "main" / "res" / "drawable-nodpi" / "ic_launcher_foreground.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fg.save(out)
    print(f"wrote {out}")


IOS_BACKGROUND = (226, 212, 184)

if __name__ == "__main__":
    write_icons()
