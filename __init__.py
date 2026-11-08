"""Rig Bridge: modular control rigs for Blender, in the spirit of Unreal
Engine's Modular Control Rig.

* **Module Assets**: tiles (Spine, Neck, Arm, Leg, Foot, Finger, Tail, Dynamic
  Chain, Cape, Piston, Aim, IK Chain, Control…).
* **Sockets** in the viewport show where a module can go, with a suggestion.
  Select bones and click a tile, or click a tile and then click a socket.
* **Connectors** (Start/End, Cylinder/Rod, Chains[]…) auto-resolve, can be
  re-pointed in Details, and turn red when unresolved.
* **Module Hierarchy**: a tree that runs root to leaf, with Mirror, Duplicate
  and Auto Resolve.
* **Compile** builds a Rigify-quality body rig plus native modules. The
  imported skeleton is driven non-destructively, ready to bake back to
  UE/Mixamo.
"""

bl_info = {
    "name": "Rig Bridge",
    "author": "Nils Gallist",
    "version": (0, 4, 2),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Rig Bridge",
    "description": "Modular control rigs (Unreal-style) on top of Rigify; Mixamo/UE auto-detect",
    "category": "Rigging",
}

import glob
import importlib.util
import json
import os
import sys

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)

from . import builder, detect, modular, modules, native, overlay, resolve, stdmatch
from .modules import CATEGORIES, MODULES, PartSpec
from .naming import core_tokens, is_helper, side_of

sys.modules.setdefault("rig_bridge_api", modules)   # stable import name for user modules
_ENUM_CACHE = {}


def _module_items(self, context):
    _ENUM_CACHE["modules"] = [(k, m.label, m.description, m.icon, i)
                              for i, (k, m) in enumerate(sorted(MODULES.items(),
                                                                key=lambda kv: (CATEGORIES.index(kv[1].category)
                                                                                if kv[1].category in CATEGORIES else 99,
                                                                                kv[1].label)))]
    return _ENUM_CACHE["modules"]


def _profile_items(self, context):
    if not detect.PROFILES:
        detect.load_profiles()
    items = [("AUTO", "Auto-detect", "Your profiles, then every known standard, then topology analysis")]
    for pid, p in sorted(detect.PROFILES.items()):
        tag = " (user)" if p.get("_user") else ""
        items.append((pid, p.get("label", pid) + tag, p.get("description", "")))
    for std in stdmatch.standards():
        if not std.control_rig:
            items.append((std.pid, std.name, std.description + "  [standard shared with Bulletproof Retarget]"))
    items.append((detect.TOPOLOGY, "Topology (any humanoid)", "Name hints for head/hands/feet + bone hierarchy"))
    _ENUM_CACHE["profiles"] = items
    return items


def _retarget_addon():
    """Bulletproof Retarget installed? (separate add-on; only used for a hint)"""
    return any(k.endswith("bulletproof_retarget") for k in bpy.context.preferences.addons.keys())


def _is_armature(self, obj):
    return obj.type == "ARMATURE"


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------

class RB_BoneRef(bpy.types.PropertyGroup):
    bone: StringProperty(name="Bone")


def ensure_settings(part, reset=False):
    mod = MODULES.get(part.module)
    if mod is None:
        return
    for st in mod.settings:
        key = "s_" + st.key
        if reset or key not in part:
            v = st.default
            part[key] = float(v) if isinstance(v, float) else v
            kw = {"description": st.desc or st.label}
            if st.min is not None and not isinstance(v, bool):
                kw["min"] = kw["soft_min"] = st.min
            if st.max is not None and not isinstance(v, bool):
                kw["max"] = kw["soft_max"] = st.max
            try:
                part.id_properties_ui(key).update(**kw)
            except Exception:
                pass


def _module_update(self, context):
    ensure_settings(self)


def _set_bones(part, names):
    part.bones.clear()
    for n in names:
        part.bones.add().bone = n


def _get_start(self):
    return self.bones[0].bone if len(self.bones) else ""


def _set_start(self, value):
    src = self.id_data
    mod = MODULES.get(self.module)
    if not value or mod is None or value not in src.data.bones:
        if len(self.bones):
            self.bones[0].bone = value
        else:
            self.bones.add().bone = value
        return
    bones, tip = mod.auto_resolve(src, value)      # connector event
    _set_bones(self, bones)
    self.tip = tip


def _get_end(self):
    return self.bones[len(self.bones) - 1].bone if len(self.bones) else ""


def _set_end(self, value):
    src = self.id_data
    start = _get_start(self)
    if not value or not start or value not in src.data.bones or start not in src.data.bones:
        return
    path = []
    b = src.data.bones[value]
    while b and b.name != start:
        path.append(b.name)
        b = b.parent
    if b is None:            # not below Start: treat as new start
        _set_start(self, value)
        return
    chain = [start] + list(reversed(path))
    if self.module in ("arm", "leg", "ik_chain"):
        chain = [c for c in chain if not (set(core_tokens(c)) & {"twist", "roll"})]
    _set_bones(self, chain)


def _slot(i):
    def g(self):
        return self.bones[i].bone if len(self.bones) > i else ""

    def s(self, v):
        while len(self.bones) <= i:
            self.bones.add()
        self.bones[i].bone = v
    return g, s


