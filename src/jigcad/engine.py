"""Deterministic feature-tree executor + per-feature self checks.

The spec (YAML dict) is executed top to bottom. Every feature is validated
right after it is applied (cut really removed material, through-holes really
pierce, adds are attached, no wall breakout, min wall). The first hard
failure stops the build; earlier features stay valid and are reported.
"""
from __future__ import annotations

import difflib
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from build123d import Plane, Vector

from . import expr as X
from . import standards
from .components import ParamError, get, load_all, validate_params
from .geom import EPS, bbox_anchors, bbox_dict, classify_points, diag, is_manifold, place, ray_hits

TOP_KEYS = {"part", "component", "kind", "summary", "units", "process", "material", "materials", "params",
            "features", "checks", "export", "description", "interface"}
FEATURE_KEYS = {"id", "use", "with", "on", "at", "rotate", "frame_rotate", "flip", "repeat", "op", "when", "note",
                "material"}
MAX_FEATURES = 200
MAX_INSTANCES = 400
MAX_NESTING = 4
VOL_EPS = 1e-6

DENSITY = {"pla": 1.24, "petg": 1.27, "abs": 1.04, "asa": 1.07, "nylon": 1.14, "resin": 1.18,
           "aluminium": 2.70, "aluminum": 2.70, "steel": 7.85, "stainless": 8.0, "brass": 8.5, "pom": 1.41}


class SpecError(ValueError):
    pass


def _make_loader():
    import re
    import yaml

    class L(yaml.SafeLoader):
        pass
    # YAML 1.1 treats on/off/yes/no as booleans -> 'on:' would become True.
    L.yaml_implicit_resolvers = {k: [r for r in v if r[0] != "tag:yaml.org,2002:bool"]
                                 for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    L.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                            list("tTfF"))
    return L


def yaml_load(text: str):
    import yaml
    return yaml.load(text, Loader=_make_loader())  # noqa: S506 (SafeLoader subclass)


# --------------------------------------------------------------------------- records

class FeatureRef:
    """Read-only view of a built feature, usable in expressions: ``=base.xmax - 5``."""
    _ATTRS = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax", "size_x", "size_y", "size_z",
              "cx", "cy", "cz", "points", "x", "y", "z", "volume", "n")

    def __init__(self, bb, points, volume):
        self.xmin, self.ymin, self.zmin = bb.min.X, bb.min.Y, bb.min.Z
        self.xmax, self.ymax, self.zmax = bb.max.X, bb.max.Y, bb.max.Z
        self.size_x, self.size_y, self.size_z = bb.size.X, bb.size.Y, bb.size.Z
        c = bb.center()
        self.cx, self.cy, self.cz = c.X, c.Y, c.Z
        self.points = [list(p) for p in points]
        self.n = len(points)
        self.x, self.y, self.z = (points[0] if points else (self.cx, self.cy, self.cz))
        self.volume = volume

    def expr_attrs(self):
        return self._ATTRS


@dataclass
class Issue:
    level: str          # error | warn
    code: str           # machine readable
    where: str          # feature id or check name
    msg: str
    fix: str = ""

    def as_dict(self):
        d = {"level": self.level, "code": self.code, "where": self.where, "msg": self.msg}
        if self.fix:
            d["fix"] = self.fix
        return d


@dataclass
class FeatureRecord:
    id: str
    use: str
    kind: str
    op: str
    shape: Any = None                 # tool (cut/add) or created body (base) or removed material
    anchors: dict = field(default_factory=dict)
    points: list = field(default_factory=list)
    instances: int = 1
    vol_before: float = 0.0
    vol_after: float = 0.0
    ms: float = 0.0
    skipped: bool = False
    info: dict = field(default_factory=dict)
    material: str | None = None       # explicit material of an add/base feature

    def summary(self) -> dict:
        d = {"id": self.id, "use": self.use, "op": self.op, "n": self.instances,
             "dV": round(self.vol_after - self.vol_before, 3)}
        if self.material:
            d["material"] = self.material
        if self.shape is not None:
            d["bbox"] = bbox_dict(self.shape.bounding_box())
        if self.points:
            d["points"] = self.points[:8] + (["..."] if len(self.points) > 8 else [])
        if self.skipped:
            d["skipped"] = True
        return d


@dataclass
class BuildResult:
    name: str
    spec: dict
    params: dict
    body: Any = None
    features: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    checks: list = field(default_factory=list)      # (name, ok, detail)
    props: dict = field(default_factory=dict)
    failed_at: str | None = None
    ms: float = 0.0
    n_features: int = 0
    regions: dict = field(default_factory=dict)     # material -> shape (multi-material parts)
    palette: Any = None
    default_material: str = "default"

    @property
    def ok(self) -> bool:
        return self.body is not None and not any(i.level == "error" for i in self.issues)

    def first_error(self) -> str:
        for i in self.issues:
            if i.level == "error":
                return f"[{i.where}] {i.msg}"
        return ""


