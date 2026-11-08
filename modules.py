"""Rig modules — the lego pieces.

A *part* is one piece of the mapping (e.g. "left arm = upperarm_l, lowerarm_l,
hand_l").  Its *module* decides how that piece becomes Rigify meta-rig bones,
which Rigify rig type drives it, which generated bones the source skeleton is
bound to, and which FK controls receive retargeted animation.

Add your own module by subclassing ``Module`` and decorating it with
``@register_module`` — either here or in a ``.py`` file dropped into the user
modules folder (see README).
"""

from dataclasses import dataclass, field
from mathutils import Vector

from . import resolve
from .naming import chain_name

MODULES: dict[str, "Module"] = {}

AUTO_PARENT = "<auto>"

CATEGORIES = ["Body", "Limbs", "Hands", "Chains", "Physics", "Mechanical", "Utility"]


def register_module(cls):
    MODULES[cls.id] = cls()
    return cls


@dataclass
class PartSpec:
    module: str
    name: str
    side: str            # 'L', 'R' or ''
    bones: list[str]     # source bone names, root -> tip (slots may hold "")
    tip: str = ""        # optional end-site bone, only used for its position
    settings: dict = field(default_factory=dict)
    parent: str = ""     # socket override: attach to this source bone
    all_bones: list[str] = field(default_factory=list)   # filled by Module.expand

    def get(self, key, default=None):
        return self.settings.get(key, default)


@dataclass
class Setting:
    """A per-module option shown in the Details panel (Unreal: module settings)."""
    key: str
    label: str
    default: object
    min: float | None = None
    max: float | None = None
    desc: str = ""


@dataclass
class BoneLayout:
    name: str
    head: Vector
    tail: Vector
    parent: str = AUTO_PARENT      # meta-rig bone name, AUTO_PARENT or None
    connect: bool = False
    roll_z: Vector | None = None   # desired Z axis direction (None = generic)
    src: str | None = None         # source bone bound to this meta bone


@dataclass
class CollDef:
    name: str
    row: int
    title: str = ""
    color: int = 0


# Standard Rigify-like collection layout (rows match the human meta-rig)
def _limb_colls(kind, side):
    s = f".{side}" if side else ""
    return [CollDef(f"{kind}{s} (IK)", 10 if kind == "Arm" else 14, "", 2),
            CollDef(f"{kind}{s} (FK)", 11 if kind == "Arm" else 15, "(FK)", 5),
            CollDef(f"{kind}{s} (Tweak)", 12 if kind == "Arm" else 16, "(Tweak)", 4)]


TORSO = CollDef("Torso", 4, "", 3)
TORSO_TWEAK = CollDef("Torso (Tweak)", 5, "(Tweak)", 4)
FINGERS = CollDef("Fingers", 7, "", 6)
FINGERS_DETAIL = CollDef("Fingers (Detail)", 8, "(Detail)", 5)
EXTRA = CollDef("Extra", 18, "", 6)


def set_param(params, key, value):
    if hasattr(params, key):
        try:
            setattr(params, key, value)
        except (TypeError, AttributeError, ValueError):
            pass


def add_coll_ref(params, attr, bcoll):
    refs = getattr(params, attr + "_coll_refs", None)
    if refs is not None and bcoll is not None:
        refs.add().set_collection(bcoll)


def _chain_root_type(pbones, name):
    """Rigify type of the rig owning bone `name` (walk up connected parents)."""
    pb = pbones[name]
    while not pb.rigify_type and pb.bone.use_connect and pb.parent:
        pb = pb.parent
    return pb.rigify_type


