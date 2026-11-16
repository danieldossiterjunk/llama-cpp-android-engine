#!/usr/bin/env python3
"""
elf_diff.py - explain how two builds of the same ELF library differ, section
by section (size and SHA-256 of every section), so a reproducibility failure
can be traced to debug information, code, data or notes.

Usage: elf_diff.py A.so B.so
Exit status: 0 if the files are identical, 1 if they differ, 2 on errors.
"""

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_elf import Elf, ElfError  # noqa: E402


def sections(path):
    e = Elf(path)
    out = {}
    for s in e.shdrs:
        name = s["name"] or "(unnamed)"
        if s["type"] == 8:  # SHT_NOBITS
            data = b""
        else:
            data = e.data[s["offset"]:s["offset"] + s["size"]]
        out[name] = (s["size"], hashlib.sha256(data).hexdigest())
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = sys.argv[1:3]
    da, db = open(a, "rb").read(), open(b, "rb").read()
    if da == db:
        print("identical: %s" % os.path.basename(a))
        return 0
    try:
        sa, sb = sections(a), sections(b)
    except ElfError as err:
        print("cannot parse: %s" % err)
        return 2
    print("differs: %s (A %d bytes, B %d bytes)" % (os.path.basename(a), len(da), len(db)))
    same = 0
    for name in sorted(set(sa) | set(sb)):
        x, y = sa.get(name), sb.get(name)
        if x == y:
            same += 1
            continue
        print("  %-28s A=%s B=%s" % (name, "%d bytes %s" % (x[0], x[1][:12]) if x else "absent",
                                       "%d bytes %s" % (y[0], y[1][:12]) if y else "absent"))
    print("  (%d sections identical)" % same)
    return 1


if __name__ == "__main__":
    sys.exit(main())
