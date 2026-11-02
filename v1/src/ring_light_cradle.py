"""
Ring-light cradle: the v1 cradle on a riser that screws onto a ring light's 1/4"-20
phone screw (the screw on the block inside the bottom of the ring), holding the phone
just behind the ring with its camera roughly at the ring's centre.

Orientation: the phone's camera is at the TOP of the phone, on the subject's right
(as in the reference photo). That needs the mirror-image ("left") cradle and keeps the
riser short. The part is built in the normal cradle frame and mirrored at the end.

Cradle frame: x along the cradle (closed end at x = -WALL, open end at x = CRADLE_LEN),
y: screen-side lips at y < 0, front (subject) plate at y = T..T+WALL, z up (floor top = 0).

    python3 ring_light_cradle.py      # writes STLs to ./out/
"""
import numpy as np
from manifold3d import Manifold, CrossSection
import wall_phone_mount as w

# ---------------- measure these on your ring light ----------------
RING_ID = 203.2          # ring inner diameter (8")
SCREW_TO_CENTER = 101.6  # block top (where the screw comes out) -> ring centre. MEASURED: 8" from the block top to the inner top of the ring, minus the 4" radius
SCREW_TO_BACK = 10.5     # screw axis -> back face of the ring (the side you stand on). MEASURED
PAD_X = (-32.0, 12.0)    # pad extent along the block, relative to the screw; the far end is a semicircle centred on the screw (radius PAD_D/2)
BLOCK_HALF_W = 34.5       # half the width of the light's mounting block top (photo estimate)
PAD_D = 24.0             # pad depth front-to-back on the block top
SCREW_REACH = 5.3        # how far the light's screw sticks up (measured: just under 4 x 1.39 mm washers)
# -------------------------------------------------------------------
CLR_BACK = 10.0          # gap between the ring's back face and the cradle's front plate (about a centimetre is fine)
# lens centres along the cradle (bump edge sits ~1.5 mm past the open end, + measured bump-edge-to-lens):
#   13 mini 112+1.5+17.0 = 130.5, 16 Pro Max 112+1.5+21.3 = 134.8, S20 FE 112+1.5+24.7 = 138.2  -> centre of the range
LENS_X = {"iPhone 13 mini": 130.5, "iPhone 16 Pro Max": 134.8, "Galaxy S20 FE": 138.2}
CAM_X, CAM_Z = 134.5, 52.0   # ring centre in the cradle frame (CAM_Z: camera near the top edge, estimated)
SLAB_X = (30.0, w.CRADLE_LEN)  # riser meets the cradle floor over this x range (cradle frame)
T_NUT_AF, T_NUT_H = 11.2, 5.48
T_NUT_CAV = T_NUT_H + 0.4
T_FLOOR = 0.6
T_HOLE_D = 6.7
DENS = 1.27 * 0.6 / 1000

W_ = w.WALL
XS = CAM_X                                   # screw under the ring centre
YS = w.T + W_ + CLR_BACK + SCREW_TO_BACK     # screw axis, in front of the cradle's front plate
ZS = CAM_Z - SCREW_TO_CENTER                 # block top surface (pad bottom)


def _snap_up(z):
    return float(np.ceil(round(z / w.LAYER_H, 6)) * w.LAYER_H)


def roof_x():
    return _snap_up(XS + T_NUT_AF / np.sqrt(3) + 0.15 + W_) - W_


def pause_height():
    return roof_x() + W_


ROUND = 5.0              # corner radius used by the cradle's shoulder trim

# ---- the arm: one lofted shape from the screw pad up to a footprint on the cradle's underside ----
ARM_TOP = (66.0, 9.5)    # centre of the arm's axis where it reaches the cradle's underside (x, y)
ARM_RX, ARM_RY = 9.5, 9.0   # half-widths of the body's horizontal section (about 12.5 mm thick at its thinnest)
FOOT = dict(xr=3.0, y=3.0, hold=6.0, blend=30.0)        # pad: extra width toward +x and +-y, held over the nut, then blended away
FLARE = dict(xl=18.0, xr=17.0, yl=3.5, yr=3.5, h=14.0)  # top: flares out to the footprint, tangent to the cradle's underside
LOFT_N = 128             # points around each section
LOFT_STEPS = 120         # sections from the pad to the cradle


