# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FreeCAD project.

"""Summarize ThreadSanitizer logs from the recompute benchmark.

    python tools/benchmark/tsan_summary.py LOG_DIR [--markdown out.md]

Reads every file in LOG_DIR (TSAN_OPTIONS=log_path=...), splits it into
reports and groups data races by the first frame of each of the two stacks
that lies in FreeCAD's own sources. Prints one line per distinct race with
the number of reports, and the full text of one example of each.

Exit status is 1 if any report was found.
"""

import argparse
import collections
import os
import re
import sys

FRAME = re.compile(r"^\s+#\d+\s+(?P<func>.+?)\s+(?P<loc>\S+:\d+)(?::\d+)?(?:\s+\(.*\))?\s*$")
SOURCE_MARKERS = ("/src/App/", "/src/Base/", "/src/Gui/", "/src/Mod/")


def first_freecad_frame(lines):
    for line in lines:
        m = FRAME.match(line)
        if m and any(marker in m.group("loc") for marker in SOURCE_MARKERS):
            loc = m.group("loc")
            loc = loc[loc.index("/src/") + 1 :]
            return f"{m.group('func')} at {loc}"
    return "(no FreeCAD frame)"


def split_stacks(report):
    """Return the frame lines of the first two stacks (the two conflicting accesses)."""
    stacks, current = [], None
    for line in report.splitlines():
        if re.match(r"^\s+(Previous )?(read|write|atomic)", line, re.IGNORECASE) or line.lstrip().startswith(
            ("Write of size", "Read of size", "Previous write", "Previous read", "Atomic")
        ):
            current = []
            stacks.append(current)
        elif current is not None and line.strip().startswith("#"):
            current.append(line)
        elif current is not None and not line.strip():
            current = None
    return stacks[:2]


def parse(directory):
    reports = []
    for name in sorted(os.listdir(directory)):
        with open(os.path.join(directory, name), errors="replace") as fh:
            text = fh.read()
        for chunk in text.split("==================")[1:]:
            if "WARNING: ThreadSanitizer:" in chunk:
                reports.append(chunk.strip())
    return reports


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("log_dir")
    p.add_argument("--markdown")
    args = p.parse_args()

    reports = parse(args.log_dir) if os.path.isdir(args.log_dir) else []
    groups = collections.OrderedDict()
    for report in reports:
        kind = re.search(r"WARNING: ThreadSanitizer: ([^(\n]+)", report).group(1).strip()
        stacks = split_stacks(report)
        where = " / ".join(sorted({first_freecad_frame(s) for s in stacks})) or "(unknown)"
        key = f"{kind}: {where}"
        groups.setdefault(key, []).append(report)

    lines = [f"ThreadSanitizer: {len(reports)} reports, {len(groups)} distinct", ""]
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {len(items)} x {key}")
    lines.append("")
    for key, items in groups.items():
        lines += [f"<details><summary>{key}</summary>", "", "```", items[0][:6000], "```", "", "</details>", ""]
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if reports else 0


if __name__ == "__main__":
    sys.exit(main())
