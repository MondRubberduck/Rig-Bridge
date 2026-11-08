"""Rig Bridge <-> Bulletproof Retarget, the Unreal way:

    mocap (Mixamo) --BP Retarget--> UE5 character skeleton --Bake to Control Rig--> UE5 control rig
    UE5 control rig (skeleton as source) --BP Retarget--> Daz G8 skeleton --Bake to Control Rig--> Daz control rig

Bulletproof Retarget is installed from its own release zip and only called,
never modified. Every result is compared with BP retargeting onto a plain,
unrigged copy of the same skeleton (= what the skeleton must look like).

blender -b --factory-startup --python tests/test_bp_interop.py -- <dir containing rig_bridge> <out.blend> <bpr checkout> <UE5 character .fbx>
(BLENDER_USER_RESOURCES must contain the installed Bulletproof Retarget extension.)
"""
import math
import os
import sys
import traceback

import bpy
from mathutils import Matrix, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
sys.path.insert(0, argv[0])
OUT, BPR, FBX = argv[1], argv[2], argv[3]
sys.path.insert(0, os.path.join(argv[0], "rig_bridge", "tests"))
os.environ["RB_NO_MAIN"] = "1"
import rig_bridge  # noqa: E402

rig_bridge.register()
import test_standards as T  # noqa: E402  (skeleton builders, clip, compare)
from rig_bridge import stdmatch  # noqa: E402

T.BPR = BPR
C = bpy.context
RESULTS = T.RESULTS
check = T.check
bpy.ops.preferences.addon_enable(module="bl_ext.user_default.bulletproof_retarget")
S = C.scene.bp_retarget


def select_only(obj):
    for o in C.selected_objects:
        o.select_set(False)
    C.view_layer.objects.active = obj
    obj.select_set(True)


def bp_retarget(src, tgt, action=None):
    S.source, S.target = src, tgt
    S.action = action
    S.range_mode = "ACTION"
    S.use_batch = False
    S.overwrite = False          # keep every result as its own action
    r = bpy.ops.bpr.retarget()
    act = tgt.animation_data.action if tgt.animation_data else None
    return r, act


def plain_copy(obj, name):
    c = obj.copy()
    c.name = name
    c.animation_data_clear()
    for pb in c.pose.bones:
        for con in list(pb.constraints):
            pb.constraints.remove(con)
    obj.users_collection[0].objects.link(c)
    return c


def capsules(sk, coll, rgb):
    """Rigid capsule per deforming bone so the synthetic characters are visible."""
    m = bpy.data.materials.new(f"{sk.name}_mat")
    m.diffuse_color = (*rgb, 1)
    for b in sk.data.bones:
        low = b.name.lower()
        if b.parent is None or any(k in low for k in ("twist", "carpal", "eye")) or \
                (low.endswith("end") and not low.endswith("bend")):   # HeadTop_End, not Daz ThighBend
            continue
        # draw to the main child's head: works whichever way the bone points (Daz: sideways)
        kids = []
        todo = list(b.children)
        while todo:                    # walk through Daz/UE twist bones to the next real joint
            c = todo.pop(0)
            if "twist" in c.name.lower():
                todo += list(c.children)
            else:
                kids.append(c)
        h = sk.matrix_world @ b.head_local
        t = sk.matrix_world @ (max(kids, key=lambda c: len(c.children_recursive)).head_local if kids
                               else b.head_local + (b.tail_local - b.head_local).normalized() *
                               min(b.length, 0.08 / sk.matrix_world.to_scale().x))
        if (t - h).length < 1e-3:
            continue
        L = (t - h).length
        r = 0.012 if any(k in b.name.lower() for k in ("thumb", "index", "mid", "ring", "pinky")) else \
            min(0.06, max(0.02, L * 0.22))
        bpy.ops.mesh.primitive_cylinder_add(vertices=10, radius=r, depth=L)
        o = C.object
        o.name = f"{sk.name}_{b.name}"
        d = (t - h).normalized()
        o.matrix_world = Matrix.Translation((h + t) / 2) @ d.to_track_quat("Z", "Y").to_matrix().to_4x4()
        for c in o.users_collection:
            c.objects.unlink(o)
        coll.objects.link(o)
        o.data.materials.append(m)
        o.parent = sk
        o.parent_type = "BONE"
        o.parent_bone = b.name
        # bone parents sit at the bone's tail; keep the capsule where it is at rest
        o.matrix_parent_inverse = (sk.matrix_world @ b.matrix_local @ Matrix.Translation((0, b.length, 0))).inverted()


