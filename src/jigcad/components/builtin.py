"""Built-in component library (jig / fixture / bracket oriented).

Conventions
-----------
* Built in the local frame of the anchor plane: +Z = outward normal.
* ``base``/``add`` grow along +Z starting at z=0.
* ``cut`` tools start EPS above z=0 and go down to -depth.
"""
from __future__ import annotations

import math

from build123d import (Align, Axis, Circle, Cone, Cylinder, Plane, Polygon, RectangleRounded, Solid, Vector,
                       revolve as b_revolve,
                       Rectangle, RegularPolygon, Sketch, SlotCenterToCenter,
                       chamfer as b_chamfer, extrude, fillet as b_fillet, offset as b_offset,
                       Kind)

from .. import standards
from ..geom import EPS, select_edges
from .base import Out, P, ParamError, component

BOT = (Align.CENTER, Align.CENTER, Align.MIN)
TOP = (Align.CENTER, Align.CENTER, Align.MAX)


def _w(shape, plane: Plane):
    return shape.moved(plane.location)


def _pt(plane: Plane, x=0.0, y=0.0, z=0.0):
    v = plane.from_local_coords((x, y, z))
    return (round(v.X, 4), round(v.Y, 4), round(v.Z, 4))


def _axis(plane: Plane):
    z = plane.z_dir
    return (-z.X, -z.Y, -z.Z)


def _rect_prism(l, w, h, r=0.0, align_top=False):
    sk = RectangleRounded(l, w, r) if r > 0 else Rectangle(l, w)
    s = extrude(sk, h)
    if align_top:
        s = s.moved(Plane.XY.offset(-h).location)
    return s


def _local_plane(plane: Plane, x=0.0, y=0.0, z=0.0) -> Plane:
    return Plane(origin=plane.from_local_coords((x, y, z)), x_dir=plane.x_dir, z_dir=plane.z_dir)


# --------------------------------------------------------------------------- base

@component("box", "base", "rectangular block, centred in XY, sitting on the plane (z 0..h)",
           {"l": P(required=True, min=0.001, doc="length along local x"),
            "w": P(required=True, min=0.001, doc="width along local y"),
            "h": P(required=True, min=0.001, doc="height along +z"),
            "r": P(default=0.0, min=0, doc="vertical corner radius")},
           example={"use": "box", "with": {"l": 40, "w": 20, "h": 10}})
def box(ctx, p):
    if p["r"] * 2 >= min(p["l"], p["w"]):
        raise ParamError(f"r={p['r']} too large: must be < min(l,w)/2 = {min(p['l'], p['w'])/2}")
    return Out(_w(_rect_prism(p["l"], p["w"], p["h"], p["r"]), ctx.plane))


@component("plate", "base", "flat plate (alias of box with thickness t), optional corner radius",
           {"l": P(required=True, min=0.001), "w": P(required=True, min=0.001),
            "t": P(required=True, min=0.001, doc="thickness"),
            "r": P(default=0.0, min=0, doc="corner radius")},
           example={"use": "plate", "with": {"l": 120, "w": 60, "t": 6, "r": 4}})
def plate(ctx, p):
    if p["r"] * 2 >= min(p["l"], p["w"]):
        raise ParamError(f"r={p['r']} too large: must be < min(l,w)/2")
    return Out(_w(_rect_prism(p["l"], p["w"], p["t"], p["r"]), ctx.plane))


@component("cylinder", "base", "cylinder standing on the plane",
           {"d": P(required=True, min=0.001), "h": P(required=True, min=0.001)})
def cylinder(ctx, p):
    return Out(_w(Cylinder(p["d"] / 2, p["h"], align=BOT), ctx.plane))


@component("tube", "base", "hollow cylinder standing on the plane",
           {"od": P(required=True, min=0.001), "id": P(required=True, min=0.0),
            "h": P(required=True, min=0.001)})
