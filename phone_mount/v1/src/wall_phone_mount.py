"""
Wall-mounted phone mount (Command-strip) — parametric generator.

Three printed parts, joined with 2x M5x20 screws + 2x M5 hex nuts (+ washers under the heads).
Each joint has a hex nut pocket on both of its outer faces, so the nut can go on either side.
  1. wall_plate   – flat back sticks to Command strips; horizontal fin = PAN joint.
                    Pan bolt head sits UNDER the fin (reach it from below when mounted high).
  2. arm          – horizontal disc (pan) -> vertical disc (TILT); both nuts are in the arm
                    (top of the pan boss, outer face of the tilt plate).
  3. cradle       – phone's back lies flat against a solid front plate; camera
                    bump overhangs the open end. Loads "lift-and-hook" from the
                    screen side: top edge up under the deep lip, swing the bottom
                    past the low lip, drop. Remove: lift, tip out.

One cradle fits every phone between PHONE_W_MIN and PHONE_W_MAX wide (short side,
case included) and up to ~13 mm thick. Defaults cover iPhone 13 mini (bare) through
iPhone 16 Pro Max in a thick case, incl. Galaxy S20 FE. Regenerate with:
    pip install manifold3d trimesh
    python3 wall_phone_mount.py
"""
import numpy as np
import trimesh
import manifold3d
from manifold3d import Manifold, CrossSection

# ---------------- user parameters (mm) ----------------
PHONE_W_MIN = 64.0  # narrowest phone short side (iPhone 13 mini bare = 64.2)
PHONE_W_MAX = 81.0  # widest phone short side incl. case (16 Pro Max bare = 77.6)
INNER_DEPTH = 19.0  # channel depth; fits phones up to ~13 mm thick incl. case
CRADLE_LEN = 112.0  # ~ the 16 Pro Max's flat back length; shorter phones sit out from the end wall
ARM_LEN = 30.0      # pan pivot -> tilt pivot; leaves ~40 mm behind the cradle to load the phone
PLATE_W, PLATE_H, PLATE_T = 46.0, 80.0, 4.0   # fits 1 or 2 Command Large strips

# ---------------- fixed design values ----------------
WALL = 3.0          # cradle wall thickness
LIP_BOT = 4.0       # lower front lip height
BOLT_R = 2.75       # M5 clearance
NUT_R = 8.4 / np.sqrt(3)   # M5 nut, 8mm AF + clearance -> circumradius
NUT_D = 4.2
PAN_BOSS_T = 8.0    # arm thickness at the pan joint (room for the nut pocket on top)
LAYER_H = 0.2       # used to put the captive-nut cavity roofs exactly on a layer boundary
NUT_CAV_H = 4.4     # captive-nut cavity thickness (nut is 4.0)
PAN_NUT_Z0 = 2.6    # captive pan nut: floor under the cavity (the nut is pulled down onto it)
DISC_R = 12.0
JOINT_T = 6.0       # arm bar thickness
FIN_T = 8.0         # wall-plate fin thickness (room for a nut pocket on its underside)
TONGUE_T = 8.0      # cradle tongue thickness (room for a nut pocket on its open-end face)
ARM_PLATE_T = 8.0   # arm's vertical (tilt) plate thickness
MESH_FRONT = True   # diamond mesh in the plate the phone's back rests on
MESH_HOLE = 12.0    # diamond hole size (point to point)
MESH_RIB = 2.4      # rib width between holes
MESH_BORDER = 5.0   # solid frame around the mesh
SEG = 64

H = PHONE_W_MAX + LIP_BOT + 1.5     # inner height: widest phone can still lift over the lower lip
LIP_TOP = H - PHONE_W_MIN + 2.5     # deep top lip: narrowest phone still overlapped 2.5 mm
T = INNER_DEPTH


def box(x0, x1, y0, y1, z0, z1):
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def circ(r, cx=0, cy=0, n=SEG):
    return CrossSection.circle(r, n).translate([cx, cy])


def rect2(x0, x1, y0, y1):
    return CrossSection.square([x1 - x0, y1 - y0]).translate([x0, y0])


