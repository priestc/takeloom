"""
Wall-mounted phone mount (Command-strip) — v2, magnetic toothed joints.

No bolts. Each joint (pan at the wall, tilt at the cradle) is a pair of discs with
fine radial teeth, held together by a stack of 5 x 2 mm magnets on each side of the
joint, on its axis. A printed pin through the centre of each joint carries the
sideways/prying load, which magnets alone can't. To re-aim: pull the part ~2 mm off
the teeth (the pin keeps it aligned), turn it a notch, let it snap back.

Printed parts:
  1. wall_plate   – Command strips on the back; fin on top is the PAN joint (teeth + pin socket)
  2. arm          – pan boss underneath (teeth + pin hole + magnets), tilt plate (teeth + pin socket)
  3. cradle       – same lift-and-hook cradle as v1; tongue is the TILT joint (teeth + pin hole + magnets)
  4. pin x2       – glued into the wall-plate fin and the arm's tilt plate; magnet stack in its tip

Magnets: 12 x (5 mm dia x 1.96 mm) – a stack of 3 in each pin tip and 3 in each mating pocket.

Regenerate with:
    pip install manifold3d trimesh
    python3 wall_phone_mount_v2.py
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
ARM_LEN = 33.0      # pan pivot -> tilt pivot; leaves ~40 mm behind the cradle to load the phone
PLATE_W, PLATE_H, PLATE_T = 46.0, 80.0, 4.0   # fits 1 or 2 Command Large strips

# ---------------- fixed design values ----------------
WALL = 3.0          # cradle wall thickness
LIP_BOT = 4.0       # lower front lip height
# --- magnetic toothed joint ---
TEETH = 40          # teeth per joint -> 360/TEETH degree notches (9 deg)
TOOTH_R_IN = 9.0    # tooth ring inner radius
DISC_R = 15.0       # joint disc radius = tooth ring outer radius
FLANK = 50.0        # tooth flank angle from the joint plane (deg); steeper = holds better, 45-50 prints cleanly
TOOTH_CLR = 0.25    # axial clearance built into the mating teeth
PIN_R = 5.0         # centre pin radius (10 mm pin)
PIN_ENGAGE = 12.0   # how far the pin reaches into the mating part (longer = less sag)
PIN_CLR = 0.10      # radial clearance pin <-> sliding hole (smaller = less sag; tune with fit_test)
PIN_GLUE_CLR = 0.1  # radial clearance pin <-> glued socket
MAG_D = 5.0         # magnet diameter
MAG_T = 1.96        # magnet thickness
MAG_STACK = 3       # magnets per stack (two stacks per joint)
MAG_POCKET_D = 5.25 # pocket diameter (magnets glued in)
MAG_GAP = 0.75      # pin tip to pocket face, nominal (~0.5 mm once the teeth seat)
SOCKET_T = 8.0      # thickness of the parts the pins are glued into (fin, tilt plate)
ROLL_COMP = 0.0     # deg: pre-tilt the arm's tilt plate to cancel cradle sag (see README)
JOINT_T = 6.0       # arm bar thickness
ARM_PLATE_T = SOCKET_T  # arm's vertical (tilt) plate thickness
MESH_FRONT = True   # diamond mesh in the plate the phone's back rests on
MESH_HOLE = 12.0    # diamond hole size (point to point)
MESH_RIB = 2.4      # rib width between holes
MESH_BORDER = 5.0   # solid frame around the mesh
SEG = 64

H = PHONE_W_MAX + LIP_BOT + 1.5     # inner height: widest phone can still lift over the lower lip
LIP_TOP = H - PHONE_W_MIN + 2.5     # deep top lip: narrowest phone still overlapped 2.5 mm
T = INNER_DEPTH
MAG_DEPTH = MAG_STACK * MAG_T + 0.12                       # pocket depth for one stack
PIN_LEN = SOCKET_T + PIN_ENGAGE                            # 20 mm
B_T = PIN_ENGAGE + MAG_GAP + MAG_DEPTH + 1.2               # thickness of the hole side of a joint
TOOTH_H = np.pi * DISC_R / TEETH * np.tan(np.radians(FLANK))  # tooth height at the rim


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


# ======================= MAGNETIC TOOTHED JOINT =======================
# Every joint is built in a local frame: joint plane z=0, axis +z.
#   "A" side (pin side): body below z=0, teeth rise above it, pin glued in a socket.
#   "B" side (hole side): body above z=0 with matching grooves, a sliding hole for
#   the pin, and a magnet pocket at the end of that hole.

def _frame(m, M):
    """Apply a 3x4 placement matrix to a joint built in its local frame."""
    return m.transform(np.asarray(M, dtype=float))


def tooth_ring(r_out=DISC_R):
    """Radial (Hirth-style) teeth on the A side, z>=0. Tooth height grows with
    radius so every flank is a plane through the axis and meshes at any notch."""
    k = np.pi / TEETH * np.tan(np.radians(FLANK))       # height per mm of radius
    half = np.pi / TEETH
    teeth = []
    for i in range(TEETH):
        th = 2 * np.pi * i / TEETH
        pts = []
        for r in (TOOTH_R_IN, r_out):
            for a in (th - half, th + half):
                pts.append((r * np.cos(a), r * np.sin(a), 0.0))
            pts.append((r * np.cos(th), r * np.sin(th), k * r))
            for a in (th - half * 1.03, th + half * 1.03):    # below z=0: overlap neighbours
                pts.append((r * np.cos(a), r * np.sin(a), -0.3))
        teeth.append(Manifold.hull_points(pts))
    base = Manifold.cylinder(0.51, r_out, r_out, SEG).translate([0, 0, -0.5]) - \
        Manifold.cylinder(2, TOOTH_R_IN - 0.01, TOOTH_R_IN - 0.01, SEG).translate([0, 0, -1])
    return Manifold.batch_boolean(teeth + [base], manifold3d.OpType.Add)


def joint_A_add():
    """A-side teeth with a 45-degree chamfer round the rim, so the tooth ends that
    face downward in print aren't flat ceilings. The B grooves run out past the
    rim, so the chamfer doesn't affect the mesh."""
    cone = Manifold.cylinder(3.0, DISC_R + 0.6, DISC_R - 2.4, SEG).translate([0, 0, -0.6])
    return tooth_ring() ^ cone


