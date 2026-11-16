#!/usr/bin/env python3
"""
header_api.py - list the functions that the public llama.cpp / ggml headers
declare with LLAMA_API, GGML_API or GGML_BACKEND_API, and optionally write a
C source file that takes the address of every one of them.

Linking that C file against the built libraries with -Wl,--no-undefined
proves that every function the shipped headers declare is exported by the
shipped libraries (headers and libraries come from the same commit).

With --src, every declared function is also looked up in the source tree
(.c/.cc/.cpp/.m files, not headers): a function that is declared but has no
definition anywhere in upstream's source is written to --out-undefined and
left out of the C reference file. Those are upstream header defects, reported
separately; they are not library export failures.

Usage:
  header_api.py --out-names names.txt [--out-c refs.c] [--src DIR --out-undefined undefined.txt]
                HEADER.h [HEADER.h ...]
"""

import argparse
import os
import re
import sys

API_MACROS = ("LLAMA_API", "GGML_API", "GGML_BACKEND_API")
NOT_NAMES = {
    "DEPRECATED", "GGML_DEPRECATED", "__attribute__", "__declspec", "GGML_ATTRIBUTE_FORMAT",
    "visibility", "deprecated", "LLAMA_API", "GGML_API", "GGML_BACKEND_API", "GGML_RESTRICT",
    "sizeof", "defined",
}


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def strip_preprocessor(text):
    out = []
    cont = False
    for line in text.split("\n"):
        s = line.strip()
        if cont or s.startswith("#"):
            cont = s.endswith("\\")
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def declarations(text):
    text = strip_preprocessor(strip_comments(text))
    pat = re.compile(r"\b(%s)\b" % "|".join(API_MACROS))
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return
        end = text.find(";", m.end())
        if end < 0:
            return
        yield text[m.end():end]
        pos = end + 1


def function_name(decl):
    # a declaration looks like: <return type> name ( params ) [, "deprecation hint")]
    # skip function-pointer declarations of the form  (*name)(...)
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", decl):
        name = m.group(1)
        if name in NOT_NAMES:
            continue
        before = decl[:m.start()].rstrip()
        if before.endswith("(*") or before.endswith("( *"):
            continue
        return name
    return None


SOURCE_EXTS = (".c", ".cc", ".cpp", ".cxx", ".m", ".mm")


def find_definitions(src, names):
    """Return the subset of names that have a function definition in src."""
    wanted = set(names)
    found = set()
    pat = re.compile(r"\b(%s)\s*\(" % "|".join(sorted(map(re.escape, wanted), key=len, reverse=True)))
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in (".git", "build")]
        for fn in files:
            if not fn.endswith(SOURCE_EXTS):
                continue
            try:
                text = strip_comments(open(os.path.join(root, fn), encoding="utf-8", errors="replace").read())
            except OSError:
                continue
            for m in pat.finditer(text):
                name = m.group(1)
                if name in found:
                    continue
                # skip to the matching ')' and see whether a body '{' follows
                depth, i = 0, m.end() - 1
                while i < len(text):
                    c = text[i]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                j = i + 1
                while j < len(text) and text[j] in " \t\r\n":
                    j += 1
                if j < len(text) and text[j] == "{":
                    found.add(name)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-names", required=True)
    ap.add_argument("--out-c")
    ap.add_argument("--src")
    ap.add_argument("--out-undefined")
    ap.add_argument("headers", nargs="+")
    a = ap.parse_args()

    names = []
    per_header = {}
    for h in a.headers:
        with open(h, "r", encoding="utf-8") as f:
            text = f.read()
        found = []
        for d in declarations(text):
            n = function_name(d)
            if n and n not in names:
                names.append(n)
                found.append(n)
        per_header[os.path.basename(h)] = len(found)

    with open(a.out_names, "w", encoding="utf-8") as f:
        f.write("# functions declared with %s in: %s\n" % ("/".join(API_MACROS),
                ", ".join("%s (%d)" % kv for kv in per_header.items())))
        for n in names:
            f.write(n + "\n")

    undefined = []
    if a.src:
        defined = find_definitions(a.src, names)
        undefined = [n for n in names if n not in defined]
        if a.out_undefined:
            with open(a.out_undefined, "w", encoding="utf-8") as f:
                f.write("# declared in the public headers but defined nowhere in the upstream source tree\n")
                for n in undefined:
                    f.write(n + "\n")
    refs = [n for n in names if n not in undefined]

    if a.out_c:
        with open(a.out_c, "w", encoding="utf-8") as f:
            f.write("/* Generated by header_api.py: takes the address of every function the public\n")
            f.write(" * headers declare, so that linking with -Wl,--no-undefined proves each one is\n")
            f.write(" * exported by the built libraries. Not part of any app. */\n")
            for h in a.headers:
                f.write('#include "%s"\n' % os.path.basename(h))
            f.write("\ntypedef void (*apkb_fn)(void);\n")
            f.write("__attribute__((visibility(\"default\"))) const apkb_fn apkb_header_refs[] = {\n")
            for n in refs:
                f.write("    (apkb_fn) &%s,\n" % n)
            f.write("};\n")
            f.write("__attribute__((visibility(\"default\"))) const unsigned apkb_header_ref_count = %d;\n" % len(refs))

    total = len(names)
    print("header_api.py: %d functions (%s)" % (total, ", ".join("%s %d" % kv for kv in per_header.items())))
    if a.src:
        print("header_api.py: %d declared but never defined upstream: %s" % (len(undefined), ", ".join(undefined) or "none"))
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
