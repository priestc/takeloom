"""
One-piece tripod cradle: the v1 cradle fused to a solid base that screws onto a
standard 1/4"-20 tripod screw. No joints and no M5 hardware; aim with the tripod head.

Strength choices
  * The base runs the FULL length of the cradle, so the cradle is supported along its
    whole length instead of hanging off a small boss.
  * Printed like the cradle, standing on its closed end. Every layer runs across the
    base-to-cradle junction, so that junction never relies on layer adhesion.
  * A steel 1/4"-20 hex nut is sealed inside the base (one pause), centred under the
    cradle + phone centre of gravity. The screw only pulls the nut toward the tripod,
    squeezing the 1.2 mm floor between nut and tripod plate (compression only).
  * Blind screw hole: even a long tripod screw stops 4 mm below the phone.

    python3 tripod_cradle.py      # writes STLs to ./out/
"""
import numpy as np
from manifold3d import Manifold, CrossSection
import wall_phone_mount as w

BASE_H = 15.0        # base depth below the cradle floor
NECK = True          # True: short tapered neck under the nut; False: full-length solid base
PAD_L = 40.0         # neck: length of the flat face that sits on the tripod
SCREW_XY = None      # (x, y) in the cradle frame; None = computed from the CG
T_NUT_AF = 11.4      # 1/4"-20 nut pocket across flats (nut 11.11)
T_NUT_H = 5.56
T_NUT_CAV = T_NUT_H + 0.4
T_FLOOR = 0.6        # plastic between the washer and the tripod plate (squeezed flat, nothing else)
WASHER_OD = 16.0     # M8 flat washer (DIN 125): 8.4 ID x 16 OD x 1.6 thick; set 0 for no washer
WASHER_T = 1.6
WASHER_CAV_D = WASHER_OD + 0.4
WASHER_CAV_T = WASHER_T + 0.3
SOLID_D = WASHER_OD + 4.0   # 100% infill column under the nut (project file)
T_HOLE_D = 6.7
TOP_STOP = 4.0       # blind hole stops this far below the phone's floor surface
DENS = 1.27 * 0.6 / 1000   # g/mm3, rough PETG part density for CG estimates

W_ = w.WALL
Z_BOT = -W_ - BASE_H   # base bottom face (sits on the tripod)


def _snap_up(z):
    return float(np.ceil(round(z / w.LAYER_H, 6)) * w.LAYER_H)


def phones():
    """(name, length, width, thickness, x-offset in cradle, mass g) seated in the cradle."""
    return [("iPhone 16 Pro Max", 163.0, 80.2, 12.2, 0.5, 270),
            ("Galaxy S20 FE", 159.8, 76.0, 10.0, 6.0, 220),
            ("iPhone 13 mini", 131.5, 66.0, 9.5, 13.0, 170)]


def _base_block(sy, sx=None):
    y1 = 2 * sy + W_                        # footprint centred on the screw in y
    if not NECK or sx is None:
        return w.box(-W_, w.CRADLE_LEN, -W_, y1, Z_BOT, -W_ + 0.01), y1
    # neck: flat pad under the screw, both ends tapered at 45 deg up to the cradle floor
    # (45 deg because the part prints standing on its end: no flat ceilings)
    a, b = sx - PAD_L / 2, sx + PAD_L / 2
    top = -W_ + 0.01
    prof = CrossSection([[(max(a - BASE_H, -W_), top), (a, Z_BOT), (b, Z_BOT), (min(b + BASE_H, w.CRADLE_LEN), top)]])
    neck = prof.extrude(y1 + W_).transform(np.array([[1, 0, 0, 0], [0, 0, -1, y1], [0, 1, 0, 0]]))
    return neck, y1


def screw_xy():
    if SCREW_XY is not None:
        return SCREW_XY
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    sx, sy = 74.0, 13.0
    for _ in range(3):                      # iterate: the base itself shifts the CG a little
        base, _ = _base_block(sy, sx)
        body = cr + base
        tb = w.to_trimesh(body); mb = tb.volume * DENS
        G = []
        for _, L, Wd, t, s0, mp in phones():
            tp = w.to_trimesh(w.box(s0, s0 + L, w.T - t, w.T, 0, Wd))
            G.append((tb.center_mass * mb + tp.center_mass * mp) / (mb + mp))
        G = np.mean(G, axis=0)
        sx, sy = round(float(G[0]), 1), round(float(G[1]), 1)
    return sx, sy