def joint_A_cut(socket_depth, point_up_local=None):
    """Glued socket for the pin, going down into the A body (through it).
    point_up_local: (ux, uy) direction in the local xy plane that ends up as 'up'
    when the part is printed with this axis horizontal -> teardrop socket."""
    r = PIN_R + PIN_GLUE_CLR
    cs = circ(r)
    if point_up_local is not None:
        ux, uy = point_up_local
        ang = np.degrees(np.arctan2(uy, ux)) - 90
        tip = CrossSection.square([r * 0.75, r * 0.75]).rotate(45).translate([0, r * 0.35])
        cs = CrossSection.batch_hull([cs, tip.rotate(ang)])
    return cs.extrude(socket_depth + 2).translate([0, 0, -socket_depth - 1])


def joint_B_cut():
    """Everything removed from the B body (body occupies z>=0 in the local frame)."""
    grooves = tooth_ring(DISC_R + 2).translate([0, 0, TOOTH_CLR])   # run out past the rim
    # trim the whole part to z >= TOOTH_CLR so the toothed face is the flat face on the bed
    under = box(-300, 300, -300, 300, -60, TOOTH_CLR)
    hole_top = PIN_ENGAGE + MAG_GAP
    hole = Manifold.cylinder(hole_top + 1, PIN_R + PIN_CLR, PIN_R + PIN_CLR, SEG).translate([0, 0, -1])
    pocket = Manifold.cylinder(MAG_DEPTH + 0.01, MAG_POCKET_D / 2, MAG_POCKET_D / 2, 48) \
        .translate([0, 0, hole_top - 0.01])
    return grooves + under + hole + pocket


def pin():
    """Centre pin, printed standing. Glued SOCKET_T deep into the A side; its tip
    holds a magnet stack and slides PIN_ENGAGE into the B side."""
    body = Manifold.cylinder(PIN_LEN, PIN_R, PIN_R, SEG)
    ch = 0.6
    top_ch = Manifold.cylinder(ch, PIN_R + 1, PIN_R, SEG).translate([0, 0, PIN_LEN - ch])
    top_ring = Manifold.cylinder(ch + 0.01, PIN_R + 2, PIN_R + 2, SEG).translate([0, 0, PIN_LEN - ch]) - \
        Manifold.cylinder(ch + 0.02, PIN_R, PIN_R - ch, SEG).translate([0, 0, PIN_LEN - ch])
    bot_ring = Manifold.cylinder(0.5, PIN_R + 2, PIN_R + 2, SEG).translate([0, 0, -0.01]) - \
        Manifold.cylinder(0.5, PIN_R - 0.5, PIN_R, SEG).translate([0, 0, -0.01])
    bore = Manifold.cylinder(MAG_DEPTH + 0.01, MAG_POCKET_D / 2, MAG_POCKET_D / 2, 48) \
        .translate([0, 0, PIN_LEN - MAG_DEPTH])
    return body - top_ring - bot_ring - bore


