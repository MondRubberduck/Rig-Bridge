"""Skeleton detection.

Strategies, in order, all feeding the same editable part list:

1. **Your saved profiles** (JSON, exact names).
2. **Standards** shared with Bulletproof Retarget (stdmatch.py): Mixamo, UE4/
   UE5/UEFN/MetaHuman, CC/ActorCore/AccuRIG, Daz G3/8/9, VRoid, VRM/Unity,
   Rigify names, ARP export, HumanIK, OptiTrack, CMU, 3ds Max Biped, VRChat,
   MMD, RenderPeople. Control rigs (P2M, generated Rigify, Auto-Rig Pro) are
   recognised and left alone.
3. **Built-in profiles** (Mixamo, Unreal).
4. **Topology** (fallback): head / hands / feet by name hints, the rest from
   the bone hierarchy.
"""

import glob
import json
import os
import re

import bpy

from . import stdmatch
from .modules import MODULES, PartSpec
from .naming import (core_tokens, finger_of, is_helper, norm, side_of,
                     strip_namespace)

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "profiles")
PROFILES: dict[str, dict] = {}
TOPOLOGY = "TOPOLOGY"


# ---------------------------------------------------------------------------
# Profile files
# ---------------------------------------------------------------------------

def user_dir(sub: str) -> str:
    pkg = __package__ or "rig_bridge"
    try:
        return bpy.utils.extension_path_user(pkg, path=sub, create=True)
    except Exception:
        return bpy.utils.user_resource("SCRIPTS", path=os.path.join("rig_bridge", sub), create=True)


def load_profiles():
    PROFILES.clear()
    raw = {}
    for d, is_user in ((BUILTIN_DIR, False), (user_dir("profiles"), True)):
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                data["_path"], data["_user"] = path, is_user
                raw[data["id"]] = data
            except Exception as exc:  # keep going, report in console
                print(f"[Rig Bridge] could not load profile {path}: {exc}")

    def resolve(pid, seen=()):
        p = dict(raw[pid])
        base = p.get("extends")
        if base and base in raw and base not in seen:
            parent = resolve(base, seen + (pid,))
            key = lambda part: (part["module"], part.get("name", ""))
            mine = {key(x): x for x in p.get("parts", [])}
            merged = [mine.pop(key(x), x) for x in parent.get("parts", [])]
            p["parts"] = merged + list(mine.values())
            p.setdefault("sides", parent.get("sides", {}))
            p.setdefault("strip_prefix", parent.get("strip_prefix", ""))
        return p

    for pid in raw:
        PROFILES[pid] = resolve(pid)


def _index(obj, strip_prefix=""):
    idx = {}
    rx = re.compile(strip_prefix, re.I) if strip_prefix else None
    for b in obj.data.bones:
        k = norm(b.name)
        idx.setdefault(k, b.name)
        if rx:
            idx.setdefault(rx.sub("", k), b.name)
    return idx


def match_profile(obj, prof):
    """Return (parts, score 0..1, matched_required, missing_names)."""
    idx = _index(obj, prof.get("strip_prefix", ""))
    rx = re.compile(prof["strip_prefix"], re.I) if prof.get("strip_prefix") else None

    def look(name):
        k = name.lower()
        hit = idx.get(k)
        if not hit and rx:
            hit = idx.get(rx.sub("", k))
        return hit

    sides = prof.get("sides", {"L": "L", "R": "R"})
    parts, missing = [], []
    req_total = req_found = 0
    for p in prof.get("parts", []):
        if p.get("mirror"):
            variants = [("L", sides.get("L", "L")), ("R", sides.get("R", "R"))]
        else:
            variants = [(p.get("side_fixed", ""), "")]
        for side, token in variants:
            bones, ok = [], True
            local_req = local_found = 0
            for raw_name in p["bones"]:
                if not raw_name:              # empty optional slot (e.g. piston sockets)
                    bones.append("")
                    continue
                optional = raw_name.startswith("?")
                name = raw_name.lstrip("?").replace("{side}", token)
                hit = look(name)
                if not optional:
                    local_req += 1
                    if hit:
                        local_found += 1
                    else:
                        ok = False
                        missing.append(name)
                if hit:
                    bones.append(hit)
            if not p.get("optional"):
                req_total += local_req
                req_found += local_found
            if ok and any(bones):
                tip = look(p["tip"].replace("{side}", token)) if p.get("tip") else ""
                par = look(p["parent"].replace("{side}", token)) if p.get("parent") else ""
                parts.append(PartSpec(p["module"], p.get("name", ""), side, bones, tip or "",
                                      dict(p.get("settings", {})), par or ""))
    score = req_found / req_total if req_total else 0.0
    return parts, score, req_found, missing


