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
