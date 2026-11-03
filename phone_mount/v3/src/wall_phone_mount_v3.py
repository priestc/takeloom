"""
Wall-mounted phone mount v3: sculpted arm and wall plate, captive bolts, thumb knobs.

Same cradle as v1 (only its tilt tongue is thicker). The arm and the wall plate are new:

  1. wall_plate  - flat back sticks to Command strips; a filleted horizontal fin is the PAN joint.
                   The pan bolt is sealed inside the fin (head + washer in a closed pocket),
                   threaded end pointing down.
  2. arm         - hangs under the fin: a round puck (pan) sweeping up into an upright
                   plate (TILT). The tilt bolt is sealed inside the upright plate, threaded
                   end pointing toward the cradle.
  3. cradle      - the v1 cradle, tongue 11 mm thick with a plain hole.
  4. knob (x2)   - thumb knob with a sealed M5 nut and a blind bore. It screws onto the
                   bolt's threaded end and clamps the joint. Nothing to lose, no tools.

Every bolt is an M5 x 20 hex-head (ISO 4017 / DIN 933, full thread) with an M5 washer
(DIN 125, 10 x 1 mm). Each joint's clamped thickness is set from the bolt length, so the
tip ends TIP_PAST_NUT past the nut, inside the knob's blind bore: no threaded end sticks out.

Use frame: wall at y = 0, room toward +y, z up, x along the wall.

    pip install manifold3d trimesh numpy
    python3 wall_phone_mount_v3.py      # writes STLs to ../stl/
"""
import functools
import math
import os
import sys

import numpy as np
from manifold3d import Manifold, CrossSection

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "v1", "src"))
import wall_phone_mount as v1  # noqa: E402  (the cradle is reused from v1)

# ---------------- hardware (mm) ----------------
BOLT_L = 20.0                     # M5 x 20 hex-head bolt, length under the head
HEAD_AF, HEAD_K = 8.0, 3.5        # ISO 4017 M5 head: 8 mm across flats, 3.5 mm tall
WASHER_OD, WASHER_T = 10.0, 1.0   # DIN 125 M5 washer
NUT_AF, NUT_M = 8.0, 4.0          # ISO 4032 M5 nut

# ---------------- clearances and skins ----------------
HOLE_R = 2.75                     # M5 clearance hole
HEAD_AF_C, HEAD_K_C = 8.3, 3.9    # head pocket
WASHER_R_C, WASHER_T_C = 5.25, 1.3
NUT_AF_C, NUT_CAV = 8.4, 4.4      # same nut pocket as v1
SKIN = 1.6                        # wall between the washer and the joint face (8 layers)
OUTER_SKIN = 2.2                  # wall behind the bolt head
KNOB_FLOOR = 1.0                  # wall between the knob's nut and its bearing face
TIP_PAST_NUT = 1.4                # bolt tip ends this far past the nut when tight
TIP_ROOM = 3.0                    # blind bore beyond the tip (tightening travel + length tolerance)
LAYER_H = 0.2

# clamped thickness of each joint (arm puck, cradle tongue), set by the bolt length
CLAMP = BOLT_L - WASHER_T - SKIN - KNOB_FLOOR - NUT_M - TIP_PAST_NUT      # 11.0
POCKET_DEPTH = SKIN + WASHER_T_C + HEAD_K_C                              # face -> back of head pocket
BOSS_T = POCKET_DEPTH + OUTER_SKIN                                       # fin / tilt plate thickness (9.0)
ROOF_REL = max(WASHER_R_C, HEAD_AF_C / math.sqrt(3)) + 0.15              # pocket roof above the bolt axis

# ---------------- wall plate ----------------
PLATE_W, PLATE_H, PLATE_T = 46.0, 80.0, 4.0   # fits 1 or 2 Command Large strips
PLATE_R = 8.0          # corner radius
FIN_Z = 46.0           # fin underside (the arm hangs below it)
PIV_Y = 24.0           # pan axis distance from the wall (finger room around the pan knob)
FIN_R = 12.5           # fin's round end around the pan axis
FIN_ROOT_R = 15.0      # fin root (30 mm wide where it meets the plate)
FILLET = 6.0           # fin-to-plate fillet blend

# ---------------- arm ----------------
ARM_LEN = 34.0         # pan axis -> tilt axis
PUCK_R = 12.5
TILT_R = 12.0          # upright plate's round top around the tilt axis
FLAT_R = 14.5          # the arm stays below the fin inside this radius (fin contact + clearance)
SWEEP_Y0 = 14.0        # the arm's top starts rising here
EDGE_R = 3.0           # rounded top edges
ARM_FLARE = 3.5        # the upright is this much thicker at the bed than at the tilt axis

