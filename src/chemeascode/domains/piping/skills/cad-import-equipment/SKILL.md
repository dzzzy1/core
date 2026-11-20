---
name: cad-import-equipment
description: Specialist skill for extracting process equipment (vessels, tanks, pumps, packages), nozzles, title banners, and EquipmentList entries from a CAD DXF entity catalog into DEXPI 2.0.1 objects.
---

# CAD Import — Equipment & Nozzles Specialist

## Purpose and when to use

Use this skill when extracting major process equipment, package skids, nozzle ports, and top/bottom equipment identification banners from a frozen CAD DXF entity catalog (`sheet_<id>_catalog.json`).

## Required inputs

- Frozen DXF entity catalog with `msp_idx`, layer, block name, text strings, and sheet-mm coordinates.
- Equipment scope rules and target DEXPI classes (`Vessel`, `Tank`, `CentrifugalPump`, `Equipment`, `Nozzle`, `PipingNode`).

## Workflow and rules

1. **Identify equipment bodies and banners**:
   - Group body lines, circles, arcs, polylines, and `INSERT` symbols belonging to each vessel, storage tank, pump, or vendor package.
   - Pair duplicated equipment callout banners (top title block callouts or bottom equipment labels) with their parent `Equipment` object using `Static` or label groups so they highlight together in viewers.
2. **Extract nozzles strictly with port coordinates**:
   - Enumerate every drawn process, utility, relief, level-bridle, drain, vent, spare, and instrument nozzle on the equipment outline.
   - Assign clean nozzle sub-tags (`N1`, `N2`, `P1`, `P2`, `L1`, `L2A`, `L2B`, etc.) preserving the exact CAD labels where visible.
   - Record each nozzle's connection point (`PipingNodePosition`) directly on the outer flange face or nozzle tip in sheet mm so downstream piping segments snap cleanly without gaps.
   - Attach nominal diameter (`DN`) when explicitly labeled on the nozzle or unequivocally inherited from its connected line; leave unset (`None`) when indeterminate (e.g., unassigned blinded spare nozzles) rather than guessing.
3. **Rescued instrument taps on equipment heads**:
   - Check remote-origin `INSERT` gauge/transmitter blocks anchored over top instrument nozzles (`P1`, `P2`, `L1`, etc.) and ensure the corresponding nozzle object and port coordinate exist in the equipment spec even if the gauge isolation valve belongs to the instrument or valve specialist.
4. **Sync `equipment/equipment-list.yaml` and separate CAD dimension callouts**:
   - Keep pure CAD dimension callouts (extension lines, arrowheads, height/elevation/span texts) in dedicated drawing-level `<Static id="Dim_*">` groups rather than bundling them into equipment `RepresentationGroup`s, preventing false `label-overlap` warnings and keeping vessel outlines clean when clicked.
   - Populate `tag`, `type`/`name`, `sheet`, structured `dimensions` (`diameter`, `height`/`length`, `source`), and `notes` observable on the P&ID (including extracted seal depths or sight-glass spans), plus instrument span notes on the related gauge/level indicator.
   - Leave unstated design pressures, design temperatures, corrosion allowances, metallurgy, and pump duties blank (`null` / omitted) pending process datasheets.

## Output schema / hand-off

Emit `spec_<id>_equipment.json` containing:
- `equipment`: list of `{id, tag, name, dexpi_class, category, body_msp_idxs, banner_msp_idxs, tag_text_msp_idxs, nozzles: [{subtag, dn, piping_class, pos, geom_msp_idxs, text_msp_idxs}]}`
- `banners`: standalone title callouts mapped to their equipment tags.

## Validation and human review gate

- Confirm every process line touching an equipment outline terminates at an explicit `Nozzle` ID rather than floating near the shell.
- Flag ambiguous equipment specs (e.g., an atmospheric/low-pressure tank whose nozzle classes follow connected vent headers by convention, or missing pump nozzle ratings) for human confirmation instead of inventing datasheet values.
