"""Native Rig Bridge modules: built directly into the generated control rig
after Rigify has run (Rigify only sees placeholder ORG bones for them).

Unreal equivalents:
  Control          -> a single FK control
  FK Chain         -> Chain / generic FK modules
  Tail             -> tail with curl master + harmonics ("Chain Harmonics")
  Dynamic Chain    -> Control Rig Dynamics / chain physics (hair, ears, straps)
  Cape             -> cloth physics module (array connector of chains)
  IK Chain         -> generic N-bone IK (tentacles, mech arms)
  Piston           -> mechanical aim pair
  Aim              -> Look At (eyes, turrets)

Dynamics run on Blender's own soft-body / cloth solvers, so they work without
the add-on and render on farms. Layout (no dependency cycles):

  control rig (CR)  ──armature mod──▶  SIM mesh (soft body / cloth)
        │                                   │ vertex groups
        └──Copy Rot/Transforms──▶  DYN rig (Damped Track, influence = 'dynamics')
                                            │
  source skeleton  ◀──Copy Rot/Loc (BIND)───┘
"""

import math

import bpy
from mathutils import Matrix, Vector

from . import resolve, widgets
from .modules import Module, Setting, register_module

SIM_COLL = "RigBridge_Sim"

SIDE_COLORS = {
    "L": ((0.10, 0.40, 1.00), (0.55, 0.75, 1.00), (0.85, 0.92, 1.00)),
    "R": ((1.00, 0.15, 0.10), (1.00, 0.55, 0.50), (1.00, 0.88, 0.85)),
    "": ((1.00, 0.80, 0.05), (1.00, 0.92, 0.55), (1.00, 1.00, 0.85)),
    "PHYS": ((0.70, 0.25, 1.00), (0.85, 0.60, 1.00), (0.95, 0.88, 1.00)),
}


def clean_rest(m: Matrix):
    loc, rot, _ = m.decompose()
    return loc, rot.to_matrix()


def set_bone_color(rig, name, key):
    b = rig.data.bones.get(name)
    if b is None:
        return
    n, s, a = SIDE_COLORS[key]
    b.color.palette = "CUSTOM"
    b.color.custom.normal = n
    b.color.custom.select = s
    b.color.custom.active = a
    pb = rig.pose.bones.get(name)
    if pb is not None:
        pb.color.palette = "DEFAULT"


def add_prop(pb, key, value, lo=None, hi=None, desc=""):
    pb[key] = value
    kw = {"description": desc}
    if lo is not None:
        kw["min"] = kw["soft_min"] = lo
    if hi is not None:
        kw["max"] = kw["soft_max"] = hi
    try:
        pb.id_properties_ui(key).update(**kw)
    except Exception:
        pass
    try:
        pb.property_overridable_library_set(f'["{key}"]', True)
    except Exception:
        pass


def add_driver(owner, path, expr, variables, index=-1):
    fc = owner.driver_add(path, index) if index >= 0 else owner.driver_add(path)
    drv = fc.driver
    drv.type = "SCRIPTED"
    for name, idobj, dp in variables:
        v = drv.variables.new()
        v.name = name
        v.type = "SINGLE_PROP"
        v.targets[0].id_type = "OBJECT"
        v.targets[0].id = idobj
        v.targets[0].data_path = dp
    drv.expression = expr
    return fc


def sim_collection():
    c = bpy.data.collections.get(SIM_COLL)
    if c is None:
        c = bpy.data.collections.new(SIM_COLL)
        bpy.context.scene.collection.children.link(c)
    return c


def remove_sims(src_name):
    for o in [o for o in bpy.data.objects if o.get("rb_src") == src_name and o.get("rb_kind") == "sim"]:
        me = o.data
        bpy.data.objects.remove(o)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)


# ---------------------------------------------------------------------------
# context shared by native modules during one build
# ---------------------------------------------------------------------------

