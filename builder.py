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


def build(context, src, specs: list[PartSpec]):
    """Create/refresh the meta-rig, generate the Rigify rig, add the native
    modules on top and bind src to the result."""
    ensure_rigify()
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
    src_bones = src.data.bones
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
    saved_action = None
    if old_rig is not None:
        unbind(src)
        if old_rig.animation_data:
            saved_action = old_rig.animation_data.action
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
    if saved_action is not None:
        rig.animation_data_create().action = saved_action

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

def bake_to_source(context, src, rig, start, end, mute_after=True):
    names = {pb.name for pb in src.pose.bones if any(c.name.startswith(CON_PREFIX) for c in pb.constraints)}
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
        rig["rb_skel_action"] = act.name
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


def _key_matrix(pb, m, frame, prev_q):
    """Pose bone to armature-space matrix `m`, keyed; quaternion sign kept continuous."""
    pb.matrix = m
    pb.keyframe_insert("location", frame=frame)
    if pb.rotation_mode == "QUATERNION":
        q = pb.rotation_quaternion.copy()
        p = prev_q.get(pb.name)
        if p is not None and p.dot(q) < 0:
            q.negate()
            pb.rotation_quaternion = q
        prev_q[pb.name] = q
        pb.keyframe_insert("rotation_quaternion", frame=frame)
    elif pb.rotation_mode == "AXIS_ANGLE":
        pb.keyframe_insert("rotation_axis_angle", frame=frame)
    else:
        p = prev_q.get(pb.name)
        if p is not None:
            pb.rotation_euler = pb.rotation_euler.to_matrix().to_euler(pb.rotation_mode, p)
        prev_q[pb.name] = pb.rotation_euler.copy()
        pb.keyframe_insert("rotation_euler", frame=frame)


