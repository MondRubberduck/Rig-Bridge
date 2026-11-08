"""Bone-name parsing helpers: namespaces, sides, tokens."""

import re

SIDE_L = {"l", "left", "lft", "lf"}
SIDE_R = {"r", "right", "rgt", "rt"}

FINGER_TOKENS = (
    ("thumb", "thumb"),
    ("index", "f_index"),
    ("middle", "f_middle"),
    ("mid", "f_middle"),
    ("ring", "f_ring"),
    ("pinky", "f_pinky"),
    ("pinkie", "f_pinky"),
    ("little", "f_pinky"),
)
# 3ds Max Biped: Finger0 = thumb, Finger1 = index, ... Finger01 = 2nd joint of thumb
BIPED_FINGERS = ["thumb", "f_index", "f_middle", "f_ring", "f_pinky"]

# Tokens that mark helper / IK / twist bones that must never be picked as a
# primary hand, foot or head joint.
HELPER_TOKENS = {
    "ik", "pole", "target", "tgt", "twist", "end", "nub", "tip", "top", "ctrl",
    "control", "gun", "roll", "helper", "corrective", "correctiveroot", "offset",
    "socket", "attach", "weapon", "prop", "mch", "org", "jiggle", "null", "dummy",
}


# Exporter prefixes that carry no meaning (Character Creator, 3ds Max Biped, Source)
VENDOR_PREFIX = re.compile(r"^(?:cc_base_|cc3_base_|ccbase_|valvebiped\.bip01_|bip0?1[_ ]?)", re.I)
# Substrings of helper bones written without separators (CC: 'l_upperarmtwist01',
# 'elbowsharebone'); they follow their parent / limb instead of getting controls
HELPER_SUBSTR = ("twist", "share", "corrective", "teeth")


def strip_vendor(name: str) -> str:
    return VENDOR_PREFIX.sub("", name)


def strip_namespace(name: str) -> str:
    """'mixamorig:LeftArm' -> 'LeftArm', 'Char|Root' -> 'Root'."""
    return re.split(r"[:|]", name)[-1]


def norm(name: str) -> str:
    return strip_namespace(name).lower()


def tokens(name: str) -> list[str]:
    """Split a bone name into lower-case tokens (camelCase and separators)."""
    s = strip_vendor(strip_namespace(name))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", s)
    return [t.lower() for t in re.split(r"[\s_\.\-]+", s) if t]


def side_of(name: str) -> str:
    """Return 'L', 'R' or '' for a bone name."""
    toks = tokens(name)
    for t in toks:
        if t in SIDE_L or t.startswith("left"):
            return "L"
        if t in SIDE_R or t.startswith("right"):
            return "R"
    return ""


def core_tokens(name: str) -> list[str]:
    """Tokens with side markers and pure digits removed."""
    out = []
    for t in tokens(name):
        if t in SIDE_L or t in SIDE_R:
            continue
        if t.startswith("left"):
            t = t[4:]
        elif t.startswith("right"):
            t = t[5:]
        if t and not t.isdigit():
            out.append(t)
    return out


def finger_of(name: str):
    """Return ('f_index', joint_hint) or (None, None)."""
    for t in core_tokens(name):
        m = re.match(r"^finger(\d)(\d?)$", t)
        if m:
            i = int(m.group(1))
            if i < len(BIPED_FINGERS):
                return BIPED_FINGERS[i], m.group(2)
        for key, canon in FINGER_TOKENS:
            if t.startswith(key):
                rest = t[len(key):]
                if rest == "" or rest.isdigit() or rest in ("finger", "metacarpal"):
                    return canon, rest
    return None, None


def is_helper(name: str) -> bool:
    toks = set(core_tokens(name))
    if toks & HELPER_TOKENS:
        return True
    n = strip_namespace(name)
    low = strip_vendor(n).lower()
    if any(h in low for h in HELPER_SUBSTR):
        return True
    return n.startswith(("DEF-", "ORG-", "MCH-", "VIS_", "IK_", "ik_"))


def chain_name(base: str, index: int, side: str, finger_style=False) -> str:
    if finger_style:
        s = f"{base}.{index + 1:02d}"
    else:
        s = base if index == 0 else f"{base}.{index:03d}"
    return f"{s}.{side}" if side else s
