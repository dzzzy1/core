"""Standalone DEXPI 2.0.1 native attribute inspection in ``_inspect_pid_object`` (without YAML)."""

from pathlib import Path

import pydexpi.pydexpi_classes as pc
from pydexpi.loaders.xml_serializer import DexpiXmlModelMetadata, DexpiXmlSerializer

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