class RB_Part(bpy.types.PropertyGroup):
    enabled: BoolProperty(name="Enabled", default=True)
    module: EnumProperty(name="Module", items=_module_items, update=_module_update)
    part_name: StringProperty(name="Name", description="Base name for the generated controls")
    side: EnumProperty(name="Side", items=[("NONE", "Center", ""), ("L", "Left", ""), ("R", "Right", "")])
    bones: CollectionProperty(type=RB_BoneRef)
    tip: StringProperty(name="Tip", description="Optional end bone, only used for its position")
    parent: StringProperty(name="Socket", description="Attach this module to another bone (empty = automatic)")
    start: StringProperty(name="Start", get=_get_start, set=_set_start,
                          description="First bone. Setting it re-runs auto-resolve")
    end: StringProperty(name="End", get=_get_end, set=_set_end, description="Last bone of the chain")
    slot0: StringProperty(name="Slot 0", get=_slot(0)[0], set=_slot(0)[1])
    slot1: StringProperty(name="Slot 1", get=_slot(1)[0], set=_slot(1)[1])
    slot2: StringProperty(name="Slot 2", get=_slot(2)[0], set=_slot(2)[1])
    slot3: StringProperty(name="Slot 3", get=_slot(3)[0], set=_slot(3)[1])


def _active_update(self, context):
    _select_part_bones(self, context)


class RB_Settings(bpy.types.PropertyGroup):
    profile: EnumProperty(name="Profile", items=_profile_items)
    status: StringProperty()
    used_profile: StringProperty()
    bake_mode: EnumProperty(name="Limbs", items=builder.BAKE_MODES, default="IK_LEGS",
                            description="Which IK/FK switch state the baked animation starts in")
    parts: CollectionProperty(type=RB_Part)
    active_part: IntProperty(update=_active_update)
    metarig: PointerProperty(type=bpy.types.Object)
    rig: PointerProperty(type=bpy.types.Object)
    source: PointerProperty(type=bpy.types.Object)
    anim_source: PointerProperty(name="Animation", type=bpy.types.Object, poll=_is_armature,
                                 description="Armature whose action is transferred onto the rig's FK controls")
    mute_after_bake: BoolProperty(name="Mute binding after bake", default=True)
    control_size: FloatProperty(name="Control Size", default=1.0, min=0.05, max=20.0,
                                description="Global control scale (Unreal: root metadata)")
    side_colors: BoolProperty(name="Side Colors", default=False,
                              description="Left controls blue, right red, center yellow (Unreal convention)")
    show_sockets: BoolProperty(name="Sockets", default=True, description="Show the schematic overlay")
    empty_only: BoolProperty(name="Empty Only", default=False, description="Show only empty sockets")
    show_helpers: BoolProperty(name="Helper Bones", default=False,
                               description="Also offer twist/IK/end helper bones as sockets")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def get_source(context):
    return overlay.source_of(context.object)


def _part_spec(p, with_settings=True):
    mod = MODULES.get(p.module)
    keep_empty = mod is not None and mod.kind == "slots"
    bones = [b.bone for b in p.bones if (b.bone or keep_empty)]
    settings = {}
    if mod and with_settings:
        for s in mod.settings:
            settings[s.key] = p.get("s_" + s.key, s.default)
    return PartSpec(p.module, p.part_name, "" if p.side == "NONE" else p.side,
                    bones, p.tip, settings, p.parent)


def specs_from_settings(st):
    return [_part_spec(p) for p in st.parts if p.enabled]


def specs_from_settings_part(p):
    return _part_spec(p, with_settings=False)


def add_specs(st, specs):
    for s in specs:
        p = st.parts.add()
        p.module = s.module
        p.part_name = s.name
        p.side = s.side or "NONE"
        p.tip = s.tip or ""
        p.parent = s.parent or ""
        _set_bones(p, s.bones)
        ensure_settings(p)
        mod = MODULES.get(s.module)
        for k, v in (s.settings or {}).items():
            if mod and any(x.key == k for x in mod.settings):
                p["s_" + k] = v
    if specs:
        st.active_part = len(st.parts) - 1


def settings_from_specs(st, specs):
    st.parts.clear()
    add_specs(st, specs)
    st.active_part = 0


def _select_part_bones(st, context):
    src = st.id_data
    if not (context and context.object == src and src.mode == "POSE"):
        return
    if not (0 <= st.active_part < len(st.parts)):
        return
    p = st.parts[st.active_part]
    names = {b.bone for b in p.bones}
    mod = MODULES.get(p.module)
    if mod:
        try:
            names |= set(mod.expand(src, specs_from_settings_part(p)))
        except Exception:
            pass
    for pb in src.pose.bones:
        builder.pose_select(pb, pb.name in names)


def part_tree(src, st):
    """[(index, depth, parent_index)] in execution order (root -> leaf)."""
    specs = [specs_from_settings_part(p) for p in st.parts]
    owner = {}
    for i, s in enumerate(specs):
        mod = MODULES.get(s.module)
        try:
            bs = mod.expand(src, s) if mod else s.bones
        except Exception:
            bs = s.bones
        for b in bs:
            if b:
                owner.setdefault(b, i)
    parent = {}
    for i, s in enumerate(specs):
        first = next((b for b in s.bones if b), None)
        par = None
        if s.parent and s.parent in owner and owner[s.parent] != i:
            par = owner[s.parent]
        elif first and first in src.data.bones:
            b = src.data.bones[first].parent
            while b:
                if b.name in owner and owner[b.name] != i:
                    par = owner[b.name]
                    break
                b = b.parent
        parent[i] = par
    kids = {}
    for i, p in parent.items():
        kids.setdefault(p, []).append(i)
    out, seen = [], set()

    def walk(p, depth):
        for i in kids.get(p, []):
            if i in seen:
                continue
            seen.add(i)
            out.append((i, depth, p))
            walk(i, depth + 1)
    walk(None, 0)
    for i in range(len(specs)):           # cycles / leftovers
        if i not in seen:
            out.append((i, 0, None))
    return out


