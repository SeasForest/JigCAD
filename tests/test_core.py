"""Core behaviour + defect-injection tests (self-check must catch common mistakes)."""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

from jigcad.engine import build_spec, load_spec, yaml_load
from jigcad.project import report

EX = Path(__file__).parent.parent / "examples"

PLATE = yaml_load("""
part: t
process: fdm
params: {L: 60, W: 40, T: 6}
features:
  - id: base
    use: plate
    with: {l: =L, w: =W, t: =T}
""")


def spec_with(*features, checks=None):
    s = copy.deepcopy(PLATE)
    s["features"] += [yaml_load(f) if isinstance(f, str) else f for f in features]
    if checks:
        s["checks"] = yaml_load(checks) if isinstance(checks, str) else checks
    return s


def codes(res):
    return {i.code for i in res.issues if i.level == "error"}


def warn_codes(res):
    return {i.code for i in res.issues if i.level == "warn"}


# ---------------------------------------------------------------- benchmark set

@pytest.mark.parametrize("spec", sorted(EX.glob("*.yaml")), ids=lambda p: p.stem)
def test_examples_pass(spec):
    res = build_spec(load_spec(spec), base_dir=spec.parent)
    assert res.ok, [i.as_dict() for i in res.issues]


def test_deterministic():
    spec = load_spec(EX / "mount_plate.yaml")
    a = report(build_spec(spec, base_dir=EX))
    b = report(build_spec(spec, base_dir=EX))
    assert a["fingerprint"] == b["fingerprint"]


def test_param_override_changes_geometry_consistently():
    spec = load_spec(EX / "mount_plate.yaml")
    res = build_spec(spec, overrides={"L": 150}, base_dir=EX)
    assert res.ok
    assert res.props["bbox"]["size"][0] == pytest.approx(150)


# ---------------------------------------------------------------- defect injection

def test_cut_missing_the_part():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [80, 0], with: {d: 4}}"))
    assert "cut_missed" in codes(res)


def test_cut_flipped_outward():
    res = build_spec(spec_with("{id: p, use: pocket, on: base.top, flip: true, with: {l: 10, w: 10, depth: 2}}"))
    assert "cut_missed" in codes(res)


def test_floating_add():
    res = build_spec(spec_with("{id: b, use: boss, on: world, at: [0, 0, 20], with: {d: 8, h: 5}}"))
    assert "floating" in codes(res)


def test_buried_add():
    res = build_spec(spec_with("{id: b, use: boss, on: base.top, flip: true, with: {d: 8, h: 3}}"))
    assert "add_buried" in codes(res)


def test_breakout_detected():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [28, 0], with: {d: 6}}"))
    assert "breakout" in codes(res)


def test_thin_wall_detected():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [26.5, 0], with: {d: 6}}"))
    assert "thin_wall" in codes(res)


def test_split_body_detected():
    res = build_spec(spec_with("{id: n, use: notch, on: base.top, with: {l: 4, w: 50, depth: through}}"))
    assert "solids" in {i.where for i in res.issues}


def test_wrong_dimension_detected():
    res = build_spec(spec_with(
        "{id: h, use: hole, on: base.top, at: [15, 0], repeat: {mirror: x}, with: {d: 4}}",
        checks="dims: [{name: pitch, value: '=dist(h.points[0], h.points[1])', expect: 25, tol: 0.01}]"))
    assert not res.ok and any(i.where == "dim:pitch" for i in res.issues)


def test_probe_points():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [10, 0], with: {d: 4}}",
                               checks="{empty_at: [[0, 0, 3]], solid_at: [[10, 0, 3]]}"))
    assert {i.where for i in res.issues} == {"empty_at[0, 0, 3]", "solid_at[10, 0, 3]"}


def test_multi_wall_warning_and_depth_next():
    spec = load_spec(EX / "enclosure.yaml")
    spec["features"][-1]["with"]["depth"] = "through"
    res = build_spec(spec, base_dir=EX)
    assert "multi_wall" in warn_codes(res)


def test_bbox_check():
    res = build_spec(spec_with(checks="bbox: {size: [60, 40, 8], tol: 0.01}"))
    assert any(i.where == "bbox.size_z" for i in res.issues)


# ---------------------------------------------------------------- AI-facing diagnostics

