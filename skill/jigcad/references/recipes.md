# Recipes

**Corner mounting holes**: `on: base.top, at: ["=L/2-m", "=W/2-m"], repeat: {mirror: xy}, with: {size: M4, type: cbore}`

**Bolt circle**: `repeat: {polar: {n: 6, r: "=BC/2"}}`

**Enclosure**: `box` → `fillet {edges: outer_vertical}` (before shell) → `shell {t: wall, open: top}` →
standoffs `on: <box>.bottom, flip: true, at: [x, y, "=wall"]` → wall cut-outs `on: <box>.front`, `depth: next`/`=wall`.

**L bracket with gussets**: `l_bracket` → `gusset on: bracket.leg_top, at: ["=-(L-T)/2", "=W/2-2"], repeat: {mirror: y}`
→ holes `on: bracket.wall_face`.

**Locating pins underneath**: `pin on: base.bottom` (grows downward).

**Heat-set inserts**: `standoff {size: M3, fit: insert}` or `hole {size: M3, fit: insert, depth: 6}`.

**Captive nut**: `nut_trap {size: M4}` on the face opposite to the screw head.

**Edge finishing**: `chamfer {d: 0.8, edges: top, of: base}` — `of` limits to that feature's own
edges so later holes are not chamfered. Selectors: all vertical outer_vertical horizontal top bottom circular linear.

**Variants**: params + `when:`; try sizes with `jig check spec.yaml -s L=150`.
