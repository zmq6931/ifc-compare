"""生成覆盖六种差异状态的示例 IFC4 文件（sample_v1.ifc / sample_v2.ifc）。

差异设计（修改类拆分为三种）：
- Wall-01：仅参数修改（FireRating F60→F90 + 新增 LoadBearing），几何不变
- Wall-02：仅几何修改（长度 3000→3500），属性不变
- Wall-03：完全不变
- Wall-04：几何+参数都修改（长度 3000→3500 且 FireRating F60→F90）
- Door-01：仅存在于 v1（删除）
- Window-01：仅存在于 v2（新增）
"""
from __future__ import annotations

import base64
import hashlib
import os
import random
import string

_GUID_CHARS = string.digits + string.ascii_uppercase + string.ascii_lowercase + "_$"


def make_guid() -> str:
    return "".join(random.SystemRandom().choice(_GUID_CHARS) for _ in range(22))


def _num(value: float) -> str:
    """格式化为带小数点的 STEP REAL（几何内核对整数形 token 很挑剔）。"""
    s = repr(float(value))
    if "e" in s:
        mant, _, exp = s.partition("e")
        if "." not in mant:
            mant += ".0"
        s = f"{mant}E{int(exp)}"
    return s


def stable_guid(name: str) -> str:
    """由构件名称派生的确定性 GUID，保证同一构件在两个版本中 GlobalId 一致。"""
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    guid = base64.b64encode(digest).decode("ascii")[:22].replace("+", "_").replace("/", "$")
    return guid + "0" * (22 - len(guid))


class _Writer:
    def __init__(self):
        self._lines: list[str] = []
        self._next = 0

    def add(self, body: str) -> int:
        self._next += 1
        self._lines.append(f"#{self._next}={body}")
        return self._next

    def lines(self) -> list[str]:
        return self._lines


def _header(filename: str) -> list[str]:
    return [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');",
        f"FILE_NAME('{filename}','2026-08-17T11:00:00',('ifc-compare'),('ifc-compare'),"
        "'ifc-compare 0.1','ifc-compare 0.1','');",
        "FILE_SCHEMA(('IFC4'));",
        "ENDSEC;",
        "DATA;",
    ]


