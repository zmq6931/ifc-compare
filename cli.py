"""ifc-compare command line entry.

Usage:
    python cli.py samples                        # generate sample IFC files
    python cli.py compare old.ifc new.ifc [-o out] [--jobs N]
    python cli.py serve [out] [--port 8080]
    python cli.py inspect model.ifc
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time

import ifcopenshell

from ifc_compare import VERSION, diff, export, presets, samples

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_DIR = os.path.join(BASE_DIR, "viewer")


def cmd_samples(args) -> int:
    path_v1, path_v2 = samples.write_samples(args.out)
    print("Sample files generated:")
    print(f"  {path_v1}")
    print(f"  {path_v2}")
    print(f"Next: python cli.py compare {path_v1} {path_v2}")
    return 0


def _load_classification(args):
    """构建分类规则配置：--config 优先（custom），否则 --classification 预设。"""
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        config.setdefault("mode", "custom")
        return config
    if args.classification:
        return {"mode": args.classification}
    return None


def run_compare(old_path, new_path, out_dir, jobs, old_name=None, new_name=None, classification=None):
    """执行完整比对流水线（解析→比对→导出→拷贝查看器），返回统计信息 dict。"""
    t0 = time.time()
    old = ifcopenshell.open(old_path)
    new = ifcopenshell.open(new_path)
    report = diff.compare_models(
        old, new, old_file=old_name or old_path, new_file=new_name or new_path,
        classification=classification,
    )

    models_dir = os.path.join(out_dir, "models")
    os.makedirs(models_dir, exist_ok=True)
    # 清理旧的 gltf/bin（.bin 文件名带内容哈希，避免旧版本残留混淆）
    for stale in glob.glob(os.path.join(models_dir, "*.bin")) + glob.glob(os.path.join(models_dir, "*.gltf")):
        os.remove(stale)
    with open(os.path.join(out_dir, "diff.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    buckets_old, bmin_old, bmax_old, n_old = export.collect_geometry(
        old_path, diff.status_map(old, report, "old"), jobs=jobs
    )
    buckets_new, bmin_new, bmax_new, n_new = export.collect_geometry(
        new_path, diff.status_map(new, report, "new"), jobs=jobs
    )
    # 联合包围盒中心作为共同原点：大地坐标（如 81 万米）转为局部坐标，
    # 避免 GPU float32 量化误差导致的旋转/移动时面片微缝闪烁
    joint_min = [min(bmin_old[i], bmin_new[i]) for i in range(3)]
    joint_max = [max(bmax_old[i], bmax_new[i]) for i in range(3)]
    origin = [(joint_min[i] + joint_max[i]) / 2.0 for i in range(3)]
    export.write_gltf(os.path.join(models_dir, "old.gltf"), buckets_old, origin)
    export.write_gltf(os.path.join(models_dir, "new.gltf"), buckets_new, origin)
    shutil.copytree(VIEWER_DIR, out_dir, dirs_exist_ok=True)

    counts = report["meta"]["counts"]
    return {
        "counts": counts,
        "elapsed": time.time() - t0,
        "exportedOld": n_old,
        "exportedNew": n_new,
    }


def cmd_compare(args) -> int:
    if not os.path.isfile(args.old):
        print(f"Error: old model file not found: {args.old}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.new):
        print(f"Error: new model file not found: {args.new}", file=sys.stderr)
        return 2

    print(f"Comparing: {args.old}  vs  {args.new}")
    classification = _load_classification(args)
    if classification:
        print(f"  classification: {classification.get('mode')}")
    result = run_compare(args.old, args.new, args.out, args.jobs, classification=classification)
    counts = result["counts"]
    print()
    print("Done:")
    print(f"  added {counts['added']} | deleted {counts['deleted']}")
    print(f"  geometry {counts['geom']} | parameters {counts['param']} | both {counts['both']} | unchanged {counts['unchanged']}")
    print(f"  exported elements: old {result['exportedOld']} / new {result['exportedNew']} | {result['elapsed']:.1f}s")
    print(f"Report: {os.path.join(args.out, 'report.html')}")
    print(f"View: python cli.py serve {args.out}")
    print("      (then open http://localhost:8080/report.html, or use the Load IFC button on the page)")
    return 0


def cmd_serve(args) -> int:
    import http.server
    import socketserver
    import subprocess
    import urllib.parse

    directory = os.path.abspath(args.dir)
    uploads_dir = os.path.join(directory, "_uploads")
    if not os.path.isfile(os.path.join(directory, "report.html")):
        print(f"Note: no report.html found in {directory}. Run compare first.", file=sys.stderr)

    jobs = os.cpu_count() or 1

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def end_headers(self):
            # 本地工具：禁止浏览器缓存，保证刷新后永远是刚生成的最新报告
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == "/api/presets":
                return self._json({"ok": True, "presets": presets.SOFTWARE_PRESETS})
            return super().do_GET()

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            try:
                if parsed.path == "/api/upload":
                    query = urllib.parse.parse_qs(parsed.query)
                    slot = query.get("slot", [""])[0]
                    if slot not in ("old", "new"):
                        return self._json({"error": "slot must be 'old' or 'new'"}, 400)
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0:
                        return self._json({"error": "empty request body"}, 400)
                    data = self.rfile.read(length)
                    os.makedirs(uploads_dir, exist_ok=True)
                    with open(os.path.join(uploads_dir, f"{slot}.ifc"), "wb") as f:
                        f.write(data)
                    return self._json({"ok": True})

                if parsed.path == "/api/compare":
                    length = int(self.headers.get("Content-Length") or 0)
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except json.JSONDecodeError:
                        return self._json({"error": "request body is not valid JSON"}, 400)
                    old_path = os.path.join(uploads_dir, "old.ifc")
                    new_path = os.path.join(uploads_dir, "new.ifc")
                    if not (os.path.isfile(old_path) and os.path.isfile(new_path)):
                        return self._json({"error": "upload old and new IFC files first"}, 400)
                    print(f"[api] comparing {body.get('oldName')} vs {body.get('newName')} …")
                    # Run the compare in a fresh subprocess: the server is long-running and
                    # in-process calls would use the code loaded at server start.
                    cmd = [sys.executable, os.path.join(BASE_DIR, "cli.py"), "compare",
                           old_path, new_path, "-o", directory, "--jobs", str(jobs)]
                    cls_mode = body.get("classification")
                    derived_props = body.get("derivedProps")
                    if isinstance(derived_props, list) and derived_props:
                        # 设置页勾选的派生参数 → 写临时配置，子进程按 custom 规则比对
                        config_path = os.path.join(uploads_dir, "_classification.json")
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(
                                {
                                    "mode": "custom",
                                    "derivedSets": ["Qto_*", "Dimensions"],
                                    "derivedProps": [str(p) for p in derived_props if p],
                                },
                                f,
                                ensure_ascii=False,
                            )
                        cmd += ["--config", config_path]
                    elif cls_mode in ("default", "balanced", "custom"):
                        cmd += ["--classification", cls_mode]
                    try:
                        proc = subprocess.run(cmd, capture_output=True, text=True,
                                              encoding="utf-8", errors="replace")
                    except Exception as exc:
                        return self._json({"error": f"failed to start compare process: {exc}"}, 500)
                    if proc.returncode != 0:
                        tail = (proc.stdout + "\n" + proc.stderr)[-800:]
                        return self._json({"error": f"compare failed: {tail}"}, 500)
                    # Overwrite report metadata with the display names from the page
                    report_path = os.path.join(directory, "diff.json")
                    with open(report_path, encoding="utf-8") as f:
                        report = json.load(f)
                    report["meta"]["oldFile"] = body.get("oldName") or "old.ifc"
                    report["meta"]["newFile"] = body.get("newName") or "new.ifc"
                    with open(report_path, "w", encoding="utf-8") as f:
                        json.dump(report, f, ensure_ascii=False, indent=2)
                    counts = report["meta"]["counts"]
                    print(f"[api] done: added {counts['added']} | deleted {counts['deleted']} | geometry {counts['geom']} | "
                          f"parameters {counts['param']} | both {counts['both']} | unchanged {counts['unchanged']}")
                    return self._json({"ok": True, "url": "/report.html", "counts": counts})

                return self._json({"error": f"unknown endpoint {parsed.path}"}, 404)
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        def log_message(self, fmt, *a):
            sys.stderr.write("  %s\n" % (fmt % a))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print(f"Server started: http://127.0.0.1:{args.port}/report.html")
        print("Use the Load IFC button on the page to compare files (or run compare on the CLI and refresh).")
        print("Press Ctrl+C to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped")
    return 0


def cmd_inspect(args) -> int:
    """List the property sets in an IFC file (check whether custom parameters were exported)."""
    from collections import defaultdict

    if not os.path.isfile(args.file):
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        return 2
    model = ifcopenshell.open(args.file)
    psets = defaultdict(set)
    type_psets = defaultdict(set)

    for pset in model.by_type("IfcPropertySet"):
        for prop in pset.HasProperties or ():
            if (
                prop.is_a("IfcPropertySingleValue")
                or prop.is_a("IfcPropertyListValue")
                or prop.is_a("IfcPropertyEnumeratedValue")
            ):
                psets[pset.Name].add(prop.Name)
    for qto in model.by_type("IfcElementQuantity"):
        for quantity in getattr(qto, "Quantities", None) or ():
            psets[qto.Name].add(quantity.Name)
    for entity_type in model.by_type("IfcTypeObject"):
        for pset in getattr(entity_type, "HasPropertySets", None) or ():
            for prop in pset.HasProperties or ():
                if prop.is_a("IfcPropertySingleValue"):
                    type_psets[f"{entity_type.is_a()}:{entity_type.Name}"].add(f"{pset.Name}.{prop.Name}")

    print(f"File: {args.file}")
    print(f"Element property sets ({len(psets)}):")
    for name in sorted(psets):
        print(f"  {name}: {', '.join(sorted(psets[name]))}")
    if type_psets:
        print(f"Type property sets ({len(type_psets)}):")
        for name in sorted(type_psets):
            print(f"  {name}: {', '.join(sorted(type_psets[name]))}")
    print()
    print("Note: if your custom parameters (shared/project parameters) are not in the list above,")
    print("they were not exported to IFC. Configure the property set mapping in Revit's IFC export settings")
    print("(Revit: File > Export > IFC > Modify Setup > Property Sets).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ifc-compare",
        description="IFC model diff tool: compares two IFC versions by GlobalId "
        "(added / deleted / geometry / parameters / both / unchanged) and generates "
        "an HTML report with a synchronized dual 3D viewer.",
    )
    parser.add_argument("--version", action="version", version=f"ifc-compare {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cmp = sub.add_parser("compare", help="compare two IFC files and generate an HTML report")
    p_cmp.add_argument("old", help="path to the old IFC file")
    p_cmp.add_argument("new", help="path to the new IFC file")
    p_cmp.add_argument("-o", "--out", default="out", help="output directory (default: out)")
    p_cmp.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                       help="parallel processes for geometry export (default: CPU count; use 1 on Windows issues)")
    p_cmp.add_argument("--classification", choices=["default", "balanced", "custom"], default=None,
                       help="derived-property rule: default (only Qto/Dimensions count as derived), "
                       "balanced (measured props like Span/Slope/Length also), custom (see --config)")
    p_cmp.add_argument("--config", default=None,
                       help='JSON config for custom classification: {"derivedSets": ["Qto_*", "Pset_BeamCommon"], "derivedProps": ["Span"]}')
    p_cmp.set_defaults(func=cmd_compare)

    p_smp = sub.add_parser("samples", help="generate sample IFC files covering all six change states")
    p_smp.add_argument("-o", "--out", default="samples", help="output directory (default: samples)")
    p_smp.set_defaults(func=cmd_samples)

    p_srv = sub.add_parser("serve", help="start a local HTTP server to view the report")
    p_srv.add_argument("dir", nargs="?", default="out", help="report directory (default: out)")
    p_srv.add_argument("--port", type=int, default=8080)
    p_srv.set_defaults(func=cmd_serve)

    p_ins = sub.add_parser("inspect", help="list the property sets in an IFC file (check custom parameter export)")
    p_ins.add_argument("file", help="path to the IFC file")
    p_ins.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