class Ctx:
    """Passed to component builders."""

    def __init__(self, plane, body, result: BuildResult, base_dir: Path, process: str, depth: int):
        self.plane = plane
        self.body = body
        self._result = result
        self.base_dir = base_dir
        self.process = process
        self.depth = depth
        bb = body.bounding_box() if body is not None else None
        self.through = (diag(bb) * 2 + 10) if bb else 1000.0

    def next_depth(self) -> float:
        """Distance from the anchor along -normal to where the first material run ends."""
        o = self.plane.origin + self.plane.z_dir * EPS
        hits = ray_hits(self.body, o, -self.plane.z_dir)
        inside = False
        for t, entering in hits:
            if entering:
                inside = True
            elif inside or t > 0:
                # ray starts EPS above the plane: exit depth is t-EPS, plus EPS overshoot below
                return t
        raise ParamError("depth: next found no material below this anchor")

    def feature(self, fid: str) -> FeatureRecord:
        if fid not in self._result.features:
            raise ParamError(f"unknown feature '{fid}'")
        return self._result.features[fid]


# --------------------------------------------------------------------------- helpers

def _dist(a, b):
    return math.dist([float(x) for x in a], [float(x) for x in b])


def _namespace(res: BuildResult, body) -> dict:
    ns = dict(res.params)
    for fid, rec in res.features.items():
        if rec.shape is not None:
            ns[fid] = FeatureRef(rec.shape.bounding_box(), rec.points, rec.vol_after - rec.vol_before)
    if body is not None:
        ns["body"] = FeatureRef(body.bounding_box(), [], _vol(body))
    return ns


X.FUNCS["dist"] = _dist


def _resolve(v, ns, where):
    try:
        return X.resolve(v, ns)
    except X.ExprError as e:
        raise SpecError(f"{where}: {e}") from e
    except (ZeroDivisionError, TypeError, ValueError) as e:
        raise SpecError(f"{where}: cannot evaluate {v!r}: {e}") from e


def _plane_from(on, res: BuildResult, body, kind: str) -> Plane:
    if on is None:
        if kind == "base" and body is None:
            return Plane.XY
        on = "body.top"
    if isinstance(on, dict):
        return Plane(origin=on.get("origin", (0, 0, 0)), x_dir=on.get("x_dir", (1, 0, 0)),
                     z_dir=on.get("normal", (0, 0, 1)))
    s = str(on)
    worlds = {"world": Plane.XY, "XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}
    if s in worlds:
        return worlds[s]
    if "." not in s:
        raise SpecError(f"on='{s}': expected '<feature>.<anchor>', 'body.<face>' or world|XY|XZ|YZ")
    fid, an = s.split(".", 1)
    if fid == "body":
        if body is None:
            raise SpecError("on='body.*' but there is no body yet; first feature must be a base component")
        anchors = bbox_anchors(body.bounding_box())
    else:
        if fid not in res.features:
            hint = difflib.get_close_matches(fid, res.features, n=1)
            raise SpecError(f"on='{s}': unknown feature '{fid}'" + (f" (did you mean '{hint[0]}'?)" if hint else "")
                            + f"; defined so far: {', '.join(res.features) or 'none'}")
        anchors = res.features[fid].anchors
    if an not in anchors:
        raise SpecError(f"on='{s}': no anchor '{an}'; available: {', '.join(sorted(anchors))}")
    return anchors[an]


def _instances(rep, at) -> list[tuple[list[float], float]]:
    """Instance positions ([u, v, w] in the anchor frame) and extra spin (deg).

    grid / polar / points / linear generate offsets added to ``at``; ``mirror``
    (alone or combined with one of them) then mirrors every position about the
    anchor origin.
    """
    a = [float(x) for x in (at or [0, 0])]
    a += [0.0] * (3 - len(a))
    rep = rep or {}
    gens = [k for k in ("grid", "polar", "points", "linear") if k in rep]
    unknown = set(rep) - {"grid", "polar", "points", "linear", "mirror"}
    if unknown or len(gens) > 1:
        raise SpecError("repeat: use one of grid|polar|points|linear, optionally with mirror")
    offs: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0)]
    if "grid" in rep:
        g = rep["grid"]
        nx, ny = int(g.get("nx", 1)), int(g.get("ny", 1))
        dx, dy = float(g.get("dx", 0)), float(g.get("dy", 0))
        offs = [((i - (nx - 1) / 2) * dx, (j - (ny - 1) / 2) * dy, 0.0, 0.0) for j in range(ny) for i in range(nx)]
    elif "polar" in rep:
        g = rep["polar"]
        n, r = int(g["n"]), float(g["r"])
        start, sweep = float(g.get("start", 0)), float(g.get("sweep", 360))
        step = sweep / n if abs(sweep - 360) < 1e-9 else (sweep / (n - 1) if n > 1 else 0)
        orient = bool(g.get("orient", False))
        offs = []
        for k in range(n):
            t = start + k * step
            offs.append((r * math.cos(math.radians(t)), r * math.sin(math.radians(t)), 0.0, t if orient else 0.0))
    elif "points" in rep:
        offs = [(float(p[0]), float(p[1]), 0.0, float(p[2]) if len(p) > 2 else 0.0) for p in rep["points"]]
    elif "linear" in rep:
        g = rep["linear"]
        n = int(g["n"])
        st = [float(x) for x in g["step"]] + [0.0] * (3 - len(g["step"]))
        offs = [(st[0] * k, st[1] * k, st[2] * k, 0.0) for k in range(n)]
    pos = [([a[0] + du, a[1] + dv, a[2] + dw], dr) for du, dv, dw, dr in offs]
    if "mirror" in rep:
        m = str(rep["mirror"])
        signs = {"x": [(1, 1), (-1, 1)], "y": [(1, 1), (1, -1)],
                 "xy": [(1, 1), (-1, 1), (1, -1), (-1, -1)]}.get(m)
        if signs is None:
            raise SpecError("repeat.mirror must be x|y|xy (mirrors positions about the anchor origin)")
        pos = [([p[0] * sx, p[1] * sy, p[2]], dr) for sx, sy in signs for p, dr in pos]
    return pos