def test_unknown_param_hint():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, with: {dia: 4}}"))
    assert "did you mean" in res.first_error() or "params:" in res.first_error()


def test_unknown_anchor_lists_available():
    res = build_spec(spec_with("{id: h, use: hole, on: base.topp, with: {d: 4}}"))
    assert "available:" in res.first_error() and "top" in res.first_error()


def test_unknown_feature_ref_hint():
    res = build_spec(spec_with("{id: h, use: hole, on: bse.top, with: {d: 4}}"))
    assert "did you mean 'base'" in res.first_error()


def test_unknown_component_hint():
    res = build_spec(spec_with("{id: h, use: hol, on: base.top, with: {d: 4}}"))
    assert "hole" in res.first_error()


def test_on_key_not_parsed_as_bool():
    s = yaml_load("{on: base.top}")
    assert "on" in s


def test_fillet_too_big_is_explained():
    res = build_spec(spec_with("{id: f, use: fillet, with: {r: 30, edges: outer_vertical}}"))
    assert "fillet r=30" in res.first_error()


def test_failure_keeps_earlier_features():
    res = build_spec(spec_with("{id: ok, use: hole, on: base.top, with: {d: 4}}",
                               "{id: bad, use: hole, on: nowhere.top, with: {d: 4}}"))
    assert res.failed_at == "bad" and "ok" in res.features and res.body is not None


def test_expression_sandbox():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, with: {d: \"=__import__('os')\"}}"))
    assert not res.ok and "only math functions" in res.first_error()


# ---------------------------------------------------------------- reuse / assets

def test_feature_refs_in_expressions():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: ['=base.xmax - 5', 0], with: {d: 3}}"))
    assert res.ok and res.features["h"].points[0][0] == pytest.approx(25)


def test_when_and_repeat_polar():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, repeat: {polar: {n: 6, r: 12}}, with: {d: 3}}",
                               "{id: skip, use: hole, on: base.top, when: '=L > 100', with: {d: 3}}"))
    assert res.ok and len(res.features["h"].points) == 6 and res.features["skip"].skipped


def test_promote_and_reuse(tmp_path):
    src = tmp_path / "foot.yaml"
    src.write_text("""
part: foot
params: {D: 12, H: 6}
features:
  - {id: post, use: cylinder, with: {d: =D, h: =H}}
  - {id: hole, use: hole, on: post.top, with: {size: M3, fit: tap, depth: 4}}
""")
    r = subprocess.run([sys.executable, "-m", "jigcad", "promote", str(src), "--name", "rubber_foot",
                        "--expose", "D,H"], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    user = tmp_path / "part.yaml"
    user.write_text("""
part: user
params: {}
features:
  - {id: base, use: plate, with: {l: 60, w: 40, t: 5}}
  - {id: feet, use: rubber_foot, on: base.bottom, at: [22, 12], repeat: {mirror: xy}, with: {D: 10}}
checks:
  bbox: {size: [60, 40, 11], tol: 0.01}
""")
    res = build_spec(load_spec(user), base_dir=tmp_path)
    assert res.ok, res.first_error()


def test_cli_build_and_history(tmp_path):
    spec = tmp_path / "p.yaml"
    spec.write_text((EX / "nut_bar.yaml").read_text())
    r = subprocess.run([sys.executable, "-m", "jigcad", "build", str(spec), "-f", "step,3mf"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "out" / "nut_bar" / "nut_bar.step").exists()
    bad = spec.read_text().replace("use: nut_trap", "use: nut_trapp")
    spec.write_text(bad)
    r = subprocess.run([sys.executable, "-m", "jigcad", "build", str(spec)], capture_output=True, text=True)
    assert r.returncode == 1 and "jig restore" in r.stdout
    r = subprocess.run([sys.executable, "-m", "jigcad", "restore", str(spec), "1"], capture_output=True, text=True)
    assert r.returncode == 0 and "nut_trapp" not in spec.read_text()


# ---------------------------------------------------------------- regressions from the procgen trial

def test_profile_winding_does_not_flip_extrusion():
    cw = "{id: p, use: profile, on: base.top, with: {h: 5, points: [[0, 0], [0, 10], [10, 10], [10, 0]]}}"
    res = build_spec(spec_with(cw))
    assert res.ok, res.first_error()
    assert res.features["p"].shape.bounding_box().min.Z == pytest.approx(6)


def test_boss_hole_depth_next():
    res = build_spec(spec_with("{id: b, use: boss, on: base.top, with: {d: 10, h: 5, hole_d: 3, hole_depth: next}}"))
    assert res.ok, res.first_error()
    from build123d import Vector
    from jigcad.geom import classify_points
    assert not any(classify_points(res.body, [Vector(0, 0, z) for z in (1, 3, 5, 8)]))


def test_touch_only_on_curved_face():
    s = copy.deepcopy(PLATE)
    s["features"] = [yaml_load("{id: c, use: cylinder, on: XZ, with: {d: 20, h: 30}}"),
                     yaml_load("{id: b, use: boss, on: c.top, with: {d: 6, h: 5}}")]
    res = build_spec(s)
    assert "touch_only" in codes(res)


def test_non_manifold_contact_located():
    s = copy.deepcopy(PLATE)
    s["features"] += [yaml_load("{id: post, use: block, on: base.top, at: [0, 0], with: {l: 4, w: 4, h: 10}}"),
                      # lying cylinder tangent to the plate top (z=6) and passing through the post
                      yaml_load("{id: wheel, use: cylinder, on: {origin: [0, -10, 11], normal: [0, 1, 0]}, "
                                "with: {d: 10, h: 20}}")]
    res = build_spec(s)
    assert "non_manifold" in codes(res) or "touch_only" in codes(res)


def test_frame_rotate_moves_offsets():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [10, 0], frame_rotate: 90, with: {d: 3}}"))
    assert res.ok
    x, y, _ = res.features["h"].points[0]
    assert x == pytest.approx(0, abs=1e-6) and y == pytest.approx(10)
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [10, 0], rotate: 90, with: {d: 3}}"))
    assert res.features["h"].points[0][0] == pytest.approx(10)


