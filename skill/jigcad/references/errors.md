# Error codes → what to change

| code | meaning | typical fix |
|---|---|---|
| `feature` | spec/param problem (message lists valid names, near matches) | follow the hint |
| `param` | bad param / override | fix name or expression |
| `kernel` | OpenCascade rejected the op | sizes ≤ 0, tangent/coincident faces, fillet too large |
| `cut_missed` | cut removed nothing | wrong `on` face, `flip`, `at` outside part |
| `not_through` | `through` cut still hits material on its axis | wrong axis/face; a later add refills it |
| `breakout` | cut opens a side wall | move inward / shrink; if intended: `breakout: true` (pocket) or `notch` |
| `thin_wall` | wall < min_wall | move/shrink cut, thicken part; lower `checks.min_wall` only if the process allows |
| `floating` | add does not touch body | anchor on a real face; check `at`, `w` offset |
| `add_buried` | add entirely inside body | wrong face / flip |
| `touch_only` | add touches only along a line/point (curved face) | sink it in: `at: [u, v, -s]`, height + s |
| `non_manifold` | fusing created a line/point contact (wheel on rail) | shift/resize ~0.5 mm to overlap or leave a gap |
| `internal` | bug inside a JigCAD component, not your geometry | try another param form; report it |
| `disjoint` / `solids` | part split into pieces | a cut severs the part or an add floats |
| `check` | a declared check failed (`bbox.size_x`, `dim:pitch`, `empty_at[..]`) | geometry disagrees with intent: fix the feature, not the check |
| `multi_wall` (W) | through-all cut pierces several walls | use `depth: next` if only the first wall is meant |
| timeout | build exceeded `--timeout` | reduce repeat counts / complexity |

Fix the first `E` line first: later errors are often consequences.
