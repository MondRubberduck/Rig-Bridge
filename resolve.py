"""Connector events: how a module auto-resolves its bones from one socket.

Equivalent of Unreal's *Connector Event* — when a module is dropped onto a
socket (a bone), these functions find the rest of the bones it needs.
"""

from .naming import core_tokens, is_helper, side_of, strip_namespace

END_TOKENS = {"end", "tip", "nub", "top", "endsite", "leaf"}


def _bones(src):
    return src.data.bones


def kids(src, name, helpers=False):
    b = _bones(src)[name]
    return [c for c in b.children if helpers or not is_helper(c.name)]


def subtree_size(src, name):
    return len(_bones(src)[name].children_recursive) + 1


def is_end_bone(b):
    """Leaf marker bones such as HeadTop_End, Toe_End, hair_end."""
    if b.children:
        return False
    toks = set(core_tokens(b.name))
    return bool(toks & END_TOKENS) or strip_namespace(b.name).lower().endswith(("_end", "end"))


def split_tip(src, chain):
    """Move a trailing end-site bone out of the chain -> (chain, tip)."""
    bones = _bones(src)
    if len(chain) >= 2 and is_end_bone(bones[chain[-1]]):
        return chain[:-1], chain[-1]
    last = bones[chain[-1]]
    if len(last.children) == 1 and is_end_bone(last.children[0]):
        return chain, last.children[0].name
    return chain, ""


def follow_chain(src, start, max_len=64):
    """Follow single children (helpers ignored) until a branch or leaf."""
    out = [start]
    while len(out) < max_len:
        k = kids(src, out[-1])
        if len(k) != 1:
            break
        out.append(k[0].name)
    return out


def heaviest_child(src, name, exclude=()):
    k = [c for c in kids(src, name) if c.name not in exclude]
    if not k:
        return None
    return max(k, key=lambda c: (len(c.children_recursive), -abs(len(c.name) - len(name)))).name


def follow_heavy(src, start, count, skip_twist=True):
    """`count` bones down the heaviest path, skipping twist/roll helpers."""
    out = [start]
    cur = start
    while len(out) < count:
        k = [c for c in kids(src, cur)
             if not (skip_twist and set(core_tokens(c.name)) & {"twist", "roll"})]
        if not k:
            break
        nxt = max(k, key=lambda c: len(c.children_recursive))
        out.append(nxt.name)
        cur = nxt.name
    return out


def spine_chain(src, start):
    """Hips -> chest: follow the heaviest child until a bone that splits into
    three or more substantial branches (neck + two arms)."""
    out = [start]
    cur = start
    for _ in range(16):
        big = [c for c in kids(src, cur) if len(c.children_recursive) >= 2]
        if cur != start and len(big) >= 3:
            break
        nxt = heaviest_child(src, cur)
        if not nxt or len(_bones(src)[nxt].children_recursive) < 2:
            break
        out.append(nxt)
        cur = nxt
    return out


def neck_head_chain(src, start):
    out = [start]
    if "head" in core_tokens(start):
        return out, _head_tip(src, start)
    cur = start
    for _ in range(6):
        nxt = heaviest_child(src, cur)
        if not nxt:
            break
        out.append(nxt)
        cur = nxt
        if "head" in core_tokens(nxt):
            break
    return out, _head_tip(src, out[-1])


def _head_tip(src, head):
    for c in _bones(src)[head].children:
        if is_end_bone(c):
            return c.name
    return ""


def limb_chain(src, start, with_toe=False):
    chain = follow_heavy(src, start, 3)
    tip = ""
    if with_toe and len(chain) == 3:
        foot = chain[-1]
        cand = [c for c in kids(src, foot) if not is_end_bone(c)]
        toe = None
        for c in cand:
            if set(core_tokens(c.name)) & {"toe", "toes", "ball", "toebase"}:
                toe = c
                break
        if toe is None and len(cand) == 1:
            toe = cand[0]
        if toe is not None:
            chain.append(toe.name)
            tk = [c for c in toe.children]
            if len(tk) == 1 and not tk[0].children:
                tip = tk[0].name
    return chain, tip


def name_stem(name):
    toks = [t for t in core_tokens(name) if t not in ("m", "c", "mid", "center")]
    return toks[0] if toks else strip_namespace(name).lower()


def sibling_chain_starts(src, start, used=()):
    """All siblings of `start` sharing its name stem (cape panels, skirt)."""
    b = _bones(src)[start]
    stem = name_stem(start)
    pool = b.parent.children if b.parent else [x for x in _bones(src) if x.parent is None]
    out = [c.name for c in pool if name_stem(c.name) == stem and c.name not in used]
    return order_left_to_right(src, out)


def order_left_to_right(src, names):
    mw = src.matrix_world
    return sorted(names, key=lambda n: (mw @ _bones(src)[n].head_local).x, reverse=True)


def flip_name(src, name):
    """Mirror a bone name (Left<->Right, _l<->_r, .L<->.R, _L_<->_R_)."""
    import re
    n = name
    cands = []
    for a, b in (("Left", "Right"), ("left", "right"), ("LEFT", "RIGHT")):
        if a in n:
            cands.append(n.replace(a, b))
        if b in n:
            cands.append(n.replace(b, a))
    swap = {"L": "R", "R": "L", "l": "r", "r": "l"}
    for rx in (r"(?<=[._\- ])([LlRr])(?=$|[._\- \d])", r"^([LlRr])(?=[._\-])"):
        m = list(re.finditer(rx, n))
        for mm in m:
            s, e = mm.span(1)
            cands.append(n[:s] + swap[n[s:e]] + n[e:])
    bones = _bones(src)
    for c in cands:
        if c != name and c in bones:
            return c
    return ""


def piston_rod(src, cyl, used=()):
    """Find the rod that slides in `cyl`: name hint 'rod' or a bone lying on
    the cylinder's axis, outside the cylinder's own hierarchy."""
    bones = _bones(src)
    mw = src.matrix_world
    cb = bones[cyl]
    h = mw @ cb.head_local
    t = mw @ cb.tail_local
    axis = (t - h)
    L = axis.length or 1e-6
    axis = axis / L
    anc = {x.name for x in cb.parent_recursive} | {cyl}
    stem = name_stem(cyl)
    best = None
    for b in bones:
        if b.name in anc or b.name in used or is_helper(b.name):
            continue
        a = mw @ b.head_local
        z = mw @ b.tail_local
        # distance of both ends from the cylinder axis line
        def dist(p):
            v = p - h
            return (v - axis * v.dot(axis)).length
        off = (dist(a) + dist(z)) / 2 / L
        d = (z - a)
        if d.length < 1e-6:
            continue
        par = abs(d.normalized().dot(axis))
        toks = set(core_tokens(b.name))
        score = off * 4 + (1 - par) * 2
        if "rod" in toks or "shaft" in toks or "plunger" in toks:
            score -= 1.0
        if name_stem(b.name) == stem:
            score -= 0.3
        if best is None or score < best[0]:
            best = (score, b.name)
    if best and best[0] < 0.8:
        return best[1]
    return ""


def piston_rod_socket(src, cyl, rod):
    """What the rod hangs on (usually the moving part). Empty = use hierarchy."""
    if not rod:
        return ""
    bones = _bones(src)
    p = bones[rod].parent
    cyl_desc = {x.name for x in bones[cyl].children_recursive} | {cyl}
    if p and p.name not in cyl_desc:
        return p.name
    return ""


def eye_pair(src, eye, used=()):
    other = flip_name(src, eye)
    if other and other not in used and side_of(other):
        return order_left_to_right(src, [eye, other])
    return [eye]