def roof_x(sx):
    """Nut/washer slot roof, in the cradle frame, snapped so it lands on a layer boundary in print."""
    R = max(T_NUT_AF / np.sqrt(3), WASHER_CAV_D / 2)
    return _snap_up(sx + R + 0.15 + W_) - W_


def nut_z0():
    """Bottom of the nut cavity (the washer sits below it)."""
    return Z_BOT + T_FLOOR + (WASHER_CAV_T if WASHER_OD else 0.0)


def pause_height():
    sx, _ = screw_xy()
    return roof_x(sx) + W_                  # print z = cradle x + WALL


def tripod_cradle(captive=True):
    cr, _, _ = w.cradle(nut_pocket=False, tongue=False)
    sx, sy = screw_xy()
    base, y1 = _base_block(sy, sx)
    m = cr + base
    R = T_NUT_AF / np.sqrt(3)
    z0 = nut_z0()
    zw = Z_BOT + T_FLOOR
    rw = WASHER_CAV_D / 2
    # screw hole: blind, horizontal in print -> teardrop pointing print-up (+x)
    hole = w.teardrop(T_HOLE_D / 2, (-W_ - TOP_STOP) - (Z_BOT - 1))     # along +z, point +y
    hole = hole.rotate([0, 0, -90]).translate([sx, sy, Z_BOT - 1])      # point -> +x
    m = m - hole
    hexcs = CrossSection.circle(R, 6)           # one corner along +x / -x (print up / down)
    if captive:
        xr = roof_x(sx)
        slot = CrossSection.batch_hull([hexcs.translate([sx, sy]),
                                        w.rect2(sx, xr, sy - T_NUT_AF / 2, sy + T_NUT_AF / 2)])
        m = m - slot.extrude(T_NUT_CAV).translate([0, 0, z0])
        if WASHER_OD:   # washer stands on edge in a round seat under the nut, same pause
            wslot = CrossSection.batch_hull([w.circ(rw, sx, sy), w.rect2(sx, xr, sy - rw, sy + rw)])
            m = m - wslot.extrude(WASHER_CAV_T + 0.01).translate([0, 0, zw])
    else:
        # open pocket: the nut slides in from the base's +y side face and the screw pulls it down
        slot = CrossSection.batch_hull([hexcs.rotate(30).translate([sx, sy]),
                                        w.rect2(sx - T_NUT_AF / 2, sx + T_NUT_AF / 2, sy, y1 + 1)])
        m = m - slot.extrude(T_NUT_CAV).translate([0, 0, z0])
        if WASHER_OD:   # washer slides in the same way, just below the nut
            wslot = CrossSection.batch_hull([w.circ(rw, sx, sy), w.rect2(sx - rw, sx + rw, sy, y1 + 1)])
            m = m - wslot.extrude(WASHER_CAV_T + 0.01).translate([0, 0, zw])
    pr = m.translate([W_, 0, 0]).rotate([0, -90, 0])
    pr = pr.translate([0, 0, -pr.bounding_box()[2]])
    return m, pr


if __name__ == "__main__":
    import os
    os.makedirs("out", exist_ok=True)
    print("tripod screw at x=%.1f y=%.1f (cradle frame)" % screw_xy())
    for name, captive in [("tripod_cradle_captive", True), ("tripod_cradle", False)]:
        _, pr = tripod_cradle(captive)
        t = w.to_trimesh(w.on_bed(pr)); t.export(f"out/{name}.stl")
        print(f"{name:27s} watertight={t.is_watertight} vol={t.volume/1000:.1f}cm3 bbox={np.round(t.extents, 1)}")
    print("captive: pause before the layer at %.1f mm to drop in the 1/4\"-20 nut" % pause_height())