def teardrop(r, length):
    """Hole along +Z, teardrop point toward +Y (print-up after reorientation)."""
    tip = CrossSection.square([r * 0.6, r * 0.6]).rotate(45).translate([0, r * 0.62])
    cs = CrossSection.batch_hull([circ(r), tip])
    return cs.extrude(length)


def yz_extrude(cs, x0, x1):
    """Extrude a CrossSection drawn in (Y,Z) along X from x0 to x1."""
    # cross-section coords (u,v) -> (y,z): extrude along z, then rotate
    m = cs.extrude(x1 - x0)                  # u->x, v->y, height->z
    m = m.transform(np.array([[0, 0, 1, x0],   # x = h
                              [1, 0, 0, 0],    # y = u
                              [0, 1, 0, 0]]))  # z = v
    return m


# ======================= WALL PLATE =======================
def wall_plate(nut_pocket=True):
    """nut_pocket=False for the captive-nut build: the screw head side is a plain flat face."""
    zc = PLATE_H / 2 + 6          # fin top
    piv_y = PLATE_T + 18
    plate = box(-PLATE_W / 2, PLATE_W / 2, 0, PLATE_T, 0, PLATE_H)
    fin_cs = CrossSection.batch_hull([rect2(-14, 14, PLATE_T - 1, PLATE_T),
                                      circ(DISC_R, 0, piv_y)])
    fin = fin_cs.extrude(FIN_T).translate([0, 0, zc - FIN_T])
    gus_cs = CrossSection([[(PLATE_T - 1, zc - FIN_T), (PLATE_T - 1, zc - FIN_T - 16),
                            (PLATE_T + 12, zc - FIN_T)]])
    gusset = yz_extrude(gus_cs, -2, 2)
    m = plate + fin + gusset
    # pan bolt hole (vertical in use, horizontal in print -> teardrop).
    # Hex pockets on BOTH outer faces of the pan joint (this one, and the top of the
    # arm boss), so the nut can go on either side and the head on the other.
    hole = teardrop(BOLT_R, 40).translate([0, piv_y, zc - 20])
    # one hex corner points away from the wall = up when printed on its back
    nut = CrossSection.circle(NUT_R, 6).rotate(90).extrude(NUT_D + 0.01) \
        .translate([0, piv_y, zc - FIN_T - 0.01])
    m = m - hole - (nut if nut_pocket else Manifold())
    # print orientation: back face (y=0) on bed -> rotate so +Y becomes +Z
    return m, m.rotate([-90, 0, 0]).rotate([180, 0, 0])


# ======================= ARM =======================
def _snap_up(z):
    return float(np.ceil(round(z / LAYER_H, 6)) * LAYER_H)


def arm(captive=False):
    """captive=False: open hex pockets on the arm's outer faces.
    captive=True : the two nuts are sealed inside the arm during printing. The print is
                   paused at captive_pause_heights() to drop each nut into its cavity."""
    bar = CrossSection.batch_hull([circ(DISC_R), circ(8, 0, ARM_LEN)]).extrude(JOINT_T)
    tz = JOINT_T + 14                    # tilt axis height
    vplate_cs = CrossSection.batch_hull([rect2(ARM_LEN - 12, ARM_LEN + 8, 0, JOINT_T),
                                         circ(DISC_R, ARM_LEN, tz)])
    vplate = yz_extrude(vplate_cs, -ARM_PLATE_T / 2, ARM_PLATE_T / 2)
    boss = Manifold.cylinder(PAN_BOSS_T, DISC_R, DISC_R, SEG)
    m = bar + vplate + boss
    m = m - Manifold.cylinder(40, BOLT_R, BOLT_R, SEG).translate([0, 0, -5])
    # tilt hole along X, teardrop point toward +Z
    th = teardrop(BOLT_R, 40)            # along z, point +y
    th = th.transform(np.array([[0, 0, 1, -20], [1, 0, 0, ARM_LEN], [0, 1, 0, tz]]))
    m = m - th
    hexcs = CrossSection.circle(NUT_R, 6)
    if not captive:
        # pan nut: hex pocket in the top of the boss
        m = m - hexcs.extrude(NUT_D + 0.01).translate([0, 0, PAN_BOSS_T - NUT_D])
        # tilt nut: hex pocket in the arm plate's outer face (-x), one corner up so it prints cleanly
        m = m - yz_extrude(hexcs.rotate(90).translate([ARM_LEN, tz]), -ARM_PLATE_T / 2 - 0.01, -ARM_PLATE_T / 2 + NUT_D)
        return m, m, tz
    pan_top, tilt_top = captive_pause_heights()
    # pan nut: flat hex cavity sealed inside the boss; roof is printed after the pause
    m = m - hexcs.extrude(pan_top - PAN_NUT_Z0).translate([0, 0, PAN_NUT_Z0])
    # tilt nut: stands on edge in the upright plate, corner down in a V seat, with a
    # straight-sided slot above it up to a flat roof printed after the pause
    af = NUT_R * np.sqrt(3)              # across flats incl. clearance
    slot = CrossSection.batch_hull([hexcs.rotate(90).translate([ARM_LEN, tz]),
                                    rect2(ARM_LEN - af / 2, ARM_LEN + af / 2, tz, tilt_top)])
    x0 = -ARM_PLATE_T / 2 + 1.0          # 1 mm outer skin; the nut is pulled toward the cradle side
    m = m - yz_extrude(slot, x0, x0 + NUT_CAV_H)
    return m, m, tz


