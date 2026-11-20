---
name: cad-import-orchestrator
description: End-to-end multi-agent CAD (DWG/DXF) to DEXPI 2.0.1 P&ID XML and ChemE-as-Code YAML orchestration. Covers DWG-to-DXF conversion, deterministic entity cataloging, remote-origin INSERT block rescue, parallel specialist dispatch, deterministic overlap arbitration with zero residual artwork, visual crop verification, validation gates, and human-in-the-loop CAD anomaly reporting.
---

# CAD Import Orchestrator

## Purpose and when to use

Use this skill as the coordinator when converting an engineering CAD P&ID sheet (`.dwg` / `.dxf`) into a faithful DEXPI 2.0.1 XML file (`pid/<sheet>.xml`) synchronized with ChemE-as-Code YAML lists, without losing CAD geometry, duplicating shared lines across objects, or silently fabricating unverified engineering data.

## Required inputs

- Source CAD drawing (`.dwg` or `.dxf`) and sheet index / frame boundary (`X0, Y0, X1, Y1` in sheet mm, top-left origin or DXF modelspace transform).
- Target plant repository root containing `plant.yaml`.
- Specialist JSON schemas or delegation targets covering five domains:
  1. `cad-import-equipment`
  2. `cad-import-valves-fittings`
  3. `cad-import-piping`
  4. `cad-import-instruments`
  5. `cad-import-spec-breaks`

## Workflow and rules

### Phase 1 — Export DXF and build a deterministic entity catalog

1. If starting from `.dwg`, convert to ASCII `.dxf` using AutoCAD Core Console (`accoreconsole.exe`) or an equivalent lossless converter.
2. Parse modelspace (`msp`) once in fixed order and assign every entity an immutable `msp_idx` alongside DXF handle, DXF type, layer, color, block name, bounding box, and text content.
3. Freeze `sheet_<id>_catalog.json` before dispatching domain specialists so every worker references the exact same `msp_idx` integers.

### Phase 2 — Rescue remote-origin INSERT blocks

- **Critical CAD pitfall**: CAD designers frequently create anonymous or named `INSERT` blocks (for instrument seals, gauge assemblies, breather/conservation valves, or off-page connector boxes) whose block origin sits far outside the sheet frame while its translated/exploded sub-entities land directly inside the sheet bbox.
- Never filter sheet entities solely by `INSERT.dxf.insert` point. Expand candidate `INSERT` entities via virtual entities (`entity.virtual_entities()`), compute the transformed geometry bbox in sheet coordinates, and rescue any `INSERT` whose expanded geometry intersects the sheet bbox even when its base point is remote.

### Phase 3 — Dispatch the 5 domain specialists in parallel

Dispatch each domain worker with the frozen catalog, sheet bbox, rescued remote-block inventory, and coordinate transform:

1. `cad-import-equipment`: vessels, tanks, pumps, packages, nozzles, equipment banners.
2. `cad-import-valves-fittings`: inline valves, special items, flanges, blind flanges, reducers.
3. `cad-import-piping`: `PipingNetworkSystem`, `PipingNetworkSegment`, clickable `PipeTee` junctions, off-page connectors, shared branch geometry.
4. `cad-import-instruments`: `ProcessInstrumentationFunction`, measuring/signal/pneumatic/interlock lines, instrument loops and cause-effect entries, rescued gauge isolation valves.
5. `cad-import-spec-breaks`: native DEXPI `PropertyBreak` transitions, boundary governing piping class, `piping/pipe-specs.yaml` registration.

Each worker outputs a structured JSON spec referencing exact `msp_idx` indices and remote block handles rather than mutating the final XML independently.

### Phase 4 — Deterministic overlap arbitration and assembly

Boundary entities (nozzle stubs, shared junction lines, pipe labels near valves/spec breaks) often appear in more than one specialist proposal. Resolve ownership once in a single deterministic assembler using priority tiers:

1. `SpecBreaks` (class-change markers & break labels)
2. `Equipment` (vessels, pumps, packages, nozzles)
3. `Valves & Fittings` (inline valves, reducers, flanges, special items)
4. `Instruments` (bubbles, transmitters, signal/impulse lines)
5. `Piping` (remaining pipe segments and line number labels)
6. Title-block metadata texts (non-process border/frame annotations)

Every graphic entity inside the active sheet must end with a single owner: enforce `sheet_artwork_residual == 0` in the assembly report.

### Phase 5 — Render, inspect zoomed crops, and validate

1. Assemble `pid/<sheet>.xml` using `pydexpi` and sync lists (`equipment/`, `piping/`, `control/`) via ChemE-as-Code rules.
2. Render full-sheet PNG and zoomed `--crop` images around dense equipment heads, pump skids, rescued remote blocks, and sheet margins:
   - `cheme render --png`
   - `cheme render --png --crop X0,Y0,X1,Y1 --scale 6`
3. Visually inspect the rendered PNGs to verify nearby text labels (e.g., `DN` labels near reducers vs. root valves) belong to the symbol they actually sit beside.
4. Run `cheme validate` and require `0 error(s)`.

## Human-in-the-loop CAD anomaly gate

Never silently alter ambiguous or contradictory original CAD engineering content (such as an instrument tag omitting a pump suffix, a mismatched reducer label vs. nominal line size, unlabelled suction/discharge reducers, or uncertain equipment nozzle piping class). Collect every real source-drawing discrepancy into a structured review list and ask the human engineer which items to preserve faithfully versus repair in a separate explicit commit.
