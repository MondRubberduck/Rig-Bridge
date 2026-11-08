"""One-click control rig on every skeleton standard Rig Bridge shares with
Bulletproof Retarget, plus Bake to Control Rig (FK / IK legs / full IK).

blender -b --factory-startup --python tests/test_standards.py -- <dir containing rig_bridge> <out.blend> [bpr checkout]

Every case: build the skeleton -> key a procedural clip on the skeleton ->
press Compile once -> Bake to Control Rig -> the skeleton, now driven only by
the control rig, must reproduce the clip in all three IK/FK modes.
"""
import json
import math
import os
import sys
import traceback

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if argv:
    sys.path.insert(0, argv[0])
OUT = argv[1] if len(argv) > 1 else ""
BPR = argv[2] if len(argv) > 2 else ""
import rig_bridge  # noqa: E402

if not os.environ.get("RB_NO_MAIN"):
    rig_bridge.register()
from rig_bridge import builder, stdmatch  # noqa: E402

C = bpy.context
RESULTS = []
LAST = {}


def check(name, ok, info=""):
    RESULTS.append((name, bool(ok), info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}", flush=True)


# ---------------------------------------------------------------------------
# canonical humanoid (metres, Z up, -Y forward, T-pose)
# ---------------------------------------------------------------------------
POS = {
    "root": (0, 0, 0), "hips": (0, 0, 1.0), "spine": (0, 0, 1.1), "spine1": (0, 0, 1.2),
    "spine2": (0, 0, 1.3), "chest": (0, 0, 1.38), "neck": (0, -0.01, 1.5), "neck1": (0, -0.01, 1.56),
    "head": (0, -0.01, 1.63), "head_end": (0, -0.01, 1.83),
}
SIDE = {"shoulder": (0.03, 0, 1.45), "upper_arm": (0.17, 0.0, 1.45), "forearm": (0.44, 0.02, 1.45),
        "hand": (0.70, 0, 1.45), "thigh": (0.1, 0, 0.95), "shin": (0.1, -0.02, 0.52),
        "foot": (0.1, 0.02, 0.09), "toe": (0.1, -0.11, 0.02), "toe_end": (0.1, -0.18, 0.02)}
FING = {"thumb": (0.72, -0.045, 1.43), "index": (0.79, -0.03, 1.45), "middle": (0.795, -0.008, 1.45),
        "ring": (0.79, 0.014, 1.45), "pinky": (0.78, 0.034, 1.45)}
for s, k in (("l", 1), ("r", -1)):
    for j, p in SIDE.items():
        POS[f"{j}_{s}"] = (p[0] * k, p[1], p[2])
    for f, p in FING.items():
        step = 0.028 if f != "thumb" else 0.025
        for i in range(4):
            dy = -0.012 * i if f == "thumb" else 0.0
            POS[f"{f}_{i + 1:02d}_{s}"] = ((p[0] + step * i) * k, p[1] + dy, p[2] - (0.006 * i if f == "thumb" else 0))

PARENT = {"root": None, "hips": "root", "spine": "hips", "spine1": "spine", "spine2": "spine1", "chest": "spine2",
          "neck": "chest", "neck1": "neck", "head": "neck1", "head_end": "head"}
for s in "lr":
    PARENT.update({f"shoulder_{s}": "chest", f"upper_arm_{s}": f"shoulder_{s}", f"forearm_{s}": f"upper_arm_{s}",
                   f"hand_{s}": f"forearm_{s}", f"thigh_{s}": "hips", f"shin_{s}": f"thigh_{s}",
                   f"foot_{s}": f"shin_{s}", f"toe_{s}": f"foot_{s}", f"toe_end_{s}": f"toe_{s}"})
    for f in FING:
        PARENT[f"{f}_01_{s}"] = f"hand_{s}"
        for i in (2, 3, 4):
            PARENT[f"{f}_{i:02d}_{s}"] = f"{f}_{i - 1:02d}_{s}"


def a_pose(pos, deg=40):
    """Lower both arms about the shoulder joint (A-pose variant)."""
    out = dict(pos)
    for s, k in (("l", 1), ("r", -1)):
        piv = Vector(pos[f"upper_arm_{s}"])
        R = Matrix.Rotation(math.radians(deg * k), 3, "Y")
        for j in pos:
            if j.endswith(f"_{s}") and any(j.startswith(x) for x in ("forearm", "hand", "thumb", "index", "middle",
                                                                     "ring", "pinky")):
                out[j] = tuple(piv + R @ (Vector(pos[j]) - piv))
    return out


def spec_from_standard(std, pose="T"):
    """[(name, parent name, head)] using the first candidate name of every joint."""
    pos = a_pose(POS) if pose == "A" else POS
    names = {j: c[0] for j, c in std.bones.items() if c}
    rows = []
    for j in PARENT:
        if j not in names:
            continue
        p = PARENT[j]
        while p is not None and p not in names:
            p = PARENT[p]
        rows.append([names[j], names.get(p) if p else None, Vector(pos[j]), j])
    return rows


def add(rows, name, parent, head, before=None):
    rows.append([name, parent, Vector(head), None])
    return rows


def reparent(rows, child, parent):
    for r in rows:
        if r[0] == child:
            r[1] = parent


def find(rows, joint):
    return next(r[0] for r in rows if r[3] == joint)


def head_of(rows, name):
    return next(r[2] for r in rows if r[0] == name)


def insert_between(rows, upper, lower, name, frac=0.5):
    h = head_of(rows, upper).lerp(head_of(rows, lower), frac)
    add(rows, name, upper, h)
    reparent(rows, lower, name)


def make_armature(name, rows, style="chain", obj_matrix=None, collection=None):
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    (collection or C.scene.collection).objects.link(obj)
    if obj_matrix is not None:
        obj.matrix_world = obj_matrix
    C.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    inv = obj.matrix_world.inverted()
    kids = {}
    for r in rows:
        kids.setdefault(r[1], []).append(r)
    ebs = {}
    for n, p, h, j in rows:
        e = arm.edit_bones.new(n)
        hl = inv @ h
        ch = [c for c in kids.get(n, []) if (c[2] - h).length > 1e-4]
        if style == "sideways":            # FBX-like: every bone points along +X
            d = inv.to_3x3() @ Vector((0.04, 0, 0))
        elif ch:
            d = inv @ ch[0][2] - hl
        elif p is not None:
            d = (hl - inv @ head_of(rows, p)).normalized() * (inv.to_3x3() @ Vector((0, 0, 0.04))).length
        else:
            d = inv.to_3x3() @ Vector((0, 0, 0.1))
        if d.length < 1e-5:
            d = inv.to_3x3() @ Vector((0, 0, 0.03))
        e.head, e.tail = hl, hl + d
        ebs[n] = e
    for n, p, h, j in rows:
        if p:
            ebs[n].parent = ebs[p]
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def ue_fixture(which):
    path = os.path.join(BPR, "tests", "fixtures", "unreal_skeletons.json")
    with open(path, encoding="utf-8") as fh:
        bones = json.load(fh)[which]
    rot, pos = [None] * len(bones), [None] * len(bones)
    for i, b in enumerate(bones):
        x, y, z, w = b["pose"][:4]
        q, t = Quaternion((w, x, y, z)), Vector(b["pose"][4:7])
        if b["parent"] < 0:
            rot[i], pos[i] = q, t
        else:
            rot[i], pos[i] = rot[b["parent"]] @ q, pos[b["parent"]] + rot[b["parent"]] @ t
    heads = [Vector((p.x, -p.y, p.z)) * 0.01 for p in pos]   # UE: left-handed, cm
    rows = [[b["name"], bones[b["parent"]]["name"] if b["parent"] >= 0 else None, heads[i], None]
            for i, b in enumerate(bones)]
    return rows


# ---------------------------------------------------------------------------
# per-standard skeleton quirks (hierarchy facts documented in the standards)
# ---------------------------------------------------------------------------

def q_cc(rows, pre="CC_Base_"):
    hips = find(rows, "hips")
    add(rows, pre + "Pelvis", hips, head_of(rows, hips))
    for s in "LR":
        reparent(rows, f"{pre}{s}_Thigh", pre + "Pelvis")
        add(rows, f"{pre}{s}_UpperarmTwist01", f"{pre}{s}_Upperarm", head_of(rows, f"{pre}{s}_Upperarm"))
        add(rows, f"{pre}{s}_ForearmTwist01", f"{pre}{s}_Forearm",
            head_of(rows, f"{pre}{s}_Forearm").lerp(head_of(rows, f"{pre}{s}_Hand"), 0.5))
        add(rows, f"{pre}{s}_ThighTwist01", f"{pre}{s}_Thigh", head_of(rows, f"{pre}{s}_Thigh"))
        add(rows, f"{pre}{s}_CalfTwist01", f"{pre}{s}_Calf",
            head_of(rows, f"{pre}{s}_Calf").lerp(head_of(rows, f"{pre}{s}_Foot"), 0.5))
    add(rows, pre + "FacialBone", find(rows, "head"), head_of(rows, find(rows, "head")))


def q_daz8(rows):
    hips = find(rows, "hips")
    add(rows, "pelvis", hips, head_of(rows, hips))
    for s in "lr":
        reparent(rows, f"{s}ThighBend", "pelvis")
        insert_between(rows, f"{s}ShldrBend", f"{s}ForearmBend", f"{s}ShldrTwist")
        insert_between(rows, f"{s}ForearmBend", f"{s}Hand", f"{s}ForearmTwist")
        insert_between(rows, f"{s}ThighBend", f"{s}Shin", f"{s}ThighTwist")
        hand = head_of(rows, f"{s}Hand")
        add(rows, f"{s}Carpal1", f"{s}Hand", hand.lerp(head_of(rows, f"{s}Index1"), 0.4))
        add(rows, f"{s}Carpal2", f"{s}Hand", hand.lerp(head_of(rows, f"{s}Ring1"), 0.4))
        for f, c in (("Index", 1), ("Mid", 1), ("Ring", 2), ("Pinky", 2)):
            reparent(rows, f"{s}{f}1", f"{s}Carpal{c}")


def q_daz9(rows):
    for s in "lr":
        add(rows, f"{s}_upperarmtwist1", f"{s}_upperarm", head_of(rows, f"{s}_upperarm"))
        add(rows, f"{s}_forearmtwist1", f"{s}_forearm",
            head_of(rows, f"{s}_forearm").lerp(head_of(rows, f"{s}_hand"), 0.5))
        for f in ("index", "mid", "ring", "pinky"):
            h = head_of(rows, f"{s}_hand").lerp(head_of(rows, f"{s}_{f}1"), 0.5)
            add(rows, f"{s}_{f}_metacarpal", f"{s}_hand", h)
            reparent(rows, f"{s}_{f}1", f"{s}_{f}_metacarpal")


def q_cmu(rows):
    for s, side in (("L", "Left"), ("R", "Right")):
        add(rows, f"{s}HipJoint", "Hips", head_of(rows, "Hips"))
        reparent(rows, f"{side}UpLeg", f"{s}HipJoint")


def q_biped(rows):
    pre = "Bip01 "
    add(rows, "Bip01", None, (0, 0, 1.0))
    reparent(rows, pre + "Pelvis", "Bip01")
    for s in "LR":
        reparent(rows, f"{pre}{s} Clavicle", pre + "Neck")
        reparent(rows, f"{pre}{s} Thigh", pre + "Spine")


def q_mmd(rows):
    root, hips, spine = find(rows, "root"), find(rows, "hips"), find(rows, "spine")
    add(rows, "センター", root, (0, 0, 0.75))
    reparent(rows, hips, "センター")
    add(rows, "腰", hips, (0, 0.02, 1.0))
    reparent(rows, spine, "腰")
    add(rows, "下半身", "腰", (0, 0.02, 1.0))
    for s in "LR":
        reparent(rows, f"足.{s}", "下半身")


def q_hik(rows):
    for side in ("Left", "Right"):
        add(rows, f"{side}ArmRoll", f"{side}Arm", head_of(rows, f"{side}Arm").lerp(head_of(rows, f"{side}ForeArm"), .5))
        add(rows, f"{side}UpLegRoll", f"{side}UpLeg",
            head_of(rows, f"{side}UpLeg").lerp(head_of(rows, f"{side}Leg"), .5))


def q_opti(rows):
    for r in rows:
        r[0] = "Skeleton_" + r[0]
        if r[1]:
            r[1] = "Skeleton_" + r[1]


def q_mixamo(rows):
    for r in rows:
        r[0] = r[0].replace("mixamorig:", "mixamorig1:")
        if r[1]:
            r[1] = r[1].replace("mixamorig:", "mixamorig1:")


MIXAMO_M = Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(0.01, 4)

CASES = [
    # (standard id, quirk, pose, bone style, object matrix)
    ("MIXAMO", q_mixamo, "T", "chain", "mixamo"),
    ("UE4_MANNEQUIN", None, "A", "sideways", None),
    ("UE5_MANNY", "fixture", None, None, None),
    ("UEFN_MANNEQUIN", "fixture", None, None, None),
    ("RENDERPEOPLE", None, "A", "chain", None),
    ("CC_ACTORCORE", q_cc, "A", "sideways", None),
    ("ACCURIG_MB", lambda r: q_cc(r, ""), "T", "chain", None),
    ("DAZ_G8", q_daz8, "T", "sideways", None),
    ("DAZ_G9", q_daz9, "A", "chain", None),
    ("VROID", None, "A", "chain", None),
    ("VRM_HUMANOID", None, "T", "chain", None),
    ("RIGIFY_META", None, "A", "chain", None),
    ("RIGIFY_DEF", None, "T", "chain", None),
    ("ARP_EXPORT", None, "A", "chain", None),
    ("CMU_BVH", q_cmu, "T", "sideways", None),
    ("HUMANIK", q_hik, "T", "chain", None),
    ("OPTITRACK", q_opti, "T", "chain", None),
    ("MAX_BIPED", q_biped, "A", "sideways", None),
    ("CATS_VRCHAT", None, "A", "chain", None),
    ("MMD", q_mmd, "A", "sideways", None),
]


# ---------------------------------------------------------------------------
# procedural clip on the skeleton (world-axis rotations about each rest frame)
# ---------------------------------------------------------------------------

def motion(j, t):
    """(euler degrees XYZ in world axes, world location offset) for canonical joint j at phase t."""
    s, c = math.sin(t), math.cos(t)
    side = 1 if j.endswith("_l") else -1
    base = j.rsplit("_", 1)[0] if j.endswith(("_l", "_r")) else j
    if j == "hips":
        return (4 * s, 0, 15 * s), Vector((0.02 * s, -0.1 * (1 - c), 0.03 * s))
    if j in ("spine", "spine1", "spine2", "chest"):
        return (6 * s, 3 * c, 5 * s), None
    if j in ("neck", "neck1"):
        return (0, 0, 10 * s), None
    if j == "head":
        return (10 * c, 5 * s, 0), None
    if base == "shoulder":
        return (0, 8 * s * side, 6 * c), None
    if base == "upper_arm":
        return (15 * c, 50 * side + 15 * s * side, 25 * s), None
    if base == "forearm":                      # hinge: flex in the rest arm plane
        return ("HINGE", 35 + 20 * s), None
    if base == "hand":
        return (20 * s, 10 * c, 15 * s), None
    if base == "thigh":
        return (-30 * s * side, 5 * c, 8 * s), None
    if base == "shin":                          # hinge: flex in the rest leg plane
        return ("HINGE", 25 + 25 * (1 + s)), None
    if base == "foot":
        return (15 * s, 0, 5 * c), None
    if base == "toe":
        return (12 * c, 0, 0), None
    for f in FING:
        if base.startswith(f) and not base.endswith("04"):
            return (0, (25 + 20 * s) * side, 0), None
    return None, None


def key_clip(obj, res, frames=range(1, 41, 4), period=40):
    obj.animation_data_create()
    act = bpy.data.actions.new(f"{obj.name}_clip")
    obj.animation_data.action = act
    mw3 = obj.matrix_world.to_3x3()
    rot_w = mw3.normalized()
    hinge = {}
    for s_ in "lr":
        for up, mid, lo, sign in (("upper_arm", "forearm", "hand", -1), ("thigh", "shin", "foot", 1)):
            js = [res.get(f"{x}_{s_}") for x in (up, mid, lo)]
            if all(j in obj.data.bones for j in js):
                h = [obj.matrix_world @ obj.data.bones[j].head_local for j in js]
                a, b = (h[1] - h[0]).normalized(), (h[2] - h[1]).normalized()
                n = a.cross(b)
                if n.length < 1e-4:          # straight at rest: elbow back, knee forward
                    fwd = Vector((0, -1, 0)) * sign
                    n = a.cross(fwd)
                hinge[f"{mid}_{s_}"] = n.normalized()
    for f in frames:
        t = 2 * math.pi * (f - 1) / period
        for j, bn in res.items():
            if bn not in obj.pose.bones:
                continue
            e, loc = motion(j, t)
            if e is None:
                continue
            pb = obj.pose.bones[bn]
            R3 = pb.bone.matrix_local.to_3x3().normalized()
            if e[0] == "HINGE":
                if j not in hinge:
                    continue
                Rw = Matrix.Rotation(math.radians(e[1]), 3, hinge[j])
            else:
                Rw = Euler([math.radians(x) for x in e], "XYZ").to_matrix()
            Ra = rot_w.inverted() @ Rw @ rot_w
            pb.rotation_mode = "QUATERNION"
            pb.rotation_quaternion = (R3.inverted() @ Ra @ R3).to_quaternion()
            pb.keyframe_insert("rotation_quaternion", frame=f)
            if loc is not None:
                pb.location = pb.bone.matrix_local.to_3x3().inverted() @ (mw3.inverted() @ loc)
                pb.keyframe_insert("location", frame=f)
    return act


def world(obj, bn):
    return obj.matrix_world @ obj.pose.bones[bn].matrix


def compare(src, ref, bones, frames, no_rot=()):
    """Max head error (m) and rotation error (deg) of src vs ref over frames."""
    for o in (src, ref):            # hidden objects may not be evaluated: compare only visible ones
        o.hide_set(False)
        o.hide_viewport = False
    pe = re = 0.0
    worst = ("", 0.0)
    worst_p = ""
    for f in frames:
        C.scene.frame_set(f)
        for b in bones:
            a, r = world(src, b), world(ref, b)
            d = (a.translation - r.translation).length
            q = a.to_quaternion().rotation_difference(r.to_quaternion())
            ang = math.degrees(q.angle)
            ang = min(ang, 360 - ang)
            if b in no_rot:
                continue
            if d > pe:
                pe, worst_p = d, b
            if ang > re:
                re, worst = ang, (b, ang)
    LAST["pos"] = worst_p
    return pe, re, worst


def run_case(sid, quirk, pose, style, objm, x):
    std = stdmatch.get(sid)
    coll = bpy.data.collections.new(sid)
    C.scene.collection.children.link(coll)
    if quirk == "fixture":
        rows = ue_fixture("UE5" if sid == "UE5_MANNY" else "UEFN")
        style = "chain"
    else:
        rows = spec_from_standard(std, pose)
        if quirk:
            quirk(rows)
    for r in rows:
        r[2] = r[2] + Vector((x, 0, 0))
    m = MIXAMO_M.copy() if objm == "mixamo" else None
    if m is not None:
        m.translation = (x, 0, 0)
        for r in rows:
            pass
    sk = make_armature(f"{sid}_skel", rows, style or "chain", m, coll)
    names = [b.name for b in sk.data.bones]
    _, res = stdmatch.score(std, names)
    act = key_clip(sk, res)
    ref = sk.copy()
    ref.name = f"{sid}_reference"
    coll.objects.link(ref)

    for o in C.selected_objects:
        o.select_set(False)
    C.view_layer.objects.active = sk
    sk.select_set(True)
    r = bpy.ops.rig_bridge.build()                                   # THE button
    st = sk.rig_bridge
    mods = {}
    for p in st.parts:
        mods[p.module] = mods.get(p.module, 0) + 1
    fingers_expected = 2 * sum(1 for f in FING if f"{f}_01_l" in std.bones and f"{f}_02_l" in std.bones) \
        if quirk != "fixture" else 10
    ok_body = all(mods.get(k, 0) == v for k, v in (("spine", 1), ("neck_head", 1), ("arm", 2), ("leg", 2)))
    check(f"{sid}: detected", st.used_profile == std.pid, f"{st.used_profile} | {st.status}")
    check(f"{sid}: one-click compile", r == {"FINISHED"} and st.rig is not None and ok_body
          and mods.get("finger", 0) >= fingers_expected, str(mods))
    rig = st.rig
    if rig is None:
        return None
    # rest pose unchanged
    sk.animation_data.action = None
    for pb in sk.pose.bones:
        pb.matrix_basis = Matrix()
    C.view_layer.update()
    drift = max((world(sk, b.name).translation - (sk.matrix_world @ b.matrix_local).translation).length
                for b in sk.data.bones)
    check(f"{sid}: rest pose unchanged", drift < 1e-4 * max(1.0, sk.matrix_world.to_scale().x * 100), f"{drift:.1e}")
    bound = [pb.name for pb in sk.pose.bones if any(c.name.startswith("RB_") for c in pb.constraints)]
    joints = [bn for bn in res.values() if bn in bound]
    # Daz Bend bones hand their roll to the in-chain Twist bone on purpose
    inchain = {b.name for b in sk.data.bones if any("twist" in c.name.lower() and any(
        x.name in bound for x in c.children_recursive) for c in b.children)}
    # middle neck bones (UE5 neck_02, Daz neckUpper…) have no own control in
    # Rigify's super_head: they are interpolated between neck and head
    mid_neck = {b.bone for p in st.parts if p.module == "neck_head" for b in list(p.bones)[:-1]} \
        if any(len(p.bones) > 2 for p in st.parts if p.module == "neck_head") else set()
    inchain |= mid_neck
    # bake the skeleton's clip onto the control rig
    sk.animation_data.action = act
    st.anim_source = None
    C.view_layer.objects.active = sk
    if os.environ.get("RB_DEBUG_BAKE"):
        a_, b_ = 1, 40
        builder.transfer_to_rig(C, sk, rig, sk, a_, b_, keep_helpers=True)
        C.scene.frame_set(11)
        for n in ("f_index.01.L", "f_index.02.L", "hand_fk.L", "forearm_fk.L"):
            if n in rig.pose.bones:
                cw = rig.matrix_world @ rig.pose.bones[n].matrix
                hw = rig.matrix_world @ rig.pose.bones["RETC-" + n].matrix
                q = cw.to_quaternion().rotation_difference(hw.to_quaternion())
                print("DBG", n, "ctrl vs RETC", round(math.degrees(q.angle), 2),
                      [(c.name, c.subtarget, c.mute, round(c.influence, 2)) for c in rig.pose.bones[n].constraints])
        bpy.ops.wm.save_as_mainfile(filepath=OUT)
        raise SystemExit
    r = bpy.ops.rig_bridge.transfer(mode="IK_LEGS")
    check(f"{sid}: Bake to Control Rig", r == {"FINISHED"} and rig.animation_data.action is not None)
    sk.animation_data.action = None          # from now on only the control rig moves the skeleton
    frames = [1, 6, 11, 17, 23, 29, 35]
    scale = 1.8 / max((world(ref, res["head"]).translation - world(ref, res["foot_l"]).translation).length, 1e-6)
    out = {}
    for mode in ("IK_LEGS", "IK", "FK"):
        builder.set_ik_mode(rig, mode, 1)
        pe, re_, worst = compare(sk, ref, joints, frames, inchain)
        worst_p = LAST.get("pos")
        out[mode] = (pe * scale, re_)
        if mid_neck:
            _, nr, nw = compare(sk, ref, sorted(mid_neck), frames)
            print(f"   info {sid}: middle neck bones interpolated, up to {nr:.1f}° ({nw[0]})")
        check(f"{sid}: {mode} reproduces the clip", pe * scale < 0.005 and re_ < 0.5,
              f"pos {pe * scale * 100:.3f} cm ({worst_p}), rot {re_:.3f}° ({worst[0]})")
    builder.set_ik_mode(rig, "IK_LEGS", 1)
    return sk, rig, out


def control_rig_cases(x):
    """A generated Rigify rig must be recognised and left alone."""
    bpy.ops.object.armature_human_metarig_add()
    meta = C.object
    meta.location.x = x
    bpy.ops.pose.rigify_generate()
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE" and o.name.startswith("rig"))
    for o in C.selected_objects:
        o.select_set(False)
    C.view_layer.objects.active = rig
    rig.select_set(True)
    r = bpy.ops.rig_bridge.auto_rig()
    st = rig.rig_bridge
    check("generated Rigify rig: recognised as a control rig, nothing stacked",
          len(st.parts) == 0 and "already a control rig" in st.status, st.status)
    try:
        r = bpy.ops.rig_bridge.build()
    except RuntimeError:
        r = {"CANCELLED"}
    check("generated Rigify rig: Compile refuses politely", r == {"CANCELLED"})
    meta.hide_set(True)
    rig.hide_set(True)


def main():
    src = stdmatch.db()["source"]
    print("standards from", src, flush=True)
    x = 0.0
    only = [c for c in os.environ.get("RB_CASES", "").split(",") if c]
    for case in CASES:
        if only and case[0] not in only:
            continue
        try:
            run_case(*case, x)
        except Exception:
            traceback.print_exc()
            check(f"{case[0]}: exception", False)
        x += 2.0
    try:
        if not only:
            control_rig_cases(x)
    except Exception:
        traceback.print_exc()
        check("control rig case: exception", False)
    C.scene.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=OUT)
    fails = [r for r in RESULTS if not r[1]]
    print("\nSUMMARY:", len(RESULTS) - len(fails), "/", len(RESULTS), "passed", flush=True)
    for f in fails:
        print("  FAILED", f[0], f[2])


if not os.environ.get("RB_NO_MAIN"):
    main()
