"""
Tripod adapter for the v1 cradle (cradle unchanged).

Replaces the wall plate + arm: an upright plate takes the cradle's tilt joint exactly
like the arm does (M5 x 20 screw, washer, nut on the adapter side), and a flat base
screws onto a standard 1/4"-20 tripod screw.

The 1/4"-20 thread is a steel hex nut sealed inside the base during printing, centred
under the cradle + phone centre of gravity, so the phone balances over the tripod.

    captive=True  : both nuts (1/4"-20 tripod nut and M5 tilt nut) are sealed in;
                    pause at pause_heights() to drop them in.
    captive=False : open hex pockets you press the nuts into after printing.

Regenerate with:
    python3 tripod_adapter.py      # writes STLs to ./out/
"""
import numpy as np
from manifold3d import Manifold, CrossSection
import wall_phone_mount as w

# ---------------- parameters (mm) ----------------
BASE_T = 6.0          # base slab thickness (cradle tongue clears it by 2 mm)
TZ = 20.0             # tilt axis height above the bed (same as the arm)
PLATE_T = w.ARM_PLATE_T
SCREW_XY = (83.0, 4.0)  # tripod screw position: under the cradle + phone CG
BOSS_R = 13.0         # boss around the tripod nut
BOSS_H = 12.0
BASE_HALF_W = 18.0    # base half-width at the plate

# 1/4"-20 UNC hex nut: 7/16" (11.11 mm) across flats, 7/32" (5.56 mm) thick
T_NUT_AF = 11.4       # pocket across flats incl. clearance
T_NUT_H = 5.56
T_NUT_CAV = T_NUT_H + 0.4
T_FLOOR = 1.2         # plastic between the tripod and the nut (in compression only)
T_HOLE_D = 6.7        # 1/4" screw clearance


def _snap_up(z):
    return float(np.ceil(round(z / w.LAYER_H, 6)) * w.LAYER_H)


def pause_heights():
    """Heights of the two cavity roofs (adapter printed flat). Pause before these layers."""
    return _snap_up(T_FLOOR + T_NUT_CAV), _snap_up(TZ + w.NUT_R + 0.15)


def adapter(captive=True):
    sx, sy = SCREW_XY
    # base: hull of a strip under the upright plate and a disc under the screw
    base_cs = CrossSection.batch_hull([w.rect2(-PLATE_T / 2, PLATE_T / 2, -BASE_HALF_W, BASE_HALF_W),
                                       w.circ(BOSS_R + 4, sx, sy)])
    base = base_cs.extrude(BASE_T)
    boss = Manifold.cylinder(BOSS_H, BOSS_R, BOSS_R, w.SEG).translate([sx, sy, 0])
    # upright tilt plate, same profile idea as the arm's (centred on y = 0)
    plate_cs = CrossSection.batch_hull([w.rect2(-14, 14, 0, BASE_T), w.circ(w.DISC_R, 0, TZ)])
    plate = w.yz_extrude(plate_cs, -PLATE_T / 2, PLATE_T / 2)
    m = base + boss + plate

    # tripod screw hole, straight through
    m = m - Manifold.cylinder(BOSS_H + 2, T_HOLE_D / 2, T_HOLE_D / 2, 48).translate([sx, sy, -1])
    # M5 tilt hole along x, teardrop pointing up
    th = w.teardrop(w.BOLT_R, 40).transform(np.array([[0, 0, 1, -20], [1, 0, 0, 0], [0, 1, 0, TZ]]))
    m = m - th

    t_hex = CrossSection.circle(T_NUT_AF / np.sqrt(3), 6)
    m_hex = CrossSection.circle(w.NUT_R, 6)
    if not captive:
        # 1/4"-20 nut pressed in from the top of the boss; the tripod screw pulls it down onto the floor
        m = m - t_hex.extrude(BOSS_H).translate([sx, sy, T_FLOOR])
        # M5 nut pocket in the plate's outer face (away from the cradle), one corner up
        m = m - w.yz_extrude(m_hex.rotate(90).translate([0, TZ]), -PLATE_T / 2 - 0.01, -PLATE_T / 2 + w.NUT_D)
        return m
    t_top, m_top = pause_heights()
    # sealed 1/4"-20 nut cavity
    m = m - t_hex.extrude(t_top - T_FLOOR).translate([sx, sy, T_FLOOR])
    # sealed M5 nut: on edge in a V seat, straight slot up to a flat roof (same as the captive arm)
    af = w.NUT_R * np.sqrt(3)
    slot = CrossSection.batch_hull([m_hex.rotate(90).translate([0, TZ]), w.rect2(-af / 2, af / 2, TZ, m_top)])
    x0 = -PLATE_T / 2 + 1.0
    m = m - w.yz_extrude(slot, x0, x0 + w.NUT_CAV_H)
    return m


def cradle_on_adapter(tilt=0.0, cradle=None):
    """The (right) cradle placed on the adapter at a tilt angle, in the adapter frame."""
    if cradle is None:
        cradle, _, _ = w.cradle(nut_pocket=False)
    ay, az = w.CRADLE_AXIS if hasattr(w, "CRADLE_AXIS") else (9.0, -w.WALL - 16)
    return cradle.translate([0, -ay, -az]).rotate([tilt, 0, 0]).translate([w.WALL + PLATE_T / 2, 0, TZ])


if __name__ == "__main__":
    import os
    os.makedirs("out", exist_ok=True)
    for name, m in [("tripod_adapter_captive", adapter(True)),
                    ("tripod_adapter_captive_left", adapter(True).mirror([1, 0, 0])),
                    ("tripod_adapter", adapter(False)),
                    ("tripod_adapter_left", adapter(False).mirror([1, 0, 0]))]:
        t = w.to_trimesh(w.on_bed(m)); t.export(f"out/{name}.stl")
        print(f"{name:28s} watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 bbox={np.round(t.extents, 1)}")
    print("pause before the layers at %.1f mm (1/4\"-20 tripod nut) and %.1f mm (M5 tilt nut)" % pause_heights())