def main():
    for o in list(bpy.data.objects):             # factory-startup cube, camera, light
        bpy.data.objects.remove(o)
    coll = bpy.data.collections.new("Interop")
    C.scene.collection.children.link(coll)
    # -- mocap source: Mixamo skeleton with the procedural clip ----------------
    mstd = stdmatch.get("MIXAMO")
    rows = T.spec_from_standard(mstd, "T")
    T.q_mixamo(rows)
    for r in rows:
        r[2] += Vector((-2.0, 0, 0))
    mm = T.MIXAMO_M.copy()
    mm.translation = (-2.0, 0, 0)
    mocap = T.make_armature("Mocap_Mixamo", rows, "chain", mm, coll)
    _, mres = stdmatch.score(mstd, [b.name for b in mocap.data.bones])
    clip = T.key_clip(mocap, mres, frames=range(1, 81, 4), period=40)
    capsules(mocap, coll, (0.55, 0.55, 0.6))

    # -- the user's UE5 character ----------------------------------------------
    bpy.ops.import_scene.fbx(filepath=FBX)
    ue = next(o for o in C.selected_objects if o.type == "ARMATURE")
    ue.name = "UE5_Character"
    if ue.animation_data:
        ue.animation_data.action = None
    for pb in ue.pose.bones:
        pb.matrix_basis = Matrix()
    ue_ref = plain_copy(ue, "UE5_reference")
    select_only(ue)
    r = bpy.ops.rig_bridge.build()
    st = ue.rig_bridge
    check("UE5 character: one-click control rig", r == {"FINISHED"} and st.rig is not None, st.status)
    ue_rig = st.rig

    # -- mocap -> UE5 skeleton (bound!) -> Bake to Control Rig ----------------
    r1, act_ref = bp_retarget(mocap, ue_ref, clip)
    check("BP: mocap -> plain UE5 copy", r1 == {"FINISHED"} and act_ref is not None, S.last_report.splitlines()[0])
    select_only(ue)
    bpy.ops.rig_bridge.mute(mute=True)          # 1. Release Skeleton
    r2, act_bound = bp_retarget(mocap, ue, clip)
    check("BP: mocap -> UE5 skeleton that Rig Bridge drives", r2 == {"FINISHED"} and act_bound is not None,
          S.last_report.splitlines()[0])
    # BP must write the same animation whether or not the skeleton is bound
    probe = plain_copy(ue, "UE5_probe")
    probe.animation_data_create()
    probe.animation_data.action = act_bound
    probe.animation_data.action_slot = act_bound.slots[0]      # Blender 5: slotted actions
    bones = [b.name for b in ue.data.bones if b.name in ue_ref.pose.bones]
    pe, re_, w = T.compare(probe, ue_ref, bones, [1, 21, 41, 61])
    check("BP writes identical animation onto the released skeleton", pe < 1e-4 and re_ < 0.01,
          f"{pe * 100:.4f} cm, {re_:.4f}°")
    bpy.data.objects.remove(probe)
    select_only(ue)
    r = bpy.ops.rig_bridge.transfer(mode="IK_LEGS")
    check("Rig Bridge: Bake to Control Rig (UE5)", r == {"FINISHED"})
    ue.animation_data.action = None              # the control rig alone drives it now
    bound = [pb.name for pb in ue.pose.bones if any(c.name.startswith("RB_") for c in pb.constraints)]
    _, ures = stdmatch.score(stdmatch.get("UE5_MANNY"), [b.name for b in ue.data.bones])
    joints = [b for b in ures.values() if b in bound]
    mid = {b.bone for p in st.parts if p.module == "neck_head" for b in list(p.bones)[:-1]}
    legs = {ures[f"{j}_{s}"] for j in ("thigh", "shin") for s in "lr" if f"{j}_{s}" in ures}
    arms = {ures[f"{j}_{s}"] for j in ("upper_arm", "forearm") for s in "lr" if f"{j}_{s}" in ures}
    frames = list(range(1, 78, 7))
    # FK: every bone exactly. IK: every joint position and every hand/foot/body
    # rotation exactly; the roll of the IK-driven upper/lower limb follows the
    # rig's elbow/knee hinge (reported, not hidden).
    for mode, rolled in (("FK", set()), ("IK_LEGS", legs), ("IK", legs | arms)):
        rig_bridge.builder.set_ik_mode(ue_rig, mode, 1)
        pe, re_, w = T.compare(ue, ue_ref, joints, frames, mid | rolled)
        check(f"UE5 control rig ({mode}) reproduces the retargeted mocap", pe < 0.005 and re_ < 0.5,
              f"pos {pe * 100:.3f} cm ({T.LAST.get('pos')}), rot {re_:.3f}° ({w[0]})")
        if rolled:
            _, rr, rw = T.compare(ue, ue_ref, sorted(rolled), frames)
            print(f"   info {mode}: limb roll follows the rig hinge, up to {rr:.1f}° ({rw[0]})", flush=True)
    rig_bridge.builder.set_ik_mode(ue_rig, "IK_LEGS", 1)

    # -- UE5 control rig -> Daz G8 skeleton -> Daz control rig -----------------
    dstd = stdmatch.get("DAZ_G8")
    rows = T.spec_from_standard(dstd, "T")
    T.q_daz8(rows)
    for r in rows:
        r[2] += Vector((2.0, 0, 0))
    daz = T.make_armature("Daz_G8", rows, "sideways", None, coll)
    capsules(daz, coll, (0.85, 0.55, 0.35))
    daz_ref = plain_copy(daz, "Daz_reference")
    select_only(daz)
    r = bpy.ops.rig_bridge.build()
    check("Daz G8: one-click control rig", r == {"FINISHED"} and daz.rig_bridge.rig is not None,
          daz.rig_bridge.status)
    # source = the UE5 skeleton: Bake to Skeleton (UE: Bake Animation Sequence), then retarget
    select_only(ue)
    ue.rig_bridge.mute_after_bake = True
    r = bpy.ops.rig_bridge.bake()
    baked = ue.animation_data.action
    pe, re_, w = T.compare(ue, ue_ref, joints, list(range(1, 78, 7)), mid | legs)
    check("Bake to Skeleton keeps the control-rig motion", r == {"FINISHED"} and pe < 0.005 and re_ < 0.5,
          f"{pe * 100:.3f} cm, {re_:.3f}°")
    select_only(daz)
    bpy.ops.rig_bridge.mute(mute=True)
    r1, a1 = bp_retarget(ue, daz, baked)
    check("BP: UE5 (baked from its control rig) -> Daz skeleton", r1 == {"FINISHED"} and a1 is not None,
          S.last_report.splitlines()[0])
    # the reference: BP from the plain UE5 copy (same motion) onto a plain Daz copy
    r2, a2 = bp_retarget(ue_ref, daz_ref, act_ref)
    select_only(ue)
    bpy.ops.rig_bridge.mute(mute=False)
    select_only(daz)
    daz.rig_bridge.anim_source = None
    S_frames = (1, 77)
    r = bpy.ops.rig_bridge.transfer(mode="IK_LEGS")
    check("Rig Bridge: Bake to Control Rig (Daz)", r == {"FINISHED"})
    daz.animation_data.action = None
    dbound = [pb.name for pb in daz.pose.bones if any(c.name.startswith("RB_") for c in pb.constraints)]
    _, dres = stdmatch.score(dstd, [b.name for b in daz.data.bones])
    djoints = [b for b in dres.values() if b in dbound]
    skip = {b.name for b in daz.data.bones if any("twist" in c.name.lower() and any(
        x.name in dbound for x in c.children_recursive) for c in b.children)}
    skip |= {b.bone for p in daz.rig_bridge.parts if p.module == "neck_head" for b in list(p.bones)[:-1]}
    pe, re_, w = T.compare(daz, daz_ref, djoints, list(range(1, 78, 7)), skip)
    check("Daz control rig reproduces UE5 control-rig motion via BP", pe < 0.01 and re_ < 1.0,
          f"pos {pe * 100:.3f} cm ({T.LAST.get('pos')}), rot {re_:.3f}° ({w[0]})")

    # -- scene for viewing ------------------------------------------------------
    for o in (ue_ref, daz_ref):
        o.hide_set(True)
        o.hide_render = True
    sc = C.scene
    sc.frame_start, sc.frame_end = 1, 77
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    sc.collection.objects.link(cam)
    cam.location = (0, -7.5, 1.3)
    cam.rotation_euler = (math.radians(86), 0, 0)
    cam.data.lens = 32
    sc.camera = cam
    fl = bpy.data.meshes.new("Floor")
    fl.from_pydata([(-4, -3, 0), (4, -3, 0), (4, 3, 0), (-4, 3, 0)], [], [(0, 1, 2, 3)])
    sc.collection.objects.link(bpy.data.objects.new("Floor", fl))
    for txt, x in (("mocap (Mixamo)", -2), ("UE5 character + control rig", 0), ("Daz G8 + control rig", 2)):
        cu = bpy.data.curves.new(txt, "FONT")
        cu.body, cu.size, cu.align_x = txt, 0.12, "CENTER"
        o = bpy.data.objects.new(txt, cu)
        o.location, o.rotation_euler = (x, 0, 2.05), (math.radians(90), 0, 0)
        sc.collection.objects.link(o)
    sc.frame_set(1)
    bpy.ops.wm.save_as_mainfile(filepath=OUT)


try:
    main()
except Exception:
    traceback.print_exc()
    check("interop: exception", False)
fails = [r for r in RESULTS if not r[1]]
print("\nSUMMARY:", len(RESULTS) - len(fails), "/", len(RESULTS), "passed", flush=True)
for f in fails:
    print("  FAILED", f[0], f[2])
