# 3D printed parts

The real inventory. The v1 README listed twelve filenames — `head.stl`,
`motor_base_2_.stl`, `sinfinity.stl`, `arduino_caes.stl` and others — **none of
which are in this directory**. That table was fabricated.

These seventeen files are what the prototype was printed from. The names are
Creality slicer autosaves (`CE3V2NEO_*`) and typing accidents (`ffggh`, `sdf`),
so the descriptions below are inferred from geometry, not from any source CAD.
Bounding boxes are measured from the meshes.

Print in **PETG or ASA**, not PLA — PLA creeps under sustained load and softens
in direct sun, and a rooftop bracket sees both.

| File | Bounding box (mm) | Triangles | Probably |
|---|---|---|---|
| `CE3V2NEO_difference_1 (2).stl` | 165 × 146 × 78 | 6,548 | Frame corner node (mirror pair with the file below) |
| `CE3V2NEO_difference_1 (5).stl` | 146 × 165 × 78 | 6,620 | Frame corner node (mirror of the above — X and Y swapped) |
| `CE3V2NEO_node (2r.stl` | 80 × 80 × 220 | 41,680 | Tall corner node / spring post housing |
| `CASE.stl` | 152 × 82 × 98 | 517,118 | Electronics enclosure (the black box in the photographs) |
| `joint.stl` | 137 × 88 × 89 | 848 | Frame joint connector |
| `ffggh.stl` | 137 × 88 × 89 | 2,472 | Joint block (same envelope as joint.stl, higher poly) |
| `BEARING.stl` | 163 × 66 × 55 | 43,630 | Linear bearing / rail carriage block |
| `CE3V2NEO_difference_5 (1).stl` | 70 × 70 × 35 | 13,276 | Motor mount plate, 70×70 |
| `CE3V2NEO_difference_3 (2).stl` | 56 × 58 × 35 | 2,624 | Rod support bracket (identical footprint to rodsupport.stl) |
| `rodsupport.stl` | 56 × 58 × 32 | 2,624 | Steel rod support bracket |
| `CE3V2NEO_X-Belt-Holder-GT2.stl` | 50 × 73 × 20 | 87,992 | GT2 belt holder — from a belt-drive variant that was not built |
| `sdf.stl` | 72 × 44 × 17 | 18,988 | Unidentified bracket |
| `CE3V2NdualEO_union_3 (2).stl` | 54 × 50 × 8 | 25,224 | Bearing holder, dual (54 mm wide vs 24 mm) |
| `vents.stl` | 22 × 152 × 6 | 8,468 | Ventilation grille for the enclosure |
| `CE3V2NEO_difference_4 (1).stl` | 49 × 25 × 11 | 14,872 | Small clamp / retainer |
| `CE3V2NEO_union_bearingggggggggggg3 (2).stl` | 24 × 50 × 8 | 12,624 | Bearing holder, single |
| `bearinghod323.stl` | 20 × 41 × 7 | 12,300 | Bearing hood / cap |

## Notes

- `CE3V2NEO_difference_1 (2)` and `(5)` have swapped X/Y extents (165×146 vs
  146×165) — a mirrored pair, so print one of each.
- `ffggh.stl` and `joint.stl` occupy the same 137 × 88 × 89 envelope at very
  different triangle counts (2,472 vs 848). Almost certainly the same part
  exported twice; print `ffggh.stl`.
- `CE3V2NEO_difference_3 (2).stl` and `rodsupport.stl` share a 56 × 58.5
  footprint and differ only in height (35.2 vs 32.4 mm).
- `CASE.stl` is 25 MB and 517,118 triangles for a simple box. Decimate it
  before slicing unless you enjoy waiting.
- No source CAD (STEP, F3D, SCAD) was committed, so these meshes cannot be
  parametrically modified. If the rail diameter or motor changes, the parts
  have to be remodelled.
