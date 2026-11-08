"""Modular-rig editing operations (Unreal Modular Control Rig semantics).

* **Sockets**: places where a module can be dropped. A socket is any bone not
  yet driven by a module whose parent is driven (or that is a root bone).
* **Suggestions**: for every socket, the module Auto Rig would put there.
* **place()**: dropping a module on one or more sockets runs the module's
  connector event (auto-resolve). Array modules (Cape, Aim) take all selected
  sockets as one module. Other modules make one module per socket, like
  Unreal's multi-socket finger drop.
"""

import re

from . import detect, stdmatch, resolve
from .modules import MODULES, PartSpec
from .naming import core_tokens, is_helper, side_of, strip_namespace

HINTS = [
    # (tokens, module) – first match wins
    ({"rod", "shaft", "plunger"}, None),          # rods are found by their cylinder
    ({"cylinder", "cyl", "piston", "housing", "barrel", "shock", "damper"}, "piston"),
    ({"cape", "cloak", "skirt", "robe", "flag", "banner", "coat", "tabard"}, "cape"),
    ({"hair", "ponytail", "pony", "braid", "strand", "bang", "bangs", "fringe", "earring",
      "pendant", "dangle", "jiggle", "physics", "dyn", "dynamic", "tassel", "rope"}, "dynamic_chain"),
    ({"tail"}, "tail"),
    ({"tentacle"}, "ik_chain"),
    ({"jaw", "jawroot", "mandible"}, "control"),   # jaw on its own; the tongue gets its own chain
    ({"eye", "eyeball"}, "aim"),
    ({"thigh", "upleg", "upperleg"}, "leg"),
    ({"upperarm"}, "arm"),
    ({"clavicle", "shoulder"}, "shoulder"),
    ({"pelvis", "hips", "hip"}, "spine"),
    ({"neck"}, "neck_head"),
    ({"root"}, "root"),
]
DIGITS = ("toe", "thumb", "index", "middle", "ring", "pinky", "finger")   # leaf digits keep controls
BODY_MODULES = ("root", "spine", "shoulder", "arm", "leg", "palm", "finger", "foot")
PRIORITY = ["root", "spine", "piston", "cape", "neck_head", "shoulder", "arm", "leg",
            "dynamic_chain", "tail", "ik_chain", "aim", "fk_chain", "control"]


def assigned(src, specs):
    out = set()
    for s in specs:
        mod = MODULES.get(s.module)
        out |= set(mod.expand(src, s) if mod else [b for b in s.bones if b])
        if s.tip:
            out.add(s.tip)
    return out


def is_duplicate_leaf(b):
    """Leaf bone repeating its parent's name (CC 'CC_Base_Pelvis' under 'pelvis'):
    an exporter helper, not something to animate."""
    if b.children or b.parent is None:
        return False
    t = set(core_tokens(b.name))
    return bool(t) and t == set(core_tokens(b.parent.name))


def sockets(src, specs, helpers=False):
    used = assigned(src, specs)
    out = []

    def passthrough(b):
        # an unused bone with used descendants (CC_Base_Pelvis, Daz carpals,
        # CC_Base_FacialBone) just follows its parent – and is see-through for
        # its other children, so the jaw under FacialBone still gets a socket
        return not helpers and b.name not in used and any(c.name in used for c in b.children_recursive)

    def anchored(p):
        while p is not None and passthrough(p):
            p = p.parent
        return p is None or p.name in used

    for b in src.data.bones:
        if b.name in used:
            continue
        if not helpers and (is_helper(b.name) or resolve.is_end_bone(b) or is_duplicate_leaf(b)):
            continue
        if passthrough(b):
            continue
        p = b.parent
        if p is None or anchored(p):
            out.append(b.name)
        elif not helpers and is_helper(p.name):
            # skip over helper parents (e.g. UE ik_hand_root) – not a socket
            continue
    return out


def name_hint(src, bone):
    toks = set(core_tokens(bone))
    for keys, mid in HINTS:
        if toks & keys:
            if mid == "leg" and "upper" in toks and "arm" in toks:
                return "arm"
            return mid
    if "arm" in toks and "fore" not in toks and "lower" not in toks:
        chain = resolve.follow_heavy(src, bone, 3)
        if len(chain) == 3 and any(t in core_tokens(chain[-1]) for t in ("hand", "wrist")):
            return "arm"
    if "head" in toks:
        return "control"
    return None


def fallback(src, bone):
    if src.data.bones[bone].parent is None and "root" in core_tokens(bone):
        return "root"
    chain = resolve.follow_chain(src, bone)
    chain, _ = resolve.split_tip(src, chain)
    return "fk_chain" if len(chain) >= 2 else "control"


def suggest(src, bone):
    return name_hint(src, bone) or fallback(src, bone)


def _base_name(mid, bones):
    mod = MODULES[mid]
    if mid in ("arm", "leg", "spine", "shoulder", "root", "foot"):
        return {"arm": "arm", "leg": "leg", "spine": "spine", "shoulder": "shoulder",
                "root": "root", "foot": "foot"}[mid]
    if mid == "neck_head":
        return "head"
    b = next((x for x in bones if x), "")
    toks = [t for t in core_tokens(b) if not t.isdigit()]
    toks = [re.sub(r"\d+$", "", t) for t in toks if re.sub(r"\d+$", "", t)]
    if mid == "piston":
        toks = [t for t in toks if t not in ("cylinder", "cyl", "rod", "housing", "barrel")] or ["piston"]
    stem = "_".join(toks[:2]) if toks else (mod.base_name or mid)
    return stem or mod.base_name or mid