def tube(ctx, p):
    if p["id"] >= p["od"]:
        raise ParamError("id must be < od")
    s = Cylinder(p["od"] / 2, p["h"], align=BOT) - Cylinder(p["id"] / 2, p["h"], align=BOT)
    return Out(_w(s, ctx.plane))


@component("profile", "base", "extrude a closed 2D polygon (local x,y points) by h; optional corner radius",
           {"points": P("list", required=True, doc="[[x,y],...] >=3 points, any winding"),
            "h": P(required=True, min=0.001), "r": P(default=0.0, min=0, doc="2D corner fillet")},
           example={"use": "profile", "with": {"points": [[0, 0], [40, 0], [0, 30]], "h": 5}})
def profile(ctx, p):
    pts = [tuple(map(float, q)) for q in p["points"]]
    if len(pts) < 3:
        raise ParamError("profile needs >= 3 points")
    area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    if abs(area2) < 1e-9:
        raise ParamError("profile points are collinear / zero area")
    if area2 < 0:  # clockwise -> make CCW so the extrusion always grows along +normal
        pts = pts[::-1]
    sk = Sketch() + Polygon(*pts, align=None)
    if p["r"] > 0:
        sk = b_fillet(sk.vertices(), p["r"])
    return Out(_w(extrude(sk, p["h"]), ctx.plane))


@component("l_bracket", "base", "L bracket: horizontal leg on the plane + vertical wall at local -x end",
           {"l": P(required=True, min=0.001, doc="horizontal leg length (x)"),
            "w": P(required=True, min=0.001, doc="width (y)"),
            "h": P(required=True, min=0.001, doc="wall height (z)"),
            "t": P(required=True, min=0.001, doc="thickness of both legs")},
           anchors="leg_top (top of horizontal leg, centred on free span), wall_face (inner face of wall, normal +x)",
           example={"use": "l_bracket", "with": {"l": 50, "w": 30, "h": 40, "t": 5}})
def l_bracket(ctx, p):
    l, w, h, t = p["l"], p["w"], p["h"], p["t"]
    if t >= min(l, h):
        raise ParamError("t must be smaller than l and h")
    pts = [(-l / 2, 0), (l / 2, 0), (l / 2, t), (-l / 2 + t, t), (-l / 2 + t, h), (-l / 2, h)]
    sk = Polygon(*pts, align=None)
    s = extrude(Plane.XZ * sk, w / 2, both=True)
    pl = ctx.plane
    anchors = {
        "leg_top": Plane(origin=pl.from_local_coords(((t) / 2, 0, t)), x_dir=pl.x_dir, z_dir=pl.z_dir),
        "wall_face": Plane(origin=pl.from_local_coords((-l / 2 + t, 0, (h + t) / 2)),
                           x_dir=pl.y_dir, z_dir=pl.x_dir),
    }
    return Out(_w(s, pl), anchors=anchors)


@component("revolve", "base", "lathe: revolve a closed [r, z] profile about the anchor normal (columns, rings, domes)",
           {"points": P("list", required=True, doc="[[r, z], ...] r >= 0 = distance from axis, z along normal"),
            "angle": P(default=360.0, min=1, max=360)},
           example={"use": "revolve", "on": "base.top", "with": {"points": [[0, 0], [20, 0], [15, 30], [0, 30]]}})
def revolve(ctx, p):
    pts = [tuple(map(float, q)) for q in p["points"]]
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        raise ParamError("revolve needs >= 3 [r, z] points")
    if min(q[0] for q in pts) < -1e-9:
        raise ParamError("revolve: r must be >= 0 (profile may not cross the axis)")
    area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
    if abs(area2) < 1e-9:
        raise ParamError("revolve profile has zero area")
    face = Plane.XZ * Polygon(*pts, align=None)
    s = b_revolve(face, Axis.Z, p["angle"])
    pl = ctx.plane
    zmax = max(q[1] for q in pts)
    return Out(_w(s, pl), anchors={"tip": _local_plane(pl, z=zmax)}, points=[_pt(pl)], axis=_axis(pl))


