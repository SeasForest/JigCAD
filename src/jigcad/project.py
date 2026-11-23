"""Exports, verified-state history (snapshots/rollback) and the AI-facing report."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .engine import BuildResult

FORMATS = ("step", "stl", "3mf", "gltf", "brep")


# --------------------------------------------------------------------------- export

def export(body, name: str, out_dir: Path, formats=("step", "stl")) -> list[str]:
    from build123d import Mesher, export_gltf, export_step, export_stl
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    body = body.clean() if hasattr(body, "clean") else body
    body.label = name
    for fmt in formats:
        p = out_dir / f"{name}.{fmt}"
        if fmt == "step":
            export_step(body, p, timestamp="2000-01-01T00:00:00")  # deterministic file
        elif fmt == "stl":
            export_stl(body, p, tolerance=0.01, angular_tolerance=0.1)
        elif fmt == "3mf":
            m = Mesher()
            m.add_shape(body, linear_deflection=0.01, angular_deflection=0.1)
            m.write(str(p))
        elif fmt == "gltf":
            p = out_dir / f"{name}.glb"
            export_gltf(body, p, binary=True, linear_deflection=0.01)
        elif fmt == "brep":
            body.export_brep(str(p))
        else:
            raise ValueError(f"unknown format {fmt}; use {FORMATS}")
        written.append(str(p))
    return written


# --------------------------------------------------------------------------- report

def fingerprint(props: dict) -> str:
    key = json.dumps({k: props.get(k) for k in ("volume", "area", "bbox", "faces", "edges", "solids")},
                     sort_keys=True, default=str)
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def report(res: BuildResult) -> dict:
    errors = [i.as_dict() for i in res.issues if i.level == "error"]
    warns = [i.as_dict() for i in res.issues if i.level == "warn"]
    built = sum(1 for f in res.features.values())
    rep = {
        "status": "PASS" if res.ok else "FAIL",
        "part": res.name,
        "features": f"{built}/{res.n_features}",
        "ms": res.ms,
        "props": res.props,
        "checks": {"passed": sum(1 for c in res.checks if c[1]), "total": len(res.checks)},
        "errors": errors,
        "warnings": warns,
    }
    if res.failed_at:
        rep["failed_at"] = res.failed_at
    if res.props:
        rep["fingerprint"] = fingerprint(res.props)
    walls = {fid: f.info["min_wall"] for fid, f in res.features.items() if "min_wall" in f.info}
    if walls:
        rep["min_wall"] = min(walls.values())
    return rep


def format_text(rep: dict) -> str:
    """Compact, token-cheap report for agents."""
    L = []
    p = rep.get("props") or {}
    head = f"{rep['status']} {rep['part']}  features {rep['features']}  {rep['ms'] / 1000:.2f}s"
    if rep.get("fingerprint"):
        head += f"  fp={rep['fingerprint']}"
    L.append(head)
    if rep.get("failed_at"):
        L.append(f"stopped at feature '{rep['failed_at']}' (earlier features OK)")
    if p:
        s = p["bbox"]["size"]
        line = f"size {s[0]:g} x {s[1]:g} x {s[2]:g}  bbox {list(p['bbox']['min'])}..{list(p['bbox']['max'])}"
        L.append(line)
        extra = f"vol {p['volume']:g} mm3  solids {p['solids']}"
        if "mass_g" in p:
            extra += f"  mass {p['mass_g']:g} g"
        if "com" in p:
            extra += f"  com {p['com']}"
        if rep.get("min_wall") is not None:
            extra += f"  min_wall {rep['min_wall']:g}"
        L.append(extra)
    for e in rep["errors"] + rep["warnings"]:
        tag = "E" if e["level"] == "error" else "W"
        L.append(f"{tag} {e['code']} [{e['where']}] {e['msg']}")
        if e.get("fix"):
            L.append(f"  fix: {e['fix']}")
    c = rep["checks"]
    if c["total"]:
        L.append(f"checks {c['passed']}/{c['total']} ok")
    for k in ("outputs", "render", "snapshot", "hint"):
        if rep.get(k):
            v = rep[k]
            L.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(L)


def inspect_text(res: BuildResult, anchors: bool = True) -> str:
    L = [f"{res.name}: params " + ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
                                            for k, v in res.params.items())]
    for f in res.features.values():
        if f.skipped:
            L.append(f"- {f.id} ({f.use}) skipped (when=false)")
            continue
        s = f.summary()
        bb = s.get("bbox")
        line = f"- {f.id} ({f.use},{f.op}" + (f" x{f.instances}" if f.instances > 1 else "") + f") dV={s['dV']:g}"
        if bb:
            line += f" bbox {list(bb['min'])}..{list(bb['max'])}"
        L.append(line)
        if f.points:
            pts = f.points[:6]
            L.append("    points " + " ".join(str(list(p)) for p in pts) + (" ..." if len(f.points) > 6 else ""))
        if f.info:
            L.append("    info " + ", ".join(f"{k}={v}" for k, v in f.info.items()))
        if anchors and f.anchors:
            L.append("    anchors " + ", ".join(sorted(f.anchors)))
    return "\n".join(L)


# --------------------------------------------------------------------------- history

def hist_dir(spec_path: Path, part: str) -> Path:
    return spec_path.parent / ".jig" / "history" / part


def snapshot(spec_path: Path, part: str, rep: dict, overrides: dict) -> str:
    d = hist_dir(spec_path, part)
    d.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit())
    if existing:  # dedupe identical verified state
        last = d / f"{existing[-1]:04d}"
        try:
            if (last / "spec.yaml").read_text() == spec_path.read_text() and \
               json.loads((last / "meta.json").read_text()).get("overrides") == overrides:
                return last.name
        except FileNotFoundError:
            pass
    n = (existing[-1] + 1) if existing else 1
    vd = d / f"{n:04d}"
    vd.mkdir()
    shutil.copy2(spec_path, vd / "spec.yaml")
    meta = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "fingerprint": rep.get("fingerprint"),
            "overrides": overrides, "props": rep.get("props")}
    (vd / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    return vd.name


def history(spec_path: Path, part: str) -> list[dict]:
    d = hist_dir(spec_path, part)
    if not d.exists():
        return []
    out = []
    for vd in sorted(p for p in d.iterdir() if p.is_dir() and p.name.isdigit()):
        meta = json.loads((vd / "meta.json").read_text())
        meta["version"] = vd.name
        out.append(meta)
    return out


def restore(spec_path: Path, part: str, version: str) -> str:
    d = hist_dir(spec_path, part)
    vd = d / f"{int(version):04d}"
    if not vd.exists():
        raise FileNotFoundError(f"no version {version}; run `jig log {spec_path}`")
    backup = d / f"unsaved-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
    if spec_path.exists():
        shutil.copy2(spec_path, backup)
    shutil.copy2(vd / "spec.yaml", spec_path)
    return str(backup)
