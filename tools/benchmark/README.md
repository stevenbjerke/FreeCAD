<!--
SPDX-FileCopyrightText: 2026 FreeCAD Developers
SPDX-License-Identifier: CC-BY-4.0
-->

# Recompute benchmark

Tools for measuring how long a document recompute takes and for checking that
a change to recompute does not change its results.

| Script | Purpose |
|---|---|
| `recompute_bench.py` | Builds a synthetic model, times full recomputes, records peak memory and a fingerprint of every object. |
| `compare_results.py` | Compares a baseline and a candidate build: speed, memory, geometry and topological element names. |
| `run_tests.py` | Runs FreeCAD unit test modules with a chosen `RecomputeThreads` value. |
| `BASELINE` | The upstream commit the `recompute_benchmark.yml` workflow compares against by default. |

## Running locally

From a FreeCAD source tree built with pixi in release mode
(`pixi run configure-release && pixi run build-release`, which builds into `build/release`):

```sh
pixi run python tools/benchmark/recompute_bench.py --freecad-lib build/release/lib \
    --model part_trees --repeat 5 --out part_trees.json
```

Models:

- `part_trees`: N independent Part trees (filleted box, nine holes cut, refined). A wide
  dependency graph.
- `pd_pattern`: one PartDesign body with a polar pattern of N holes. A narrow graph with one
  expensive feature.
- `pd_bodies`: N independent PartDesign bodies, each with a polar pattern.
- `assembly`: an Assembly with N linked instances of four parts, chained with fixed joints.

Useful options: `--size N` (model size), `--threads N` (sets the `RecomputeThreads` document
parameter; omit it to clear the parameter), `--mesh` (also time tessellating every shape once,
as the 3D view would), `--save-model file.FCStd` (keep the generated model).

To compare two builds, write each build's results to its own directory and run:

```sh
python3 tools/benchmark/compare_results.py baseline/ candidate/ --markdown comparison.md
```

The exit status is 1 if geometry differs, an object is invalid, the serial path changes element
names, or two level-scheduler thread counts disagree.

To scale the number of CPUs available, which limits OCCT's own thread pool as well, run under
`taskset`, for example `taskset -c 0-3 pixi run python ...`.

## What the fingerprint covers

For every object: whether it is valid, its placement, and for shapes: validity, volume, area,
face, edge and vertex counts, bounding box, and a SHA-256 of the sorted topological element map.
Floats are compared with a relative tolerance of 1e-6; element maps must match exactly.

## Element names and RecomputeThreads

String IDs in topological element names are numbered in the order new strings are created.
With `RecomputeThreads` 0 or unset, recompute runs the classic serial loop and names must match
the baseline exactly. With `RecomputeThreads` >= 1, recompute proceeds level by level; new names
are numbered in level order, so the first recompute of new geometry can number them differently
from the serial loop. That numbering is the same for every thread count >= 1, which the
comparison checks. Strings that already exist keep their IDs either way.