def unique_name(specs, base, side, skip=None):
    taken = {(s.name, s.side) for s in specs if s is not skip}
    n, k = base, 2
    while (n, side) in taken:
        n = f"{base}_{k}"
        k += 1
    return n


def default_settings(mid):
    return {st.key: st.default for st in MODULES[mid].settings}


def make_spec(src, specs, mid, bones, tip=""):
    first = next((b for b in bones if b), "")
    side = side_of(first) if first else ""
    if MODULES[mid].kind == "array" and len([b for b in bones if b]) > 1:
        sides = {side_of(b) for b in bones if b}
        side = sides.pop() if len(sides) == 1 else ""
    name = unique_name(specs, _base_name(mid, bones), side)
    if mid == "finger":
        from .naming import finger_of
        canon, _ = finger_of(first)
        if canon:
            name = unique_name(specs, canon, side)
    return PartSpec(mid, name, side, list(bones), tip, default_settings(mid))


def place(src, specs, mid, socket_bones):
    """Drop module `mid` on the given sockets. Returns the new PartSpecs."""
    mod = MODULES[mid]
    used = assigned(src, specs)
    new = []
    socket_bones = [b for b in socket_bones if b]
    if not socket_bones:
        return new
    grouped = mod.kind == "array" or (mid == "piston" and len(socket_bones) == 2)
    if grouped:
        bones, tip = mod.auto_resolve(src, socket_bones[0], selection=socket_bones, used=used)
        new.append(make_spec(src, specs + new, mid, bones, tip))
    else:
        for s in socket_bones:
            bones, tip = mod.auto_resolve(src, s, selection=(), used=used)
            bones = [b for b in bones if b not in used] if mod.kind == "chain" else bones
            if not any(bones):
                continue
            sp = make_spec(src, specs + new, mid, bones, tip)
            new.append(sp)
            used |= set(mod.expand(src, sp))
    return new


LAST_DETECT = {}


def auto_rig(src, specs, profile="AUTO"):
    """Fill every empty socket: profile/topology detection for the humanoid,
    then name hints + fallbacks for everything else (tails, hair, capes,
    pistons, props). Existing modules are kept."""
    specs = list(specs)
    parts, used_pid, msg = detect.detect(src, profile)
    LAST_DETECT["pid"] = used_pid
    std = stdmatch.get(used_pid) if used_pid.startswith(stdmatch.PREFIX) else None
    if std is not None and std.control_rig:
        return specs, msg          # never stack a second control rig on P2M / Rigify / ARP
    used = assigned(src, specs)
    for p in parts:
        mod = MODULES.get(p.module)
        if mod is None:
            continue
        p.settings = default_settings(p.module) | p.settings
        bs = set(mod.expand(src, p))
        if bs & used:
            continue
        specs.append(p)
        used |= bs
    # pistons first: their rods hang elsewhere in the hierarchy
    for b in src.data.bones:
        if b.name not in used and name_hint(src, b.name) == "piston":
            new = place(src, specs, "piston", [b.name])
            if new and new[0].bones[1]:
                specs += new
                used = assigned(src, specs)
    body = set()
    for p in specs:
        if p.module in BODY_MODULES:
            mod = MODULES.get(p.module)
            body |= set(mod.expand(src, p) if mod else p.bones)
    skipped = set()
    for _ in range(len(src.data.bones)):
        socks = []
        for b in sockets(src, specs):
            bb = src.data.bones[b]
            # corrective / helper leaves on the body (UE clavicle_pec, wrist_inner,
            # center_of_mass…) follow their parent instead of getting controls
            if not bb.children and bb.parent is not None and bb.parent.name in body \
                    and suggest(src, b) in BODY_MODULES + ("control", "fk_chain") \
                    and not any(w in strip_namespace(b).lower() for w in DIGITS):
                skipped.add(b)
                continue
            socks.append(b)
        if not socks:
            break
        ranked = sorted(socks, key=lambda b: (PRIORITY.index(suggest(src, b)) if suggest(src, b) in PRIORITY else 99,
                                              len(src.data.bones[b].parent_recursive)))
        b = ranked[0]
        mid = suggest(src, b)
        new = place(src, specs, mid, [b])
        if not new:
            new = place(src, specs, "control", [b])
        if not new:
            break
        specs += new
    added = len(specs)
    return specs, f"{msg} → {added} modules"


def plan(src, specs):
    """{socket bone: suggested module id} for the viewport schematic."""
    socks = sockets(src, specs)
    if not socks:
        return {}
    full, _ = auto_rig(src, specs)
    first = {}
    for s in full[len(specs):]:
        b = next((x for x in s.bones if x), None)
        if b:
            first[b] = s.module
    out = {}
    for b in socks:
        out[b] = first.get(b) or suggest(src, b)
    return out


def mirror_spec(src, specs, spec):
    """Mirror a module to the other side. Returns (PartSpec | None, message)."""
    flipped = []
    for b in spec.bones:
        if not b:
            flipped.append("")
            continue
        f = resolve.flip_name(src, b)
        if not f:
            return None, f"No mirror bone for '{b}'"
        flipped.append(f)
    tip = resolve.flip_name(src, spec.tip) if spec.tip else ""
    side = {"L": "R", "R": "L"}.get(spec.side, spec.side)
    if side == spec.side:
        side = side_of(flipped[0]) if flipped and flipped[0] else spec.side
    used = assigned(src, specs)
    clash = [b for b in flipped if b and b in used]
    if clash:
        return None, f"'{clash[0]}' already has a module"
    par = resolve.flip_name(src, spec.parent) if spec.parent else ""
    new = PartSpec(spec.module, unique_name(specs, spec.name, side), side, flipped, tip,
                   dict(spec.settings), par or spec.parent)
    return new, f"Mirrored to {new.name}.{side}"