# --------------------------------------------------------------------------- add

@component("boss", "add", "cylindrical boss grown from the face, optional centre hole",
           {"d": P(required=True, min=0.001), "h": P(required=True, min=0.001),
            "hole_d": P(default=0.0, min=0),
            "hole_depth": P("depth", default=None, doc="from tip: number | through | next (boss + wall below); default = h")},
           anchors="tip (top face of boss)")
def boss(ctx, p):
    s = Cylinder(p["d"] / 2, p["h"], align=BOT)
    out = Out(_w(s, ctx.plane), anchors={"tip": _local_plane(ctx.plane, z=p["h"])},
              points=[_pt(ctx.plane)], axis=_axis(ctx.plane))
    if p["hole_d"] > 0:
        if p["hole_d"] >= p["d"]:
            raise ParamError("hole_d must be < d")
        hdp = p["hole_depth"]
        if hdp is None:
            hd = p["h"]
        elif hdp == "through":
            hd = ctx.through
        elif hdp == "next":  # through the boss and the first wall it stands on
            hd = p["h"] + (ctx.next_depth() if ctx.body is not None else 0.0)
        else:
            hd = hdp
        tool = Cylinder(p["hole_d"] / 2, hd + EPS, align=TOP).moved(Plane.XY.offset(p["h"] + EPS).location)
        out.info["post_cut"] = _w(tool, ctx.plane)
    return out


@component("standoff", "add", "screw standoff: boss with a fastener-sized hole (tap / insert / clearance)",
           {"size": P("str", required=True, doc="M2..M12"),
            "h": P(required=True, min=0.001),
            "fit": P("str", default="insert", choices=("insert", "tap", "close", "normal", "loose")),
            "od": P(default=None, doc="outer diameter, default = hole + 2*max(2, 0.8*hole)"),
            "hole_depth": P("depth", default=None, doc="default = h")},
           anchors="tip")
def standoff(ctx, p):
    hd = standards.clearance(p["size"], p["fit"])
    od = p["od"] or round(hd + 2 * max(2.0, 0.8 * hd), 2)
    depth = p["hole_depth"]
    if depth is None and p["fit"] == "insert":
        depth = max(p["h"], standards.fastener(p["size"])["insert_depth"] + 1)
    return boss(ctx, {"d": od, "h": p["h"], "hole_d": hd, "hole_depth": depth})


@component("gusset", "add", "triangular rib in the local XZ plane: legs along +x (length l) and +z (height h)",
           {"l": P(required=True, min=0.001), "h": P(required=True, min=0.001),
            "t": P(required=True, min=0.001, doc="thickness (centred on local y=0)")},
           example={"use": "gusset", "on": "bracket.leg_top", "at": ["=-(L/2)+T", 0], "with": {"l": 20, "h": 20, "t": 4}})
def gusset(ctx, p):
    sk = Polygon((0, 0), (p["l"], 0), (0, p["h"]), align=None)
    s = extrude(Plane.XZ * sk, p["t"] / 2, both=True)
    return Out(_w(s, ctx.plane), axis=_axis(ctx.plane))


@component("rod", "add", "round bar between two local points a and b (bracing, rails, rakes)",
           {"a": P("vec3", required=True, doc="[u, v, w] start, anchor-local"),
            "b": P("vec3", required=True, doc="[u, v, w] end, anchor-local"),
            "d": P(required=True, min=0.001)})
def rod(ctx, p):
    a, b = Vector(*p["a"]), Vector(*p["b"])
    ln = (b - a).length
    if ln < 1e-6:
        raise ParamError("rod: a and b coincide")
    s = Solid.make_cylinder(p["d"] / 2, ln, Plane(origin=a, z_dir=b - a))
    pl = ctx.plane
    return Out(_w(s, pl), points=[_pt(pl, *p["a"]), _pt(pl, *p["b"])])