# ---------------------------------------------------------------------------
# Topology detection
# ---------------------------------------------------------------------------

def _ancestors(b):
    out = []
    while b:
        out.append(b)
        b = b.parent
    return out


def _lca(bones):
    bones = [b for b in bones if b]
    if not bones:
        return None
    common = _ancestors(bones[0])
    for b in bones[1:]:
        s = set(x.name for x in _ancestors(b))
        common = [x for x in common if x.name in s]
    return common[0] if common else None


def _path(a, b):
    """Bones from ancestor a down to b (inclusive) or None."""
    out = []
    while b:
        out.append(b)
        if b == a:
            return list(reversed(out))
        b = b.parent
    return None


def _pick(bones, pred, side):
    c = [b for b in bones if not is_helper(b.name) and side_of(b.name) == side and pred(b)]
    if not c:
        return None
    return max(c, key=lambda b: (len(b.children_recursive), -len(b.parent_recursive)))


def _has(b, *words):
    return any(t.startswith(w) for t in core_tokens(b.name) for w in words)


def _drop_twist(chain, keep_min):
    out = [b for b in chain if not _has(b, "twist", "roll")]
    return out if len(out) >= keep_min else chain


def detect_topology(obj):
    bones = obj.data.bones
    report = []
    is_head = lambda b: "head" in core_tokens(b.name) and finger_of(b.name)[0] is None
    is_hand = lambda b: (_has(b, "hand", "wrist")) and finger_of(b.name)[0] is None
    is_foot = lambda b: _has(b, "foot", "ankle")

    head = _pick(bones, is_head, "")
    hands = {s: _pick(bones, is_hand, s) for s in "LR"}
    feet = {s: _pick(bones, is_foot, s) for s in "LR"}

    if not head or not (any(hands.values()) or any(feet.values())):
        return [], "Topology: no humanoid (head + hands/feet) recognised"

    chest = _lca([head, hands["L"], hands["R"]]) if (hands["L"] or hands["R"]) else None
    if chest is None or chest == head:
        chest = head.parent if head and head.parent else head
    root_spine = _lca([feet["L"], feet["R"], chest]) or chest
    parts = []

    # root above the spine (UE 'root', CC 'BoneRoot', Biped 'Bip01', ...)
    top = _ancestors(root_spine)[-1]
    if top != root_spine:
        parts.append(PartSpec("root", "root", "", [top.name]))

    spine = _path(root_spine, chest) or [root_spine]
    parts.append(PartSpec("spine", "spine", "", [b.name for b in spine]))

    if head and chest != head:
        neck = (_path(chest, head) or [head])[1:]
        tip = ""
        for c in head.children:
            if not c.children and _has(c, "top", "end", "nub", "tip"):
                tip = c.name
        if neck:
            parts.append(PartSpec("neck_head", "head", "", [b.name for b in neck], tip))
        for c in head.children_recursive:
            s = side_of(c.name)
            ct = core_tokens(c.name)
            if s and ("eye" in ct or "eyeball" in ct):
                parts.append(PartSpec("copy", "eye", s, [c.name]))
            elif not s and "jaw" in ct and len(c.parent_recursive) - len(head.parent_recursive) <= 2:
                parts.append(PartSpec("copy", "jaw", "", [c.name]))

    for s in "LR":
        hand = hands[s]
        if hand:
            p = _path(chest, hand)
            p = _drop_twist(p[1:], 3) if p else []
            if len(p) >= 4:
                parts.append(PartSpec("shoulder", "shoulder", s, [p[-4].name]))
            if len(p) >= 3:
                parts.append(PartSpec("arm", "arm", s, [b.name for b in p[-3:]]))
            else:
                report.append(f"arm.{s}: chain too short")
            parts += _fingers(hand, s)
        foot = feet[s]
        if foot:
            p = _path(root_spine, foot)
            p = _drop_twist(p[1:], 3) if p else []
            if len(p) >= 3:
                leg = [b.name for b in p[-3:]]
                toe = None
                for c in foot.children_recursive:
                    if len(c.parent_recursive) - len(foot.parent_recursive) <= 2 \
                            and _has(c, "toe", "ball") and not is_helper(c.name):
                        toe = c
                        break
                if not toe:
                    kids = [c for c in foot.children if not is_helper(c.name)]
                    toe = kids[0] if len(kids) == 1 else None
                tip = ""
                if toe:
                    leg.append(toe.name)
                    if len(toe.children) == 1 and not toe.children[0].children:
                        tip = toe.children[0].name
                parts.append(PartSpec("leg", "leg", s, leg, tip))
            else:
                report.append(f"leg.{s}: chain too short")

    found = ", ".join(sorted({f"{p.module}" for p in parts}))
    return parts, f"Topology: {len(parts)} parts ({found})" + (f"; {'; '.join(report)}" if report else "")