def _union(shapes):
    acc = shapes[0]
    for s in shapes[1:]:
        acc = acc + s
    return acc


_VOLS: dict = {}


def _vol(s) -> float:
    """Volume with a per-build cache (big bodies are queried many times)."""
    if s is None:
        return 0.0
    key = id(s)
    hit = _VOLS.get(key)
    if hit is not None and hit[0] is s:
        return hit[1]
    v = s.volume
    if len(_VOLS) > 4096:
        _VOLS.clear()
    _VOLS[key] = (s, v)
    return v


# --------------------------------------------------------------------------- per-feature checks

def _check_cut(res, fid, pre, post, axis_outs, min_wall):
    issues = res.issues
    for k, (out, tool) in enumerate(axis_outs):
        tag = fid if len(axis_outs) == 1 else f"{fid}[{k}]"
        rem_shape = pre & tool
        removed = _vol(rem_shape)
        if removed >= VOL_EPS and out.axis:
            ax_ = Vector(*out.axis)
            proj = [v.center().dot(ax_) if hasattr(v, "center") else Vector(v).dot(ax_) for v in rem_shape.vertices()]
            if proj and max(proj) - min(proj) <= 2.5 * EPS:
                removed = 0.0  # only the overshoot skin was cut: the tool points away from the body
        if removed < VOL_EPS:
            bb = pre.bounding_box()
            tb = tool.bounding_box()
            issues.append(Issue("error", "cut_missed", tag,
                                f"cut removed 0 mm3: tool bbox {bbox_dict(tb)['min']}..{bbox_dict(tb)['max']} "
                                f"does not intersect body {bbox_dict(bb)['min']}..{bbox_dict(bb)['max']}",
                                "check on/at/flip: cut tools go INTO the face along -normal"))
            continue
        if out.through and out.axis and out.points:
            ax = Vector(*out.axis)
            L = min(diag(pre.bounding_box()), out.depth or 1e9)
            for p in out.points:
                p0 = Vector(*p)
                pts = [p0 + ax * (L * (i + 0.5) / 40) for i in range(40)]
                if any(classify_points(post, pts)):
                    issues.append(Issue("error", "not_through", tag,
                                        f"'through' cut at {list(p)} does not pierce the body along {list(out.axis)}",
                                        "hole axis hits material: wrong face/flip, or a later feature refills it"))
                    break
            if out.depth and out.depth >= diag(pre.bounding_box()):

                p0 = Vector(*out.points[0]) - ax * EPS
                runs = sum(1 for _, ent in ray_hits(pre, p0, ax) if ent)
                if runs > 1:
                    issues.append(Issue("warn", "multi_wall", tag,
                                        f"through-all cut pierces {runs} separate walls along {list(out.axis)}",
                                        "if only the first wall is intended use depth: next"))
        _wall_and_breakout(res, tag, pre, tool, out, min_wall, rem_shape)