class Module:
    id = ""
    label = ""
    description = ""
    min_bones = 1
    max_bones = 64
    rigify_type = ""
    finger_style = False
    base_name = ""
    # --- modular-rig metadata (Unreal Module Assets) ----------------------
    category = "Utility"
    icon = "BONE_DATA"
    backend = "rigify"          # 'rigify' (meta-rig) | 'native' (Rig Bridge) | 'merge'
    kind = "chain"              # 'chain' | 'single' | 'slots' | 'array'
    slots: tuple = ()           # connector labels for kind == 'slots'
    optional_slots: tuple = ()  # indices of optional slots
    array_label = "Bones"
    in_library = True           # shown as a tile in Module Assets
    settings: tuple = ()        # tuple[Setting]
    short = ""                  # tile / overlay label (defaults to label)

    @property
    def tile(self):
        return self.short or self.label

    # ---- connector event (auto-resolve from a socket) -------------------
    def auto_resolve(self, src, socket: str, selection=(), used=()):
        """Return (bones, tip) when this module is dropped on `socket`."""
        if self.kind == "single":
            return [socket], ""
        if self.kind == "chain":
            chain = resolve.follow_chain(src, socket)
            chain, tip = resolve.split_tip(src, chain)
            return chain[: self.max_bones], tip
        return [socket], ""

    def expand(self, src, spec) -> list[str]:
        """Every skeleton bone this part drives (used for validation/binding)."""
        return [b for b in spec.bones if b]

    def connectors(self, spec_len=0):
        """[(label, kind, index, optional)] for the Details panel."""
        if self.kind == "chain":
            return [("Start", "start", 0, False), ("End", "end", -1, False)]
        if self.kind == "single":
            return [("Bone", "slot", 0, False)]
        if self.kind == "slots":
            return [(lbl, "slot", i, i in self.optional_slots) for i, lbl in enumerate(self.slots)]
        return [(self.array_label, "array", 0, False)]

    def post_rigify(self, rig, spec, layout):
        """Apply module settings to the generated Rigify rig."""

    def validate(self, spec) -> str:
        n = len([b for b in spec.bones if b])
        if not (self.min_bones <= n <= self.max_bones):
            return f"{self.label} needs {self.min_bones}–{self.max_bones} bones, got {n}"
        return ""

    # ---- naming --------------------------------------------------------
    def base(self, spec: PartSpec) -> str:
        return spec.name or self.base_name or self.id

    def names(self, spec: PartSpec, count: int) -> list[str]:
        return [chain_name(self.base(spec), i, spec.side, self.finger_style) for i in range(count)]

    # ---- geometry ------------------------------------------------------
    def layout(self, ctx, spec: PartSpec) -> list[BoneLayout]:
        """Default: one meta bone per source bone, connected chain."""
        heads, tails = ctx.chain_geometry(spec)
        names = self.names(spec, len(spec.bones))
        out = []
        for i, (n, h, t, src) in enumerate(zip(names, heads, tails, spec.bones)):
            out.append(BoneLayout(
                name=n, head=h, tail=t,
                parent=AUTO_PARENT if i == 0 else names[i - 1],
                connect=i > 0, roll_z=None, src=src))
        return out

    # ---- rigify configuration -----------------------------------------
    def collections(self, spec) -> list[CollDef]:
        return [TORSO]

    def configure(self, ctx, spec, layout, pbones, colls):
        first = pbones[layout[0].name]
        first.rigify_type = self.rigify_type
        main = colls.get(self.collections(spec)[0].name)
        for lb in layout:
            if main:
                main.assign(pbones[lb.name])

    # ---- binding / retarget -------------------------------------------
    def bind_targets(self, spec, layout, rig_bones) -> list[tuple[str, str]]:
        """(source bone, generated rig bone) pairs."""
        out = []
        for lb in layout:
            if not lb.src:
                continue
            for cand in (f"DEF-{lb.name}", f"ORG-{lb.name}", lb.name):
                if cand in rig_bones:
                    out.append((lb.src, cand))
                    break
        return out

    def fk_controls(self, spec, layout, rig_bones) -> list[tuple[str, str, bool]]:
        """(source bone, control, copy_location) for animation transfer."""
        return [(lb.src, lb.name, False) for lb in layout if lb.src and lb.name in rig_bones]


# ---------------------------------------------------------------------------
# Built-in modules
# ---------------------------------------------------------------------------

def _bind_org_first(layout, rig_bones):
    """ORG bones carry the exact FK/IK result; Rigify's DEF spine/neck are
    B-bones stretched between tweaks whose roll is interpolated."""
    out = []
    for lb in layout:
        if lb.src:
            for cand in (f"ORG-{lb.name}", f"DEF-{lb.name}", lb.name):
                if cand in rig_bones:
                    out.append((lb.src, cand))
                    break
    return out


