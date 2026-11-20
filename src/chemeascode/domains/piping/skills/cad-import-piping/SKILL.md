---
name: cad-import-piping
description: Specialist skill for reconstructing DEXPI 2.0.1 PipingNetworkSystem line groups, ordered PipingNetworkSegment chains, clickable PipeTee branch junctions (with line-number transitions), and FlowIn/FlowOutPipeOffPageConnector topology from CAD DXF catalogs.
---

# CAD Import — Piping & Topology Specialist

## Purpose and when to use

Use this skill when turning disjoint CAD line segments, branch junctions, line-number text strings, and sheet-edge arrows into a continuous, queryable DEXPI 2.0.1 piping topology (`PipingNetworkSystem`, `PipingNetworkSegment`, `PipeTee`, `FlowInPipeOffPageConnector`, `FlowOutPipeOffPageConnector`).

## Required inputs

- Frozen DXF entity catalog (`sheet_<id>_catalog.json`) plus rescued remote-origin `图框` / OPC arrow blocks.
- Nozzle coordinates from `cad-import-equipment`, inline port coordinates from `cad-import-valves-fittings`, and class-break points from `cad-import-spec-breaks`.

## Workflow and rules

1. **Parse line designations systematically**:
   - Decode standard P&ID line numbers (e.g., `<DN>-<Fluid>-<Seq>-<Class>` with optional tracing/insulation suffix) into `NominalDiameter`, `FluidCode`, `LineNumber`, `PipingClassCode`, and `InsulationType`.
   - Group parallel/continuous branches under their owning `PipingNetworkSystem` and register every main process/utility line in `piping/line-list.yaml` with accurate `from` and `to` endpoints.
2. **Build continuous, ordered `PipingNetworkSegment` chains**:
   - Walk monotonically from each upstream source (`Equipment:Nozzle`, `PipeTee`, `PropertyBreak`, or `FlowInPipeOffPageConnector`) through ordered inline components (`[Flange, Valve, SpecialItem, Reducer, ...]`) to a single downstream target (`Nozzle`, `PipeTee`, `PropertyBreak`, or `FlowOutPipeOffPageConnector`).
   - Avoid floating orphaned line polylines: split corner/cross geometry cleanly at branch tees, valves, and spec breaks so viewer selection highlights the exact segment and exposes clickable upstream/downstream connections.
3. **Model every branch junction as a clickable `PipeTee`**:
   - Every three-way (or four-way split) intersection must become a first-class `PipeTee` object with distinct ports (`N1`, `N2`, `N3`) connecting the main run and branch segments.
   - **Line-number boundaries at tees**: whenever a branch leaves a main line under a different `LineNumber` (e.g., main `<DN1>-<Fluid>-<SeqA>-<Class>` feeding branch `<DN2>-<Fluid>-<SeqB>-<Class>`), preserve each segment under its own `PipingNetworkSystem` and connect both at the shared `PipeTee`. Viewers and API inspectors rely on differing `via_tag` values across the tee's connected segments to render the line-number transition (`管道号分界`) and mixed port DNs (`DN100 × DN50`).
4. **Use directional Off-Page Connectors (`pydexpi` schema constraint)**:
   - Bare `PipeOffPageConnector` is neither a `PipingSourceItem` nor a `PipingTargetItem` in DEXPI 2.0.1 / `pydexpi`.
   - Always instantiate `FlowInPipeOffPageConnector` for incoming sheet/battery-limit/open-drain/utility sources and `FlowOutPipeOffPageConnector` for outgoing sheet/vent/drain/instrument-tap destinations.
   - Attach rescued sheet-margin OPC arrow `INSERT` graphics and destination sheet references (`<sheet_no>` + service description) directly to the matching OPC object.

## Output schema / hand-off

Emit `spec_<id>_piping.json` containing:
- `piping_systems`: line metadata (`line_number`, `dn`, `fluid`, `piping_class`, `insulation`, `label_text_msp_idxs`)
- `segments`: ordered endpoint/component topological definitions (`id`, `source`, `components`, `target`, `dn`, `piping_class`, `pipe_geom_msp_idxs`)
- `tees`: `{id, pos, main_dn, branch_dn, line_transitions}`
- `off_page_connectors`: `{id, direction, label, target_sheet, pos, geom_msp_idxs, text_msp_idxs, remote_handle}`

## Validation and human review gate

- Verify zero disconnected inline components (`SourceItem`/`TargetItem` valid IDs in `pydexpi` schema).
- Preserve original CAD interior OPC arrow coordinates (even if flagged by `offpage-not-on-edge` warnings) and flag layout relocations to the user instead of altering original drawing positions silently.
