"""Comprehensive Modular Control Rig tests for Rig Bridge 1.0:
  1. Partial Rigging on an animated character (rigging ONLY a tail, ONLY hair,
     or ONLY one arm without rigging the body — verifies auto-socket anchors and
     Bake to Control Rig on partial rigs).
  2. Live Module Experimentation / Swapping on the same bone chain:
     - Swap Hair chain in-place across: Dynamic Chain -> Tail -> IK Chain ->
       FK Chain -> Arm -> Leg -> Dynamic Chain via both p.module and add_module
       click-to-swap, recompiling at every step and checking zero rest drift &
       controls.
  3. Explicit User-Defined Sockets (rig_bridge.socket_add / socket_remove) and
     Skeleton <-> Control Rig mode switching (select_skeleton / select_rig) +
     selecting bones directly on the Control Rig to swap/place modules.
"""
import math
import os
import sys

import bpy
from mathutils import Euler, Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rig_bridge  # noqa: E402
from rig_bridge import builder, modular, overlay  # noqa: E402

rig_bridge.register()

C = bpy.context
SC = C.scene
RESULTS = []


def check(name, ok, info=""):
    RESULTS.append((name, bool(ok), info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {info}")


def activate(obj, mode="OBJECT"):
    if C.object and C.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in C.view_layer.objects:
        o.select_set(False)
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.select_set(True)
    C.view_layer.objects.active = obj
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode=mode)


def select_bones(arm, names):
    activate(arm, "POSE")
    for pb in arm.pose.bones:
        builder.pose_select(pb, pb.name in names)
    if names and names[0] in arm.data.bones:
        arm.data.bones.active = arm.data.bones[names[0]]


def rest_drift(sk):
    C.view_layer.update()
    return max((sk.pose.bones[b.name].matrix - b.matrix_local).to_translation().length
               for b in sk.data.bones)


def build_test_creature(name="Creature"):
    arm = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, arm)
    SC.collection.objects.link(obj)
    activate(obj, "EDIT")
    eb = arm.edit_bones

    def add(n, par, h, t):
        b = eb.new(n)
        b.head, b.tail = Vector(h), Vector(t)
        if par:
            b.parent = eb[par]
        return b

    add("root", None, (0, 0, 0), (0, -0.2, 0))
    add("pelvis", "root", (0, 0, 0.95), (0, 0, 1.05))
    add("spine_01", "pelvis", (0, 0, 1.05), (0, 0, 1.25))
    add("spine_02", "spine_01", (0, 0, 1.25), (0, 0, 1.45))
    add("neck_01", "spine_02", (0, 0, 1.45), (0, 0, 1.55))
    add("head", "neck_01", (0, 0, 1.55), (0, 0, 1.75))

    # Hair strand (5 bones under head)
    p = "head"
    for k in range(5):
        n = f"hair_{k + 1:02d}"
        add(n, p, (0, 0.08 + 0.02 * k, 1.70 - 0.08 * k), (0, 0.10 + 0.02 * k, 1.62 - 0.08 * k))
        p = n

    # Tail (6 bones under pelvis)
    p = "pelvis"
    for k in range(6):
        n = f"tail_{k + 1:02d}"
        add(n, p, (0, 0.10 + 0.12 * k, 0.95 - 0.03 * k), (0, 0.22 + 0.12 * k, 0.92 - 0.03 * k))
        p = n

    # Arms (under clavicle -> spine_02)
    for s, sx in (("l", 1), ("r", -1)):
        add(f"clavicle_{s}", "spine_02", (0.04 * sx, 0, 1.42), (0.15 * sx, 0, 1.42))
        add(f"upperarm_{s}", f"clavicle_{s}", (0.15 * sx, 0, 1.42), (0.40 * sx, 0.02, 1.42))
        add(f"lowerarm_{s}", f"upperarm_{s}", (0.40 * sx, 0.02, 1.42), (0.65 * sx, 0, 1.42))
        add(f"hand_{s}", f"lowerarm_{s}", (0.65 * sx, 0, 1.42), (0.75 * sx, 0, 1.42))

    activate(obj, "OBJECT")
    return obj