def captive_pause_heights():
    """Print heights (mm above the bed, arm printed flat) of the two cavity roofs.
    Pause BEFORE the layer that starts at each height."""
    tz = JOINT_T + 14
    return _snap_up(PAN_NUT_Z0 + NUT_CAV_H), _snap_up(tz + NUT_R + 0.15)


# ======================= CRADLE =======================
def cradle(nut_pocket=True, tongue=True):
    """nut_pocket=False for the captive-nut build: the screw head side is a plain flat face.
    tongue=False: no tilt joint at all (used by the one-piece tripod cradle)."""
    L, w = CRADLE_LEN, WALL
    # FRONT plate (subject side, meshed below): the phone's back lies flat against it.
    # Lips are on the screen side (toward the wall), so the screen stays visible
    # and the phone loads lift-and-hook from that side.
    front = box(-w, L, T, T + w, -w, H + w)
    floor = box(-w, L, -w, T + w, -w, 0)
    top = box(-w, L, -w, T + w, H, H + w)
    lip_b = yz_extrude(CrossSection([[(-w, -w), (0, -w), (0, LIP_BOT - 1.2),
                                      (-1.2, LIP_BOT), (-w, LIP_BOT)]]), -w, L)
    lip_t = yz_extrude(CrossSection([[(-w, H - LIP_TOP), (-1.2, H - LIP_TOP), (0, H - LIP_TOP + 1.2),
                                      (0, H + w), (-w, H + w)]]), -w, L)
    endw = box(-w, 0, -w, T + w, -w, H + w)
    m = front + floor + top + lip_b + lip_t + endw

    # Mesh the front plate: diamond holes (45-degree sides print cleanly with the
    # cradle standing on its end), solid border all round for stiffness.
    if MESH_FRONT:
        d, rib, border = MESH_HOLE, MESH_RIB, MESH_BORDER
        p = d + rib * np.sqrt(2)
        x0, x1, z0, z1 = border, L - border, border, H - border
        holes = []
        for i in range(-2, int(L / p) + 3):
            for j in range(-2, int(H / p) + 3):
                for ox, oz in ((0, 0), (p / 2, p / 2)):
                    cx, cz = x0 + d / 2 + i * p + ox, z0 + d / 2 + j * p + oz
                    if cx - d / 2 >= x0 and cx + d / 2 <= x1 and cz - d / 2 >= z0 and cz + d / 2 <= z1:
                        holes.append(CrossSection([[(cx + d / 2, cz), (cx, cz + d / 2),
                                                    (cx - d / 2, cz), (cx, cz - d / 2)]]))
        if holes:
            cs = CrossSection.batch_boolean(holes, manifold3d.OpType.Add)
            bx = cs.bounds()   # centre the pattern inside the border
            cs = cs.translate([(x0 + x1) / 2 - (bx[0] + bx[2]) / 2, (z0 + z1) / 2 - (bx[1] + bx[3]) / 2])
            hm = cs.extrude(w + 2).transform(np.array([[1, 0, 0, 0], [0, 0, -1, T + w + 1], [0, 1, 0, 0]]))
            m = m - hm

    # charging-cable / mic slot in the closed end
    m = m - box(-w - 1, 1, -w - 1, T, 18, 56)   # covers USB port height for all phones

    # tilt tongue under the floor, at the closed end
    ay, az = 9.0, -w - 16
    tongue_cs = CrossSection.batch_hull([rect2(-w, T + w, -w - 1, -w), circ(DISC_R, ay, az)])
    if not tongue:
        pr = m.translate([w, 0, 0]).rotate([0, -90, 0])
        return m, pr.translate([0, 0, -pr.bounding_box()[2]]), (ay, az)
    tongue = yz_extrude(tongue_cs, -w, -w + TONGUE_T)
    m = m + tongue
    # tilt bolt hole, with a hex pocket in the tongue's open-end face (+x). Together with the
    # pocket in the arm plate's outer face, the nut can go on either side.
    m = m - Manifold.cylinder(40, BOLT_R, BOLT_R, SEG).rotate([0, 90, 0]).translate([-20, ay, az])
    if nut_pocket:
        m = m - yz_extrude(CrossSection.circle(NUT_R, 6).rotate(90).translate([ay, az]),
                       -w + TONGUE_T - NUT_D, -w + TONGUE_T + 0.01)
    # print orientation: closed end on the bed (+X up). x=-w face -> z=0
    pr = m.translate([w, 0, 0]).rotate([0, -90, 0])
    pr = pr.translate([0, 0, -pr.bounding_box()[2]])
    return m, pr, (ay, az)