def _wall_and_breakout(res, tag, pre, tool, out, min_wall, removed=None):
    if not out.axis:
        return
    if removed is None:
        removed = pre & tool      # no overshoot: closest points stay inside the part
    if removed is None or _vol(removed) < VOL_EPS:
        return
    # only faces near the cut can make a thin wall or a breakout: bbox prefilter
    rb = removed.bounding_box()
    m = max(3 * min_wall, 5.0)
    lo = (rb.min.X - m, rb.min.Y - m, rb.min.Z - m)
    hi = (rb.max.X + m, rb.max.Y + m, rb.max.Z + m)
    faces = []
    for f in pre.faces():
        fb = f.bounding_box()
        if (fb.max.X >= lo[0] and fb.min.X <= hi[0] and fb.max.Y >= lo[1] and fb.min.Y <= hi[1]
                and fb.max.Z >= lo[2] and fb.min.Z <= hi[2]):
            faces.append(f)
    if len(faces) > 400:
        return
    ax = Vector(*out.axis)

    def material_after(pts):  # in the part and not removed by this tool (no big boolean)
        a = classify_points(pre, pts)
        b = classify_points(tool, pts)
        return [x and not y for x, y in zip(a, b)]
    min_d, min_info = None, None
    for f in faces:
        try:
            d, pt_tool, pt_face = removed.distance_to_with_closest_points(f)
        except Exception:  # noqa: BLE001
            continue
        if d < 1e-4:
            if out.allow_breakout:
                continue
            try:
                n = f.normal_at(pt_face)
            except Exception:  # noqa: BLE001
                continue
            if abs(abs(n.dot(ax)) - 1) > 1e-3:
                # tool touches a face that is not perpendicular to the cut axis
                # -> side wall opened. Ignore curved faces that are coaxial
                # boss/hole walls of earlier cuts only if tangent.
                res.issues.append(Issue("error", "breakout", tag,
                                        f"cut breaks through a side face near {[round(c, 2) for c in pt_face]}",
                                        "move it inward / reduce size, or set breakout:true / use notch if intended"))
                return
        else:
            mid = (pt_tool + pt_face) * 0.5
            if not any(material_after([mid, mid + ax * 0.02, mid - ax * 0.02])):
                continue  # gap through air, not a wall
            if min_d is None or d < min_d:
                min_d, min_info = d, pt_face
    if min_d is not None:
        res.features[tag.split("[")[0]].info.setdefault("min_wall", round(min_d, 3))
        cur = res.features[tag.split("[")[0]].info["min_wall"]
        res.features[tag.split("[")[0]].info["min_wall"] = round(min(cur, min_d), 3)
        if min_d < min_wall - 1e-6:
            res.issues.append(Issue("error", "thin_wall", tag,
                                    f"wall {min_d:.2f} mm < min_wall {min_wall} near {[round(c, 2) for c in min_info]}",
                                    "move/shrink the cut, thicken the part, or lower checks.min_wall"))


def _check_add(res, fid, pre, post, outs):
    """Per-instance add checks, cheap first: volume bookkeeping and point classification;
    distance / boolean queries against the (large) body only when something is wrong."""
    n_pre, n_post = len(pre.solids()), len(post.solids())
    split = n_post > n_pre
    man_pre = is_manifold(pre)
    man_post = is_manifold(post) if man_pre else True
    grown = _vol(post) - _vol(pre)
    tools = [_vol(t) for _, t in outs]
    if not split and man_post and abs(grown - sum(tools)) <= 1e-7 * max(sum(tools), 1.0) + 1e-6 and grown > VOL_EPS:
        return  # every instance attached, nothing overlaps the body: all good
    pbb = pre.bounding_box()
    for k, (out, tool) in enumerate(outs):
        tag = fid if len(outs) == 1 else f"{fid}[{k}]"
        if split:  # some instance is not connected: find which (distance only here)
            d = pre.distance_to(tool)
            if d > 1e-5:
                tb, bb = bbox_dict(tool.bounding_box()), bbox_dict(pbb)
                res.issues.append(Issue("error", "floating", tag,
                                        f"added material is {d:.3f} mm away from the body (floating): it spans "
                                        f"{list(tb['min'])}..{list(tb['max'])}, body {list(bb['min'])}..{list(bb['max'])}",
                                        "compare the spans to find the wrong axis/sign; note flip mirrors v; "
                                        "use on: <feature>.<face> so it sits on a real face"))
                continue
            if _vol(pre & tool) < VOL_EPS:
                res.issues.append(Issue("error", "touch_only", tag,
                                        "added material only touches the body along a line/point (no shared area), "
                                        "typical on curved faces",
                                        "sink it in: at: [u, v, -s] and add s to its height (s ≈ 2-5 mm)"))
                return
        # buried? screen with points (centre + bbox-interior samples), confirm with a boolean
        tb = tool.bounding_box()
        c = tb.center()
        probes = [c] + [Vector(c.X + sx * tb.size.X * 0.3, c.Y + sy * tb.size.Y * 0.3, c.Z + sz * tb.size.Z * 0.3)
                        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        probes = [p_ for p_, inside in zip(probes, classify_points(tool, probes)) if inside]
        if probes and all(classify_points(pre, probes)):
            if _vol(tool) - _vol(pre & tool) < VOL_EPS:
                res.issues.append(Issue("error", "add_buried", tag, "added material is completely inside the body",
                                        "it grows along +normal of the anchor: check flip / anchor face"))
    if split:
        res.issues.append(Issue("error", "disjoint", fid, f"body split into {n_post} solids after add"))
    elif man_pre and not man_post:
        res.issues.append(Issue("error", "non_manifold", fid,
                                "fusing this feature created a non-manifold edge/vertex (bodies touch along a "
                                "line or point, e.g. a wheel standing on a rail)",
                                "shift or resize it ~0.5 mm to overlap or to leave a real gap; moving parts "
                                "belong in separate parts"))


# --------------------------------------------------------------------------- build

def load_spec(path: Path) -> dict:
    import yaml
    try:
        data = yaml_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise SpecError(f"YAML error: {e}") from e
    if not isinstance(data, dict):
        raise SpecError("spec must be a mapping")
    return data


def _params(spec, overrides) -> dict:
    raw = dict(spec.get("params") or {})
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, dict):  # composite style declaration
            v = v.get("default")
        if k in (overrides or {}):
            v = overrides[k]
        out[k] = _resolve(v, out, f"params.{k}")
    for k in (overrides or {}):
        if k not in raw:
            hint = difflib.get_close_matches(k, raw, n=1)
            raise SpecError(f"override '{k}' is not a param" + (f" (did you mean '{hint[0]}'?)" if hint else ""))
    return out