# ======================= WALL PLATE =======================
ZC = PLATE_H / 2 + 6          # pan joint plane (top of fin)
PIV_Y = PLATE_T + 18          # pan axis distance from wall
PAN_FRAME = [[1, 0, 0, 0], [0, 1, 0, PIV_Y], [0, 0, 1, ZC]]


def wall_plate():
    plate = box(-PLATE_W / 2, PLATE_W / 2, 0, PLATE_T, 0, PLATE_H)
    fin_cs = CrossSection.batch_hull([rect2(-14, 14, PLATE_T - 1, PLATE_T),
                                      circ(DISC_R, 0, PIV_Y)])
    fin = fin_cs.extrude(SOCKET_T).translate([0, 0, ZC - SOCKET_T])
    gus_cs = CrossSection([[(PLATE_T - 1, ZC - SOCKET_T), (PLATE_T - 1, ZC - SOCKET_T - 18),
                            (PLATE_T + 13, ZC - SOCKET_T)]])
    gusset = yz_extrude(gus_cs, -2.5, 2.5)
    m = plate + fin + gusset + _frame(joint_A_add(), PAN_FRAME)
    # printed on its back -> pan axis is horizontal; teardrop socket points away from the wall (+y = up in print)
    m = m - _frame(joint_A_cut(SOCKET_T, point_up_local=(0, 1)), PAN_FRAME)
    # print orientation: back face (y=0) on bed -> rotate so +Y becomes +Z
    return m, m.rotate([-90, 0, 0]).rotate([180, 0, 0])


# ======================= ARM =======================
TZ = JOINT_T + DISC_R + 1.5   # tilt axis height: cradle tongue clears the arm bar
TILT_FRAME = [[0, 0, 1, ARM_PLATE_T / 2], [1, 0, 0, ARM_LEN], [0, 1, 0, TZ]]   # local z -> +x


def arm():
    bar = CrossSection.batch_hull([circ(DISC_R), circ(ARM_PLATE_T / 2, 0, ARM_LEN)]).extrude(JOINT_T)
    boss = Manifold.cylinder(B_T, DISC_R, DISC_R, SEG)
    vplate_cs = CrossSection.batch_hull([rect2(ARM_LEN - 12, ARM_LEN + 8, 0, JOINT_T),
                                         circ(DISC_R, ARM_LEN, TZ)])
    vplate = yz_extrude(vplate_cs, -ARM_PLATE_T / 2, ARM_PLATE_T / 2)
    tilt_add = vplate + _frame(joint_A_add(), TILT_FRAME)
    # tilt pin socket is horizontal in print; local +y of TILT_FRAME is world +z (up)
    tilt_cut = _frame(joint_A_cut(SOCKET_T, point_up_local=(0, 1)), TILT_FRAME)
    if ROLL_COMP:
        rc = lambda q: q.translate([0, -ARM_LEN, -TZ]).rotate([0, -ROLL_COMP, 0]).translate([0, ARM_LEN, TZ])
        tilt_add, tilt_cut = rc(tilt_add), rc(tilt_cut)
    m = bar + boss + tilt_add
    m = m - joint_B_cut()                                       # pan joint: underside of the boss
    m = m - tilt_cut
    return m, m, TZ


def fit_test():
    """Small A/B joint pair (uses one pin) to check the pin slide fit and tooth
    mesh before printing the real parts. Both print toothed face as shown."""
    a = Manifold.cylinder(SOCKET_T, DISC_R, DISC_R, SEG).translate([0, 0, -SOCKET_T]) + joint_A_add()
    a = a - joint_A_cut(SOCKET_T)
    b = Manifold.cylinder(B_T, DISC_R, DISC_R, SEG) - joint_B_cut()
    # grip tab on each so they're easy to pull apart / turn
    a = a + box(DISC_R - 3, DISC_R + 8, -4, 4, -SOCKET_T, -SOCKET_T + 3)
    b = b + box(DISC_R - 3, DISC_R + 8, -4, 4, TOOTH_CLR, TOOTH_CLR + 3)
    return a, b


