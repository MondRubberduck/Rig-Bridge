"""Meta-rig construction, Rigify generation, binding, baking and retargeting."""

import json
import math
import re

import bpy
from mathutils import Matrix, Vector

from . import native
from .detect import module_ok
from .modules import AUTO_PARENT, MODULES, BoneLayout, CollDef, PartSpec, merge_feet
from .naming import norm, side_of, strip_namespace, strip_vendor

BIND_PREFIX = "BIND-"
RET_PREFIX = "RET-"
RETC_PREFIX = "RETC-"
CON_PREFIX = "RB_"
BIND_COLL = "RigBridge (Helpers)"
PAIRS_KEY = "rig_bridge_pairs"
FK_KEY = "rig_bridge_fk"
INFO_KEY = "rig_bridge_info"


class BuildError(Exception):
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def ensure_rigify():
    import addon_utils
    if "rigify" not in bpy.context.preferences.addons:
        try:
            bpy.ops.preferences.addon_enable(module="rigify")
        except Exception:
            addon_utils.enable("rigify", default_set=True, persistent=True)
    if not hasattr(bpy.types.PoseBone, "rigify_type"):
        raise BuildError("Rigify could not be enabled")


def set_mode(context, obj, mode):
    if context.object and context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in context.view_layer.objects:
        if o.select_get():
            o.select_set(False)
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.select_set(True)
    context.view_layer.objects.active = obj
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode=mode)


def pose_select(pb, value=True):
    if hasattr(pb, "select"):      # Blender 5.0+
        pb.select = value
    else:
        pb.bone.select = value


def clean_rest(m: Matrix):
    """World rest matrix without scale -> (loc, 3x3 rotation)."""
    loc, rot, _ = m.decompose()
    return loc, rot.to_matrix()


class BuildContext:
    """Geometry helpers shared by modules (all positions in world space)."""

    def __init__(self, src, specs: list[PartSpec]):
        self.src = src
        mw = src.matrix_world
        bones = src.data.bones
        self.head_w = {b.name: mw @ b.head_local for b in bones}
        self.tail_w = {b.name: mw @ b.tail_local for b in bones}
        self.rest_w = {b.name: mw @ b.matrix_local for b in bones}
        self.specs = specs
        self.up = Vector((0, 0, 1))
        self.mapped = {b: s for s in specs for b in (s.all_bones or s.bones) if b}

        pts = list(self.head_w.values()) or [Vector()]
        zs = [p.z for p in pts]
        self.unit = max(max(zs) - min(zs), 1e-3)
        self.floor = Vector((0, 0, min(zs)))
        self.eps = self.unit * 0.004

        side = None
        for mod in ("arm", "leg", "shoulder"):
            L = [s for s in specs if s.module == mod and s.side == "L"]
            R = [s for s in specs if s.module == mod and s.side == "R"]
            if L and R:
                v = self.flat(self.head_w[L[0].bones[0]] - self.head_w[R[0].bones[0]])
                if v.length > 1e-6:
                    side = v.normalized()
                    break
        self.side = side or Vector((1, 0, 0))
        self.forward = self.side.cross(self.up).normalized()

        # nearest mapped ancestor for every mapped bone
        self.mapped_parent = {}
        for b in self.mapped:
            p = bones[b].parent
            while p and p.name not in self.mapped:
                p = p.parent
            self.mapped_parent[b] = p.name if p else None

        # back-of-hand normal per side (for finger roll)
        self.hand_back = {}
        for s in "LR":
            arm = [x for x in specs if x.module == "arm" and x.side == s]
            fing = {x.name: x for x in specs if x.module == "finger" and x.side == s}
            a = fing.get("f_index") or fing.get("f_middle")
            b = fing.get("f_pinky") or fing.get("f_ring")
            if arm and a and b and a is not b:
                h = self.head_w[arm[0].bones[-1]]
                n = (self.head_w[a.bones[0]] - h).cross(self.head_w[b.bones[0]] - h)
                if n.length > 1e-8:
                    self.hand_back[s] = n.normalized() * (1 if s == "L" else -1)

    def flat(self, v):
        return v - self.up * v.dot(self.up)

    def mapped_children(self, bone):
        return [b for b, p in self.mapped_parent.items() if p == bone]

    def chain_geometry(self, spec):
        heads = [self.head_w[b].copy() for b in spec.bones]
        tails = heads[1:] + [self.end_tail(spec, heads)]
        for i in range(len(heads)):
            if (tails[i] - heads[i]).length < self.eps:
                d = self.rest_w[spec.bones[i]].to_3x3().normalized().col[1]
                tails[i] = heads[i] + d * self.eps * 5
        return heads, tails

    def end_tail(self, spec, heads):
        last = spec.bones[-1]
        h = heads[-1]
        if spec.tip and spec.tip in self.head_w:
            t = self.head_w[spec.tip]
            if (t - h).length > self.eps:
                return t.copy()
        n = len(heads)
        if n >= 2:
            d = heads[-1] - heads[-2]
            length = d.length
        else:
            d = self.rest_w[last].to_3x3().normalized().col[1]
            length = (self.tail_w[last] - h).length
        d = d.normalized() if d.length > 1e-9 else self.up.copy()
        kids = [self.head_w[k] for k in self.mapped_children(last)
                if (self.head_w[k] - h).length > self.eps]
        if kids:
            if len(kids) == 1 and n == 1:
                return kids[0].copy()
            best = max(kids, key=lambda p: (p - h).normalized().dot(d))
            if (best - h).normalized().dot(d) > 0.5:
                return best.copy()
        if n == 1 and length > self.eps:
            return self.tail_w[last].copy()
        return h + d * max(length * 0.75, self.eps * 5)


def validate(src, specs):
    errs, seen = [], {}
    for s in specs:
        e = module_ok(s)
        if e:
            errs.append(f"{s.name or s.module}{'.' + s.side if s.side else ''}: {e}")
        for b in (s.all_bones or [x for x in s.bones if x]):
            if b not in src.data.bones:
                errs.append(f"bone '{b}' not found")
            elif b in seen:
                errs.append(f"bone '{b}' used twice ({seen[b]} / {s.name})")
            seen[b] = s.name
    return errs


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

COLOR_SETS = [
    ("Root", (0.4353, 0.1843, 0.4157)), ("IK", (0.6039, 0.0, 0.0)),
    ("Special", (0.9569, 0.7882, 0.0471)), ("Tweak", (0.0392, 0.2118, 0.5804)),
    ("FK", (0.1176, 0.5686, 0.0353)), ("Extra", (0.9686, 0.2510, 0.0941)),
]


def _setup_metarig_look(arm, colldefs):
    from mathutils import Color
    arm.rigify_colors.clear()
    for name, normal in COLOR_SETS:
        c = arm.rigify_colors.add()
        c.name = name
        c.normal = Color(normal)
        c.active = Color((0.5490, 1.0, 1.0))
        c.select = Color((0.3137, 0.7843, 1.0))
        c.standard_colors_lock = True
    for bc in list(arm.collections_all):
        arm.collections.remove(bc)
    colls = {}
    for cd in colldefs:
        if cd.name in colls:
            continue
        bc = arm.collections.new(cd.name)
        bc.rigify_ui_row = cd.row
        bc.rigify_ui_title = cd.title
        bc.rigify_color_set_id = cd.color
        colls[cd.name] = bc
    return colls