# ===========================================================================
# 1. Partial Rigging on an unrigged body (e.g. ONLY Hair, ONLY Tail, ONLY Arm)
# ===========================================================================
def test_partial_rigs():
    print("\n--- Test 1: Partial Rigging & Auto-Socket Anchors ---")
    sk = build_test_creature("Partial_Skel")

    # Keyframe a head & pelvis animation on the source skeleton (frames 1..20)
    activate(sk, "POSE")
    SC.frame_start, SC.frame_end = 1, 20
    for f, x_off, rot_z in ((1, 0.0, 0.0), (10, 0.35, 0.4), (20, 0.0, 0.0)):
        SC.frame_set(f)
        pb_p = sk.pose.bones["pelvis"]
        pb_p.location = (x_off, 0, 0)
        pb_p.keyframe_insert("location", frame=f)
        pb_h = sk.pose.bones["head"]
        pb_h.rotation_mode = "XYZ"
        pb_h.rotation_euler = (0, 0, rot_z)
        pb_h.keyframe_insert("rotation_euler", frame=f)
        pb_t = sk.pose.bones["tail_01"]
        pb_t.rotation_mode = "XYZ"
        pb_t.rotation_euler = (rot_z * 0.5, 0, 0)
        pb_t.keyframe_insert("rotation_euler", frame=f)
    SC.frame_set(1)

    # User ONLY selects `tail_01` and `hair_01` and drops Tail & FK Chain —
    # neither `pelvis` nor `head` nor `spine` has a module!
    select_bones(sk, ["tail_01"])
    r1 = bpy.ops.rig_bridge.add_module(module="tail")
    select_bones(sk, ["hair_01"])
    r2 = bpy.ops.rig_bridge.add_module(module="fk_chain")
    check("partial: placed tail + hair without rigging body",
          r1 == {"FINISHED"} and r2 == {"FINISHED"} and len(sk.rig_bridge.parts) == 2)

    # Compile partial rig
    r_build = bpy.ops.rig_bridge.build()
    rig = sk.rig_bridge.rig
    check("partial: compiled cleanly", r_build == {"FINISHED"} and rig is not None)
    check("partial: auto-created parent socket controls ('pelvis', 'head') on rig",
          "pelvis" in rig.pose.bones and "head" in rig.pose.bones,
          str([b.name for b in rig.pose.bones if not b.name.startswith(("MCH", "ORG", "DEF", "BIND", "OUT"))]))

    # Transfer animation from skeleton to partial Control Rig
    r_tr = bpy.ops.rig_bridge.transfer()
    check("partial: Bake to Control Rig succeeded on partial rig", r_tr == {"FINISHED"})

    # At frame 10, pelvis shifted +0.35m in X and head rotated +0.4 rad around Z:
    # tail_01's head is at x = 0.35m, and hair_01's head (offset y=0.08 from head)
    # rotates around head's Z axis by +0.4 rad -> x = 0.35 - 0.08 * sin(0.4) = 0.319m!
    SC.frame_set(10)
    C.view_layer.update()
    tail_x = (sk.matrix_world @ sk.pose.bones["tail_01"].head).x
    hair_x = (sk.matrix_world @ sk.pose.bones["hair_01"].head).x
    expected_hair_x = 0.35 - 0.08 * math.sin(0.4)
    check("partial: tail & hair follow animated pelvis/head sockets at frame 10",
          abs(tail_x - 0.35) < 0.01 and abs(hair_x - expected_hair_x) < 0.035,
          f"tail_x={tail_x:.3f} (exp 0.350), hair_x={hair_x:.3f} (exp {expected_hair_x:.3f})")
    SC.frame_set(1)


# ===========================================================================
# 2. Live Module Experimentation / Swapping on the Same Bone Chain
# ===========================================================================
def test_module_experimentation():
    print("\n--- Test 2: Live Module Experimentation & In-Place Swapping ---")
    sk = build_test_creature("Swap_Skel")
    st = sk.rig_bridge

    # Drop Control on head, and start with Dynamic Chain on hair_01
    select_bones(sk, ["head"])
    bpy.ops.rig_bridge.add_module(module="control")
    select_bones(sk, ["hair_01"])
    bpy.ops.rig_bridge.add_module(module="dynamic_chain")

    hp = st.parts[1]
    check("swap: initial hair module is dynamic_chain (5 bones)",
          hp.module == "dynamic_chain" and [b.bone for b in hp.bones] == [f"hair_{k:02d}" for k in range(1, 6)])

    # Experiment 1: Swap via Details dropdown (`hp.module = ...`)
    #   dynamic_chain (5 bones) -> arm (auto-resolves to 3 bones!) -> tail (auto-resolves back to 5 bones!)
    for target_mod, expected_len in (
        ("tail", 5),
        ("ik_chain", 5),
        ("fk_chain", 5),
        ("arm", 3),
        ("leg", 4),
        ("control", 1),
        ("dynamic_chain", 5),
    ):
        hp.module = target_mod
        bones_now = [b.bone for b in hp.bones]
        prob = rig_bridge.part_problem(sk, hp)
        r = bpy.ops.rig_bridge.build()
        rig = st.rig
        d = rest_drift(sk)
        check(f"swap dropdown -> '{target_mod}': resolved {len(bones_now)} bones, valid & compiled (drift={d:.1e})",
              len(bones_now) == expected_len and not prob and r == {"FINISHED"} and d < 1e-3,
              f"bones={bones_now}, prob='{prob}'")

    # Experiment 2: Swap by selecting a bone on the compiled Control Rig (or skeleton)
    # and clicking a Module Asset tile (`rig_bridge.add_module`)!
    # Right now after `build()`, `rig` is active in Pose Mode and `sk` is hidden.
    check("swap: after compile, Control Rig is active in Pose Mode", C.object is st.rig and st.rig.mode == "POSE")

    # Select the hair control bone directly on the Control Rig and click 'tail' tile!
    hair_ctrl = next(b.name for b in st.rig.pose.bones if "hair" in b.name and not b.name.startswith(("MCH", "ORG", "DEF", "BIND", "OUT")))
    select_bones(st.rig, [hair_ctrl])
    sel_mapped = rig_bridge.selected_socket_bones(C, sk)
    check("swap: selecting control on Control Rig maps back to source bone",
          bool(sel_mapped) and sel_mapped[0].startswith("hair_"), f"{hair_ctrl} -> {sel_mapped}")

    r_swap = bpy.ops.rig_bridge.add_module(module="tail")
    check("swap: clicking Module Asset tile while Control Rig bone selected swaps module in-place (no duplicate)",
          r_swap == {"FINISHED"} and len(st.parts) == 2 and st.parts[1].module == "tail")

    # Recompile after tile swap
    r_re = bpy.ops.rig_bridge.build()
    check("swap: recompile after tile swap succeeded",
          r_re == {"FINISHED"} and "tail_master" in [b.name for b in st.rig.pose.bones] or
          any(b.name.endswith("_master") for b in st.rig.pose.bones))


