# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FreeCAD project.

"""Run FreeCAD unit test modules with a given RecomputeThreads setting.

    python tools/benchmark/run_tests.py --freecad-lib build/release/lib \\
        --threads 4 TestPartApp TestPartDesignApp

With --threads 0 (the default) recompute uses the classic serial loop; with a
positive value every document recompute in the tests goes through the level
scheduler, so the existing test suites exercise the parallel path. Exits with
status 1 if any test fails or errors.
"""

import argparse
import os
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--freecad-lib", default=None, help="directory containing FreeCAD.so")
    p.add_argument("--threads", type=int, default=0, help="RecomputeThreads value")
    p.add_argument("tests", nargs="+", help="test module names, e.g. TestPartApp")
    args = p.parse_args()

    if args.freecad_lib:
        sys.path.insert(0, os.path.abspath(args.freecad_lib))
    import FreeCAD

    FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Document").SetInt(
        "RecomputeThreads", args.threads
    )
    import TestApp

    failed = []
    for name in args.tests:
        result = TestApp.TestText(name)
        if not result.wasSuccessful():
            failed.append(f"{name}: {len(result.failures)} failures, {len(result.errors)} errors")

    print(f"\nRecomputeThreads={args.threads}: ran {', '.join(args.tests)}", file=sys.stderr)
    for line in failed:
        print(f"  FAILED {line}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