def placeholder_layout(ctx, spec):
    """Native parts appear in the meta-rig as plain (untyped) bones, so Rigify
    keeps the hierarchy and Rigify parts below them get the right parent."""
    out = []
    for b in spec.all_bones:
        loc, rot = clean_rest(ctx.rest_w[b])
        L = max((ctx.tail_w[b] - ctx.head_w[b]).length, ctx.eps * 5)
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", b)
        out.append(BoneLayout(f"nat.{safe}", loc, loc + rot.col[1] * L, AUTO_PARENT, False,
                              rot.col[2], b))
    return out


def owns_rig(src):
    """True if src's control rig belongs to src. A Shift+D copy of a compiled skeleton keeps the
    original's rig pointer and binding constraints; it must not bake onto or rebuild that rig (QA I3)."""
    st = getattr(src, "rig_bridge", None)
    rig = st.rig if st is not None else None
    if rig is None:
        return False
    owner = rig.rig_bridge.source
    return owner is None or owner == src


def detach_copy(src):
    """A copied skeleton forgets the original's rig and meta-rig and drops the copied binding."""
    st = src.rig_bridge
    unbind(src)
    st.rig = None
    st.metarig = None


_STRIP_PROPS = ("frame_start", "frame_end", "action_frame_start", "action_frame_end", "blend_type", "blend_in",
                "blend_out", "use_auto_blend", "extrapolation", "influence", "use_animated_influence", "repeat",
                "scale", "mute", "use_reverse", "use_sync_length")


def _snapshot_nla(obj):
    """Tracks and strips of obj's animation, to put back after Rigify regenerates the rig (QA I5)."""
    ad = obj.animation_data
    if ad is None:
        return None
    snap = {"action": ad.action, "slot": getattr(getattr(ad, "action_slot", None), "identifier", ""),
            "blend": ad.action_blend_type, "influence": ad.action_influence,
            "extrapolation": ad.action_extrapolation, "tracks": []}
    for tr in ad.nla_tracks:
        strips = []
        for st in tr.strips:
            if st.action is None:
                continue
            d = {k: getattr(st, k) for k in _STRIP_PROPS if hasattr(st, k)}
            d.update(name=st.name, action=st.action,
                     slot=getattr(getattr(st, "action_slot", None), "identifier", ""))
            strips.append(d)
        snap["tracks"].append({"name": tr.name, "mute": tr.mute, "lock": tr.lock, "solo": tr.is_solo,
                               "strips": strips})
    return snap


def _set_slot(owner, action, ident):
    if not hasattr(owner, "action_slot") or action is None or not getattr(action, "slots", None):
        return
    slot = next((sl for sl in action.slots if sl.identifier == ident), None) or action.slots[0]
    try:
        owner.action_slot = slot
    except Exception:
        pass


def _restore_nla(obj, snap):
    if not snap:
        return
    ad = obj.animation_data_create()
    for tr in list(ad.nla_tracks):
        ad.nla_tracks.remove(tr)
    for td in snap["tracks"]:
        tr = ad.nla_tracks.new()
        tr.name = td["name"]
        for d in td["strips"]:
            st = tr.strips.new(d["name"], int(d["frame_start"]), d["action"])
            _set_slot(st, d["action"], d["slot"])
            for k in _STRIP_PROPS:
                if k in d and k not in ("frame_start",):
                    try:
                        setattr(st, k, d[k])
                    except Exception:
                        pass
        tr.mute, tr.lock = td["mute"], td["lock"]
        if td["solo"]:
            tr.is_solo = True
    if snap["action"] is not None:
        ad.action = snap["action"]
        _set_slot(ad, snap["action"], snap["slot"])
    ad.action_blend_type = snap["blend"]
    ad.action_influence = snap["influence"]
    ad.action_extrapolation = snap["extrapolation"]