class NCtx:
    def __init__(self, src, rig, meta_of, size=1.0):
        self.src, self.rig, self.meta_of = src, rig, meta_of
        self.size = size
        mw = src.matrix_world
        self.rest = {b.name: mw @ b.matrix_local for b in src.data.bones}
        self.head = {b.name: mw @ b.head_local for b in src.data.bones}
        self.tail = {b.name: mw @ b.tail_local for b in src.data.bones}
        self.length = {n: (self.tail[n] - self.head[n]).length for n in self.head}
        self.inv = rig.matrix_world.inverted()
        self.inv3 = self.inv.to_3x3()
        self.eb = None
        self.state = {}
        self.org_follow = {}     # src bone -> CR bone the ORG placeholder copies
        self.bind = {}           # src bone -> (bone, object name or "")
        self.fk = []             # (src, control, use_loc)
        self.dyn = []            # requests for the DYN rig
        self.info = []           # animator panel entries
        self.row = 21

    # ---- geometry -------------------------------------------------------
    def rest_y(self, b):
        return clean_rest(self.rest[b])[1].col[1].normalized()

    def rest_z(self, b):
        return clean_rest(self.rest[b])[1].col[2].normalized()

    def st(self, spec):
        return self.state.setdefault(id(spec), {})

    # ---- edit-mode helpers ------------------------------------------------
    def unique(self, name):
        n, k = name, 2
        while n in self.eb:
            n = f"{name}_{k}"
            k += 1
        return n

    def bone(self, name, head, tail, z=None, parent=None, deform=False):
        e = self.eb.new(self.unique(name))
        e.head = self.inv @ head
        e.tail = self.inv @ tail
        if (e.tail - e.head).length < 1e-6:
            e.tail = e.head + Vector((0, 0, 1e-3))
        if z is not None and z.length > 1e-8:
            d = (e.tail - e.head).normalized()
            zz = (self.inv3 @ z).normalized()
            if abs(d.dot(zz)) < 0.999:
                e.align_roll(zz)
        e.use_deform = deform
        e.use_connect = False
        if parent:
            e.parent = self.eb[parent]
        return e.name

    def src_bone(self, name, src_name, parent, deform=False):
        loc, rot = clean_rest(self.rest[src_name])
        L = max(self.length[src_name], 1e-3)
        return self.bone(name, loc, loc + rot.col[1] * L, rot.col[2], parent, deform)

    def attach(self, spec, bone=None, override=None):
        """CR bone a part (or one of its bones) hangs from: the ORG bone of the
        nearest driven ancestor, or the socket override."""
        own = set(spec.all_bones or spec.bones)
        ov = override if override is not None else spec.parent
        if ov and ov in self.meta_of:
            n = "ORG-" + self.meta_of[ov]
            if n in self.eb:
                return n
        start = bone or next(b for b in spec.bones if b)
        b = self.src.data.bones[start].parent
        while b and (b.name in own or b.name not in self.meta_of):
            b = b.parent
        if b:
            n = "ORG-" + self.meta_of[b.name]
            if n in self.eb:
                return n
        return "root" if "root" in self.eb else None

    REGIONS = ((("foot", "toe", "heel"), "Toes"), (("hand", "palm", "f_", "thumb"), "Hands"),
               (("head", "neck"), "Face"))
    GROUPED = ("control", "fk_chain", "aim")

    def group_name(self, module, spec):
        """Small helper modules share one collection per body region (Toes.L,
        Hands.R, Face) instead of one collection each – keeps the Bone
        Collections list short, like Unreal's control groups."""
        label = module.label_of(spec)
        if module.id not in self.GROUPED:
            return label
        first = next((b for b in spec.bones if b), None)
        b = self.src.data.bones[first].parent if first else None
        while b is not None:
            meta = self.meta_of.get(b.name)
            if meta:
                low = meta.lower()
                for keys, region in self.REGIONS:
                    if low.startswith(keys):
                        sfx = f".{spec.side}" if spec.side in ("L", "R") and region != "Face" else ""
                        return region + sfx
            b = b.parent
        return label

    # ---- pose-mode helpers ------------------------------------------------
    def coll(self, name, visible=True, ui=True):
        arm = self.rig.data
        bc = arm.collections_all.get(name)
        if bc is None:
            bc = arm.collections.new(name)
            if ui and hasattr(bc, "rigify_ui_row"):
                bc.rigify_ui_row = self.row
                self.row += 1
        bc.is_visible = visible
        return bc

    def control(self, name, shape, scale, side, coll, phys=False):
        pb = self.rig.pose.bones[name]
        pb.custom_shape = widgets.get(shape)
        pb.custom_shape_scale_xyz = (scale, scale, scale)
        pb.use_custom_shape_bone_size = True
        coll.assign(pb.bone)
        set_bone_color(self.rig, name, "PHYS" if phys else side)
        return pb

    def mech(self, *names):
        bc = self.coll("RB Mechanics", visible=False, ui=False)
        for n in names:
            if n:
                bc.assign(self.rig.data.bones[n])
                self.rig.data.bones[n].hide_select = False

    def add_info(self, label, bone, props):
        self.info.append({"label": label, "bone": bone, "props": props})


def chain_frames(nctx, bones, tip=""):
    """Joint-to-joint frames (head_i -> head_i+1) for a chain of source bones."""
    out = []
    n = len(bones)
    for i, b in enumerate(bones):
        h = nctx.head[b].copy()
        if i < n - 1:
            t = nctx.head[bones[i + 1]].copy()
        elif tip and tip in nctx.head and (nctx.head[tip] - h).length > 1e-5:
            t = nctx.head[tip].copy()
        else:
            y = nctx.rest_y(b)
            L = nctx.length[b]
            if n >= 2:
                prev = h - nctx.head[bones[i - 1]]
                if prev.length > 1e-6 and y.angle(prev) > math.radians(60):
                    y = prev.normalized()       # e.g. UE bones with sideways tails
                    L = prev.length * 0.8
            t = h + y * max(L, 1e-3)
        if (t - h).length < 1e-5:
            t = h + nctx.rest_y(b) * max(nctx.length[b], 1e-3)
        out.append((h, t))
    return out


def chain_x(frames):
    """Bend axis shared by the whole chain (normal of its plane)."""
    h0 = frames[0][0]
    te = frames[-1][1]
    d = te - h0
    if len(frames) >= 2:
        best = None
        for h, t in frames[1:]:
            nrm = (h - h0).cross(te - h)
            if best is None or nrm.length > best.length:
                best = nrm
        if best is not None and best.length > 1e-4 * max(d.length, 1e-6) ** 2:
            return best.normalized()
    dd = d.normalized() if d.length > 1e-9 else Vector((0, 0, -1))
    ref = Vector((1, 0, 0)) if abs(dd.x) < 0.9 else Vector((0, 1, 0))
    return (ref - ref.project(dd)).normalized()


def z_for(x, y, fallback):
    z = x.cross(y)
    return z.normalized() if z.length > 1e-4 else fallback


def make_sim_mesh(nctx, name, verts, edges, faces, armature_groups, groups):
    """verts in world space; armature_groups: per-vertex bone name;
    groups: {group: {vertex: weight}}."""
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], edges, faces)
    obj = bpy.data.objects.new(name, me)
    sim_collection().objects.link(obj)
    obj["rb_src"] = nctx.src.name
    obj["rb_kind"] = "sim"
    for vi, bname in enumerate(armature_groups):
        vg = obj.vertex_groups.get(bname) or obj.vertex_groups.new(name=bname)
        vg.add([vi], 1.0, "REPLACE")
    for gname, weights in groups.items():
        vg = obj.vertex_groups.get(gname) or obj.vertex_groups.new(name=gname)
        for vi, w in weights.items():
            vg.add([vi], float(w), "REPLACE")
    m = obj.modifiers.new("Rig", "ARMATURE")
    m.object = nctx.rig
    m.use_vertex_groups = True
    m.use_bone_envelopes = False
    obj.display_type = "WIRE"
    obj.hide_render = True
    obj.show_in_front = True
    return obj


def _cache_range(mod):
    sc = bpy.context.scene
    mod.point_cache.frame_start = sc.frame_start
    mod.point_cache.frame_end = max(sc.frame_end, sc.frame_start + 1)


def setup_soft_body(obj, spec):
    sb = obj.modifiers.new("Dynamics", "SOFT_BODY")
    s = sb.settings
    s.use_goal = True
    s.vertex_group_goal = "goal"
    s.goal_min, s.goal_max = 0.0, 1.0
    s.goal_default = 1.0
    s.goal_spring = 0.5
    s.goal_friction = float(spec.get("damping", 0.8)) * 25.0
    s.use_edges = True
    s.pull = s.push = 0.98
    s.damping = 5.0
    s.bend = float(spec.get("bend", 2.0))
    s.mass = float(spec.get("mass", 1.0))
    s.friction = float(spec.get("drag", 3.0))
    s.speed = 1.0
    s.use_edge_collision = True
    s.effector_weights.gravity = float(spec.get("gravity", 1.0))
    _cache_range(sb)
    return sb


