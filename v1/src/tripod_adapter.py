"""
Tripod adapter for an ALREADY-PRINTED v1 cradle. One printed piece, fixed (no tilt):
aim with the tripod head.

The cradle's tilt tongue drops into a fitted slot in a solid block, and the cradle's
floor rests on the block's flat top along its whole length. One M5 screw through the
tongue's existing hole locks it in. A 1/4"-20 nut sits under the cradle + phone centre
of gravity for the tripod screw.

Strength choices
  * Load goes cradle floor -> block top (bearing along the full length), not through the
    screw. The tongue is captured on both faces and around its rounded bottom, so it
    can't rotate even if the screw loosens.
  * Printed upright, flat bottom on the bed: the bending from the cantilevered phone
    runs along the layers, and bearing loads press layers together.
  * Both nuts drop into open shafts from the block's top face before the cradle goes
    on, so there are no print pauses. The cradle floor then covers the shafts.
  * The tripod screw pulls its nut down onto a 1.2 mm floor squeezed against the tripod
    plate (compression only).

    python3 tripod_adapter.py      # writes STLs to ./out/
"""
import numpy as np
from manifold3d import Manifold, CrossSection
import wall_phone_mount as w

# ---- measure your printed cradle's tongue (the flat tab under the closed end) ----
TONGUE_T_FIT = w.TONGUE_T   # 8.0 for prints made after the "pockets on both sides" change; 6.0 before
CLR = 0.2                   # slot clearance per side
CHEEK = 4.5                 # outer cheek on the closed-end side of the tongue
NUT_GAP = 1.0               # block material between the tongue slot and the M5 nut
FLOOR_UNDER_TONGUE = 3.0

T_NUT_AF = 11.4             # 1/4"-20 nut: 11.11 across flats -> pocket
T_FLOOR = 1.2
T_HOLE_D = 6.7
DENS = 1.27 * 0.6 / 1000

W_ = w.WALL
AY, AZ = 9.0, -W_ - 16      # tongue hole position (same in every v1 cradle)
Z_TOP = -W_                 # block top = cradle floor's underside
Z_BOT = AZ - w.DISC_R - CLR - FLOOR_UNDER_TONGUE
X0 = -W_ - CLR - CHEEK      # outer end of the block
X1 = w.CRADLE_LEN           # block runs the full length of the cradle


def phones():
    return [("iPhone 16 Pro Max", 163.0, 80.2, 12.2, 0.5, 270),
            ("Galaxy S20 FE", 159.8, 76.0, 10.0, 6.0, 220),
            ("iPhone 13 mini", 131.5, 66.0, 9.5, 13.0, 170)]


def _tongue_cs(grow=0.0):
    cs = CrossSection.batch_hull([w.rect2(-W_, w.T + W_, -W_ - 1, -W_), w.circ(w.DISC_R, AY, AZ)])
    return cs.offset(grow, CrossSection.JoinType.Round if hasattr(CrossSection, "JoinType") else None) if grow else cs


def _block(sy):
    return w.box(X0, X1, -W_, 2 * sy + W_, Z_BOT, Z_TOP)


def adapter(sx=None, sy=None):
    if sx is None:
        sx, sy = screw_xy()
    m = _block(sy)
    # tongue slot (opens through the top face)
    from manifold3d import JoinType
    slot_cs = CrossSection.batch_hull([w.rect2(-W_, w.T + W_, -W_ - 1, Z_TOP + 2), w.circ(w.DISC_R, AY, AZ)]) \
        .offset(CLR, JoinType.Round, circular_segments=64)
    # the tongue's flat side is flush with the screen-side face: open that side cleanly
    # (avoids a paper-thin sliver where the round bottom meets the face)
    slot_cs = slot_cs + w.rect2(-W_ - 2, AY, AZ - 6, Z_TOP + 2)
    m = m - w.yz_extrude(slot_cs, -W_ - CLR, -W_ + TONGUE_T_FIT + CLR)
    # M5 nut: drops into a shaft from the top, flats vertical, corner down into a V seat
    # that centres it on the screw axis
    af = w.NUT_R * np.sqrt(3)
    nx0 = -W_ + TONGUE_T_FIT + CLR + NUT_GAP
    shaft = CrossSection.batch_hull([CrossSection.circle(w.NUT_R, 6).rotate(90).translate([AY, AZ]),
                                     w.rect2(AY - af / 2, AY + af / 2, AZ, Z_TOP + 1)])
    m = m - w.yz_extrude(shaft, nx0, nx0 + w.NUT_CAV_H)
    # M5 screw hole along x: through the cheek and tongue slot, past the nut
    th = w.teardrop(w.BOLT_R, (nx0 + w.NUT_CAV_H + 8) - (X0 - 1))
    th = th.transform(np.array([[0, 0, 1, X0 - 1], [1, 0, 0, AY], [0, 1, 0, AZ]]))
    m = m - th
    # 1/4"-20 nut shaft from the top down to a thin floor, and the tripod screw hole
    m = m - CrossSection.circle(T_NUT_AF / np.sqrt(3), 6).extrude(Z_TOP + 1 - (Z_BOT + T_FLOOR)).translate([sx, sy, Z_BOT + T_FLOOR])
    m = m - Manifold.cylinder(T_FLOOR + 2, T_HOLE_D / 2, T_HOLE_D / 2, 48).translate([sx, sy, Z_BOT - 1])
    return m.simplify(1e-4)   # drops zero-area slivers from the slot cut so the STL is watertight


def screw_xy():
    cr, _, _ = w.cradle(nut_pocket=False)
    sx, sy = 74.0, 13.0
    for _ in range(3):
        body = cr + adapter(sx, sy)
        tb = w.to_trimesh(body); mb = tb.volume * DENS
        G = np.mean([(tb.center_mass * mb + w.to_trimesh(w.box(s0, s0 + L, w.T - t, w.T, 0, Wd)).center_mass * mp) / (mb + mp)
                     for _, L, Wd, t, s0, mp in phones()], axis=0)
        sx, sy = round(float(G[0]), 1), round(float(G[1]), 1)
    return sx, sy


def screw_length_note():
    nx0 = -W_ + TONGUE_T_FIT + CLR + NUT_GAP
    stack = (-W_ - X0) + (nx0 - (-W_)) + w.NUT_CAV_H   # head face -> far side of nut
    return stack


if __name__ == "__main__":
    import os
    os.makedirs("out", exist_ok=True)
    sx, sy = screw_xy()
    print(f"tripod screw at x={sx} y={sy} (cradle frame)")
    a = adapter(sx, sy)
    t = w.to_trimesh(w.on_bed(a)); t.export("out/tripod_adapter.stl")
    print(f"tripod_adapter  watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 bbox={np.round(t.extents, 1)}")
    print(f"M5: head face to far side of nut = {screw_length_note():.1f} mm -> M5x20 with a 1 mm washer")
