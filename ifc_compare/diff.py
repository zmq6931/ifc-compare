"""IFC 模型差异分析：按 GlobalId 比对构件的新增 / 删除 / 属性变化。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import ifcopenshell

from . import VERSION


def collect_elements(model):
    """返回 {GlobalId: entity}，仅包含参与比对的构件（IfcElement / IfcSpace）。"""
    elements = {}
    for entity in model.by_type("IfcProduct"):
        guid = getattr(entity, "GlobalId", None)
        if not guid:
            continue
        if entity.is_a("IfcElement") or entity.is_a("IfcSpace"):
            elements[guid] = entity
    return elements


def _unwrap(value):
    """把 IfcOpenShell 的属性值规范化为 JSON 可序列化的 Python 值。"""
    if isinstance(value, ifcopenshell.entity_instance):
        wrapped = getattr(value, "wrappedValue", None)
        if wrapped is not None:
            return _unwrap(wrapped)
        return value.is_a()
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, tuple):
        return [_unwrap(v) for v in value]
    if isinstance(value, (str, bool, int)):
        return value
    return str(value)


_QUANTITY_VALUES = {
    "IfcQuantityLength": "LengthValue",
    "IfcQuantityArea": "AreaValue",
    "IfcQuantityVolume": "VolumeValue",
    "IfcQuantityCount": "CountValue",
    "IfcQuantityWeight": "WeightValue",
    "IfcQuantityTime": "TimeValue",
}

# Revit 导出中的协作元数据属性：编辑信息而非设计参数，不参与比对
_METADATA_PROP_NAMES = {
    "Edited by",
    "Edited date",
    "Created by",
    "Created date",
    "Last modified by",
    "Last modified date",
}

# 几何派生属性集（尺寸/测量值随几何联动，处理方式同 Qto 量值）
_GEOM_LINKED_PSETS = {"Dimensions"}


def _definition_props(definition):
    """读取一个 IfcPropertySet / IfcElementQuantity 的 {属性名: 值}。"""
    props = {}
    prop_list = getattr(definition, "HasProperties", None)
    if prop_list is None:
        # IFC4 的 IfcElementQuantity 使用 Quantities 属性名（不是 HasProperties）
        prop_list = getattr(definition, "Quantities", None)
    for prop in prop_list or ():
        if prop.is_a("IfcPropertySingleValue"):
            if prop.NominalValue is not None:
                props[prop.Name] = _unwrap(prop.NominalValue)
        elif prop.is_a("IfcPropertyEnumeratedValue"):
            values = [
                _unwrap(v)
                for v in getattr(prop, "EnumerationValues", ()) or ()
                if v is not None
            ]
            if values:
                props[prop.Name] = values
        elif prop.is_a("IfcPropertyListValue"):
            values = [
                _unwrap(v)
                for v in getattr(prop, "ListValues", ()) or ()
                if v is not None
            ]
            if values:
                props[prop.Name] = values
        elif prop.is_a("IfcPhysicalSimpleQuantity"):
            attr = _QUANTITY_VALUES.get(prop.is_a())
            if attr:
                value = getattr(prop, attr, None)
                if value is not None:
                    props[prop.Name] = _unwrap(value)
    return props


def element_properties(entity):
    """{属性集名: {属性名: 值}}：实例属性集 + 类型实体（IfcWallType 等）上的属性集 + 量值。

    同名属性集合并时实例优先（类型属性集先收集）。
    """
    result = {}
    definitions = []
    # 类型实体上的属性集（Revit 等软件的类型参数常放这里）
    for rel in getattr(entity, "IsTypedBy", None) or ():
        rel_type = getattr(rel, "RelatingType", None)
        if rel_type is not None and rel_type.is_a("IfcTypeObject"):
            for definition in getattr(rel_type, "HasPropertySets", None) or ():
                definitions.append(definition)
    # 实例自身关联的属性集
    for rel in getattr(entity, "IsDefinedBy", None) or ():
        if rel.is_a("IfcRelDefinesByProperties"):
            definitions.append(rel.RelatingPropertyDefinition)
    for definition in definitions:
        if definition is None or not (
            definition.is_a("IfcPropertySet") or definition.is_a("IfcElementQuantity")
        ):
            continue
        props = _definition_props(definition)
        if props:
            result.setdefault(definition.Name, {}).update(props)
    return result


def element_materials(entity):
    """返回构件关联的材质描述列表（材质名 / 材质层名），用于材质变化检测。"""
    names = []
    for rel in getattr(entity, "HasAssociations", None) or ():
        if not rel.is_a("IfcRelAssociatesMaterial"):
            continue
        material = rel.RelatingMaterial
        if material is None:
            continue
        if material.is_a("IfcMaterial"):
            names.append(material.Name or "(未命名)")
        elif material.is_a("IfcMaterialLayerSetUsage") or material.is_a("IfcMaterialLayerSet"):
            layerset = material.ForLayerSet if material.is_a("IfcMaterialLayerSetUsage") else material
            for layer in getattr(layerset, "MaterialLayers", None) or ():
                names.append(layer.Material.Name if layer.Material else "(未命名)")
        elif material.is_a("IfcMaterialList"):
            for item in getattr(material, "Materials", None) or ():
                names.append(item.Name or "(未命名)")
        elif material.is_a("IfcMaterialProfileSet") or material.is_a("IfcMaterialProfileSetUsage"):
            profile_set = material.ForProfileSet if material.is_a("IfcMaterialProfileSetUsage") else material
            for profile in getattr(profile_set, "MaterialProfiles", None) or ():
                names.append(profile.Material.Name if profile.Material else "(未命名)")
    return sorted(names)


# ---------------------------------------------------------------------------
# 几何签名：对 Representation / ObjectPlacement 的表征树做规范化序列化后取哈希，
# 用于在不计算网格的情况下粗略检测“几何 / 位置是否发生变化”。
# ---------------------------------------------------------------------------

_SIG_DEPTH = 24


def _canonical(entity, depth, seen):
    if entity is None:
        return "null"
    if not isinstance(entity, ifcopenshell.entity_instance):
        return _canonical_value(entity, depth, seen)
    if depth <= 0:
        return "REF"
    if id(entity) in seen:
        return "CYCLE"
    seen = seen | {id(entity)}
    info = entity.get_info(recursive=False, include_identifier=False)
    parts = [entity.is_a()]
    for name, value in info.items():
        if name in ("id", "type"):
            continue
        # 放置链只取自身 RelativePlacement，避免楼层整体移动导致所有构件误报
        if entity.is_a("IfcLocalPlacement") and name == "PlacementRelTo":
            parts.append("PlacementRelTo=REF")
            continue
        parts.append(f"{name}={_canonical_value(value, depth, seen)}")
    return "(" + ",".join(parts) + ")"


def _canonical_value(value, depth, seen):
    if isinstance(value, ifcopenshell.entity_instance):
        return _canonical(value, depth - 1, seen)
    if isinstance(value, tuple):
        return "[" + ",".join(_canonical_value(v, depth, seen) for v in value) + "]"
    if isinstance(value, float):
        return repr(round(value, 3))
    return repr(value)


def geometry_signature(entity):
    """构件的几何签名（16 位十六进制）。"""
    digest = hashlib.sha256()
    digest.update(
        _canonical(getattr(entity, "Representation", None), _SIG_DEPTH, frozenset()).encode("utf-8")
    )
    digest.update(b"|")
    digest.update(
        _canonical(getattr(entity, "ObjectPlacement", None), _SIG_DEPTH, frozenset()).encode("utf-8")
    )
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------


def _element_summary(entity):
    return {
        "guid": entity.GlobalId,
        "name": entity.Name or "(unnamed)",
        "type": entity.is_a(),
    }


def compare_models(old, new, old_file="", new_file=""):
    """比对两个 IFC 模型，返回报告 dict（写入 diff.json 的结构）。"""
    old_elements = collect_elements(old)
    new_elements = collect_elements(new)

    added_guids = sorted(set(new_elements) - set(old_elements))
    deleted_guids = sorted(set(old_elements) - set(new_elements))

    changed = []
    unchanged = 0
    for guid in sorted(set(old_elements) & set(new_elements)):
        old_el, new_el = old_elements[guid], new_elements[guid]
        changes = []
        has_param_change = False
        if old_el.Name != new_el.Name:
            changes.append({"pset": "(Basic info)", "property": "Name", "old": old_el.Name, "new": new_el.Name})
            has_param_change = True
        if getattr(old_el, "ObjectType", None) != getattr(new_el, "ObjectType", None):
            changes.append(
                {
                    "pset": "(Basic info)",
                    "property": "ObjectType",
                    "old": getattr(old_el, "ObjectType", None),
                    "new": getattr(new_el, "ObjectType", None),
                }
            )
            has_param_change = True
        props_old = element_properties(old_el)
        props_new = element_properties(new_el)
        for pset_name in sorted(set(props_old) | set(props_new)):
            a = props_old.get(pset_name) or {}
            b = props_new.get(pset_name) or {}
            for prop_name in sorted(set(a) | set(b)):
                if a.get(prop_name) != b.get(prop_name):
                    if prop_name in _METADATA_PROP_NAMES:
                        # 协作元数据（编辑者/日期等）非设计参数，跳过
                        continue
                    change = {
                        "pset": pset_name,
                        "property": prop_name,
                        "old": a.get(prop_name),
                        "new": b.get(prop_name),
                    }
                    if pset_name.startswith("Qto_") or pset_name in _GEOM_LINKED_PSETS:
                        # 量值/尺寸是几何的派生测量值：几何变时它自然联动，不视为参数修改
                        change["quantity"] = True
                    else:
                        has_param_change = True
                    changes.append(change)
        geometry_changed = geometry_signature(old_el) != geometry_signature(new_el)
        # 材质变化视为参数修改
        mats_old = element_materials(old_el)
        mats_new = element_materials(new_el)
        if mats_old != mats_new:
            changes.append({"pset": "(Materials)", "property": "Material", "old": mats_old, "new": mats_new})
            has_param_change = True
        quantity_changed = any(c.get("quantity") for c in changes)
        # 分类规则：几何已变时，量值联动不提升为参数修改；
        # 几何未变时，量值被单独修改也视为参数修改。
        param_changed = has_param_change or (quantity_changed and not geometry_changed)
        if geometry_changed and param_changed:
            kind = "both"
        elif geometry_changed:
            kind = "geom"
        elif param_changed:
            kind = "param"
        else:
            unchanged += 1
            continue
        changed.append(
            {**_element_summary(new_el), "kind": kind, "geometryChanged": geometry_changed, "changes": changes}
        )

    changed.sort(key=lambda c: (c["kind"], c["type"], c["name"], c["guid"]))

    added = [
        {**_element_summary(new_elements[guid]), "properties": element_properties(new_elements[guid])}
        for guid in added_guids
    ]
    deleted = [
        {**_element_summary(old_elements[guid]), "properties": element_properties(old_elements[guid])}
        for guid in deleted_guids
    ]

    tz = timezone(timedelta(hours=8))
    report = {
        "meta": {
            "version": VERSION,
            "oldFile": old_file,
            "newFile": new_file,
            "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
            "counts": {
                "added": len(added),
                "deleted": len(deleted),
                "geom": sum(1 for c in changed if c["kind"] == "geom"),
                "param": sum(1 for c in changed if c["kind"] == "param"),
                "both": sum(1 for c in changed if c["kind"] == "both"),
                "unchanged": unchanged,
            },
        },
        "elements": {"added": added, "deleted": deleted, "changed": changed},
    }
    return report


def status_map(model, report, side):
    """guid -> status，用于几何导出着色。

    side == "old"：deleted / 各类修改高亮；side == "new"：added / 各类修改高亮；
    其余构件为 unchanged。修改按 kind 映射到 geom / param / both 三种状态。
    """
    mapping = {guid: "unchanged" for guid in collect_elements(model)}
    key = "deleted" if side == "old" else "added"
    for item in report["elements"][key]:
        mapping[item["guid"]] = "deleted" if side == "old" else "added"
    for item in report["elements"]["changed"]:
        mapping[item["guid"]] = item["kind"]
    return mapping
