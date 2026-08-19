"""几何导出：把 IFC 模型导出为 glTF 2.0（外部 .bin）。

每个构件一个独立 mesh + node，node.extras 携带 guid / 状态 / 名称 / 类型，
材质按差异状态共享（颜色烘焙）。浏览器端可逐构件拾取与高亮：
- unchanged: 半透明灰
- added:     绿
- deleted:   红
- geom:      黄
- param:     蓝
- both:      紫
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os

import ifcopenshell
import ifcopenshell.geom

from . import VERSION

# (r, g, b, alpha)，与 viewer 中的状态色保持一致
STATUS_COLORS = {
    "unchanged": (0.580, 0.600, 0.630, 0.35),
    "added":     (0.086, 0.639, 0.290, 1.0),   # 新增：绿
    "deleted":   (1.000, 0.000, 0.000, 1.0),   # 删除：纯红 255,0,0
    "geom":      (0.918, 0.702, 0.031, 1.0),   # 仅几何：黄
    "param":     (0.145, 0.388, 0.922, 1.0),   # 仅参数：蓝
    "both":      (0.486, 0.227, 0.929, 1.0),   # 几何+参数：紫
}
STATUS_ORDER = ("unchanged", "added", "deleted", "geom", "param", "both")


def _geom_settings():
    try:
        settings = ifcopenshell.geom.settings()
    except AttributeError:
        settings = ifcopenshell.geom.main.settings()
    settings.set("weld-vertices", True)
    settings.set("apply-default-materials", False)
    return settings


def collect_geometry(model_path, status_of_guid, jobs=1):
    """遍历模型收集分状态几何。

    返回 (buckets, bbox_min, bbox_max, 导出构件数)。buckets[status] 是元素列表，
    每个元素为独立几何桶 dict：{guid, name, type, pos, idx, min, max}。
    顶点先用 float64 保存，写入 glTF 时才平移到原点附近并转 float32，
    避免大地坐标（如 81 万米）在 float32 中的量化误差导致渲染时面片微缝闪烁。
    """
    model = ifcopenshell.open(model_path)
    iterator = ifcopenshell.geom.iterator(_geom_settings(), model, jobs)

    buckets = {status: [] for status in STATUS_ORDER}

    def new_element(guid):
        """为单个构件创建独立几何桶：每构件独立 mesh/node，前端才能拾取与高亮。"""
        name = guid
        etype = ""
        try:
            ent = model.by_guid(guid)
            if ent is not None:
                name = getattr(ent, "Name", None) or guid
                etype = ent.is_a()
        except Exception:
            pass
        return {
            "guid": guid,
            "name": name,
            "type": etype,
            "pos": array.array("d"),
            "idx": array.array("I"),
            "min": [math.inf] * 3,
            "max": [-math.inf] * 3,
        }

    exported = 0
    if iterator.initialize():
        while True:
            try:
                shape = iterator.get()
            except RuntimeError:
                # 个别实体可能无法生成形状，跳过
                if not iterator.next():
                    break
                continue
            if shape is not None:
                status = status_of_guid.get(shape.guid)
                geometry = shape.geometry
                if status in buckets and geometry is not None:
                    verts = geometry.verts
                    faces = geometry.faces
                    if len(verts) and len(faces):
                        el = new_element(shape.guid)
                        mat = _matrix_of(shape)  # 行主序 4x4；无放置时为 None
                        # ifcopenshell >= 0.8.1 返回扁平数组 [x,y,z,...]；旧版返回 (x,y,z) 元组列表
                        flat = isinstance(verts[0], (int, float))
                        if flat:
                            for i in range(0, len(verts) - 2, 3):
                                x, y, z = verts[i], verts[i + 1], verts[i + 2]
                                if mat is not None:
                                    x, y, z = _apply(mat, x, y, z)
                                el["pos"].extend((x, y, z))
                                if x < el["min"][0]:
                                    el["min"][0] = x
                                if y < el["min"][1]:
                                    el["min"][1] = y
                                if z < el["min"][2]:
                                    el["min"][2] = z
                                if x > el["max"][0]:
                                    el["max"][0] = x
                                if y > el["max"][1]:
                                    el["max"][1] = y
                                if z > el["max"][2]:
                                    el["max"][2] = z
                            # 默认三角化输出，faces 为三角形索引扁平序列
                            for idx in faces:
                                el["idx"].append(idx)
                        else:
                            for x, y, z in verts:
                                if mat is not None:
                                    x, y, z = _apply(mat, x, y, z)
                                el["pos"].extend((x, y, z))
                                if x < el["min"][0]:
                                    el["min"][0] = x
                                if y < el["min"][1]:
                                    el["min"][1] = y
                                if z < el["min"][2]:
                                    el["min"][2] = z
                                if x > el["max"][0]:
                                    el["max"][0] = x
                                if y > el["max"][1]:
                                    el["max"][1] = y
                                if z > el["max"][2]:
                                    el["max"][2] = z
                            for face in faces:
                                # 防御性三角化（扇形）
                                for k in range(1, len(face) - 1):
                                    el["idx"].extend((face[0], face[k], face[k + 1]))
                        buckets[status].append(el)
                        exported += 1
            if not iterator.next():
                break

    bbox_min = [math.inf] * 3
    bbox_max = [-math.inf] * 3
    for elements in buckets.values():
        for el in elements:
            for axis in range(3):
                if el["min"][axis] < bbox_min[axis]:
                    bbox_min[axis] = el["min"][axis]
                if el["max"][axis] > bbox_max[axis]:
                    bbox_max[axis] = el["max"][axis]
    if bbox_min[0] == math.inf:
        bbox_min, bbox_max = [0.0] * 3, [0.0] * 3
    return buckets, tuple(bbox_min), tuple(bbox_max), exported


def write_gltf(gltf_path, buckets, origin=(0.0, 0.0, 0.0)):
    """把收集好的几何写入 gltf_path（顶点坐标减去 origin 后转 float32）。"""
    _write_gltf(gltf_path, buckets, origin)


def export_gltf(model_path, gltf_path, status_of_guid, jobs=1):
    """兼容入口：把 model_path 的几何导出为 gltf_path（不平移原点）。返回导出构件数。"""
    buckets, _bmin, _bmax, exported = collect_geometry(model_path, status_of_guid, jobs)
    write_gltf(gltf_path, buckets)
    return exported


def _matrix_of(shape):
    """取 shape 的放置矩阵（4 行×4 列行主序元组，每行末位为平移分量）；无放置或单位阵时返回 None。"""
    try:
        m = shape.transformation.data()
    except Exception:
        return None
    try:
        if m.is_identity():
            return None
    except Exception:
        pass
    comps = m.components() if callable(m.components) else m.components
    if not comps:
        return None
    rows = tuple(tuple(r) for r in comps)
    if len(rows) != 4 or any(len(r) != 4 for r in rows):
        return None
    return rows


def _apply(mat, x, y, z):
    return (
        mat[0][0] * x + mat[0][1] * y + mat[0][2] * z + mat[0][3],
        mat[1][0] * x + mat[1][1] * y + mat[1][2] * z + mat[1][3],
        mat[2][0] * x + mat[2][1] * y + mat[2][2] * z + mat[2][3],
    )


def _write_gltf(gltf_path, buckets, origin=(0.0, 0.0, 0.0)):
    """每个元素独立 mesh + node（extras 带 guid/status），材质按状态共享。"""
    materials = []
    status_material = {}
    for status in STATUS_ORDER:
        r, g, b, a = STATUS_COLORS[status]
        status_material[status] = len(materials)
        materials.append(
            {
                "name": "m_" + status,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r, g, b, a],
                    "metallicFactor": 0.05,
                    "roughnessFactor": 0.9,
                },
                "doubleSided": True,
                "alphaMode": "BLEND" if a < 1.0 else "OPAQUE",
            }
        )

    buffer_data = bytearray()
    buffer_views = []
    accessors = []
    meshes = []
    mesh_nodes = []

    def pad4(data):
        while len(data) % 4:
            data.append(0)

    for status in STATUS_ORDER:
        for el in buckets[status]:
            if not len(el["idx"]):
                continue

            ox, oy, oz = origin
            pos_f = array.array("f")
            pos_d = el["pos"]
            for i in range(0, len(pos_d) - 2, 3):
                pos_f.append(pos_d[i] - ox)
                pos_f.append(pos_d[i + 1] - oy)
                pos_f.append(pos_d[i + 2] - oz)
            pos_bytes = pos_f.tobytes()
            idx_bytes = el["idx"].tobytes()

            # POSITION
            pad4(buffer_data)
            pos_offset = len(buffer_data)
            buffer_data += pos_bytes
            buffer_views.append(
                {"buffer": 0, "byteOffset": pos_offset, "byteLength": len(pos_bytes), "target": 34962}
            )
            accessors.append(
                {
                    "bufferView": len(buffer_views) - 1,
                    "byteOffset": 0,
                    "componentType": 5126,
                    "count": len(pos_bytes) // 12,
                    "type": "VEC3",
                    "min": [round(el["min"][i] - origin[i], 4) for i in range(3)],
                    "max": [round(el["max"][i] - origin[i], 4) for i in range(3)],
                }
            )
            pos_acc = len(accessors) - 1

            # indices（UInt32）
            pad4(buffer_data)
            idx_offset = len(buffer_data)
            buffer_data += idx_bytes
            buffer_views.append(
                {"buffer": 0, "byteOffset": idx_offset, "byteLength": len(idx_bytes), "target": 34963}
            )
            accessors.append(
                {
                    "bufferView": len(buffer_views) - 1,
                    "byteOffset": 0,
                    "componentType": 5125,
                    "count": len(idx_bytes) // 4,
                    "type": "SCALAR",
                }
            )
            idx_acc = len(accessors) - 1

            meshes.append(
                {
                    "name": el["guid"],
                    "primitives": [
                        {
                            "attributes": {"POSITION": pos_acc},
                            "indices": idx_acc,
                            "material": status_material[status],
                        }
                    ],
                }
            )
            mesh_nodes.append(
                {
                    "name": el["guid"],
                    "mesh": len(meshes) - 1,
                    "extras": {
                        "guid": el["guid"],
                        "status": status,
                        "name": el["name"],
                        "type": el["type"],
                    },
                }
            )

    nodes = [{"name": "root", "children": [i + 1 for i in range(len(mesh_nodes))]}] + mesh_nodes

    # .bin 文件名带内容哈希：内容变了文件名就变，浏览器旧缓存天然失效
    bin_hash = hashlib.sha1(bytes(buffer_data)).hexdigest()[:8]
    stem = os.path.splitext(os.path.basename(gltf_path))[0]
    bin_name = f"{stem}.{bin_hash}.bin"
    bin_path = os.path.join(os.path.dirname(gltf_path), bin_name)

    gltf = {
        "asset": {"version": "2.0", "generator": f"ifc-compare {VERSION}"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"uri": bin_name, "byteLength": len(buffer_data)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }

    with open(bin_path, "wb") as f:
        f.write(bytes(buffer_data))
    with open(gltf_path, "w", encoding="utf-8") as f:
        json.dump(gltf, f, ensure_ascii=False, separators=(",", ":"))
