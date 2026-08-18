# ifc-compare

IFC model diff tool: compares two versions of an IFC file by GlobalId and generates
an HTML report with a **synchronized dual 3D viewer** for design change reviews.

## Features

- **Element-level diff** (Python + IfcOpenShell)
  - Compares by GlobalId with six change states:
    Added (green) / Deleted (pure red) / Geometry (yellow) / Parameters (blue) / Both (purple) / Unchanged (gray)
  - Property diff: IfcPropertySet, IfcElementQuantity, list values, type-entity property
    sets (IfcWallType etc.), and material/layer changes — including added, removed and
    modified properties, plus name / type changes
  - Geometry signature: a canonical hash of the Representation + ObjectPlacement entity
    tree detects "geometry / position changed" (profile size, extrusion depth, moves)
    without meshing
  - Classification rule: geometry-change × parameter-change matrix. Quantity/Dimensions
    values that follow geometry are shown as "Qty" and do not promote to "Both";
    Revit metadata (Edited by / Created by etc.) is ignored
- **HTML report + dual 3D viewer** (Three.js, zero build, offline-ready)
  - Side-by-Side: drag the divider; left = old model, right = new model, fully
    synchronized rotation / zoom (photo-compare slider principle)
  - Merged View: all elements of both models rendered in one view with status colors;
    overlapping old/new elements are blended so both remain visible
  - Status toggles: show/hide each of the six states individually
  - Fit View / Focus Changes: focus the camera on all changed elements
  - Change list: grouped by state, searchable (name / type / GUID), with old/new value tables
  - In-page "Load IFC": pick two files and compare them locally (no internet upload)
- **Sample model generator**: IFC4 samples covering all six change states
- **inspect**: list the property sets inside an IFC file to check whether custom
  parameters were exported

## Install

```bash
pip install -r requirements.txt   # only requires ifcopenshell>=0.7
```

## Usage

```bash
# 1. Generate sample files (optional, for a quick tour)
python cli.py samples

# 2. Compare two IFC files
python cli.py compare samples/sample_v1.ifc samples/sample_v2.ifc -o out

# 3. Serve the report locally
python cli.py serve out
# open http://localhost:8080/report.html
```

Alternatively, open the report page and use the **Load IFC** button to pick the old and
new files — the comparison runs through the local server (files never leave your machine).

Check which property sets an IFC file contains:

```bash
python cli.py inspect model.ifc
```

Note: the report uses ES modules + importmap and must be served over HTTP
(`cli.py serve`). Opening `report.html` directly via `file://` will not work.

`compare` options:

- `-o, --out`: output directory (default `out`)
- `--jobs`: parallel processes for geometry export (default: CPU count; use 1 if a platform misbehaves)

## Output layout

```
out/
├── report.html          # main report page (copied from viewer/)
├── diff.json            # diff data (per-element change details)
├── style.css / app.js / lib/   # viewer assets (vendored Three.js, works offline)
└── models/
    ├── old.gltf / old.<hash>.bin   # old model (deleted=red, geometry=yellow, parameters=blue, both=purple, rest=translucent gray)
    └── new.gltf / new.<hash>.bin   # new model (added=green, geometry=yellow, parameters=blue, both=purple, rest=translucent gray)
```

`.bin` filenames carry a content hash so stale browser caches can never mix versions.

## How it works

1. **Diff** (`ifc_compare/diff.py`): collects `IfcElement` / `IfcSpace` from both models,
   matches by GlobalId, and compares properties per element (float rounding, enum
   unwrapping, quantity mapping, list values). The "modified" state is split into
   Geometry / Parameters / Both using the geometry-signature × parameter-change matrix.
2. **Geometry signature**: deterministic serialization of the Representation and
   ObjectPlacement entity trees (placement chain truncated, cycle-safe, mm rounding),
   hashed with SHA-256.
3. **Geometry export** (`ifc_compare/export.py`): triangulates element by element with
   `ifcopenshell.geom.iterator`, buckets by change state, applies placement matrices and
   writes glTF 2.0 (POSITION + indices, external .bin). Vertices are collected in
   float64 and translated to a shared origin before float32 export — large geodetic
   coordinates (e.g. 811000 m) would otherwise cause z-fighting shimmer in the GPU.
4. **Viewer** (`viewer/app.js`): single canvas; Side-by-Side renders both scenes with the
   full-viewport projection and clips each side with `gl.scissor` (photo-compare slider).
   Merged View renders the new scene first and the old scene second without the old
   scene's background quad, with overlap-aware transparency so overlapping old/new
   elements both stay visible. Both scenes share one camera + OrbitControls.

## Troubleshooting

- **Buttons do nothing**: the page was likely opened via `file://`, or the browser does
  not support importmap. Serve with `python cli.py serve out`, open
  http://localhost:8080/report.html, and use a recent Chrome / Edge (120+). The page
  shows a self-check overlay when scripts fail.
- **Blank 3D view / WebGL error**: browser hardware acceleration is disabled — enable it
  and refresh.
- **Load IFC fails**: make sure the page is served by `cli.py serve` (not double-clicked)
  and the URL is http://localhost:8080.
- **Changed parameters are not reported**: custom parameters (Revit shared/project
  parameters) are only exported to IFC if the property set mapping is configured.
  Run `python cli.py inspect your_model.ifc` to see which property sets exist; if your
  parameter is missing, configure it in Revit's IFC export settings
  (File > Export > IFC > Modify Setup > Property Sets). Also note that element
  dimensions and positions are geometry in IFC — changing them is classified as
  "Geometry" (yellow), not a parameter change.

## Known limitations (v1)

- Only IfcElement / IfcSpace participate; spatial structure (storeys, sites) and
  annotations do not
- Geometry change detection is based on the representation-tree signature — it does not
  distinguish "moved" from "reshaped", and fine mesh-only changes (e.g. freeform
  surfaces) can be missed
- Property values are compared raw, without unit conversion (mm vs m mixes cause
  false positives)
- Large models export to a single glTF each; first-load time and memory are higher —
  raise `--jobs` for big comparisons
- Tested on Python ≥ 3.8 with ifcopenshell 0.7 / 0.8

## Roadmap

- Per-element meshes with click-to-highlight (list ↔ 3D)
- Unit conversion and IFC schema normalization (IFC2X3 ↔ IFC4)
- Tiling / Draco compression and streaming for very large models
- Spatial filtering (by storey) and property whitelists
