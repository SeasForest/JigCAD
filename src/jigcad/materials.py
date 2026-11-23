"""Material / colour palette and multi-material region partitioning.

A material is a name with a display colour (and optional density g/cm3).
Built-ins cover common filament colours, engineering materials and the
procgen slot names (so procgen kits map 1:1). Specs may add or override
entries under top-level ``materials:``.
"""
from __future__ import annotations

import difflib

# name: ((r, g, b) 0..1, density g/cm3 or None)
BUILTIN: dict[str, tuple[tuple[float, float, float], float | None]] = {
    # neutral default
    "default": ((0.59, 0.69, 0.80), None),
    # engineering / filament materials
    "pla": ((0.85, 0.85, 0.82), 1.24), "petg": ((0.75, 0.82, 0.88), 1.27), "abs": ((0.80, 0.80, 0.78), 1.04),
    "asa": ((0.78, 0.78, 0.76), 1.07), "nylon": ((0.90, 0.88, 0.80), 1.14), "resin": ((0.70, 0.72, 0.75), 1.18),
    "tpu": ((0.25, 0.25, 0.27), 1.21), "pom": ((0.92, 0.92, 0.90), 1.41),
    "aluminium": ((0.77, 0.78, 0.80), 2.70), "aluminum": ((0.77, 0.78, 0.80), 2.70),
    "steel": ((0.49, 0.51, 0.51), 7.85), "stainless": ((0.62, 0.63, 0.64), 8.0), "brass": ((0.71, 0.58, 0.29), 8.5),
    # plain print colours (density unknown -> uses part material density)
    "white": ((0.95, 0.95, 0.94), None), "black": ((0.12, 0.12, 0.13), None), "grey": ((0.55, 0.56, 0.57), None),
    "gray": ((0.55, 0.56, 0.57), None), "red": ((0.80, 0.16, 0.14), None), "orange": ((0.93, 0.47, 0.13), None),
    "yellow": ((0.96, 0.80, 0.16), None), "green": ((0.22, 0.60, 0.29), None), "blue": ((0.16, 0.38, 0.75), None),
    "purple": ((0.48, 0.28, 0.62), None), "brown": ((0.45, 0.31, 0.20), None),
    # procgen-hy4 slot colours (procgen/src/doc/doc_eval.cpp)
    "stone": ((0.62, 0.56, 0.45), 2.6), "iron": ((0.19, 0.39, 0.34), 7.2), "bronze": ((0.63, 0.40, 0.20), 8.8),
    "glass": ((0.52, 0.72, 0.73), 2.5), "brick": ((0.53, 0.30, 0.22), 1.9), "wood": ((0.50, 0.35, 0.24), 0.6),
    "tile": ((0.74, 0.69, 0.57), 2.0), "opal": ((0.93, 0.81, 0.51), None),
    "weathered-brick": ((0.56, 0.31, 0.20), 1.9), "chipped-limestone": ((0.74, 0.66, 0.49), 2.5),
    "weathered-iron": ((0.31, 0.29, 0.25), 7.2), "earth": ((0.46, 0.38, 0.28), None),
    "water": ((0.22, 0.34, 0.38), None), "grass": ((0.36, 0.44, 0.24), None), "leaf": ((0.27, 0.43, 0.21), None),
    "terracotta": ((0.66, 0.37, 0.25), 1.9), "patina": ((0.37, 0.57, 0.50), 8.9), "ballast": ((0.41, 0.39, 0.36), None),
    "pier-ashlar": ((0.70, 0.66, 0.56), 2.6), "copper": ((0.72, 0.43, 0.24), 8.96), "slate": ((0.27, 0.29, 0.32), 2.8),
    "concrete": ((0.58, 0.57, 0.54), 2.4), "sand": ((0.76, 0.69, 0.52), None), "marking": ((0.86, 0.84, 0.76), None),
}


class MaterialError(ValueError):
    pass


def _parse_color(c) -> tuple[float, float, float]:
    if isinstance(c, str):
        h = c.lstrip("#")
        if len(h) != 6:
            raise MaterialError(f"color '{c}': use #rrggbb")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    if isinstance(c, (list, tuple)) and len(c) == 3:
        v = [float(x) for x in c]
        if max(v) > 1:
            v = [x / 255 for x in v]
        return tuple(v)  # type: ignore[return-value]
    raise MaterialError(f"color {c!r}: use '#rrggbb' or [r, g, b]")


class Palette:
    def __init__(self, spec_materials: dict | None):
        self.table = dict(BUILTIN)
        for name, d in (spec_materials or {}).items():
            if isinstance(d, (str, list)):
                d = {"color": d}
            if not isinstance(d, dict):
                raise MaterialError(f"materials.{name}: expected {{color: '#rrggbb', density: g/cm3}}")
            base = self.table.get(name, ((0.6, 0.6, 0.6), None))
            col = _parse_color(d["color"]) if "color" in d else base[0]
            self.table[name] = (col, float(d["density"]) if d.get("density") is not None else base[1])

    def check(self, name: str) -> str:
        if name not in self.table:
            hint = difflib.get_close_matches(name, self.table, n=3)
            raise MaterialError(f"unknown material '{name}'" + (f" (did you mean {hint}?)" if hint else "")
                                + "; declare it under top-level materials: {name: {color: '#rrggbb'}}")
        return name

    def color(self, name: str) -> tuple[float, float, float]:
        return self.table[name][0]

    def density(self, name: str) -> float | None:
        return self.table[name][1]

    def hex(self, name: str) -> str:
        return "#" + "".join(f"{round(c * 255):02x}" for c in self.color(name))


