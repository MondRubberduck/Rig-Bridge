"""Skeleton standards shared with Bulletproof Retarget.

The tables live in ``standards/standards.json``, generated from the
Bulletproof Retarget source by ``tools/import_bp_standards.py`` (read-only).
This module matches an armature against them with the same rules as the
retarget add-on (exact name, loose key, normalised name, shared asset prefix),
so both add-ons always agree on which standard a rig is. Rig Bridge then turns
the canonical joints (hips, spine…chest, neck, head, arms, legs, fingers) into
its modules.

Rig Bridge does not import or require the retarget add-on.
"""

import functools
import json
import os
import re
import unicodedata

from . import resolve as _resolve
from .modules import PartSpec

DATA = os.path.join(os.path.dirname(__file__), "standards", "standards.json")
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
PREFIX = "STD:"


class Standard:
    def __init__(self, d, matcher):
        self.__dict__.update(d)
        self.matcher = matcher
        self.exclude_rx = re.compile(d.get("exclude_re"), re.I) if d.get("exclude_re") else None
        self.sig_need = max(1, min(d.get("sig_need") or len(self.signature), len(self.signature) or 1))

    @property
    def pid(self):
        return PREFIX + self.id


_DB = {}


def db():
    if not _DB:
        with open(DATA, encoding="utf-8") as fh:
            raw = json.load(fh)
        m = raw["matcher"]
        m["not_asset"] = re.compile(m["not_asset_re"], re.I)
        m["body_word"] = re.compile(m["body_word_re"], re.I)
        _DB["matcher"] = m
        _DB["source"] = raw.get("source", {})
        _DB["list"] = [Standard(d, m) for d in raw["standards"]]
        _DB["by_id"] = {s.id: s for s in _DB["list"]}
    return _DB


def standards():
    return db()["list"]


def get(pid):
    return db()["by_id"].get(pid[len(PREFIX):] if pid.startswith(PREFIX) else pid)


# ---------------------------------------------------------------------------
# name keys (same rules as the retarget add-on)
# ---------------------------------------------------------------------------

_PREFIX_RE = re.compile(r"^.*:")
_MIXAMO_RE = re.compile(r"^mixamorig\d*[_:]?", re.I)
_DUP_RE = re.compile(r"\.\d{3}$")


def normalize(name):
    n = _PREFIX_RE.sub("", name)
    n = _MIXAMO_RE.sub("", n)
    n = _DUP_RE.sub("", n)
    return re.sub(r"[\W_]", "", n.lower())


@functools.lru_cache(maxsize=4096)
def _key(name):
    n = unicodedata.normalize("NFKC", name)
    n = re.sub(r"^.*:", "", n)
    n = re.sub(r"^mixamorig\d*_?", "", n, flags=re.I)
    return re.sub(r"[\W_]+", "", n.casefold())


def _seg_ok(seg):
    m = db()["matcher"]
    return not m["not_asset"].match(seg.strip(m["asset_seps"]))


def _rest_ok(names, prefix):
    m = db()["matcher"]
    rest = [n[len(prefix):] for n in names if n.startswith(prefix)]
    return sum(1 for r in rest if m["body_word"].search(r)) >= 5