def part_problem(src, p):
    mod = MODULES.get(p.module)
    if mod is None:
        return "unknown module"
    spec = specs_from_settings_part(p)
    e = mod.validate(spec)
    if e:
        return e
    for b in spec.bones:
        if b and b not in src.data.bones:
            return f"missing bone '{b}'"
    return ""


def selected_socket_bones(context, src):
    if src is not None and context.object is src and src.mode == "POSE":
        sel = context.selected_pose_bones or []
        act = context.active_pose_bone
        names = [b.name for b in sel]
        if act and act.name in names:          # active bone first
            names.remove(act.name)
            names.insert(0, act.name)
        return names
    return []


def frame_range(context, obj=None):
    if obj and obj.animation_data and obj.animation_data.action:
        a, b = obj.animation_data.action.frame_range
        return int(a), int(b)
    return context.scene.frame_start, context.scene.frame_end


def load_user_modules():
    d = detect.user_dir("modules")
    for path in sorted(glob.glob(os.path.join(d, "*.py"))):
        name = f"rig_bridge_user_{os.path.splitext(os.path.basename(path))[0]}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            print(f"[Rig Bridge] user module {path} failed: {exc}")


def _redraw(context):
    for area in (context.screen.areas if context.screen else []):
        if area.type == "VIEW_3D":
            area.tag_redraw()


# ---------------------------------------------------------------------------
# operators: modular editing
# ---------------------------------------------------------------------------

class _SourceOp:
    @classmethod
    def poll(cls, context):
        return get_source(context) is not None


class RIGBRIDGE_OT_auto_rig(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.auto_rig"
    bl_label = "Auto Rig"
    bl_description = "Fill every empty socket with the suggested module (keeps existing modules)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        st = src.rig_bridge
        before = specs_from_settings(st)
        specs, msg = modular.auto_rig(src, before, st.profile)
        add_specs(st, specs[len(before):])
        st.status = msg
        st.used_profile = modular.LAST_DETECT.get("pid", "")
        self.report({"INFO"}, msg)
        _redraw(context)
        return {"FINISHED"}


class RIGBRIDGE_OT_detect(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.detect"
    bl_label = "Detect Skeleton"
    bl_description = "Replace the module list with the detected profile/topology (humanoid only)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        st = src.rig_bridge
        parts, used, msg = detect.detect(src, st.profile)
        for p in parts:
            p.settings = modular.default_settings(p.module) | p.settings
        settings_from_specs(st, parts)
        st.status, st.used_profile = msg, used
        self.report({"INFO"} if parts else {"WARNING"}, msg)
        return {"FINISHED"}


def place_on(op, context, src, st, mid, bones):
    specs = specs_from_settings(st)
    used = modular.assigned(src, specs)
    free = [b for b in bones if b not in used]
    if not free:
        op.report({"WARNING"}, "Selected bones already have modules")
        return {"CANCELLED"}
    new = modular.place(src, specs, mid, free)
    if not new:
        op.report({"WARNING"}, "Nothing to place")
        return {"CANCELLED"}
    add_specs(st, new)
    names = ", ".join(f"{s.name}{'.' + s.side if s.side else ''}" for s in new)
    op.report({"INFO"}, f"Added {MODULES[mid].label}: {names}")
    _redraw(context)
    return {"FINISHED"}


class RIGBRIDGE_OT_add_module(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.add_module"
    bl_label = "Add Module"
    bl_description = "Drop this module on the selected bone(s). Nothing selected: click a socket in the viewport"
    bl_options = {"REGISTER", "UNDO"}
    module: StringProperty()

    @classmethod
    def description(cls, context, props):
        m = MODULES.get(props.module)
        if not m:
            return cls.bl_description
        return (f"{m.description}\n\nSelect socket bone(s) in Pose Mode and click, "
                "or click and then pick a socket in the viewport")

    def invoke(self, context, event):
        src = get_source(context)
        if selected_socket_bones(context, src):
            return self.execute(context)
        if context.area and context.area.type == "VIEW_3D":
            bpy.ops.rig_bridge.place_modal("INVOKE_DEFAULT", module=self.module)
            return {"FINISHED"}
        self.report({"WARNING"}, "Select bones in Pose Mode first")
        return {"CANCELLED"}

    def execute(self, context):
        src = get_source(context)
        return place_on(self, context, src, src.rig_bridge, self.module, selected_socket_bones(context, src))


class RIGBRIDGE_OT_place_modal(bpy.types.Operator):
    bl_idname = "rig_bridge.place_modal"
    bl_label = "Place Module"
    bl_description = "Click sockets in the viewport to place the module"
    bl_options = {"REGISTER", "UNDO"}
    module: StringProperty()

    def _region(self, context):
        for r in context.area.regions:
            if r.type == "WINDOW":
                return r
        return None

    def invoke(self, context, event):
        src = get_source(context)
        if src is None or context.area is None or context.area.type != "VIEW_3D":
            return {"CANCELLED"}
        self.src_name = src.name
        self.placed = 0
        overlay.PLACING.update(module=self.module, hover=None, src=src.name)
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set("EYEDROPPER")
        context.area.tag_redraw()
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        overlay.PLACING.update(module="", hover=None, src="")
        context.window.cursor_modal_restore()
        if context.area:
            context.area.tag_redraw()

    def modal(self, context, event):
        src = bpy.data.objects.get(self.src_name)
        region = self._region(context) if context.area else None
        if src is None or region is None:
            self._finish(context)
            return {"CANCELLED"}
        mx, my = event.mouse_x - region.x, event.mouse_y - region.y
        inside = 0 <= mx <= region.width and 0 <= my <= region.height
        if event.type == "MOUSEMOVE":
            overlay.PLACING["hover"] = overlay.pick(context, context.area, region, mx, my, src) if inside else None
            context.area.tag_redraw()
        elif event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not inside:
                return {"PASS_THROUGH"}
            b = overlay.pick(context, context.area, region, mx, my, src)
            if b:
                place_on(self, context, src, src.rig_bridge, self.module, [b])
                self.placed += 1
                if not event.shift:
                    self._finish(context)
                    return {"FINISHED"}
            return {"RUNNING_MODAL"}
        elif event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"FINISHED"} if self.placed else {"CANCELLED"}
        elif event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "TRACKPADPAN", "TRACKPADZOOM"}:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}