@register_module
class RootModule(Module):
    id = "root"
    short = "Root"
    label = "Root"
    description = "Top-level root/motion bone, bound to the Rigify 'root' (global) control"
    min_bones = max_bones = 1
    category = "Body"
    icon = "OBJECT_ORIGIN"
    kind = "single"

    def layout(self, ctx, spec):
        return []

    def collections(self, spec):
        return []

    def configure(self, ctx, spec, layout, pbones, colls):
        pass

    def bind_targets(self, spec, layout, rig_bones):
        return [(spec.bones[0], "root")] if "root" in rig_bones else []

    def fk_controls(self, spec, layout, rig_bones):
        return [(spec.bones[0], "root", True)] if "root" in rig_bones else []


@register_module
class SpineModule(Module):
    id = "spine"
    short = "Spine"
    label = "Spine (hips → chest)"
    description = "Hips → chest. Torso, hips and chest controls with FK (Rigify basic_spine)"
    min_bones = 1
    rigify_type = "spines.basic_spine"
    base_name = "spine"
    category = "Body"
    icon = "MOD_ARMATURE"
    settings = (Setting("fk_controls", "FK Controls", True, desc="Extra FK spine controls"),)

    def auto_resolve(self, src, socket, selection=(), used=()):
        return resolve.spine_chain(src, socket), ""

    def layout(self, ctx, spec):
        out = super().layout(ctx, spec)
        for lb in out:
            lb.roll_z = ctx.forward
        return out

    def collections(self, spec):
        return [TORSO, TORSO_TWEAK]

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        p = pbones[layout[0].name]
        if len(layout) < 2:
            p.rigify_type = "basic.super_copy"
            return
        prm = p.rigify_parameters
        set_param(prm, "pivot_pos", max(1, min(2, len(layout) - 1)))
        set_param(prm, "make_fk_controls", bool(spec.get("fk_controls", True)))
        add_coll_ref(prm, "tweak", colls.get(TORSO_TWEAK.name))
        add_coll_ref(prm, "fk", colls.get(TORSO_TWEAK.name))

    def bind_targets(self, spec, layout, rig_bones):
        return _bind_org_first(layout, rig_bones)

    def post_rigify(self, rig, spec, layout):
        """basic_spine parents ORG-spine.N to its joint tweak and nothing else, so
        ORG (and every limb hanging from it) takes the tweak's roll – the roll of
        the segment below. Give each middle ORG the FK control's rotation, then
        swing it back onto the next tweak: exact roll, tweaks still work."""
        super().post_rigify(rig, spec, layout)
        pbs = rig.pose.bones
        tweaks = [pb for pb in pbs if pb.name.startswith("tweak_spine")]
        for i, lb in enumerate(layout):
            org = pbs.get(f"ORG-{lb.name}")
            fk = pbs.get(chain_name("spine_fk", i, ""))
            if i == 0 or org is None or fk is None or "RB_Roll" in org.constraints:
                continue
            tail = org.bone.tail_local
            nxt = min(tweaks, key=lambda t: (t.bone.head_local - tail).length, default=None)
            c = org.constraints.new("COPY_ROTATION")
            c.name, c.target, c.subtarget = "RB_Roll", rig, fk.name
            if nxt is not None and (nxt.bone.head_local - tail).length < 1e-4 * max(1.0, org.bone.length):
                c = org.constraints.new("DAMPED_TRACK")
                c.name, c.target, c.subtarget = "RB_Aim", rig, nxt.name

    def fk_controls(self, spec, layout, rig_bones):
        out = []
        if "torso" in rig_bones and layout:
            # torso carries the world position/orientation of the hips
            out.append((layout[0].src, "torso", True))
        for i, lb in enumerate(layout):
            fk = chain_name("spine_fk", i, "") if self.base(spec) == "spine" else f"{lb.name}_fk"
            if fk in rig_bones:
                out.append((lb.src, fk, False))
        return out