# ---------------- knob ----------------
KNOB_R = 14.0
KNOB_LOBES = 7
KNOB_SCALLOP_R = 3.4
KNOB_BORE_TOP = KNOB_FLOOR + NUT_M + TIP_PAST_NUT + TIP_ROOM               # 9.4
KNOB_H = KNOB_BORE_TOP + 2.0

SEG = 64


def snap_up(z):
    return float(np.ceil(round(z / LAYER_H, 6)) * LAYER_H)


# ======================= SDF helpers (negative inside) =======================
def smin(a, b, k):
    h = max(k - abs(a - b), 0.0) / k
    return min(a, b) - h * h * k / 4.0


def round_isect(a, b, r):
    """Intersection of two SDFs with the shared edge rounded by r."""
    qa, qb = a + r, b + r
    return math.hypot(max(qa, 0.0), max(qb, 0.0)) + min(max(qa, qb), 0.0) - r


def sd_round_rect(px, py, x0, x1, y0, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    qx, qy = abs(px - cx) - hx, abs(py - cy) - hy
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sd_uneven_capsule(px, py, r1, r2, h):
    """2D: circle r1 at the origin, circle r2 at (0, h), and their tangent hull (iq)."""
    px = abs(px)
    b = (r1 - r2) / h
    a = math.sqrt(max(1 - b * b, 0.0))
    k = -b * px + a * py   # dot((-b, a), (px, py))
    if k < 0:
        return math.hypot(px, py) - r1
    if k > a * h:
        return math.hypot(px, py - h) - r2
    return px * a + py * b - r1


def level(f, bounds, edge=0.45):
    """Mesh an SDF; then merge the many near-coplanar triangles on flat faces (0.02 mm tolerance)."""
    return Manifold.level_set(lambda x, y, z: -f(x, y, z), bounds, edge).simplify(0.02)


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def xform(m, cols, origin):
    """Map local (x, y, z) to world with the given column vectors and origin."""
    a = np.array(cols, float).T
    return m.transform(np.c_[a, np.array(origin, float)])


# ======================= captive bolt pocket =======================
def bolt_pocket(roof):
    """Pocket for a sealed bolt, in local coordinates: the bolt axis is local z, the joint
    face is z = 0 and the threaded end leaves through it toward -z. Local +y is print-up.
    Each section is open from the axis up to `roof` so the bolt can be dropped in at a pause;
    the layers above the roof seal it."""
    def slot(cs, half_w, z0, z1):
        cs = CrossSection.batch_hull([cs, CrossSection.square([2 * half_w, roof]).translate([-half_w, 0])])
        return cs.extrude(z1 - z0).translate([0, 0, z0])
    shaft = slot(CrossSection.circle(HOLE_R, SEG), HOLE_R, -1.0, SKIN + 0.01)
    washer = slot(CrossSection.circle(WASHER_R_C, SEG), WASHER_R_C, SKIN, SKIN + WASHER_T_C + 0.01)
    # hex head, one corner pointing down (print-down) so it sits in a V and can't turn
    hexa = CrossSection.circle(HEAD_AF_C / math.sqrt(3), 6).rotate(90)
    head = slot(hexa, HEAD_AF_C / 2, SKIN + WASHER_T_C, POCKET_DEPTH)
    return shaft + washer + head


# ======================= WALL PLATE =======================
def wall_plate_sdf(x, y, z):
    plate2 = sd_round_rect(x, z, -PLATE_W / 2, PLATE_W / 2, 0, PLATE_H, PLATE_R)
    plate = round_isect(plate2, abs(y - (PLATE_T - 6) / 2) - (PLATE_T + 6) / 2, 1.5)  # back face cut flat later
    fin2 = sd_uneven_capsule(x, y - PLATE_T, FIN_ROOT_R, FIN_R, PIV_Y - PLATE_T)
    fin = round_isect(fin2, abs(z - (FIN_Z + BOSS_T / 2)) - BOSS_T / 2, 1.5)
    return smin(plate, fin, FILLET)


def pan_roof():
    """Pan pocket roof, as print height (the plate prints on its back, so print height = y)."""
    return snap_up(PIV_Y + ROOF_REL)


@functools.lru_cache(None)
def wall_plate():
    """Returns (use-position part, print-orientation part)."""
    m = level(wall_plate_sdf, [-PLATE_W / 2 - 2, -3, -2, PLATE_W / 2 + 2, PIV_Y + FIN_R + 2, PLATE_H + 2])
    m = m ^ box(-50, 50, 0, 50, -10, 100)                                     # flat back
    m = m - Manifold.cylinder(6, FLAT_R, FLAT_R, SEG).translate([0, PIV_Y, FIN_Z - 6])  # flat fin underside
    pocket = bolt_pocket(pan_roof() - PIV_Y)          # local y = world y, local z = world z
    m = m - pocket.translate([0, PIV_Y, FIN_Z])
    pr = m.rotate([90, 0, 0])                         # back face (y = 0) onto the bed
    pr = pr.translate([0, 0, -pr.bounding_box()[2]])
    return m, pr


# ======================= ARM =======================
# Arm frame: pan axis = z axis, arm top (against the fin) at z = 0, bottom (knob side) at
# z = -CLAMP. The tilt axis runs along x at (y = ARM_LEN, z = 0); the upright plate's
# joint face is x = 0 and the cradle sits on the +x side.
def arm_top(y):
    if y <= SWEEP_Y0:
        return 0.0
    if y < ARM_LEN:
        t = (y - SWEEP_Y0) / (ARM_LEN - SWEEP_Y0)
        return TILT_R * (1 - math.cos(math.pi * t)) / 2        # S-curve up to the top of the tilt disc
    d = y - ARM_LEN
    return math.sqrt(max(TILT_R * TILT_R - d * d, 0.0)) if d < TILT_R else -1.0


def arm_sdf(x, y, z):
    puck = math.hypot(x, y) - PUCK_R
    # the upright's outer face leans out toward the bed (a draft, so it prints without overhang)
    t = min(max(-z / CLAMP, 0.0), 1.0)
    outer = BOSS_T + ARM_FLARE * t * t
    strip = sd_round_rect(x, y, -outer, 0.0, 0.0, ARM_LEN + TILT_R, 1.0)
    plan = smin(puck, strip, 12.0)
    if y < ARM_LEN:
        top = z - arm_top(y)
    else:   # round end: true distance to the tilt disc's arc (a "tombstone" in the y-z plane)
        top = math.hypot(y - ARM_LEN, max(z, 0.0)) - TILT_R
    return round_isect(plan, top, EDGE_R)


def tilt_roof():
    """Tilt pocket roof, as print height (the arm prints upright, bottom at z = -CLAMP)."""
    return snap_up(CLAMP + ROOF_REL)


@functools.lru_cache(None)
def arm():
    """Returns (use-position part in the arm frame, print-orientation part)."""
    m = level(arm_sdf, [-PUCK_R - 3, -PUCK_R - 3, -CLAMP - 3, PUCK_R + 3, ARM_LEN + TILT_R + 3, TILT_R + 3])
    m = m ^ box(-50, 50, -50, 100, -CLAMP, 50)                                         # flat bottom
    m = m - Manifold.cylinder(20, FLAT_R, FLAT_R, SEG)                                 # flat top under the fin
    m = m - box(0, 50, PUCK_R + 2.5, 100, -50, 50)                                     # flat tilt face
    m = m - Manifold.cylinder(30, HOLE_R, HOLE_R, SEG).translate([0, 0, -15])          # pan hole
    pocket = bolt_pocket(tilt_roof() - CLAMP)
    # local z (into the part) -> -x, local y (print up) -> +z, local x -> -y
    m = m - xform(pocket, [(0, -1, 0), (0, 0, 1), (-1, 0, 0)], (0, ARM_LEN, 0))
    return m, m.translate([0, 0, CLAMP])


# ======================= KNOB =======================
def knob_sdf(x, y, z):
    r = math.hypot(x, y)
    d2 = r - KNOB_R
    a = math.atan2(y, x)
    step = 2 * math.pi / KNOB_LOBES
    a0 = round(a / step) * step
    sc = math.hypot(x - (KNOB_R + 1.6) * math.cos(a0), y - (KNOB_R + 1.6) * math.sin(a0)) - KNOB_SCALLOP_R
    d2 = max(d2, -sc)
    return round_isect(d2, z - KNOB_H, 3.0)


def knob_pause():
    return snap_up(KNOB_FLOOR + NUT_CAV)


@functools.lru_cache(None)
def knob():
    """Print orientation = use orientation: bearing face down (z = 0), bore up."""
    m = level(knob_sdf, [-KNOB_R - 2, -KNOB_R - 2, -2, KNOB_R + 2, KNOB_R + 2, KNOB_H + 2], 0.35)
    m = m ^ box(-50, 50, -50, 50, 0, 50)
    m = m - Manifold.cylinder(KNOB_BORE_TOP + 1, HOLE_R, HOLE_R, SEG).translate([0, 0, -1])
    roof = knob_pause()
    m = m - CrossSection.circle(NUT_AF_C / math.sqrt(3), 6).extrude(roof - KNOB_FLOOR).translate([0, 0, KNOB_FLOOR])
    return m


# ======================= CRADLE =======================
@functools.lru_cache(None)
def cradle(mirror=False):
    """v1 cradle, tilt tongue CLAMP thick, plain hole. Returns (cradle frame part, print part, (ay, az))."""
    v1.TONGUE_T = CLAMP
    m, pr, axis = v1.cradle(nut_pocket=False)
    if mirror:
        return m.mirror([1, 0, 0]), pr.mirror([1, 0, 0]), axis
    return m, pr, axis


# ======================= assembly =======================
def bolt(length=BOLT_L):
    """Hex bolt + washer along -z from z = 0 (washer face at z = 0)."""
    head = CrossSection.circle(HEAD_AF / math.sqrt(3), 6).rotate(90).extrude(HEAD_K).translate([0, 0, WASHER_T])
    washer = Manifold.cylinder(WASHER_T, WASHER_OD / 2, WASHER_OD / 2, SEG)
    shank = Manifold.cylinder(length, 2.5, 2.5, 32).translate([0, 0, WASHER_T - length])
    return head + washer + shank


def assemble(pan_deg=0.0, tilt_deg=0.0, mirror=False, hardware=True):
    """Use-position parts: dict name -> Manifold."""
    wp, _ = wall_plate()
    am, _ = arm()
    cr, _, (ay, az) = cradle()
    w = v1.WALL
    parts_arm = {"arm": am}
    # cradle: tongue face (x = -w) against the tilt face (x = 0), tongue axis on the tilt axis
    c = cr.translate([w, -ay, -az]).rotate([tilt_deg, 0, 0]).translate([0, ARM_LEN, 0])
    parts_arm["cradle"] = c
    if hardware:
        kb = knob()
        parts_arm["knob_pan"] = kb.rotate([180, 0, 0]).translate([0, 0, -CLAMP])
        parts_arm["knob_tilt"] = kb.rotate([0, 90, 0]).translate([CLAMP, ARM_LEN, 0])
        # tilt bolt: washer face on the skin, threaded end toward +x
        parts_arm["bolt_tilt"] = bolt().rotate([0, 0, 90]).rotate([0, -90, 0]).translate([-SKIN, ARM_LEN, 0])
    out = {"wall_plate": wp}
    for k, m in parts_arm.items():
        if mirror:
            m = m.mirror([1, 0, 0])
        out[k] = m.rotate([0, 0, pan_deg]).translate([0, PIV_Y, FIN_Z])
    if hardware:
        out["bolt_pan"] = bolt().translate([0, PIV_Y, FIN_Z + SKIN])
    return out


def to_trimesh(m):
    import trimesh
    mesh = m.to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3],
                           faces=np.asarray(mesh.tri_verts), process=True)


