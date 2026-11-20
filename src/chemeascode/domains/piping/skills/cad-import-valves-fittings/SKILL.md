---
name: cad-import-valves-fittings
description: Specialist skill for extracting valves, inline special items, flanges, blind flanges, and reducers from a CAD DXF catalog into DEXPI 2.0.1 PipingComponent objects without fabricating missing CAD details.
---

# CAD Import — Valves & Fittings Specialist

## Purpose and when to use

Use this skill when extracting all inline and trim piping components (gate, globe, ball, butterfly, check, control, safety/relief, solenoid valves; flame arresters, breather valves, strainers, sample coolers, inline couplings; weld-neck/slip-on flanges, spectacles/blind flanges, concentric/eccentric reducers) from a frozen CAD DXF entity catalog.

## Required inputs

- Frozen DXF entity catalog (`sheet_<id>_catalog.json`) plus rescued remote-origin `INSERT` inventory.
- Upstream line-number & piping-class context (or joint boundary coordinates with piping & spec-break specialists).

## Workflow and rules

1. **Classify inline components to DEXPI 2.0.1 classes**:
   - Map each CAD symbol cleanly (`GateValve`, `CheckValve`, `ControlValve`, `SafetyValve`, `PipeReducer`, `PipeFlange`, `BlindFlange`, `PipingComponent` / special item).
   - Assign exact entry and exit port coordinates (`p1`, `p2` in sheet mm) along the piping centerline so adjacent segments terminate cleanly at node ports without overlapping valve internals.
2. **Tagging discipline (`C1..CN` vs. Special Items)**:
   - Inline manual valves, routine check valves, flanges, blind flanges, and standard reducers without an engineering tag use sequential component numbers `C1`, `C2`, ..., `CN` (`PipingComponentNumber`).
   - Only specialty piping items (e.g., strainers, breather valves, flame arresters, quick couplings, packaged silencers) and tagged safety/automated valves (`PSV-*`, `XV-*`, `PV-*`, `XSV-*`) carry real engineering tags and are registered in `piping/special-items.yaml`.
3. **Visual fidelity rule — do NOT invent drawn flanges on simplified CAD valves**:
   - Engineering P&IDs frequently draft inline valves using simplified flangeless bowties (`><`) even on flanged piping classes.
   - Preserve original CAD geometry faithfully: do **not** synthesize extra flange geometry lines on simplified flangeless valves.
   - Keep `EndConnection` / `connectionType` unset (`None` -> displayed as `—` in the inspector) unless explicitly annotated in CAD or verified from the project Piping Material Specification (PMS).
4. **Rescued remote blocks & adjacent text ownership**:
   - When an inline specialty block (such as a breather valve body) or root isolation valve comes from a remote-origin `INSERT`, bind the rescued block handle to the proper `PipingComponent` / `GateValve`.
   - Verify text placement carefully in dense areas (e.g., ensure a `DN25` label beside a gauge root valve goes to that root valve rather than an adjacent main-header reducer).
5. **Handle actuators, fail positions, and set pressures**:
   - Capture observable fail action (`FC`, `FO`, `FL`), actuator type (pneumatic, solenoid, manual gear), and `SET AT ...` pressure values on `PSV` / breather valves from the CAD text. Do not invent missing set pressures.

## Output schema / hand-off

Emit `spec_<id>_fittings.json` containing:
- `items`: list of `{id, tag, dexpi_class, kind, line, dn, piping_class, pos, p1, p2, insert_msp_idxs, geom_msp_idxs, text_msp_idxs, notes}`
- Any boundary valve candidates flagged for review by `cad-import-spec-breaks`.

## Validation and human review gate

- Confirm `Red_*` reducers record distinct upstream/downstream DNs (`dn1`, `dn2`) when both sizes are known from connected lines or labels.
- Report real CAD reducer mismatches (e.g., reducer size text contradicting the upstream line nominal diameter, or missing pump nozzle reducer text) in the human review list rather than quietly editing drawing text.