def build(context, src, specs: list[PartSpec]):
    """Create/refresh the meta-rig, generate the Rigify rig, add the native
    modules on top and bind src to the result."""
    ensure_rigify()
    if src.rig_bridge.rig is not None and not owns_rig(src):
        detach_copy(src)
    specs = merge_feet(src, [s for s in specs])
    specs = [s for s in specs if any(s.bones)]
    for s in specs:
        s.all_bones = MODULES[s.module].expand(src, s) if s.module in MODULES else list(s.bones)
    errs = validate(src, specs)
    if errs:
        raise BuildError("; ".join(errs[:6]))
    if not specs:
        raise BuildError("No modules – add some from Module Assets or press Auto Rig")

    ctx = BuildContext(src, specs)
    settings = src.rig_bridge
    layouts, used, meta_of = [], set(), {}
    for spec in specs:
        mod = MODULES[spec.module]
        lay = mod.layout(ctx, spec) if mod.backend == "rigify" else placeholder_layout(ctx, spec)
        rename = {}
        for lb in lay:
            n, k = lb.name, 2
            while n in used:
                n = f"{lb.name}_{k}"
                k += 1
            rename[lb.name] = n
            lb.name = n
            used.add(n)
        for lb in lay:
            if lb.parent in rename:
                lb.parent = rename[lb.parent]
            if lb.src:
                meta_of[lb.src] = lb.name
        layouts.append((spec, mod, lay))

    # --- meta-rig object -------------------------------------------------
    meta = settings.metarig
    if meta is None or meta.name not in context.scene.objects:
        arm = bpy.data.armatures.new(f"{src.name}_metarig")
        meta = bpy.data.objects.new(arm.name, arm)
        coll = src.users_collection[0] if src.users_collection else context.scene.collection
        coll.objects.link(meta)
        settings.metarig = meta
    meta.matrix_world = Matrix.Identity(4)
    meta.show_in_front = True
    meta.data.display_type = "OCTAHEDRAL"

    colldefs = [CollDef("Root", 19, "", 1)]
    for spec, mod, lay in layouts:
        if mod.backend == "rigify":
            colldefs += mod.collections(spec)

    set_mode(context, meta, "OBJECT")
    colls_by_name = _setup_metarig_look(meta.data, colldefs)

    set_mode(context, meta, "EDIT")
    eb = meta.data.edit_bones
    for b in list(eb):
        eb.remove(b)
    for spec, mod, lay in layouts:
        for lb in lay:
            e = eb.new(lb.name)
            e.head, e.tail = lb.head, lb.tail
    # --- auto-create parent socket anchors for partial rigs --------------
    # If a module is placed on a bone (e.g. hair_01, tail_01, upperarm_l) whose
    # parent bone on src (head, pelvis, clavicle_l) is NOT driven by any module,
    # synthesize a lightweight parent socket bone on the rig so the partial rig
    # has a proper parent control at the socket joint and Bake to Control Rig
    # transfers the parent bone's animation automatically.
    auto_sockets = []
    seen_auto = set()
    src_bones = src.data.bones
    for spec, mod, lay in layouts:
        if not lay:
            continue
        first = lay[0].src or (spec.bones[0] if spec.bones else "")
        if not first or first not in src_bones:
            continue
        par_src = spec.parent if (spec.parent and spec.parent in src_bones) else (
            src_bones[first].parent.name if src_bones[first].parent else ""
        )
        if not par_src or par_src in meta_of:
            continue
        # Walk up to check if ANY ancestor is in meta_of
        anc = src_bones[par_src]
        while anc and anc.name not in meta_of:
            anc = anc.parent
        if anc is None and par_src not in seen_auto:
            seen_auto.add(par_src)
            auto_sockets.append(par_src)

    for par_src in auto_sockets:
        s_side = side_of(par_src) or ""
        s_base = re.sub(r"[^A-Za-z0-9_]", "_", strip_vendor(strip_namespace(par_src)))
        if s_side and s_base.lower().endswith((f"_{s_side.lower()}", f".{s_side.lower()}")):
            s_base = s_base[:-2]
        s_spec = PartSpec("control", s_base or par_src, s_side, [par_src], "",
                          {"translate": True, "shape": "CIRCLE", "size": 1.0})
        s_spec.all_bones = [par_src]
        s_lay = placeholder_layout(ctx, s_spec)
        for lb in s_lay:
            n, k = lb.name, 2
            while n in used:
                n = f"{lb.name}_{k}"
                k += 1
            lb.name = n
            used.add(n)
            if lb.src:
                meta_of[lb.src] = lb.name
            e = eb.new(lb.name)
            e.head, e.tail = lb.head, lb.tail
        specs.insert(0, s_spec)
        layouts.insert(0, (s_spec, MODULES["control"], s_lay))
    for spec, mod, lay in layouts:
        for lb in lay:
            e = eb[lb.name]
            parent = lb.parent
            if parent == AUTO_PARENT:
                first = lb.src or spec.bones[0]
                if spec.parent and spec.parent in meta_of and first == spec.all_bones[0]:
                    parent = meta_of[spec.parent]
                else:
                    p = src_bones[first].parent
                    while p and (p.name not in meta_of or meta_of[p.name] == lb.name):
                        p = p.parent
                    parent = meta_of[p.name] if p else None
            if parent:
                e.parent = eb[parent]
                touching = (eb[parent].tail - e.head).length < ctx.eps * 0.1
                if lb.connect or (lb.parent == AUTO_PARENT and touching
                                  and getattr(mod, "connect_to_parent", False)):
                    e.use_connect = True
            d = (e.tail - e.head).normalized()
            ref = lb.roll_z
            if ref is None or abs(d.dot(ref.normalized())) > 0.98:
                ref = ctx.forward if abs(d.dot(ctx.up)) > 0.7 else ctx.up
            e.align_roll(ref)

    set_mode(context, meta, "POSE")
    pbones = meta.pose.bones
    for spec, mod, lay in layouts:      # pass 1: rig types (modules may query them)
        if lay and mod.rigify_type and mod.backend == "rigify":
            pbones[lay[0].name].rigify_type = mod.rigify_type
    for spec, mod, lay in layouts:
        if lay and mod.backend == "rigify":
            mod.configure(ctx, spec, lay, pbones, colls_by_name)
    set_mode(context, meta, "OBJECT")

    # --- generate -------------------------------------------------------
    old_rig = settings.rig if settings.rig and settings.rig.name in context.scene.objects else None
    saved_anim = None
    if old_rig is not None:
        unbind(src)
        native.remove_sims(src.name)
        native._remove_dyn_rig(src)
        if old_rig.animation_data:
            saved_anim = _snapshot_nla(old_rig)
            for fc in list(old_rig.animation_data.drivers):     # stale native drivers
                old_rig.animation_data.drivers.remove(fc)
        old_rig.hide_set(False)
    meta.data.rigify_target_rig = old_rig
    if hasattr(meta.data, "rigify_rig_basename") and not old_rig:
        meta.data.rigify_rig_basename = f"{src.name}_rig"
    set_mode(context, meta, "OBJECT")
    try:
        # call the generator directly: the operator refuses metarigs that only
        # contain untyped placeholder bones (native-only rigs)
        import importlib
        importlib.import_module("rigify.generate").generate_rig(context, meta)
    except Exception as exc:
        raise BuildError(f"Rigify generation failed: {exc}") from exc
    rig = meta.data.rigify_target_rig
    if rig is None:
        raise BuildError("Rigify did not produce a rig")
    settings.rig = rig
    rig.rig_bridge.source = src
    meta.hide_set(True)

    _place_root(context, src, rig)

    # --- native modules on top of the Rigify rig --------------------------
    nctx = native.run_native(context, src, rig, specs, meta_of, settings.control_size, set_mode)
    for spec, mod, lay in layouts:
        if mod.backend == "rigify":
            mod.post_rigify(rig, spec, lay)
    _apply_layer_visibility(rig)
    if settings.side_colors:
        _side_colors(rig)
    if saved_anim is not None:
        _restore_nla(rig, saved_anim)

    # --- store binding information on the rig ---------------------------
    names = set(rig.data.bones.keys())
    pairs, fk = [], []
    for spec, mod, lay in layouts:
        if mod.backend == "rigify":
            pairs += [list(p) for p in mod.bind_targets(spec, lay, names)]
            fk += mod.fk_controls(spec, lay, names)
    for b, (bone, objname) in nctx.bind.items():
        pairs.append([b, bone, objname] if objname else [b, bone])
    fk += nctx.fk
    rig[PAIRS_KEY] = json.dumps(pairs)
    rig[FK_KEY] = json.dumps(fk)
    rig[INFO_KEY] = json.dumps(nctx.info)
    rig["rb_dyn"] = f"{src.name}_dyn" if nctx.dyn else ""

    bind(context, src, rig)
    set_mode(context, rig, "OBJECT")
    return rig, len(pairs)


def _place_root(context, src, rig):
    """Rigify puts 'root' at the meta-rig origin (world origin). Move it under
    the character so every rig in a scene gets its own, sensibly sized root."""
    if "root" not in rig.data.bones:
        return
    mw = src.matrix_world
    pts = [mw @ b.head_local for b in src.data.bones] + [mw @ b.tail_local for b in src.data.bones]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    size = max(hi.x - lo.x, hi.y - lo.y, (hi.z - lo.z) * 0.5, 0.1)
    set_mode(context, rig, "EDIT")
    e = rig.data.edit_bones["root"]
    inv = rig.matrix_world.inverted()
    h = inv @ centre
    e.head = h
    e.tail = h + (inv.to_3x3() @ Vector((0, 1, 0))).normalized() * size * 0.6
    e.roll = 0.0
    set_mode(context, rig, "OBJECT")


def _side_colors(rig):
    """Unreal convention: left controls blue, right red (native parts are
    coloured when created)."""
    for b in rig.data.bones:
        n = b.name
        if n.startswith(("ORG-", "MCH-", "DEF-", "VIS", "OUT-", "BIND-", "RET")):
            continue
        if b.color.palette == "CUSTOM":
            continue
        sd = side_of(n.replace(".001", ""))
        if sd and any(c.is_visible for c in b.collections):
            native.set_bone_color(rig, n, sd)


# ---------------------------------------------------------------------------
# Bind / unbind
# ---------------------------------------------------------------------------

def _helper_coll(arm):
    bc = arm.collections_all.get(BIND_COLL)
    if bc is None:
        bc = arm.collections.new(BIND_COLL)
    bc.is_visible = False
    return bc


def _add_world_bone(eb, name, world: Matrix, length, rig_inv: Matrix, parent=None):
    loc, rot = clean_rest(world)
    e = eb.get(name) or eb.new(name)
    head = rig_inv @ loc
    y = (rig_inv.to_3x3() @ rot.col[1]).normalized()
    z = (rig_inv.to_3x3() @ rot.col[2]).normalized()
    e.head = head
    e.tail = head + y * max(length, 1e-4)
    e.align_roll(z)
    e.use_deform = False
    e.parent = parent
    return e


