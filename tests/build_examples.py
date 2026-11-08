"""Build the Rig Bridge example scene and verify every modular module.

blender -b --factory-startup --python build_examples.py -- <dir containing rig_bridge> <out.blend>

Each example is built the way a user would do it: select socket bone(s) in
Pose Mode, click a Module Asset (rig_bridge.add_module), Compile. Then it is
animated, and the behaviour is measured by stepping through the frames.
"""
import json
import math
import sys
import traceback

import bpy
from mathutils import Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
sys.path.insert(0, argv[0])
OUT = argv[1]
import rig_bridge  # noqa: E402

rig_bridge.register()
from rig_bridge import builder, modular  # noqa: E402

C = bpy.context
SC = C.scene
RESULTS = []
SC.frame_start, SC.frame_end = 1, 120
SC.render.fps = 24


def check(name, ok, info=""):
    RESULTS.append((name, bool(ok), info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")


# ---------------------------------------------------------------------------
# scene helpers
# ---------------------------------------------------------------------------

def coll(name):
    c = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    if c.name not in SC.collection.children:
        SC.collection.children.link(c)
    return c


def mat(name, rgb):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.diffuse_color = (*rgb, 1.0)
    return m


def skeleton(name, joints, collection):
    """joints: [(name, parent, head, tail)] world space."""
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    collection.objects.link(obj)
    arm.display_type = "STICK"
    C.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    for n, p, h, t in joints:
        e = arm.edit_bones.new(n)
        e.head, e.tail = Vector(h), Vector(t)
    for n, p, h, t in joints:
        if p:
            e = arm.edit_bones[n]
            e.parent = arm.edit_bones[p]
            e.use_connect = (e.parent.tail - e.head).length < 1e-5
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def skin(obj, skel):
    m = obj.modifiers.new("Armature", "ARMATURE")
    m.object = skel
    obj.parent = skel
    return obj


def tube(name, pts, radii, weights, skel, collection, material, segs=10):
    """Tube through pts; weights[i] = [(bone, w), ...] per ring."""
    pts = [Vector(p) for p in pts]
    verts, faces = [], []
    for i, p in enumerate(pts):
        d = (pts[min(i + 1, len(pts) - 1)] - pts[max(i - 1, 0)]).normalized()
        ref = Vector((0, 0, 1)) if abs(d.z) < 0.9 else Vector((1, 0, 0))
        u = d.cross(ref).normalized()
        v = d.cross(u).normalized()
        for k in range(segs):
            a = 2 * math.pi * k / segs
            verts.append(p + (u * math.cos(a) + v * math.sin(a)) * radii[i])
    for i in range(len(pts) - 1):
        for k in range(segs):
            a, b = i * segs + k, i * segs + (k + 1) % segs
            faces.append((a, b, b + segs, a + segs))
    verts.append(pts[0])
    verts.append(pts[-1])
    c0, c1 = len(verts) - 2, len(verts) - 1
    for k in range(segs):
        faces.append((c0, (k + 1) % segs, k))
        n = (len(pts) - 1) * segs
        faces.append((c1, n + k, n + (k + 1) % segs))
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    for poly in me.polygons:
        poly.use_smooth = True
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.data.materials.append(material)
    groups = {}
    ring_w = list(weights) + [weights[0], weights[-1]]
    for i, wl in enumerate(weights):
        for bn, w in wl:
            g = groups.get(bn) or obj.vertex_groups.new(name=bn)
            groups[bn] = g
            g.add(list(range(i * segs, (i + 1) * segs)), w, "REPLACE")
    for vi, wl in ((c0, weights[0]), (c1, weights[-1])):
        for bn, w in wl:
            g = groups.get(bn) or obj.vertex_groups.new(name=bn)
            groups[bn] = g
            g.add([vi], w, "REPLACE")
    return skin(obj, skel)


def chain_weights(skel, bones):
    """Ring weights for a tube along a chain: joint k between bone k-1 and k."""
    out = []
    n = len(bones)
    for k in range(n + 1):
        if k == 0:
            out.append([(bones[0], 1.0)])
        elif k == n:
            out.append([(bones[-1], 1.0)])
        else:
            out.append([(bones[k - 1], 0.5), (bones[k], 0.5)])
    return out


def chain_points(skel, bones):
    mw = skel.matrix_world
    b = skel.data.bones
    return [mw @ b[x].head_local for x in bones] + [mw @ b[bones[-1]].tail_local]


def box(name, center, size, bone, skel, collection, material):
    me = bpy.data.meshes.new(name)
    sx, sy, sz = size
    v = [(x * sx / 2, y * sy / 2, z * sz / 2) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    f = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    me.from_pydata(v, [], f)
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.location = center
    C.view_layer.update()
    me.transform(Matrix.Translation(Vector(center)))
    obj.location = (0, 0, 0)
    obj.data.materials.append(material)
    obj.vertex_groups.new(name=bone).add(list(range(8)), 1.0, "REPLACE")
    return skin(obj, skel)


def sphere(name, center, radius, bone, skel, collection, material, seg=24, ring=12):
    me = bpy.data.meshes.new(name)
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=ring, radius=radius)
    bmesh.ops.translate(bm, verts=bm.verts, vec=Vector(center))
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    obj.data.materials.append(material)
    obj.vertex_groups.new(name=bone).add(list(range(len(me.vertices))), 1.0, "REPLACE")
    return skin(obj, skel)


def label(text, loc, collection, size=0.18):
    cu = bpy.data.curves.new(f"LBL_{text[:20]}", "FONT")
    cu.body = text
    cu.size = size
    cu.align_x = "CENTER"
    o = bpy.data.objects.new(f"Label {text.splitlines()[0]}", cu)
    o.location = loc
    o.rotation_euler = (math.radians(90), 0, 0)
    collection.objects.link(o)
    o.data.materials.append(mat("Label", (0.9, 0.9, 0.9)))
    return o


# ---------------------------------------------------------------------------
# the user workflow, via the real operators
# ---------------------------------------------------------------------------

def activate(obj, mode="OBJECT"):
    builder.set_mode(C, obj, mode)


def drop(skel, module, bones):
    """Select socket bones in Pose Mode and click a Module Asset tile."""
    activate(skel, "POSE")
    for pb in skel.pose.bones:
        builder.pose_select(pb, pb.name in bones)
    skel.data.bones.active = skel.data.bones[bones[0]]
    r = bpy.ops.rig_bridge.add_module("EXEC_DEFAULT", module=module)
    return r


def compile_rig(skel):
    activate(skel, "POSE")
    r = bpy.ops.rig_bridge.build()
    activate(skel.rig_bridge.rig, "OBJECT")
    return r, skel.rig_bridge.rig


def key(rig, bone, frames_values, path="location"):
    """Keys given in WORLD axes (x right, -y forward, z up) are converted to the
    control's local rest axes, which is what pose channels use."""
    pb = rig.pose.bones[bone]
    R = (rig.matrix_world.to_3x3() @ pb.bone.matrix_local.to_3x3()).normalized()
    Ri = R.inverted()
    from mathutils import Euler
    for f, v in frames_values:
        if path == "rotation_euler":
            pb.rotation_mode = "XYZ"
            m = Ri @ Euler(v, "XYZ").to_matrix() @ R
            pb.rotation_euler = m.to_euler("XYZ")
        else:
            pb.location = Ri @ Vector(v)
        pb.keyframe_insert(path, frame=f)


def wp(obj, bone):
    return obj.matrix_world @ obj.pose.bones[bone].matrix


def head_w(obj, bone):
    return obj.matrix_world @ obj.pose.bones[bone].head


def tail_w(obj, bone):
    return obj.matrix_world @ obj.pose.bones[bone].tail


def rest_drift(skel, frame=1):
    SC.frame_set(frame)
    worst = 0.0
    for b in skel.data.bones:
        rest = skel.matrix_world @ b.matrix_local
        pose = wp(skel, b.name)
        worst = max(worst, (rest.translation - pose.translation).length)
    return worst


def run(frames):
    """Step frames sequentially (needed for soft body / cloth caches)."""
    out = {}
    for f in range(SC.frame_start, max(frames) + 1):
        SC.frame_set(f)
        if f in frames:
            yield f


def parts_summary(skel):
    return [(p.module, p.part_name, p.side, [b.bone for b in p.bones]) for p in skel.rig_bridge.parts]


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------

M_SKIN = mat("Skin", (0.85, 0.65, 0.5))
M_HAIR = mat("Hair", (0.35, 0.18, 0.08))
M_CLOTH = mat("Cloth", (0.6, 0.05, 0.08))
M_METAL = mat("Metal", (0.55, 0.58, 0.62))
M_CHROME = mat("Chrome", (0.9, 0.9, 0.95))
M_PAINT = mat("Paint", (0.95, 0.6, 0.05))
M_GREEN = mat("Green", (0.2, 0.6, 0.25))
M_EYE = mat("Eye", (0.95, 0.95, 0.95))


def ex_leg(x):
    col = coll("01 IK Leg")
    J = [("hip", None, (x, 0, 1.0), (x, 0, 1.12)),
         ("thigh_L", "hip", (x + 0.1, 0, 0.95), (x + 0.1, -0.03, 0.52)),
         ("shin_L", "thigh_L", (x + 0.1, -0.03, 0.52), (x + 0.1, 0.0, 0.1)),
         ("foot_L", "shin_L", (x + 0.1, 0.0, 0.1), (x + 0.1, -0.13, 0.03)),
         ("toe_L", "foot_L", (x + 0.1, -0.13, 0.03), (x + 0.1, -0.21, 0.03)),
         ("thigh_R", "hip", (x - 0.1, 0, 0.95), (x - 0.1, -0.03, 0.52)),
         ("shin_R", "thigh_R", (x - 0.1, -0.03, 0.52), (x - 0.1, 0.0, 0.1)),
         ("foot_R", "shin_R", (x - 0.1, 0.0, 0.1), (x - 0.1, -0.13, 0.03)),
         ("toe_R", "foot_R", (x - 0.1, -0.13, 0.03), (x - 0.1, -0.21, 0.03))]
    sk = skeleton("IKLeg_Skeleton", J, col)
    box("IKLeg_Hip", (x, 0, 1.02), (0.36, 0.2, 0.16), "hip", sk, col, M_PAINT)
    for s in "LR":
        bones = [f"thigh_{s}", f"shin_{s}"]
        tube(f"IKLeg_Leg_{s}", chain_points(sk, bones), [0.075, 0.06, 0.045], chain_weights(sk, bones), sk, col, M_SKIN)
        fx = x + (0.1 if s == "L" else -0.1)
        box(f"IKLeg_Foot_{s}", (fx, -0.06, 0.04), (0.09, 0.17, 0.07), f"foot_{s}", sk, col, M_PAINT)
        box(f"IKLeg_Toe_{s}", (fx, -0.18, 0.03), (0.09, 0.07, 0.05), f"toe_{s}", sk, col, M_PAINT)
    label("IK LEG\nLeg module (+ auto toe) x2, Control on hip\nfoot roll: rotate foot_heel_ik", (x, 0, 1.55), col, 0.09)
    # user workflow: Control on hip, Leg on left thigh, Mirror for right
    drop(sk, "control", ["hip"])
    drop(sk, "leg", ["thigh_L"])
    st = sk.rig_bridge
    st.active_part = len(st.parts) - 1
    r = bpy.ops.rig_bridge.part_mirror()
    check("leg: mirror Leg.L -> Leg.R", r == {"FINISHED"} and any(p.module == "leg" and p.side == "R" for p in st.parts),
          str(parts_summary(sk)))
    leg = next(p for p in st.parts if p.module == "leg" and p.side == "L")
    check("leg: connector event added toe", [b.bone for b in leg.bones] == ["thigh_L", "shin_L", "foot_L", "toe_L"],
          str([b.bone for b in leg.bones]))
    r, rig = compile_rig(sk)
    check("leg: compile", r == {"FINISHED"} and rig is not None)
    check("leg: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    # animation: walk-ish steps + hip bob + heel roll
    for s, ph in (("L", 0), ("R", 24)):
        vals = []
        for f in range(1, 121, 6):
            t = (f + ph) / 48.0 * 2 * math.pi
            vals.append((f, (0, -0.18 * math.sin(t), max(0.0, 0.12 * math.cos(t)))))
        key(rig, f"foot_ik.{s}", vals)
        key(rig, f"foot_heel_ik.{s}", [(f, (0.35 * math.sin((f + ph) / 48.0 * 2 * math.pi), 0, 0))
                                       for f in range(1, 121, 6)], "rotation_euler")
    key(rig, "hip", [(f, (0, 0, -0.05 + 0.03 * math.cos(f / 24.0 * 2 * math.pi))) for f in range(1, 121, 6)])
    return sk, rig


def ex_hair(x):
    col = coll("02 Dragging Hair")
    J = [("head", None, (x, 0, 1.5), (x, 0, 1.75))]
    starts = []
    for i, a in enumerate((-60, -30, 0, 30, 60)):
        r = math.radians(a)
        hx, hy = math.sin(r) * 0.11, math.cos(r) * 0.11
        prev = "head"
        p0 = Vector((x + hx, hy, 1.68))
        nm = "ABCDE"[i]
        for k in range(6):
            p1 = p0 + Vector((math.sin(r) * 0.02, 0.025, -0.075))
            n = f"hair_{nm}_{k + 1:02d}"
            J.append((n, prev, tuple(p0), tuple(p1)))
            prev = n
            p0 = p1
        starts.append(f"hair_{nm}_01")
    sk = skeleton("Hair_Skeleton", J, col)
    sphere("Hair_Head", (x, 0, 1.62), 0.12, "head", sk, col, M_SKIN)
    for s in starts:
        bones = [s[:-2] + f"{k:02d}" for k in range(1, 7)]
        tube(f"Hair_Strand_{s[5]}", chain_points(sk, bones), [0.018 - 0.0022 * k for k in range(7)],
             chain_weights(sk, bones), sk, col, M_HAIR, segs=6)
    label("DRAGGING HAIR\n5x Dynamic Chain (one click, multi-socket)\nslider 'dynamics' blends anim <-> sim",
          (x, 0, 2.05), col, 0.09)
    drop(sk, "control", ["head"])
    drop(sk, "dynamic_chain", starts)          # multi-select -> one module per socket
    st = sk.rig_bridge
    n_dyn = sum(1 for p in st.parts if p.module == "dynamic_chain")
    check("hair: multi-socket drop -> 5 modules", n_dyn == 5, f"{n_dyn}")
    r, rig = compile_rig(sk)
    check("hair: compile", r == {"FINISHED"} and rig is not None)
    check("hair: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    # head whips left/right then stops (frame 60+) so the hair settles
    key(rig, "head", [(1, (0, 0, 0)), (12, (0.35, 0, 0)), (24, (-0.35, 0, 0)), (36, (0.3, 0, 0.05)),
                      (48, (0, 0, 0)), (120, (0, 0, 0))])
    key(rig, "head", [(1, (0, 0, 0)), (12, (0, 0, 0.6)), (24, (0, 0, -0.6)), (36, (0, 0, 0.3)),
                      (48, (0, 0, 0)), (120, (0, 0, 0))], "rotation_euler")
    return sk, rig


def ex_cape(x):
    col = coll("03 Cape")
    J = [("torso", None, (x, 0, 1.0), (x, 0, 1.45))]
    starts = []
    for c, cx in enumerate((-0.24, -0.12, 0.0, 0.12, 0.24)):
        prev = "torso"
        p0 = Vector((x + cx, 0.13, 1.42))
        for k in range(7):
            p1 = p0 + Vector((cx * 0.12, 0.012, -0.13))
            n = f"cape_{c + 1:02d}_{k + 1:02d}"
            J.append((n, prev, tuple(p0), tuple(p1)))
            prev = n
            p0 = p1
        starts.append(f"cape_{c + 1:02d}_01")
    sk = skeleton("Cape_Skeleton", J, col)
    body = tube("Cape_Body", [(x, 0, 0.55), (x, 0, 1.0), (x, 0, 1.45), (x, 0, 1.58)], [0.1, 0.12, 0.12, 0.06],
                [[("torso", 1.0)]] * 4, sk, col, M_SKIN)
    sphere("Cape_Head", (x, 0, 1.68), 0.1, "torso", sk, col, M_SKIN)
    # visible cape mesh: grid over the joints, skinned rigidly per joint, subdivided
    rows = 8
    verts, faces, vg = [], [], []
    for c in range(5):
        bones = [f"cape_{c + 1:02d}_{k + 1:02d}" for k in range(7)]
        for k, p in enumerate(chain_points(sk, bones)):
            verts.append(tuple(p))
            vg.append(bones[max(k - 1, 0)] if k else bones[0])
    for c in range(4):
        for k in range(rows - 1):
            a, b = c * rows + k, (c + 1) * rows + k
            faces.append((a, b, b + 1, a + 1))
    me = bpy.data.meshes.new("Cape_Cloth")
    me.from_pydata(verts, [], faces)
    cape = bpy.data.objects.new("Cape_Cloth", me)
    col.objects.link(cape)
    cape.data.materials.append(M_CLOTH)
    for i, bn in enumerate(vg):
        g = cape.vertex_groups.get(bn) or cape.vertex_groups.new(name=bn)
        g.add([i], 1.0, "REPLACE")
    skin(cape, sk)
    cape.modifiers.new("Smooth", "SUBSURF").levels = 2
    cape.modifiers.new("Solidify", "SOLIDIFY").thickness = 0.008
    label("CAPE\nCape module: 5 chains auto-collected from one socket\ncloth + collision with body",
          (x, 0, 2.0), col, 0.09)
    drop(sk, "control", ["torso"])
    drop(sk, "cape", ["cape_03_01"])          # one socket -> array connector auto-resolves siblings
    st = sk.rig_bridge
    cp = next(p for p in st.parts if p.module == "cape")
    check("cape: array connector auto-collected 5 chains", len(cp.bones) == 5, str([b.bone for b in cp.bones]))
    r, rig = compile_rig(sk)
    check("cape: compile", r == {"FINISHED"} and rig is not None)
    check("cape: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    # body collider (proxy deformed by the control rig -> no dependency cycle)
    activate(sk, "OBJECT")
    for o in C.selected_objects:
        o.select_set(False)
    body.select_set(True)
    C.view_layer.objects.active = sk
    r = bpy.ops.rig_bridge.add_collider()
    prox = bpy.data.objects.get("COL-Cape_Body")
    check("cape: collider proxy created", r == {"FINISHED"} and prox is not None)
    # run forward, turn, stop
    key(rig, "torso", [(1, (0, 0, 0)), (30, (0, -0.8, 0)), (50, (0.3, -1.1, 0)), (70, (0.3, -1.1, 0)), (120, (0.3, -1.1, 0))])
    key(rig, "torso", [(1, (0, 0, 0)), (30, (0, 0, 0)), (50, (0, 0, 0.8)), (70, (0, 0, 0.8)), (120, (0, 0, 0.8))],
        "rotation_euler")
    return sk, rig


def ex_tail(x):
    col = coll("04 Tail")
    J = [("pelvis", None, (x, 0, 0.8), (x, 0.12, 0.8))]
    prev = "pelvis"
    p0 = Vector((x, 0.12, 0.8))
    tail = []
    for k in range(8):
        p1 = p0 + Vector((0, 0.11, 0.02 - 0.012 * k))
        n = f"tail_{k + 1:02d}"
        J.append((n, prev, tuple(p0), tuple(p1)))
        tail.append(n)
        prev = n
        p0 = p1
    sk = skeleton("Tail_Skeleton", J, col)
    box("Tail_Body", (x, -0.05, 0.8), (0.3, 0.4, 0.25), "pelvis", sk, col, M_GREEN)
    tube("Tail_Mesh", chain_points(sk, tail), [0.07 * (1 - k / 9) + 0.008 for k in range(9)],
         chain_weights(sk, tail), sk, col, M_GREEN)
    label("TAIL\nTail module: curl master + harmonic wave + dynamics", (x, 0, 1.35), col, 0.09)
    drop(sk, "control", ["pelvis"])
    drop(sk, "tail", ["tail_01"])
    st = sk.rig_bridge
    tp = next(p for p in st.parts if p.module == "tail")
    tp["s_wave"] = True
    tp["s_dynamics_enabled"] = True
    tp["s_stiffness"] = 0.55
    tp["s_stiffness_tip"] = 0.3
    r, rig = compile_rig(sk)
    check("tail: compile", r == {"FINISHED"} and rig is not None)
    m = rig.pose.bones["tail_master"]
    m["wave"] = 0.0
    rig.update_tag()
    d = rest_drift(sk)
    m["wave"] = 1.0
    rig.update_tag()
    check("tail: zero rest drift (wave muted)", d < 1e-4, f"{d:.2e}")
    SC.frame_set(30)
    C.view_layer.update()
    tip_a = tail_w(sk, "tail_08").copy()
    SC.frame_set(31)
    tip_b = tail_w(sk, "tail_08").copy()
    check("tail: wave animates the tip by itself", (tip_a - tip_b).length > 1e-3, f"{(tip_a - tip_b).length:.4f} m/frame")
    key(rig, "pelvis", [(1, (0, 0, 0)), (20, (0.25, 0, 0)), (40, (-0.25, 0, 0)), (60, (0, 0, 0)), (120, (0, 0, 0))])
    key(rig, "tail_master", [(1, (0, 0, 0)), (60, (0, 0, 0)), (80, (0.18, 0, 0)), (100, (-0.1, 0, 0)), (120, (0, 0, 0))],
        "rotation_euler")
    return sk, rig


def ex_piston(x):
    col = coll("05 Piston")
    pb_, pr_ = Vector((x, -0.08, 0.05)), Vector((x, -0.62, 0.27))
    d_ = (pr_ - pb_).normalized()
    J = [("base", None, (x, 0, 0.0), (x, 0, 0.3)),
         ("boom", "base", (x, 0, 0.3), (x, -1.0, 0.3)),
         ("piston_cylinder", "base", tuple(pb_), tuple(pb_ + d_ * 0.36)),
         ("piston_rod", "boom", tuple(pr_), tuple(pr_ - d_ * 0.44))]   # overlap -> rod slides inside
    sk = skeleton("Piston_Skeleton", J, col)
    box("Piston_Base", (x, 0, 0.12), (0.3, 0.3, 0.24), "base", sk, col, M_METAL)
    box("Piston_Boom", (x, -0.5, 0.3), (0.1, 1.05, 0.08), "boom", sk, col, M_PAINT)
    b = sk.data.bones
    mw = sk.matrix_world
    cyl = [mw @ b["piston_cylinder"].head_local, mw @ b["piston_cylinder"].tail_local]
    rod = [mw @ b["piston_rod"].head_local, mw @ b["piston_rod"].tail_local]
    tube("Piston_Cylinder", cyl, [0.035, 0.035], [[("piston_cylinder", 1.0)]] * 2, sk, col, M_METAL)
    # overlap check: the rod must stay inside the cylinder through the whole animation
    tube("Piston_Rod", rod, [0.018, 0.018], [[("piston_rod", 1.0)]] * 2, sk, col, M_CHROME)
    label("PISTON\nPiston module (rod auto-found)\nrotate the boom control", (x, 0, 1.0), col, 0.09)
    drop(sk, "control", ["base"])
    drop(sk, "control", ["boom"])
    drop(sk, "piston", ["piston_cylinder"])
    st = sk.rig_bridge
    pp = next(p for p in st.parts if p.module == "piston")
    check("piston: connector event found rod + rod socket",
          [b.bone for b in pp.bones][:3] == ["piston_cylinder", "piston_rod", "boom"], str([b.bone for b in pp.bones]))
    r, rig = compile_rig(sk)
    check("piston: compile", r == {"FINISHED"} and rig is not None)
    check("piston: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    key(rig, "boom", [(1, (0, 0, 0)), (30, (math.radians(-35), 0, 0)), (60, (math.radians(10), 0, 0)),
                      (90, (math.radians(-20), 0, 0)), (120, (0, 0, 0))], "rotation_euler")
    return sk, rig


def ex_eyes(x):
    col = coll("06 Aim (Eyes)")
    J = [("head", None, (x, 0, 1.4), (x, 0, 1.7)),
         ("eye_L", "head", (x + 0.045, -0.1, 1.58), (x + 0.045, -0.13, 1.58)),
         ("eye_R", "head", (x - 0.045, -0.1, 1.58), (x - 0.045, -0.13, 1.58))]
    sk = skeleton("Eyes_Skeleton", J, col)
    sphere("Eyes_Head", (x, 0, 1.55), 0.13, "head", sk, col, M_SKIN)
    for s, sx in (("L", 0.045), ("R", -0.045)):
        sphere(f"Eyes_Ball_{s}", (x + sx, -0.1, 1.58), 0.032, f"eye_{s}", sk, col, M_EYE, 16, 8)
        sphere(f"Eyes_Pupil_{s}", (x + sx, -0.128, 1.58), 0.012, f"eye_{s}", sk, col, mat("Pupil", (0.02, 0.02, 0.05)), 12, 6)
    label("AIM / LOOK AT\nAim on eye_L -> pair + master target", (x, 0, 1.95), col, 0.09)
    drop(sk, "control", ["head"])
    drop(sk, "aim", ["eye_L"])
    st = sk.rig_bridge
    ap = next(p for p in st.parts if p.module == "aim")
    check("aim: eye pair auto-resolved", sorted(b.bone for b in ap.bones) == ["eye_L", "eye_R"], str([b.bone for b in ap.bones]))
    r, rig = compile_rig(sk)
    check("aim: compile", r == {"FINISHED"} and rig is not None)
    check("aim: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    tgt = next(b.name for b in rig.data.bones if b.name.endswith("_target") and not b.name.startswith("MCH"))
    key(rig, tgt, [(1, (0, 0, 0)), (30, (0.6, 0, 0.2)), (60, (-0.6, 0, -0.2)), (90, (0, 0, 0.4)), (120, (0, 0, 0))])
    return sk, rig


def ex_tentacle(x):
    col = coll("07 IK Chain")
    J = [("base", None, (x, 0, 0), (x, 0, 0.1))]
    prev = "base"
    p0 = Vector((x, 0, 0.1))
    bones = []
    for k in range(6):
        p1 = p0 + Vector((0, -0.02 * (k % 2), 0.15))
        n = f"tentacle_{k + 1:02d}"
        J.append((n, prev, tuple(p0), tuple(p1)))
        bones.append(n)
        prev = n
        p0 = p1
    sk = skeleton("Tentacle_Skeleton", J, col)
    box("Tentacle_Base", (x, 0, 0.05), (0.25, 0.25, 0.1), "base", sk, col, M_METAL)
    tube("Tentacle_Mesh", chain_points(sk, bones), [0.05 * (1 - k / 8) + 0.01 for k in range(7)],
         chain_weights(sk, bones), sk, col, mat("Purple", (0.45, 0.2, 0.6)))
    label("IK CHAIN\nIK Chain module (6 bones, pole, IK/FK)", (x, 0, 1.3), col, 0.09)
    drop(sk, "control", ["base"])
    drop(sk, "ik_chain", ["tentacle_01"])
    r, rig = compile_rig(sk)
    check("ik_chain: compile", r == {"FINISHED"} and rig is not None)
    check("ik_chain: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    ik = next(b.name for b in rig.data.bones if b.name.endswith("_ik") and not b.name.startswith("MCH"))
    key(rig, ik, [(1, (0, 0, 0)), (30, (0.35, -0.2, -0.3)), (60, (-0.3, 0.1, -0.25)), (90, (0.1, -0.35, -0.1)),
                  (120, (0, 0, 0))])
    return sk, rig


def ex_try(x):
    """Un-rigged creature: open it to try the modular workflow yourself."""
    col = coll("09 Try It Yourself")
    J = []

    def add(n, p, h, t):
        J.append((n, p, (h[0] + x, h[1], h[2]), (t[0] + x, t[1], t[2])))
    add("pelvis", None, (0, 0, 0.9), (0, -0.1, 0.95))
    add("spine_01", "pelvis", (0, -0.1, 0.95), (0, -0.3, 1.0))
    add("spine_02", "spine_01", (0, -0.3, 1.0), (0, -0.5, 1.05))
    add("neck_01", "spine_02", (0, -0.5, 1.05), (0, -0.65, 1.3))
    add("head", "neck_01", (0, -0.65, 1.3), (0, -0.85, 1.32))
    for s, sx in (("l", 1), ("r", -1)):
        for nm, y0 in (("thigh", 0.0), ("thigh_front", -0.45)):
            pre = "pelvis" if nm == "thigh" else "spine_02"
            n0 = f"{nm}_{s}" if nm == "thigh" else f"upperarm_{s}"
            n1 = f"calf_{s}" if nm == "thigh" else f"lowerarm_{s}"
            n2 = f"foot_{s}" if nm == "thigh" else f"hand_{s}"
            add(n0, pre, (0.15 * sx, y0, 0.88), (0.17 * sx, y0 - 0.05, 0.5))
            add(n1, n0, (0.17 * sx, y0 - 0.05, 0.5), (0.17 * sx, y0 + 0.02, 0.1))
            add(n2, n1, (0.17 * sx, y0 + 0.02, 0.1), (0.17 * sx, y0 - 0.1, 0.02))
        p = "head"
        for k in range(3):
            add(f"ear_{s}_{k + 1:02d}", p, (0.05 * sx, -0.7 - 0.0 * k, 1.38 + 0.08 * k), (0.07 * sx, -0.7, 1.46 + 0.08 * k))
            p = f"ear_{s}_{k + 1:02d}"
    p = "pelvis"
    for k in range(6):
        add(f"tail_{k + 1:02d}", p, (0, 0.05 + 0.12 * k, 0.9 - 0.02 * k), (0, 0.17 + 0.12 * k, 0.88 - 0.02 * k))
        p = f"tail_{k + 1:02d}"
    sk = skeleton("TryMe_Skeleton", J, col)
    for b in sk.data.bones:
        h, t = sk.matrix_world @ b.head_local, sk.matrix_world @ b.tail_local
        r = 0.02 if "ear" in b.name else 0.05
        tube(f"TryMe_{b.name}", [h, t], [r, r * 0.8], [[(b.name, 1.0)]] * 2, sk, col,
             mat("Blue", (0.25, 0.45, 0.85)), segs=8)
    label("TRY IT YOURSELF\nselect TryMe_Skeleton > Pose Mode > N panel > Rig Bridge\n"
          "rings = empty sockets (+ suggestion). Select bones + click a tile,\n"
          "or click a tile then a ring. Or just Auto Rig. Then Compile.", (x, 0, 1.75), col, 0.07)
    sugg = modular.plan(sk, [])
    check("try-it: schematic offers sockets with suggestions", bool(sugg), str(sugg))
    return sk


def ex_character(x):
    """UE5-style humanoid + tail + ponytail + cape: everything from Auto Rig."""
    col = coll("08 Auto Rig Character")
    J = []

    def add(n, p, h, t):
        J.append((n, p, (h[0] + x, h[1], h[2]), (t[0] + x, t[1], t[2])))
    add("root", None, (0, 0, 0), (0, -0.2, 0))
    add("pelvis", "root", (0, 0, 0.97), (0, 0, 1.05))
    add("spine_01", "pelvis", (0, 0, 1.05), (0, 0, 1.17))
    add("spine_02", "spine_01", (0, 0, 1.17), (0, 0, 1.3))
    add("spine_03", "spine_02", (0, 0, 1.3), (0, 0, 1.45))
    add("neck_01", "spine_03", (0, 0, 1.45), (0, -0.01, 1.55))
    add("head", "neck_01", (0, -0.01, 1.55), (0, -0.01, 1.75))
    for s, sx in (("l", 1), ("r", -1)):
        add(f"clavicle_{s}", "spine_03", (0.03 * sx, 0, 1.42), (0.16 * sx, 0, 1.42))
        add(f"upperarm_{s}", f"clavicle_{s}", (0.16 * sx, 0, 1.42), (0.42 * sx, 0.02, 1.42))
        add(f"lowerarm_{s}", f"upperarm_{s}", (0.42 * sx, 0.02, 1.42), (0.66 * sx, 0, 1.42))
        add(f"hand_{s}", f"lowerarm_{s}", (0.66 * sx, 0, 1.42), (0.74 * sx, 0, 1.42))
        for f, fy in (("index", -0.025), ("middle", 0.0), ("ring", 0.02), ("pinky", 0.035)):
            p = f"hand_{s}"
            for k in range(3):
                h = (0.74 * sx + 0.03 * k * sx, fy, 1.42)
                t = (0.74 * sx + 0.03 * (k + 1) * sx, fy, 1.42)
                add(f"{f}_0{k + 1}_{s}", p, h, t)
                p = f"{f}_0{k + 1}_{s}"
        p = f"hand_{s}"
        for k in range(3):
            add(f"thumb_0{k + 1}_{s}", p, (0.69 * sx + 0.025 * k * sx, -0.04 - 0.015 * k, 1.41),
                (0.69 * sx + 0.025 * (k + 1) * sx, -0.055 - 0.015 * k, 1.41))
            p = f"thumb_0{k + 1}_{s}"
        add(f"thigh_{s}", "pelvis", (0.1 * sx, 0, 0.93), (0.1 * sx, -0.02, 0.5))
        add(f"calf_{s}", f"thigh_{s}", (0.1 * sx, -0.02, 0.5), (0.1 * sx, 0.01, 0.08))
        add(f"foot_{s}", f"calf_{s}", (0.1 * sx, 0.01, 0.08), (0.1 * sx, -0.11, 0.02))
        add(f"ball_{s}", f"foot_{s}", (0.1 * sx, -0.11, 0.02), (0.1 * sx, -0.18, 0.02))
    p = "pelvis"
    for k in range(6):
        add(f"tail_{k + 1:02d}", p, (0, 0.1 + 0.1 * k, 0.95 - 0.04 * k), (0, 0.2 + 0.1 * k, 0.91 - 0.04 * k))
        p = f"tail_{k + 1:02d}"
    p = "head"
    for k in range(5):
        add(f"ponytail_{k + 1:02d}", p, (0, 0.1 + 0.02 * k, 1.68 - 0.07 * k), (0, 0.12 + 0.02 * k, 1.61 - 0.07 * k))
        p = f"ponytail_{k + 1:02d}"
    for c, cx in (("l", 0.12), ("m", 0.0), ("r", -0.12)):
        p = "spine_03"
        for k in range(5):
            add(f"cape_{c}_{k + 1:02d}", p, (cx, 0.12, 1.42 - 0.12 * k), (cx * 1.1, 0.13, 1.30 - 0.12 * k))
            p = f"cape_{c}_{k + 1:02d}"
    sk = skeleton("Character_Skeleton", J, col)
    # rigid capsule per bone (enough to see deformation)
    for b in sk.data.bones:
        if b.name == "root":
            continue
        h, t = sk.matrix_world @ b.head_local, sk.matrix_world @ b.tail_local
        r = 0.012 if any(k in b.name for k in ("index", "middle", "ring", "pinky", "thumb")) else \
            0.02 if any(k in b.name for k in ("ponytail", "cape")) else 0.045
        if b.name == "head":
            sphere("Char_head", (h + t) / 2, 0.1, "head", sk, col, M_SKIN)
            continue
        m = M_HAIR if "ponytail" in b.name else M_CLOTH if "cape" in b.name else M_GREEN if "tail" in b.name else M_SKIN
        tube(f"Char_{b.name}", [h, t], [r, r * 0.85], [[(b.name, 1.0)]] * 2, sk, col, m, segs=8)
    label("AUTO RIG CHARACTER\nUE5 skeleton + tail + ponytail + cape\none click: Auto Rig -> Compile", (x, 0, 2.05), col, 0.09)
    activate(sk, "POSE")
    st = sk.rig_bridge
    # what the schematic shows before anything is placed
    sugg = modular.plan(sk, [])
    check("auto: first socket suggestion (root)", sugg.get("root") == "root", str(sugg))
    r = bpy.ops.rig_bridge.auto_rig()
    mods = {}
    for p in st.parts:
        mods[p.module] = mods.get(p.module, 0) + 1
    print("   auto rig modules:", mods)
    want = {"root": 1, "spine": 1, "neck_head": 1, "shoulder": 2, "arm": 2, "leg": 2, "finger": 10,
            "tail": 1, "dynamic_chain": 1, "cape": 1}
    check("auto: Auto Rig picked the right modules", all(mods.get(k, 0) == v for k, v in want.items()), str(mods))
    socks = modular.sockets(sk, [rig_bridge._part_spec(p) for p in st.parts])
    check("auto: no empty sockets left", not socks, str(socks))
    # mirror workflow: delete arm.R and mirror arm.L back
    i_r = next(i for i, p in enumerate(st.parts) if p.module == "arm" and p.side == "R")
    st.active_part = i_r
    bpy.ops.rig_bridge.part_remove()
    i_l = next(i for i, p in enumerate(st.parts) if p.module == "arm" and p.side == "L")
    st.active_part = i_l
    bpy.ops.rig_bridge.part_mirror()
    arm_r = next((p for p in st.parts if p.module == "arm" and p.side == "R"), None)
    check("auto: mirror Arm.L -> Arm.R", arm_r is not None and [b.bone for b in arm_r.bones] ==
          ["upperarm_r", "lowerarm_r", "hand_r"], str(arm_r and [b.bone for b in arm_r.bones]))
    # tail gets dynamics too
    tp = next(p for p in st.parts if p.module == "tail")
    tp["s_dynamics_enabled"] = True
    r, rig = compile_rig(sk)
    check("auto: compile (Rigify body + native tail/hair/cape)", r == {"FINISHED"} and rig is not None)
    check("auto: zero rest drift", rest_drift(sk) < 1e-4, f"{rest_drift(sk):.2e}")
    tree = rig_bridge.part_tree(sk, st)
    depth = {st.parts[i].module: d for i, d, _ in tree}
    check("auto: hierarchy root->spine->neck/legs", depth.get("root") == 0 and depth.get("spine") == 1
          and depth.get("leg") == 2, str(depth))
    key(rig, "torso", [(1, (0, 0, 0)), (25, (0.25, -0.3, -0.05)), (50, (-0.2, -0.6, 0)), (75, (0, -0.9, -0.05)),
                       (120, (0, -0.9, 0))])
    key(rig, "torso", [(1, (0, 0, 0)), (25, (0, 0, 0.5)), (50, (0, 0, -0.5)), (75, (0, 0, 0)), (120, (0, 0, 0))],
        "rotation_euler")
    key(rig, "upper_arm_fk.L", [(1, (0, 0, 0)), (40, (0.8, 0, 0.5)), (80, (-0.4, 0, 0)), (120, (0, 0, 0))],
        "rotation_euler")
    rig.pose.bones["upper_arm_parent.L"]["IK_FK"] = 1.0
    return sk, rig


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------

def measure(ex):
    leg_sk, leg_rig = ex["leg"]
    hair_sk, hair_rig = ex["hair"]
    cape_sk, cape_rig = ex["cape"]
    tail_sk, tail_rig = ex["tail"]
    pis_sk, pis_rig = ex["piston"]
    eye_sk, eye_rig = ex["eyes"]
    ten_sk, ten_rig = ex["tentacle"]
    chr_sk, chr_rig = ex["char"]

    # hair: compare against pure FK (dynamics 0) – record FK tips first
    hair_tip = "hair_C_06"
    hair_prop = next(p for p in hair_rig.pose.bones if "dynamics" in p.keys())
    dyn_props = [p for p in hair_rig.pose.bones if "dynamics" in p.keys()]
    def set_dyn(v):
        for p in dyn_props:
            p["dynamics"] = v
        hair_rig.update_tag()
        C.view_layer.update()
    set_dyn(0.0)
    fk_tip = {}
    for f in run({1, 14, 20, 30, 48, 100, 120}):
        fk_tip[f] = tail_w(hair_sk, hair_tip).copy()
    set_dyn(1.0)
    SC.frame_set(1)
    bpy.ops.ptcache.free_bake_all()

    samples = {}
    cape_low = {}
    tail_tips = {}
    piston = {}
    eyes = {}
    tent = {}
    leg = {}
    chr_dyn = {}
    col_prox = bpy.data.objects.get("COL-Cape_Body")
    cape_sim = next(o for o in bpy.data.objects if o.get("rb_kind") == "sim" and o.name.startswith("SIM-Cape"))
    hair_dyn_rig = bpy.data.objects.get("Hair_Skeleton_dyn")
    frames = {1, 14, 20, 30, 40, 48, 60, 75, 90, 100, 110, 120}
    min_clear = 1e9
    for f in run(frames | set(range(30, 71, 5))):
        samples[f] = tail_w(hair_sk, hair_tip).copy()
        # cape: bottom-middle bone tail vs torso; and cloth clearance from body axis
        dg = C.evaluated_depsgraph_get()
        ev = cape_sim.evaluated_get(dg)
        torso_m = wp(cape_sk, "torso")
        axis_o, axis_d = torso_m.translation, torso_m.to_3x3().col[1].normalized()
        for v in ev.data.vertices:
            p = ev.matrix_world @ v.co
            rel = p - axis_o
            along = rel.dot(axis_d)
            if -0.35 < along < 0.4:
                min_clear = min(min_clear, (rel - axis_d * along).length)
        cape_low[f] = (tail_w(cape_sk, "cape_03_07").copy(), torso_m.translation.copy(), torso_m.to_3x3().col[2].copy())
        tail_tips[f] = tail_w(tail_sk, "tail_08").copy()
        # piston geometry
        cy, ro = wp(pis_sk, "piston_cylinder"), wp(pis_sk, "piston_rod")
        pc, pr = cy.translation, ro.translation
        cy_y = cy.to_3x3().normalized().col[1]
        ro_y = ro.to_3x3().normalized().col[1]
        attach = (pis_rig.matrix_world @ pis_rig.pose.bones[[b for b in pis_rig.pose.bones.keys()
                                                             if b.startswith("MCH-") and "anchor_rod" in b][0]].head)
        piston[f] = (math.degrees(cy_y.angle(pr - pc)), math.degrees(ro_y.angle(pc - pr)), (pr - pc).length,
                     (pr - attach).length, 0.36 + 0.44 - (pr - pc).length)
        # eyes
        mchs = sorted(n for n in eye_rig.pose.bones.keys() if n.startswith("MCH-eye_aim"))
        for i, mn in enumerate(mchs):
            mch = eye_rig.pose.bones[mn]
            t = eye_rig.pose.bones[f"eye_target.{i + 1:02d}"]
            eyes.setdefault(f, []).append(math.degrees(
                (eye_rig.matrix_world.to_3x3() @ mch.y_axis).angle(eye_rig.matrix_world @ t.head - head_w(eye_rig, mn))))
            # the eyeball (source bone) must move with its aim bone
            eyes[f].append(math.degrees((wp(eye_sk, "eye_L" if i == 0 else "eye_R").to_3x3().normalized().col[1])
                                        .angle(eye_rig.matrix_world @ t.head - head_w(eye_rig, mn))))
        # tentacle tip vs IK target
        ikname = next(b.name for b in ten_rig.data.bones if b.name.endswith("_ik") and not b.name.startswith("MCH"))
        tent[f] = (tail_w(ten_sk, "tentacle_06") - head_w(ten_rig, ikname)).length
        # leg: knee forward of hip-ankle line when foot lifted, source foot follows ik
        leg[f] = (wp(leg_sk, "foot_L").translation - head_w(leg_rig, "ORG-foot.L")).length
        chr_dyn[f] = (tail_w(chr_sk, "ponytail_05").copy(), tail_w(chr_sk, "cape_m_05").copy(), tail_w(chr_sk, "tail_06").copy())

    lag = (samples[14] - fk_tip[14]).length
    check("hair: tip drags behind animation while head moves", lag > 0.03, f"{lag:.3f} m off FK at f14")
    still = (samples[120] - samples[110]).length
    sag = (samples[120] - fk_tip[120]).length
    check("hair: comes to rest after the head stops", still < 0.005, f"moved {still * 1000:.1f} mm f110->f120")
    check("hair: hangs under gravity, close to its pose", sag < 0.15, f"{sag:.3f} m sag vs. pose")
    check("hair: no explosion / NaN", all(math.isfinite(v) for p in samples.values() for v in p), "")
    # blend 0 == FK exactly
    set_dyn(0.0)
    SC.frame_set(20)
    e0 = (tail_w(hair_sk, hair_tip) - fk_tip[20]).length
    check("hair: dynamics=0 reproduces pure animation", e0 < 1e-5, f"{e0:.2e}")
    set_dyn(1.0)

    # cape: when torso runs forward (-Y) the cape bottom trails behind (+Y relative)
    low, tor, up = cape_low[30]
    trail = (low - tor).y
    rest_low, rest_tor, _ = cape_low[1]
    check("cape: bottom trails behind while running", trail - (rest_low - rest_tor).y > 0.05,
          f"rel y rest {(rest_low - rest_tor).y:+.3f} -> f30 {trail:+.3f}")
    check("cape: cloth stays outside the body collider", min_clear > 0.1, f"min clearance {min_clear:.3f} m (body r=0.12)")

    # tail: wave + dynamics move the tip even while pelvis rests (f100..120 is static pelvis, master animates)
    moved = (tail_tips[60] - tail_tips[48]).length
    check("tail: tip moving (wave/dynamics/curl)", moved > 0.02, f"{moved:.3f} m")
    check("tail: no explosion", all(math.isfinite(v) for p in tail_tips.values() for v in p))

    # piston
    a_err = max(max(v[0], v[1]) for v in piston.values())
    lengths = [v[2] for v in piston.values()]
    att = max(v[3] for v in piston.values())
    check("piston: cylinder & rod always aim at each other", a_err < 0.5, f"max {a_err:.3f}°")
    check("piston: rod pivot stays on the boom", att < 1e-4, f"{att:.2e} m")
    ov = min(v[4] for v in piston.values())
    check("piston: rod never leaves the cylinder", ov > 0.03, f"min overlap {ov:.3f} m")
    check("piston: extends/retracts with the boom", max(lengths) - min(lengths) > 0.05,
          f"pivot distance {min(lengths):.3f}..{max(lengths):.3f} m")
    # eyes
    e_err = max(max(v) for v in eyes.values())
    check("aim: eyes point at their targets", e_err < 0.5, f"max {e_err:.3f}°")
    # tentacle
    reach = [tent[f] for f in (30, 60, 90)]
    check("ik_chain: tip reaches the IK control", max(reach) < 0.02, f"{[round(r, 4) for r in reach]}")
    check("leg: source foot follows Rigify foot", max(leg.values()) < 1e-3, f"{max(leg.values()):.2e}")
    # character dynamics move
    mv = [(chr_dyn[50][i] - chr_dyn[40][i]).length for i in range(3)]
    check("auto: ponytail/cape/tail simulate", all(m > 0.005 for m in mv), str([round(m, 3) for m in mv]))


def measure_leg_ik(leg_sk, leg_rig):
    """Static IK checks on the leg (frame 1 pose + manual edit, then restore)."""
    SC.frame_set(1)
    pb = leg_rig.pose.bones["foot_ik.L"]
    old = pb.location.copy()
    act = leg_rig.animation_data.action
    leg_rig.animation_data.action = None
    pb.location = (0, 0, 0.3)
    C.view_layer.update()
    knee = head_w(leg_sk, "shin_L")
    hip = head_w(leg_sk, "thigh_L")
    ank = head_w(leg_sk, "foot_L")
    mid = hip + (ank - hip) * 0.5
    check("leg: IK knee bends forward", (knee - mid).y < -0.05, f"knee offset y {(knee - mid).y:+.3f}")
    pb.location = (0, 0, 0)
    heel = leg_rig.pose.bones["foot_heel_ik.L"]
    heel.rotation_mode = "XYZ"
    toe0 = tail_w(leg_sk, "toe_L").copy()
    heel.rotation_euler = (0.5, 0, 0)
    C.view_layer.update()
    toe1 = tail_w(leg_sk, "toe_L")
    heel.rotation_euler = (0, 0, 0)
    check("leg: foot roll moves the toe", (toe1 - toe0).length > 0.02, f"{(toe1 - toe0).length:.3f} m")
    leg_rig.animation_data.action = act


def rebuild_keeps_animation(sk):
    rig = sk.rig_bridge.rig
    act = rig.animation_data.action
    activate(sk, "POSE")
    r = bpy.ops.rig_bridge.build()
    rig2 = sk.rig_bridge.rig
    check("rebuild: same rig object, animation kept", r == {"FINISHED"} and rig2 == rig and
          rig2.animation_data and rig2.animation_data.action == act, f"{act and act.name}")
    sims = [o for o in bpy.data.objects if o.get("rb_src") == sk.name and o.get("rb_kind") == "sim"]
    check("rebuild: no duplicate sim objects", len(sims) == 5, f"{len(sims)}")


def bake_dynamics_to_skeleton(sk):
    activate(sk, "POSE")
    SC.frame_set(1)
    ref = {}
    for f in run({20, 40}):
        ref[f] = tail_w(sk, "hair_A_06").copy()
    SC.frame_set(1)
    r = bpy.ops.rig_bridge.bake()
    errs = []
    for f in run({20, 40}):
        errs.append((tail_w(sk, "hair_A_06") - ref[f]).length)
    check("bake: dynamics baked onto the skeleton", r == {"FINISHED"} and max(errs) < 0.01,
          f"{[round(e, 4) for e in errs]}")
    # restore live binding for the example file
    sk.animation_data.action = None
    bpy.ops.rig_bridge.mute(mute=False)


def finish_scene():
    # camera framing all examples
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    SC.collection.objects.link(cam)
    cam.location = (12.2, -18, 1.6)
    cam.rotation_euler = (math.radians(88), 0, 0)
    cam.data.lens = 24
    SC.camera = cam
    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
    SC.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    floor = bpy.data.objects.new("Floor", bpy.data.meshes.new("Floor"))
    floor.data.from_pydata([(-2, -3, 0), (27, -3, 0), (27, 3, 0), (-2, 3, 0)], [], [(0, 1, 2, 3)])
    SC.collection.objects.link(floor)
    floor.data.materials.append(mat("Floor", (0.25, 0.25, 0.27)))
    SC.frame_set(1)
    bpy.ops.ptcache.free_bake_all()
    for o in bpy.data.objects:
        if o.type == "ARMATURE" and o.rig_bridge.rig:
            o.hide_set(False)
    # leave the hair rig active in pose mode with the panel content visible
    activate(bpy.data.objects["Hair_Skeleton"].rig_bridge.rig, "POSE")
    txt = bpy.data.texts.new("README Rig Bridge examples")
    txt.write(README)


README = """Rig Bridge examples
====================
Press PLAY (Space) from frame 1: dynamics (hair, cape, tail, character) simulate
while playing. Scrubbing backwards needs a replay or Animate > Bake.

Every example was built with the modular workflow:
  1. select the skeleton, Pose Mode
  2. select socket bone(s), click a tile in Rig Bridge > Module Assets
     (or click a tile with nothing selected, then click a socket in the viewport)
  3. Compile

01 IK Leg       Control(hip) + Leg(thigh_L, toe auto) + Mirror -> Leg.R.
                Animated: foot_ik.L/R steps, foot_heel_ik roll, hip bob.
02 Hair         Control(head) + 5x Dynamic Chain from ONE multi-socket drop.
                Slider 'dynamics' (Animate panel) blends anim <-> sim.
03 Cape         Control(torso) + Cape on one chain: siblings auto-collected.
                Body collider = 'Make Collider' proxy driven by the rig.
04 Tail         Control(pelvis) + Tail (curl master, wave, dynamics).
05 Piston       Control(base) + Control(boom) + Piston(cylinder; rod found).
06 Aim          Aim on eye_L -> both eyes + master target.
07 IK Chain     6-bone IK with pole + IK_FK slider.
08 Character    ONE click Auto Rig on a UE5-style skeleton with tail,
                ponytail and cape -> Rigify body + native physics modules.
09 Try it       Un-rigged creature. Select it, Pose Mode, Rig Bridge panel:
                rings in the viewport are empty sockets with a suggestion.

Select any *_Skeleton and open the sidebar (N) > Rig Bridge to see the Module
Hierarchy, the connectors and the settings. Change a setting and click Compile.
"""


def main():
    ex = {}
    try:
        ex["leg"] = ex_leg(0.0)
        ex["hair"] = ex_hair(3.0)
        ex["cape"] = ex_cape(6.0)
        ex["tail"] = ex_tail(9.0)
        ex["piston"] = ex_piston(12.0)
        ex["eyes"] = ex_eyes(15.0)
        ex["tentacle"] = ex_tentacle(18.0)
        ex["char"] = ex_character(21.0)
        ex["try"] = ex_try(24.5)
        measure_leg_ik(*ex["leg"])
        measure(ex)
        rebuild_keeps_animation(ex["hair"][0])
        bake_dynamics_to_skeleton(ex["hair"][0])
    except Exception:
        traceback.print_exc()
        check("exception", False)
    try:
        finish_scene()
    except Exception:
        traceback.print_exc()
    bpy.ops.wm.save_as_mainfile(filepath=OUT)
    print("\nSUMMARY:", sum(ok for _, ok, _ in RESULTS), "/", len(RESULTS), "passed")


main()
