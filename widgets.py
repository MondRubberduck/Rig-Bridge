"""Control shapes (custom-shape mesh objects), created once per file."""

import math

import bpy

COLL = "WGTS_RigBridge"


def _coll():
    c = bpy.data.collections.get(COLL)
    if c is None:
        c = bpy.data.collections.new(COLL)
        bpy.context.scene.collection.children.link(c)
    # exclude from every view layer (Rigify does the same with its widgets)
    for vl in bpy.context.scene.view_layers:
        lc = _find_lc(vl.layer_collection, c.name)
        if lc:
            lc.exclude = True
    return c


def _find_lc(lc, name):
    if lc.name == name:
        return lc
    for ch in lc.children:
        r = _find_lc(ch, name)
        if r:
            return r
    return None


def _circle(n=32, r=1.0, plane="XZ", y=0.0):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        c, s = math.cos(a) * r, math.sin(a) * r
        pts.append({"XZ": (c, y, s), "XY": (c, s, y), "YZ": (y, c, s)}[plane])
    return pts, [(i, (i + 1) % n) for i in range(n)]


def _merge(*parts):
    verts, edges = [], []
    for v, e in parts:
        o = len(verts)
        verts += v
        edges += [(a + o, b + o) for a, b in e]
    return verts, edges


def _shape(name):
    if name == "circle":
        return _circle()
    if name == "circle_mid":           # ring half-way along the bone
        return _circle(y=0.5, r=0.6)
    if name == "sphere":
        return _merge(_circle(24, 1, "XZ"), _circle(24, 1, "XY"), _circle(24, 1, "YZ"))
    if name == "cube":
        v = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
        e = [(a, b) for a in range(8) for b in range(a + 1, 8)
             if sum(1 for i in range(3) if v[a][i] != v[b][i]) == 1]
        return v, e
    if name == "diamond":
        v = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        e = [(0, 2), (0, 3), (0, 4), (0, 5), (1, 2), (1, 3), (1, 4), (1, 5), (2, 4), (4, 3), (3, 5), (5, 2)]
        return v, e
    if name == "master":               # ring + arrow along the chain
        c = _circle(32, 1.2)
        a = ([(0, 0, 0), (0, 2.2, 0), (-0.25, 1.85, 0), (0.25, 1.85, 0)], [(0, 1), (1, 2), (1, 3)])
        return _merge(c, a)
    if name == "gear":                 # settings / physics control
        pts = []
        n = 16
        for i in range(n * 2):
            a = math.pi * i / n
            r = 1.0 if i % 2 == 0 else 0.75
            pts.append((math.cos(a) * r, 0, math.sin(a) * r))
        e = [(i, (i + 1) % (2 * n)) for i in range(2 * n)]
        return _merge((pts, e), _circle(12, 0.3))
    if name == "target":
        return _merge(_circle(24, 1, "XZ"), ([(-1.4, 0, 0), (1.4, 0, 0), (0, 0, -1.4), (0, 0, 1.4)], [(0, 1), (2, 3)]))
    if name == "square":
        v = [(-1, 0, -1), (1, 0, -1), (1, 0, 1), (-1, 0, 1)]
        return v, [(0, 1), (1, 2), (2, 3), (3, 0)]
    return _circle()


def get(name):
    oname = f"WGT-RB_{name}"
    obj = bpy.data.objects.get(oname)
    if obj is not None:
        return obj
    v, e = _shape(name)
    me = bpy.data.meshes.new(oname)
    me.from_pydata(v, e, [])
    obj = bpy.data.objects.new(oname, me)
    _coll().objects.link(obj)
    return obj