def _build(filename: str, walls: list, doors: list, windows: list) -> str:
    """walls: [(name, cx, cy, length, thickness, height, [(pset, [(prop, value), ...]), ...])]
    doors/windows: [(name, cx, cy, cz, xdim, ydim, depth, psets)]"""
    w = _Writer()
    add = w.add

    person = add("IFCPERSON($,$,'Demo',$,$,$,$,$)")
    org = add("IFCORGANIZATION($,'ifc-compare',$,$,$)")
    pao = add(f"IFCPERSONANDORGANIZATION(#{person},#{org},$)")
    app = add(f"IFCAPPLICATION(#{org},'1.0','ifc-compare','ifc-compare')")
    oh = add(f"IFCOWNERHISTORY(#{pao},#{app},$,.ADDED.,$,$,$,0)")

    origin = add("IFCCARTESIANPOINT((0.,0.,0.))")
    axis3 = add(f"IFCAXIS2PLACEMENT3D(#{origin},$,$)")
    zdir = add("IFCDIRECTION((0.,0.,1.))")
    ctx = add(f"IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.0E-5,#{axis3},$)")

    unit_l = add("IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.)")
    unit_a = add("IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.)")
    unit_v = add("IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.)")
    unit_p = add("IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.)")
    units = add(f"IFCUNITASSIGNMENT((#{unit_l},#{unit_a},#{unit_v},#{unit_p}))")
    project = add(f"IFCPROJECT('{make_guid()}',#{oh},'ifc-compare-demo',$,$,$,$,(#{ctx}),#{units})")

    site_pl = add(f"IFCLOCALPLACEMENT($,#{axis3})")
    site = add(f"IFCSITE('{make_guid()}',#{oh},'Site',$,$,#{site_pl},$,$,.ELEMENT.,$,$,$,$,$)")
    building_pl = add(f"IFCLOCALPLACEMENT(#{site_pl},#{axis3})")
    building = add(f"IFCBUILDING('{make_guid()}',#{oh},'Building',$,$,#{building_pl},$,$,.ELEMENT.,$,$,$)")
    storey_pl = add(f"IFCLOCALPLACEMENT(#{building_pl},#{axis3})")
    storey = add(f"IFCBUILDINGSTOREY('{make_guid()}',#{oh},'Storey-1',$,$,#{storey_pl},$,$,.ELEMENT.,0.)")
    add(f"IFCRELAGGREGATES('{make_guid()}',#{oh},$,$,#{project},(#{site}))")
    add(f"IFCRELAGGREGATES('{make_guid()}',#{oh},$,$,#{site},(#{building}))")
    add(f"IFCRELAGGREGATES('{make_guid()}',#{oh},$,$,#{building},(#{storey}))")

    contained: list[int] = []

    def placement_at(x: float, y: float, z: float = 0.0, rel_to: int | None = None) -> int:
        pt = add(f"IFCCARTESIANPOINT(({_num(x)},{_num(y)},{_num(z)}))")
        ax = add(f"IFCAXIS2PLACEMENT3D(#{pt},$,$)")
        rel = storey_pl if rel_to is None else rel_to
        return add(f"IFCLOCALPLACEMENT(#{rel},#{ax})")

    def body_rep(xdim: float, ydim: float, depth: float) -> int:
        pt2d = add("IFCCARTESIANPOINT((0.,0.))")
        ax2d = add(f"IFCAXIS2PLACEMENT2D(#{pt2d},$)")
        profile = add(f"IFCRECTANGLEPROFILEDEF(.AREA.,$,#{ax2d},{_num(xdim)},{_num(ydim)})")
        solid = add(f"IFCEXTRUDEDAREASOLID(#{profile},#{axis3},#{zdir},{_num(depth)})")
        shape_rep = add(f"IFCSHAPEREPRESENTATION(#{ctx},'Body','SweptSolid',(#{solid}))")
        return add(f"IFCPRODUCTDEFINITIONSHAPE($,$,(#{shape_rep}))")

    def define_properties(element_ref: int, pset_specs: list) -> None:
        for pset_name, props in pset_specs:
            is_quantity = pset_name.startswith("Qto_")
            prop_refs: list[int] = []
            for pname, pvalue in props:
                if isinstance(pvalue, bool):
                    val = ".T." if pvalue else ".F."
                    prop_refs.append(add(f"IFCPROPERTYSINGLEVALUE('{pname}',$,IFCBOOLEAN({val}),$)"))
                elif is_quantity and isinstance(pvalue, (int, float)):
                    prop_refs.append(add(f"IFCQUANTITYLENGTH('{pname}',$,$,{_num(pvalue)})"))
                elif isinstance(pvalue, (int, float)):
                    prop_refs.append(add(f"IFCPROPERTYSINGLEVALUE('{pname}',$,IFCLENGTHMEASURE({_num(pvalue)}),$)"))
                else:
                    prop_refs.append(add(f"IFCPROPERTYSINGLEVALUE('{pname}',$,IFCLABEL('{pvalue}'),$)"))
            refs = ",".join(f"#{p}" for p in prop_refs)
            if is_quantity:
                pset = add(f"IFCELEMENTQUANTITY('{make_guid()}',#{oh},'{pset_name}',$,$,({refs}))")
            else:
                pset = add(f"IFCPROPERTYSET('{make_guid()}',#{oh},'{pset_name}',$,({refs}))")
            add(f"IFCRELDEFINESBYPROPERTIES('{make_guid()}',#{oh},$,$,(#{element_ref}),#{pset})")

    for name, cx, cy, length, thickness, height, pset_specs in walls:
        pl = placement_at(cx, cy)
        rep = body_rep(thickness, length, height)
        el = add(f"IFCWALLSTANDARDCASE('{stable_guid(name)}',#{oh},'{name}',$,$,#{pl},#{rep},$,$)")
        define_properties(el, pset_specs)
        contained.append(el)

    for name, cx, cy, cz, xdim, ydim, depth, pset_specs in doors:
        pl = placement_at(cx, cy, cz)
        rep = body_rep(xdim, ydim, depth)
        el = add(f"IFCDOOR('{stable_guid(name)}',#{oh},'{name}',$,$,#{pl},#{rep},$,{_num(depth)},{_num(ydim)},.DOOR.,$,$)")
        define_properties(el, pset_specs)
        contained.append(el)

    for name, cx, cy, cz, xdim, ydim, depth, pset_specs in windows:
        pl = placement_at(cx, cy, cz)
        rep = body_rep(xdim, ydim, depth)
        el = add(f"IFCWINDOW('{stable_guid(name)}',#{oh},'{name}',$,$,#{pl},#{rep},$,{_num(depth)},{_num(ydim)},.WINDOW.,$,$)")
        define_properties(el, pset_specs)
        contained.append(el)

    add(f"IFCRELCONTAINEDINSPATIALSTRUCTURE('{make_guid()}',#{oh},$,$,"
        f"({','.join(f'#{e}' for e in contained)}),#{storey})")

    lines = _header(filename) + w.lines() + ["ENDSEC;", "END-ISO-10303-21;"]
    return "\n".join(lines) + "\n"