def unbind(src, remove=True):
    for pb in src.pose.bones:
        for c in [c for c in pb.constraints if c.name.startswith(CON_PREFIX)]:
            if remove:
                pb.constraints.remove(c)


def set_bind_muted(src, muted):
    n = 0
    for pb in src.pose.bones:
        for c in pb.constraints:
            if c.name.startswith(CON_PREFIX):
                c.mute = muted
                n += 1
    return n


# Twist / share-bone distribution (UE twist bones, CC 'ShareBone' correctives)
TW_UPPER = ("upper_arm", "thigh")    # twist from the parent side: 0 at the joint -> 1 at the far end
TW_LOWER = ("forearm", "shin")       # twist from the hand/foot:  0 at the elbow  -> 1 at the wrist


def _distribution_plan(src, pmap):
    """[(source bone, kind, A, B, t)] for unmapped twist/share bones.
    twist_upper: blend swing-only(A) -> A          by t
    twist_lower: blend A -> A + twist of child B   by t
    share:       blend (B in A's parent) -> B      by 0.5"""
    mw = src.matrix_world
    out = []
    for b in src.data.bones:
        low = strip_vendor(strip_namespace(b.name)).lower()
        if b.name in pmap or not ("twist" in low or "share" in low):
            continue
        a = b.parent
        while a is not None and a.name not in pmap:
            a = a.parent
        if a is None:
            continue
        base = pmap[a.name].split("-", 1)[-1].partition(".")[0]
        if "twist" in low:
            kind = "twist_upper" if base in TW_UPPER else "twist_lower" if base in TW_LOWER else None
            e = next((c for c in a.children if c.name in pmap), None)
            inchain = False
            if e is None:
                # Daz-style in-chain twist (ShldrBend -> ShldrTwist -> ForearmBend):
                # the next limb joint hangs below the twist bone
                e = next((c for c in sorted(b.children_recursive, key=lambda x: len(x.parent_recursive))
                          if c.name in pmap), None)
                inchain = e is not None
            if kind is None or e is None:
                continue
            ha, he = mw @ a.head_local, mw @ e.head_local
            ax = he - ha
            if ax.length < 1e-6:
                continue
            t = 1.0 if inchain else max(0.0, min(1.0, (mw @ b.head_local - ha).dot(ax) / ax.length_squared))
            out.append((b.name, kind, a.name, e.name, t, inchain))
        else:
            # the joint the share bone sits on: A itself (elbow/knee) or a mapped child (toe)
            hb = mw @ b.head_local
            cands = [a] + [c for c in a.children if c.name in pmap]
            j = min(cands, key=lambda c: ((mw @ c.head_local) - hb).length)
            par = j.parent
            while par is not None and par.name not in pmap:
                par = par.parent
            if par is None:
                continue
            out.append((b.name, "share", par.name, j.name, 0.5, False))
    return out


def _distribution_helpers(src, eb, pmap, ctx_rest, inv, hc, made, obj):
    plan = _distribution_plan(src, pmap)
    blends = []

    def geo(name, like, parent):
        n = eb.get(name) or eb.new(name)
        n.head, n.tail, n.roll = like.head.copy(), like.tail.copy(), like.roll
        n.parent, n.use_connect, n.use_deform = parent, False, False
        n.inherit_scale = "NONE"
        hc.assign(n)
        return n

    for s, kind, a, e, t, inchain in plan:
        ta, te = pmap.get(a), pmap.get(e)
        if ta not in eb or te not in eb:
            continue
        A, E = eb[ta], eb[te]
        if kind == "twist_upper":
            x = geo(f"{BIND_PREFIX}TWS-{a}", A, A.parent)          # swing only, no roll
            x_cons = [("COPY_LOCATION", ta, {}), ("DAMPED_TRACK", ta, {"head_tail": 1.0})]
            y, y_cons, like = ta, [], A
            bend = eb.get(BIND_PREFIX + a)
            if inchain and bend is not None:
                bend.parent = x        # the Bend bone swings, its Twist bone rolls
        elif kind == "twist_lower":
            x, x_cons, like = A, [], A
            h = geo(f"{BIND_PREFIX}TWH-{a}", A, E)                  # A-shaped, follows the hand
            y = geo(f"{BIND_PREFIX}TWE-{a}", A, A)                  # hand roll about A's axis
            y_cons = [("COPY_ROTATION", h.name, {}), ("DAMPED_TRACK", ta, {"head_tail": 1.0})]
            y = y.name
        else:  # share: half way between the parent limb and the joint bone
            x = geo(f"{BIND_PREFIX}SHB-{e}", E, A)                  # E-shaped, follows parent only
            x_cons, y, y_cons, like = [], te, [], E
        xn = x if isinstance(x, str) else x.name
        w = geo(f"{BIND_PREFIX}TW-{s}", like, like)
        b = src.data.bones[s]
        length = ((src.matrix_world @ b.tail_local) - (src.matrix_world @ b.head_local)).length
        bind = _add_world_bone(eb, BIND_PREFIX + s, ctx_rest[s], length, inv, w)
        bind.inherit_scale = "NONE"
        hc.assign(bind)
        made.append((s, bind.name, obj))
        blends.append((xn, x_cons, y, y_cons, w.name, t))
    return blends


def _distribution_constraints(obj, blends):
    pbs = obj.pose.bones
    done = set()

    def add(owner, cons):
        if owner in done:
            return
        done.add(owner)
        for typ, sub, kw in cons:
            c = pbs[owner].constraints.new(typ)
            c.name = CON_PREFIX + typ.title()
            c.target, c.subtarget = obj, sub
            for k, v in kw.items():
                setattr(c, k, v)

    for xn, x_cons, y, y_cons, w, t in blends:
        add(xn, x_cons)
        add(y, y_cons) if y_cons else None
        add(w, [("COPY_TRANSFORMS", xn, {}), ("COPY_TRANSFORMS", y, {"influence": t})])


def bind(context, src, rig):
    pairs = json.loads(rig.get(PAIRS_KEY, "[]"))
    if not pairs:
        raise BuildError("Rig has no Rig Bridge binding data – rebuild it")
    ctx_rest = {b.name: src.matrix_world @ b.matrix_local for b in src.data.bones}
    mw = src.matrix_world
    groups = {}
    for p in pairs:
        on = p[2] if len(p) > 2 and p[2] else rig.name
        groups.setdefault(on, []).append((p[0], p[1]))

    made = []
    for on, lst in groups.items():
        obj = bpy.data.objects.get(on)
        if obj is None:
            continue
        hidden = obj.hide_get()
        set_mode(context, obj, "EDIT")
        eb = obj.data.edit_bones
        for e in [e for e in eb if e.name.startswith(BIND_PREFIX)]:
            eb.remove(e)
        hc = _helper_coll(obj.data)
        inv = obj.matrix_world.inverted()
        for s, t in lst:
            if s not in src.data.bones or t not in eb:
                continue
            b = src.data.bones[s]
            length = ((mw @ b.tail_local) - (mw @ b.head_local)).length
            e = _add_world_bone(eb, BIND_PREFIX + s, ctx_rest[s], length, inv, eb[t])
            e.inherit_scale = "NONE"   # Rigify stretch must not shear the bind frame
            hc.assign(e)
            made.append((s, e.name, obj))
        blends = []
        if on == rig.name:
            pmap = {p[0]: p[1] for p in pairs if not (len(p) > 2 and p[2])}
            blends = _distribution_helpers(src, eb, pmap, ctx_rest, inv, hc, made, obj)
        set_mode(context, obj, "OBJECT")
        _distribution_constraints(obj, blends)
        if hidden:
            obj.hide_set(True)

    unbind(src)
    for s, bname, obj in made:
        pb = src.pose.bones[s]
        c = pb.constraints.new("COPY_ROTATION")
        c.name = CON_PREFIX + "Rot"
        c.target, c.subtarget = obj, bname
        c.target_space = c.owner_space = "WORLD"
        c = pb.constraints.new("COPY_LOCATION")
        c.name = CON_PREFIX + "Loc"
        c.target, c.subtarget = obj, bname
        c.target_space = c.owner_space = "WORLD"
    _match_ik_bones(src, {s for s, _, _ in made})
    context.view_layer.update()
    return len(made)