def setup_cloth(obj, spec):
    cl = obj.modifiers.new("Dynamics", "CLOTH")
    s = cl.settings
    s.quality = int(spec.get("quality", 6))
    s.mass = float(spec.get("mass", 0.3))
    s.tension_stiffness = s.compression_stiffness = 15.0
    s.shear_stiffness = 5.0
    s.bending_stiffness = float(spec.get("bending", 0.5))
    s.air_damping = float(spec.get("air", 1.0))
    s.vertex_group_mass = "pin"
    s.pin_stiffness = 1.0
    s.effector_weights.gravity = float(spec.get("gravity", 1.0))
    c = cl.collision_settings
    c.use_collision = True
    c.distance_min = float(spec.get("collide_distance", 0.015))
    c.use_self_collision = False
    _cache_range(cl)
    return cl


# ---------------------------------------------------------------------------
# base class
# ---------------------------------------------------------------------------

class NativeModule(Module):
    backend = "native"

    def cname(self, spec):
        base = (spec.name or self.base_name or self.id).replace(" ", "_")
        return base, (f".{spec.side}" if spec.side else "")

    def label_of(self, spec):
        base, sfx = self.cname(spec)
        return f"{base}{sfx}"

    def validate(self, spec):
        n = len([b for b in spec.bones if b])
        if not (self.min_bones <= n <= self.max_bones):
            return f"{self.label} needs {self.min_bones}–{self.max_bones} bones, got {n}"
        return ""

    def native_edit(self, nctx, spec):
        raise NotImplementedError

    def native_pose(self, nctx, spec):
        pass

    def native_objects(self, nctx, spec):
        pass

    def bind_targets(self, spec, layout, rig_bones):
        return []

    def fk_controls(self, spec, layout, rig_bones):
        return []


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

@register_module
class ControlModule(NativeModule):
    id = "control"
    short = "Control"
    label = "Control"
    description = "One FK control for any bone (head, prop, base, jaw, root of a gadget)"
    category = "Utility"
    icon = "PIVOT_BOUNDBOX"
    kind = "single"
    min_bones = max_bones = 1
    settings = (Setting("size", "Control Size", 1.0, 0.05, 20.0),
                Setting("cube", "Cube Shape", False, desc="Box instead of circle"))

    def native_edit(self, nctx, spec):
        b = spec.bones[0]
        base, sfx = self.cname(spec)
        c = nctx.src_bone(f"{base}{sfx}", b, nctx.attach(spec))
        o = nctx.src_bone(f"OUT-{b}", b, c, deform=True)
        nctx.st(spec).update(ctrl=c, out=o)
        nctx.org_follow[b] = o
        nctx.bind[b] = (o, "")

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        coll = nctx.coll(nctx.group_name(self, spec))
        shape = "cube" if spec.get("cube", False) else "circle"
        sc = float(spec.get("size", 1.0)) * nctx.size * (0.3 if shape == "cube" else 0.5)
        nctx.control(st["ctrl"], shape, sc, spec.side, coll)
        nctx.mech(st["out"])
        nctx.fk.append((spec.bones[0], st["ctrl"], True))


# ---------------------------------------------------------------------------
# Chains: FK Chain / Tail / Dynamic Chain
# ---------------------------------------------------------------------------

DYN_SETTINGS = (
    Setting("stiffness", "Stiffness", 0.6, 0.0, 1.0, "How strongly the chain is pulled back to the animated pose (root)"),
    Setting("stiffness_tip", "Tip Stiffness", 0.35, 0.0, 1.0, "Stiffness at the tip – lower = more drag/flow"),
    Setting("damping", "Damping", 0.8, 0.0, 2.0, "Reduces wobble"),
    Setting("drag", "Air Drag", 3.0, 0.0, 10.0, "Air friction"),
    Setting("bend", "Bend Stiffness", 2.0, 0.0, 10.0, "Resistance to bending (0 = rope)"),
    Setting("gravity", "Gravity", 1.0, 0.0, 5.0, "Gravity multiplier"),
    Setting("mass", "Mass", 1.0, 0.01, 10.0),
)