def test_base_components_report_points():
    res = build_spec(spec_with("{id: c, use: cylinder, on: base.top, at: [5, 5], with: {d: 4, h: 3}}"))
    assert res.features["c"].points[0][:2] == (5.0, 5.0)


def test_geom_hash_is_position_sensitive():
    a = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [10, 0], with: {d: 3}}"))
    b = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [-10, 0], with: {d: 3}}"))
    assert a.props["volume"] == pytest.approx(b.props["volume"])
    assert report(a)["fingerprint"] != report(b)["fingerprint"]


def test_section_render(tmp_path):
    from jigcad.render import render
    res = build_spec(load_spec(EX / "enclosure.yaml"), base_dir=EX)
    p = render(res.body, tmp_path / "s.png", section="y", labels=[("a", (0, 10, 5)), ("b", (0, -10, 5))])
    assert Path(p).exists()


# ---------------------------------------------------------------- multi-material + city-gate trial

MULTI = """
part: mm
material: white
materials: {accent: {color: "#d4af37", density: 19.3}}
features:
  - {id: base, use: box, with: {l: 40, w: 20, h: 10}}
  - {id: logo, use: block, op: paint, on: base.top, at: [0, 0, -1], material: red, with: {l: 10, w: 10, h: 1}}
  - {id: knob, use: cylinder, on: base.top, material: accent, with: {d: 6, h: 5}}
checks:
  materials: 3
  material_at: [[4, 0, 9.5, red], [0, 0, 12, accent], [15, 0, 5, white]]
"""


def test_multi_material_regions_and_probes():
    res = build_spec(yaml_load(MULTI))
    assert res.ok, [i.as_dict() for i in res.issues]
    mats = res.props["materials"]
    assert set(mats) == {"white", "red", "accent"}
    assert sum(m["volume"] for m in mats.values()) == pytest.approx(res.props["volume"], rel=1e-6)
    assert mats["red"]["volume"] == pytest.approx(100)          # 10 x 10 x 1 recoloured skin
    assert mats["accent"]["mass_g"] == pytest.approx(mats["accent"]["volume"] / 1000 * 19.3, rel=1e-3)


def test_paint_needs_material_and_overlap():
    s = yaml_load(MULTI)
    s["features"][1]["at"] = [0, 0, 5]  # paint volume floats above the part
    res = build_spec(s)
    assert "paint_missed" in codes(res)
    s = yaml_load(MULTI)
    del s["features"][1]["material"]
    assert "needs material" in build_spec(s).first_error()