def build_spec(spec: dict, overrides: dict | None = None, base_dir: Path | None = None,
               run_checks: bool = True, nested: int = 0, stop_after: str | None = None) -> BuildResult:
    t0 = time.perf_counter()
    base_dir = Path(base_dir or ".")
    load_all([base_dir / "components"])
    name = spec.get("part") or spec.get("component") or "part"
    res = BuildResult(name=name, spec=spec, params={})
    if nested > MAX_NESTING:
        res.issues.append(Issue("error", "nesting", name, "composite nesting too deep"))
        return res
    unknown = set(spec) - TOP_KEYS
    if unknown:
        res.issues.append(Issue("error", "spec", "spec", f"unknown top-level keys {sorted(unknown)}; allowed {sorted(TOP_KEYS)}"))
        return res
    process = spec.get("process", "generic")
    if process not in standards.PROCESS:
        res.issues.append(Issue("error", "spec", "process", f"process must be one of {list(standards.PROCESS)}"))
        return res
    try:
        res.params = _params(spec, overrides)
    except SpecError as e:
        res.issues.append(Issue("error", "param", "params", str(e)))
        return res
    checks_spec = spec.get("checks") or {}
    min_wall = float(checks_spec.get("min_wall", standards.PROCESS[process]["min_wall"]))
    from .materials import MaterialError, Palette
    try:
        res.palette = Palette(spec.get("materials"))
        res.default_material = res.palette.check(str(spec.get("material") or "default"))
    except MaterialError as e:
        res.issues.append(Issue("error", "material", "materials", str(e)))
        return res

    feats = spec.get("features") or []
    if not feats:
        res.issues.append(Issue("error", "spec", "features", "no features"))
        return res
    if len(feats) > MAX_FEATURES:
        res.issues.append(Issue("error", "spec", "features", f"more than {MAX_FEATURES} features"))
        return res
    res.n_features = len(feats)
    body = None
    for i, f in enumerate(feats):
        fid = str(f.get("id") or f"f{i}")
        tf = time.perf_counter()
        try:
            body = _apply_feature(res, f, fid, body, base_dir, process, nested, min_wall)
        except (SpecError, ParamError) as e:
            res.issues.append(Issue("error", "feature", fid, str(e)))
            res.failed_at = fid
            break
        except (TypeError, AttributeError, KeyError, IndexError, NameError) as e:  # bug, not geometry
            res.issues.append(Issue("error", "internal", fid, f"{type(e).__name__}: {str(e)[:200]}",
                                    "JigCAD component bug (not your geometry): report it; try another param form"))
            res.failed_at = fid
            break
        except Exception as e:  # noqa: BLE001  kernel failures
            res.issues.append(Issue("error", "kernel", fid, f"{type(e).__name__}: {str(e)[:200]}",
                                    "geometry kernel rejected this op; check sizes / tangencies / zero-thickness"))
            res.failed_at = fid
            break
        if fid in res.features:
            res.features[fid].ms = round((time.perf_counter() - tf) * 1000, 1)
        if any(x.level == "error" for x in res.issues):
            res.failed_at = fid
            break
        if stop_after and fid == stop_after:
            break
    res.body = body
    if body is not None:
        _global_checks(res, body, checks_spec, spec, run_checks and res.failed_at is None)
    res.ms = round((time.perf_counter() - t0) * 1000, 1)
    return res