@component("pin", "add", "locating / dowel pin with tip chamfer",
           {"d": P(required=True, min=0.001), "h": P(required=True, min=0.001),
            "chamfer": P(default=0.5, min=0)}, anchors="tip")
def pin(ctx, p):
    c = min(p["chamfer"], p["d"] / 2 * 0.9, p["h"] * 0.5)
    s = Cylinder(p["d"] / 2, p["h"] - c, align=BOT)
    if c > 0:
        s = s + Cone(p["d"] / 2, p["d"] / 2 - c, c, align=BOT).moved(Plane.XY.offset(p["h"] - c).location)
    return Out(_w(s, ctx.plane), anchors={"tip": _local_plane(ctx.plane, z=p["h"])},
               points=[_pt(ctx.plane)], axis=_axis(ctx.plane))


@component("block", "add", "rectangular block grown from the face (centred at anchor)",
           {"l": P(required=True, min=0.001), "w": P(required=True, min=0.001),
            "h": P(required=True, min=0.001), "r": P(default=0.0, min=0)}, anchors="tip")
def block(ctx, p):
    return Out(_w(_rect_prism(p["l"], p["w"], p["h"], p["r"]), ctx.plane),
               anchors={"tip": _local_plane(ctx.plane, z=p["h"])}, axis=_axis(ctx.plane))


# --------------------------------------------------------------------------- cut

def _depth(ctx, d):
    if d == "through":
        return ctx.through
    if d == "next":
        return ctx.next_depth()
    return d


def _thru(d):
    return d in ("through", "next")


@component("hole", "cut", "drilled hole: simple / counterbore / countersink; size+fit or explicit d",
           {"d": P(default=None, min=0.01, doc="diameter (or give size)"),
            "size": P("str", default=None, doc="fastener size M2..M12 -> d from fit table"),
            "fit": P("str", default="normal", choices=("close", "normal", "loose", "tap", "insert")),
            "depth": P("depth", default="through", doc="number | through (all) | next (first wall only)"),
            "type": P("str", default="simple", choices=("simple", "cbore", "csk")),
            "cbore_d": P(default=None), "cbore_depth": P(default=None),
            "csk_d": P(default=None), "csk_angle": P(default=90.0, min=30, max=150)},
           example={"use": "hole", "on": "base.top", "at": [10, 0], "with": {"size": "M4", "type": "cbore"}})
def hole(ctx, p):
    if p["d"] is None and p["size"] is None:
        raise ParamError("hole needs d or size")
    d = p["d"] if p["d"] is not None else standards.clearance(p["size"], p["fit"])
    depth = _depth(ctx, p["depth"])
    tool = Cylinder(d / 2, depth + EPS, align=TOP).moved(Plane.XY.offset(EPS).location)
    std = standards.fastener(p["size"]) if p["size"] else None
    info = {"d": d}
    if p["type"] == "cbore":
        cd = p["cbore_d"] or (std["cbore_d"] if std else None)
        ch = p["cbore_depth"] or (std["head_h"] + 0.3 if std else None)
        if not cd or not ch:
            raise ParamError("cbore needs cbore_d and cbore_depth (or size)")
        if cd <= d:
            raise ParamError("cbore_d must be > d")
        tool = tool + Cylinder(cd / 2, ch + EPS, align=TOP).moved(Plane.XY.offset(EPS).location)
        info.update(cbore_d=cd, cbore_depth=ch)
    elif p["type"] == "csk":
        cd = p["csk_d"] or (std["csk_d"] + 0.4 if std else None)
        if not cd:
            raise ParamError("csk needs csk_d (or size)")
        hcone = (cd - d) / 2 / math.tan(math.radians(p["csk_angle"] / 2))
        cone = Cone(d / 2, cd / 2, hcone, align=BOT).moved(Plane.XY.offset(-hcone).location)
        cap = Cylinder(cd / 2, EPS, align=BOT)
        tool = tool + cone + cap
        info.update(csk_d=cd)
    return Out(_w(tool, ctx.plane), points=[_pt(ctx.plane)], through=_thru(p["depth"]),
               axis=_axis(ctx.plane), info=info, depth=depth)