def _match_ik_bones(src, bound):
    """UE 'ik_foot_l' / 'ik_hand_r' / 'ik_hand_gun' must sit on the real foot/hand
    in exported animation (in-engine IK and weapon attach read them)."""
    names = {norm(b.name): b.name for b in src.data.bones}
    for b in src.data.bones:
        n = norm(b.name)
        if not n.startswith("ik_") or b.name in bound or n.endswith("_root"):
            continue
        key = "hand_r" if n == "ik_hand_gun" else n[3:]
        tgt = names.get(key)
        if tgt is None or tgt not in bound:
            continue
        c = src.pose.bones[b.name].constraints.new("COPY_TRANSFORMS")
        c.name = CON_PREFIX + "IK"
        c.target, c.subtarget = src, tgt
        c.target_space = c.owner_space = "WORLD"


# ---------------------------------------------------------------------------
# Bake rig motion onto the source skeleton (for FBX / game-engine export)
# ---------------------------------------------------------------------------

def bake_to_source(context, src, rig, start, end, mute_after=True, notes=None):
    owner = rig.rig_bridge.source
    if owner is not None and owner != src:
        raise BuildError(f"'{src.name}' is a copy and still follows the control rig of '{owner.name}'. "
                         "Compile it to give it its own rig")
    names = {pb.name for pb in src.pose.bones if any(c.name.startswith(CON_PREFIX) for c in pb.constraints)}
    prev = src.animation_data.action if src.animation_data else None
    prev_name = prev.name if prev is not None else ""
    set_mode(context, src, "POSE")
    for pb in src.pose.bones:
        pose_select(pb, pb.name in names)
    bpy.ops.nla.bake(frame_start=start, frame_end=end, step=1, only_selected=True,
                     visual_keying=True, clear_constraints=False, clear_parents=False,
                     use_current_action=False, bake_types={"POSE"})
    if mute_after:
        set_bind_muted(src, True)
    set_mode(context, src, "OBJECT")
    act = src.animation_data.action if src.animation_data else None
    if act is not None:
        take = rig.animation_data.action.name if rig.animation_data and rig.animation_data.action else rig.name
        take = take.split("|", 1)[1] if take.startswith(rig.name + "|") else take
        claim_take_name(act, f"{src.name}|{take}", notes)
        act[BAKE_TAG] = keys_checksum(act)
        rig["rb_skel_action"] = act.name
        old = bpy.data.actions.get(prev_name) if prev_name else None
        if old is not None and old != act:
            old.use_fake_user = True             # the skeleton's previous take is replaced, not lost
    return act


# ---------------------------------------------------------------------------
# Transfer an animation (e.g. a Mixamo/UE clip) onto the rig's FK controls
# ---------------------------------------------------------------------------

def _key(n):
    return re.sub(r"^mixamorig\d*_?", "", norm(n))


BAKE_MODES = (("IK_LEGS", "FK Arms + IK Legs", "Arms in FK, legs in IK (feet stay planted when you edit the body)"),
              ("IK", "Full IK", "Arms and legs in IK"),
              ("FK", "Full FK", "Everything in FK"))

# (kind, upper, lower, end, IK control, pole control, switch-property bone)
_IK_LIMBS = (("arm", "upper_arm", "forearm", "hand", "hand_ik", "upper_arm_ik_target", "upper_arm_parent"),
             ("leg", "thigh", "shin", "foot", "foot_ik", "thigh_ik_target", "thigh_parent"))


def _ik_limbs(rig):
    out = []
    bones = rig.data.bones
    for kind, a, b, c, ik, pole, par in _IK_LIMBS:
        for S in ("L", "R"):
            n = {k: f"{v}.{S}" for k, v in (("a", a), ("b", b), ("c", c), ("ik", ik), ("pole", pole), ("par", par))}
            if all(x in bones for x in (n["ik"], n["pole"], n["par"])) and all(
                    f"ORG-{n[k]}" in bones for k in ("a", "b", "c")):
                out.append((kind, n))
    return out


def _pose_values(pb, m, prev_q):
    """Pose bone to armature-space matrix `m` (not keyed). Returns its local channels
    {data_path: tuple}; quaternion sign kept continuous with the previous call."""
    pb.matrix = m
    out = {"location": tuple(pb.location)}
    if pb.rotation_mode == "QUATERNION":
        q = pb.rotation_quaternion.copy()
        p = prev_q.get(pb.name)
        if p is not None and p.dot(q) < 0:
            q.negate()
            pb.rotation_quaternion = q
        prev_q[pb.name] = q
        out["rotation_quaternion"] = tuple(q)
    elif pb.rotation_mode == "AXIS_ANGLE":
        out["rotation_axis_angle"] = tuple(pb.rotation_axis_angle)
    else:
        p = prev_q.get(pb.name)
        if p is not None:
            pb.rotation_euler = pb.rotation_euler.to_matrix().to_euler(pb.rotation_mode, p)
        prev_q[pb.name] = pb.rotation_euler.copy()
        out["rotation_euler"] = tuple(pb.rotation_euler)
    return out


def _fcurve(obj, path, index, group):
    act = obj.animation_data.action
    if hasattr(act, "fcurve_ensure_for_datablock"):          # Blender 4.4+: slotted actions
        return act.fcurve_ensure_for_datablock(obj, path, index=index, group_name=group)
    fc = act.fcurves.find(path, index=index)
    return fc if fc is not None else act.fcurves.new(path, index=index, action_group=group)


def _write_keys(obj, bone, channels):
    """Write keys in bulk: channels = {data_path_tail: {frame: tuple}} for pose bone `bone`.
    Existing keys on those frames are replaced, keys on other frames are kept.

    Inserting keys one at a time while the scene is re-evaluated in between makes Blender copy the
    whole action for every evaluation, which grows with the length of the take (QA R1)."""
    import numpy as np
    prefs = bpy.context.preferences.edit
    ipo = prefs.keyframe_new_interpolation_type
    handle = prefs.keyframe_new_handle_type
    for tail, per_frame in channels.items():
        if not per_frame:
            continue
        frames = sorted(per_frame)
        path = f'pose.bones["{bpy.utils.escape_identifier(bone)}"].{tail}'
        width = len(per_frame[frames[0]])
        for i in range(width):
            fc = _fcurve(obj, path, i, bone)
            new = {f: per_frame[f][i] for f in frames}
            n = len(fc.keyframe_points)
            if n:
                co = np.empty(2 * n)
                fc.keyframe_points.foreach_get("co", co)
                for f, v in co.reshape(-1, 2):
                    if int(round(f)) not in new and abs(f - round(f)) < 1e-4:
                        new[int(round(f))] = v
                    elif abs(f - round(f)) >= 1e-4:
                        new[float(f)] = v
            keys = sorted(new)
            fc.keyframe_points.clear()
            fc.keyframe_points.add(len(keys))
            co = np.empty(2 * len(keys))
            co[0::2] = keys
            co[1::2] = [new[k] for k in keys]
            fc.keyframe_points.foreach_set("co", co)
            fc.keyframe_points.foreach_set("interpolation", [bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items[ipo].value] * len(keys))
            for attr in ("handle_left_type", "handle_right_type"):
                fc.keyframe_points.foreach_set(attr, [bpy.types.Keyframe.bl_rna.properties[attr].enum_items[handle].value] * len(keys))
            fc.update()


