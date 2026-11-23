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
from .geom import EPS, bbox_anchors, bbox_dict, classify_points, diag, place, ray_hits

TOP_KEYS = {"part", "component", "kind", "summary", "units", "process", "material", "params",
            "features", "checks", "export", "description", "interface"}
FEATURE_KEYS = {"id", "use", "with", "on", "at", "rotate", "flip", "repeat", "op", "when", "note"}
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

    def summary(self) -> dict:
        d = {"id": self.id, "use": self.use, "op": self.op, "n": self.instances,
             "dV": round(self.vol_after - self.vol_before, 3)}
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
        ns["body"] = FeatureRef(body.bounding_box(), [], body.volume)
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


def _offsets(rep) -> list[tuple[float, float, float]]:
    """Return (du, dv, rot_deg) instance offsets in the anchor plane."""
    if not rep:
        return [(0.0, 0.0, 0.0)]
    if "grid" in rep:
        g = rep["grid"]
        nx, ny = int(g.get("nx", 1)), int(g.get("ny", 1))
        dx, dy = float(g.get("dx", 0)), float(g.get("dy", 0))
        return [((i - (nx - 1) / 2) * dx, (j - (ny - 1) / 2) * dy, 0.0) for j in range(ny) for i in range(nx)]
    if "polar" in rep:
        g = rep["polar"]
        n, r = int(g["n"]), float(g["r"])
        start, sweep = float(g.get("start", 0)), float(g.get("sweep", 360))
        step = sweep / n if abs(sweep - 360) < 1e-9 else (sweep / (n - 1) if n > 1 else 0)
        orient = bool(g.get("orient", False))
        out = []
        for k in range(n):
            a = start + k * step
            out.append((r * math.cos(math.radians(a)), r * math.sin(math.radians(a)), a if orient else 0.0))
        return out
    if "mirror" in rep:
        m = str(rep["mirror"])
        base = [(1, 1)]
        if m == "x":
            base = [(1, 1), (-1, 1)]
        elif m == "y":
            base = [(1, 1), (1, -1)]
        elif m == "xy":
            base = [(1, 1), (-1, 1), (1, -1), (-1, -1)]
        else:
            raise SpecError("repeat.mirror must be x|y|xy (mirrors the 'at' offset about the anchor origin)")
        return [(sx, sy, 0.0) for sx, sy in base]  # handled specially (multiplies at)
    if "points" in rep:
        return [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0) for p in rep["points"]]
    raise SpecError("repeat must contain grid|polar|mirror|points")


def _union(shapes):
    acc = shapes[0]
    for s in shapes[1:]:
        acc = acc + s
    return acc


def _vol(s) -> float:
    return s.volume if s is not None else 0.0


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
        _wall_and_breakout(res, tag, pre, tool, out, min_wall)


