"""Renders and checks for v3. Writes PNGs to ../images/ and prints the checks.

    pip install manifold3d trimesh numpy matplotlib
    python3 render_v3.py
"""
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import wall_phone_mount_v3 as m3

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "..", "images")
COLORS = {"wall_plate": "#d9cfb8", "arm": "#c9b48f", "cradle": "#e6e0d2", "knob_pan": "#b2553f",
          "knob_tilt": "#b2553f", "knob": "#b2553f", "bolt_pan": "#8a8f96", "bolt_tilt": "#8a8f96"}


_RND = None


def _look_at(eye, target, up=(0, 0, 1)):
    f = np.asarray(target, float) - eye; f /= np.linalg.norm(f)
    r = np.cross(f, up); r /= np.linalg.norm(r)
    u = np.cross(r, f)
    pose = np.eye(4)
    pose[:3, 0], pose[:3, 1], pose[:3, 2], pose[:3, 3] = r, u, -f, eye
    return pose


def figure(parts, path, elev=20, azim=-60, title=None, size=(900, 760), bg=(1, 1, 1)):
    """parts: name -> (Manifold, color). Camera orbits the parts' centre (azim from +x, ccw)."""
    import trimesh
    import pyrender
    scene = pyrender.Scene(bg_color=list(bg) + [1], ambient_light=[0.35] * 3)
    pts = []
    for name, (man, color) in parts.items():
        t = m3.to_trimesh(man)
        pts.append(t.vertices)
        t = t.smooth_shaded
        rgb = list(matplotlib.colors.to_rgb(color))
        mat = pyrender.MetallicRoughnessMaterial(baseColorFactor=rgb + [1], metallicFactor=0.6 if "bolt" in name else 0.0,
                                                 roughnessFactor=0.35 if "bolt" in name else 0.75)
        scene.add(pyrender.Mesh.from_trimesh(t, material=mat, smooth=True))
    p = np.vstack(pts)
    lo, hi = p.min(0), p.max(0)
    c, r = (lo + hi) / 2, np.linalg.norm(hi - lo) / 2
    e, a = np.radians(elev), np.radians(azim)
    d = np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
    up = (0, 0, 1) if abs(elev) < 85 else (0, 1, 0)
    eye = c + d * r / np.tan(np.radians(10.5))
    scene.add(pyrender.PerspectiveCamera(yfov=np.radians(22), aspectRatio=size[0] / size[1]), pose=_look_at(eye, c, up))
    for de, da, inten in ((35, -40, 3.2), (10, 120, 1.6), (-50, 30, 0.9)):
        ee, aa = np.radians(elev + de), np.radians(azim + da)
        ld = np.array([np.cos(ee) * np.cos(aa), np.cos(ee) * np.sin(aa), np.sin(ee)])
        scene.add(pyrender.DirectionalLight(intensity=inten), pose=_look_at(c + ld * r * 3, c, up))
    global _RND
    if _RND is None:
        _RND = pyrender.OffscreenRenderer(*size)
    _RND.viewport_width, _RND.viewport_height = size
    color, depth = _RND.render(scene)
    color = color.copy(); color[depth == 0] = (np.array(bg) * 255).astype(np.uint8)
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1]); ax.imshow(color); ax.set_axis_off()
    if title:
        ax.text(0.03, 0.96, title, transform=ax.transAxes, fontsize=13, va="top")
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def checks():
    ok = True
    # 1. aiming range: free pan at tilt 0, free tilt at pan 0, and a grid inside those
    def hits(pan, tilt, mirror):
        a = m3.assemble(pan, tilt, mirror=mirror, hardware=True)
        names = [n for n in a if not n.startswith("bolt")]
        return [(names[i], names[j]) for i in range(len(names)) for j in range(i + 1, len(names))
                if (a[names[i]] ^ a[names[j]]).volume() > 1.0]
    def sweep(fn, rng):
        lo = hi = None
        for d in rng:
            if not fn(d):
                lo = d if lo is None else lo; hi = d
            elif lo is not None:
                break
        return lo, hi
    for mirror in (False, True):
        pan = sweep(lambda d: hits(d, 0, mirror), [d for d in range(-120, 121, 5)])
        tilt_up = max(d for d in range(0, 91, 5) if all(not hits(0, t, mirror) for t in range(0, d + 1, 5)))
        tilt_dn = min(d for d in range(0, -91, -5) if all(not hits(0, t, mirror) for t in range(0, d - 1, -5)))
        print(f"{'left' if mirror else 'right'} cradle: pan {pan[0]}..{pan[1]} deg at tilt 0; "
              f"tilt {tilt_dn} (camera down) .. +{tilt_up} (camera up) at pan 0")
    # 2. hardware sits only in its pockets/holes and the knob bores (a tiny overlap is mesh noise)
    a = m3.assemble(0, 0)
    for hw in ("bolt_pan", "bolt_tilt"):
        for p in ("wall_plate", "arm", "cradle", "knob_pan", "knob_tilt"):
            v = (a[hw] ^ a[p]).volume()
            if v > 0.5:
                ok = False
                print(f"  {hw} overlaps {p}: {v:.1f} mm3")
    # 3. bolt tips end inside the knobs: nothing of the bolt pokes out past the knob's outer face
    for hw, k, axis, sign in (("bolt_pan", "knob_pan", 2, -1), ("bolt_tilt", "knob_tilt", 0, 1)):
        bb, kb = a[hw].bounding_box(), a[k].bounding_box()
        tip = bb[axis] if sign < 0 else bb[axis + 3]
        face = kb[axis] if sign < 0 else kb[axis + 3]
        inside = (tip - face) * -sign
        print(f"  {hw}: tip is {inside:.1f} mm inside the knob's outer face")
        ok &= inside > 1.0
    tip_pan = m3.FIN_Z + m3.SKIN + m3.WASHER_T - m3.BOLT_L
    knob_out = m3.FIN_Z - m3.CLAMP - m3.KNOB_H
    print(f"pan bolt tip at {tip_pan - (m3.FIN_Z - m3.CLAMP):.1f} mm below the arm; "
          f"nut top at {-(m3.KNOB_FLOOR + m3.NUT_M):.1f}, bore top at {-m3.KNOB_BORE_TOP:.1f}, "
          f"knob outside at {knob_out - (m3.FIN_Z - m3.CLAMP):.1f}")
    return ok


