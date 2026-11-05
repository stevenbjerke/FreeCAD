# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FreeCAD project.

"""Compare recompute_bench.py results from a baseline and a candidate build.

    python tools/benchmark/compare_results.py baseline_dir candidate_dir [--markdown out.md]

Each directory holds JSON files written by recompute_bench.py. Every
candidate result (one per model, size and RecomputeThreads value) is
compared with the baseline result for the same model, size and CPU count:

* speed: ratio of median recompute times;
* geometry: validity, volume, area, topology counts and bounding boxes must
  match the baseline;
* element names: with the serial loop (RecomputeThreads 0 or unset) the
  topological element maps must match the baseline exactly. The level
  scheduler (RecomputeThreads >= 1) creates new names in level order, so
  its names may be numbered differently from the baseline; they must instead
  be identical across all thread counts >= 1.

Exit status is 1 if any check fails, so the script can gate a CI job.
"""

import argparse
import copy
import glob
import json
import math
import os
import sys

FLOAT_REL_TOL = 1e-6
NAME_KEYS = ("element_map_sha256", "element_map_size")


def load(directory):
    results = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            results.append(json.load(fh))
    return results


def _close(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=1e-9)
    return a == b


def diff_values(path, a, b, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: present in only one result")
            else:
                diff_values(f"{path}.{key}", a[key], b[key], out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diff_values(f"{path}[{i}]", x, y, out)
    elif not _close(a, b):
        out.append(f"{path}: {a!r} != {b!r}")


def without_names(fingerprint):
    stripped = copy.deepcopy(fingerprint)
    for entry in stripped.values():
        for key in NAME_KEYS:
            entry.get("shape", {}).pop(key, None)
    return stripped


def name_diffs(a, b):
    out = []
    for obj in sorted(set(a) | set(b)):
        sa = a.get(obj, {}).get("shape", {})
        sb = b.get(obj, {}).get("shape", {})
        for key in NAME_KEYS:
            if sa.get(key) != sb.get(key):
                out.append(f"{obj}.{key}")
    return out


def group_key(result):
    return (result["model"], result.get("size"), result.get("affinity"))


def compare(baseline, candidate):
    base_by_key = {group_key(r): r for r in baseline}
    rows, problems = [], []

    for c in sorted(candidate, key=lambda r: (group_key(r), r.get("threads_param") or 0)):
        key = group_key(c)
        threads = c.get("threads_param")
        name = f"{c['model']} (size {c.get('size')}, cpus {c.get('affinity')})"
        b = base_by_key.get(key)
        if b is None:
            problems.append(f"{name}: no baseline result")
            continue

        geometry = []
        diff_values("", without_names(b["fingerprint"]), without_names(c["fingerprint"]), geometry)
        names = name_diffs(b["fingerprint"], c["fingerprint"])
        serial_path = not threads
        if c["invalid_objects"]:
            problems.append(f"{name}, threads {threads}: invalid objects {c['invalid_objects']}")
        if geometry:
            problems.append(f"{name}, threads {threads}: {len(geometry)} geometry differences, first: {geometry[:3]}")
        if names and serial_path:
            problems.append(f"{name}, threads {threads}: element names differ for {len(names)} objects, first: {names[:3]}")

        rows.append(
            {
                "name": name,
                "threads": threads,
                "objects": c["objects"],
                "base_s": b["median_seconds"],
                "cand_s": c["median_seconds"],
                "base_first_s": b["first_recompute_seconds"],
                "cand_first_s": c["first_recompute_seconds"],
                "speedup": b["median_seconds"] / c["median_seconds"] if c["median_seconds"] else float("nan"),
                "base_mb": b["peak_rss_mb"],
                "mesh_s": c.get("tessellation_seconds"),
                "cand_mb": c["peak_rss_mb"],
                "geometry_ok": not geometry,
                "names_ok": not names,
                "serial_path": serial_path,
            }
        )

    # Determinism: every level-scheduler thread count must produce the same result.
    by_key = {}
    for c in candidate:
        if c.get("threads_param"):
            by_key.setdefault(group_key(c), []).append(c)
    for key, results in by_key.items():
        ref = results[0]
        for other in results[1:]:
            if ref["fingerprint_sha256"] != other["fingerprint_sha256"]:
                diffs = []
                diff_values("", ref["fingerprint"], other["fingerprint"], diffs)
                problems.append(
                    f"{key[0]}: threads {ref.get('threads_param')} and {other.get('threads_param')} "
                    f"differ ({len(diffs)} differences, first: {diffs[:3]})"
                )
    return rows, problems


def to_markdown(rows, problems):
    lines = [
        "| Model | Threads | Objects | Baseline (s) | Candidate (s) | Speedup | First recompute base / cand (s) "
        "| Peak RSS base / cand (MB) | Mesh once (s) | Geometry | Names |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['threads']} | {r['objects']} | {r['base_s']:.3f} | {r['cand_s']:.3f} | "
            f"{r['speedup']:.2f}x | {r['base_first_s']:.3f} / {r['cand_first_s']:.3f} | "
            f"{r['base_mb']} / {r['cand_mb']} | {r['mesh_s'] if r['mesh_s'] is not None else '-'} | "
            f"{'same' if r['geometry_ok'] else '**DIFFERS**'} | "
            f"{'same' if r['names_ok'] else ('**DIFFER**' if r['serial_path'] else 'renumbered')} |"
        )
    lines.append("")
    if problems:
        lines += ["**Problems:**", ""] + [f"- {p}" for p in problems]
    else:
        lines.append(
            "All checks passed: geometry matches the baseline, the serial path keeps the "
            "baseline element names, and all level-scheduler thread counts agree exactly."
        )
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--markdown", help="also write the table to this file")
    args = p.parse_args()

    rows, problems = compare(load(args.baseline), load(args.candidate))
    text = to_markdown(rows, problems)
    print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