def _arm_section(h):
    """(x_left, x_right, y_left, y_right) of the arm's horizontal section at height h above
    the pad's bottom. The axis is a straight line from the screw up to ARM_TOP; it rises less
    than 45 deg off print-vertical (print up = +x), so the body never overhangs."""
    H = -W_ - ZS
    k = (XS - ARM_TOP[0]) / H
    m = (YS - ARM_TOP[1]) / H
    cx, cy = XS - k * h, YS - m * h
    # pad: wider (except toward -x, which must keep rising at the body's slope) up to above the nut
    s = np.clip((h - FOOT["hold"]) / FOOT["blend"], 0, 1)
    foot = 0.5 * (1 + np.cos(np.pi * s))                 # 1 -> 0, smooth at both ends
    # flare at the top: a quarter-ellipse profile, tangent to the cradle's underside
    q = np.clip((h - (H - FLARE["h"])) / FLARE["h"], 0, 1)
    fl = 1 - np.sqrt(np.maximum(0.0, 1 - q * q))
    hh = h   # cancels the axis drift, so the pad's far sides stand straight over the nut (and blend smoothly after)
    return (cx - ARM_RX - FLARE["xl"] * fl,
            cx + ARM_RX + (FOOT["xr"] + k * hh) * foot + FLARE["xr"] * fl,
            cy - ARM_RY - FOOT["y"] * foot - FLARE["yl"] * fl,
            cy + ARM_RY + (FOOT["y"] + m * hh) * foot + FLARE["yr"] * fl)


def _riser():
    """The arm as a single lofted surface (no hulls, no seams): elliptical horizontal sections
    from a flat pad on the light's block to a footprint on the cradle's underside."""
    from manifold3d import Mesh
    H = -W_ - ZS
    # sections bunch up toward the top so the tangent flare stays smooth
    u = np.linspace(0, 1, LOFT_STEPS + 1)
    hs = H * np.sin(u * np.pi / 2) ** 0.7
    hs = np.unique(np.concatenate([hs, np.linspace(0, FOOT["hold"] + FOOT["blend"], 30)]))
    th = np.linspace(0, 2 * np.pi, LOFT_N, endpoint=False)
    rings = []
    for h in hs:
        xl, xr, yl, yr = _arm_section(h)
        rings.append(np.c_[(xl + xr) / 2 + (xr - xl) / 2 * np.cos(th), (yl + yr) / 2 + (yr - yl) / 2 * np.sin(th),
                           np.full(LOFT_N, ZS + h)])
    rings.append(rings[-1] + [0, 0, W_ - 1.0])           # straight up into the cradle floor (to z = -1)
    v = np.vstack(rings + [[rings[0][:, 0].mean(), rings[0][:, 1].mean(), ZS], [rings[-1][:, 0].mean(), rings[-1][:, 1].mean(), -1.0]])
    nR, N = len(rings), LOFT_N
    f = []
    for i in range(nR - 1):
        for j in range(N):
            a, b_, c_, d_ = i * N + j, i * N + (j + 1) % N, (i + 1) * N + (j + 1) % N, (i + 1) * N + j
            f += [(a, b_, c_), (a, c_, d_)]
    bot, top = nR * N, nR * N + 1
    f += [(bot, (j + 1) % N, j) for j in range(N)]
    f += [(top, (nR - 1) * N + j, (nR - 1) * N + (j + 1) % N) for j in range(N)]
    return Manifold(Mesh(vert_properties=np.ascontiguousarray(v, np.float32), tri_verts=np.array(f, np.uint32)))


def _shoulder_geometry():
    r = ROUND
    pr_ = PAD_D / 2
    end_c = (XS + PAD_X[1] - pr_, YS)
    return (SLAB_X[1] - r, -W_ + r, r), (end_c[0], end_c[1], pr_)


LIP_TAPER = (76.0, 92.0)  # x range where the bottom lip ramps from full height down to the floor
LIP_TRIM_TOP = w.LIP_BOT + 0.01   # the shoulder arc also trims the cradle's bottom lip up to its top
SHOULDER_R = 55.0        # plan-view radius where the arm's diagonal meets the screen-side face


