"""`jig` command line. Every geometry job runs in a child process with a timeout."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

from . import __version__

DEFAULT_TIMEOUT = float(os.environ.get("JIGCAD_TIMEOUT", "120"))


# --------------------------------------------------------------------------- helpers

def _overrides(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--set expects k=v, got {it}")
        k, v = it.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _child(q, fn, kw):
    try:
        q.put(fn(**kw))
    except SystemExit as e:
        q.put({"text": str(e), "code": 2})
    except Exception as e:  # noqa: BLE001
        import traceback
        q.put({"text": f"ERROR {type(e).__name__}: {e}\n" + (traceback.format_exc() if os.environ.get("JIGCAD_DEBUG") else ""),
               "code": 3})


def _run(fn, timeout, **kw):
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q, fn, kw))
    p.start()
    try:
        res = q.get(timeout=timeout)
    except Exception:  # noqa: BLE001  queue.Empty
        p.kill()
        p.join()
        return {"text": f"FAIL timeout after {timeout:g}s (runaway geometry? reduce repeat counts / complexity, "
                        f"or raise --timeout)", "code": 4}
    p.join()
    return res


def _emit(res, as_json):
    if as_json and "json" in res:
        print(json.dumps(res["json"], indent=1, default=str))
    else:
        print(res["text"])
    return res.get("code", 0)


# --------------------------------------------------------------------------- jobs (run in child)

def job_build(spec, overrides, export_formats, do_render, check_only, out_dir=None, labels=True):
    from .engine import build_spec, load_spec
    from .project import export, format_text, report, snapshot
    sp = Path(spec)
    data = load_spec(sp)
    res = build_spec(data, overrides=overrides, base_dir=sp.parent)
    rep = report(res)
    od = Path(out_dir) if out_dir else sp.parent / "out" / res.name
    if res.ok and not check_only:
        rep["outputs"] = export(res.body, res.name, od, export_formats)
        rep["snapshot"] = f"v{snapshot(sp, res.name, rep, overrides)} (verified state saved; `jig log {spec}`)"
    if not res.ok:
        from .project import history
        h = history(sp, res.name)
        if h:
            rep["hint"] = f"last verified version v{h[-1]['version']} -> `jig restore {spec} {int(h[-1]['version'])}`"
    if do_render and res.body is not None:
        rep["render"] = _render(res, od, labels)
    return {"text": format_text(rep), "json": rep, "code": 0 if res.ok else 1}


def _render(res, od, labels=True, views=("iso", "top", "front", "right"), path=None):
    from .render import render
    od.mkdir(parents=True, exist_ok=True)
    lab = [(fid, p) for fid, f in res.features.items() for p in f.points[:12]] if labels else None
    path = path or od / f"{res.name}.png"
    render(res.body, path, views=views, labels=lab,
           title=f"{res.name}  " + ("PASS" if res.ok else f"FAIL @ {res.failed_at or 'checks'}"))
    return str(path)


def job_render(spec, overrides, views, out, labels):
    from .engine import build_spec, load_spec
    sp = Path(spec)
    res = build_spec(load_spec(sp), overrides=overrides, base_dir=sp.parent, run_checks=False)
    if res.body is None:
        return {"text": f"FAIL nothing to render: {res.first_error()}", "code": 1}
    od = sp.parent / "out" / res.name
    p = _render(res, od, labels, tuple(views), Path(out) if out else None)
    return {"text": f"render: {p}" + (f"\nnote: build stopped at '{res.failed_at}'" if res.failed_at else ""),
            "json": {"render": p}, "code": 0}


def job_inspect(spec, overrides, anchors):
    from .engine import build_spec, load_spec
    from .project import format_text, inspect_text, report
    sp = Path(spec)
    res = build_spec(load_spec(sp), overrides=overrides, base_dir=sp.parent)
    rep = report(res)
    rep["features_detail"] = [f.summary() | {"anchors": sorted(f.anchors)} for f in res.features.values()]
    return {"text": inspect_text(res, anchors) + "\n" + format_text(report(res)), "json": rep,
            "code": 0 if res.ok else 1}


def job_comp(action, name, kind, spec_dir):
    from .components import REGISTRY, get, load_all
    load_all([Path(spec_dir) / "components"] if spec_dir else None)
    if action == "list":
        rows = [c for c in REGISTRY.values() if not kind or c.kind == kind]
        lines = [f"{c.name:<12} {c.kind:<8} {c.summary}" for c in sorted(rows, key=lambda c: ("base add cut modifier".split().index(c.kind), c.name))]
        return {"text": "\n".join(lines) + "\n(`jig comp show <name>` for params)",
                "json": [{"name": c.name, "kind": c.kind, "summary": c.summary} for c in rows], "code": 0}
    c = get(name)
    if action == "show":
        L = [f"{c.name} [{c.kind}] {c.summary}", f"source: {c.source}", "params (* = required):"]
        for pn, p in c.params.items():
            rng = "" if p.min is None and p.max is None else f" range[{p.min},{p.max}]"
            ch = f" one of {list(p.choices)}" if p.choices else ""
            dflt = "" if p.required else f" default={p.default!r}"
            L.append(f"  {pn}{'*' if p.required else ''}: {p.type}{dflt}{rng}{ch}  {p.doc}".rstrip())
        L.append("anchors: top bottom front back left right center" + (f" + {c.anchors_doc}" if c.anchors_doc else ""))
        if c.example:
            L.append("example: " + json.dumps(c.example))
        return {"text": "\n".join(L), "code": 0}
    if action == "test":
        if c.source == "builtin":
            return {"text": "builtin components are covered by the test-suite", "code": 0}
        from .engine import build_spec, load_spec
        from .project import format_text, report
        sp = Path(c.source)
        res = build_spec(load_spec(sp), base_dir=sp.parent)
        return {"text": format_text(report(res)), "code": 0 if res.ok else 1}
    raise SystemExit(f"unknown comp action {action}")


def job_promote(spec, name, expose, kind, summary, out_dir):
    import yaml

    from .engine import build_spec, load_spec
    sp = Path(spec)
    data = load_spec(sp)
    res = build_spec(data, base_dir=sp.parent)
    if not res.ok:
        return {"text": f"FAIL cannot promote: spec does not pass ({res.first_error()})", "code": 1}
    params = data.get("params") or {}
    exp = [e for e in (expose or "").split(",") if e] or list(params)
    missing = [e for e in exp if e not in params]
    if missing:
        return {"text": f"FAIL unknown params to expose: {missing}", "code": 1}
    comp = {"component": name, "kind": kind,
            "summary": summary or f"promoted from {sp.name} (params: {', '.join(exp)})",
            "interface": exp}
    comp.update({k: v for k, v in data.items() if k not in ("part", "component", "kind", "summary", "interface")})
    od = Path(out_dir) if out_dir else sp.parent / "components"
    od.mkdir(parents=True, exist_ok=True)
    dst = od / f"{name}.yaml"
    if dst.exists():
        return {"text": f"FAIL {dst} exists; choose another --name", "code": 1}
    dst.write_text(yaml.safe_dump(comp, sort_keys=False, allow_unicode=True))
    return {"text": f"OK component '{name}' [{kind}] -> {dst}\nuse: {{use: {name}, on: <anchor>, with: {{{', '.join(f'{e}: ...' for e in exp)}}}}}",
            "code": 0}


def job_bench(directory):
    import time

    from .engine import build_spec, load_spec
    from .project import report
    rows = []
    for sp in sorted(Path(directory).glob("*.yaml")):
        t = time.perf_counter()
        try:
            res = build_spec(load_spec(sp), base_dir=sp.parent)
            rep = report(res)
            rows.append({"spec": sp.name, "status": rep["status"], "ms": rep["ms"],
                         "checks": f"{rep['checks']['passed']}/{rep['checks']['total']}",
                         "errors": [e["code"] + ":" + e["where"] for e in rep["errors"]]})
        except Exception as e:  # noqa: BLE001
            rows.append({"spec": sp.name, "status": "CRASH", "ms": round((time.perf_counter() - t) * 1000),
                         "checks": "-", "errors": [str(e)[:80]]})
    ok = sum(r["status"] == "PASS" for r in rows)
    lines = [f"{r['status']:<5} {r['spec']:<28} {r['ms'] / 1000:6.2f}s checks {r['checks']:<6} {' '.join(r['errors'])}" for r in rows]
    lines.append(f"pass {ok}/{len(rows)}")
    return {"text": "\n".join(lines), "json": {"rows": rows, "pass": ok, "total": len(rows)},
            "code": 0 if ok == len(rows) else 1}


TEMPLATES = {
    "blank": """part: {name}
