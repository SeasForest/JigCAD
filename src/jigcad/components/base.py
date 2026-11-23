"""Component framework: parameter schema, registry and result type.

A component is a high level, parameterised feature. It is built in a local
frame (anchor plane: +Z is the outward normal of the face it sits on) and
returned in world coordinates. Kinds:

* ``base``     – creates material from nothing (first feature of a part)
* ``add``      – material grown outward (+Z of the anchor plane), fused to body
* ``cut``      – material removed inward (-Z of the anchor plane)
* ``modifier`` – rewrites the body (fillet, chamfer, shell ...)
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Callable

KINDS = ("base", "add", "cut", "modifier")


@dataclass
class P:
    """Parameter declaration."""
    type: str = "num"        # num|int|bool|str|depth|vec2|vec3|list|dict|size
    default: Any = None
    required: bool = False
    doc: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple | None = None

    def sig(self, name: str) -> str:
        t = self.type
        if self.choices:
            t = "|".join(map(str, self.choices))
        if self.required:
            return f"{name}:{t}*"
        return f"{name}:{t}={self.default!r}" if self.default is not None else f"{name}:{t}"


@dataclass
class Out:
    """What a component build returns (all geometry in world coordinates)."""
    shape: Any                                   # build123d Part/Solid (tool or body)
    anchors: dict[str, Any] = field(default_factory=dict)   # name -> Plane
    points: list[tuple[float, float, float]] = field(default_factory=list)  # key points (hole centres...)
    through: bool = False                        # cut must pierce body completely
    axis: tuple[float, float, float] | None = None  # cut/add direction (world)
    allow_breakout: bool = False                 # cut may open side walls
    depth: float | None = None                   # actual cut depth along axis
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class Component:
    name: str
    kind: str
    summary: str
    params: dict[str, P]
    build: Callable[..., Out]
    anchors_doc: str = ""
    source: str = "builtin"
    example: dict | None = None

    def sig(self) -> str:
        return f"{self.name} [{self.kind}] " + " ".join(p.sig(n) for n, p in self.params.items())


class ParamError(ValueError):
    pass


REGISTRY: dict[str, Component] = {}


def component(name: str, kind: str, summary: str, params: dict[str, P], anchors: str = "",
              example: dict | None = None):
    assert kind in KINDS

    def deco(fn):
        REGISTRY[name] = Component(name, kind, summary, params, fn, anchors, "builtin", example)
        return fn
    return deco


def _coerce(name: str, decl: P, v: Any) -> Any:
    t = decl.type
    try:
        if t == "num":
            v = float(v)
        elif t == "int":
            if isinstance(v, float) and not v.is_integer():
                raise ParamError(f"{name}: expected integer, got {v}")
            v = int(v)
        elif t == "bool":
            if not isinstance(v, bool):
                raise ParamError(f"{name}: expected true/false")
        elif t == "depth":
            if v not in ("through", "next"):
                v = float(v)
                if v <= 0:
                    raise ParamError(f"{name}: depth must be > 0, 'through' or 'next'")
        elif t in ("vec2", "vec3"):
            n = 2 if t == "vec2" else 3
            if not isinstance(v, (list, tuple)) or len(v) != n:
                raise ParamError(f"{name}: expected list of {n} numbers")
            v = [float(x) for x in v]
        elif t == "str":
            v = str(v)
    except (TypeError, ValueError) as e:
        if isinstance(e, ParamError):
            raise
        raise ParamError(f"{name}: expected {t}, got {v!r}") from e
    if decl.choices and v not in decl.choices:
        raise ParamError(f"{name}: '{v}' not in {list(decl.choices)}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if decl.min is not None and v < decl.min:
            raise ParamError(f"{name}: {v} < min {decl.min}")
        if decl.max is not None and v > decl.max:
            raise ParamError(f"{name}: {v} > max {decl.max}")
    return v


def validate_params(comp: Component, given: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in given:
        if k not in comp.params:
            hint = difflib.get_close_matches(k, comp.params, n=1)
            msg = f"unknown param '{k}' for {comp.name}"
            if hint:
                msg += f" (did you mean '{hint[0]}'?)"
            msg += f"; params: {', '.join(comp.params)}"
            raise ParamError(msg)
    for name, decl in comp.params.items():
        if name in given and given[name] is not None:
            out[name] = _coerce(name, decl, given[name])
        elif decl.required:
            raise ParamError(f"missing required param '{name}' for {comp.name}; signature: {comp.sig()}")
        else:
            out[name] = decl.default
    return out


def get(name: str) -> Component:
    from . import load_all
    load_all()
    if name not in REGISTRY:
        hint = difflib.get_close_matches(name, REGISTRY, n=3)
        raise ParamError(f"unknown component '{name}'" + (f"; did you mean {hint}?" if hint else "")
                         + " (run `jig comp list`)")
    return REGISTRY[name]