def _apply_feature(res, f, fid, body, base_dir, process, nested, min_wall):
    if not isinstance(f, dict):
        raise SpecError("feature must be a mapping")
    bad = set(f) - FEATURE_KEYS
    if bad:
        hint = {b: difflib.get_close_matches(b, FEATURE_KEYS, n=1) for b in bad}
        raise SpecError(f"unknown feature keys {sorted(bad)} {hint}; allowed {sorted(FEATURE_KEYS)}")
    if fid in res.features:
        raise SpecError(f"duplicate feature id '{fid}'")
    if fid in res.params or fid in ("body", "dist"):
        raise SpecError(f"feature id '{fid}' clashes with a param/reserved name")
    if "use" not in f:
        raise SpecError("feature needs 'use: <component>'")
    comp = get(str(f["use"]))
    ns = _namespace(res, body)
    if "when" in f and not _resolve(f["when"], ns, f"{fid}.when"):
        res.features[fid] = FeatureRecord(fid, comp.name, comp.kind, "skip", skipped=True)
        return body
    op = f.get("op") or ("cut" if comp.kind == "cut" else "modifier" if comp.kind == "modifier" else "add")
    if op not in ("add", "cut", "modifier", "intersect", "paint"):
        raise SpecError("op must be add|cut|intersect|paint")
    if op == "paint" and (f.get("material") is None or body is None or comp.kind == "modifier"):
        raise SpecError("op: paint recolours existing material: needs material: and an existing body")
    if body is None and (op != "add" or comp.kind not in ("base", "add")):
        raise SpecError(f"first feature must create material (base component), got {comp.name} [{comp.kind}]")
    params = validate_params(comp, _resolve(f.get("with") or {}, ns, f"{fid}.with"))
    base_plane = _plane_from(_resolve(f.get("on"), ns, f"{fid}.on"), res, body, comp.kind)
    frot = float(_resolve(f.get("frame_rotate", 0), ns, f"{fid}.frame_rotate") or 0)
    if frot:  # rotate the whole local frame: at offsets, repeat patterns and the part itself
        base_plane = place(base_plane, None, frot)
    at = _resolve(f.get("at"), ns, f"{fid}.at")
    rot = float(_resolve(f.get("rotate", 0), ns, f"{fid}.rotate") or 0)
    rep = _resolve(f.get("repeat"), ns, f"{fid}.repeat")
    offs = _instances(rep, at)
    if len(offs) > MAX_INSTANCES:
        raise SpecError(f"repeat produces {len(offs)} instances > {MAX_INSTANCES}")
    rec = FeatureRecord(fid, comp.name, comp.kind, op, instances=len(offs), vol_before=_vol(body))
    if f.get("material") is not None:
        if op not in ("add", "paint"):
            raise SpecError(f"material: only on features that add material or op: paint (this one is {op}); "
                            "to recolour existing material use op: paint")
        from .materials import MaterialError
        try:
            rec.material = res.palette.check(str(_resolve(f["material"], ns, f"{fid}.material")))
        except MaterialError as e:
            raise SpecError(str(e)) from e
    res.features[fid] = rec  # register early so checks can attach info

    if comp.kind == "modifier":
        ctx = Ctx(base_plane, body, res, base_dir, process, nested)
        out = comp.build(ctx, params)
        new = out.shape
        if new is None or _vol(new) < VOL_EPS:
            raise SpecError("modifier produced empty body")
        rec.shape, rec.vol_after, rec.info = new, _vol(new), out.info
        rec.anchors = bbox_anchors(new.bounding_box())
        return new

    outs = []
    for a, drot in offs:
        pl = place(base_plane, a, rot + drot, bool(f.get("flip")))
        ctx = Ctx(pl, body, res, base_dir, process, nested)
        out = comp.build(ctx, params)
        if not out.points:  # every instance is at least locatable by its anchor origin
            o = pl.origin
            out.points = [(round(o.X, 4), round(o.Y, 4), round(o.Z, 4))]
        outs.append((out, out.shape))
    tool = _union([o.shape for o, _ in outs])
    post_cuts = [o.info["post_cut"] for o, _ in outs if "post_cut" in o.info]

    if body is None:
        new = tool
    elif op == "add":
        new = body + tool
        _check_add(res, fid, body, new, outs)
    elif op == "intersect":
        new = body & tool
    elif op == "paint":  # geometry unchanged; tool only claims material for a colour region
        new = body
        for k, (o_, t_) in enumerate(outs):
            if _vol(body & t_) < VOL_EPS:
                tag = fid if len(outs) == 1 else f"{fid}[{k}]"
                res.issues.append(Issue("error", "paint_missed", tag, "paint volume does not overlap the part",
                                        "move it onto the material you want to recolour (check on/at/flip)"))
    else:
        new = body - tool
        _check_cut(res, fid, body, new, outs, min_wall)
    for pc in post_cuts:
        new = new - pc
    if new is None or _vol(new) < VOL_EPS:
        raise SpecError(f"{op} produced an empty body")

    first = outs[0][0]
    if op == "cut":
        removed = body & tool
        rec.shape = removed if _vol(removed) > VOL_EPS else tool
    elif op == "paint":
        painted = body & tool
        rec.shape = painted if _vol(painted) > VOL_EPS else tool
    else:
        rec.shape = tool
    rec.anchors = bbox_anchors(rec.shape.bounding_box())
    if len(outs) == 1:
        rec.anchors.update(first.anchors)
    else:
        for k, (o, _) in enumerate(outs):
            for an, pl in o.anchors.items():
                rec.anchors[f"{an}{k}"] = pl
        rec.anchors.update(first.anchors)
    rec.points = [p for o, _ in outs for p in o.points]
    rec.info.update({k: v for k, v in first.info.items() if k != "post_cut"})
    rec.vol_after = _vol(new)
    return new