process: fdm          # fdm|sla|cnc|laser|generic -> default min_wall
material: petg
params:
  L: 60
  W: 40
  T: 6
features:
  - id: base
    use: plate
    with: {{l: =L, w: =W, t: =T, r: 3}}
checks:
  bbox: {{size: [=L, =W, =T], tol: 0.01}}
""",
}


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="jig", description="JigCAD: agent-first parametric B-Rep CAD")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def spec_cmd(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("spec")
        p.add_argument("-s", "--set", action="append", metavar="K=V", help="override a param")
        p.add_argument("--json", action="store_true")
        p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        return p

    b = spec_cmd("build", "build + self-check + export + snapshot (only when PASS)")
    b.add_argument("-f", "--formats", default="step,stl", help="step,stl,3mf,gltf,brep")
    b.add_argument("-r", "--render", action="store_true", help="also write multi-view PNG")
    b.add_argument("-o", "--out", help="output dir (default <spec dir>/out/<part>)")
    spec_cmd("check", "build + self-check only (no files written)").add_argument("-r", "--render", action="store_true")
    i = spec_cmd("inspect", "feature table: points, bbox, dV, anchors")
    i.add_argument("--no-anchors", action="store_true")
    r = spec_cmd("render", "multi-view PNG (iso/top/front/right) with feature labels")
    r.add_argument("--views", default="iso,top,front,right")
    r.add_argument("-o", "--out")
    r.add_argument("--no-labels", action="store_true")

    c = sub.add_parser("comp", help="component library")
    c.add_argument("action", choices=["list", "show", "test"])
    c.add_argument("name", nargs="?")
    c.add_argument("--kind", choices=["base", "add", "cut", "modifier"])
    c.add_argument("--dir", default=".", help="project dir containing components/")

    s = sub.add_parser("std", help="fastener / process standard tables")
    s.add_argument("size", nargs="?")

    n = sub.add_parser("new", help="create a spec from a template")
    n.add_argument("name")
    n.add_argument("--dir", default=".")

    pr = sub.add_parser("promote", help="turn a PASSing spec into a reusable component")
    pr.add_argument("spec")
    pr.add_argument("--name", required=True)
    pr.add_argument("--expose", help="comma separated params forming the interface (default all)")
    pr.add_argument("--kind", default="add", choices=["base", "add", "cut"])
    pr.add_argument("--summary")
    pr.add_argument("--dir")

    lg = sub.add_parser("log", help="verified versions of a spec")
    lg.add_argument("spec")
    rs = sub.add_parser("restore", help="restore a verified version (current spec is backed up)")
    rs.add_argument("spec")
    rs.add_argument("version")

    sk = sub.add_parser("skill", help="print the agent skill path or install it into an agent's skill dir")
    sk.add_argument("action", choices=["path", "install"])
    sk.add_argument("--to", default=".claude/skills", help="target skills directory")

    be = sub.add_parser("bench", help="build every *.yaml in a directory, report pass rate")
    be.add_argument("dir")
    be.add_argument("--json", action="store_true")
    be.add_argument("--timeout", type=float, default=600)

    a = ap.parse_args(argv)
    try:
        ov = _overrides(getattr(a, "set", None))
    except SystemExit as e:
        print(e)
        return 2

    if a.cmd in ("build", "check"):
        res = _run(job_build, a.timeout, spec=a.spec, overrides=ov,
                   export_formats=tuple(getattr(a, "formats", "step").split(",")),
                   do_render=a.render, check_only=a.cmd == "check", out_dir=getattr(a, "out", None))
        return _emit(res, a.json)
    if a.cmd == "inspect":
        return _emit(_run(job_inspect, a.timeout, spec=a.spec, overrides=ov, anchors=not a.no_anchors), a.json)
    if a.cmd == "render":
        return _emit(_run(job_render, a.timeout, spec=a.spec, overrides=ov, views=a.views.split(","),
                          out=a.out, labels=not a.no_labels), a.json)
    if a.cmd == "comp":
        if a.action != "list" and not a.name:
            print("component name required")
            return 2
        return _emit(_run(job_comp, DEFAULT_TIMEOUT, action=a.action, name=a.name, kind=a.kind, spec_dir=a.dir), False)
    if a.cmd == "std":
        from . import standards
        if a.size:
            print(a.size + ": " + ", ".join(f"{k}={v:g}" for k, v in standards.fastener(a.size).items()))
        else:
            print("size   tap  clr(close/normal/loose)  head_d/h  cbore_d  csk_d  nut_af/h  insert_d/depth")
            for k, v in standards.METRIC.items():
                print(f"{k:<6} {v['tap']:<4} {v['clr_close']}/{v['clr_normal']}/{v['clr_loose']:<14} "
                      f"{v['head_d']}/{v['head_h']:<5} {v['cbore_d']:<8} {v['csk_d']:<6} {v['nut_af']}/{v['nut_h']:<5} "
                      f"{v['insert_d']}/{v['insert_depth']}")
            print("process min_wall: " + ", ".join(f"{k}={v['min_wall']}" for k, v in standards.PROCESS.items()))
        return 0
    if a.cmd == "new":
        p = Path(a.dir) / f"{a.name}.yaml"
        if p.exists():
            print(f"{p} exists")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATES["blank"].format(name=a.name))
        print(f"created {p}")
        return 0
    if a.cmd == "promote":
        return _emit(_run(job_promote, DEFAULT_TIMEOUT, spec=a.spec, name=a.name, expose=a.expose, kind=a.kind,
                          summary=a.summary, out_dir=a.dir), False)
    if a.cmd in ("log", "restore"):
        from .engine import load_spec
        from .project import history, restore
        sp = Path(a.spec)
        data = load_spec(sp) if sp.exists() else {}
        part = data.get("part") or data.get("component") or sp.stem
        if a.cmd == "log":
            h = history(sp, part)
            if not h:
                print("no verified versions yet (a PASSing `jig build` creates one)")
                return 0
            for m in h:
                pr_ = m.get("props") or {}
                size = pr_.get("bbox", {}).get("size")
                print(f"v{m['version']}  {m['time']}  fp={m['fingerprint']}  size={size}  vol={pr_.get('volume')}"
                      + (f"  set={m['overrides']}" if m.get("overrides") else ""))
            return 0
        b_ = restore(sp, part, a.version)
        print(f"restored v{int(a.version):04d} -> {sp} (previous spec backed up to {b_})")
        return 0
    if a.cmd == "skill":
        import shutil
        src = Path(__file__).resolve().parents[2] / "skill" / "jigcad"
        if not src.exists():
            print("skill files not found (install jigcad from a source checkout)")
            return 1
        if a.action == "path":
            print(src)
            return 0
        dst = Path(a.to) / "jigcad"
        shutil.copytree(src, dst, dirs_exist_ok=True)
        print(f"installed skill -> {dst}")
        return 0
    if a.cmd == "bench":
        return _emit(_run(job_bench, a.timeout, directory=a.dir), a.json)
    return 2


if __name__ == "__main__":
    sys.exit(main())