@register_module
class NeckHeadModule(Module):
    id = "neck_head"
    short = "Neck/Head"
    label = "Neck + Head"
    description = "Neck bone(s) + head, with head/neck follow switches (Rigify super_head)"
    min_bones = 1
    rigify_type = "spines.super_head"
    category = "Body"
    icon = "USER"

    def auto_resolve(self, src, socket, selection=(), used=()):
        return resolve.neck_head_chain(src, socket)
    # never use_connect the neck: Rigify chains swallow connected children
    connect_to_parent = False

    def names(self, spec, count):
        return [chain_name("neck", i, "") for i in range(count - 1)] + ["head"]

    def layout(self, ctx, spec):
        heads, tails = ctx.chain_geometry(spec)
        if not spec.tip:  # head end: straight up, ~11% of character height
            tails[-1] = heads[-1] + ctx.up * max(ctx.unit * 0.11, (heads[-1] - heads[0]).length * 1.2)
        names = self.names(spec, len(spec.bones))
        out = []
        for i, (n, h, t, src) in enumerate(zip(names, heads, tails, spec.bones)):
            out.append(BoneLayout(n, h, t, AUTO_PARENT if i == 0 else names[i - 1],
                                  i > 0, ctx.forward, src))
        return out

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        p = pbones[layout[0].name]
        if len(layout) < 2:
            p.rigify_type = "basic.super_copy"
            return
        prm = p.rigify_parameters
        par = p.bone.parent
        touching = bool(par) and (par.tail_local - p.bone.head_local).length < 1e-5
        set_param(prm, "connect_chain", touching
                  and _chain_root_type(pbones, par.name) == "spines.basic_spine")
        add_coll_ref(prm, "tweak", colls.get(TORSO_TWEAK.name))

    def collections(self, spec):
        return [TORSO, TORSO_TWEAK]

    def bind_targets(self, spec, layout, rig_bones):
        return _bind_org_first(layout, rig_bones)

    def fk_controls(self, spec, layout, rig_bones):
        out = []
        if len(layout) >= 2 and "neck" in rig_bones:
            out.append((layout[0].src, "neck", False))
        if "head" in rig_bones:
            out.append((layout[-1].src, "head", False))
        if len(layout) > 2:
            # multi-bone neck (UE neck_01/02, CC NeckTwist01/02, Daz neckLower/Upper):
            # ORG-neck.N copies tweak_neck.N and stretches to the next one, so
            # keying every tweak (rotation + joint position) makes each neck
            # bone exact instead of interpolated – a full FK neck, like Unreal's
            for lb in layout[:-1]:
                tw = f"tweak_{lb.name}"
                if lb.src and tw in rig_bones:
                    out.append((lb.src, tw, True))
        return out


@register_module
class ShoulderModule(Module):
    id = "shoulder"
    short = "Shoulder"
    label = "Shoulder / Clavicle"
    description = "Clavicle control (one bone)"
    min_bones = max_bones = 1
    rigify_type = "basic.super_copy"
    base_name = "shoulder"
    category = "Limbs"
    icon = "BONE_DATA"
    kind = "single"

    def layout(self, ctx, spec):
        out = super().layout(ctx, spec)
        out[0].roll_z = ctx.up
        return out

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        prm = pbones[layout[0].name].rigify_parameters
        set_param(prm, "make_widget", True)
        set_param(prm, "super_copy_widget_type", "shoulder")
        set_param(prm, "make_deform", True)


