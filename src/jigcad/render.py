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


def tessellate(body, tol=0.05):
    verts, tris, fids = [], [], []
    off = 0
    bb = body.bounding_box()
    tol = max(tol, max(bb.size.X, bb.size.Y, bb.size.Z) / 800)
    for fi, face in enumerate(body.faces()):
        v, t = face.tessellate(tol, 0.2)
        if not t:
            continue
        verts.extend((p.X, p.Y, p.Z) for p in v)
        tris.extend((a + off, b + off, c + off) for a, b, c in t)
        fids.extend([fi] * len(t))
        off += len(v)
    return np.array(verts, float), np.array(tris, int), np.array(fids, int)


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
    e = np.zeros(fid.shape, bool)
    for dy, dx in ((0, 1), (1, 0)):
        a = fid[:fid.shape[0] - dy, :fid.shape[1] - dx]
        b = fid[dy:, dx:]
        diff = a != b
        da = depth[:fid.shape[0] - dy, :fid.shape[1] - dx]
        db = depth[dy:, dx:]
        with np.errstate(invalid="ignore"):
            dd = np.where(np.isfinite(da) & np.isfinite(db), np.abs(da - db), 0.0)
        jump = dd > 2.0 / max(scale, 1e-9)
        diff |= jump
        e[:a.shape[0], :a.shape[1]] |= diff
    return e


def render(body, path, views=("iso", "top", "front", "right"), tile=460, labels=None, title=""):
    """labels: list of (text, (x,y,z)) shown as markers."""
    V, T, F = tessellate(body)
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
    img = Image.new("RGB", (cols * tile, rows * tile + (24 if title else 0)), BG)
    dr = ImageDraw.Draw(img)
    f_small, f_big = _font(12), _font(15)
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
        col[mask] = np.clip(BASE[None, :] * shade[mask][:, None] + 40, 0, 255).astype(np.uint8)
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
