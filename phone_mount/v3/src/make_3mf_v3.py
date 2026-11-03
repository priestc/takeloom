"""Writes the ready-to-print Bambu Studio projects for v3 (one plate: wall plate, arm, cradle,
two knobs), with the three pauses and the solid-infill zones around the bolts already set.

The printer/filament/process settings are copied from v1's project (H2D, Generic PETG,
0.20 mm Standard, 4 walls, 30% gyroid, 5 mm outer brim), so the two versions print alike.

    python3 make_3mf_v3.py      # writes ../3mf/phone_mount_v3_{right,left}_project.3mf
"""
import io
import os
import zipfile

import numpy as np
from manifold3d import Manifold

import wall_phone_mount_v3 as m3

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "..", "v1", "3mf", "phone_mount_v1_right_captive_nuts_project.3mf")
OUT = os.path.join(HERE, "..", "3mf")
KEEP = ("[Content_Types].xml", "Metadata/project_settings.config", "Metadata/slice_info.config")
NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')
SOLID_R = 8.0     # radius of the 100%-infill zone around each bolt


# ---------- each part in print orientation, plus its solid-infill modifiers ----------
def _wall_plate():
    use, pr = m3.wall_plate()
    zmin = use.rotate([90, 0, 0]).bounding_box()[2]
    mod = Manifold.cylinder(m3.BOSS_T + 1, SOLID_R, SOLID_R, 48).translate([0, m3.PIV_Y, m3.FIN_Z - 0.5])
    return pr, [mod.rotate([90, 0, 0]).translate([0, 0, -zmin])]


def _arm(mirror):
    _, pr = m3.arm()
    pan = Manifold.cylinder(m3.CLAMP, SOLID_R, SOLID_R, 48).translate([0, 0, -m3.CLAMP])
    t = m3.BOSS_T + m3.ARM_FLARE
    tilt = Manifold.cylinder(t, SOLID_R, SOLID_R, 48).rotate([0, 90, 0]).translate([-t, m3.ARM_LEN, 0])
    mods = [x.translate([0, 0, m3.CLAMP]) for x in (pan, tilt)]
    if mirror:
        return pr.mirror([1, 0, 0]), [x.mirror([1, 0, 0]) for x in mods]
    return pr, mods


def _cradle(mirror):
    use, pr, (ay, az) = m3.cradle()
    w = m3.v1.WALL
    zmin = use.translate([w, 0, 0]).rotate([0, -90, 0]).bounding_box()[2]
    mod = Manifold.cylinder(m3.CLAMP, SOLID_R, SOLID_R, 48).rotate([0, 90, 0]).translate([-w, ay, az])
    mod = mod.translate([w, 0, 0]).rotate([0, -90, 0]).translate([0, 0, -zmin])
    if mirror:
        return pr.mirror([1, 0, 0]), [mod.mirror([1, 0, 0])]
    return pr, [mod]