def _quat_continuous(per_frame):
    """Keep the sign of a quaternion channel continuous over the frames (in place)."""
    prev = None
    for f in sorted(per_frame):
        q = per_frame[f]
        if prev is not None and sum(x * y for x, y in zip(prev, q)) < 0:
            q = tuple(-x for x in q)
            per_frame[f] = q
        prev = q


def bake_ik_controls(context, rig, start, end):
    """Key every IK control (and its pole) to where the FK pose puts the limb.
    Run while the limbs are in FK; afterwards the IK/FK switch can be flipped on
    any frame without the pose changing – like Unreal's Bake to Control Rig,
    which solves both control sets.

    Nothing is keyed inside the frame loops: values are collected and written once per channel
    (QA R1: per-frame keying made the bake quadratic in the take length).
    The pole follows the FK upper bone's own bend axis, not the joint positions, so a straight or
    slightly over-straight limb keeps its roll (QA R5: such limbs flipped 180 degrees in IK)."""
    limbs = _ik_limbs(rig)
    if not limbs:
        return 0
    R, P = rig.data.bones, rig.pose.bones
    info = []
    for kind, n in limbs:
        off = R[f"ORG-{n['c']}"].matrix_local.inverted() @ R[n["ik"]].matrix_local
        pole_rot = R[n["pole"]].matrix_local.to_3x3().to_4x4()
        A0, K0, C0 = (R[f"ORG-{n[k]}"].head_local for k in ("a", "b", "c"))
        # the bend axis is where the rig's pole sits (it defines the IK chain's bend plane); the joint
        # positions only as a fallback (a straight rest limb has no measurable bend of its own)
        ax0 = (C0 - A0).normalized()
        to_pole = R[n["pole"]].head_local - K0
        rest_bend = to_pole - ax0 * to_pole.dot(ax0)
        rest_bend = (rest_bend.normalized() if rest_bend.length > 1e-6 else
                     _bend_dir(A0, K0, C0) or to_pole.normalized())
        # pole position in a frame built from the limb (origin = middle joint,
        # x = bend direction, y = root->end), so any rest offset is reproduced
        pole_local = _limb_frame(A0, K0, C0, rest_bend).transposed() @ (R[n["pole"]].head_local - K0)
        # the bend direction in the upper bone's own frame: carried along by its rotation it gives the
        # limb's bend axis on every frame, also when the limb is straight
        bend_local = (R[f"ORG-{n['a']}"].matrix_local.to_3x3().normalized().inverted() @ rest_bend).normalized()
        par = P[n["par"]]
        if "pole_vector" in par.keys():
            par["pole_vector"] = type(par["pole_vector"])(1)
            par.keyframe_insert('["pole_vector"]', frame=start)
        for extra in (f"foot_heel_ik.{n['ik'][-1]}", f"foot_spin_ik.{n['ik'][-1]}"):
            if kind == "leg" and extra in P:
                P[extra].matrix_basis = Matrix()
        info.append((kind, n, off, pole_local, pole_rot, bend_local))
    prev_q, last_bend = {}, {}
    scene = context.scene
    samples = {}
    keys = {}                                   # bone -> data path -> {frame: values}

    def put(bone, values, f):
        for path, v in values.items():
            keys.setdefault(bone, {}).setdefault(path, {})[f] = v

    frames = range(start, end + 1)
    for f in frames:
        scene.frame_set(f)
        poles = []
        for kind, n, off, pole_local, pole_rot, bend_local in info:
            org = lambda k: P[f"ORG-{n[k]}"].matrix  # noqa: E731
            a, b, c = org("a").translation, org("b").translation, org("c").translation
            y = c - a
            bend = org("a").to_3x3().normalized() @ bend_local
            bend = bend - y.normalized() * bend.dot(y.normalized()) if y.length > 1e-8 else bend
            if bend.length < 1e-6:
                bend = last_bend.get(n["ik"]) or _bend_dir(a, b, c) or Vector((1, 0, 0))
            bend = bend.normalized()
            last_bend[n["ik"]] = bend
            samples[(n["ik"], f)] = (a.copy(), b.copy(), c.copy(), bend.copy())
            put(n["ik"], _pose_values(P[n["ik"]], org("c") @ off, prev_q), f)
            poles.append((n, Matrix.Translation(b + _limb_frame(a, b, c, bend) @ pole_local) @ pole_rot))
        context.view_layer.update()              # a pole may hang from its IK control
        for n, m_pole in poles:
            put(n["pole"], _pose_values(P[n["pole"]], m_pole, prev_q), f)
    for bone, chans in keys.items():
        for path, per_frame in chans.items():
            if path == "rotation_quaternion":
                _quat_continuous(per_frame)
        _write_keys(rig, bone, chans)

    # Make it exact: on every frame switch the limbs to IK, measure how far the elbow/knee ended up
    # rotated about the root->end axis and turn the pole back by that much (converges in 1-2 steps).
    # A limb that is straight, or bent the other way than its joint bends (over-straight in the take),
    # keeps the pole on its own bend axis: IK cannot bend backwards, and following such a knee would
    # roll the whole limb by 180 degrees. The joint then comes out bent forwards by the same angle (a
    # mirror image; the hand or foot stays exact): up to 7 cm off on a fight take's 9 degree knee.
    exact = [(kind, n, pole_local, pole_rot) for kind, n, _o, pole_local, pole_rot, _b in info
             if "IK_FK" in P[n["par"]].keys()]
    fixed = {}
    for f in frames:
        scene.frame_set(f)
        todo = []
        for kind, n, pole_local, pole_rot in exact:
            a, b, c, bend = samples[(n["ik"], f)]
            ax = (c - a).normalized()
            u = b - a - ax * (b - a).dot(ax)
            if u.length < 1e-3 * (c - a).length or u.dot(bend) <= 0.0:
                continue
            todo.append([n, pole_rot, a, b, ax, u, _limb_frame(a, b, c, bend) @ pole_local])
        if not todo:
            continue
        for item in todo:
            P[item[0]["par"]]["IK_FK"] = 0.0
        for _ in range(4):
            rig.update_tag()                     # custom props don't tag the depsgraph by themselves
            context.view_layer.update()
            left = []
            for item in todo:
                n, pole_rot, a, b, ax, u, off_w = item
                b_ik = P[f"ORG-{n['b']}"].matrix.translation
                v = b_ik - a - ax * (b_ik - a).dot(ax)
                if v.length < 1e-6:
                    continue
                ang = math.atan2(u.cross(v).dot(ax), u.dot(v))
                if abs(ang) < 1e-5:
                    continue
                item[6] = Matrix.Rotation(-ang, 3, ax) @ off_w
                fixed.setdefault(n["pole"], {})[f] = _pose_values(
                    P[n["pole"]], Matrix.Translation(b + item[6]) @ pole_rot, prev_q)
                left.append(item)
            if not left:
                break
            todo = left
        for kind, n, pole_local, pole_rot in exact:
            P[n["par"]]["IK_FK"] = 1.0
    for bone, per_frame in fixed.items():
        chans = {}
        for f, values in per_frame.items():
            for path, v in values.items():
                chans.setdefault(path, {})[f] = v
        if "rotation_quaternion" in chans:
            merged = dict(keys[bone]["rotation_quaternion"])
            merged.update(chans["rotation_quaternion"])
            _quat_continuous(merged)
            chans["rotation_quaternion"] = {f: merged[f] for f in chans["rotation_quaternion"]}
        _write_keys(rig, bone, chans)
    rig.update_tag()
    return len(info)


