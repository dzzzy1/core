"""Standalone DEXPI 2.0.1 native attribute inspection in ``_inspect_pid_object`` (without YAML)."""

from pathlib import Path

import pydexpi.pydexpi_classes as pc
from pydexpi.loaders.xml_serializer import DexpiXmlModelMetadata, DexpiXmlSerializer
from pydexpi.validation import validate as dexpi_validate

from chemeascode.domains.piping.show import Sheet
from chemeascode.interfaces.api.serve import _inspect_pid_object


def _write_sample_dexpi(path: Path) -> Path:
    pm = pc.PlantModel(id="PlantModel1")

    vessel = pc.Vessel(
        id="Equip_V_12107",
        tagName="V-12107",
        equipmentDescription=pc.MultiLanguageString(
            singleLanguageStrings=[pc.SingleLanguageString(language="zh", value="液封罐")]
        ),
        chambers=[
            pc.Chamber(
                id="Equip_V_12107_Chamber1",
                nominalDiameter=pc.PhysicalQuantity(value=500.0, unit=pc.LengthUnit.MILLIMETRE),
                height=pc.PhysicalQuantity(value=1.0, unit=pc.LengthUnit.METRE),
            )
        ],
    )
    pm.taggedPlantItems.append(vessel)

    breather = pc.BreatherValve(
        id="Sp_HX_121101",
        positionNumber="HX-121101",
        setPressureHigh=pc.PhysicalQuantity(value=3.0, unit=pc.PressureGaugeUnit.KILOPASCAL),
        setPressureLow=pc.PhysicalQuantity(value=-0.35, unit=pc.PressureGaugeUnit.KILOPASCAL),
        flowInPipingClassCode="2.5K2",
        flowOutPipingClassCode="2.5K2",
    )
    xv = pc.BallValve(id="Vlv_12XV_1101", pipingComponentNumber="12XV-1101")
    seg = pc.PipingNetworkSegment(id="Seg_1", items=[breather, xv])
    sys_ = pc.PipingNetworkSystem(id="Line_1", lineNumber="80-FG-HX121101-2.5K2", segments=[seg])
    pm.pipingNetworkSystems.append(sys_)

    act = pc.ControlledActuator(
        id="Act_Vlv_12XV_1101",
        deviceTypeName="气动执行机构 (pneumatic)",
        failAction=pc.FailActionClassification.FAILCLOSE,
        failActionRepresentation="FC",
    )
    ovr = pc.OperatedValveReference(id="OVR_Vlv_12XV_1101", valve=xv)
    asys = pc.ActuatingSystem(
        id="ASys_Vlv_12XV_1101",
        controlledActuator=act,
        operatedValveReference=ovr,
    )
    afn = pc.ActuatingFunction(id="AFn_Vlv_12XV_1101", systems=asys)
    pm.actuatingSystems.append(asys)

    pif_field = pc.ProcessInstrumentationFunction(
        id="Inst_12XV_1101",
        processInstrumentationFunctionCategory="12XV",
        processInstrumentationFunctionNumber="1101",
        location=pc.LocationClassification.FIELD,
        actuatingFunctions=[afn],
    )
    pif_dcs = pc.ProcessInstrumentationFunction(
        id="Inst_12PICA_1101",
        processInstrumentationFunctionCategory="12PICA",
        processInstrumentationFunctionNumber="1101",
        location=pc.LocationClassification.CENTRALLOCATION,
    )
    pm.processInstrumentationFunctions.extend([pif_field, pif_dcs])

    model = pc.EngineeringModel(id="EngineeringModel1", conceptualModel=pm)
    xml_bytes = DexpiXmlSerializer(
        export_metadata=DexpiXmlModelMetadata(name="test", uri="urn:test:dexpi")
    ).export_to_bytes(model)
    out = path / "sample.xml"
    out.write_bytes(xml_bytes)
    return out


def _props_map(payload: dict) -> dict[str, str | None]:
    return {p["key"]: p["value"] for p in payload["properties"]}