@functools.lru_cache(maxsize=32)
def asset_prefix(names):
    """Prefix shared by the whole rig ('Skeleton_', 'Actor1:'), or ''."""
    m = db()["matcher"]
    seps = m["asset_seps"]
    if len(names) < m["asset_min_bones"]:
        return ""
    counts = {}
    for name in names:
        for i in range(1, len(name)):
            if name[i] in seps:
                p = name[:i + 1]
                counts[p] = counts.get(p, 0) + 1
    present = set(names)
    need = len(names) - max(2, len(names) // 20)
    fits = [p for p, c in counts.items()
            if c + (p.rstrip(seps) in present) >= need and len(p.rstrip(seps)) >= 2]
    best = ""
    for p in sorted(fits, key=len):
        if not _seg_ok(p[len(best):]):
            break
        best = p
    if best:
        return best if _rest_ok(names, best) else ""
    lcp = os.path.commonprefix(list(names))
    if len(lcp) < 2 or not _seg_ok(lcp) or not (lcp[-1].islower() or lcp[-1].isdigit()):
        return ""
    if all(n == lcp or n[len(lcp)].isupper() or (lcp[-1].isdigit() and n[len(lcp)].isalpha()) for n in names):
        return lcp if _rest_ok(names, lcp) else ""
    return ""


def _strip(name, prefix):
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def _asset_bone(names, prefix):
    if not prefix:
        return None
    stem = prefix.rstrip(db()["matcher"]["asset_seps"])
    present = set(names)
    return next((n for n in (prefix + stem, stem) if n in present), None)


@functools.lru_cache(maxsize=8)
def _index(names):
    by_key, by_norm = {}, {}
    for n in names:
        by_key.setdefault(_key(n), []).append(n)
        by_norm.setdefault(normalize(n), []).append(n)
    prefix = asset_prefix(names)
    if prefix:
        for n in names:
            s = _strip(n, prefix)
            if s != n:
                if n not in by_key.setdefault(_key(s), []):
                    by_key[_key(s)].append(n)
                if n not in by_norm.setdefault(normalize(s), []):
                    by_norm[normalize(s)].append(n)
    return frozenset(names), by_key, by_norm


# ---------------------------------------------------------------------------
# resolve + score
# ---------------------------------------------------------------------------

def _resolve_base(std, names):
    exact = set(names)
    loose = {}
    for n in names:
        loose.setdefault(normalize(n), n)
    prefix = asset_prefix(names)
    stripped = {}
    if prefix:
        for n in names:
            s = _strip(n, prefix)
            if s != n:
                stripped.setdefault(s, n)
                loose.setdefault(normalize(s), n)
    out, used = {}, set()
    for jid, cands in std.bones.items():
        hit = next((c for c in cands if c in exact and c not in used), None)
        if hit is None:
            hit = next((stripped[c] for c in cands if c in stripped and stripped[c] not in used), None)
        if hit is None:
            hit = next((loose[normalize(c)] for c in cands
                        if normalize(c) in loose and loose[normalize(c)] not in used), None)
        if hit is not None:
            out[jid] = hit
            used.add(hit)
    return out


def _resolve_std(std, names):
    exact, by_key, by_norm = _index(names)
    out, used = {}, set()

    def free(lst):
        return next((n for n in lst or () if n not in used), None)

    for jid, cands in std.bones.items():
        hit = next((c for c in cands if c in exact and c not in used), None)
        if hit is None:
            for c in cands:
                hit = free(by_key.get(_key(c)))
                if hit:
                    break
        if hit is None:
            for c in cands:
                if not c.isascii() or _DUP_RE.search(c):
                    continue
                k = normalize(c)
                if len(k) >= 3:
                    hit = free(by_norm.get(k))
                    if hit:
                        break
        if hit is None and jid == std.asset_joint:
            hit = _asset_bone(names, asset_prefix(names))
            if hit in used:
                hit = None
        if hit is not None:
            out[jid] = hit
            used.add(hit)
    return out


def resolve(std, names):
    names = tuple(names)
    return _resolve_std(std, names) if std.kind == "standard" else _resolve_base(std, names)


def score(std, names):
    names = tuple(names)
    m = db()["matcher"]
    res = resolve(std, names)
    core = sum(1 for j in m["core_joints"] if j in res) / len(m["core_joints"])
    if std.kind != "standard":
        if std.signature:
            have = set(names)
            prefix = asset_prefix(names)
            if prefix:
                have |= {_strip(n, prefix) for n in names}
            sig = sum(1 for s in std.signature if s in have) / len(std.signature)
            core = core * 0.85 + sig * 0.15
        if "chest" in std.bones and "chest" not in res:
            core *= 0.98
        return core, res
    exact, by_key, _ = _index(names)
    keys = by_key.keys()
    has = lambda x: x in exact or _key(x) in keys  # noqa: E731
    s = core
    if std.signature:
        sig = min(1.0, sum(1 for x in std.signature if has(x)) / std.sig_need)
        s = core * (0.85 + 0.15 * sig)
    if (any(has(x) for x in std.exclude)
            or (std.exclude_rx is not None and any(std.exclude_rx.search(n) for n in names))
            or (std.require and not any(has(x) for x in std.require))
            or (std.asset_joint and std.asset_joint not in res)):
        s *= m["exclude_factor"]
    return round(max(0.0, s - std.bias), 6), res


def detect(names):
    """(Standard | None, score, resolved joints) – best match above the threshold."""
    names = tuple(names)
    best = (None, 0.0, {})
    for std in standards():
        s, res = score(std, names)
        if s > best[1] or (s == best[1] and best[0] is not None and len(res) > len(best[2])):
            best = (std, s, res)
    if best[1] < db()["matcher"]["threshold"]:
        return None, best[1], best[2]
    return best


# ---------------------------------------------------------------------------
# canonical joints -> Rig Bridge modules
# ---------------------------------------------------------------------------

def _path(top, bottom):
    """Bones from ancestor `top` down to `bottom` (inclusive), or None."""
    out, b = [], bottom
    while b is not None:
        out.append(b)
        if b == top:
            return out[::-1]
        b = b.parent
    return None


def _desc(a, b):
    return a is not None and b is not None and _path(a, b) is not None


def _socket(bone, spine, default):
    """The spine/neck bone a limb really hangs from (3ds Max Biped thighs hang
    from Bip01 Spine and clavicles from Bip01 Neck, OptiTrack shoulders from
    Chest), so the control rig parents it the same way and it follows exactly."""
    names = {b.name for b in spine}
    p = bone.parent
    while p is not None:
        if p.name in names:
            return p.name
        p = p.parent
    return default.name


def humanoid_parts(obj, res):
    """PartSpecs for a resolved humanoid. Returns (parts, notes)."""
    B = obj.data.bones
    get = lambda j: B.get(res[j]) if res.get(j) else None  # noqa: E731
    notes, parts = [], []
    hips = get("hips")
    if hips is None:
        return [], ["no hips"]
    torso = [get(j) for j in ("spine", "spine1", "spine2", "chest")]
    chest = next((b for b in reversed(torso) if b is not None and _desc(hips, b)), hips)

    root = get("root")
    if root is not None and root != hips and _desc(root, hips):
        parts.append(PartSpec("root", "root", "", [root.name]))
    else:
        top = hips
        while top.parent is not None:
            top = top.parent
        if top != hips:
            parts.append(PartSpec("root", "root", "", [top.name]))

    # in-between bones the table skips (UE5 spine_04, MMD 腰) belong to the spine
    spine = _path(hips, chest) or [hips]
    parts.append(PartSpec("spine", "spine", "", [b.name for b in spine]))

    head = get("head")
    necks = []
    if head is not None:
        neck = (_path(chest, head) or [chest, head])[1:]
        necks = neck[:-1]
        tip = res.get("head_end", "")
        parts.append(PartSpec("neck_head", "head", "", [b.name for b in neck], tip if tip in B else ""))

    for s, S in (("l", "L"), ("r", "R")):
        sh = get(f"shoulder_{s}")
        arm = [get(f"{j}_{s}") for j in ("upper_arm", "forearm", "hand")]
        if sh is not None and (arm[0] is None or _desc(sh, arm[0])):
            parts.append(PartSpec("shoulder", "shoulder", S, [sh.name], "", {}, _socket(sh, spine + necks, chest)))
        if all(arm) and _desc(arm[0], arm[1]) and _desc(arm[1], arm[2]):
            parts.append(PartSpec("arm", "arm", S, [b.name for b in arm],
                                  "", {}, "" if sh is not None else _socket(arm[0], spine + necks, chest)))
        elif any(arm):
            notes.append(f"arm.{S}: incomplete")
        leg = [get(f"{j}_{s}") for j in ("thigh", "shin", "foot")]
        if all(leg) and _desc(leg[0], leg[1]) and _desc(leg[1], leg[2]):
            toe = get(f"toe_{s}")
            bones = [b.name for b in leg] + ([toe.name] if toe is not None and _desc(leg[2], toe) else [])
            tip = res.get(f"toe_end_{s}", "") if len(bones) == 4 else ""
            parts.append(PartSpec("leg", "leg", S, bones, tip if tip in B else "", {}, _socket(B[bones[0]], spine, hips)))
        elif any(leg):
            notes.append(f"leg.{S}: incomplete")
        hand = arm[2]
        if hand is None:
            continue
        starts = {}
        for f in FINGERS:
            js = [get(f"{f}_{i:02d}_{s}") for i in (1, 2, 3)]
            js = [b for b in js if b is not None]
            if js and all(_desc(js[i], js[i + 1]) for i in range(len(js) - 1)) and _desc(hand, js[0]):
                starts[f] = js
        meta_count = {}
        for js in starts.values():
            p = js[0].parent
            if p is not None and p != hand:
                meta_count[p.name] = meta_count.get(p.name, 0) + 1
        for f, js in starts.items():
            p = js[0].parent
            if p is not None and p != hand and p.parent == hand and meta_count.get(p.name) == 1:
                parts.append(PartSpec("palm", f"palm_{f}", S, [p.name]))
            tip = res.get(f"{f}_04_{s}", "")
            parts.append(PartSpec("finger", "thumb" if f == "thumb" else f"f_{f}", S,
                                  [b.name for b in js], tip if tip in B else ""))
    return parts, notes
