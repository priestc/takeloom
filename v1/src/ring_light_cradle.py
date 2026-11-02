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
PAD_X = (-32.0, 22.0)    # pad extent along the block, relative to the screw. The block top is ~70 mm wide (from the photo); keep within +-34
BLOCK_HALF_W = 34.5       # half the width of the light's mounting block top (photo estimate)
PAD_D = 24.0             # pad depth front-to-back on the block top
SCREW_REACH = 5.3        # how far the light's screw sticks up (measured: just under 4 x 1.39 mm washers)
# -------------------------------------------------------------------
CLR_BACK = 10.0          # gap between the ring's back face and the cradle's front plate (about a centimetre is fine)
# lens centres along the cradle (bump edge sits ~1.5 mm past the open end, + measured bump-edge-to-lens):
#   13 mini 112+1.5+17.0 = 130.5, 16 Pro Max 112+1.5+21.3 = 134.8, S20 FE 112+1.5+24.7 = 138.2  -> centre of the range
LENS_X = {"iPhone 13 mini": 130.5, "iPhone 16 Pro Max": 134.8, "Galaxy S20 FE": 138.2}
CAM_X, CAM_Z = 134.5, 52.0   # ring centre in the cradle frame (CAM_Z: camera near the top edge, estimated)
PAD_T = 12.0             # pad thickness
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


ROUND = 5.0              # edge radius on the arm


def _balls(points, r):
    return Manifold.batch_hull([Manifold.sphere(r, 32).translate(list(p)) for p in points])


def _rbox(x0, x1, y0, y1, z0, z1, r=ROUND):
    """Box with every edge rounded to radius r (outer size unchanged)."""
    return _balls([(x, y, z) for x in (x0 + r, x1 - r) for y in (y0 + r, y1 - r) for z in (z0 + r, z1 - r)], r)


SLAB_T = 12.0            # plate under the cradle floor (below the floor's underside)
SWEEP_STEPS = 10         # slices along the arm's curved underside


def _riser():
    """The arm. Its underside sweeps in a smooth curve (quadratic Bezier) from horizontal
    under the cradle down to the post behind the ring, reaching exactly 45 deg at the
    bottom, so nothing is steeper than 45 deg when printed standing on the closed end.
    A pad then reaches forward over the light's block. All edges are rounded (ROUND);
    the pad's bottom is flat and the arm stays flush with the cradle's outer faces."""
    r = ROUND
    y_back = YS - SCREW_TO_BACK - 1.0               # stays 1 mm behind the ring's back face
    x0 = XS + PAD_X[0]
    y0, y1 = YS - PAD_D / 2, YS + PAD_D / 2
    x_end = SLAB_X[1]
    # --- curved underside: Bezier from T (slab bottom, horizontal) to B (post bottom, 45 deg)
    zT = -W_ - SLAB_T
    xT = SLAB_X[0] + SLAB_T - W_ + 3.0              # where the 45 deg tip meets the slab bottom
    B = np.array([x0, ZS])
    C = np.array([B[0] - (zT - ZS), zT])            # on the 45 deg line through B, at the slab-bottom height
    T = np.array([xT, zT])
    slices = []
    for t in np.linspace(0, 1, SWEEP_STEPS + 1):
        px, pz = (1 - t) ** 2 * T + 2 * (1 - t) * t * C + t ** 2 * B
        ymax = (w.T + W_) + (y_back - (w.T + W_)) * t ** 2       # front edge eases out to the post
        slices.append(_rbox(px, x_end, -W_, ymax, pz, 0.0, r))
    # 45 deg tip at the closed-end side, tangent to the plane x + z = SLAB_X0 - WALL
    xt = lambda zc: SLAB_X[0] - W_ + r * np.sqrt(2) - zc
    tip = _balls([(xt(zc), y, zc) for y in (-W_ + r, w.T + W_ - r) for zc in (zT + r, -r)] +
                 [(x_end - r, y, zc) for y in (-W_ + r, w.T + W_ - r) for zc in (zT + r, -r)], r)
    chain = [tip] + slices
    arm = Manifold()
    for a_, b_ in zip(chain[:-1], chain[1:]):
        arm = arm + Manifold.batch_hull([a_, b_])
    post = slices[-1]
    # pad over the block: closed-end side angled 45 deg in plan, lined up with the post's corner
    xc = lambda yc: (x0 + r) + (yc - (y_back - r))
    pad = _balls([(x, y, z) for y in (y0 + r, y1 - r) for x in (xc(y), XS + PAD_X[1] - r)
                  for z in (ZS + r, ZS + PAD_T - r)], r)
    arm = arm + Manifold.batch_hull([post, pad])
    # collar: flush with the cradle's lip and front faces right under the floor, tip at 45 deg
    collar = Manifold.batch_hull([w.box(SLAB_X[0] + r, x_end, -W_, w.T + W_, -W_ - r, -W_ + 0.01),
                                  w.box(SLAB_X[0], SLAB_X[0] + 0.01, -W_, w.T + W_, -W_ - 0.01, -W_ + 0.01)])
    floor_zone = w.box(-50, 250, -50, 100, -W_, 1.0)                # nothing pokes into the phone channel
    return (arm - floor_zone) + collar


def ring_light_cradle(captive=True):
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    riser = _riser()
    m = cr + riser
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