@component("pocket", "cut", "rectangular pocket centred at anchor, optional corner radius",
           {"l": P(required=True, min=0.001), "w": P(required=True, min=0.001),
            "depth": P("depth", required=True), "r": P(default=0.0, min=0),
            "breakout": P("bool", default=False, doc="allow opening side walls")},
           anchors="floor (pocket floor, normal outward)")
def pocket(ctx, p):
    depth = _depth(ctx, p["depth"])
    if p["r"] * 2 >= min(p["l"], p["w"]):
        raise ParamError("r too large for pocket")
    tool = _rect_prism(p["l"], p["w"], depth + EPS, p["r"]).moved(Plane.XY.offset(-depth).location)
    anchors = {} if _thru(p["depth"]) else {"floor": _local_plane(ctx.plane, z=-depth)}
    return Out(_w(tool, ctx.plane), anchors=anchors, points=[_pt(ctx.plane)], depth=depth,
               through=_thru(p["depth"]), axis=_axis(ctx.plane), allow_breakout=p["breakout"])


@component("slot", "cut", "obround slot (e.g. adjustable mounting), length = centre-to-centre along local x",
           {"length": P(required=True, min=0.0), "width": P(required=True, min=0.001),
            "depth": P("depth", default="through")})
def slot(ctx, p):
    depth = _depth(ctx, p["depth"])
    sk = SlotCenterToCenter(p["length"], p["width"]) if p["length"] > 0 else None
    if sk is None:
        tool = Cylinder(p["width"] / 2, depth + EPS, align=BOT)
    else:
        tool = extrude(sk, depth + EPS)
    tool = tool.moved(Plane.XY.offset(-depth).location)
    pl = ctx.plane
    return Out(_w(tool, pl), points=[_pt(pl, -p["length"] / 2), _pt(pl, p["length"] / 2)],
               through=_thru(p["depth"]), axis=_axis(pl), depth=depth)


@component("arch", "cut", "round-headed opening: jambs of height h from the anchor (v=0) + semicircle of span w",
           {"w": P(required=True, min=0.001, doc="clear span"), "h": P(default=0.0, min=0, doc="jamb height to springing"),
            "depth": P("depth", default="through"),
            "breakout": P("bool", default=False, doc="allow opening the face below (doorways, passages)")},
           anchors="crown (top of the opening)")
def arch(ctx, p):
    depth = _depth(ctx, p["depth"])
    w, h = p["w"], p["h"]
    sk = Circle(w / 2).moved(Plane.XY.offset(0).location * _loc_xy(0, h))
    if h > 0:
        sk = sk + Rectangle(w, h).moved(_loc_xy(0, h / 2))
    tool = extrude(sk, depth + EPS).moved(Plane.XY.offset(-depth).location)
    pl = ctx.plane
    return Out(_w(tool, pl), anchors={"crown": _local_plane(pl, 0, h + w / 2)}, points=[_pt(pl, 0, h)],
               through=_thru(p["depth"]), axis=_axis(pl), allow_breakout=p["breakout"], depth=depth)


def _loc_xy(x, y):
    from build123d import Location
    return Location((x, y, 0))


@component("nut_trap", "cut", "hexagonal nut pocket (ISO 4032 across-flats + clearance)",
           {"size": P("str", required=True), "depth": P(default=None, doc="default nut height + 0.3"),
            "clearance": P(default=0.2, min=0, doc="added to across-flats"),
            "bolt_hole": P("bool", default=True, doc="also cut a through clearance hole")})