class _ChainBase(NativeModule):
    kind = "chain"
    category = "Chains"
    min_bones = 1
    use_master = False
    use_wave = False
    dyn_mode = "off"          # 'off' | 'setting' | 'on'

    def dynamics_on(self, spec):
        if self.dyn_mode == "on":
            return True
        if self.dyn_mode == "setting":
            return bool(spec.get("dynamics_enabled", False))
        return False

    def native_edit(self, nctx, spec):
        st = nctx.st(spec)
        bones = list(spec.bones)
        frames = chain_frames(nctx, bones, spec.tip)
        x = chain_x(frames)
        par = nctx.attach(spec)
        base, sfx = self.cname(spec)
        master = None
        if self.use_master and spec.get("curl_master", True) and len(bones) >= 2:
            h = frames[0][0]
            d = frames[-1][1] - h
            dn = d.normalized()
            master = nctx.bone(f"{base}_master{sfx}", h, h + dn * d.length * 0.3, z_for(x, dn, nctx.rest_z(bones[0])), par)
        ctrls, mchs, outs = [], [], []
        prev = par
        for i, (b, (h, t)) in enumerate(zip(bones, frames)):
            y = (t - h).normalized()
            z = z_for(x, y, nctx.rest_z(b))
            p = prev
            if master or self.use_wave:
                p = nctx.bone(f"MCH-{base}_curl.{i + 1:02d}{sfx}", h, t, z, prev)
                mchs.append(p)
            c = nctx.bone(f"{base}_fk.{i + 1:02d}{sfx}", h, t, z, p)
            o = nctx.src_bone(f"OUT-{b}", b, c, deform=True)
            ctrls.append(c)
            outs.append(o)
            prev = c
            nctx.org_follow[b] = o
            nctx.bind[b] = (o, "")
        st.update(frames=frames, x=x, master=master, ctrls=ctrls, mchs=mchs, outs=outs, base=base, sfx=sfx)

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        rig = nctx.rig
        pbs = rig.pose.bones
        size = float(spec.get("size", 1.0)) * nctx.size
        coll = nctx.coll(nctx.group_name(self, spec))
        for c in st["ctrls"]:
            nctx.control(c, "circle", 0.45 * size, spec.side, coll)
        master = st["master"]
        if master:
            nctx.control(master, "master", 0.6 * size, spec.side, coll)
        nctx.mech(*st["mchs"], *st["outs"])
        prop_bone = master or st["ctrls"][0]
        st["prop_bone"] = prop_bone
        props = []
        n = len(st["ctrls"])
        if master:
            for m in st["mchs"]:
                con = pbs[m].constraints.new("COPY_ROTATION")
                con.name = "Curl"
                con.target, con.subtarget = rig, master
                con.owner_space = con.target_space = "LOCAL"
                con.mix_mode = "ADD"
        if self.use_wave:
            pb = pbs[prop_bone]
            add_prop(pb, "wave", 1.0 if spec.get("wave", False) else 0.0, 0.0, 1.0, "Harmonic wave on/off (keyframable)")
            add_prop(pb, "wave_amplitude", float(spec.get("wave_amplitude", 15.0)), 0.0, 90.0, "Degrees per segment at the tip")
            add_prop(pb, "wave_speed", float(spec.get("wave_speed", 0.15)), 0.0, 2.0, "Radians per frame")
            add_prop(pb, "wave_phase", float(spec.get("wave_phase", 0.6)), 0.0, 3.0, "Phase offset per segment")
            props += ["wave", "wave_amplitude", "wave_speed", "wave_phase"]
            dp = f'pose.bones["{prop_bone}"]'
            for i, m in enumerate(st["mchs"]):
                pbs[m].rotation_mode = "XYZ"
                w = (i + 1) / n
                add_driver(pbs[m], "rotation_euler",
                           f"on*amp*0.0174533*{w:.4f}*sin(frame*spd-{i}*ph)",
                           [("on", rig, dp + '["wave"]'), ("amp", rig, dp + '["wave_amplitude"]'),
                            ("spd", rig, dp + '["wave_speed"]'), ("ph", rig, dp + '["wave_phase"]')], index=2)
        if self.dynamics_on(spec):
            add_prop(pbs[prop_bone], "dynamics", float(spec.get("dynamics", 1.0)), 0.0, 1.0,
                     "Blend between animation (0) and simulation (1). Keyframable")
            props.append("dynamics")
        if props:
            nctx.add_info(self.label_of(spec), prop_bone, props)
        for b, c in zip(spec.bones, st["ctrls"]):
            nctx.fk.append((b, c, False))

    def native_objects(self, nctx, spec):
        if not self.dynamics_on(spec):
            return
        st = nctx.st(spec)
        frames, outs = st["frames"], st["outs"]
        n = len(frames)
        pts = [f[0] for f in frames] + [frames[-1][1]]
        arm_groups = [outs[0]] + [outs[k - 1] for k in range(1, n + 1)]
        s0 = float(spec.get("stiffness", 0.6))
        s1 = float(spec.get("stiffness_tip", 0.35))
        goal = {0: 1.0}
        for k in range(1, n + 1):
            t = (k - 1) / max(n - 1, 1)
            goal[k] = s0 + (s1 - s0) * t
        groups = {"goal": goal}
        for k in range(n + 1):
            groups[f"p{k}"] = {k: 1.0}
        obj = make_sim_mesh(nctx, f"SIM-{nctx.src.name}-{st['base']}{st['sfx']}", pts,
                            [(k, k + 1) for k in range(n)], [], arm_groups, groups)
        setup_soft_body(obj, spec)
        nctx.dyn.append({"name": f"{st['base']}{st['sfx']}", "bones": list(spec.bones), "frames": frames,
                         "x": st["x"], "ctrls": st["ctrls"], "sim": obj.name,
                         "vgs": [f"p{k + 1}" for k in range(n)], "prop_bone": st["prop_bone"]})


@register_module
class FKChainModule(_ChainBase):
    id = "fk_chain"
    short = "FK Chain"
    label = "FK Chain"
    description = "Simple FK controls along any chain (ears, straps, antennae, spine of a prop)"
    icon = "LINKED"
    base_name = "chain"
    settings = (Setting("size", "Control Size", 1.0, 0.05, 20.0),)


@register_module
class TailModule(_ChainBase):
    id = "tail"
    short = "Tail"
    label = "Tail"
    description = "FK tail + curl master + harmonic wave; optional dynamics for follow-through"
    icon = "CURVE_PATH"
    base_name = "tail"
    use_master = True
    use_wave = True
    dyn_mode = "setting"
    settings = (
        Setting("size", "Control Size", 1.0, 0.05, 20.0),
        Setting("curl_master", "Curl Master", True, desc="One control that curls the whole tail"),
        Setting("wave", "Wave", False, desc="Procedural harmonic wave (Unreal 'Chain Harmonics')"),
        Setting("wave_amplitude", "Wave Amplitude", 15.0, 0.0, 90.0),
        Setting("wave_speed", "Wave Speed", 0.15, 0.0, 2.0),
        Setting("wave_phase", "Wave Phase", 0.6, 0.0, 3.0),
        Setting("dynamics_enabled", "Dynamics", False, desc="Add physics follow-through on top"),
        Setting("dynamics", "Dynamics Blend", 1.0, 0.0, 1.0),
    ) + DYN_SETTINGS


@register_module
class DynamicChainModule(_ChainBase):
    id = "dynamic_chain"
    short = "Dyn. Chain"
    label = "Dynamic Chain"
    description = "Physics chain (hair, ponytail, earrings, straps): drags behind the animation"
    category = "Physics"
    icon = "FORCE_WIND"
    base_name = "hair"
    dyn_mode = "on"
    settings = (Setting("size", "Control Size", 1.0, 0.05, 20.0),
                Setting("dynamics", "Dynamics Blend", 1.0, 0.0, 1.0,
                        "0 = pure animation, 1 = full simulation (keyframable on the rig)"),
                ) + DYN_SETTINGS


