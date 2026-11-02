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


def _shoulder_geometry():
    r = ROUND
    pr_ = PAD_D / 2
    end_c = (XS + PAD_X[1] - pr_, YS)
    return (SLAB_X[1] - r, -W_ + r, r), (end_c[0], end_c[1], pr_)


def _riser():
    """The arm. Its underside leaves the cradle's underside tangentially (no step, no tip)
    and sweeps down in a smooth curve (quadratic Bezier) to the post behind the ring,
    reaching exactly 45 deg at the bottom -- nothing steeper than 45 deg when printed
    standing on the closed end. Its sides continue the cradle's screen-side and front faces
    straight down; the front then flares out smoothly toward the post. A rounded puck reaches
    forward over the light's block. Bottom edges are rounded (ROUND); the pad's bottom is flat."""
    r = ROUND
    y_back = YS - SCREW_TO_BACK - 1.0               # stays 1 mm behind the ring's back face
    x0 = XS + PAD_X[0]
    x_end = SLAB_X[1]
    y_front = w.T + W_                              # the cradle's front face
    T = np.array([SLAB_X[0], -W_])                  # starts ON the cradle's underside, horizontal
    B = np.array([x0, ZS])                          # post bottom, 45 deg
    C = np.array([B[0] - (T[1] - ZS), T[1]])        # 45 deg line through B, at the underside height
    slices = []
    for t in np.linspace(0, 1, SWEEP_STEPS + 1):
        px, pz = (1 - t) ** 2 * T + 2 * (1 - t) * t * C + t ** 2 * B
        ymax = y_front + (y_back - y_front) * t ** 2           # front flares out toward the post
        zc = pz + r                                            # bottom-ball centres
        top = w.box(px + r, x_end, -W_, y_front, min(zc, -W_), 0.0)      # flush with the cradle's faces
        balls = [(x, y, zc) for x in (px + r, x_end - r) for y in (-W_ + r, ymax - r)]
        slices.append(Manifold.batch_hull([top] + [Manifold.sphere(r, 32).translate(list(p)) for p in balls]))
    arm = Manifold()
    for a_, b_ in zip(slices[:-1], slices[1:]):
        arm = arm + Manifold.batch_hull([a_, b_])
    post = slices[-1]
    # pad over the block: a rounded puck. Far end is a semicircle on the screw; the closed-end
    # side stays angled 45 deg in plan (lined up with the post) so it prints on end.
    (_, _, _), (ex, ey, pr_) = _shoulder_geometry()
    y0, y1 = YS - PAD_D / 2, YS + PAD_D / 2
    cham = lambda yy: x0 + (yy - y_back)
    plan = CrossSection.batch_hull([w.circ(pr_, ex, ey, n=96),
                                    CrossSection([[(cham(y0), y0), (cham(y1), y1), (cham(y1) + 0.01, y1), (cham(y0) + 0.01, y0)]])])
    core = plan.offset(-r, __import__("manifold3d").JoinType.Round, circular_segments=96)
    pad = core.extrude(PAD_T - 2 * r).translate([0, 0, ZS + r]).minkowski_sum(Manifold.sphere(r, 32))
    arm = arm + Manifold.batch_hull([post, pad])
    # keep it below the phone channel (it merges into the cradle floor between -WALL and -1),
    # and flush with the cradle's front face down to the floor's underside
    arm = arm ^ w.box(-50, 250, -50, 100, -100, -1.0)
    return arm - w.box(-50, 250, y_front, 100, -W_, 1.0)


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


def ring_light_cradle(captive=True):
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    riser = _riser()
    m = cr + riser
    # one continuous curve at the open end's screen-side corner: the same arc trims the arm
    # and the cradle's floor and bottom lip above it
    m = m - _shoulder_trim(*_shoulder_geometry(), z_top=LIP_TRIM_TOP)
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