def _wall_and_breakout(res, tag, pre, tool, out, min_wall):
    faces = list(pre.faces())
    if len(faces) > 400 or not out.axis:
        return
    ax = Vector(*out.axis)
    solid_after = pre - tool
    removed = pre & tool          # no overshoot: closest points stay inside the part
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
            if not any(classify_points(solid_after, [mid, mid + ax * 0.02, mid - ax * 0.02])):
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
    for k, (out, tool) in enumerate(outs):
        tag = fid if len(outs) == 1 else f"{fid}[{k}]"
        d = pre.distance_to(tool)
        if d > 1e-5:
            res.issues.append(Issue("error", "floating", tag,
                                    f"added material is {d:.3f} mm away from the body (floating)",
                                    "use on: <feature>.<face> so it sits on a real face; check at/flip"))
            continue
        added = _vol(tool) - _vol(pre & tool)
        if added < VOL_EPS:
            res.issues.append(Issue("error", "add_buried", tag, "added material is completely inside the body",
                                    "it grows along +normal of the anchor: check flip / anchor face"))
    if len(post.solids()) > len(pre.solids()):
        res.issues.append(Issue("error", "disjoint", fid, f"body split into {len(post.solids())} solids after add"))


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
    if op not in ("add", "cut", "modifier", "intersect"):
        raise SpecError("op must be add|cut|intersect")
    if body is None and (op != "add" or comp.kind not in ("base", "add")):
        raise SpecError(f"first feature must create material (base component), got {comp.name} [{comp.kind}]")
    params = validate_params(comp, _resolve(f.get("with") or {}, ns, f"{fid}.with"))
    base_plane = _plane_from(_resolve(f.get("on"), ns, f"{fid}.on"), res, body, comp.kind)
    at = _resolve(f.get("at"), ns, f"{fid}.at")
    rot = float(_resolve(f.get("rotate", 0), ns, f"{fid}.rotate") or 0)
    rep = _resolve(f.get("repeat"), ns, f"{fid}.repeat")
    offs = _offsets(rep)
    if len(offs) > MAX_INSTANCES:
        raise SpecError(f"repeat produces {len(offs)} instances > {MAX_INSTANCES}")
    rec = FeatureRecord(fid, comp.name, comp.kind, op, instances=len(offs), vol_before=_vol(body))
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
    mirror = rep and "mirror" in rep
    for du, dv, drot in offs:
        if mirror:
            a = list(at or [0, 0])
            a[0], a[1] = float(a[0]) * du, float(a[1]) * dv
            pl = place(base_plane, a, rot, bool(f.get("flip")))
        else:
            a = list(at or [0, 0])
            a = [float(a[0]) + du, float(a[1]) + dv] + ([float(a[2])] if len(a) > 2 else [])
            pl = place(base_plane, a, rot + drot, bool(f.get("flip")))
        ctx = Ctx(pl, body, res, base_dir, process, nested)
        out = comp.build(ctx, params)
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
    props = {"volume": round(body.volume, 3), "area": round(body.area, 3), "bbox": bbox_dict(bb),
             "solids": len(solids), "faces": len(body.faces()), "edges": len(body.edges())}
    try:
        com = body.center()
        props["com"] = [round(com.X, 3), round(com.Y, 3), round(com.Z, 3)]
    except Exception:  # noqa: BLE001
        pass
    mat = spec.get("material")
    if mat:
        dens = DENSITY.get(str(mat).lower())
        if dens:
            props["mass_g"] = round(body.volume / 1000 * dens, 2)
    res.props = props
    if not run_checks:
        return

    def chk(name, ok, detail, fix="", level="error"):
        res.checks.append((name, bool(ok), detail))
        if not ok:
            res.issues.append(Issue(level, "check", name, detail, fix))

    chk("valid", body.is_valid, "B-Rep validity (BRepCheck)", "a boolean produced bad geometry; simplify the last feature")
    chk("manifold", all(s.is_manifold for s in solids), "closed manifold solids")
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
            chk("volume", abs(body.volume - e) <= abs(e) * tp / 100, f"{body.volume:.3f} vs {e:.3f} (±{tp}%)")
        elif isinstance(v, dict):
            lo, hi = ev(v.get("min", 0), "volume.min"), ev(v.get("max", float("inf")), "volume.max")
            chk("volume", lo <= body.volume <= hi, f"{body.volume:.3f} in [{lo}, {hi}]")
    if "com" in cs:
        e = ev(cs["com"]["expect"], "com.expect")
        tol = float(cs["com"].get("tol", 0.1))
        got = props.get("com", [0, 0, 0])
        chk("com", all(x is None or _num_close(g, x, tol) for g, x in zip(got, e)), f"{got} vs {e} (tol {tol})")
    for k, item in enumerate(cs.get("dims") or []):
        nm = item.get("name", f"dim{k}")
        try:
            got = ev(item["value"], f"dims.{nm}")
            e = ev(item["expect"], f"dims.{nm}")
        except SpecError as err:
            chk(f"dim:{nm}", False, str(err))
            continue
        tol = float(item.get("tol", 0.01))
        chk(f"dim:{nm}", _num_close(got, e, tol), f"{float(got):.4f} vs {float(e):.4f} (tol {tol})")
    for kind in ("solid_at", "empty_at"):
        pts = ev(cs.get(kind) or [], kind)
        if pts:
            inside = classify_points(body, [Vector(*p) for p in pts])
            for p, ins in zip(pts, inside):
                ok = ins if kind == "solid_at" else not ins
                chk(f"{kind}{list(p)}", ok, ("material" if ins else "empty") + f" at {list(p)}",
                    "probe point disagrees with intent: feature missing or mis-placed")
