# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FreeCAD project.

"""Recompute benchmark and correctness fingerprint for FreeCAD.

Builds a synthetic, deterministic model, forces a full recompute several
times, and writes timings, peak memory and a geometry fingerprint to JSON.
Comparing the fingerprint of two builds shows whether a change altered
results (shape validity, volume, topology counts or topological element
names), not just speed.

Run it with the Python interpreter FreeCAD was built against, pointing at
the build's lib directory:

    python tools/benchmark/recompute_bench.py --freecad-lib build/release/lib \\
        --model part_trees --size 40 --repeat 5 --out part_trees.json

or through FreeCADCmd, passing arguments after --pass:

    FreeCADCmd tools/benchmark/recompute_bench.py --pass --model part_trees ...

Models:
    part_trees   N independent Part trees (filleted box, holes cut, refine).
                 Wide dependency graph; tests scheduling of independent objects.
    pd_pattern   One PartDesign body with a polar pattern of N holes.
                 Deep, narrow graph; tests work inside a single feature.
    pd_bodies    N independent PartDesign bodies, each with a polar pattern.
                 Combines graph width with pattern work.
    assembly     An Assembly with N linked instances of a few heavy parts,
                 chained with fixed joints. Tests shape work plus joint solve.
"""

import argparse
import hashlib
import json
import os
import platform
import shlex
import sys
import time
import types

MODELS = ("part_trees", "pd_pattern", "pd_bodies", "assembly")


def parse_args():
    if "--pass" in sys.argv:
        argv = sys.argv[sys.argv.index("--pass") + 1 :]
    elif os.environ.get("FC_BENCH_ARGS"):
        argv = shlex.split(os.environ["FC_BENCH_ARGS"])
    else:
        argv = sys.argv[1:]

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", choices=MODELS, required=True)
    p.add_argument("--size", type=int, default=None, help="model size (N); model-specific default")
    p.add_argument("--repeat", type=int, default=5, help="timed full recomputes after warm-up")
    p.add_argument("--out", default=None, help="write JSON results here (default: stdout)")
    p.add_argument("--freecad-lib", default=None, help="directory containing FreeCAD.so")
    p.add_argument(
        "--threads",
        type=int,
        default=None,
        help="value for the RecomputeThreads document parameter, if the build supports it",
    )
    p.add_argument("--label", default="", help="free-form label stored in the results")
    p.add_argument("--save-model", default=None, help="also save the generated .FCStd here")
    p.add_argument(
        "--mesh",
        action="store_true",
        help="also time tessellating every shape once, as the 3D view would after a recompute",
    )
    return p.parse_args(argv)


def import_freecad(lib_dir):
    if lib_dir:
        sys.path.insert(0, os.path.abspath(lib_dir))
    # FreeCAD's start-up script runs in the __main__ namespace and deletes the
    # modules it finds there (time, json, ...), so put ours back afterwards.
    saved = {k: v for k, v in globals().items() if isinstance(v, types.ModuleType)}
    import FreeCAD  # noqa: F401  (imported for side effects and returned)

    globals().update(saved)
    return FreeCAD


# ---------------------------------------------------------------------------
# Model builders. Each takes (App, doc, size) and returns a short description.
# Geometry is fixed and deterministic so fingerprints can be compared exactly.
# ---------------------------------------------------------------------------