# ===========================================================================
# 3. User-Defined Sockets & Skeleton <-> Control Rig Mode Switching
# ===========================================================================
def test_custom_sockets_and_mode_switch():
    print("\n--- Test 3: Custom Sockets & Mode Switching ---")
    sk = build_test_creature("Socket_Skel")
    st = sk.rig_bridge

    # Before any module is placed, only root is a default socket
    default_socks = modular.sockets(sk, [])
    check("sockets: default initial socket is root", default_socks == ["root"], str(default_socks))

    # User selects `hair_01` and `upperarm_l` in Pose Mode and marks them as Custom Sockets
    select_bones(sk, ["hair_01", "upperarm_l"])
    r_pin = bpy.ops.rig_bridge.socket_add()
    socks_after = modular.sockets(sk, [])
    check("sockets: Marked bones appear immediately in sockets() and schematic overlay",
          r_pin == {"FINISHED"} and "hair_01" in socks_after and "upperarm_l" in socks_after,
          str(socks_after))

    schem = overlay.schematic(sk)
    user_flags = {b: is_u for b, _, is_u in schem["sockets"]}
    check("sockets: schematic marks user sockets with is_user=True",
          user_flags.get("hair_01") is True and user_flags.get("upperarm_l") is True,
          str(user_flags))

    # Drop Arm on upperarm_l and compile
    select_bones(sk, ["upperarm_l"])
    bpy.ops.rig_bridge.add_module(module="arm")
    bpy.ops.rig_bridge.build()
    check("mode_switch: after Compile, Control Rig is active and skeleton is hidden",
          C.object is st.rig and sk.hide_get() is True)

    # Click 'Edit Skeleton / Sockets' (rig_bridge.select_skeleton)
    r_sk = bpy.ops.rig_bridge.select_skeleton()
    check("mode_switch: select_skeleton unhides skeleton and enters Pose Mode with active module bones selected",
          r_sk == {"FINISHED"} and C.object is sk and sk.mode == "POSE" and sk.hide_get() is False)

    # Switch back to Control Rig (rig_bridge.select_rig)
    r_rg = bpy.ops.rig_bridge.select_rig()
    check("mode_switch: select_rig returns to Control Rig in Pose Mode and hides skeleton",
          r_rg == {"FINISHED"} and C.object is st.rig and st.rig.mode == "POSE" and sk.hide_get() is True)

    # Remove custom socket
    bpy.ops.rig_bridge.socket_remove(index=0)
    check("sockets: socket_remove removed entry", len(st.user_sockets) == 1)

    # UE5-style shoulder socket intent: dropping Arm on clavicle_r auto-places
    # Shoulder on clavicle_r and resolves Arm on upperarm_r..hand_r!
    select_bones(sk, ["clavicle_r"])
    r_sh = bpy.ops.rig_bridge.add_module(module="arm")
    arm_r = next((p for p in st.parts if p.module == "arm" and p.side == "R"), None)
    sh_r = next((p for p in st.parts if p.module == "shoulder" and p.side == "R"), None)
    check("sockets: UE5 shoulder intent (Arm dropped on clavicle_r -> Shoulder + Arm upperarm_r..hand_r)",
          r_sh == {"FINISHED"} and sh_r is not None and arm_r is not None and
          [b.bone for b in arm_r.bones] == ["upperarm_r", "lowerarm_r", "hand_r"],
          str(arm_r and [b.bone for b in arm_r.bones]))


def main():
    test_partial_rigs()
    test_module_experimentation()
    test_custom_sockets_and_mode_switch()
    passed = sum(ok for _, ok, _ in RESULTS)
    total = len(RESULTS)
    print(f"\nSUMMARY: {passed} / {total} passed")
    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