def overhangs(man, name, limit_deg=46.0, ignore_below=0.3):
    """Area of downward-facing surface steeper than the limit, excluding the bed and the
    intentional bridge roofs (horizontal ceilings)."""
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3]
    f = np.asarray(mesh.tri_verts)
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(n, axis=1) / 2
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    zmin = tri[:, :, 2].min(1)
    down = n[:, 2] < -np.cos(np.radians(90 - limit_deg))
    flat_ceiling = n[:, 2] < -0.999
    bad = down & ~flat_ceiling & (zmin > ignore_below)
    print(f"  {name:11s} steep overhang area {area[bad].sum():6.1f} mm2 "
          f"(flat bridge ceilings {area[flat_ceiling & (zmin > ignore_below)].sum():.0f} mm2)")
    if bad.any():
        c = tri[bad].mean(1)
        print("     around", np.round(c[np.argsort(-area[bad])[:5]], 1).tolist())


if __name__ == "__main__":
    os.makedirs(IMG, exist_ok=True)
    what = sys.argv[1:] or ["checks", "images"]
    if "checks" in what:
        checks()
        _, wp = m3.wall_plate(); _, am = m3.arm(); kb = m3.knob(); _, cr, _ = m3.cradle()
        print("overhangs (print orientation):")
        for n_, mm in (("wall_plate", wp), ("arm", am), ("knob", kb), ("cradle", cr)):
            overhangs(m3.on_bed(mm), n_)
    if "images" in what:
        a = m3.assemble(25, -15)
        parts = {k: (v, COLORS[k]) for k, v in a.items()}
        figure(parts, f"{IMG}/v3_assembly.png", elev=18, azim=-20)
        figure(parts, f"{IMG}/v3_assembly_below.png", elev=-30, azim=-35)
        a0 = m3.assemble(0, 0)
        figure({k: (v, COLORS[k]) for k, v in a0.items() if k != "cradle"}, f"{IMG}/v3_joints.png", elev=12, azim=35)
        am, _ = m3.arm()
        figure({"arm": (am, COLORS["arm"])}, f"{IMG}/v3_arm.png", elev=25, azim=145)
        wp, _ = m3.wall_plate()
        figure({"wall_plate": (wp, COLORS["wall_plate"])}, f"{IMG}/v3_wall_plate.png", elev=20, azim=50)
        figure({"knob": (m3.knob(), COLORS["knob"])}, f"{IMG}/v3_knob.png", elev=40, azim=-60)
        # cutaways through both bolts
        cut = m3.box(-200, 0, -200, 200, -200, 200)
        ac = {k: (v ^ cut, COLORS[k]) for k, v in a0.items() if k in ("wall_plate", "arm", "knob_pan", "bolt_pan")}
        figure(ac, f"{IMG}/v3_pan_cutaway.png", elev=5, azim=0)
        cut2 = m3.box(-25, 35, -200, m3.PIV_Y + m3.ARM_LEN, m3.FIN_Z - 28, m3.FIN_Z + 22)
        ac2 = {k: (v ^ cut2, COLORS[k]) for k, v in a0.items() if k in ("arm", "cradle", "knob_tilt", "bolt_tilt")}
        figure(ac2, f"{IMG}/v3_tilt_cutaway.png", elev=5, azim=90)
        print("images written to", os.path.normpath(IMG))