def on_bed(m):
    b = m.bounding_box()
    return m.translate([-(b[0] + b[3]) / 2, -(b[1] + b[4]) / 2, -b[2]])


def pauses():
    """(Bambu slider height, what to drop in). The slider shows each layer's top, so the
    first roofing layer is shown 0.2 mm above where the roof starts."""
    return [(knob_pause() + LAYER_H, "knobs: drop an M5 nut flat into each knob"),
            (tilt_roof() + LAYER_H, "arm: drop the tilt bolt (washer on, under the head) into its slot"),
            (pan_roof() + LAYER_H, "wall plate: drop the pan bolt (washer on, under the head) into its slot")]


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stl")
    os.makedirs(out, exist_ok=True)
    _, wp = wall_plate()
    _, am = arm()
    kb = knob()
    _, cr_r, _ = cradle()
    _, cr_l, _ = cradle(mirror=True)
    for name, m in [("wall_plate", wp), ("arm_right", am), ("arm_left", am.mirror([1, 0, 0])),
                    ("knob", kb), ("cradle_right", cr_r), ("cradle_left", cr_l)]:
        t = to_trimesh(on_bed(m))
        t.export(f"{out}/{name}.stl")
        print(f"{name:13s} watertight={t.is_watertight} vol={t.volume / 1000:.1f}cm3 bbox={np.round(t.extents, 1)}")
    print(f"clamped thickness (arm puck, cradle tongue) = {CLAMP:.1f} mm; fin / tilt plate {BOSS_T:.1f} mm")
    for h, what in pauses():
        print(f"pause at {h:.2f} mm: {what}")