def bake_ik_controls(context, rig, start, end):
    """Key every IK control (and its pole) to where the FK pose puts the limb.
    Run while the limbs are in FK; afterwards the IK/FK switch can be flipped on
    any frame without the pose changing – like Unreal's Bake to Control Rig,
    which solves both control sets."""
    limbs = _ik_limbs(rig)
    if not limbs:
        return 0
    R, P = rig.data.bones, rig.pose.bones
    info = []
    for kind, n in limbs:
        off = R[f"ORG-{n['c']}"].matrix_local.inverted() @ R[n["ik"]].matrix_local
        pole_rot = R[n["pole"]].matrix_local.to_3x3().to_4x4()
        rest_bend = _bend_dir(R[f"ORG-{n['a']}"].head_local, R[f"ORG-{n['b']}"].head_local,
                              R[f"ORG-{n['c']}"].head_local) or (R[n["pole"]].head_local
                                                                - R[f"ORG-{n['b']}"].head_local).normalized()
        # pole position in a frame built from the limb (origin = middle joint,
        # x = bend direction, y = root->end), so any rest offset is reproduced
        A0, K0, C0 = (R[f"ORG-{n[k]}"].head_local for k in ("a", "b", "c"))
        pole_local = _limb_frame(A0, K0, C0, rest_bend).transposed() @ (R[n["pole"]].head_local - K0)
        par = P[n["par"]]
        if "pole_vector" in par.keys():
            par["pole_vector"] = type(par["pole_vector"])(1)
            par.keyframe_insert('["pole_vector"]', frame=start)
        for extra in (f"foot_heel_ik.{n['ik'][-1]}", f"foot_spin_ik.{n['ik'][-1]}"):
            if kind == "leg" and extra in P:
                P[extra].matrix_basis = Matrix()
        info.append((kind, n, off, pole_local, pole_rot, rest_bend))
    prev_q, last_bend = {}, {}
    scene = context.scene
    samples = {}
    for f in range(start, end + 1):
        scene.frame_set(f)
        for kind, n, off, pole_local, pole_rot, rest_bend in info:
            org = lambda k: P[f"ORG-{n[k]}"].matrix  # noqa: E731
            a, b, c = org("a").translation, org("b").translation, org("c").translation
            bend = _bend_dir(a, b, c) or last_bend.get(n["ik"], rest_bend)
            last_bend[n["ik"]] = bend
            samples[(n["ik"], f)] = (a.copy(), b.copy(), c.copy(), bend.copy(), org("a").copy())
            m_ik = org("c") @ off
            m_pole = Matrix.Translation(b + _limb_frame(a, b, c, bend) @ pole_local) @ pole_rot
            _key_matrix(P[n["ik"]], m_ik, f, prev_q)
            context.view_layer.update()          # the pole may hang from the IK control
            _key_matrix(P[n["pole"]], m_pole, f, prev_q)
    # Make it exact: on every frame switch the limb to IK, measure how far the
    # elbow/knee ended up rotated about the root->end axis and turn the pole
    # back by that much (converges in 1-2 steps; solver/pole-angle independent).
    for kind, n, off, pole_local, pole_rot, rest_bend in info:
        par = P[n["par"]]
        if "IK_FK" not in par.keys():
            continue
        for f in range(start, end + 1):
            a, b, c, bend, _ = samples[(n["ik"], f)]
            ax = (c - a).normalized()
            off_w = _limb_frame(a, b, c, bend) @ pole_local
            scene.frame_set(f)
            for _ in range(4):
                par["IK_FK"] = 0.0
                rig.update_tag()             # custom props don't tag the depsgraph by themselves
                context.view_layer.update()
                b_ik = P[f"ORG-{n['b']}"].matrix.translation
                u = b - a - ax * (b - a).dot(ax)
                v = b_ik - a - ax * (b_ik - a).dot(ax)
                if u.length < 1e-6 or v.length < 1e-6:
                    break
                ang = math.atan2(u.cross(v).dot(ax), u.dot(v))
                if abs(ang) < 1e-5:
                    break
                off_w = Matrix.Rotation(-ang, 3, ax) @ off_w
                _key_matrix(P[n["pole"]], Matrix.Translation(b + off_w) @ pole_rot, f, prev_q)
        par["IK_FK"] = 1.0
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
    depend on the torso's position."""
    d, ctrl = _root_located(entries)
    if not ctrl:
        return ""
    bind = next((b.name for b in rig.data.bones if b.name.startswith(BIND_PREFIX)
                 and _key(b.name[len(BIND_PREFIX):]) == _key(d)), "")
    if not bind:
        return ""
    pb = rig.pose.bones[ctrl]
    inv = rig.matrix_world.inverted()
    for f in range(start, end + 1):
        context.scene.frame_set(f)
        err = (drv.matrix_world @ drv.pose.bones[d].head) - (rig.matrix_world @ rig.pose.bones[bind].head)
        if err.length < 1e-7:
            continue
        m = pb.matrix.copy()
        m.translation += inv.to_3x3() @ err
        pb.matrix = m
        pb.keyframe_insert("location", frame=f)
    return ctrl


def _root_located(entries):
    """(source bone, control) of the body's located control (torso)."""
    loc = [(d, ctrl) for d, ctrl, use_loc in entries if use_loc and ctrl != "root"]
    return next(((d, c) for d, c in loc if c == "torso"), loc[0] if loc else ("", ""))


def transfer_to_rig(context, src, rig, anim_obj, start, end, keep_helpers=False, mode="FK"):
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
    # "Armature|clip"); a previous bake of the same take is replaced, not piled up
    name = f"{rig.name}|{anim_obj.animation_data.action.name}"
    old = bpy.data.actions.get(name)
    if old is not None and old != act and old.get("rig_bridge_bake") and old.users - int(old.use_fake_user) == 0:
        bpy.data.actions.remove(old)
    act.name = name
    act["rig_bridge_bake"] = True

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
    set_mode(context, rig, "OBJECT")
    return act, len(entries)
