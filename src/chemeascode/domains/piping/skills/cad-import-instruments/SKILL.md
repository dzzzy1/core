---
name: cad-import-instruments
description: Specialist skill for extracting instruments, ProcessInstrumentationFunction loops, measuring/impulse/signal/interlock lines, rescued diaphragm-seal gauge root valves, control loops, and cause-effect entries from CAD DXF catalogs.
---

# CAD Import — Instruments & Signals Specialist

## Purpose and when to use

Use this skill when extracting field transmitters, local gauges, control/alarm/interlock bubbles, signal lines (electrical, software/data link, pneumatic/IA), measuring impulse lines, and SIS/interlock logic tables from a frozen CAD DXF entity catalog.

## Required inputs

- Frozen DXF entity catalog (`sheet_<id>_catalog.json`) plus rescued remote-origin `INSERT` instrument blocks.
- Equipment nozzle tags (`cad-import-equipment`) and inline actuated/control valve tags (`cad-import-valves-fittings`).

## Workflow and rules

1. **Classify instrument bubbles cleanly into `ProcessInstrumentationFunction`**:
   - Pair split tag strings (`<LoopFunc>` top + `<LoopNum>` bottom inside circle/square bubbles) into normalized engineering tags (`<Area><Func>-<Num>`).
   - Distinguish mounting/category cleanly: local field gauges/transmitters, DCS shared display/control (`PICA`, `LIA`, `LIC`), SIS/interlock logic diamonds (`I`, `KCZL`, `HCZS`, `UY`), and local panel/hand switches.
   - Extract high/low alarm setpoints (`H`, `L`, `HH`, `LL`) and descriptive callouts (`罐旁指示仪`, pump status annotations) attached to bubbles.
2. **Rescue composite remote-origin instrument assemblies**:
   - Diaphragm-seal gauges and transmitters are frequently inserted as composite CAD blocks whose block origin sits outside the sheet boundary.
   - Split sub-entities cleanly by ownership rather than dumping the entire composite block onto a valve: keep the **root isolation gate valve** graphics (tap stub, valve flanges, gate valve bowtie) in its inline `GateValve` on the instrument tap `PipingNetworkSegment`, while moving the **diaphragm seal housing, corrugated membrane waves, hatch, capillary stub, and `×` mark** (plus the circular instrument bubble and split tag text) into the **diaphragm pressure gauge/transmitter (`ProcessInstrumentationFunction`)** and recording diaphragm-seal service in `control/instrument-list.yaml`.
   - Verify nearby `DN` text labels on tap lines belong to the root isolation valve/nozzle rather than adjacent headers.
3. **Trace individual signal and measuring groups**:
   - Group dashed electrical, DCS link, pneumatic tubing (`IA`), and mechanical/capillary segments into distinct graphical signal groups linking specific source instruments to target controllers, solenoid valves, or interlock diamonds.
4. **Sync ChemE-as-Code YAML lists under strict schema rules**:
   - Register every instrument in `control/instrument-list.yaml` with observed `tag`, `type`/`function`, `location`, `service`, `loop`, and `sheet`. Leave unstated calibration ranges, failsafe engineering units, and vendor models blank.
   - Register closed regulatory loops in `control/loops.yaml` **without** adding an invalid `sheet` key (the `control/loops.yaml` Pydantic schema forbids extra keys like `sheet`).
   - Place `HH`/`LL` trip actions (e.g., pump trip / `XV` + `XSV` shutdown) in `control/cause-effect.yaml` rather than treating interlock trips solely as operator alarms.

## Output schema / hand-off

Emit `spec_<id>_instruments.json` containing:
- `instruments`: list of `{tag, category, loop, location, alarms, ins_idx, geom_msp_idxs, cat_text_msp_idx, num_text_msp_idx, extra_text_msp_idxs, remote_handle}`
- `signals`: list of `{id, kind, from_tag, to_tag, geom_msp_idxs, text_msp_idxs}`
- `loop_entries` & `cause_effect_entries` matching `chemeascode` control schemas.

## Validation and human review gate

- Confirm `cheme validate` produces zero YAML schema errors on `control/instrument-list.yaml`, `control/loops.yaml`, and `control/cause-effect.yaml`.
- Flag CAD tag typos or omissions (such as an indicator bubble missing a dual-pump `A`/`B` suffix) as `label-tag-unknown` review items for human sign-off rather than silently renaming drawing text.
