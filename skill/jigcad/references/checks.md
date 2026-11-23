# Checks

Always on (no config): B-Rep validity, closed manifold, solid count (default 1), and
per feature: cut removes material, `through` really pierces, no side-wall breakout,
wall thickness ≥ `min_wall`, adds touch the body and add material, multi-wall warning.

```yaml
checks:
  solids: 1
  min_wall: 1.5                       # override the process default
  bbox: {size: [=L, =W, =H], tol: 0.01}        # also min: [...], max: [...]; null = skip axis
  volume: {expect: 12345, tol_pct: 0.5}        # or {min: .., max: ..}
  com: {expect: [0, 0, null], tol: 0.05}       # centre of mass
  dims:                                        # any measurable quantity
    - {name: pitch, value: "=dist(h.points[0], h.points[1])", expect: =P, tol: 0.01}
    - {name: lip, value: "=lid.zmax - base.zmax", expect: 2, tol: 0.01}
    - {name: clearance, value: "=x_step - 2*foot_r", min: 0}          # inequality guard
    - {name: style, value: "=apex - roof", max: 300, level: warn}     # soft rule: warning only
  materials: 3                                 # or {min: 2, max: 6}
  material_at: [[0, 0, 12, red], [15, 0, 5, white]]   # colour probes
  min_island: 1.0                              # mm3, smaller colour specks -> warning
  solid_at: [[0, 0, 1]]                        # must be material
  empty_at: [["=L/2-8", 0, 3]]                 # must be empty (holes, pockets, slots)
```
Guidelines: one `dims` entry per dimension the user stated; one `empty_at` per functional
opening; keep `tol` at the requirement's tolerance (default engineering: 0.01 mm).
