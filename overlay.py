"""Viewport schematic overlay (Unreal 'Schematic Overlay').

Sockets are drawn as rings at bone heads with the suggested module, placed
modules as filled dots. While placing a module (click a Module Asset tile with
nothing selected) the socket under the mouse is highlighted; clicking drops the
module there.
"""

import math

import blf
import bpy
import gpu
from bpy_extras.view3d_utils import location_3d_to_region_2d
from gpu_extras.batch import batch_for_shader

from . import modular
from .modules import MODULES
from .naming import side_of

PLACING = {"module": "", "hover": None, "src": ""}
_CACHE = {}
_HANDLE = None

COL = {
    "L": (0.25, 0.55, 1.0, 1.0),
    "R": (1.0, 0.3, 0.25, 1.0),
    "": (1.0, 0.82, 0.2, 1.0),
    "socket": (0.85, 0.85, 0.85, 0.9),
    "hover": (1.0, 1.0, 1.0, 1.0),
    "bad": (1.0, 0.1, 0.1, 1.0),
}


def source_of(obj):
    if obj is None or obj.type != "ARMATURE":
        return None
    s = obj.rig_bridge.source
    return s if s else obj


def specs_key(st):
    return tuple((p.module, p.enabled, tuple(b.bone for b in p.bones), p.tip) for p in st.parts)


def schematic(src):
    from . import specs_from_settings
    st = src.rig_bridge
    key = (src.name, specs_key(st), st.show_helpers)
    hit = _CACHE.get(src.name)
    if hit and hit[0] == key:
        return hit[1]
    specs = specs_from_settings(st)
    try:
        sugg = modular.plan(src, specs)
    except Exception as exc:   # never break drawing
        print("[Rig Bridge] plan failed:", exc)
        sugg = {}
    socks = modular.sockets(src, specs, helpers=st.show_helpers)
    data = {
        "sockets": [(b, sugg.get(b) or modular.suggest(src, b)) for b in socks],
        "parts": [(next((b for b in p.bones if b), None), p.module, p.side, p.name) for p in specs],
    }
    _CACHE[src.name] = (key, data)
    return data


def socket_screen_positions(context, src, region, rv3d):
    out = []
    data = schematic(src)
    mw = src.matrix_world
    for b, mid in data["sockets"]:
        pb = src.pose.bones.get(b)
        if pb is None:
            continue
        p2 = location_3d_to_region_2d(region, rv3d, mw @ pb.head)
        if p2 is not None:
            out.append((b, mid, p2))
    return out


def pick(context, area, region, mx, my, src):
    rv3d = area.spaces.active.region_3d
    best = None
    for b, mid, p in socket_screen_positions(context, src, region, rv3d):
        d = math.hypot(p.x - mx, p.y - my)
        if d < 28 and (best is None or d < best[0]):
            best = (d, b)
    return best[1] if best else None


def _ring(cx, cy, r, n=24):
    return [(cx + math.cos(2 * math.pi * i / n) * r, cy + math.sin(2 * math.pi * i / n) * r) for i in range(n + 1)]


def _disc(cx, cy, r, n=20):
    tris = []
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = 2 * math.pi * (i + 1) / n
        tris += [(cx, cy), (cx + math.cos(a0) * r, cy + math.sin(a0) * r), (cx + math.cos(a1) * r, cy + math.sin(a1) * r)]
    return tris


def _line_shader(region, width):
    try:
        sh = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        sh.bind()
        sh.uniform_float("viewportSize", (region.width, region.height))
        sh.uniform_float("lineWidth", width)
        return sh
    except Exception:
        return gpu.shader.from_builtin("UNIFORM_COLOR")


def draw():
    context = bpy.context
    src = source_of(context.object)
    if src is None or src.mode == "EDIT":
        return
    st = src.rig_bridge
    if not st.show_sockets and not PLACING["module"]:
        return
    region, rv3d = context.region, context.region_data
    if rv3d is None:
        return
    try:
        data = schematic(src)
    except Exception:
        return
    mw = src.matrix_world
    gpu.state.blend_set("ALPHA")
    fill = gpu.shader.from_builtin("UNIFORM_COLOR")
    font = 0
    blf.size(font, 12)
    placing = PLACING["module"] and PLACING["src"] == src.name
    # placed modules
    if not st.empty_only and not placing:
        for b, mid, side, name in data["parts"]:
            pb = src.pose.bones.get(b) if b else None
            if pb is None:
                continue
            p = location_3d_to_region_2d(region, rv3d, mw @ pb.head)
            if p is None:
                continue
            col = COL.get(side, COL[""])
            batch = batch_for_shader(fill, "TRIS", {"pos": _disc(p.x, p.y, 6)})
            fill.bind()
            fill.uniform_float("color", col)
            batch.draw(fill)
            mod = MODULES.get(mid)
            blf.color(font, *col)
            blf.position(font, p.x + 9, p.y - 4, 0)
            blf.draw(font, f"{mod.tile if mod else mid}: {name}{'.' + side if side else ''}")
    # sockets
    for b, mid, p in socket_screen_positions(context, src, region, rv3d):
        hover = placing and PLACING["hover"] == b
        col = COL["hover"] if hover else COL["socket"]
        sh = _line_shader(region, 3.0 if hover else 1.5)
        batch = batch_for_shader(sh, "LINE_STRIP", {"pos": _ring(p.x, p.y, 11 if hover else 8)})
        sh.bind()
        sh.uniform_float("color", col)
        batch.draw(sh)
        mod = MODULES.get(mid)
        label = f"+ {MODULES[PLACING['module']].tile}" if hover else (f"+ {mod.tile}" if mod else "+")
        blf.color(font, *col)
        blf.position(font, p.x + 12, p.y - 4, 0)
        blf.draw(font, label if (hover or not placing) else "")
    if placing:
        blf.size(font, 15)
        blf.color(font, 1, 1, 1, 1)
        blf.position(font, 20, region.height - 60, 0)
        blf.draw(font, f"Placing {MODULES[PLACING['module']].tile}: click a socket "
                       f"(Shift = keep placing, Esc/Right-click = done)")
    gpu.state.blend_set("NONE")


def register():
    global _HANDLE
    if _HANDLE is None:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(draw, (), "WINDOW", "POST_PIXEL")


def unregister():
    global _HANDLE
    if _HANDLE is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_HANDLE, "WINDOW")
        _HANDLE = None
    _CACHE.clear()