def to_trimesh(m):
    mesh = m.to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3],
                           faces=np.asarray(mesh.tri_verts), process=True)


def on_bed(m):
    b = m.bounding_box()
    return m.translate([-(b[0] + b[3]) / 2, -(b[1] + b[4]) / 2, -b[2]])


def assemble(pan_deg=30, tilt_deg=-20, mirror=False):
    """Use-position assembly (wall at y=0, room at +y) for preview."""
    wp, _ = wall_plate()
    am, _, tz = arm()
    cr, _, (ay, az) = cradle()
    zc = PLATE_H / 2 + 6
    piv_y = PLATE_T + 18
    # cradle: tilt about its own axis, place axis at arm's tilt pivot, stack beside arm plate
    c = cr.translate([0, -ay, -az]).rotate([tilt_deg, 0, 0])
    c = c.translate([WALL + ARM_PLATE_T / 2, ARM_LEN, tz])   # tongue face (x=-w) against arm plate
    if mirror:
        c = c.mirror([1, 0, 0])
    arm_c = (am + c).rotate([0, 0, pan_deg]).translate([0, piv_y, zc])
    return wp, arm_c


if __name__ == "__main__":
    import os
    out = "out"
    os.makedirs(out, exist_ok=True)
    _, wp_p = wall_plate()
    _, am_p, _ = arm()
    _, cr_p, _ = cradle()
    for name, m in [("wall_plate", on_bed(wp_p)), ("arm", on_bed(am_p)),
                    ("arm_left", on_bed(am_p.mirror([1, 0, 0]))),   # tilt nut on the other face
                    ("arm_captive", on_bed(arm(captive=True)[1])),
                    ("wall_plate_no_pocket", on_bed(wall_plate(nut_pocket=False)[1])),
                    ("cradle_right_no_pocket", on_bed(cradle(nut_pocket=False)[1])),
                    ("cradle_left_no_pocket", on_bed(cradle(nut_pocket=False)[1].mirror([1, 0, 0]))),
                    ("arm_captive_left", on_bed(arm(captive=True)[1].mirror([1, 0, 0]))),
                    ("cradle_right", on_bed(cr_p)),
                    ("cradle_left", on_bed(cr_p.mirror([1, 0, 0])))]:
        t = to_trimesh(m)
        t.export(f"{out}/{name}.stl")
        print(f"{name:23s} watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 "
              f"bbox={np.round(t.extents,1)}")
    print("captive-nut arm: pause before the layers at %.1f mm (pan nut) and %.1f mm (tilt nut)" % captive_pause_heights())
