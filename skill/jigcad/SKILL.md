---
name: jigcad
description: Build precise, self-checked parametric CAD parts (STEP/STL/3MF), single- or multi-colour/material, with the `jig` CLI — brackets, plates, jigs, fixtures, enclosures, flanges, 3D-print parts, architectural kits. Use when the user asks to model, design, modify or export a 3D part with real dimensions.
---

# JigCAD skill

You write a **declarative YAML spec** (params + feature list + checks). `jig` builds it on an
exact B-Rep kernel (OpenCascade), self-checks every feature, and exports. You never write kernel
code and never compute absolute 3D coordinates by hand.

## Loop (follow in order)

1. **Pin the intent as numbers.** Every size the user gave → a `param`. Ask only if a
   functional dimension is truly missing; otherwise pick a sane default and say so.
2. **Pick components** — `jig comp list` (once per session), `jig comp show <name>` only
   for the ones you use. Prefer a component over hand-made geometry. Fasteners: use
   `size: M4, fit: normal|close|loose|tap|insert` instead of raw diameters (`jig std M4`).
3. **Write the spec** (start from `jig new <name>`). Format → `references/spec.md`.
4. **Encode acceptance criteria as checks**: overall `bbox`, every critical spacing as a
   `dims` entry, `empty_at`/`solid_at` probes for "a hole must be here". → `references/checks.md`
5. **`jig check part.yaml`** → read the compact report. Fix the **first** `E` line only,
   re-run. Codes and fixes → `references/errors.md`. Use `jig inspect` to see each
   feature's points/bbox/anchors when unsure where something landed.
6. **Look once**: `jig render part.yaml` → open the PNG (iso/top/front/right, feature
   labels, mm ruler). Compare against the request: orientation, which face, symmetry.
   Hollow / pit-like parts: add `--section y` (or x/z) to see inside.
   Text-only model? use `jig inspect` and probe checks instead.
7. **`jig build part.yaml -f step,stl,3mf`** → exports + saves a verified snapshot.
8. Something reusable emerged? `jig promote part.yaml --name <comp> --expose a,b`.

## Hard rules

- **Position by anchors, not coordinates**: `on: <feature>.<anchor>` + `at: [u, v]`
  (local offsets from the anchor centre) + expressions like `"=L/2 - margin"` or
  `"=base.xmax - 5"`. Never hard-code a number that is derivable from params.
- Symmetric patterns: `repeat: {mirror: xy}` / `{grid: ...}` / `{polar: ...}` — don't
  write N copies.
- **Cuts go INTO the anchor face, adds grow OUT of it.** Wrong face ⇒ `cut_missed` /
  `floating` / `add_buried`. Use `flip: true` only for anchors facing away from material.
- Cuts through hollow parts (shell walls): `depth: next`, not `through`.
- Don't "fix" a failing check by loosening tolerance, raising `min_wall` exceptions,
  deleting the check or deleting a feature. Fix geometry; if the requirement itself is
  impossible, tell the user.
- Edits are small diffs to the spec. If you broke a previously passing spec:
  `jig log part.yaml` / `jig restore part.yaml <n>`.
- **Sweep every control** the user may change: `jig check part.yaml -s L=150` at min /
  default / max (and e.g. several angles). Write checks that stay true across the sweep
  (distances to an axis, not world x/y): a mis-placed feature can pass at one value only.
- One part = one fused solid: overlapping features merge silently. When separate pieces
  must not collide (columns, ribs in a row), add a clearance check (`dims` with `min: 0`).
- Escape hatch for truly custom geometry: `use: python` (see `references/spec.md`) —
  last resort, its result is still self-checked.

## Colours / materials (multi-colour prints, kits)

- `material: <name>` at top level = default; per feature `material: <name>` on adds.
  Built-in names: print colours (`white black red blue …`), engineering (`petg steel
  aluminium …`) and all procgen slots (`stone pier-ashlar chipped-limestone slate bronze
  copper iron …`). Custom: top-level `materials: {gold: {color: "#d4af37", density: 19.3}}`.
- **Later features win** where they overlap. To recolour existing material without
  changing geometry use `op: paint` (stripes, logo skin, inlay look, trim bands).
- Check every colour that matters: `material_at: [[x, y, z, name], ...]` and
  `materials: N`. Warnings `material_hidden` / `material_island` = colour not visible /
  unprintable specks.
- `jig build -f step,stl,3mf,gltf` writes a coloured STEP assembly, a 3MF with one coloured
  object per material, a coloured GLB and `part.<material>.stl` per material (slicers).

## Anchor frames (local axes of `at: [u, v, w]`)

| anchor  | normal (+w) | u (+x local) | v (+y local) |
|---------|-------------|--------------|--------------|
| top     | +Z | +X | +Y |
| bottom  | −Z | +X | −Y |
| front   | −Y | +X | +Z |
| back    | +Y | −X | +Z |
| right   | +X | +Y | +Z |
| left    | −X | −Y | +Z |
| center  | +Z | +X | +Y |

`rotate: deg` spins each instance in place (its position from `at` is unchanged);
`frame_rotate: deg` rotates the whole local frame first, so `at`, `repeat` and the part
all turn together (use it for anything that follows a rotating assembly).

`flip: true` reverses the normal and **mirrors v** (u is kept): on a flipped side face
"up" becomes `-v`. Check with `jig inspect` (points) when unsure.

Every feature has these (from its own bounding box, for cuts: the removed material),
plus component extras (`tip` of boss/pin/standoff/block, `floor` of pocket, `leg_top`,
`wall_face` of l_bracket). `body.<anchor>` = current whole part. Base parts sit on XY,
centred in X/Y, from z=0 upward.

## Reading the report

```
FAIL enclosure  features 5/5  1.04s  fp=7c39e8573f
stopped at feature 'vents' (earlier features OK)
E breakout [vents[0]] cut breaks through a side face near [15.95, 30.0, 16.5]
  fix: move it inward / reduce size, or set breakout:true / use notch if intended
```
`[id[k]]` = k-th instance of a repeated feature. `W` lines are warnings (still PASS).
Multi-material parts add a `materials stone 52%  copper 2% …` line.
Add `--json` for machine-readable output.