def _twist_about(q, axis):
    """Signed rotation angle of quaternion q about a unit axis (swing removed)."""
    v = Vector((q.x, q.y, q.z))
    ang = 2 * math.atan2(v.dot(axis), q.w)
    return (ang + math.pi) % (2 * math.pi) - math.pi


def _limb_frame(a, b, c, bend):
    """3x3 basis: x = bend direction, y = a->c, z = x × y."""
    y = (c - a).normalized()
    x = (bend - y * bend.dot(y)).normalized()
    z = x.cross(y)
    m = Matrix((x, y, z)).transposed()
    return m


def _bend_dir(a, b, c):
    """Unit vector from the a–c line to the middle joint b (None if straight)."""
    ac = c - a
    if ac.length < 1e-8:
        return None
    proj = a + ac * ((b - a).dot(ac) / ac.length_squared)
    d = b - proj
    return d.normalized() if d.length > ac.length * 1e-3 else None


def _apply_layer_visibility(rig):
    """Set sensible default Rigify bone collection visibility: hide tweak/detail
    layers and show either IK or FK per limb matching its IK_FK property."""
    colls = rig.data.collections_all
    for name in ("Torso (Tweak)", "Fingers (Detail)",
                 "Arm.L (Tweak)", "Arm.R (Tweak)",
                 "Leg.L (Tweak)", "Leg.R (Tweak)"):
        bc = colls.get(name)
        if bc is not None:
            bc.is_visible = False
    for kind, n in _ik_limbs(rig):
        pb = rig.pose.bones.get(n["par"])
        ik_fk = float(pb.get("IK_FK", 0.0)) if pb is not None else 0.0
        side = n["par"].rpartition(".")[2]
        prefix = f"{'Arm' if kind == 'arm' else 'Leg'}.{side}"
        bc_ik = colls.get(f"{prefix} (IK)")
        bc_fk = colls.get(f"{prefix} (FK)")
        if bc_ik is not None:
            bc_ik.is_visible = ik_fk < 0.5
        if bc_fk is not None:
            bc_fk.is_visible = ik_fk >= 0.5


def set_ik_mode(rig, mode, frame):
    for kind, n in _ik_limbs(rig):
        pb = rig.pose.bones[n["par"]]
        if "IK_FK" not in pb.keys():
            continue
        ik = mode == "IK" or (mode == "IK_LEGS" and kind == "leg")
        pb["IK_FK"] = 0.0 if ik else 1.0
        pb.keyframe_insert('["IK_FK"]', frame=frame)
    _apply_layer_visibility(rig)


def _fix_root_translation(context, rig, drv, entries, start, end):
    """Rigify's FK hips/spine pivot elsewhere than the source pelvis, so copying
    rotations shifts the body a little. Move the located control (torso) by the
    remaining pelvis offset on every frame – exact, because rotations don't
    depend on the torso's position. Collected first and written once (QA R1)."""
    d, ctrl = _root_located(entries)
    if not ctrl:
        return ""
    bind = next((b.name for b in rig.data.bones if b.name.startswith(BIND_PREFIX)
                 and _key(b.name[len(BIND_PREFIX):]) == _key(d)), "")
    if not bind:
        return ""
    pb = rig.pose.bones[ctrl]
    inv = rig.matrix_world.inverted()
    loc = {}
    for f in range(start, end + 1):
        context.scene.frame_set(f)
        err = (drv.matrix_world @ drv.pose.bones[d].head) - (rig.matrix_world @ rig.pose.bones[bind].head)
        if err.length < 1e-7:
            continue
        m = pb.matrix.copy()
        m.translation += inv.to_3x3() @ err
        pb.matrix = m
        loc[f] = tuple(pb.location)
    if loc:
        _write_keys(rig, ctrl, {"location": loc})
    return ctrl


def _root_located(entries):
    """(source bone, control) of the body's located control (torso)."""
    loc = [(d, ctrl) for d, ctrl, use_loc in entries if use_loc and ctrl != "root"]
    return next(((d, c) for d, c in loc if c == "torso"), loc[0] if loc else ("", ""))


BAKE_TAG = "rig_bridge_bake"   # on baked takes: a checksum of their keys (older versions stored True)


def _action_fcurves(action):
    if hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    yield from bag.fcurves
    else:
        yield from action.fcurves


def keys_checksum(action):
    import zlib
    import numpy as np
    crc, count = 0, 0
    for fc in _action_fcurves(action):
        n = len(fc.keyframe_points)
        co = np.empty(2 * n, dtype=np.float32)
        fc.keyframe_points.foreach_get("co", co)
        count += n
        crc = zlib.crc32(fc.data_path.encode() + bytes([fc.array_index & 255]) + co.tobytes(), crc)
    return float((crc & 0xFFFFFFFF) ^ count)


def claim_take_name(act, base, notes=None):
    """Name a fresh bake `base`, replacing an earlier bake of that name only if this add-on wrote it and
    nobody changed it since; an edited take keeps its name (and a fake user), the bake gets _2, _3 ..."""
    kept, i = None, 1
    while True:
        name = base if i == 1 else f"{base}_{i}"
        old = bpy.data.actions.get(name)
        if old is None or old == act:
            break
        if is_own_bake(old) and old.users - int(old.use_fake_user) == 0:
            bpy.data.actions.remove(old)
            break
        old.use_fake_user = True                 # an edited take: keep it, also through a save
        kept = kept or old.name
        i += 1
    act.name = name
    if kept and notes is not None:
        notes.append(f"Kept '{kept}' (changed since it was baked, or baked by an older version); "
                     f"the new bake is '{name}'")
    return name


def is_own_bake(action):
    """True if Bake to Control Rig wrote the action and nobody has changed its keys since."""
    stored = action.get(BAKE_TAG)
    return isinstance(stored, float) and abs(stored - keys_checksum(action)) < 1e-3


