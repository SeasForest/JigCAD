"""Component registry loading: built-ins, YAML composite components, python escape hatch."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


from .base import REGISTRY, Component, Out, P, ParamError, component, get, validate_params  # noqa: F401

_LOADED: set[str] = set()


def search_paths(extra: list[Path] | None = None) -> list[Path]:
    paths = list(extra or [])
    paths.append(Path.cwd() / "components")
    for p in os.environ.get("JIGCAD_PATH", "").split(os.pathsep):
        if p:
            paths.append(Path(p))
    paths.append(Path.home() / ".jigcad" / "components")
    return paths


def load_all(extra: list[Path] | None = None) -> None:
    if "builtin" not in _LOADED:
        from . import builtin  # noqa: F401
        _register_python()
        _LOADED.add("builtin")
    for d in search_paths(extra):
        key = str(d.resolve()) if d.exists() else None
        if not key or key in _LOADED:
            continue
        _LOADED.add(key)
        for f in sorted(d.glob("*.yaml")):
            try:
                register_yaml(f)
            except Exception as e:  # noqa: BLE001
                print(f"warning: component {f.name} not loaded: {e}")


def _param_from_yaml(v) -> P:
    if isinstance(v, dict):
        return P(type=v.get("type", "num"), default=v.get("default"), required=v.get("required", False),
                 doc=v.get("doc", ""), min=v.get("min"), max=v.get("max"),
                 choices=tuple(v["choices"]) if v.get("choices") else None)
    return P(type="str" if isinstance(v, str) and not v.startswith("=") else "num", default=v)


def register_yaml(path: Path) -> Component:
    """A composite component is a normal part spec plus ``component:`` header.

    Its ``params`` are the public interface; it is built in its own frame and
    placed so that its world XY plane lands on the anchor plane.
    """
    from ..engine import yaml_load
    data = yaml_load(path.read_text())
    name = data.get("component")
    if not name:
        raise ParamError("composite component needs a 'component:' name")
    kind = data.get("kind", "add")
    params = {k: _param_from_yaml(v) for k, v in (data.get("params") or {}).items()}
    if data.get("interface"):
        params = {k: v for k, v in params.items() if k in data["interface"]}

    def build(ctx, p, _data=data, _path=path):
        from ..engine import build_spec
        sub = build_spec(_data, overrides=p, base_dir=_path.parent, run_checks=False, nested=ctx.depth + 1)
        if not sub.ok:
            raise ParamError(f"composite '{name}' failed: {sub.first_error()}")
        shape = sub.body.moved(ctx.plane.location)
        anchors = {}
        for fid, rec in sub.features.items():
            for an, pl in rec.anchors.items():
                anchors[f"{fid}.{an}"] = pl.moved(ctx.plane.location) if hasattr(pl, "moved") else pl
        pts = [tuple(round(c, 4) for c in ctx.plane.from_local_coords(q)) for r in sub.features.values() for q in r.points]
        return Out(shape, anchors=anchors, points=pts, axis=tuple(-ctx.plane.z_dir))

    comp = Component(name, kind, data.get("summary", f"composite from {path.name}"), params, build,
                     anchors_doc="<feature>.<anchor> of inner features", source=str(path))
    REGISTRY[name] = comp
    return comp


def _register_python():
    @component("python", "add",
               "escape hatch: call build(**args) in a local .py file; returns a build123d shape in local frame (+Z out)",
               {"file": P("str", required=True), "func": P("str", default="build"),
                "args": P("dict", default=None)})
    def python_comp(ctx, p):
        path = (ctx.base_dir / p["file"]).resolve()
        spec = importlib.util.spec_from_file_location(f"jig_user_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ParamError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, p["func"], None)
        if fn is None:
            raise ParamError(f"{path.name} has no function {p['func']}")
        shape = fn(**(p["args"] or {}))
        return Out(shape.moved(ctx.plane.location), axis=tuple(-ctx.plane.z_dir))