# ---------------------------------------------------------------------------
# Cape (cloth, array connector)
# ---------------------------------------------------------------------------

@register_module
class CapeModule(NativeModule):
    id = "cape"
    short = "Cape"
    label = "Cape / Cloth"
    description = "Several parallel chains simulated as one cloth sheet (cape, skirt, flag)"
    category = "Physics"
    icon = "MOD_CLOTH"
    kind = "array"
    array_label = "Chains"
    array_min = 2
    min_bones = 2
    base_name = "cape"
    settings = (
        Setting("size", "Control Size", 1.0, 0.05, 20.0),
        Setting("dynamics", "Dynamics Blend", 1.0, 0.0, 1.0, "0 = animation, 1 = cloth"),
        Setting("stiffness", "Pin Falloff", 0.0, 0.0, 1.0, "Pull rows below the top towards the animated pose"),
        Setting("mass", "Mass", 0.3, 0.01, 5.0),
        Setting("bending", "Bending", 0.5, 0.0, 50.0),
        Setting("air", "Air Damping", 1.0, 0.0, 10.0),
        Setting("gravity", "Gravity", 1.0, 0.0, 5.0),
        Setting("quality", "Quality", 6, 1, 20),
        Setting("collide_distance", "Collision Distance", 0.015, 0.001, 0.2),
        Setting("loop", "Closed Loop", False, desc="Connect last chain to first (skirt)"),
    )

    def auto_resolve(self, src, socket, selection=(), used=()):
        if len(selection) >= 2:
            return resolve.order_left_to_right(src, list(selection)), ""
        return resolve.sibling_chain_starts(src, socket, used), ""

    def chains(self, src, spec):
        out = []
        for s in spec.bones:
            if not s:
                continue
            c = resolve.follow_chain(src, s)
            c, _ = resolve.split_tip(src, c)
            out.append(c)
        return out

    def expand(self, src, spec):
        return [b for c in self.chains(src, spec) for b in c]

    def validate(self, spec):
        n = len([b for b in spec.bones if b])
        return "" if n >= self.array_min else f"Cape needs at least {self.array_min} chains, got {n}"

    def native_edit(self, nctx, spec):
        st = nctx.st(spec)
        base, sfx = self.cname(spec)
        chains = self.chains(nctx.src, spec)
        st.update(base=base, sfx=sfx, chains=[])
        heads = []
        for ci, chain in enumerate(chains):
            frames = chain_frames(nctx, chain)
            x = chain_x(frames)
            par = nctx.attach(spec, bone=chain[0])
            prev = par
            ctrls, outs = [], []
            for i, (b, (h, t)) in enumerate(zip(chain, frames)):
                y = (t - h).normalized()
                c = nctx.bone(f"{base}_{ci + 1:02d}_fk.{i + 1:02d}{sfx}", h, t, z_for(x, y, nctx.rest_z(b)), prev)
                o = nctx.src_bone(f"OUT-{b}", b, c, deform=True)
                ctrls.append(c)
                outs.append(o)
                prev = c
                nctx.org_follow[b] = o
                nctx.bind[b] = (o, "")
            st["chains"].append({"bones": chain, "frames": frames, "x": x, "ctrls": ctrls, "outs": outs})
            heads.append(frames[0][0])
        centre = sum(heads, Vector()) / len(heads)
        up = Vector((0, 0, 1))
        par = nctx.attach(spec, bone=chains[0][0])
        st["settings_bone"] = nctx.bone(f"{base}_settings{sfx}", centre + up * 0.05, centre + up * 0.15, Vector((0, -1, 0)), par)

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        size = float(spec.get("size", 1.0)) * nctx.size
        coll = nctx.coll(nctx.group_name(self, spec))
        for ch in st["chains"]:
            for c in ch["ctrls"]:
                nctx.control(c, "circle", 0.4 * size, spec.side, coll)
            nctx.mech(*ch["outs"])
        sb = st["settings_bone"]
        nctx.control(sb, "gear", 1.0 * size, "", coll, phys=True)
        add_prop(nctx.rig.pose.bones[sb], "dynamics", float(spec.get("dynamics", 1.0)), 0.0, 1.0,
                 "Blend between animation (0) and cloth (1). Keyframable")
        nctx.add_info(self.label_of(spec), sb, ["dynamics"])
        for ch in st["chains"]:
            for b, c in zip(ch["bones"], ch["ctrls"]):
                nctx.fk.append((b, c, False))

    def native_objects(self, nctx, spec):
        st = nctx.st(spec)
        chains = st["chains"]
        verts, arm_groups, index = [], [], {}
        for ci, ch in enumerate(chains):
            pts = [f[0] for f in ch["frames"]] + [ch["frames"][-1][1]]
            for k, p in enumerate(pts):
                index[(ci, k)] = len(verts)
                verts.append(p)
                arm_groups.append(ch["outs"][0] if k == 0 else ch["outs"][k - 1])
        faces, edges = [], []
        pairs = [(ci, ci + 1) for ci in range(len(chains) - 1)]
        if spec.get("loop", False) and len(chains) > 2:
            pairs.append((len(chains) - 1, 0))
        for a, b in pairs:
            na = len(chains[a]["frames"]) + 1
            nb = len(chains[b]["frames"]) + 1
            for k in range(min(na, nb) - 1):
                faces.append((index[(a, k)], index[(b, k)], index[(b, k + 1)], index[(a, k + 1)]))
        for ci, ch in enumerate(chains):   # edges of chains longer than their neighbours
            for k in range(len(ch["frames"])):
                edges.append((index[(ci, k)], index[(ci, k + 1)]))
        stiff = float(spec.get("stiffness", 0.0))
        pin, groups = {}, {}
        for (ci, k), vi in index.items():
            n = len(chains[ci]["frames"])
            pin[vi] = 1.0 if k == 0 else stiff * (1.0 - k / max(n, 1))
            groups[f"c{ci}_{k}"] = {vi: 1.0}
        groups["pin"] = pin
        obj = make_sim_mesh(nctx, f"SIM-{nctx.src.name}-{st['base']}{st['sfx']}", verts, edges, faces, arm_groups, groups)
        setup_cloth(obj, spec)
        for ci, ch in enumerate(chains):
            nctx.dyn.append({"name": f"{st['base']}_{ci + 1:02d}{st['sfx']}", "bones": ch["bones"],
                             "frames": ch["frames"], "x": ch["x"], "ctrls": ch["ctrls"], "sim": obj.name,
                             "vgs": [f"c{ci}_{k + 1}" for k in range(len(ch["frames"]))],
                             "prop_bone": st["settings_bone"]})


