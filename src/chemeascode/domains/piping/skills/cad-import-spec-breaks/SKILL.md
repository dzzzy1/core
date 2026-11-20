---
name: cad-import-spec-breaks
description: Specialist skill for identifying piping-class switch points (PropertyBreak), adjudicating upstream vs. downstream specification conflicts, determining governing boundary valve classes, and maintaining piping/pipe-specs.yaml without fabricating PMS metallurgy.
---

# CAD Import — Spec-Break & Piping-Class Specialist

## Purpose and when to use

Use this skill when a P&ID contains piping class transitions (`PropertyBreak` / 等级分界线), dual-class annotations across valves or branch headers (e.g., `1.6A3 | 2.5K2`, `2.5K2 | 2.5A1`), or ambiguity between upstream process equipment ratings and downstream utility/vent headers.

## Required inputs

- Line designations (`<DN>-<Fluid>-<Seq>-<Class>`) from `cad-import-piping`.
- Candidate inline boundary valves and vessel/pump nozzle connections from `cad-import-valves-fittings` and `cad-import-equipment`.
- Any project Piping Material Specification (PMS) in `piping/pipe-specs.yaml` (if already available).

## Workflow and rules

1. **Locate every class transition and emit native DEXPI `PropertyBreak`**:
   - Detect drawn CAD spec-break ticks and dual-class text labels (`<ClassA> / <ClassB>`).
   - Instantiate each transition as a native DEXPI 2.0.1 `PropertyBreak` with `pipingClassBreak = PipingClassBreakClassification.PIPINGCLASSBREAK`, a `PipingClassBreakLabel` graphic group holding the class texts, and connected `UpstreamClass` / `DownstreamClass` piping nodes.
   - Connect `PropertyBreak` directly as a topological node (`SourceItem` / `TargetItem`) between the upstream segment and downstream segment so piping-class provenance traces continuously.
2. **Determine exact switch point and governing boundary valve class**:
   - The boundary isolation valve straddling a spec break must be rated to the **governing (more stringent) class** across the transition:
     1. Compare nominal pressure first (higher MPa / PN rating governs, e.g., `2.5` > `1.6`).
     2. When nominal pressures are equal (tie, e.g., `2.5K2` vs. `2.5A1`), higher corrosion-resistance / alloy material series governs (`K / L / S > E > C / D > B > A`, so `2.5K2` governs over carbon-steel `2.5A1`).
   - Check process isolation logic: the transition normally sits on the utility/vent side *immediately downstream* of the process block valve (`PropertyBreak` placed right after the boundary valve), ensuring the boundary valve handles process media under the higher/more corrosion-resistant class, and utility check/isolation downstream sees non-backflow utility service.
3. **Register discovered piping classes conservatively in `piping/pipe-specs.yaml`**:
   - Record every unique class code (`code`, `rating` derived from MPa prefix such as `PN16` / `PN25`, `colour`, and engineering `notes`).
   - **No fabrication rule**: leave `material: ""` empty whenever the project PMS table has not yet been provided. Downstream viewers automatically fall back to pressure/series group (`PN25 / K系`) until PMS metallurgy is supplied.
4. **Cross-check boundary consistency across domains**:
   - Compare every connected valve, flange, tee, and nozzle near each transition so no boundary valve silently retains the weaker piping class.
   - **Equipment trim inheritance rule**: when an equipment item (such as a vessel, knockout drum, or local pot) has dedicated level bridles, level isolation valves, or drain valves without their own line-number piping class callout, assign their piping class according to the piping line directly connected to that equipment unless an explicit `PropertyBreak` intervenes.

## Output schema / hand-off

Emit `spec_<id>_specbreaks.json` containing:
- `spec_breaks`: list of `{id, line, upstream_class, downstream_class, governing_class, boundary_valve_id, change_note, pos, geom_msp_idxs, text_msp_idxs}`
- `boundary_class_overrides`: map `{component_id: governing_class}` applied during master assembly.
- `pipe_specs_entries`: conservative class summaries for `piping/pipe-specs.yaml`.

## Validation and human review gate

- Confirm every `PropertyBreak` connects segments with distinct `PipingClassCode`s on its upstream and downstream ports.
- Flag uncertain equipment/boot/drain class assignments (e.g., an auxiliary pot inheriting a connected vent-header class instead of main process class when both touch it) for human verification against the vessel datasheet.