def _bb_overlap(a, b, tol=1e-6) -> bool:
    return (a.max.X >= b.min.X - tol and a.min.X <= b.max.X + tol and a.max.Y >= b.min.Y - tol
            and a.min.Y <= b.max.Y + tol and a.max.Z >= b.min.Z - tol and a.min.Z <= b.max.Z + tol)


def _union(shapes):
    acc = shapes[0]
    for x in shapes[1:]:
        acc = acc + x
    return acc


def partition(body, owners, default: str):
    """Split ``body`` into material regions.

    owners: [(material, tool_shape), ...] in feature order; a later owner wins where
    tools overlap. Material not covered by a non-default owner goes to ``default``.

    region_k = (body & tool_k) - union(later tools whose bbox touches tool_k); default
    owners never need their own intersection (default = complement), so big default
    features (the main block) cost nothing. Contiguous same-material owners are merged.
    Returns ({material: shape}, [visible volume per owner]).
    """
    n = len(owners)
    contrib = [0.0] * n
    bbs = [t.bounding_box() for _, t in owners]
    runs: list[tuple[str, list[int]]] = []
    for i, (mat, _) in enumerate(owners):
        if runs and runs[-1][0] == mat:
            runs[-1][1].append(i)
        else:
            runs.append((mat, [i]))
    regions: dict[str, list] = {}
    for mat, idxs in runs:
        if mat == default:
            continue
        tool = _union([owners[i][1] for i in idxs])
        part = body & tool
        if part is None or part.volume < 1e-9:
            continue
        pb = part.bounding_box()
        later = [owners[j][1] for j in range(idxs[-1] + 1, n) if _bb_overlap(bbs[j], pb)]
        if later:
            part = part - _union(later)
        if part is None or part.volume < 1e-9:
            continue
        regions.setdefault(mat, []).append(part)
        if len(idxs) == 1:
            contrib[idxs[0]] = part.volume
        else:  # attribute inside the run (later member wins)
            left = part
            for i in reversed(idxs):
                if left is None or left.volume < 1e-9:
                    break
                own = left & owners[i][1]
                contrib[i] = own.volume if own is not None else 0.0
                left = left - owners[i][1]
    out = {m: _union(ps) for m, ps in regions.items()}
    colored = list(out.values())
    rest = body - _union(colored) if colored else body
    if rest is not None and rest.volume > 1e-9:
        out[default] = rest
    for i, (mat, _) in enumerate(owners):  # default owners: visible if not fully overridden
        if mat == default:
            contrib[i] = 1.0  # not tracked precisely (default is the complement)
    return out, contrib


def partition_events(body, events, default: str):
    """Fast partition from the feature history (no booleans against the whole body
    unless a modifier/intersect follows a coloured feature).

    events: [(op, material|None, shape)] in feature order, op in add|paint|cut|modifier|intersect.
      add   -> shape = tool (all of it became material)
      paint -> shape = body ∩ tool at that moment
      cut   -> shape = removed material
    region_k = shape_k - later cuts - later owners (bbox-filtered). Exact because material
    present after step k can only disappear through later cuts/modifiers.
    """
    n = len(events)
    owners = [i for i, e in enumerate(events) if e[0] in ("add", "paint")]
    bbs = {i: events[i][2].bounding_box() for i in range(n) if events[i][2] is not None}
    contrib = {i: 0.0 for i in owners}
    regions: dict[str, list] = {}
    # group contiguous owners of the same material (cuts in between break a run)
    runs: list[tuple[str, list[int]]] = []
    prev = None
    for i in range(n):
        op, mat, _ = events[i]
        if op in ("add", "paint"):
            m = mat or default
            if runs and prev == "owner" and runs[-1][0] == m:
                runs[-1][1].append(i)
            else:
                runs.append((m, [i]))
            prev = "owner"
        else:
            prev = op
    for mat, idxs in runs:
        if mat == default:
            continue
        last = idxs[-1]
        exact = any(events[j][0] in ("modifier", "intersect") for j in range(idxs[0] + 1, n))
        part = _union([events[i][2] for i in idxs])
        if exact:
            part = body & part
        else:
            pb = part.bounding_box()
            cuts = [events[j][2] for j in range(idxs[0] + 1, n)
                    if events[j][0] == "cut" and events[j][2] is not None and _bb_overlap(bbs[j], pb)]
            if cuts:
                part = part - _union(cuts)
        if part is None or part.volume < 1e-9:
            continue
        pb = part.bounding_box()
        later = [events[j][2] for j in range(last + 1, n)
                 if events[j][0] in ("add", "paint") and _bb_overlap(bbs[j], pb)]
        if later:
            part = part - _union(later)
        if part is None or part.volume < 1e-9:
            continue
        regions.setdefault(mat, []).append(part)
        if len(idxs) == 1:
            contrib[idxs[0]] = part.volume
        else:
            left = part
            for i in reversed(idxs):
                if left is None or left.volume < 1e-9:
                    break
                own = left & events[i][2]
                contrib[i] = own.volume if own is not None else 0.0
                left = left - events[i][2]
    out = {m: _union(ps) for m, ps in regions.items()}
    colored = list(out.values())
    rest = body - _union(colored) if colored else body
    if rest is not None and rest.volume > 1e-9:
        out[default] = rest
    for i in owners:
        if (events[i][1] or default) == default:
            contrib[i] = 1.0
    return out, [contrib[i] for i in owners]