def test_unknown_material_hint_and_hidden_warning():
    s = yaml_load(MULTI)
    s["features"][2]["material"] = "acent"
    assert "did you mean" in build_spec(s).first_error()
    s = yaml_load(MULTI)
    s["features"].append(yaml_load("{id: cover, use: block, on: base.top, material: white, with: {l: 12, w: 12, h: 6}}"))
    s["checks"] = {}
    res = build_spec(s)
    assert "material_hidden" in warn_codes(res)


def test_material_on_cut_rejected():
    s = yaml_load(MULTI)
    s["features"].append(yaml_load("{id: h, use: hole, on: base.top, at: [15, 0], material: red, with: {d: 3}}"))
    assert "op: paint" in build_spec(s).first_error()


def test_multi_material_exports(tmp_path):
    from jigcad.project import export
    res = build_spec(yaml_load(MULTI))
    out = export(res.body, "mm", tmp_path, ("step", "stl", "3mf", "gltf"), regions=res.regions,
                 palette=res.palette, default_material=res.default_material)
    names = {Path(p).name for p in out}
    assert {"mm.step", "mm.stl", "mm.red.stl", "mm.accent.stl", "mm.white.stl", "mm.3mf", "mm.glb"} <= names
    import zipfile
    model = zipfile.ZipFile(tmp_path / "mm.3mf").read("3D/3dmodel.model").decode()
    assert model.count("displaycolor") >= 3
    assert "STYLED_ITEM" in (tmp_path / "mm.step").read_text()


def test_revolve_rod_arch_components():
    s = yaml_load("""
part: t
features:
  - {id: base, use: box, with: {l: 60, w: 20, h: 40}}
  - {id: door, use: arch, on: base.front, at: [0, -20], with: {w: 16, h: 10, breakout: true}}
  - {id: col, use: revolve, on: base.top, at: [20, 0], with: {points: [[0, 0], [5, 0], [4, 20], [0, 26]]}}
  - {id: bar, use: rod, on: base.top, with: {a: [-25, 0, 0], b: [-15, 0, 15], d: 3}}
checks:
  empty_at: [[0, 0, 5], [0, 0, 17]]
  solid_at: [[0, 0, 19], [20, 0, 65]]
""")
    res = build_spec(s)
    assert res.ok, res.first_error()
    assert res.features["col"].anchors["tip"].origin.Z == pytest.approx(66)


def test_cone_tip_is_manifold():
    s = yaml_load("{part: c, features: [{id: b, use: box, with: {l: 20, w: 20, h: 5}}, "
                  "{id: cone, use: revolve, on: b.top, with: {points: [[0, 0], [8, 0], [0, 10]]}}]}")
    res = build_spec(s)
    assert res.ok, res.first_error()


def test_repeat_grid_with_mirror_and_linear():
    res = build_spec(spec_with("{id: h, use: hole, on: base.top, at: [15, 10], "
                               "repeat: {grid: {nx: 2, dx: 6}, mirror: xy}, with: {d: 2}}"))
    pts = sorted((round(p[0]), round(p[1])) for p in res.features["h"].points)
    assert pts == sorted([(12, 10), (18, 10), (-12, 10), (-18, 10), (12, -10), (18, -10), (-12, -10), (-18, -10)])
    res = build_spec(spec_with("{id: s, use: block, on: base.top, at: [-20, 0], "
                               "repeat: {linear: {n: 3, step: [10, 0, 0]}}, with: {l: 2, w: 2, h: 2}}"))
    assert sorted(round(p[0]) for p in res.features["s"].points) == [-20, -10, 0]


def test_dims_min_max_and_warn_level():
    res = build_spec(spec_with(checks="dims: [{name: t, value: '=base.size_z', min: 10}, "
                                      "{name: w, value: '=base.size_x', max: 50, level: warn}]"))
    assert any(i.where == "dim:t" and i.level == "error" for i in res.issues)
    assert any(i.where == "dim:w" and i.level == "warn" for i in res.issues)


def test_city_gate_trial_spec_loads():
    spec = load_spec(EX.parent / "trials" / "procgen" / "city_gate.yaml")
    assert {"materials", "material_at"} <= set(spec["checks"])