class _LimbModule(Module):
    limb_kind = "Arm"
    category = "Limbs"
    icon = "CON_KINEMATIC"
    settings = (
        Setting("ik_default", "IK by default", True, desc="Start in IK (off = FK), like Unreal's 'FK/IK default'"),
        Setting("ik_stretch", "IK Stretch", False,
                desc="Let the IK chain stretch/compress. Off keeps exact bone lengths (no foot sliding on game skeletons)"),
    )

    def post_rigify(self, rig, spec, layout):
        if not layout:
            return
        first = layout[0].name                       # e.g. upper_arm.L
        base, dot, side = first.partition(".")
        prop = rig.pose.bones.get(f"{base}_parent{dot}{side}")
        if prop is None:
            return
        if "IK_FK" in prop.keys():
            prop["IK_FK"] = 0.0 if spec.get("ik_default", True) else 1.0
        if "IK_Stretch" in prop.keys():
            prop["IK_Stretch"] = 1.0 if spec.get("ik_stretch", False) else 0.0
        if not spec.get("ik_stretch", False):
            # Rigify hard-codes ik_stretch=0.1 on the IK chain, which lets the
            # limb compress ~3% -> feet slide on non-stretchy game skeletons
            bases = {lb.name.partition(".")[0] for lb in layout[:2]}
            sfx = f"{dot}{side}" if dot else ""
            for pb in rig.pose.bones:
                if pb.ik_stretch > 0 and pb.name.endswith(sfx) and any(b in pb.name for b in bases):
                    pb.ik_stretch = 0.0
    bend_sign = -1.0   # elbow points backwards (-forward); knee forwards (+1)

    def bind_targets(self, spec, layout, rig_bones):
        # ORG bones carry the full FK/IK rotation including roll. Rigify's first
        # DEF segment has no twist (it lives in B-Bone segments, which a game
        # skeleton doesn't have) -> upper-arm/thigh roll would be lost.
        out = []
        for lb in layout:
            if not lb.src:
                continue
            for cand in (f"ORG-{lb.name}", f"DEF-{lb.name}", lb.name):
                if cand in rig_bones:
                    out.append((lb.src, cand))
                    break
        return out

    def collections(self, spec):
        return _limb_colls(self.limb_kind, spec.side)

    def prebend(self, ctx, heads, tails):
        """Rigify needs a bent limb to place the pole. Nudge the middle joint
        a little if the rest pose is (almost) straight or hyper-extended."""
        a, b, c = heads[0], heads[1], tails[1]
        line = c - a
        L = line.length
        if L < 1e-6:
            return
        proj = a + (b - a).project(line)
        off = b - proj
        want = ctx.forward * self.bend_sign
        want = (want - want.project(line))
        if want.length < 1e-6:
            return
        want.normalize()
        # Rigify only needs the bend *direction*. Keep the nudge tiny: the bound
        # skeleton's knee/elbow follows the rig's, so a 2% nudge put Mixamo knees
        # ~2.7 cm off once the leg bent. 0.1% is still far above float noise.
        if off.dot(want) < 0.0005 * L:
            nb = proj + want * 0.001 * L
            heads[1] = nb
            tails[0] = nb

    def configure(self, ctx, spec, layout, pbones, colls):
        cl = self.collections(spec)
        ik, fk, tw = (colls.get(c.name) for c in cl)
        first = pbones[layout[0].name]
        first.rigify_type = self.rigify_type
        for lb in layout:
            if ik:
                ik.assign(pbones[lb.name])
        prm = first.rigify_parameters
        set_param(prm, "segments", 1)
        set_param(prm, "rotation_axis", "automatic")
        set_param(prm, "ik_local_location", False)
        add_coll_ref(prm, "fk", fk)
        add_coll_ref(prm, "tweak", tw)


@register_module
class ArmModule(_LimbModule):
    id = "arm"
    short = "Arm"
    label = "Arm (upper, fore, hand)"
    description = "Upper arm, forearm, hand. IK/FK switch, pole, snapping (Rigify arm)"
    min_bones = max_bones = 3
    rigify_type = "limbs.arm"
    limb_kind = "Arm"
    bend_sign = -1.0

    def auto_resolve(self, src, socket, selection=(), used=()):
        return resolve.limb_chain(src, socket)

    def names(self, spec, count):
        s = f".{spec.side}" if spec.side else ""
        return [f"upper_arm{s}", f"forearm{s}", f"hand{s}"]

    def layout(self, ctx, spec):
        heads, tails = ctx.chain_geometry(spec)
        self.prebend(ctx, heads, tails)
        names = self.names(spec, 3)
        return [BoneLayout(n, h, t, AUTO_PARENT if i == 0 else names[i - 1], i > 0,
                           -ctx.forward, src)
                for i, (n, h, t, src) in enumerate(zip(names, heads, tails, spec.bones))]

    def fk_controls(self, spec, layout, rig_bones):
        s = f".{spec.side}" if spec.side else ""
        ctrls = [f"upper_arm_fk{s}", f"forearm_fk{s}", f"hand_fk{s}"]
        return [(lb.src, c, False) for lb, c in zip(layout, ctrls) if c in rig_bones]