def transfer_to_rig(context, src, rig, anim_obj, start, end, keep_helpers=False, mode="FK", notes=None):
    owner = rig.rig_bridge.source
    if owner is not None and owner != src:
        raise BuildError(f"'{src.name}' is a copy and still follows the control rig of '{owner.name}'. "
                         "Compile it to give it its own rig")
    fk = json.loads(rig.get(FK_KEY, "[]"))
    if not fk:
        raise BuildError("Rig has no FK mapping – rebuild it")
    if not (anim_obj.animation_data and anim_obj.animation_data.action):
        raise BuildError(f"'{anim_obj.name}' has no action")

    # A constraint-free stand-in for the animation source avoids dependency
    # cycles when the source is the bound skeleton itself.
    drv = anim_obj.copy()
    if drv.animation_data and drv.animation_data.action and getattr(drv.animation_data, "action_slot", None) is None:
        slots = getattr(drv.animation_data.action, "slots", None)
        if slots:
            drv.animation_data.action_slot = getattr(anim_obj.animation_data, "action_slot", None) or slots[0]
    context.scene.collection.objects.link(drv)
    for pb in drv.pose.bones:
        for c in list(pb.constraints):
            drv.pose.bones[pb.name].constraints.remove(c)
    idx = {_key(b.name): b.name for b in drv.data.bones}
    mw = drv.matrix_world

    entries = []
    for s, ctrl, use_loc in fk:
        d = idx.get(_key(s))
        if d and ctrl in rig.data.bones:
            entries.append((d, ctrl, use_loc))
    if not entries:
        bpy.data.objects.remove(drv)
        raise BuildError("No matching bones between animation source and rig")

    set_mode(context, rig, "EDIT")
    eb = rig.data.edit_bones
    rig_inv = rig.matrix_world.inverted()
    hc = _helper_coll(rig.data)
    for d, ctrl, _ in entries:
        b = drv.data.bones[d]
        r = _add_world_bone(eb, RET_PREFIX + ctrl, mw @ b.matrix_local,
                            max(b.length * mw.to_scale().x, 0.01), rig_inv, None)
        cb = eb[ctrl]
        c = eb.get(RETC_PREFIX + ctrl) or eb.new(RETC_PREFIX + ctrl)
        c.head, c.tail, c.roll = cb.head.copy(), cb.tail.copy(), cb.roll
        c.use_deform = False
        c.parent = r
        hc.assign(r)
        hc.assign(c)
    set_mode(context, rig, "POSE")

    pbs = rig.pose.bones
    for d, ctrl, use_loc in entries:
        r = pbs[RET_PREFIX + ctrl]
        for typ in ("COPY_ROTATION", "COPY_LOCATION"):
            c = r.constraints.new(typ)
            c.target, c.subtarget = drv, d
            c.target_space = c.owner_space = "WORLD"
        p = pbs[ctrl]
        c = p.constraints.new("COPY_ROTATION")
        c.name = CON_PREFIX + "RET_Rot"
        c.target, c.subtarget = rig, RETC_PREFIX + ctrl
        c.target_space = c.owner_space = "WORLD"
        if use_loc:
            c = p.constraints.new("COPY_LOCATION")
            c.name = CON_PREFIX + "RET_Loc"
            c.target, c.subtarget = rig, RETC_PREFIX + ctrl
            c.target_space = c.owner_space = "WORLD"

    # Rigify's neck control spans the whole neck: aim it at the source head so
    # the head lands exactly where it is, whatever the in-between neck bones do
    head_src = next((d for d, ctrl, _ in entries if ctrl == "head"), None)
    if head_src and "neck" in {ctrl for _, ctrl, _ in entries} and RETC_PREFIX + "neck" in pbs:
        c = pbs[RETC_PREFIX + "neck"].constraints.new("DAMPED_TRACK")
        c.target, c.subtarget, c.head_tail = drv, head_src, 0.0
    ctrls = {ctrl for _, ctrl, _ in entries}
    # clean slate: previous take off, every limb in FK while baking (finger
    # controls hang from ORG-hand, which follows IK otherwise), other controls at rest
    if rig.animation_data is None:
        rig.animation_data_create()
    prev = rig.animation_data.action
    prev_name = prev.name if prev is not None else ""
    rig.animation_data.action = None
    for bc in rig.data.collections_all:
        if not bc.name.startswith(("ORG", "MCH", "DEF", "RB ", "RigBridge")):
            bc.is_visible = True
    for pb in pbs:
        if pb.name not in ctrls and not pb.name.startswith((RET_PREFIX, RETC_PREFIX)):
            pb.matrix_basis = Matrix()
        if "IK_FK" in pb.keys():
            pb["IK_FK"] = 1.0
    context.view_layer.update()
    for pb in pbs:
        pose_select(pb, pb.name in ctrls)
    bpy.ops.nla.bake(frame_start=start, frame_end=end, step=1, only_selected=True,
                     visual_keying=True, clear_constraints=False, clear_parents=False,
                     use_current_action=False, bake_types={"POSE"})
    act = rig.animation_data.action
    # one readable name per rig and take: "<rig>|<action>" (like Blender's FBX
    # "Armature|clip"). A previous bake of the same take is replaced only if this
    # add-on wrote it and nobody edited it since (QA I2: a second bake deleted a take
    # that MoCap Cleanup had cleaned); otherwise it stays and the new bake gets _2, _3 ...
    claim_take_name(act, f"{rig.name}|{anim_obj.animation_data.action.name}", notes)
    # the take the rig played before is replaced, not lost (like Bulletproof Retarget)
    prev = bpy.data.actions.get(prev_name) if prev_name else None
    if prev is not None and prev != act:
        prev.use_fake_user = True

    # cleanup helpers
    if keep_helpers:
        return act, len(entries)
    # located controls below the body (toes, jaw, props…) are baked again
    # after the torso correction – otherwise they'd carry its offset
    torso = _root_located(entries)[1]
    late = {ctrl for _, ctrl, use_loc in entries if use_loc and ctrl not in (torso, "root")}
    for ctrl in ctrls - late:
        p = pbs[ctrl]
        for c in [c for c in p.constraints if c.name.startswith(CON_PREFIX + "RET")]:
            p.constraints.remove(c)
    # limbs to FK so the baked FK controls drive the deformation
    for pb in pbs:
        if "IK_FK" in pb.keys():
            pb["IK_FK"] = 1.0
            pb.keyframe_insert(data_path='["IK_FK"]', frame=start)
    if _fix_root_translation(context, rig, drv, entries, start, end) and late:
        for pb in pbs:
            pose_select(pb, pb.name in late)
        bpy.ops.nla.bake(frame_start=start, frame_end=end, step=1, only_selected=True,
                         visual_keying=True, clear_constraints=False, clear_parents=False,
                         use_current_action=True, bake_types={"POSE"})
    for ctrl in late:
        p = pbs[ctrl]
        for c in [c for c in p.constraints if c.name.startswith(CON_PREFIX + "RET")]:
            p.constraints.remove(c)
    set_mode(context, rig, "EDIT")
    eb = rig.data.edit_bones
    for e in [e for e in eb if e.name.startswith((RET_PREFIX, RETC_PREFIX))]:
        eb.remove(e)
    set_mode(context, rig, "POSE")
    bpy.data.objects.remove(drv)
    # IK controls follow the baked FK pose on every frame, then the switch
    for pb in pbs:
        if "IK_FK" in pb.keys():
            pb["IK_FK"] = 1.0
    bake_ik_controls(context, rig, start, end)
    set_ik_mode(rig, mode, start)
    rig["rb_baked_from"] = anim_obj.animation_data.action.name if anim_obj.animation_data and \
        anim_obj.animation_data.action else ""
    act[BAKE_TAG] = keys_checksum(act)          # "own, unedited" until someone changes a key
    set_mode(context, rig, "OBJECT")
    return act, len(entries)