# ---------- 3MF writing ----------
def _mesh_xml(man, oid, uuid, offset):
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties)[:, :3] - offset
    f = np.asarray(mesh.tri_verts)
    out = io.StringIO()
    out.write(f'  <object id="{oid}" p:UUID="{uuid}" type="model">\n   <mesh>\n    <vertices>\n')
    for x, y, z in v:
        out.write(f'     <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>\n')
    out.write('    </vertices>\n    <triangles>\n')
    for a, b, c in f:
        out.write(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
    out.write('    </triangles>\n   </mesh>\n  </object>\n')
    return out.getvalue(), len(f)


def write_project(path, mirror):
    side = "left" if mirror else "right"
    kb = m3.knob()
    objects = [  # name, part, modifiers, bed position (x, y), object-level settings
        ("wall_plate", *_wall_plate(), (75, 150), {}),
        (f"arm_{side}", *_arm(mirror), (150, 150) if not mirror else (165, 150), {}),
        (f"cradle_{side}", *_cradle(mirror), (245, 160), {}),
        ("knob_pan", kb, [], (60, 250), {"sparse_infill_density": "100%"}),
        ("knob_tilt", kb, [], (100, 250), {"sparse_infill_density": "100%"}),
    ]
    files, comps, settings, build, assemble, cuts = {}, [], [], [], [], []
    next_id = 1
    for n, (name, part, mods, (bx, by), obj_cfg) in enumerate(objects, start=1):
        b = part.bounding_box()
        offset = np.array([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2])
        height = b[5] - b[2]
        body, comp_xml, part_cfg = [], [], []
        for k, (mesh, subtype) in enumerate([(part, "normal_part")] + [(m, "modifier_part") for m in mods]):
            pid = next_id; next_id += 1
            uuid = f"000{n}{pid:04d}-81cb-4c03-9d28-80fed5dfa1dc"
            xml, nf = _mesh_xml(mesh, pid, uuid, offset)
            body.append(xml)
            comp_xml.append(f'    <component p:path="/3D/Objects/object_{n}.model" objectid="{pid}" '
                            f'p:UUID="000{n}{pid:04d}-b206-40ff-9872-83e8017abed1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>')
            if subtype == "normal_part":
                part_cfg.append(f'    <part id="{pid}" subtype="normal_part">\n'
                                f'      <metadata key="name" value="{name}"/>\n'
                                f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                                f'      <mesh_stat face_count="{nf}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
                                f'    </part>')
            else:
                part_cfg.append(f'    <part id="{pid}" subtype="modifier_part">\n'
                                f'      <metadata key="name" value="solid_infill_around_bolt"/>\n'
                                f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                                f'      <metadata key="extruder" value="1"/>\n'
                                f'      <metadata key="sparse_infill_density" value="100%"/>\n'
                                f'      <mesh_stat face_count="{nf}" edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" backwards_edges="0"/>\n'
                                f'    </part>')
        oid = next_id; next_id += 1
        files[f"3D/Objects/object_{n}.model"] = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
            f' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n' + "".join(body) +
            ' </resources>\n <build/>\n</model>\n')
        comps.append(f'  <object id="{oid}" p:UUID="0000000{n}-61cb-4c03-9d28-80fed5dfa1dc" type="model">\n'
                     f'   <components>\n' + "\n".join(comp_xml) + '\n   </components>\n  </object>')
        tr = f"1 0 0 0 1 0 0 0 1 {bx} {by} {height / 2:.4f}"
        build.append(f'  <item objectid="{oid}" p:UUID="0000000{n}-b1ec-4553-aec9-835e5b724bb4" transform="{tr}" printable="1"/>')
        assemble.append(f'   <assemble_item object_id="{oid}" instance_id="0" transform="{tr}" offset="0 0 0" />')
        extra = "".join(f'    <metadata key="{k}" value="{v}"/>\n' for k, v in obj_cfg.items())
        settings.append(f'  <object id="{oid}">\n    <metadata key="name" value="{name}"/>\n'
                        f'    <metadata key="extruder" value="1"/>\n{extra}' + "\n".join(part_cfg) + '\n  </object>')
        cuts.append(f' <object id="{oid}">\n  <cut_id id="0" check_sum="1" connectors_cnt="0"/>\n </object>')
    oids = [int(s.split('"')[1]) for s in comps]

    files["3D/3dmodel.model"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" {NS}>\n'
        ' <metadata name="Application">BambuStudio-02.08.02.61</metadata>\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n'
        f' <metadata name="Title">Takeloom phone mount v3 ({side})</metadata>\n'
        ' <resources>\n' + "\n".join(comps) + '\n </resources>\n'
        ' <build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">\n' + "\n".join(build) + '\n </build>\n</model>\n')
    files["3D/_rels/3dmodel.model.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n' +
        "".join(f' <Relationship Target="/3D/Objects/object_{n}.model" Id="rel-{n}" '
                'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n' for n in range(1, len(objects) + 1)) +
        '</Relationships>\n')
    files["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        '</Relationships>\n')
    instances = "".join(f'    <model_instance>\n      <metadata key="object_id" value="{o}"/>\n'
                        f'      <metadata key="instance_id" value="0"/>\n'
                        f'      <metadata key="identify_id" value="{100 + 10 * i}"/>\n    </model_instance>\n'
                        for i, o in enumerate(oids, start=1))
    files["Metadata/model_settings.config"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "\n".join(settings) + '\n  <plate>\n'
        '    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value=""/>\n'
        '    <metadata key="locked" value="false"/>\n    <metadata key="filament_map_mode" value="Manual"/>\n'
        '    <metadata key="filament_maps" value="1 1"/>\n    <metadata key="filament_volume_maps" value="0 0"/>\n'
        + instances + '  </plate>\n  <assemble>\n' + "\n".join(assemble) + '\n  </assemble>\n</config>\n')
    layers = "".join(f'<layer top_z="{h:.2f}" type="1" extruder="1" color="" extra="{what}" gcode="M400 U1"/>\n'
                     for h, what in m3.pauses())
    files["Metadata/custom_gcode_per_layer.xml"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n<plate_info id="1"/>\n'
        + layers + '<mode value="SingleExtruder"/>\n</plate>\n</custom_gcodes_per_layer>\n')
    files["Metadata/cut_information.xml"] = '<?xml version="1.0" encoding="utf-8"?>\n<objects>\n' + "\n".join(cuts) + '\n</objects>'
    files["Metadata/filament_sequence.json"] = '{"plate_1":{"nozzle_sequence":[],"optimal_assignment":[],"sequence":[]}}'

    with zipfile.ZipFile(TEMPLATE) as t, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in KEEP:
            z.writestr(name, t.read(name))
        for name, data in files.items():
            z.writestr(name, data)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for mirror in (False, True):
        p = os.path.join(OUT, f"phone_mount_v3_{'left' if mirror else 'right'}_project.3mf")
        write_project(p, mirror)
        print("wrote", os.path.normpath(p))