def test_inspect_pid_object_reads_native_dexpi_without_yaml(tmp_path: Path):
    xml_path = _write_sample_dexpi(tmp_path)
    sheet = Sheet.load(xml_path)

    eq = _inspect_pid_object(sheet, "Equip_V_12107", lists={})
    eq_props = _props_map(eq)
    assert eq["attributes"]["EquipmentDescription"] == "液封罐"
    assert eq_props["service"] == "液封罐"
    assert eq_props["dimensions"] == "Ø500 mm × H 1 m"
    assert all(p["id"] != "Equip_V_12107_Chamber1" for p in eq["parts"])

    hx = _inspect_pid_object(sheet, "Sp_HX_121101", lists={})
    hx_props = _props_map(hx)
    assert hx_props["PipingClassCode"] == "2.5K2"
    assert hx_props["SetPressure"] == "+3 kPa(g) (0.03 barg)"
    assert hx_props["VacuumSetPressure"] == "-0.35 kPa(g) (-0.0035 barg)"

    xv = _inspect_pid_object(sheet, "Vlv_12XV_1101", lists={})
    xv_props = _props_map(xv)
    assert xv_props["FailPosition"] == "FC (故障关)"
    assert xv_props["Operation"] == "气动执行机构 (pneumatic)"
    assert any(
        c["other_id"] == "Inst_12XV_1101" and c["summary"].startswith("控制信号 ←")
        for c in xv["connections"]
    )

    pif_dcs = _inspect_pid_object(sheet, "Inst_12PICA_1101", lists={})
    assert _props_map(pif_dcs)["Location"] == "中控室 (DCS)"

    pif_field = _inspect_pid_object(sheet, "Inst_12XV_1101", lists={})
    assert _props_map(pif_field)["Location"] == "现场 (Field)"
    assert any(
        c["other_id"] == "Vlv_12XV_1101" and c["summary"].startswith("驱动 →")
        for c in pif_field["connections"]
    )