@register_module
class LegModule(_LimbModule):
    id = "leg"
    short = "Leg"
    label = "Leg (thigh, shin, foot[, toe])"
    description = "Thigh, shin, foot (+toe). IK foot with roll, heel and toe pivots (Rigify leg)"
    min_bones = 3
    max_bones = 4
    rigify_type = "limbs.leg"
    limb_kind = "Leg"
    bend_sign = 1.0

    def auto_resolve(self, src, socket, selection=(), used=()):
        return resolve.limb_chain(src, socket, with_toe=True)

    def names(self, spec, count):
        s = f".{spec.side}" if spec.side else ""
        return [f"thigh{s}", f"shin{s}", f"foot{s}", f"toe{s}"]

    def layout(self, ctx, spec):
        hw = ctx.head_w
        hip, knee, ankle = (hw[b].copy() for b in spec.bones[:3])
        up = ctx.up
        if len(spec.bones) == 4:
            ball = hw[spec.bones[3]].copy()
        else:  # synthesize a ball joint in front of the ankle, near the floor
            ball = ankle + ctx.forward * ctx.unit * 0.075
            ball -= up * ((ankle - ctx.floor).dot(up) * 0.75)
        fwd = ctx.flat(ball - ankle)
        if fwd.length < 1e-6:
            fwd = ctx.forward.copy()
        fwd.normalize()
        foot_len = ctx.flat(ball - ankle).length or ctx.unit * 0.06
        if spec.tip and spec.tip in hw:
            toe_tip = hw[spec.tip].copy()
        else:
            toe_tip = ball + fwd * max(foot_len * 0.45, ctx.unit * 0.02)
        heads = [hip, knee, ankle, ball]
        tails = [knee, ankle, ball, toe_tip]
        self.prebend(ctx, heads, tails)
        names = self.names(spec, 4)
        srcs = list(spec.bones) + [None] * (4 - len(spec.bones))
        out = [BoneLayout(n, h, t, AUTO_PARENT if i == 0 else names[i - 1], i > 0,
                          ctx.forward if i < 2 else up, src)
               for i, (n, h, t, src) in enumerate(zip(names, heads, tails, srcs))]
        # heel marker: lies across the back of the foot on the floor,
        # medial -> lateral (matches the Rigify human meta-rig)
        lateral = up.cross(fwd).normalized()
        if spec.side == "R":
            lateral = -lateral
        floor_h = min((ball - ctx.floor).dot(up), (toe_tip - ctx.floor).dot(up))
        centre = ankle - up * ((ankle - ctx.floor).dot(up) - floor_h) - fwd * foot_len * 0.27
        w = foot_len * 0.35
        s = f".{spec.side}" if spec.side else ""
        out.append(BoneLayout(f"heel.02{s}", centre - lateral * w, centre + lateral * w,
                              names[2], False, up, None))
        return out

    def fk_controls(self, spec, layout, rig_bones):
        s = f".{spec.side}" if spec.side else ""
        toe = f"toe_fk{s}" if f"toe_fk{s}" in rig_bones else f"toe{s}"   # Rigify 4+: one toe control for IK and FK
        ctrls = [f"thigh_fk{s}", f"shin_fk{s}", f"foot_fk{s}", toe]
        return [(lb.src, c, False) for lb, c in zip(layout, ctrls) if lb.src and c in rig_bones]


@register_module
class FingerModule(Module):
    id = "finger"
    short = "Finger"
    label = "Finger"
    description = "Finger chain with curl master and optional IK tip (Rigify super_finger)"
    min_bones = 1
    rigify_type = "limbs.super_finger"
    finger_style = True
    base_name = "finger"
    category = "Hands"
    icon = "HAND"

    def layout(self, ctx, spec):
        out = super().layout(ctx, spec)
        back = ctx.hand_back.get(spec.side, ctx.up)
        for lb in out:
            d = (lb.tail - lb.head).normalized()
            hinge = back.cross(d)
            if hinge.length > 1e-4:
                lb.roll_z = hinge.normalized().cross(d)
        return out

    def collections(self, spec):
        return [FINGERS, FINGERS_DETAIL]

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        p = pbones[layout[0].name]
        if len(layout) < 2:
            p.rigify_type = "basic.super_copy"
            return
        prm = p.rigify_parameters
        set_param(prm, "primary_rotation_axis", "automatic")
        set_param(prm, "ik_local_location", False)
        add_coll_ref(prm, "tweak", colls.get(FINGERS_DETAIL.name))

    def post_rigify(self, rig, spec, layout):
        # Rigify aims the first finger segment with a bare Stretch To, so its
        # roll comes from the hand and the control's twist is lost (thumbs!).
        # Take the control's rotation first; Stretch To then only swings.
        if len(layout) < 2:
            return
        name = layout[0].name
        mch, ctrl = rig.pose.bones.get(f"MCH-{name}"), rig.pose.bones.get(name)
        if mch is None or ctrl is None or any(c.name == "RB_Twist" for c in mch.constraints):
            return
        st = next((i for i, c in enumerate(mch.constraints) if c.type == "STRETCH_TO"), None)
        if st is None:
            return
        c = mch.constraints.new("COPY_ROTATION")
        c.name = "RB_Twist"
        c.target, c.subtarget = rig, ctrl.name
        c.target_space = c.owner_space = "WORLD"
        mch.constraints.move(len(mch.constraints) - 1, st)


