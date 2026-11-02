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
SCREW_TO_CENTER = 90.0   # block top surface (where the screw comes out) -> ring centre. ESTIMATED from the photo
SCREW_TO_BACK = 15.0     # screw axis -> back face of the ring / block (the side you stand on). ESTIMATED
PAD_X = (-36.0, 22.0)    # pad extent along the ring's bottom, relative to the screw (asymmetric: keeps the riser's underside within 45 deg)
PAD_D = 24.0             # pad depth front-to-back on the block top
SCREW_REACH = 4.2        # how far the light's screw sticks up
# -------------------------------------------------------------------
CLR_BACK = 3.0           # gap between the ring's back face and the cradle's front plate
CAM_X, CAM_Z = 132.0, 52.0   # camera centre in the cradle frame (average of the three phones, camera at top)
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


def ring_light_cradle(captive=True):
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    y_back = YS - SCREW_TO_BACK - 1.0               # stays 1 mm behind the ring's back face
    # pad on the block; its closed-end side is angled 45 deg in plan view so that face
    # isn't a flat ceiling when the part prints standing on the cradle's closed end
    x0 = XS + PAD_X[0]
    y0, y1 = YS - PAD_D / 2, YS + PAD_D / 2
    pad = Manifold.batch_hull([w.box(x0 + (y0 - y_back), XS + PAD_X[1], y0, y0 + 0.01, ZS, ZS + PAD_T),
                               w.box(x0 + (y1 - y_back), XS + PAD_X[1], y1 - 0.01, y1, ZS, ZS + PAD_T)])
    # plate under the cradle floor; its closed-end tip tapers at 45 deg (prints on that end)
    slab = Manifold.batch_hull([w.box(SLAB_X[0] + 6, SLAB_X[1], -W_, w.T + W_, -W_ - 6, -W_ + 0.01),
                                w.box(SLAB_X[0], SLAB_X[0] + 0.01, -W_, w.T + W_, -W_ - 0.01, -W_ + 0.01)])
    # post: directly behind the ring, from the block's height up to the cradle floor
    post = w.box(XS + PAD_X[0], SLAB_X[1], -W_, y_back, ZS, -W_ + 0.01)
    # bridge: post -> pad, passing through the ring's opening only above the mounting block
    riser = Manifold.batch_hull([slab, post]) + Manifold.batch_hull([post, pad])
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