def nut_trap(ctx, p):
    std = standards.fastener(p["size"])
    af = std["nut_af"] + p["clearance"]
    depth = p["depth"] or std["nut_h"] + 0.3
    tool = extrude(RegularPolygon(radius=af / 2, side_count=6, major_radius=False), depth + EPS)
    tool = tool.moved(Plane.XY.offset(-depth).location)
    if p["bolt_hole"]:
        tool = tool + Cylinder(std["clr_normal"] / 2, ctx.through, align=TOP)
    return Out(_w(tool, ctx.plane), points=[_pt(ctx.plane)], through=p["bolt_hole"],
               axis=_axis(ctx.plane), info={"af": af, "depth": depth})


@component("notch", "cut", "rectangular cut that may open side walls (edge notches, clearances)",
           {"l": P(required=True, min=0.001), "w": P(required=True, min=0.001),
            "depth": P("depth", required=True)})
def notch(ctx, p):
    depth = _depth(ctx, p["depth"])
    tool = _rect_prism(p["l"], p["w"], depth + EPS).moved(Plane.XY.offset(-depth).location)
    return Out(_w(tool, ctx.plane), through=_thru(p["depth"]), axis=_axis(ctx.plane),
               allow_breakout=True, points=[_pt(ctx.plane)], depth=depth)


# --------------------------------------------------------------------------- modifiers

def _restrict(ctx, of):
    if not of:
        return None
    f = ctx.feature(of)
    return f.shape


@component("fillet", "modifier", "round edges chosen by semantic selector",
           {"r": P(required=True, min=0.001),
            "edges": P("str", default="outer_vertical",
                       choices=("all", "vertical", "outer_vertical", "horizontal", "top", "bottom", "circular", "linear")),
            "of": P("str", default=None, doc="only edges generated by this feature id")})
def fillet(ctx, p):
    edges = select_edges(ctx.body, p["edges"], _restrict(ctx, p["of"]))
    if not edges:
        raise ParamError(f"selector edges={p['edges']} of={p['of']} matched 0 edges")
    try:
        body = b_fillet(edges, p["r"])
    except Exception as e:  # noqa: BLE001
        mn = min(e_.length for e_ in edges)
        raise ParamError(f"fillet r={p['r']} failed on {len(edges)} edges (shortest edge {mn:.3g}); "
                         f"try smaller r or narrower selector") from e
    return Out(body, info={"edges": len(edges)})


@component("chamfer", "modifier", "chamfer edges chosen by semantic selector",
           {"d": P(required=True, min=0.001),
            "edges": P("str", default="top",
                       choices=("all", "vertical", "outer_vertical", "horizontal", "top", "bottom", "circular", "linear")),
            "of": P("str", default=None)})
def chamfer(ctx, p):
    edges = select_edges(ctx.body, p["edges"], _restrict(ctx, p["of"]))
    if not edges:
        raise ParamError(f"selector edges={p['edges']} of={p['of']} matched 0 edges")
    try:
        body = b_chamfer(edges, p["d"])
    except Exception as e:  # noqa: BLE001
        raise ParamError(f"chamfer d={p['d']} failed on {len(edges)} edges; try smaller d") from e
    return Out(body, info={"edges": len(edges)})


@component("shell", "modifier", "hollow the body with wall thickness t, optionally open one face",
           {"t": P(required=True, min=0.001),
            "open": P("str", default="top", choices=("top", "bottom", "none"))})
def shell(ctx, p):
    body = ctx.body
    bb = body.bounding_box()
    openings = []
    if p["open"] != "none":
        z = bb.max.Z if p["open"] == "top" else bb.min.Z
        openings = [f for f in body.faces() if abs(f.center().Z - z) < 1e-4
                    and abs(abs(f.normal_at().Z) - 1) < 1e-6]
        if not openings:
            raise ParamError(f"no planar {p['open']} face to open")
    try:
        res = b_offset(body, amount=-p["t"], openings=openings, kind=Kind.INTERSECTION)
    except Exception as e:  # noqa: BLE001
        raise ParamError(f"shell t={p['t']} failed; check t vs. smallest feature size") from e
    return Out(res, info={"openings": len(openings)})