# ---------------------------------------------------------------------------
# IK Chain
# ---------------------------------------------------------------------------

def _signed_angle(u, v, normal):
    a = u.angle(v)
    if normal.dot(u.cross(v)) < 0:
        a = -a
    return a


@register_module
class IKChainModule(NativeModule):
    id = "ik_chain"
    short = "IK Chain"
    label = "IK Chain"
    description = "N-bone IK with pole and IK/FK blend (tentacles, mech legs, robot arms)"
    category = "Limbs"
    icon = "CON_SPLINEIK"
    kind = "chain"
    min_bones = 2
    base_name = "ik"
    settings = (
        Setting("size", "Control Size", 1.0, 0.05, 20.0),
        Setting("use_pole", "Pole Target", True),
        Setting("ik_default", "IK by default", True),
        Setting("stretch", "IK Stretch", False),
    )

    def native_edit(self, nctx, spec):
        st = nctx.st(spec)
        bones = list(spec.bones)
        frames = chain_frames(nctx, bones, spec.tip)
        x = chain_x(frames)
        par = nctx.attach(spec)
        base, sfx = self.cname(spec)
        fks, iks, bls, outs = [], [], [], []
        pf = pi = pb = par
        for i, (b, (h, t)) in enumerate(zip(bones, frames)):
            y = (t - h).normalized()
            z = z_for(x, y, nctx.rest_z(b))
            pf = nctx.bone(f"{base}_fk.{i + 1:02d}{sfx}", h, t, z, pf)
            pi = nctx.bone(f"MCH-{base}_ik.{i + 1:02d}{sfx}", h, t, z, pi)
            pb = nctx.bone(f"MCH-{base}_blend.{i + 1:02d}{sfx}", h, t, z, pb)
            o = nctx.src_bone(f"OUT-{b}", b, pb, deform=True)
            fks.append(pf)
            iks.append(pi)
            bls.append(pb)
            outs.append(o)
            nctx.org_follow[b] = o
            nctx.bind[b] = (o, "")
        h, t = frames[-1]
        y = (t - h).normalized()
        L = (t - h).length
        root = "root" if "root" in nctx.eb else None
        ik = nctx.bone(f"{base}_ik{sfx}", t, t + y * L * 0.5, z_for(x, y, nctx.rest_z(bones[-1])), root)
        pole = None
        pole_angle = 0.0
        if spec.get("use_pole", True) and len(bones) >= 2:
            p0, pe = frames[0][0], frames[-1][1]
            mid = frames[len(frames) // 2][0]
            line = pe - p0
            v = mid - (p0 + (mid - p0).project(line))
            if v.length < 1e-4 * max(line.length, 1e-6):
                v = x.cross(line)
            total = sum((tt - hh).length for hh, tt in frames)
            pp = mid + v.normalized() * total * 0.6
            pole = nctx.bone(f"{base}_pole{sfx}", pp, pp + v.normalized() * total * 0.1, None, root)
            e0, en = nctx.eb[iks[0]], nctx.eb[iks[-1]]
            pole_normal = (en.tail - e0.head).cross(nctx.inv @ pp - e0.head)
            proj = pole_normal.cross(e0.tail - e0.head)
            pole_angle = _signed_angle(e0.x_axis, proj, e0.tail - e0.head)
        st.update(fks=fks, iks=iks, bls=bls, outs=outs, ik=ik, pole=pole, pole_angle=pole_angle)

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        rig = nctx.rig
        pbs = rig.pose.bones
        size = float(spec.get("size", 1.0)) * nctx.size
        coll = nctx.coll(nctx.group_name(self, spec))
        for c in st["fks"]:
            nctx.control(c, "circle", 0.45 * size, spec.side, coll)
        nctx.control(st["ik"], "cube", 0.5 * size, spec.side, coll)
        if st["pole"]:
            nctx.control(st["pole"], "diamond", 0.5 * size, spec.side, coll)
        nctx.mech(*st["iks"], *st["bls"], *st["outs"])
        con = pbs[st["iks"][-1]].constraints.new("IK")
        con.target, con.subtarget = rig, st["ik"]
        con.use_tail = True
        con.chain_count = len(st["iks"])
        if st["pole"]:
            con.pole_target, con.pole_subtarget = rig, st["pole"]
            con.pole_angle = st["pole_angle"]
            # the analytic pole angle can be off by 90/180 degrees depending on the
            # chain's roll; keep the candidate that leaves the rest pose untouched
            mchs = [pbs[m] for m in st["iks"]]

            def drift():
                bpy.context.view_layer.update()
                return sum((p.matrix.to_translation() - p.bone.matrix_local.to_translation()).length +
                           (p.matrix.to_3x3().normalized().col[1] - p.bone.matrix_local.to_3x3().col[1]).length
                           for p in mchs)
            a = st["pole_angle"]
            best = None
            for cand in (a, -a, a + math.pi, math.pi - a, a + math.pi / 2, a - math.pi / 2):
                cand = math.atan2(math.sin(cand), math.cos(cand))
                con.pole_angle = cand
                d = drift()
                if best is None or d < best[0] - 1e-9:
                    best = (d, cand)
            con.pole_angle = best[1]
        if spec.get("stretch", False):
            for m in st["iks"]:
                pbs[m].ik_stretch = 0.1
            con.use_stretch = True
        ik_pb = pbs[st["ik"]]
        add_prop(ik_pb, "IK_FK", 0.0 if spec.get("ik_default", True) else 1.0, 0.0, 1.0, "0 = IK, 1 = FK")
        dp = f'pose.bones["{st["ik"]}"]["IK_FK"]'
        for bl, ikb, fkb in zip(st["bls"], st["iks"], st["fks"]):
            c1 = pbs[bl].constraints.new("COPY_TRANSFORMS")
            c1.target, c1.subtarget = rig, ikb
            c2 = pbs[bl].constraints.new("COPY_TRANSFORMS")
            c2.target, c2.subtarget = rig, fkb
            add_driver(c2, "influence", "v", [("v", rig, dp)])
        nctx.add_info(self.label_of(spec), st["ik"], ["IK_FK"])
        for b, c in zip(spec.bones, st["fks"]):
            nctx.fk.append((b, c, False))


# ---------------------------------------------------------------------------
# Piston
# ---------------------------------------------------------------------------

@register_module
class PistonModule(NativeModule):
    id = "piston"
    short = "Piston"
    label = "Piston"
    description = "Cylinder + rod that always aim at each other and slide (hydraulics, shocks)"
    category = "Mechanical"
    icon = "CON_STRETCHTO"
    kind = "slots"
    slots = ("Cylinder", "Rod", "Rod Socket", "Cylinder Socket")
    optional_slots = (2, 3)
    min_bones = 2
    max_bones = 4
    base_name = "piston"
    settings = ()

    def auto_resolve(self, src, socket, selection=(), used=()):
        sel = [s for s in selection if s != socket]
        rod = sel[0] if sel else resolve.piston_rod(src, socket, used)
        return [socket, rod, resolve.piston_rod_socket(src, socket, rod), ""], ""

    def expand(self, src, spec):
        return [b for b in spec.bones[:2] if b]

    def validate(self, spec):
        b = list(spec.bones) + ["", ""]
        if not b[0] or not b[1]:
            return "Piston needs a Cylinder and a Rod"
        return ""

    def native_edit(self, nctx, spec):
        st = nctx.st(spec)
        b = list(spec.bones) + ["", "", "", ""]
        cyl, rod, rod_sock, cyl_sock = b[:4]
        base, sfx = self.cname(spec)
        att_c = nctx.attach(spec, bone=cyl, override=cyl_sock or spec.parent or "")
        att_r = nctx.attach(spec, bone=rod, override=rod_sock or "")
        pc = nctx.head[cyl]
        rh, rt = nctx.head[rod], nctx.tail[rod]
        pr = rh if (rh - pc).length >= (rt - pc).length else rt
        d = pr - pc
        L = max(d.length, 1e-3)
        u = d / L
        zc = nctx.rest_z(cyl)
        zc = (zc - zc.project(u)).normalized() if (zc - zc.project(u)).length > 1e-4 else chain_x([(pc, pr)])
        anc_c = nctx.bone(f"MCH-{base}_anchor_cyl{sfx}", pc, pc + u * L * 0.1, zc, att_c)
        anc_r = nctx.bone(f"MCH-{base}_anchor_rod{sfx}", pr, pr - u * L * 0.1, zc, att_r)
        aim_c = nctx.bone(f"MCH-{base}_aim_cyl{sfx}", pc, pc + u * max(nctx.length[cyl], 1e-3), zc, att_c)
        aim_r = nctx.bone(f"MCH-{base}_aim_rod{sfx}", pr, pr - u * max(nctx.length[rod], 1e-3), zc, att_r)
        out_c = nctx.src_bone(f"OUT-{cyl}", cyl, aim_c, deform=True)
        out_r = nctx.src_bone(f"OUT-{rod}", rod, aim_r, deform=True)
        st.update(anc_c=anc_c, anc_r=anc_r, aim_c=aim_c, aim_r=aim_r, out_c=out_c, out_r=out_r)
        for s, o in ((cyl, out_c), (rod, out_r)):
            nctx.org_follow[s] = o
            nctx.bind[s] = (o, "")

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        rig = nctx.rig
        pbs = rig.pose.bones
        for aim, tgt in ((st["aim_c"], st["anc_r"]), (st["aim_r"], st["anc_c"])):
            c = pbs[aim].constraints.new("DAMPED_TRACK")
            c.target, c.subtarget = rig, tgt
        nctx.mech(st["anc_c"], st["anc_r"], st["aim_c"], st["aim_r"], st["out_c"], st["out_r"])


# ---------------------------------------------------------------------------
# Aim / Look At
# ---------------------------------------------------------------------------

AXES = [Vector((0, 1, 0)), Vector((0, -1, 0)), Vector((0, 0, 1)), Vector((0, 0, -1)),
        Vector((1, 0, 0)), Vector((-1, 0, 0))]


@register_module
class AimModule(NativeModule):
    id = "aim"
    short = "Aim"
    label = "Aim / Look At"
    description = "Bones look at a target control (eyes share one master target, turrets, lamps)"
    category = "Utility"
    icon = "CON_TRACKTO"
    kind = "array"
    array_label = "Bones"
    array_min = 1
    min_bones = 1
    base_name = "aim"
    settings = (
        Setting("size", "Control Size", 1.0, 0.05, 20.0),
        Setting("distance", "Target Distance", 8.0, 0.5, 100.0, "In bone lengths"),
        Setting("axis", "Aim Axis", -1, -1, 5, "-1 auto (faces -Y/front), 0 +Y, 1 -Y, 2 +Z, 3 -Z, 4 +X, 5 -X"),
        Setting("follow", "Target follows parent", False, desc="Off = target stays in world space"),
    )

    def auto_resolve(self, src, socket, selection=(), used=()):
        if len(selection) >= 2:
            return list(selection), ""
        if "eye" in resolve.core_tokens(socket):
            return resolve.eye_pair(src, socket, used), ""
        return [socket], ""

    def validate(self, spec):
        return "" if any(spec.bones) else "Aim needs at least one bone"

    def _axis(self, nctx, b, spec):
        rot = clean_rest(nctx.rest[b])[1]
        idx = int(spec.get("axis", -1))
        if 0 <= idx < 6:
            return (rot @ AXES[idx]).normalized()
        fwd = Vector((0, -1, 0))
        return max(((rot @ a).normalized() for a in AXES), key=lambda v: v.dot(fwd))

    def native_edit(self, nctx, spec):
        st = nctx.st(spec)
        base, sfx = self.cname(spec)
        bones = [b for b in spec.bones if b]
        dist = float(spec.get("distance", 8.0))
        aims, outs, tgts, tpos = [], [], [], []
        for i, b in enumerate(bones):
            h = nctx.head[b]
            ax = self._axis(nctx, b, spec)
            L = max(nctx.length[b], 1e-3)
            att = nctx.attach(spec, bone=b)
            ref = nctx.rest_z(b)
            if abs(ref.dot(ax)) > 0.95:
                ref = nctx.rest_y(b)
            ref = (ref - ref.project(ax)).normalized()
            a = nctx.bone(f"MCH-{base}_aim.{i + 1:02d}{sfx}", h, h + ax * L, ref, att)
            o = nctx.src_bone(f"OUT-{b}", b, a, deform=True)
            aims.append(a)
            outs.append(o)
            tpos.append((h + ax * L * dist, ax, L, ref))
            nctx.org_follow[b] = o
            nctx.bind[b] = (o, "")
        follow_par = nctx.attach(spec) if spec.get("follow", False) else ("root" if "root" in nctx.eb else None)
        master = None
        if len(bones) > 1:
            c = sum((p for p, *_ in tpos), Vector()) / len(tpos)
            ax, L, ref = tpos[0][1], tpos[0][2], tpos[0][3]
            master = nctx.bone(f"{base}_target{sfx}", c, c + ax * L, ref, follow_par)
        for i, (p, ax, L, ref) in enumerate(tpos):
            nm = f"{base}_target.{i + 1:02d}{sfx}" if master else f"{base}_target{sfx}"
            tgts.append(nctx.bone(nm, p, p + ax * L, ref, master or follow_par))
        st.update(aims=aims, outs=outs, tgts=tgts, master=master, L=max(t[2] for t in tpos))

    def native_pose(self, nctx, spec):
        st = nctx.st(spec)
        rig = nctx.rig
        pbs = rig.pose.bones
        size = float(spec.get("size", 1.0)) * nctx.size
        coll = nctx.coll(nctx.group_name(self, spec))
        for a, t in zip(st["aims"], st["tgts"]):
            c = pbs[a].constraints.new("DAMPED_TRACK")
            c.target, c.subtarget = rig, t
        for t in st["tgts"]:
            nctx.control(t, "target", 0.8 * size, spec.side, coll)
        if st["master"]:
            nctx.control(st["master"], "square", 2.5 * size, spec.side, coll)
        nctx.mech(*st["aims"], *st["outs"])


# ---------------------------------------------------------------------------
# build stages (called from builder)
# ---------------------------------------------------------------------------

def run_native(context, src, rig, specs, meta_of, size, set_mode):
    from .modules import MODULES
    natives = [s for s in specs if MODULES[s.module].backend == "native"]
    nctx = NCtx(src, rig, meta_of, size)
    remove_sims(src.name)
    if not natives:
        _remove_dyn_rig(src)
        return nctx
    set_mode(context, rig, "EDIT")
    nctx.eb = rig.data.edit_bones
    for s in natives:
        MODULES[s.module].native_edit(nctx, s)
    set_mode(context, rig, "POSE")
    pbs = rig.pose.bones
    for s in natives:
        MODULES[s.module].native_pose(nctx, s)
    # ORG placeholders follow the native output so Rigify children inherit it
    for b, out in nctx.org_follow.items():
        org = "ORG-" + meta_of.get(b, "")
        if org in pbs:
            c = pbs[org].constraints.new("COPY_TRANSFORMS")
            c.name = "RB follow"
            c.target, c.subtarget = rig, out
    set_mode(context, rig, "OBJECT")
    for s in natives:
        MODULES[s.module].native_objects(nctx, s)
    if nctx.dyn:
        _build_dyn_rig(context, src, rig, nctx, set_mode)
    else:
        _remove_dyn_rig(src)
    return nctx


def _remove_dyn_rig(src):
    name = f"{src.name}_dyn"
    o = bpy.data.objects.get(name)
    if o is not None and o.get("rb_kind") == "dyn":
        data = o.data
        bpy.data.objects.remove(o)
        if data and data.users == 0:
            bpy.data.armatures.remove(data)


def _build_dyn_rig(context, src, rig, nctx, set_mode):
    name = f"{src.name}_dyn"
    dyn = bpy.data.objects.get(name)
    if dyn is None or dyn.type != "ARMATURE":
        arm = bpy.data.armatures.new(name)
        dyn = bpy.data.objects.new(name, arm)
        sim_collection().objects.link(dyn)
    dyn["rb_kind"] = "dyn"
    dyn["rb_src"] = src.name
    dyn.matrix_world = Matrix.Identity(4)
    dyn.hide_render = True
    dyn.hide_set(False)
    dyn.hide_viewport = False
    if dyn.animation_data:
        for fc in list(dyn.animation_data.drivers):
            dyn.animation_data.drivers.remove(fc)
    set_mode(context, dyn, "EDIT")
    eb = dyn.data.edit_bones
    for e in list(eb):
        eb.remove(e)
    made = []
    for req in nctx.dyn:
        prev = None
        chain = []
        for i, (b, (h, t)) in enumerate(zip(req["bones"], req["frames"])):
            y = (t - h).normalized()
            z = z_for(req["x"], y, nctx.rest_z(b))
            e = eb.new(f"DYN-{req['name']}.{i + 1:02d}")
            e.head, e.tail = h, t
            if abs(y.dot(z)) < 0.999:
                e.align_roll(z)
            e.use_deform = False
            if prev:
                e.parent = eb[prev]
            prev = e.name
            loc, rot = clean_rest(nctx.rest[b])
            o = eb.new(f"DOUT-{b}")
            o.head = loc
            o.tail = loc + rot.col[1] * max(nctx.length[b], 1e-3)
            o.align_roll(rot.col[2])
            o.parent = e
            o.use_deform = False
            chain.append((e.name, o.name, b))
        made.append((req, chain))
    set_mode(context, dyn, "POSE")
    pbs = dyn.pose.bones
    for req, chain in made:
        sim = bpy.data.objects[req["sim"]]
        dp = f'pose.bones["{req["prop_bone"]}"]["dynamics"]'
        for i, (dn, on, b) in enumerate(chain):
            p = pbs[dn]
            c = p.constraints.new("COPY_TRANSFORMS" if i == 0 else "COPY_ROTATION")
            c.target, c.subtarget = rig, req["ctrls"][i]
            d = p.constraints.new("DAMPED_TRACK")
            d.name = "Dynamics"
            d.target, d.subtarget = sim, req["vgs"][i]
            add_driver(d, "influence", "a", [("a", rig, dp)])
            nctx.bind[b] = (on, dyn.name)
    set_mode(context, dyn, "OBJECT")
    dyn.hide_set(True)