def test_pdms_branch_aggregation_and_validation(tmp_path: Path):
    pm = pc.PlantModel(id="PlantModel1")
    v1_n2 = pc.PipingNode(id="V1_N2_Node", nominalDiameterNumericalValueRepresentation="100")
    v1_noz = pc.Nozzle(id="Equip_V1_Noz_N2", subTagName="N2", nodes=[v1_n2])
    v1 = pc.Vessel(id="Equip_V1", tagName="V-101", nozzles=[v1_noz])

    p1_n1 = pc.PipingNode(id="P1_N1_Node", nominalDiameterNumericalValueRepresentation="100")
    p1_noz = pc.Nozzle(id="Equip_P1_Noz_N1", subTagName="N1", nodes=[p1_n1])
    p1 = pc.CentrifugalPump(id="Equip_P1", tagName="P-101A", nozzles=[p1_noz])

    p2_n1 = pc.PipingNode(id="P2_N1_Node", nominalDiameterNumericalValueRepresentation="32")
    p2_noz = pc.Nozzle(id="Equip_P2_Noz_N1", subTagName="N1", nodes=[p2_n1])
    p2 = pc.ReciprocatingPump(id="Equip_P2", tagName="P-101B", nozzles=[p2_noz])
    pm.taggedPlantItems.extend([v1, p1, p2])

    t1_n1 = pc.PipingNode(id="Tee1_N1", nominalDiameterNumericalValueRepresentation="100")
    t1_n2 = pc.PipingNode(id="Tee1_N2", nominalDiameterNumericalValueRepresentation="100")
    t1_n3 = pc.PipingNode(id="Tee1_N3", nominalDiameterNumericalValueRepresentation="50")
    tee1 = pc.PipeTee(id="Tee_Tanker", nodes=[t1_n1, t1_n2, t1_n3])

    t2_n1 = pc.PipingNode(id="Tee2_N1", nominalDiameterNumericalValueRepresentation="100")
    t2_n2 = pc.PipingNode(id="Tee2_N2", nominalDiameterNumericalValueRepresentation="100")
    t2_n3 = pc.PipingNode(id="Tee2_N3", nominalDiameterNumericalValueRepresentation="20")
    tee2 = pc.PipeTee(id="Tee_PumpB", nodes=[t2_n1, t2_n2, t2_n3])

    t3_n1 = pc.PipingNode(id="Tee3_N1", nominalDiameterNumericalValueRepresentation="32")
    t3_n2 = pc.PipingNode(id="Tee3_N2", nominalDiameterNumericalValueRepresentation="32")
    t3_n3 = pc.PipingNode(id="Tee3_N3", nominalDiameterNumericalValueRepresentation="32")
    tee3 = pc.PipeTee(id="Tee_Relief", nodes=[t3_n1, t3_n2, t3_n3])

    red_n1 = pc.PipingNode(id="Red_N1", nominalDiameterNumericalValueRepresentation="20")
    red_n2 = pc.PipingNode(id="Red_N2", nominalDiameterNumericalValueRepresentation="32")
    red = pc.PipeReducer(id="Red_B1", pipingComponentNumber="R1", nodes=[red_n1, red_n2])

    opc_tk_n = pc.PipingNode(id="OPC_TK_N", nominalDiameterNumericalValueRepresentation="50")
    opc_tk = pc.FlowInPipeOffPageConnector(id="OPC_TK", pipeConnectorNumber="TK-IN", nodes=[opc_tk_n])

    opc_rel_n = pc.PipingNode(id="OPC_REL_N", nominalDiameterNumericalValueRepresentation="32")
    opc_rel = pc.FlowInPipeOffPageConnector(id="OPC_REL", pipeConnectorNumber="REL-IN", nodes=[opc_rel_n])

    PRI = pc.PrimarySecondaryPipingNetworkSegmentClassification.PRIMARYPIPINGNETWORKSEGMENT
    SEC = pc.PrimarySecondaryPipingNetworkSegmentClassification.SECONDARYPIPINGNETWORKSEGMENT

    s_b1_1 = pc.PipingNetworkSegment(
        id="Seg_100_1", segmentNumber="100-P-101-2.5K2/B1", primarySecondaryPipingNetworkSegment=PRI,
        items=[tee1],
        connections=[pc.Pipe(id="P_1", sourceItem=v1_noz, sourceNode=v1_n2, targetItem=tee1, targetNode=t1_n1)],
        sourceItem=v1_noz, sourceNode=v1_n2, targetItem=tee1, targetNode=t1_n1,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="100",
    )
    s_b1_2 = pc.PipingNetworkSegment(
        id="Seg_100_2", segmentNumber="100-P-101-2.5K2/B1", primarySecondaryPipingNetworkSegment=PRI,
        items=[tee2],
        connections=[pc.Pipe(id="P_2", sourceItem=tee1, sourceNode=t1_n2, targetItem=tee2, targetNode=t2_n1)],
        sourceItem=tee1, sourceNode=t1_n2, targetItem=tee2, targetNode=t2_n1,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="100",
    )
    s_b1_3 = pc.PipingNetworkSegment(
        id="Seg_100_3", segmentNumber="100-P-101-2.5K2/B1", primarySecondaryPipingNetworkSegment=PRI,
        items=[],
        connections=[pc.Pipe(id="P_3", sourceItem=tee2, sourceNode=t2_n2, targetItem=p1_noz, targetNode=p1_n1)],
        sourceItem=tee2, sourceNode=t2_n2, targetItem=p1_noz, targetNode=p1_n1,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="100",
    )
    s_b2 = pc.PipingNetworkSegment(
        id="Seg_100_TK", segmentNumber="100-P-101-2.5K2/B2", primarySecondaryPipingNetworkSegment=SEC,
        items=[opc_tk],
        connections=[pc.Pipe(id="P_4", sourceItem=opc_tk, sourceNode=opc_tk_n, targetItem=tee1, targetNode=t1_n3)],
        sourceItem=opc_tk, sourceNode=opc_tk_n, targetItem=tee1, targetNode=t1_n3,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="50",
    )
    sys1 = pc.PipingNetworkSystem(
        id="Line_100", lineNumber="100-P-101-2.5K2", segments=[s_b1_1, s_b1_2, s_b1_3, s_b2],
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="100",
    )

    s_20_1 = pc.PipingNetworkSegment(
        id="Seg_20_1", segmentNumber="20-P-105-2.5K2/B1", primarySecondaryPipingNetworkSegment=PRI,
        items=[red, tee3],
        connections=[
            pc.Pipe(id="P_5", sourceItem=tee2, sourceNode=t2_n3, targetItem=red, targetNode=red_n1),
            pc.Pipe(id="P_6", sourceItem=red, sourceNode=red_n2, targetItem=tee3, targetNode=t3_n1),
        ],
        sourceItem=tee2, sourceNode=t2_n3, targetItem=tee3, targetNode=t3_n1,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="20",
    )
    s_20_2 = pc.PipingNetworkSegment(
        id="Seg_20_2", segmentNumber="20-P-105-2.5K2/B1", primarySecondaryPipingNetworkSegment=PRI,
        items=[],
        connections=[pc.Pipe(id="P_7", sourceItem=tee3, sourceNode=t3_n2, targetItem=p2_noz, targetNode=p2_n1)],
        sourceItem=tee3, sourceNode=t3_n2, targetItem=p2_noz, targetNode=p2_n1,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="32",
    )
    s_20_3 = pc.PipingNetworkSegment(
        id="Seg_20_Rel", segmentNumber="20-P-105-2.5K2/B2", primarySecondaryPipingNetworkSegment=SEC,
        items=[opc_rel],
        connections=[pc.Pipe(id="P_8", sourceItem=opc_rel, sourceNode=opc_rel_n, targetItem=tee3, targetNode=t3_n3)],
        sourceItem=opc_rel, sourceNode=opc_rel_n, targetItem=tee3, targetNode=t3_n3,
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="32",
    )
    sys2 = pc.PipingNetworkSystem(
        id="Line_20", lineNumber="20-P-105-2.5K2", segments=[s_20_1, s_20_2, s_20_3],
        fluidCode="P", pipingClassCode="2.5K2", nominalDiameterNumericalValueRepresentation="20",
    )

    pm.pipingNetworkSystems.extend([sys1, sys2])
    model = pc.EngineeringModel(id="EngineeringModel1", conceptualModel=pm)

    assert dexpi_validate(model) == []

    xml_path = tmp_path / "branches.xml"
    xml_path.write_bytes(
        DexpiXmlSerializer(export_metadata=DexpiXmlModelMetadata(name="btest", uri="urn:test:branches")).export_to_bytes(model)
    )
    sheet = Sheet.load(xml_path)

    # Picking ANY sub-segment of B1 resolves the full PDMS Branch
    mid = _inspect_pid_object(sheet, "Seg_100_2", lists={})
    mid_props = _props_map(mid)
    assert mid["highlight_ids"] == ["Seg_100_1", "Seg_100_2", "Seg_100_3"]
    assert mid_props["Segment"] == "100-P-101-2.5K2/B1"
    assert mid_props["BranchRole"] == "主管 (Primary)"
    assert mid_props["from"] == "V-101:N2"
    assert mid_props["to"] == "P-101A:N1"
    peer_tags = {c["other_tag"] for c in mid["connections"] if c["direction"] == "peer"}
    assert peer_tags == {"100-P-101-2.5K2/B2", "20-P-105-2.5K2/B1"}

    # Multi-bore DN along 20-P-105-2.5K2/B1
    b20 = _inspect_pid_object(sheet, "Seg_20_1", lists={})
    assert _props_map(b20)["DN"] == "DN20 → DN32"

    # Line lists deduplicated PDMS Branches instead of raw sub-segment count
    sys1_info = _inspect_pid_object(sheet, "Line_100", lists={})
    assert _props_map(sys1_info)["Segment"] == "2 个分支"
    branches_in_parts = [p for p in sys1_info["parts"] if p["class"] == "Branch"]
    assert [(b["tag"], b["connected_to"]) for b in branches_in_parts] == [
        ("100-P-101-2.5K2/B1", "V-101:N2 → P-101A:N1"),
        ("100-P-101-2.5K2/B2", "TK-IN → Tee_Tanker"),
    ]

    # Equal-bore 32/32/32 tee inside DN20->DN32 line keeps DN32 & cross-line transition on Tee_PumpB
    t_rel = _inspect_pid_object(sheet, "Tee_Relief", lists={})
    assert _props_map(t_rel)["DN"] == "DN32"
    t_pumpb = _inspect_pid_object(sheet, "Tee_PumpB", lists={})
    assert _props_map(t_pumpb)["line_transition"] == "100-P-101-2.5K2 ↔ 20-P-105-2.5K2"