def _shoulder_trim(post_corner, pad_circle, z_top=-W_):
    """Prism (all heights below the cradle floor) that rounds the plan-view corner between
    the screen-side face (y = -WALL) and the diagonal from the post to the pad."""
    (ax, ay_, ar), (bx, by, br) = post_corner, pad_circle
    # outer common tangent of the two plan circles (the diagonal edge), on the +x / -y side
    d = np.array([bx - ax, by - ay_]); L = np.linalg.norm(d); u = d / L
    nrm = np.array([u[1], -u[0]])                            # points to +x/-y
    cos_t = (ar - br) / L
    sin_t = np.sqrt(1 - cos_t ** 2)
    n_t = cos_t * u + sin_t * nrm                            # unit normal of the tangent line
    pA = np.array([ax, ay_]) + ar * n_t                      # tangent point on the post corner
    c0 = n_t @ pA                                            # line: n_t . p = c0
    # circle of radius R tangent to y = -WALL (from above) and to the tangent line (from inside)
    R = SHOULDER_R
    yc = -W_ + R
    xc = (c0 - R - n_t[1] * yc) / n_t[0]
    P1 = np.array([xc, -W_])                                 # tangent point on the screen-side face
    P2 = np.array([xc, yc]) + R * n_t                        # tangent point on the diagonal
    Q = np.array([(c0 - n_t[1] * (-W_)) / n_t[0], -W_])      # sharp corner being removed
    out = 30.0
    tri = CrossSection([[tuple(P1), tuple(P1 + [0, -out]), tuple(Q + [out, -out]), tuple(P2 + n_t * out), tuple(P2)]])
    cut = tri - w.circ(R, xc, yc, n=128)
    return cut.extrude(200 + z_top + W_).translate([0, 0, -200 - W_])


def arm():
    return _riser()


def ring_light_cradle(captive=True):
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    # one continuous curve at the open end's screen-side corner: the same arc trims the arm
    # and the cradle's floor and bottom lip above it
    trim = _shoulder_trim(*_shoulder_geometry(), z_top=LIP_TRIM_TOP)
    m = cr - trim
    m = m + arm()
    # taper the bottom lip's top down to the floor as it reaches that arc, so it fades out
    # instead of ending in a step (the ramp faces up when printed on end)
    xa, xb = LIP_TAPER
    ramp = CrossSection([[(xa, w.LIP_BOT + 0.01), (xb, -0.01), (300, -0.01), (300, 50), (xa, 50)]])   # in x-z
    m = m - ramp.extrude(W_ + 1.01).transform(np.array([[1, 0, 0, 0], [0, 0, -1, 0.01], [0, 1, 0, 0]]))  # y: lip only
    # 1/4"-20 nut: corner along x (print up), on a 0.6 mm floor; blind hole for the light's screw
    R = T_NUT_AF / np.sqrt(3)
    z0 = ZS + T_FLOOR
    hole = w.teardrop(T_HOLE_D / 2, T_FLOOR + T_NUT_CAV + 3 + 1).rotate([0, 0, -90]).translate([XS, YS, ZS - 1])
    m = m - hole
    hexcs = CrossSection.circle(R, 6)
    if captive:
        slot = CrossSection.batch_hull([hexcs.translate([XS, YS]), w.rect2(XS, roof_x(), YS - T_NUT_AF / 2, YS + T_NUT_AF / 2)])
    else:   # slides in from the pad's front face (toward the subject)
        slot = CrossSection.batch_hull([hexcs.rotate(30).translate([XS, YS]),
                                        w.rect2(XS - T_NUT_AF / 2, XS + T_NUT_AF / 2, YS, YS + PAD_D / 2 + 1)])
    m = m - slot.extrude(T_NUT_CAV).translate([0, 0, z0])
    m = m.simplify(1e-4)        # drop zero-area slivers where the arm's faces meet the cradle's
    pr = m.translate([W_, 0, 0]).rotate([0, -90, 0])
    pr = pr.translate([0, 0, -pr.bounding_box()[2]])
    # mirror for the camera-at-top orientation (a mirror about a vertical plane keeps it printable)
    return m.mirror([1, 0, 0]), pr.mirror([0, 1, 0])


if __name__ == "__main__":
    import os
    os.makedirs("out", exist_ok=True)
    for name, cap in (("ring_light_cradle_captive", True), ("ring_light_cradle", False)):
        _, pr = ring_light_cradle(cap)
        t = w.to_trimesh(w.on_bed(pr)); t.export(f"out/{name}.stl")
        print(f"{name:26s} watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 bbox={np.round(t.extents, 1)}")
    print(f"riser height {-W_ - ZS:.0f} mm; captive: pause before the layer at {pause_height():.1f} mm")
    print(f"thread engaged with a {SCREW_REACH} mm screw: {SCREW_REACH - T_FLOOR:.1f} mm")