def build_part_trees(App, doc, size):
    n = size or 40
    holes_per_side = 3
    for i in range(n):
        ox = (i % 10) * 30.0
        oy = (i // 10) * 30.0
        box = doc.addObject("Part::Box", f"Box{i}")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        box.Placement.Base = App.Vector(ox, oy, 0)

        fillet = doc.addObject("Part::Fillet", f"Fillet{i}")
        fillet.Base = box
        fillet.Edges = [(e, 1.5, 1.5) for e in range(1, 13)]

        cyls = []
        for a in range(holes_per_side):
            for b in range(holes_per_side):
                cyl = doc.addObject("Part::Cylinder", f"Hole{i}_{a}{b}")
                cyl.Radius = 1.2
                cyl.Height = 30.0
                cyl.Placement.Base = App.Vector(ox + 5.0 + a * 5.0, oy + 5.0 + b * 5.0, -10.0)
                cyls.append(cyl)
        tools = doc.addObject("Part::MultiFuse", f"Tools{i}")
        tools.Shapes = cyls

        cut = doc.addObject("Part::Cut", f"Cut{i}")
        cut.Base = fillet
        cut.Tool = tools

        refine = doc.addObject("Part::Refine", f"Refine{i}")
        refine.Source = cut
    return f"{n} independent trees, {holes_per_side ** 2} holes each"


def _z_axis(body):
    for feature in body.Origin.OriginFeatures:
        if getattr(feature, "Role", "") == "Z_Axis":
            return feature
    raise RuntimeError("body origin has no Z_Axis")


def _pd_body_with_pattern(App, doc, name, occurrences, offset):
    body = doc.addObject("PartDesign::Body", name)
    body.Placement.Base = offset

    disk = body.newObject("PartDesign::AdditiveCylinder", f"{name}_Disk")
    disk.Radius = 50.0
    disk.Height = 10.0

    hole = body.newObject("PartDesign::SubtractiveCylinder", f"{name}_Hole")
    hole.Radius = 2.0
    hole.Height = 30.0
    hole.Placement.Base = App.Vector(40.0, 0.0, -10.0)

    pattern = body.newObject("PartDesign::PolarPattern", f"{name}_Pattern")
    pattern.Originals = [hole]
    pattern.Axis = (_z_axis(body), [""])
    pattern.Mode = "Extent"
    pattern.Angle = 360.0
    pattern.Occurrences = occurrences
    return body


def build_pd_pattern(App, doc, size):
    n = size or 48
    _pd_body_with_pattern(App, doc, "Body", n, App.Vector(0, 0, 0))
    return f"one body, polar pattern of {n} holes"


def build_pd_bodies(App, doc, size):
    n = size or 16
    for i in range(n):
        offset = App.Vector((i % 4) * 120.0, (i // 4) * 120.0, 0)
        _pd_body_with_pattern(App, doc, f"Body{i}", 12, offset)
    return f"{n} independent bodies, polar pattern of 12 holes each"


def build_assembly(App, doc, size):
    n = size or 24
    unique = 4
    import JointObject  # Assembly module; imports cleanly without the GUI

    sources = []
    for u in range(unique):
        box = doc.addObject("Part::Box", f"SrcBox{u}")
        box.Length, box.Width, box.Height = 20.0, 20.0, 10.0
        fillet = doc.addObject("Part::Fillet", f"SrcFillet{u}")
        fillet.Base = box
        fillet.Edges = [(e, 1.0 + 0.25 * u, 1.0 + 0.25 * u) for e in range(1, 13)]
        cyl = doc.addObject("Part::Cylinder", f"SrcHole{u}")
        cyl.Radius = 2.0 + u
        cyl.Height = 30.0
        cyl.Placement.Base = App.Vector(10.0, 10.0, -10.0)
        cut = doc.addObject("Part::Cut", f"SrcPart{u}")
        cut.Base = fillet
        cut.Tool = cyl
        sources.append(cut)

    asm = doc.addObject("Assembly::AssemblyObject", "Assembly")
    joints = asm.newObject("Assembly::JointGroup", "Joints")

    instances = []
    for i in range(n):
        link = asm.newObject("App::Link", f"Inst{i}")
        link.LinkedObject = sources[i % unique]
        link.Placement.Base = App.Vector(i * 25.0, 0, 0)
        instances.append(link)
    doc.recompute()

    ground = joints.newObject("App::FeaturePython", "Ground")
    JointObject.GroundedJoint(ground, instances[0])
    for i in range(1, n):
        joint = joints.newObject("App::FeaturePython", f"Fixed{i}")
        JointObject.Joint(joint, 0)
        refs = [
            [instances[i - 1], ["Face1", "Vertex1"]],
            [instances[i], ["Face1", "Vertex1"]],
        ]
        joint.Proxy.setJointConnectors(joint, refs)
    return f"{n} linked instances of {unique} parts, {n - 1} fixed joints"


BUILDERS = {
    "part_trees": build_part_trees,
    "pd_pattern": build_pd_pattern,
    "pd_bodies": build_pd_bodies,
    "assembly": build_assembly,
}


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def _round(value, digits=6):
    return float(f"{value:.{digits}g}")


def shape_fingerprint(shape):
    fp = {"null": shape.isNull()}
    if shape.isNull():
        return fp
    bb = shape.BoundBox
    fp.update(
        {
            "valid": shape.isValid(),
            "type": shape.ShapeType,
            "volume": _round(shape.Volume),
            "area": _round(shape.Area),
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
            "vertexes": len(shape.Vertexes),
            "bbox": [_round(v) for v in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)],
        }
    )
    element_map = getattr(shape, "ElementMap", None)
    if element_map:
        items = sorted((str(k), str(v)) for k, v in dict(element_map).items())
        fp["element_map_size"] = len(items)
        fp["element_map_sha256"] = hashlib.sha256(repr(items).encode()).hexdigest()
    return fp


def document_fingerprint(doc):
    objects = {}
    for obj in sorted(doc.Objects, key=lambda o: o.Name):
        entry = {"type": obj.TypeId, "valid": obj.isValid()}
        if not obj.isValid():
            entry["state"] = list(obj.State)
        if hasattr(obj, "Placement"):
            plc = obj.Placement
            entry["placement"] = [_round(v) for v in (*plc.Base, *plc.Rotation.Q)]
        shape = getattr(obj, "Shape", None)
        if shape is not None and hasattr(shape, "isNull"):
            entry["shape"] = shape_fingerprint(shape)
        objects[obj.Name] = entry
    digest = hashlib.sha256(json.dumps(objects, sort_keys=True).encode()).hexdigest()
    return digest, objects


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def peak_rss_mb():
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS reports bytes.
        return round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except (ImportError, OSError):
        return None


def cpu_info():
    info = {"cpu_count": os.cpu_count(), "machine": platform.machine()}
    if hasattr(os, "sched_getaffinity"):
        info["affinity"] = len(os.sched_getaffinity(0))
    return info


def occt_version():
    try:
        import Part

        return str(getattr(Part, "OCC_VERSION", ""))
    except ImportError:
        return ""


def full_recompute(doc):
    for obj in doc.Objects:
        obj.touch()
    start = time.perf_counter()
    count = doc.recompute()
    return time.perf_counter() - start, count


def time_tessellation(doc, deviation_percent=0.5):
    """Tessellate a copy of every shape the way the 3D view does by default.

    The view derives the linear deflection from the bounding box and the
    Deviation setting (0.5 % by default). Copies are meshed so the document's
    shapes are left untouched. Returns total seconds and triangle count.
    """
    total = 0.0
    triangles = 0
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or not hasattr(shape, "isNull") or shape.isNull() or not shape.Faces:
            continue
        bb = shape.BoundBox
        deflection = (bb.XLength + bb.YLength + bb.ZLength) / 300.0 * deviation_percent
        if deflection <= 0:
            continue
        copy = shape.copy()
        start = time.perf_counter()
        _points, tris = copy.tessellate(deflection)
        total += time.perf_counter() - start
        triangles += len(tris)
    return total, triangles


def main():
    args = parse_args()
    App = import_freecad(args.freecad_lib)

    # Parameters persist in user.cfg between runs, so always set or clear this one.
    doc_params = App.ParamGet("User parameter:BaseApp/Preferences/Document")
    if args.threads is not None:
        doc_params.SetInt("RecomputeThreads", args.threads)
    else:
        doc_params.RemInt("RecomputeThreads")

    doc = App.newDocument("RecomputeBench")
    t0 = time.perf_counter()
    description = BUILDERS[args.model](App, doc, args.size)
    build_s = time.perf_counter() - t0

    # Warm-up: first recompute also builds element maps and fills caches.
    first_s, first_count = full_recompute(doc)

    times = []
    counts = []
    for _ in range(args.repeat):
        elapsed, count = full_recompute(doc)
        times.append(elapsed)
        counts.append(count)

    mesh_s, mesh_triangles = time_tessellation(doc) if args.mesh else (None, None)

    digest, objects = document_fingerprint(doc)
    invalid = sorted(name for name, entry in objects.items() if not entry["valid"])
    ordered = sorted(times)

    result = {
        "label": args.label,
        "model": args.model,
        "size": args.size,
        "description": description,
        "freecad_version": ".".join(App.Version()[:3]),
        "freecad_revision": App.Version()[3] if len(App.Version()) > 3 else "",
        "occt_version": occt_version(),
        "python": platform.python_version(),
        "threads_param": args.threads,
        **cpu_info(),
        "objects": len(doc.Objects),
        "build_seconds": round(build_s, 4),
        "first_recompute_seconds": round(first_s, 4),
        "first_recompute_count": first_count,
        "recompute_seconds": [round(t, 4) for t in times],
        "recompute_counts": counts,
        "median_seconds": round(ordered[len(ordered) // 2], 4) if ordered else None,
        "min_seconds": round(ordered[0], 4) if ordered else None,
        "peak_rss_mb": peak_rss_mb(),
        "tessellation_seconds": round(mesh_s, 4) if mesh_s is not None else None,
        "tessellation_triangles": mesh_triangles,
        "invalid_objects": invalid,
        "fingerprint_sha256": digest,
        "fingerprint": objects,
    }

    if args.save_model:
        doc.saveAs(os.path.abspath(args.save_model))
    App.closeDocument(doc.Name)

    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)

    summary = (
        f"{args.model}: {description}; {result['objects']} objects; "
        f"median {result['median_seconds']} s over {args.repeat} runs; "
        f"peak RSS {result['peak_rss_mb']} MB; invalid {len(invalid)}"
    )
    print(summary, file=sys.stderr)
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
