# Spec format

```yaml
part: bracket_a            # output file name
process: fdm               # fdm|sla|cnc|laser|generic  -> default min_wall (1.2/0.8/1.0/1.0/1.0)
material: petg             # default material/colour (built-in name or one from materials:)
materials:                 # optional custom/overridden materials
  gold: {color: "#d4af37", density: 19.3}
params:                    # evaluated top-down, may reference earlier params
  L: 80
  margin: 8
  pitch: "=L - 2*margin"
features:                  # executed top-down; first one must create material
  - id: base               # unique, not a param name, not 'body'
    use: plate             # component (jig comp list)
    with: {l: =L, w: 40, t: 6, r: 3}
  - id: holes
    use: hole
    on: base.top           # anchor (default: body.top; base parts default to world XY)
    at: ["=pitch/2", 0]    # local offset [u, v] or [u, v, w] from the anchor centre
    rotate: 0              # spin each instance in place about the anchor normal (degrees)
    frame_rotate: 0        # rotate the local frame first: at + repeat + part turn together
    flip: false            # reverse the anchor normal
    repeat: {mirror: x}    # see below
    when: "=L > 50"        # optional condition (feature skipped when false)
    op: cut                # optional override: add|cut|intersect|paint
    material: red          # on adds (or op: paint): colour/material of this feature's material
    with: {size: M4, type: cbore}
checks: {...}              # see checks.md
```

## Values and expressions
- A string starting with `=` is an expression: `+ - * / // % **`, comparisons,
  `a if c else b`, `sqrt sin cos tan atan2 (degrees) abs min max round floor ceil hypot dist`.
- Names: params; earlier feature ids → `xmin xmax ymin ymax zmin zmax size_x size_y size_z
  cx cy cz x y z points n volume` (x,y,z = first point); `body` = current part.
- `dist(p, q)` distance between points, e.g. `"=dist(holes.points[0], holes.points[1])"`.

## repeat
| form | meaning |
|---|---|
| `{grid: {nx: 3, ny: 2, dx: 20, dy: 15}}` | grid centred on `at` |
| `{polar: {n: 6, r: 30, start: 0, sweep: 360, orient: false}}` | circle around `at` |
| `{mirror: x \| y \| xy}` | `at` plus its mirror images about the anchor centre |
| `{points: [[u, v], ...]}` | explicit offsets added to `at` ([u, v, spin_deg] allowed) |
| `{linear: {n: 6, step: [du, dv, dw]}}` | n copies stepping in the anchor frame, incl. along the normal (w) |

`mirror` can be combined with one generator: `{grid: {...}, mirror: y}` mirrors every
generated position (porticoes front + back, columns left + right).

## Depth values (cuts)
number (mm from the anchor face) · `through` (through everything along the axis) ·
`next` (through the first wall only – use for shells/enclosures).

## op: paint (multi-colour without geometry change)
A feature with `op: paint` + `material:` keeps the geometry and recolours the part's material
inside its volume at that moment (later adds keep their own material, later cuts still cut).
Typical: coloured courses/stripes, a logo skin 0.6 mm deep for multi-colour printing, trim bands.

## Shapes worth knowing
`revolve` (lathe a closed [r, z] profile about the anchor normal: columns, domes, rings, pulleys),
`rod` (bar between two anchor-local points), `arch` (round-headed opening; `breakout: true`
for door/passage openings that reach the base), `profile` (any polygon, any winding).

## Escape hatch
```yaml
- id: custom
  use: python
  op: add                  # or cut
  on: base.top
  with: {file: shapes.py, func: build, args: {n: 5}}
```
`shapes.py: def build(n): return <build123d Part in local frame, +Z = out of face>`

## Composite components (reuse)
Any passing spec can become a component: `jig promote part.yaml --name foot --expose D,H`.
It is written to `./components/foot.yaml` and loaded automatically (also from
`$JIGCAD_PATH` and `~/.jigcad/components`). It is placed so that its own XY plane / origin
lands on the anchor; kind `add` grows out, kind `cut` composites should be modelled below z=0.
