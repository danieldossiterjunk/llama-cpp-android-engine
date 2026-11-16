#!/usr/bin/env python3
"""
trace_sources.py - list exactly which source files and headers went into each
shared library of a CMake/Ninja build, and scan them for licence markers.

It follows the real build graph: the link command of each lib*.so (from
`ninja -t commands`), the object files and static archives on that command,
the archive members, the source file of each object (compile_commands.json
"output" entries) and the headers each object included (`ninja -t deps`).

Usage:
  trace_sources.py --compile-commands compile_commands.json --ninja-commands commands.txt \
                   --ninja-deps deps.txt --src SRC_DIR --out-sources sources.txt \
                   --out-licences licence-scan.txt [--out-json trace.json]
"""

import argparse
import json
import os
import re
import shlex
import sys

LICENCE_RE = re.compile(r"(SPDX-License-Identifier|Copyright|Licensed under|License:|Public domain|LICENSE)", re.I)
THIRD_PARTY_PREFIXES = ("vendor/", "ggml/src/ggml-cpu/kleidiai", "ggml/src/ggml-cpu/llamafile", "ggml/src/ggml-cpu/amx")


def lib_name(path):
    base = os.path.basename(path)
    m = re.match(r"^(lib[^/]*?\.so)(\.[0-9.]+)?$", base)
    return m.group(1) if m else None


def split_cmd(line):
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compile-commands", required=True)
    ap.add_argument("--ninja-commands", required=True)
    ap.add_argument("--ninja-deps", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out-sources", required=True)
    ap.add_argument("--out-licences", required=True)
    ap.add_argument("--out-json")
    a = ap.parse_args()

    src_root = os.path.realpath(a.src)

    # object (relative to the build directory) -> source file
    obj_src = {}
    build_dir = None
    for e in json.load(open(a.compile_commands)):
        d = e["directory"]
        build_dir = build_dir or d
        f = e["file"] if os.path.isabs(e["file"]) else os.path.join(d, e["file"])
        out = e.get("output")
        if out is None:
            toks = split_cmd(e.get("command", "")) if "command" in e else e.get("arguments", [])
            for i, t in enumerate(toks):
                if t == "-o" and i + 1 < len(toks):
                    out = toks[i + 1]
        if out:
            obj_src[os.path.normpath(out)] = os.path.realpath(f)

    # object -> headers it included
    obj_deps = {}
    cur = None
    for line in open(a.ninja_deps, errors="replace"):
        if not line.strip():
            cur = None
            continue
        if line[:1] not in (" ", "\t"):
            m = re.match(r"^(.*?):\s+#deps", line)
            cur = os.path.normpath(m.group(1)) if m else None
            if cur is not None:
                obj_deps.setdefault(cur, [])
            continue
        if cur is not None:
            p = line.strip()
            if not os.path.isabs(p) and build_dir:
                p = os.path.join(build_dir, p)
            obj_deps[cur].append(os.path.realpath(p))

    # static archive -> member objects; shared library -> link inputs
    archives = {}
    links = {}
    for line in open(a.ninja_commands, errors="replace"):
        toks = split_cmd(line.strip())
        for i, t in enumerate(toks):
            if re.search(r"(^|/)(llvm-)?ar$", t) and i + 2 < len(toks) and toks[i + 2].endswith(".a"):
                arc = os.path.normpath(toks[i + 2])
                members = []
                for u in toks[i + 3:]:
                    if u in ("&&", ";"):
                        break
                    if u.endswith(".o"):
                        members.append(os.path.normpath(u))
                archives[arc] = members
        if "-shared" in toks and "-o" in toks:
            out = toks[toks.index("-o") + 1]
            name = lib_name(out)
            if name:
                links[name] = [os.path.normpath(t) for t in toks if t.endswith((".o", ".a"))]

    result = {}
    for name in sorted(links):
        objs = []
        static_libs = []
        for t in links[name]:
            if t.endswith(".a"):
                static_libs.append(t)
                objs.extend(archives.get(t, []))
            else:
                objs.append(t)
        sources = set()
        headers = set()
        for o in objs:
            if o in obj_src:
                sources.add(obj_src[o])
            for h in obj_deps.get(o, []):
                headers.add(h)
        # CMake unity builds compile generated wrappers that #include the real
        # .c/.cpp files: count those included translation units as sources
        unity = {h for h in headers if h.endswith((".c", ".cc", ".cpp", ".cxx"))}
        sources |= unity
        headers -= sources
        result[name] = dict(objects=len(objs), static_archives=static_libs,
                            sources=sorted(sources), headers=sorted(headers))

    def rel(p):
        return os.path.relpath(p, src_root) if p.startswith(src_root + os.sep) else None

    with open(a.out_sources, "w") as o:
        o.write("# Source files compiled into each library and headers they included,\n")
        o.write("# traced from the link commands, archive members, compile_commands.json and ninja deps.\n")
        for name, r in result.items():
            ins = [rel(p) for p in r["sources"] if rel(p)]
            hin = [rel(p) for p in r["headers"] if rel(p)]
            ext = [p for p in r["headers"] if not rel(p)]
            o.write("\n## %s: %d objects, %d llama.cpp sources, %d llama.cpp headers, %d toolchain/sysroot headers\n"
                    % (name, r["objects"], len(ins), len(hin), len(ext)))
            if r["static_archives"]:
                o.write("static archives linked in: %s\n" % ", ".join(r["static_archives"]))
            for p in ins:
                o.write("  src  %s\n" % p)
            for p in hin:
                o.write("  hdr  %s\n" % p)

    used = set()
    for r in result.values():
        for p in r["sources"] + r["headers"]:
            if rel(p):
                used.add(rel(p))
    third = sorted(p for p in used if p.startswith(THIRD_PARTY_PREFIXES))
    with open(a.out_licences, "w") as o:
        o.write("# Licence markers in the first 60 lines of every llama.cpp file that went into the libraries.\n")
        o.write("# Files without a marker are covered by the repository LICENSE (MIT, the ggml authors).\n\n")
        for p in sorted(used):
            try:
                head = open(os.path.join(src_root, p), errors="replace").read().split("\n")[:60]
            except OSError:
                continue
            hits = [l.strip()[:160] for l in head if LICENCE_RE.search(l)]
            if hits:
                o.write("%s\n" % p)
                for h in hits:
                    o.write("    %s\n" % h)
        o.write("\n# Third-party directories (vendor/, kleidiai, llamafile, amx) used by the libraries: %d file(s)\n" % len(third))
        for p in third:
            o.write("    %s\n" % p)
    if a.out_json:
        json.dump({k: dict(v, sources=[rel(p) or p for p in v["sources"]],
                           headers=[rel(p) or p for p in v["headers"]]) for k, v in result.items()},
                  open(a.out_json, "w"), indent=2)
    print("trace_sources.py: %s; %d llama.cpp files used; %d third-party"
          % (", ".join("%s %d sources" % (k, len(v["sources"])) for k, v in result.items()), len(used), len(third)))
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
