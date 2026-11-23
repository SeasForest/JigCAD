"""Dependency-light multi-view renderer (numpy z-buffer + PIL).

Produces one PNG with iso / top / front / right views. Edges come from
B-Rep face boundaries (face-id discontinuities), so holes, pockets and
fillets show up crisply. Ortho views share one scale and carry a mm ruler,
bbox dimensions and optional feature labels, so an agent can verify
positions visually.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

VIEWS = {
    # name: (forward = view direction, up)
    "iso":   ((-1, 1, -1), (0, 0, 1)),
    "top":   ((0, 0, -1), (0, 1, 0)),
    "front": ((0, 1, 0), (0, 0, 1)),
    "right": ((-1, 0, 0), (0, 0, 1)),
    "bottom": ((0, 0, 1), (0, -1, 0)),
    "back":  ((0, -1, 0), (0, 0, 1)),
    "left":  ((1, 0, 0), (0, 0, 1)),
}
BG = (250, 250, 248)
BASE = np.array([150, 175, 205], dtype=float)
EDGE = (25, 30, 40)


def _basis(fwd, up):
    f = np.array(fwd, float)
    f /= np.linalg.norm(f)
    u = np.array(up, float)
    r = np.cross(f, u)
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    return r, u, f


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # old Pillow
        return ImageFont.load_default()


def tessellate(body, tol=0.05, parts=None):
    """parts: optional [(shape, rgb)] material regions; returns per-triangle colour too."""
    verts, tris, fids, cols = [], [], [], []
    off = 0
    bb = body.bounding_box()
    tol = max(tol, max(bb.size.X, bb.size.Y, bb.size.Z) / 800)
    fi = 0
    for shp, rgb in parts or [(body, None)]:
        c = BASE / 255 if rgb is None else np.array(rgb, float)
        for face in shp.faces():
            v, t = face.tessellate(tol, 0.2)
            fi += 1
            if not t:
                continue
            verts.extend((p.X, p.Y, p.Z) for p in v)
            tris.extend((a + off, b + off, c_ + off) for a, b, c_ in t)
            fids.extend([fi] * len(t))
            cols.extend([c] * len(t))
            off += len(v)
    return np.array(verts, float), np.array(tris, int), np.array(fids, int), np.array(cols, float)


def _raster(P, tris, fids, normals, W, H, light):
    depth = np.full((H, W), np.inf)
    fid = np.full((H, W), -1, int)
    shade = np.zeros((H, W))
    for k, (a, b, c) in enumerate(tris):
        p0, p1, p2 = P[a], P[b], P[c]
        xmin, xmax = int(max(0, math.floor(min(p0[0], p1[0], p2[0])))), int(min(W - 1, math.ceil(max(p0[0], p1[0], p2[0]))))
        ymin, ymax = int(max(0, math.floor(min(p0[1], p1[1], p2[1])))), int(min(H - 1, math.ceil(max(p0[1], p1[1], p2[1]))))
        if xmin > xmax or ymin > ymax:
            continue
        den = (p1[1] - p2[1]) * (p0[0] - p2[0]) + (p2[0] - p1[0]) * (p0[1] - p2[1])
        if abs(den) < 1e-12:
            continue
        xs, ys = np.meshgrid(np.arange(xmin, xmax + 1) + 0.5, np.arange(ymin, ymax + 1) + 0.5)
        w0 = ((p1[1] - p2[1]) * (xs - p2[0]) + (p2[0] - p1[0]) * (ys - p2[1])) / den
        w1 = ((p2[1] - p0[1]) * (xs - p2[0]) + (p0[0] - p2[0]) * (ys - p2[1])) / den
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not m.any():
            continue
        z = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
        sub = depth[ymin:ymax + 1, xmin:xmax + 1]
        upd = m & (z < sub - 1e-9)
        if not upd.any():
            continue
        sub[upd] = z[upd]
        fid[ymin:ymax + 1, xmin:xmax + 1][upd] = fids[k]
        n = normals[k]
        shade[ymin:ymax + 1, xmin:xmax + 1][upd] = 0.35 + 0.65 * abs(float(np.dot(n, light)))
    return depth, fid, shade


def _edges(fid, depth, scale):
    """Feature edges = boundaries between different B-Rep faces (or face vs background).

    Depth jumps are deliberately ignored: on steep curved faces they produce false
    black bands at grazing angles."""
    e = np.zeros(fid.shape, bool)
    for dy, dx in ((0, 1), (1, 0)):
        a = fid[:fid.shape[0] - dy, :fid.shape[1] - dx]
        b = fid[dy:, dx:]
        e[:a.shape[0], :a.shape[1]] |= a != b
    return e


def section_cut(body, axis: str):
    """Remove the half of the body facing the matching ortho camera (x: +X half, y: -Y half,
    z: upper half) so interiors become visible."""
    from build123d import Box, Location
    bb = body.bounding_box()
    c = bb.center()
    big = 2 * max(bb.size.X, bb.size.Y, bb.size.Z) + 10
    off = {"x": (big / 2 + c.X, c.Y, c.Z), "y": (c.X, c.Y - big / 2, c.Z), "z": (c.X, c.Y, c.Z + big / 2)}
    if axis not in off:
        raise ValueError("section must be x|y|z")
    return body - Box(big, big, big).moved(Location(off[axis]))


def render(body, path, views=("iso", "top", "front", "right"), tile=460, labels=None, title="",
           section=None, regions=None):
    """labels: list of (text, (x,y,z)) shown as markers. section: x|y|z half cut."""
    if section:
        c = body.bounding_box().center()
        keep = {"x": lambda q: q[0] <= c.X + 1e-6, "y": lambda q: q[1] >= c.Y - 1e-6,
                "z": lambda q: q[2] <= c.Z + 1e-6}[section]
        labels = [(t, q) for t, q in labels or [] if keep(q)]
        if regions:
            regions = [(section_cut(r_, section), c_) for r_, c_ in regions]
        body = section_cut(body, section)
        title = (title + f"  [section {section}]").strip()
    legend = []
    names = list(dict.fromkeys(t for t, _ in labels or []))
    if len(names) > 8:  # too many to print in place: numbered markers + legend strip
        num = {n: str(i + 1) for i, n in enumerate(names)}
        legend = [f"{num[n]} {n}" for n in names]
        labels = [(num[t], p) for t, p in labels]
    V, T, F, TC = tessellate(body, parts=regions)
    if len(T) == 0:
        raise ValueError("nothing to render")
    tri_n = np.cross(V[T[:, 1]] - V[T[:, 0]], V[T[:, 2]] - V[T[:, 0]])
    ln = np.linalg.norm(tri_n, axis=1)
    ln[ln == 0] = 1
    tri_n = tri_n / ln[:, None]
    margin = 46
    # shared scale for ortho views
    def extent(view):
        r, u, f = _basis(*VIEWS[view])
        return np.ptp(V @ r), np.ptp(V @ u)
    scale = min((tile - 2 * margin) / max(*extent(v), 1e-9) for v in views)
    cols = 2 if len(views) > 1 else 1
    rows = math.ceil(len(views) / cols)
    f_small, f_big = _font(12), _font(15)
    leg_lines = []
    if legend:
        line = ""
        for item in legend:
            cand = (line + "   " + item) if line else item
            if len(cand) * 7 > cols * tile - 16:
                leg_lines.append(line)
                line = item
            else:
                line = cand
        leg_lines.append(line)
    leg_h = 16 * len(leg_lines) + (8 if leg_lines else 0)
    img = Image.new("RGB", (cols * tile, rows * tile + (24 if title else 0) + leg_h), BG)
    dr = ImageDraw.Draw(img)
    for i, ln_ in enumerate(leg_lines):
        dr.text((8, rows * tile + (24 if title else 0) + 4 + 16 * i), ln_, fill=(200, 40, 40), font=f_small)
    y0 = 0
    if title:
        dr.text((8, 4), title, fill=EDGE, font=f_big)
        y0 = 24
    for idx, view in enumerate(views):
        r, u, f = _basis(*VIEWS[view])
        light = -f * 0.6 + u * 0.5 + r * 0.3
        light /= np.linalg.norm(light)
        s = scale if view != "iso" else (tile - 2 * margin) / max(*extent(view), 1e-9)
        X, Y, Z = V @ r, V @ u, V @ f
        cx, cy = (X.max() + X.min()) / 2, (Y.max() + Y.min()) / 2
        P = np.stack([(X - cx) * s + tile / 2, tile / 2 - (Y - cy) * s, Z], axis=1)
        depth, fid, shade = _raster(P, T, F, tri_n, tile, tile, light)
        col = np.empty((tile, tile, 3), np.uint8)
        col[:] = BG
        mask = fid >= 0
        fcol = {}  # face id -> colour
        for k in range(len(F)):
            fcol.setdefault(F[k], TC[k])
        lut = np.zeros((max(F) + 2, 3))
        for k_, c_ in fcol.items():
            lut[k_] = c_ * 255
        base = lut[fid[mask]]
        col[mask] = np.clip(base * shade[mask][:, None] * 0.85 + 30, 0, 255).astype(np.uint8)
        col[_edges(fid, depth, s)] = EDGE
        ox, oy = (idx % cols) * tile, (idx // cols) * tile + y0
        img.paste(Image.fromarray(col), (ox, oy))
        dr.rectangle([ox, oy, ox + tile - 1, oy + tile - 1], outline=(200, 200, 200))
        rlab = "".join("+" + "XYZ"[i] if r[i] > 0.5 else "-" + "XYZ"[i] if r[i] < -0.5 else "" for i in range(3))
        ulab = "".join("+" + "XYZ"[i] if u[i] > 0.5 else "-" + "XYZ"[i] if u[i] < -0.5 else "" for i in range(3))
        hdr = view.upper() if view == "iso" else f"{view.upper()}  right={rlab} up={ulab}"
        dr.text((ox + 6, oy + 4), hdr, fill=EDGE, font=f_big)
        if view != "iso":
            w_mm, h_mm = np.ptp(X), np.ptp(Y)
            dr.text((ox + 6, oy + tile - 18), f"{w_mm:.2f} x {h_mm:.2f} mm", fill=EDGE, font=f_small)
            # ruler
            step = 10 ** math.floor(math.log10(max(w_mm, 1e-6) / 2 + 1e-9))
            for mult in (5, 2, 1):
                if step * mult * s < tile / 3:
                    step *= mult
                    break
            rx, ry = ox + tile - 16 - step * s, oy + tile - 12
            dr.line([rx, ry, rx + step * s, ry], fill=EDGE, width=2)
            dr.line([rx, ry - 4, rx, ry + 4], fill=EDGE)
            dr.line([rx + step * s, ry - 4, rx + step * s, ry + 4], fill=EDGE)
            dr.text((rx, ry - 16), f"{step:g} mm", fill=EDGE, font=f_small)
        seen = set()
        for text, p in labels or []:
            if view not in ("iso", "top") and text in seen:
                continue
            pv = np.array(p, float)
            px = ox + (pv @ r - cx) * s + tile / 2
            py = oy + tile / 2 - (pv @ u - cy) * s
            dr.ellipse([px - 3, py - 3, px + 3, py + 3], outline=(200, 40, 40), width=2)
            if text not in seen:
                dr.text((px + 5, py - 14), text, fill=(200, 40, 40), font=f_small)
                seen.add(text)
    img.save(path)
    return path