# --------------------------------------------------------------------------- global checks

def _num_close(a, b, tol):
    return abs(float(a) - float(b)) <= tol


def _global_checks(res: BuildResult, body, cs: dict, spec: dict, run_checks: bool):
    bb = body.bounding_box()
    solids = body.solids()
    props = {"volume": round(_vol(body), 3), "area": round(body.area, 3), "bbox": bbox_dict(bb),
             "solids": len(solids), "faces": len(body.faces()), "edges": len(body.edges())}
    try:
        com = body.center()
        props["com"] = [round(com.X, 3), round(com.Y, 3), round(com.Z, 3)]
    except Exception:  # noqa: BLE001
        pass
    import hashlib
    verts = sorted((round(v.X, 3), round(v.Y, 3), round(v.Z, 3)) for v in body.vertices())
    props["geom_hash"] = hashlib.sha1(repr(verts).encode()).hexdigest()[:10]  # position-sensitive
    pal = res.palette
    default = res.default_material
    owner_recs = [f for f in res.features.values() if f.op in ("add", "paint") and not f.skipped and f.shape is not None]
    owners = [(f.material or default, f.shape) for f in owner_recs]
    used = {m for m, _ in owners}
    contrib = []
    if len(used) > 1 or (used and used != {default}):
        from .materials import partition, partition_events
        events = [(f.op, f.material, f.shape) for f in res.features.values()
                  if not f.skipped and f.op in ("add", "paint", "cut", "modifier", "intersect")]
        res.regions, contrib = partition_events(body, events, default)
        total = sum(r_.volume for r_ in res.regions.values())
        if abs(total - _vol(body)) > 1e-6 * max(_vol(body), 1.0) + 1e-6:  # safety net: exact method
            res.regions, contrib = partition(body, owners, default)
    def _dens(m):
        d = pal.density(m) if pal else None
        if d is None and pal:
            d = pal.density(default)
        return d if d is not None else DENSITY.get(str(spec.get("material", "")).lower())
    if res.regions:
        mats = {}
        mass = 0.0
        for m, shp in res.regions.items():
            v = shp.volume
            d = _dens(m)
            mats[m] = {"color": pal.hex(m), "volume": round(v, 3), "pct": round(100 * v / _vol(body), 2)}
            if d:
                mats[m]["mass_g"] = round(v / 1000 * d, 2)
                mass += v / 1000 * d
        props["materials"] = mats
        if mass and all("mass_g" in d for d in mats.values()):
            props["mass_g"] = round(mass, 2)
    else:
        d = _dens(default)
        if d:
            props["mass_g"] = round(_vol(body) / 1000 * d, 2)
    res.props = props
    if not run_checks:
        return

    def chk(name, ok, detail, fix="", level="error"):
        res.checks.append((name, bool(ok), detail))
        if not ok:
            res.issues.append(Issue(level, "check", name, detail, fix))

    chk("valid", body.is_valid, "B-Rep validity (BRepCheck)", "a boolean produced bad geometry; simplify the last feature")
    chk("manifold", is_manifold(body), "closed manifold solids")
    want = int(cs.get("solids", 1))
    chk("solids", len(solids) == want, f"{len(solids)} solid(s), expected {want}",
        "disconnected pieces: a cut split the part or an add is floating")

    ns = _namespace(res, body)
    ev = lambda v, w: _resolve(v, ns, f"checks.{w}")  # noqa: E731
    if "bbox" in cs:
        b = cs["bbox"]
        tol = float(ev(b.get("tol", 0.01), "bbox.tol"))
        if "size" in b:
            exp = ev(b["size"], "bbox.size")
            for ax, got, e in zip("xyz", (bb.size.X, bb.size.Y, bb.size.Z), exp):
                if e is None:
                    continue
                chk(f"bbox.size_{ax}", _num_close(got, e, tol), f"{got:.4f} vs {float(e):.4f} (tol {tol})",
                    "overall size wrong: check base dimensions / features protruding")
        for key, vec_ in (("min", bb.min), ("max", bb.max)):
            if key in b:
                exp = ev(b[key], f"bbox.{key}")
                for ax, got, e in zip("xyz", (vec_.X, vec_.Y, vec_.Z), exp):
                    if e is None:
                        continue
                    chk(f"bbox.{key}_{ax}", _num_close(got, e, tol), f"{got:.4f} vs {float(e):.4f} (tol {tol})")
    if "volume" in cs:
        v = cs["volume"]
        if isinstance(v, dict) and "expect" in v:
            e = float(ev(v["expect"], "volume.expect"))
            tp = float(v.get("tol_pct", 0.5))
            chk("volume", abs(_vol(body) - e) <= abs(e) * tp / 100, f"{_vol(body):.3f} vs {e:.3f} (±{tp}%)")
        elif isinstance(v, dict):
            lo, hi = ev(v.get("min", 0), "volume.min"), ev(v.get("max", float("inf")), "volume.max")
            chk("volume", lo <= _vol(body) <= hi, f"{_vol(body):.3f} in [{lo}, {hi}]")
    if "com" in cs:
        e = ev(cs["com"]["expect"], "com.expect")
        tol = float(cs["com"].get("tol", 0.1))
        got = props.get("com", [0, 0, 0])
        chk("com", all(x is None or _num_close(g, x, tol) for g, x in zip(got, e)), f"{got} vs {e} (tol {tol})")
    for k, item in enumerate(cs.get("dims") or []):
        nm = item.get("name", f"dim{k}")
        lvl = "warn" if item.get("level") == "warn" else "error"
        fix = str(item.get("fix", ""))
        try:
            got = float(ev(item["value"], f"dims.{nm}"))
            e = ev(item["expect"], f"dims.{nm}") if "expect" in item else None
            lo = ev(item["min"], f"dims.{nm}") if "min" in item else None
            hi = ev(item["max"], f"dims.{nm}") if "max" in item else None
        except (SpecError, KeyError) as err:
            chk(f"dim:{nm}", False, f"bad dims entry: {err} (needs value + expect | min | max)")
            continue
        if e is None and lo is None and hi is None:
            chk(f"dim:{nm}", False, "dims entry needs expect, min or max")
            continue
        tol = float(item.get("tol", 0.01))
        if e is not None:
            chk(f"dim:{nm}", _num_close(got, e, tol), f"{got:.4f} vs {float(e):.4f} (tol {tol})", fix, lvl)
        else:
            ok = (lo is None or got >= float(lo) - 1e-9) and (hi is None or got <= float(hi) + 1e-9)
            rng = f"[{'-inf' if lo is None else f'{float(lo):.4f}'}, {'inf' if hi is None else f'{float(hi):.4f}'}]"
            chk(f"dim:{nm}", ok, f"{got:.4f} not in {rng}" if not ok else f"{got:.4f} in {rng}", fix, lvl)
    # ---- multi-material checks
    for rec, vol in zip(owner_recs, contrib):
        if rec.material and vol <= 1e-9:
            res.issues.append(Issue("warn", "material_hidden", rec.id,
                                    f"material '{rec.material}' of this feature is not visible in the final part "
                                    "(covered by later features or cut away)",
                                    "later features win where they overlap: reorder, or drop the material"))
    if "materials" in cs:
        want = cs["materials"]
        n = len(res.regions) or 1
        if isinstance(want, dict):
            lo, hi = int(want.get("min", 0)), int(want.get("max", 10 ** 6))
            chk("materials", lo <= n <= hi, f"{n} material(s), expected [{lo}, {hi}]")
        else:
            chk("materials", n == int(want), f"{n} material(s), expected {int(want)}")
    min_island = float(cs.get("min_island", 1.0))
    for m, shp in res.regions.items():
        small = [s_.volume for s_ in shp.solids() if s_.volume < min_island]
        if small:
            res.issues.append(Issue("warn", "material_island", m,
                                    f"{len(small)} tiny '{m}' island(s) < {min_island:g} mm3 (unprintable colour specks)",
                                    "enlarge/merge them or give them the neighbouring material"))
    for item in ev(cs.get("material_at") or [], "material_at"):
        p_, want_m = item[:3], str(item[3])
        got = res.default_material
        if res.regions:
            got = None
            for m, shp in res.regions.items():
                if classify_points(shp, [Vector(*p_)])[0]:
                    got = m
                    break
        elif not classify_points(body, [Vector(*p_)])[0]:
            got = None
        chk(f"material_at{list(p_)}", got == want_m, f"{got or 'empty'} at {list(p_)}, expected {want_m}",
            "colour assignment differs from intent: check which feature covers this point last")

    for kind in ("solid_at", "empty_at"):
        pts = ev(cs.get(kind) or [], kind)
        if pts:
            inside = classify_points(body, [Vector(*p) for p in pts])
            for p, ins in zip(pts, inside):
                ok = ins if kind == "solid_at" else not ins
                chk(f"{kind}{list(p)}", ok, ("material" if ins else "empty") + f" at {list(p)}",
                    "probe point disagrees with intent: feature missing or mis-placed")