def _active_part(context):
    src = get_source(context)
    if src is None:
        return None, None, None
    st = src.rig_bridge
    if 0 <= st.active_part < len(st.parts):
        return src, st, st.parts[st.active_part]
    return src, st, None


class RIGBRIDGE_OT_part_remove(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.part_remove"
    bl_label = "Delete Module"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is not None:
            st.parts.remove(st.active_part)
            st.active_part = max(0, st.active_part - 1)
        _redraw(context)
        return {"FINISHED"}


class RIGBRIDGE_OT_part_clear(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.part_clear"
    bl_label = "Clear All Modules"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        get_source(context).rig_bridge.parts.clear()
        _redraw(context)
        return {"FINISHED"}


class RIGBRIDGE_OT_part_mirror(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.part_mirror"
    bl_label = "Mirror Module"
    bl_description = "Create the same module on the other side (Left <-> Right bone names)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is None:
            return {"CANCELLED"}
        new, msg = modular.mirror_spec(src, specs_from_settings(st), _part_spec(p))
        if new is None:
            self.report({"WARNING"}, msg)
            return {"CANCELLED"}
        add_specs(st, [new])
        self.report({"INFO"}, msg)
        _redraw(context)
        return {"FINISHED"}


class RIGBRIDGE_OT_part_duplicate(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.part_duplicate"
    bl_label = "Duplicate Module"
    bl_description = "Copy module type and settings; connectors start unresolved so you can re-point them"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is None:
            return {"CANCELLED"}
        spec = _part_spec(p)
        mod = MODULES[p.module]
        n = len(mod.slots) if mod.kind == "slots" else 1
        spec.name = modular.unique_name(specs_from_settings(st), spec.name, spec.side)
        spec.bones, spec.tip, spec.parent = [""] * n, "", ""
        add_specs(st, [spec])
        return {"FINISHED"}


class RIGBRIDGE_OT_part_resolve(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.part_resolve"
    bl_label = "Auto Resolve"
    bl_description = "Re-run the module's connector event from its first bone"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is None or not len(p.bones) or not p.bones[0].bone:
            self.report({"WARNING"}, "Set the first connector first")
            return {"CANCELLED"}
        others = [_part_spec(q) for i, q in enumerate(st.parts) if i != st.active_part and q.enabled]
        bones, tip = MODULES[p.module].auto_resolve(src, p.bones[0].bone, used=modular.assigned(src, others))
        _set_bones(p, bones)
        p.tip = tip
        return {"FINISHED"}


class RIGBRIDGE_OT_use_selected(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.use_selected"
    bl_label = "Use Selected"
    bl_description = "Resolve this connector to the active bone"
    bl_options = {"REGISTER", "UNDO"}
    target: StringProperty()

    def execute(self, context):
        src, st, p = _active_part(context)
        act = context.active_pose_bone if context.object is src else None
        if p is None or act is None:
            self.report({"WARNING"}, "Select a bone of the skeleton in Pose Mode")
            return {"CANCELLED"}
        if self.target == "array":
            have = {b.bone for b in p.bones}
            for b in selected_socket_bones(context, src):
                if b not in have:
                    p.bones.add().bone = b
        else:
            setattr(p, self.target, act.name)
        return {"FINISHED"}


class RIGBRIDGE_OT_bone_add(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.bone_add"
    bl_label = "Add Entry"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is not None:
            p.bones.add()
        return {"FINISHED"}


class RIGBRIDGE_OT_bone_remove(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.bone_remove"
    bl_label = "Remove Entry"
    bl_options = {"REGISTER", "UNDO"}
    index: IntProperty()

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is not None and 0 <= self.index < len(p.bones):
            p.bones.remove(self.index)
        return {"FINISHED"}


class RIGBRIDGE_OT_reset_settings(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.reset_settings"
    bl_label = "Reset Settings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src, st, p = _active_part(context)
        if p is not None:
            ensure_settings(p, reset=True)
        return {"FINISHED"}


class RIGBRIDGE_OT_part_add(_SourceOp, bpy.types.Operator):
    """Kept for scripts written against 0.1 (dialog version of Add Module)."""
    bl_idname = "rig_bridge.part_add"
    bl_label = "Add Part"
    bl_options = {"REGISTER", "UNDO"}
    module: EnumProperty(name="Module", items=_module_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        src = get_source(context)
        sel = selected_socket_bones(context, src)
        if sel:
            return place_on(self, context, src, src.rig_bridge, self.module,
                            sel if MODULES[self.module].kind == "array" else sel[:1])
        add_specs(src.rig_bridge, [PartSpec(self.module, MODULES[self.module].base_name or self.module, "",
                                            [""], "", modular.default_settings(self.module))])
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# operators: compile / rig
# ---------------------------------------------------------------------------

class RIGBRIDGE_OT_build(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.build"
    bl_label = "Compile"
    bl_description = "Build the control rig from the modules and bind the skeleton to it"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        st = src.rig_bridge
        if not st.parts:
            bpy.ops.rig_bridge.detect()
            if not st.parts and "already a control rig" not in st.status:
                bpy.ops.rig_bridge.auto_rig()
        if not st.parts:
            self.report({"ERROR"}, st.status or "No modules – add some or run Auto Rig")
            return {"CANCELLED"}
        specs = specs_from_settings(st)
        frame = context.scene.frame_current
        try:
            rig, n = builder.build(context, src, specs)
        except builder.BuildError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        st.metarig.rig_bridge.source = src
        context.scene.frame_set(frame)
        builder.set_mode(context, rig, "POSE")
        # animators work on the control rig only; the game skeleton stays bound
        # (and evaluated) but out of the way – toggle in Animate ▸ Skeleton
        src.hide_set(True)
        self.report({"INFO"}, f"Compiled '{rig.name}': {len(specs)} modules, {n} bones driven")
        return {"FINISHED"}


class _RigOp(_SourceOp):
    @classmethod
    def poll(cls, context):
        src = get_source(context)
        return src is not None and src.rig_bridge.rig is not None


class RIGBRIDGE_OT_select_rig(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.select_rig"
    bl_label = "Select Rig"

    def execute(self, context):
        builder.set_mode(context, get_source(context).rig_bridge.rig, "POSE")
        return {"FINISHED"}


class RIGBRIDGE_OT_bind(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.bind"
    bl_label = "Rebind"
    bl_description = "Re-create the constraints from the skeleton to the control rig (rest pose)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        try:
            n = builder.bind(context, src, src.rig_bridge.rig)
        except builder.BuildError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{n} bones bound")
        return {"FINISHED"}


class RIGBRIDGE_OT_toggle_skeleton(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.toggle_skeleton"
    bl_label = "Show / Hide Skeleton"
    bl_description = "Show or hide the original (game) skeleton. It keeps following the rig either way"

    def execute(self, context):
        src = get_source(context)
        src.hide_set(not src.hide_get())
        return {"FINISHED"}


class RIGBRIDGE_OT_mute(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.mute"
    bl_label = "Mute Binding"
    bl_options = {"REGISTER", "UNDO"}
    mute: BoolProperty(default=True)

    def execute(self, context):
        n = builder.set_bind_muted(get_source(context), self.mute)
        self.report({"INFO"}, f"{'Muted' if self.mute else 'Enabled'} {n} constraints")
        return {"FINISHED"}


class RIGBRIDGE_OT_unbind(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.unbind"
    bl_label = "Remove Binding"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        builder.unbind(get_source(context))
        return {"FINISHED"}


class RIGBRIDGE_OT_bake(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.bake"
    bl_label = "Bake to Skeleton"
    bl_description = "Bake the rig animation (incl. dynamics) onto the original skeleton for FBX export"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        rig = src.rig_bridge.rig
        a, b = frame_range(context, rig)
        if rig.get("rb_dyn"):
            a, b = context.scene.frame_start, context.scene.frame_end
        act = builder.bake_to_source(context, src, rig, a, b, src.rig_bridge.mute_after_bake)
        self.report({"INFO"}, f"Baked frames {a}-{b} into '{act.name if act else '?'}'")
        return {"FINISHED"}


class RIGBRIDGE_OT_transfer(_RigOp, bpy.types.Operator):
    bl_idname = "rig_bridge.transfer"
    bl_label = "Bake to Control Rig"
    bl_description = ("Bake the skeleton's animation (or the 'From' armature's, same skeleton type) onto the "
                      "control rig: FK and IK controls are both keyed, so the IK/FK switch works on every frame")
    bl_options = {"REGISTER", "UNDO"}
    mode: EnumProperty(name="Limbs", items=builder.BAKE_MODES, default="IK_LEGS")

    def invoke(self, context, event):
        self.mode = get_source(context).rig_bridge.bake_mode
        return self.execute(context)

    def execute(self, context):
        src = get_source(context)
        st = src.rig_bridge
        anim = st.anim_source or src
        a, b = frame_range(context, anim)
        try:
            act, n = builder.transfer_to_rig(context, src, st.rig, anim, a, b, mode=self.mode)
            if anim is src:
                builder.set_bind_muted(src, False)   # the control rig drives the skeleton again
        except builder.BuildError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Transferred {n} controls, frames {a}-{b} → '{act.name}'")
        return {"FINISHED"}


class RIGBRIDGE_OT_dynamics_bake(bpy.types.Operator):
    bl_idname = "rig_bridge.dynamics_bake"
    bl_label = "Bake Dynamics"
    bl_description = "Bake (or free) all soft-body/cloth caches. Baked = instant scrubbing"
    bl_options = {"REGISTER"}
    free: BoolProperty(default=False)

    def execute(self, context):
        bpy.ops.ptcache.free_bake_all()
        if not self.free:
            bpy.ops.ptcache.bake_all(bake=True)
        self.report({"INFO"}, "Dynamics cache freed" if self.free else "Dynamics baked")
        return {"FINISHED"}


class RIGBRIDGE_OT_add_collider(bpy.types.Operator):
    bl_idname = "rig_bridge.add_collider"
    bl_label = "Make Collider"
    bl_description = ("Selected meshes collide with capes / dynamic chains. Meshes skinned to the skeleton get a "
                      "proxy driven by the control rig (avoids a dependency cycle)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        src = get_source(context)
        rig = src.rig_bridge.rig if src else None
        cr_map = {}
        if rig is not None:
            for p in json.loads(rig.get(builder.PAIRS_KEY, "[]")):
                if len(p) < 3 or not p[2]:
                    cr_map[p[0]] = p[1]
        n = 0
        for o in list(context.selected_objects):
            if o.type != "MESH" or o.get("rb_kind"):
                continue
            arm = next((m for m in o.modifiers if m.type == "ARMATURE" and src is not None and m.object is src), None)
            if arm is not None and rig is not None:
                name = f"COL-{o.name}"
                old = bpy.data.objects.get(name)
                if old is not None:
                    bpy.data.objects.remove(old)
                proxy = bpy.data.objects.new(name, o.data.copy())
                native.sim_collection().objects.link(proxy)
                proxy.matrix_world = o.matrix_world.copy()
                for vg in proxy.vertex_groups:      # group names live on the (copied) mesh
                    vg.name = cr_map.get(vg.name, vg.name)
                m = proxy.modifiers.new("Rig", "ARMATURE")
                m.object = rig
                proxy.modifiers.new("Collision", "COLLISION")
                proxy.display_type = "WIRE"
                proxy.hide_render = True
                proxy.hide_select = True
                proxy["rb_src"] = src.name
                proxy["rb_kind"] = "collider"
            elif not any(m.type == "COLLISION" for m in o.modifiers):
                o.modifiers.new("Collision", "COLLISION")
            n += 1
        self.report({"INFO"}, f"{n} collider(s) added")
        return {"FINISHED"}


class RIGBRIDGE_OT_save_profile(_SourceOp, bpy.types.Operator):
    bl_idname = "rig_bridge.save_profile"
    bl_label = "Save as Profile"
    bl_description = "Save the module list as a reusable JSON profile (Auto-detect recognises it next time)"
    pid: StringProperty(name="ID", default="my_rig")
    label: StringProperty(name="Label", default="My Rig")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        src = get_source(context)
        data = detect.parts_to_profile(specs_from_settings(src.rig_bridge), self.pid, self.label, src)
        path = os.path.join(detect.user_dir("profiles"), f"{self.pid}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        detect.load_profiles()
        self.report({"INFO"}, f"Saved {path}")
        return {"FINISHED"}


class RIGBRIDGE_OT_reload(bpy.types.Operator):
    bl_idname = "rig_bridge.reload"
    bl_label = "Reload Profiles & Modules"
    bl_description = "Re-read JSON profiles and user module files"

    def execute(self, context):
        detect.load_profiles()
        load_user_modules()
        self.report({"INFO"}, f"{len(detect.PROFILES)} profiles, {len(MODULES)} modules")
        return {"FINISHED"}


class RIGBRIDGE_OT_open_user_dir(bpy.types.Operator):
    bl_idname = "rig_bridge.open_user_dir"
    bl_label = "Open User Folder"
    bl_description = "Open the folder holding your profiles/ and modules/"

    def execute(self, context):
        d = os.path.dirname(detect.user_dir("profiles"))
        detect.user_dir("modules")
        bpy.ops.wm.path_open(filepath=d)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class RIGBRIDGE_UL_parts(bpy.types.UIList):
    """Module Hierarchy: indented tree in execution order."""

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        try:
            tree = part_tree(data.id_data, data)
        except Exception:
            tree = [(i, 0, None) for i in range(len(items))]
        self._depth = {i: d for i, d, _ in tree}
        order = [0] * len(items)
        for pos, (i, d, p) in enumerate(tree):
            order[i] = pos
        flags = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            f = self.filter_name.lower()
            for i, it in enumerate(items):
                txt = f"{it.part_name} {it.module} " + " ".join(b.bone for b in it.bones)
                if f not in txt.lower():
                    flags[i] = 0
        return flags, order

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        src = data.id_data
        depth = getattr(self, "_depth", {}).get(index, 0)
        mod = MODULES.get(item.module)
        prob = part_problem(src, item)
        row = layout.row(align=True)
        for _ in range(min(depth, 6)):
            row.label(text="", icon="BLANK1")
        row.prop(item, "enabled", text="")
        side = "" if item.side == "NONE" else f".{item.side}"
        sub = row.row(align=True)
        sub.alert = bool(prob)
        sub.label(text=f"{item.part_name}{side}", icon=mod.icon if mod else "QUESTION")
        r = row.row()
        r.alignment = "RIGHT"
        r.alert = bool(prob)
        r.label(text=(mod.tile if mod else item.module), icon="ERROR" if prob else "NONE")


class RIGBRIDGE_PT_main(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rig Bridge"
    bl_label = "Rig Bridge"

    def draw(self, context):
        layout = self.layout
        src = get_source(context)
        if src is None:
            layout.label(text="Select an armature", icon="INFO")
            return
        st = src.rig_bridge
        row = layout.row()
        row.label(text=src.name, icon="ARMATURE_DATA")
        if context.object is src and src.mode != "POSE":
            row.operator("object.mode_set", text="Pose Mode", icon="POSE_HLT").mode = "POSE"
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("rig_bridge.auto_rig", icon="SHADERFX")
        row.operator("rig_bridge.build", icon="FILE_REFRESH")
        if st.status:
            layout.label(text=st.status)
        row = layout.row(align=True)
        row.prop(st, "show_sockets", toggle=True, icon="DECORATE_LINKED")
        row.prop(st, "empty_only", toggle=True)
        row.prop(st, "show_helpers", toggle=True)
        row = layout.row(align=True)
        row.prop(st, "control_size")
        row.prop(st, "side_colors", toggle=True, icon="COLOR")


class _Sub:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rig Bridge"
    bl_parent_id = "RIGBRIDGE_PT_main"

    @classmethod
    def poll(cls, context):
        return get_source(context) is not None


class RIGBRIDGE_PT_assets(_Sub, bpy.types.Panel):
    bl_label = "Module Assets"

    def draw(self, context):
        layout = self.layout
        src = get_source(context)
        sel = selected_socket_bones(context, src)
        hint = modular.suggest(src, sel[0]) if sel else None
        if sel:
            layout.label(text=f"Drop on: {', '.join(sel[:3])}{'…' if len(sel) > 3 else ''}", icon="BONE_DATA")
        else:
            layout.label(text="Pick bones, or click a tile + a socket", icon="INFO")
        for cat in CATEGORIES:
            mods = [m for m in MODULES.values() if m.category == cat and m.in_library]
            if not mods:
                continue
            col = layout.column(align=True)
            col.label(text=cat)
            grid = col.grid_flow(row_major=True, columns=3, even_columns=True, align=True)
            for m in sorted(mods, key=lambda m: m.tile):
                op = grid.operator("rig_bridge.add_module", text=m.tile, icon=m.icon, depress=(m.id == hint))
                op.module = m.id


class RIGBRIDGE_PT_hierarchy(_Sub, bpy.types.Panel):
    bl_label = "Module Hierarchy"

    def draw(self, context):
        layout = self.layout
        st = get_source(context).rig_bridge
        row = layout.row()
        row.template_list("RIGBRIDGE_UL_parts", "", st, "parts", st, "active_part", rows=8)
        col = row.column(align=True)
        col.operator("rig_bridge.part_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("rig_bridge.part_mirror", text="", icon="MOD_MIRROR")
        col.operator("rig_bridge.part_duplicate", text="", icon="DUPLICATE")
        col.operator("rig_bridge.part_resolve", text="", icon="SNAP_ON")
        col.separator()
        col.operator("rig_bridge.part_clear", text="", icon="TRASH")


class RIGBRIDGE_PT_details(_Sub, bpy.types.Panel):
    bl_label = "Details"

    @classmethod
    def poll(cls, context):
        return _active_part(context)[2] is not None

    def draw(self, context):
        layout = self.layout
        src, st, p = _active_part(context)
        mod = MODULES.get(p.module)
        layout.prop(p, "module", text="Module")
        r = layout.row(align=True)
        r.prop(p, "part_name", text="Name")
        r.prop(p, "side", text="")
        prob = part_problem(src, p)
        if prob:
            b = layout.box()
            b.alert = True
            b.label(text=prob, icon="ERROR")
        if mod is None:
            return
        box = layout.box()
        box.label(text="Connectors", icon="LINKED")
        data = src.data
        for label, kind, idx, optional in mod.connectors():
            if kind == "array":
                for i, b in enumerate(p.bones):
                    rr = box.row(align=True)
                    rr.alert = not b.bone
                    rr.prop_search(b, "bone", data, "bones", text=f"{label} {i + 1}")
                    rr.operator("rig_bridge.bone_remove", text="", icon="X").index = i
                rr = box.row(align=True)
                rr.operator("rig_bridge.bone_add", icon="ADD")
                rr.operator("rig_bridge.use_selected", text="Add Selected", icon="EYEDROPPER").target = "array"
                continue
            prop = {"start": "start", "end": "end"}.get(kind, f"slot{idx}")
            rr = box.row(align=True)
            rr.alert = not getattr(p, prop) and not optional
            rr.prop_search(p, prop, data, "bones", text=label + (" (opt.)" if optional else ""))
            rr.operator("rig_bridge.use_selected", text="", icon="EYEDROPPER").target = prop
        if mod.kind == "chain":
            box.label(text=f"{len(p.bones)} bones: " + " › ".join(b.bone for b in p.bones)[:120])
            box.prop_search(p, "tip", data, "bones", text="Tip (opt.)")
        rr = box.row(align=True)
        rr.prop_search(p, "parent", data, "bones", text="Socket")
        rr.operator("rig_bridge.use_selected", text="", icon="EYEDROPPER").target = "parent"
        if not p.parent:
            box.label(text="Socket: automatic (nearest driven parent)", icon="INFO")
        if mod.settings:
            box = layout.box()
            row = box.row()
            row.label(text="Settings", icon="PREFERENCES")
            row.operator("rig_bridge.reset_settings", text="", icon="LOOP_BACK")
            for s in mod.settings:
                key = "s_" + s.key
                if key in p:
                    box.prop(p, f'["{key}"]', text=s.label)
                else:
                    box.label(text=f"{s.label}: {s.default} (Reset Settings to edit)")


class RIGBRIDGE_PT_rig(_Sub, bpy.types.Panel):
    bl_label = "Animate"

    @classmethod
    def poll(cls, context):
        src = get_source(context)
        return src is not None and src.rig_bridge.rig is not None

    def draw(self, context):
        layout = self.layout
        src = get_source(context)
        st = src.rig_bridge
        rig = st.rig
        row = layout.row()
        row.label(text=rig.name, icon="POSE_HLT")
        if context.object is not rig:
            row.operator("rig_bridge.select_rig", text="Select Rig", icon="RESTRICT_SELECT_OFF")
        info = json.loads(rig.get(builder.INFO_KEY, "[]"))
        if info:
            box = layout.box()
            box.label(text="Module Controls", icon="DRIVER")
            for ent in info:
                pb = rig.pose.bones.get(ent["bone"])
                if pb is None:
                    continue
                col = box.column(align=True)
                col.label(text=ent["label"])
                for prop in ent["props"]:
                    if prop in pb.keys():
                        col.prop(pb, f'["{prop}"]', slider=True)
        if rig.get("rb_dyn"):
            box = layout.box()
            box.label(text="Dynamics: play from the first frame", icon="PHYSICS")
            r = box.row(align=True)
            r.operator("rig_bridge.dynamics_bake", text="Bake", icon="REC").free = False
            r.operator("rig_bridge.dynamics_bake", text="Free", icon="X").free = True
            box.operator("rig_bridge.add_collider", icon="MOD_PHYSICS")
        box = layout.box()
        hdr = box.row()
        hdr.label(text="Skeleton", icon="ARMATURE_DATA")
        hdr.operator("rig_bridge.toggle_skeleton", text="Show" if src.hide_get() else "Hide",
                     icon="HIDE_ON" if src.hide_get() else "HIDE_OFF")
        r = box.row(align=True)
        r.operator("rig_bridge.bind", icon="LINKED")
        r.operator("rig_bridge.mute", text="Mute", icon="HIDE_ON").mute = True
        r.operator("rig_bridge.mute", text="Unmute", icon="HIDE_OFF").mute = False
        box.operator("rig_bridge.unbind", icon="UNLINKED")
        box.prop(st, "mute_after_bake")
        box.operator("rig_bridge.bake", icon="ACTION")

        box = layout.box()
        box.label(text="Bake to Control Rig", icon="IMPORT")
        act = src.animation_data.action if src.animation_data else None
        if act is not None and act.name not in (rig.get("rb_baked_from", ""), rig.get("rb_skel_action", "")) \
                and not st.anim_source:
            warn = box.column(align=True)
            warn.alert = True
            warn.label(text=f"Skeleton has '{act.name}'", icon="INFO")
            warn.label(text="(e.g. from a retarget) – bake it to edit it")
        box.prop(st, "anim_source", text="From")
        box.prop(st, "bake_mode", text="")
        box.operator("rig_bridge.transfer", icon="IMPORT").mode = st.bake_mode
        col = box.column(align=True)
        if _retarget_addon() == "BP Retarget":
            col.label(text="BP Retarget: pick this rig as Source or Target", icon="ARMATURE_DATA")
            col.label(text="(bakes to/from the Control Rig automatically)")
        else:
            col.label(text="Retarget onto this character:", icon="ARMATURE_DATA")
            col.operator("rig_bridge.mute", text="1. Release Skeleton", icon="UNLINKED").mute = True
            col.label(text="2. Retarget onto the skeleton" + (" (BP Retarget)" if _retarget_addon() else ""))
            col.label(text="3. Bake to Control Rig (re-links)")
            col.label(text="Retarget from it: Bake to Skeleton first", icon="EXPORT")


class RIGBRIDGE_PT_profiles(_Sub, bpy.types.Panel):
    bl_label = "Profiles"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        st = get_source(context).rig_bridge
        row = layout.row(align=True)
        row.prop(st, "profile", text="")
        row.operator("rig_bridge.detect", text="Detect", icon="VIEWZOOM")
        r = layout.row(align=True)
        r.operator("rig_bridge.save_profile", icon="FILE_TICK")
        r.operator("rig_bridge.reload", text="", icon="FILE_REFRESH")
        r.operator("rig_bridge.open_user_dir", text="", icon="FILE_FOLDER")


classes = (
    RB_BoneRef, RB_Part, RB_Settings,
    RIGBRIDGE_OT_auto_rig, RIGBRIDGE_OT_detect, RIGBRIDGE_OT_add_module, RIGBRIDGE_OT_place_modal,
    RIGBRIDGE_OT_part_remove, RIGBRIDGE_OT_part_clear, RIGBRIDGE_OT_part_mirror, RIGBRIDGE_OT_part_duplicate,
    RIGBRIDGE_OT_part_resolve, RIGBRIDGE_OT_use_selected, RIGBRIDGE_OT_bone_add, RIGBRIDGE_OT_bone_remove,
    RIGBRIDGE_OT_reset_settings, RIGBRIDGE_OT_part_add, RIGBRIDGE_OT_build, RIGBRIDGE_OT_select_rig,
    RIGBRIDGE_OT_bind, RIGBRIDGE_OT_toggle_skeleton, RIGBRIDGE_OT_mute, RIGBRIDGE_OT_unbind, RIGBRIDGE_OT_bake, RIGBRIDGE_OT_transfer,
    RIGBRIDGE_OT_dynamics_bake, RIGBRIDGE_OT_add_collider, RIGBRIDGE_OT_save_profile, RIGBRIDGE_OT_reload,
    RIGBRIDGE_OT_open_user_dir, RIGBRIDGE_UL_parts,
    RIGBRIDGE_PT_main, RIGBRIDGE_PT_assets, RIGBRIDGE_PT_hierarchy, RIGBRIDGE_PT_details,
    RIGBRIDGE_PT_rig, RIGBRIDGE_PT_profiles,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.rig_bridge = PointerProperty(type=RB_Settings)
    load_user_modules()
    detect.load_profiles()
    overlay.register()


def unregister():
    overlay.unregister()
    del bpy.types.Object.rig_bridge
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
