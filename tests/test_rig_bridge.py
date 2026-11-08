"""Headless test for Rig Bridge. Run:
blender -b --factory-startup --python test_rig_bridge.py -- <addon_parent_dir>
"""
import math
import sys
import traceback

import bpy
from mathutils import Euler, Matrix, Vector

addon_parent = sys.argv[sys.argv.index("--") + 1]
sys.path.insert(0, addon_parent)
import rig_bridge  # noqa: E402

rig_bridge.register()
from rig_bridge import builder, detect  # noqa: E402

RESULTS = []


def check(name, ok, info=""):
    RESULTS.append((name, ok, info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")


def make_armature(name, joints, matrix=Matrix.Identity(4), tail_mode="child"):
    """joints: list of (name, parent, world_head). Tails: towards first child."""
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    bpy.context.scene.collection.objects.link(obj)
    obj.matrix_world = matrix
    bpy.context.view_layer.update()
    inv = matrix.inverted()
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    heads = {n: Vector(h) for n, p, h in joints}
    kids = {}
    for n, p, h in joints:
        kids.setdefault(p, []).append(n)
    for n, p, h in joints:
        e = arm.edit_bones.new(n)
        e.head = inv @ heads[n]
        if tail_mode == "child" and kids.get(n):
            t = heads[kids[n][0]]
            if (t - heads[n]).length < 1e-4:
                t = heads[n] + Vector((0, 0, 0.05))
        else:  # UE-style arbitrary tails
            t = heads[n] + Vector((0.05, 0, 0)) if tail_mode == "x" else heads[n] + Vector((0, 0, 0.05))
        e.tail = inv @ t
    for n, p, h in joints:
        if p:
            arm.edit_bones[n].parent = arm.edit_bones[p]
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def mirror(joints, ltok, rtok):
    out = []
    for n, p, h in joints:
        out.append((n, p, h))
        if ltok in n:
            out.append((n.replace(ltok, rtok), p.replace(ltok, rtok) if p else p, (-h[0], h[1], h[2])))
    return out


def mixamo_joints(prefix="mixamorig:"):
    P = lambda s: prefix + s if s else None
    J = [("Hips", None, (0, 0, 1.0)), ("Spine", "Hips", (0, 0, 1.1)), ("Spine1", "Spine", (0, 0, 1.22)),
         ("Spine2", "Spine1", (0, 0, 1.35)), ("Neck", "Spine2", (0, 0, 1.5)), ("Head", "Neck", (0, -0.01, 1.58)),
         ("HeadTop_End", "Head", (0, -0.01, 1.78)), ("LeftEye", "Head", (0.03, -0.08, 1.66)),
         ("LeftShoulder", "Spine2", (0.05, 0, 1.45)), ("LeftArm", "LeftShoulder", (0.17, 0, 1.45)),
         ("LeftForeArm", "LeftArm", (0.45, 0, 1.45)), ("LeftHand", "LeftForeArm", (0.72, 0, 1.45)),
         ("LeftUpLeg", "Hips", (0.09, 0, 0.95)), ("LeftLeg", "LeftUpLeg", (0.09, 0, 0.5)),
         ("LeftFoot", "LeftLeg", (0.09, 0.02, 0.08)), ("LeftToeBase", "LeftFoot", (0.09, -0.1, 0.02)),
         ("LeftToe_End", "LeftToeBase", (0.09, -0.18, 0.02))]
    for f, y, z in (("Index", -0.03, 0), ("Middle", 0.0, 0), ("Ring", 0.02, 0), ("Pinky", 0.04, -0.005)):
        par = "LeftHand"
        for i, x in enumerate((0.80, 0.84, 0.87, 0.89)):
            n = f"LeftHand{f}{i + 1}"
            J.append((n, par, (x, y, 1.45 + z)))
            par = n
    par = "LeftHand"
    for i, (x, y) in enumerate(((0.75, -0.04), (0.78, -0.06), (0.81, -0.07), (0.83, -0.08))):
        n = f"LeftHandThumb{i + 1}"
        J.append((n, par, (x, y, 1.44)))
        par = n
    J = mirror(J, "Left", "Right")
    return [(P(n), P(p), h) for n, p, h in J]


def ue5_joints():
    J = [("root", None, (0, 0, 0)), ("pelvis", "root", (0, 0, 0.97)), ("spine_01", "pelvis", (0, 0, 1.02)),
         ("spine_02", "spine_01", (0, 0, 1.1)), ("spine_03", "spine_02", (0, 0, 1.2)),
         ("spine_04", "spine_03", (0, 0, 1.3)), ("spine_05", "spine_04", (0, 0, 1.4)),
         ("neck_01", "spine_05", (0, 0, 1.5)), ("neck_02", "neck_01", (0, -0.005, 1.55)),
         ("head", "neck_02", (0, -0.01, 1.6)),
         ("clavicle_l", "spine_05", (0.03, 0, 1.45)), ("upperarm_l", "clavicle_l", (0.16, 0, 1.42)),
         ("upperarm_twist_01_l", "upperarm_l", (0.27, 0.01, 1.33)),
         ("lowerarm_l", "upperarm_l", (0.38, 0.02, 1.25)), ("lowerarm_twist_01_l", "lowerarm_l", (0.48, 0, 1.16)),
         ("hand_l", "lowerarm_l", (0.58, -0.02, 1.07)),
         ("thumb_01_l", "hand_l", (0.6, -0.05, 1.05)), ("thumb_02_l", "thumb_01_l", (0.62, -0.07, 1.02)),
         ("thumb_03_l", "thumb_02_l", (0.63, -0.08, 0.99)),
         ("thigh_l", "pelvis", (0.09, 0, 0.93)), ("thigh_twist_01_l", "thigh_l", (0.095, 0, 0.72)),
         ("calf_l", "thigh_l", (0.1, -0.01, 0.5)), ("foot_l", "calf_l", (0.1, 0.03, 0.08)),
         ("ball_l", "foot_l", (0.1, -0.1, 0.02)),
         ("ik_foot_root", "root", (0, 0, 0)), ("ik_foot_l", "ik_foot_root", (0.1, 0.03, 0.08)),
         ("ik_hand_root", "root", (0, 0, 0)), ("ik_hand_gun", "ik_hand_root", (0.58, -0.02, 1.07)),
         ("ik_hand_l", "ik_hand_gun", (0.58, -0.02, 1.07))]
    for f, y in (("index", -0.03), ("middle", -0.01), ("ring", 0.01), ("pinky", 0.03)):
        J.append((f"{f}_metacarpal_l", "hand_l", (0.6, y, 1.06)))
        par = f"{f}_metacarpal_l"
        for i, d in enumerate((0.07, 0.11, 0.14)):
            n = f"{f}_0{i + 1}_l"
            J.append((n, par, (0.58 + d * 0.7, y, 1.07 - d * 0.7)))
            par = n
    return mirror(J, "_l", "_r")


def vroid_joints():
    J = [("Root", None, (0, 0, 0)), ("J_Bip_C_Hips", "Root", (0, 0, 0.9)), ("J_Bip_C_Spine", "J_Bip_C_Hips", (0, 0, 1.0)),
         ("J_Bip_C_Chest", "J_Bip_C_Spine", (0, 0, 1.12)), ("J_Bip_C_UpperChest", "J_Bip_C_Chest", (0, 0, 1.25)),
         ("J_Bip_C_Neck", "J_Bip_C_UpperChest", (0, 0, 1.38)), ("J_Bip_C_Head", "J_Bip_C_Neck", (0, 0, 1.46)),
         ("J_Adj_L_FaceEye", "J_Bip_C_Head", (0.03, -0.07, 1.52)),
         ("J_Bip_L_Shoulder", "J_Bip_C_UpperChest", (0.03, 0, 1.33)), ("J_Bip_L_UpperArm", "J_Bip_L_Shoulder", (0.12, 0, 1.32)),
         ("J_Bip_L_LowerArm", "J_Bip_L_UpperArm", (0.34, 0.01, 1.32)), ("J_Bip_L_Hand", "J_Bip_L_LowerArm", (0.56, 0, 1.32)),
         ("J_Bip_L_UpperLeg", "J_Bip_C_Hips", (0.08, 0, 0.85)), ("J_Bip_L_LowerLeg", "J_Bip_L_UpperLeg", (0.08, -0.01, 0.46)),
         ("J_Bip_L_Foot", "J_Bip_L_LowerLeg", (0.08, 0.02, 0.07)), ("J_Bip_L_ToeBase", "J_Bip_L_Foot", (0.08, -0.08, 0.02))]
    for f, y in (("Index", -0.025), ("Middle", -0.005), ("Ring", 0.015), ("Little", 0.03)):
        par = "J_Bip_L_Hand"
        for i in range(3):
            n = f"J_Bip_L_{f}{i + 1}"
            J.append((n, par, (0.63 + 0.03 * i, y, 1.32)))
            par = n
    par = "J_Bip_L_Hand"
    for i in range(3):
        n = f"J_Bip_L_Thumb{i + 1}"
        J.append((n, par, (0.58 + 0.025 * i, -0.04 - 0.015 * i, 1.31)))
        par = n
    return mirror(J, "_L_", "_R_")


def world_pose(obj, bname):
    return obj.matrix_world @ obj.pose.bones[bname].matrix


def ang(a, b):
    x = a.to_quaternion().rotation_difference(b.to_quaternion()).angle
    return min(x, 2 * math.pi - x)


def max_rest_drift(src, names):
    bpy.context.view_layer.update()
    worst = 0.0
    for n in names:
        rest = src.matrix_world @ src.data.bones[n].matrix_local
        pose = world_pose(src, n)
        worst = max(worst, (rest.translation - pose.translation).length, ang(rest, pose) * 0.1)
    return worst


def follow_error(src, rig, pairs):
    bpy.context.view_layer.update()
    worst = (0.0, "")
    for s, t in pairs:
        a = world_pose(src, s)
        b = rig.matrix_world @ rig.pose.bones[builder.BIND_PREFIX + s].matrix
        worst = max(worst, ((a.translation - b.translation).length, s + " loc"), (ang(a, b) * 0.1, s + " rot"))
    print("   worst follow:", worst)
    return worst[0]


def run_case(label, src, expect_profile, expect_parts_min):
    import json
    print(f"\n===== {label} =====")
    ctx = bpy.context
    bpy.context.view_layer.objects.active = src
    st = src.rig_bridge
    st.profile = "AUTO"
    bpy.ops.rig_bridge.detect()
    print("status:", st.status)
    for p in st.parts:
        print(f"   {p.module:9s} {p.part_name}.{p.side}: {[b.bone for b in p.bones]} tip={p.tip}")
    check(f"{label}: profile", st.used_profile == expect_profile, f"got {st.used_profile}")
    check(f"{label}: parts", len(st.parts) >= expect_parts_min, f"{len(st.parts)} parts")

    res = bpy.ops.rig_bridge.build()
    rig = st.rig
    check(f"{label}: build", res == {"FINISHED"} and rig is not None, str(res))
    if not rig:
        return
    pairs = json.loads(rig[builder.PAIRS_KEY])
    fk = json.loads(rig[builder.FK_KEY])
    print(f"   bound {len(pairs)} bones, {len(fk)} FK controls, rig bones {len(rig.data.bones)}")
    bpy.ops.object.mode_set(mode="OBJECT")
    names = [s for s, _ in pairs]
    d = max_rest_drift(src, names)
    check(f"{label}: zero rest drift", d < 1e-4, f"{d:.2e}")

    pb = rig.pose.bones
    for n in ("thigh_parent.L", "thigh_parent.R", "upper_arm_parent.L", "upper_arm_parent.R"):
        pb[n]["IK_Stretch"] = 0.0  # IK micro-stretch cannot be reproduced by rotation-only FK
    # --- IK test: pull the left hand towards the shoulder, elbow must go back
    arm = next(p for p in st.parts if p.module == "arm" and p.side == "L")
    up_name, fore, hand = [b.bone for b in arm.bones]
    pb["upper_arm_parent.L"]["IK_FK"] = 0.0
    sh = (rig.matrix_world @ pb["DEF-upper_arm.L"].head)
    hd = (rig.matrix_world @ pb["DEF-hand.L"].head)
    target = sh + (hd - sh) * 0.6
    pb["hand_ik.L"].matrix = Matrix.Translation(rig.matrix_world.inverted() @ target) @ pb["hand_ik.L"].matrix.to_3x3().to_4x4()
    bpy.context.view_layer.update()
    elbow_src = world_pose(src, fore).translation
    line_mid = sh + (target - sh) * 0.5
    back = (elbow_src - line_mid).y
    check(f"{label}: IK arm bends elbow backwards", back > 0.02, f"elbow offset y={back:+.3f}")
    hand_src = world_pose(src, hand).translation
    check(f"{label}: source hand follows IK", (hand_src - target).length < 0.01, f"{(hand_src - target).length:.4f}")

    # --- IK leg: raise foot, knee must go forward (-Y)
    leg = next(p for p in st.parts if p.module == "leg" and p.side == "L")
    thigh, shin = leg.bones[0].bone, leg.bones[1].bone
    hip = rig.matrix_world @ pb["DEF-thigh.L"].head
    ank = rig.matrix_world @ pb["DEF-foot.L"].head
    pb["foot_ik.L"].location.z += 0.3 / rig.matrix_world.to_scale().z
    bpy.context.view_layer.update()
    knee = world_pose(src, shin).translation
    check(f"{label}: IK leg bends knee forward", knee.y < -0.05, f"knee y={knee.y:+.3f}")

    # --- FK + torso
    pb["upper_arm_parent.L"]["IK_FK"] = 1.0
    pb["upper_arm_parent.R"]["IK_FK"] = 1.0  # avoid IK stretch (FK transfer is rotation-only)
    pb["upper_arm_fk.L"].rotation_mode = "XYZ"
    pb["upper_arm_fk.L"].rotation_euler = (0.4, 0.2, 0.6)
    pb["torso"].location = (0.1, 0.05, -0.1)
    pb["head"].rotation_mode = "XYZ"
    pb["head"].rotation_euler = (0.3, 0, 0.2)
    e = follow_error(src, rig, pairs)
    check(f"{label}: source follows rig (bind bones)", e < 1e-4, f"{e:.2e}")
    # deform bones vs source joint positions (prebend adds small offsets)
    worst = 0.0
    for s, t in pairs:
        if t.startswith("DEF-"):
            worst = max(worst, (world_pose(src, s).translation - (rig.matrix_world @ pb[t].head)).length)
    check(f"{label}: source joints ~ DEF joints", worst < 0.03, f"max {worst:.4f} m")

    # --- bake to source
    for p in rig.pose.bones:
        p.keyframe_insert("location", frame=1)
        p.keyframe_insert("rotation_quaternion", frame=1) if p.rotation_mode == "QUATERNION" else p.keyframe_insert("rotation_euler", frame=1)
    pb["upper_arm_fk.L"].rotation_euler = (-0.4, 0.0, 0.0)
    pb["upper_arm_fk.L"].keyframe_insert("rotation_euler", frame=10)
    ctx.scene.frame_start, ctx.scene.frame_end = 1, 10
    ctx.scene.frame_set(10)
    ref = {s: world_pose(src, s).copy() for s, _ in pairs}
    print("   DBG ref shin", tuple(round(x, 3) for x in ref[shin].translation), "foot", tuple(round(x, 3) for x in ref[leg.bones[2].bone].translation))
    bpy.context.view_layer.objects.active = src
    r = bpy.ops.rig_bridge.bake()
    ctx.scene.frame_set(1)
    ctx.scene.frame_set(10)
    err = max((world_pose(src, s).translation - ref[s].translation).length for s in ref)
    muted = all(c.mute for p in src.pose.bones for c in p.constraints if c.name.startswith("RB_"))
    check(f"{label}: bake to skeleton", r == {"FINISHED"} and err < 1e-3 and muted, f"err {err:.2e}, muted={muted}")

    # --- transfer: source's baked action -> fresh rig FK controls
    # reset rig pose + remove rig animation, then transfer from the baked skeleton
    rig.animation_data.action = None  # keep Rigify drivers!
    for p in rig.pose.bones:
        p.location = (0, 0, 0)
        p.rotation_quaternion = (1, 0, 0, 0)
        p.rotation_euler = (0, 0, 0)
    st.anim_source = src
    bpy.context.view_layer.objects.active = src
    r = bpy.ops.rig_bridge.transfer()
    check(f"{label}: transfer op", r == {"FINISHED"}, str(r))
    bpy.ops.rig_bridge.mute(mute=False)
    # src now follows rig again; compare with recorded reference at frame 10
    src_anim = src.animation_data.action
    src.animation_data.action = None
    ctx.scene.frame_set(1)
    ctx.scene.frame_set(10)
    errs = sorted(((world_pose(src, s).translation - ref[s].translation).length, s) for s in ref)[::-1]
    pbx = rig.pose.bones
    print("   DBG ikfk legL", pbx["thigh_parent.L"]["IK_FK"], "armL", pbx["upper_arm_parent.L"]["IK_FK"],
          "thigh_fk q", tuple(round(x, 3) for x in pbx["thigh_fk.L"].rotation_quaternion),
          "shin_fk q", tuple(round(x, 3) for x in pbx["shin_fk.L"].rotation_quaternion),
          "DEF-shin", tuple(round(x, 3) for x in rig.matrix_world @ pbx["DEF-shin.L"].head),
          "action", rig.animation_data.action.name if rig.animation_data and rig.animation_data.action else None)
    err = errs[0][0]
    rot_err = max(math.degrees(ang(world_pose(src, s), ref[s])) for s in ref)
    # Rigify IK lets limbs shrink ~3% (ik_stretch 0.1); rotation-only FK transfer
    # reproduces orientations exactly but joint positions only within that shrink.
    check(f"{label}: transfer round-trip", rot_err < 0.5 and err < 0.04,
          f"max bone rot err {rot_err:.3f}°, max joint err {err:.4f} m  worst={[(round(e, 3), n) for e, n in errs[:2]]}")


def main():
    try:
        mix = make_armature("Mixamo_Char", mixamo_joints(),
                            Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(0.01, 4))
        run_case("Mixamo", mix, "STD:MIXAMO", 19)

        # realistic case: a separately imported Mixamo clip (different namespace)
        anim = make_armature("Mixamo_Clip", mixamo_joints("mixamorig1:"),
                             Matrix.Rotation(math.radians(90), 4, "X") @ Matrix.Scale(0.01, 4))
        keys = {"Hips": (0.2, 0.1, 0.0), "Spine1": (0.2, 0.0, 0.1), "LeftArm": (0.3, -0.5, 0.8),
                "LeftForeArm": (0.0, 0.0, -1.2), "RightUpLeg": (-0.8, 0.0, 0.1), "RightLeg": (1.1, 0, 0),
                "Neck": (0.2, 0.1, 0), "LeftHandIndex1": (0, 0, 0.6), "RightShoulder": (0.1, 0.2, 0)}
        for f in (1, 10):
            for n, e in keys.items():
                p = anim.pose.bones["mixamorig1:" + n]
                p.rotation_mode = "XYZ"
                p.rotation_euler = e if f == 10 else (0, 0, 0)
                p.keyframe_insert("rotation_euler", frame=f)
            hp = anim.pose.bones["mixamorig1:Hips"]
            hp.location = (0, 0, 0) if f == 1 else (10, -20, 5)   # cm, local
            hp.keyframe_insert("location", frame=f)
        bpy.context.scene.frame_set(10)
        mix.animation_data.action = None
        mix.rig_bridge.rig.animation_data.action = None
        for p in mix.rig_bridge.rig.pose.bones:
            p.location = (0, 0, 0)
            p.rotation_quaternion = (1, 0, 0, 0)
            p.rotation_euler = (0, 0, 0)
        mix.rig_bridge.anim_source = anim
        bpy.context.view_layer.objects.active = mix
        r = bpy.ops.rig_bridge.transfer()
        bpy.ops.rig_bridge.mute(mute=False)
        bpy.context.scene.frame_set(1)
        bpy.context.scene.frame_set(10)
        worst = (0, "")
        for b in mix.data.bones:
            ab = "mixamorig1:" + b.name.split(":")[1]
            if any(t in b.name for t in ("Eye", "End", "4")):
                continue
            e = (world_pose(mix, b.name).translation - world_pose(anim, ab).translation).length
            worst = max(worst, (e, b.name))
        check("Mixamo clip -> rig FK -> skeleton", r == {"FINISHED"} and worst[0] < 2e-3,
              f"max joint err {worst[0]:.5f} m at {worst[1]}")
        ue = make_armature("UE5_Manny", ue5_joints(), tail_mode="x")
        run_case("UE5", ue, "STD:UE5_MANNY", 20)
        vr = make_armature("VRoid_Char", vroid_joints())
        run_case("VRoid(topology)", vr, "STD:VROID", 15)

        # rebuild in place + save/load a user profile
        bpy.context.view_layer.objects.active = mix
        r = bpy.ops.rig_bridge.build()
        bpy.ops.object.mode_set(mode="OBJECT")
        rigs = [o for o in bpy.data.objects if o.name.startswith("Mixamo_Char_rig")]
        names = [s for s in mix.rig_bridge.rig.keys()]
        check("rebuild in place", r == {"FINISHED"} and len(rigs) == 1, f"rigs={[o.name for o in rigs]}")
        bpy.context.view_layer.objects.active = vr
        bpy.ops.rig_bridge.save_profile(pid="vroid_test", label="VRoid test")
        detect.load_profiles()
        parts, used, msg = detect.detect(vr, "AUTO")
        check("saved profile is auto-detected", used == "vroid_test" and len(parts) == len(vr.rig_bridge.parts), msg)
    except Exception:
        traceback.print_exc()
        check("exception", False)
    bpy.ops.wm.save_as_mainfile(filepath=sys.argv[sys.argv.index("--") + 2])
    print("\nSUMMARY:", sum(ok for _, ok, _ in RESULTS), "/", len(RESULTS), "passed")


import os
if not os.environ.get("RB_NO_MAIN"):
    main()