def _sample_specs() -> tuple:
    wall_common_v1 = [("Pset_WallCommon", [("IsExternal", False), ("FireRating", "F60")])]

    v1_walls = [
        # param：属性变化（v2 里 FireRating F60→F90 + 新增 LoadBearing）
        ("Wall-01", 0.0, 1500.0, 3000.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", False), ("FireRating", "F60")]),
          ("Qto_WallBaseQuantities", [("Length", 3000.0), ("Height", 3000.0)])]),
        # geom：仅几何变化（v2 里长度 3000→3500，属性不变）
        # Qto 量值 Length 随几何联动变化，不计为参数修改（见 diff.py 分类规则）
        ("Wall-02", 0.0, 5000.0, 3000.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", True), ("FireRating", "F60")]),
          ("Qto_WallBaseQuantities", [("Length", 3000.0)])]),
        # unchanged：完全不变
        ("Wall-03", 0.0, 9000.0, 3000.0, 300.0, 3000.0,
         list(wall_common_v1)),
        # both：几何+属性都变（v2 里长度 3000→3500 且 FireRating F60→F90）
        ("Wall-04", 0.0, 12500.0, 3000.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", False), ("FireRating", "F60")])]),
    ]
    v1_doors = [
        ("Door-01", 0.0, 1500.0, 0.0, 100.0, 900.0, 2000.0,
         [("Pset_DoorCommon", [("IsExternal", False), ("FireRating", "F30")])]),
    ]
    v1_windows = []

    v2_walls = [
        # 属性变化 + 新增属性，几何不变 → param
        ("Wall-01", 0.0, 1500.0, 3000.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", False), ("FireRating", "F90"), ("LoadBearing", True)]),
          ("Qto_WallBaseQuantities", [("Length", 3000.0), ("Height", 3000.0)])]),
        # 仅几何变化（长度 3000→3500），属性不变 → geom；Qto 量值联动不计为参数修改
        ("Wall-02", 0.0, 5000.0, 3500.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", True), ("FireRating", "F60")]),
          ("Qto_WallBaseQuantities", [("Length", 3500.0)])]),
        # 完全不变 → unchanged
        ("Wall-03", 0.0, 9000.0, 3000.0, 300.0, 3000.0,
         list(wall_common_v1)),
        # 几何 + 属性都变 → both
        ("Wall-04", 0.0, 12500.0, 3500.0, 300.0, 3000.0,
         [("Pset_WallCommon", [("IsExternal", False), ("FireRating", "F90")])]),
    ]
    v2_doors = []
    v2_windows = [
        ("Window-01", 0.0, 5000.0, 1200.0, 100.0, 1500.0, 1500.0,
         [("Pset_WindowCommon", [("IsExternal", True), ("FireRating", "F30")])]),
    ]
    return (v1_walls, v1_doors, v1_windows), (v2_walls, v2_doors, v2_windows)


def write_samples(out_dir: str) -> tuple[str, str]:
    """生成 sample_v1.ifc / sample_v2.ifc，返回文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    v1, v2 = _sample_specs()
    path_v1 = os.path.join(out_dir, "sample_v1.ifc")
    path_v2 = os.path.join(out_dir, "sample_v2.ifc")
    with open(path_v1, "w", encoding="utf-8") as f:
        f.write(_build("sample_v1.ifc", *v1))
    with open(path_v2, "w", encoding="utf-8") as f:
        f.write(_build("sample_v2.ifc", *v2))
    return path_v1, path_v2