@register_module
class CopyModule(Module):
    id = "copy"
    short = "FK Bone"
    label = "Single FK bone"
    description = "Any single bone (eye, jaw, metacarpal, prop). basic.super_copy"
    min_bones = max_bones = 1
    rigify_type = "basic.super_copy"
    base_name = "bone"
    kind = "single"
    in_library = False

    def collections(self, spec):
        return [EXTRA]

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        prm = pbones[layout[0].name].rigify_parameters
        set_param(prm, "make_widget", True)
        set_param(prm, "make_deform", True)


@register_module
class MetacarpalModule(CopyModule):
    id = "palm"
    short = "Palm"
    label = "Metacarpal / Palm bone"
    description = "Metacarpal control under the hand (UE5 metacarpals)"
    base_name = "palm"
    category = "Hands"
    icon = "VIEW_PAN"
    in_library = True

    def collections(self, spec):
        return [FINGERS]


@register_module
class ChainModule(Module):
    id = "chain"
    short = "Tentacle"
    label = "FK chain (tail, ears, hair…)"
    description = "Any connected chain. limbs.simple_tentacle"
    min_bones = 1
    rigify_type = "limbs.simple_tentacle"
    base_name = "chain"
    in_library = False

    def collections(self, spec):
        return [EXTRA]

    def configure(self, ctx, spec, layout, pbones, colls):
        super().configure(ctx, spec, layout, pbones, colls)
        p = pbones[layout[0].name]
        if len(layout) < 2:
            p.rigify_type = "basic.super_copy"
            return
        add_coll_ref(p.rigify_parameters, "tweak", colls.get(EXTRA.name))


@register_module
class FootModule(Module):
    """Unreal splits Leg and Foot. Dropping Foot on a leg's foot bone folds the
    ball/toe into that leg (Rigify's leg owns the foot roll)."""
    id = "foot"
    short = "Foot"
    label = "Foot (ball / toe)"
    description = "Ball/toe bone below a Leg's foot: adds foot roll + toe control to that leg"
    min_bones = max_bones = 1
    category = "Limbs"
    icon = "CON_FLOOR"
    backend = "merge"
    kind = "single"

    def auto_resolve(self, src, socket, selection=(), used=()):
        b = src.data.bones[socket]
        # dropped on the foot itself -> use its toe child
        kids = [c for c in b.children if not resolve.is_end_bone(c)]
        if kids and not (set(resolve.core_tokens(socket)) & {"toe", "ball", "toebase"}):
            return [kids[0].name], ""
        return [socket], ""


def merge_feet(src, specs):
    """Fold Foot parts into the Leg part whose foot is the toe's parent."""
    out = []
    feet = [s for s in specs if s.module == "foot"]
    for s in specs:
        if s.module == "foot":
            continue
        if s.module == "leg" and len(s.bones) == 3:
            for f in feet:
                if not f.bones or not f.bones[0]:
                    continue
                par = src.data.bones[f.bones[0]].parent
                if par and par.name == s.bones[2]:
                    s.bones = list(s.bones) + [f.bones[0]]
                    kids = src.data.bones[f.bones[0]].children
                    if not s.tip and len(kids) == 1 and not kids[0].children:
                        s.tip = kids[0].name
                    f.bones = []
                    break
        out.append(s)
    return out


def module_items():
    return [(k, m.label, m.description) for k, m in MODULES.items()]