# ======================= CRADLE =======================
def cradle():
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

    # tilt tongue under the floor, at the closed end: B side of the tilt joint
    ay, az = 9.0, -w - 16
    tongue_cs = CrossSection.batch_hull([rect2(-w, T + w, -w - 1, -w), circ(DISC_R, ay, az)])
    tongue = yz_extrude(tongue_cs, -w, -w + B_T)
    m = m + tongue
    CR_FRAME = [[0, 0, 1, -w], [1, 0, 0, ay], [0, 1, 0, az]]   # same orientation as TILT_FRAME
    m = m - _frame(joint_B_cut(), CR_FRAME)
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


CRADLE_AXIS = (9.0, -WALL - 16)


def magnet_stacks():
    """Magnet stacks for previews, in each part's own frame: (pin_tip, pocket) per joint."""
    st = Manifold.cylinder(MAG_STACK * MAG_T, MAG_D / 2, MAG_D / 2, 32)
    pin_tip = st.translate([0, 0, PIN_ENGAGE - MAG_STACK * MAG_T])
    pocket = st.translate([0, 0, PIN_ENGAGE + MAG_GAP + MAG_DEPTH - MAG_STACK * MAG_T])
    return pin_tip, pocket


def assemble(pan_deg=0, tilt_deg=0, mirror=False, seated=True):
    """World-space parts (wall at y=0, room at +y). Angles snap to the tooth pitch.
    Returns dict name -> Manifold."""
    step = 360 / TEETH
    pan_deg = round(pan_deg / step) * step
    tilt_deg = round(tilt_deg / step) * step
    drop = TOOTH_CLR if seated else 0.0
    wp, _ = wall_plate()
    am, _, tz = arm()
    cr, _, (ay, az) = cradle()
    pn = pin()
    tip, pocket = magnet_stacks()
    # cradle in the arm's frame: rotate about its tilt axis, then seat its tongue face on the tilt plate
    c_loc = lambda m: m.translate([0, -ay, -az]).rotate([tilt_deg, 0, 0]) \
        .translate([WALL + ARM_PLATE_T / 2 - drop, ARM_LEN, tz])
    tilt_pin = _frame(pn.translate([0, 0, -SOCKET_T]), TILT_FRAME)
    tilt_mag_a = _frame(tip, TILT_FRAME)
    tilt_mag_b = c_loc(_frame(pocket, [[0, 0, 1, -WALL], [1, 0, 0, ay], [0, 1, 0, az]]))
    arm_set = {"arm": am, "cradle": c_loc(cr), "tilt_pin": tilt_pin,
               "tilt_mag_a": tilt_mag_a, "tilt_mag_b": tilt_mag_b,
               "pan_mag_b": pocket}
    if mirror:
        arm_set = {k: v.mirror([1, 0, 0]) for k, v in arm_set.items()}
    place = lambda m: m.rotate([0, 0, pan_deg]).translate([0, PIV_Y, ZC - drop])
    out = {k: place(v) for k, v in arm_set.items()}
    out["wall_plate"] = wp
    out["pan_pin"] = _frame(pn.translate([0, 0, -SOCKET_T]), PAN_FRAME)
    out["pan_mag_a"] = _frame(tip, PAN_FRAME)
    return out


if __name__ == "__main__":
    import os
    out = "out"
    os.makedirs(out, exist_ok=True)
    _, wp_p = wall_plate()
    _, am_p, _ = arm()
    _, cr_p, _ = cradle()
    for name, m in [("wall_plate", on_bed(wp_p)), ("arm", on_bed(am_p)),
                    ("cradle_right", on_bed(cr_p)),
                    ("cradle_left", on_bed(cr_p.mirror([1, 0, 0]))),
                    ("pin", on_bed(pin())),
                    ("fit_test_A", on_bed(fit_test()[0])), ("fit_test_B", on_bed(fit_test()[1]))]:
        t = to_trimesh(m)
        t.export(f"{out}/{name}.stl")
        print(f"{name:13s} watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 "
              f"bbox={np.round(t.extents,1)}")
    print(f"notch {360/TEETH:.1f} deg, tooth height at rim {TOOTH_H:.2f} mm, "
          f"lift to re-aim {TOOTH_H + TOOTH_CLR + 0.3:.1f} mm, hole-side thickness {B_T:.1f} mm")