def _fingers(hand, side):
    groups = {}
    base_depth = len(hand.parent_recursive)
    for b in hand.children_recursive:
        if len(b.parent_recursive) - base_depth > 5:
            continue
        canon, _ = finger_of(b.name)
        if canon and not is_helper(b.name) and side_of(b.name) in (side, ""):
            groups.setdefault(canon, []).append(b)
    parts = []
    for canon in ("thumb", "f_index", "f_middle", "f_ring", "f_pinky"):
        g = groups.get(canon)
        if not g:
            continue
        g.sort(key=lambda b: len(b.parent_recursive))
        meta = [b for b in g if _has(b, "metacarpal", "carpal", "palm", "meta")]
        rest = [b for b in g if b not in meta]
        if meta:
            parts.append(PartSpec("palm", "palm_" + canon.replace("f_", ""), side, [meta[0].name]))
        if not rest:
            continue
        chain = [rest[0]]
        names = {b.name for b in rest}
        while True:
            nxt = [c for c in chain[-1].children if c.name in names]
            if not nxt:
                break
            chain.append(nxt[0])
        tip = ""
        if len(chain) >= 4 and not chain[-1].children:
            tip = chain.pop().name
        elif len(chain[-1].children) == 1 and not chain[-1].children[0].children:
            tip = chain[-1].children[0].name
        parts.append(PartSpec("finger", canon, side, [b.name for b in chain], tip))
    return parts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class ControlRigError(Exception):
    pass


def detect_standard(obj, std, score=None):
    names = [b.name for b in obj.data.bones]
    if score is None:
        score, res = stdmatch.score(std, names)
    else:
        res = stdmatch.resolve(std, names)
    if std.control_rig:
        return [], f"'{obj.name}' is already a control rig ({std.name}) – animate it directly"
    parts, notes = stdmatch.humanoid_parts(obj, res)
    msg = f"Detected {std.name} ({score:.0%} match, {len(res)} joints)"
    if notes:
        msg += f"; {'; '.join(notes)}"
    return parts, msg


def detect(obj, profile_id="AUTO"):
    """Return (parts, profile_id_used, message)."""
    if not PROFILES:
        load_profiles()
    if profile_id.startswith(stdmatch.PREFIX):
        std = stdmatch.get(profile_id)
        if std is not None:
            parts, msg = detect_standard(obj, std)
            return parts, profile_id, msg
    if profile_id not in ("AUTO", TOPOLOGY) and profile_id in PROFILES:
        parts, score, n, missing = match_profile(obj, PROFILES[profile_id])
        msg = f"{PROFILES[profile_id]['label']}: {score:.0%} of required bones"
        if missing:
            msg += f" (missing: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''})"
        return parts, profile_id, msg
    if profile_id == "AUTO":
        def best_of(user):
            best = None
            for pid, prof in PROFILES.items():
                if bool(prof.get("_user")) != user:
                    continue
                parts, score, n, missing = match_profile(obj, prof)
                if best is None or (score, n) > (best[2], best[3]):
                    best = (pid, parts, score, n)
            return best if best and best[2] >= 0.85 else None

        b = best_of(True)
        if b:
            return b[1], b[0], f"Detected {PROFILES[b[0]]['label']} ({b[2]:.0%} match)"
        std, score, _ = stdmatch.detect([x.name for x in obj.data.bones])
        if std is not None:
            parts, msg = detect_standard(obj, std, score)
            if parts or std.control_rig:
                return parts, std.pid, msg
        b = best_of(False)
        if b:
            return b[1], b[0], f"Detected {PROFILES[b[0]]['label']} ({b[2]:.0%} match)"
    parts, msg = detect_topology(obj)
    return parts, TOPOLOGY, msg


def parts_to_profile(parts, pid, label, obj=None):
    """Serialise a part list to profile JSON (exact, namespace-free names)."""
    out = {"id": pid, "label": label, "description": f"Saved from '{obj.name if obj else ''}'",
           "sides": {"L": "L", "R": "R"}, "parts": []}
    for p in parts:
        d = {"module": p.module, "name": p.name,
             "bones": [strip_namespace(b) if b else "" for b in p.bones]}
        if p.settings:
            d["settings"] = {k: v for k, v in p.settings.items()}
        if p.parent:
            d["parent"] = strip_namespace(p.parent)
        if p.side:
            d["side_fixed"] = p.side
        if p.tip:
            d["tip"] = strip_namespace(p.tip)
        out["parts"].append(d)
    return out


def module_ok(spec: PartSpec) -> str:
    m = MODULES.get(spec.module)
    if not m:
        return f"unknown module '{spec.module}'"
    return m.validate(spec)
