#!/usr/bin/env python3
"""
verify_elf.py - check Android arm64-v8a shared libraries against the
APK Builder 004 (v0.4.0) "native-prebuilt" rules, with no third-party
Python modules.

It reads every library as data (it never loads or runs it) and reports, per
library and for the bundle as a whole:

  * ELF identity: ELF64, little-endian, AArch64, ET_DYN shared object
  * SONAME present and equal to the file name; DT_NEEDED are plain names
  * 16 KiB page compatibility: PT_LOAD p_align >= 16384, offset/address
    congruence, no two PT_LOAD segments in one 16 KiB page, PT_GNU_RELRO
    not sharing its last 16 KiB page with writable data
  * Android loader rules: section headers present and consistent with the
    dynamic segment, DT_HASH or DT_GNU_HASH, no text relocations, no
    writable+executable segment, non-executable stack
  * dependencies: every DT_NEEDED is another library of the bundle, an
    allowed public Android system library (with its API level), or
    libc++_shared.so (reported separately); cycles; load order
  * Android API level: the .note.android.ident API level, and the API level
    implied by the bionic symbol versions the library needs (LIBC_N, LIBC_O,
    LIBC_P, ...)
  * optional: undefined symbols resolved against the bundle and against NDK
    system stub libraries of a given API level (--system-libs DIR)
  * optional: every function named in --api-names is exported by the bundle
  * C++ runtime exposure: exported __cxa_*/_Unwind_*/operator new/... symbols
  * JNI entry points (Java_* functions or JNI_OnLoad)
  * SHA-256, size, GNU build-id

Usage:
  verify_elf.py --rules environment.json|rules.json --min-sdk 28 \
                [--system-libs DIR] [--api-names FILE] [--require-jni] \
                [--json OUT.json] [--markdown OUT.md] LIB.so [LIB.so ...]

Exit status: 0 when every required check passes, 1 otherwise, 2 on usage errors.
"""

import argparse
import hashlib
import json
import os
import struct
import sys

PAGE_16K = 16384

# ---------------------------------------------------------------- constants
ET_DYN = 3
EM_AARCH64 = 183

PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3
PT_NOTE = 4
PT_TLS = 7
PT_GNU_EH_FRAME = 0x6474E550
PT_GNU_STACK = 0x6474E551
PT_GNU_RELRO = 0x6474E552

PF_X = 1
PF_W = 2
PF_R = 4

SHT_NOTE = 7
SHT_DYNSYM = 11
SHT_GNU_VERNEED = 0x6FFFFFFE
SHT_GNU_VERSYM = 0x6FFFFFFF

DT_NULL = 0
DT_NEEDED = 1
DT_HASH = 4
DT_STRTAB = 5
DT_SYMTAB = 6
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_TEXTREL = 22
DT_RUNPATH = 29
DT_FLAGS = 30
DT_GNU_HASH = 0x6FFFFEF5
DT_VERSYM = 0x6FFFFFF0
DT_FLAGS_1 = 0x6FFFFFFB
DT_VERNEED = 0x6FFFFFFE
DT_VERNEEDNUM = 0x6FFFFFFF
DF_TEXTREL = 0x4

STB_GLOBAL = 1
STB_WEAK = 2
STB_GNU_UNIQUE = 10
STT_FUNC = 2
STT_GNU_IFUNC = 10
STV_DEFAULT = 0
STV_PROTECTED = 3
SHN_UNDEF = 0

PT_NAMES = {
    PT_LOAD: "LOAD", PT_DYNAMIC: "DYNAMIC", PT_INTERP: "INTERP", PT_NOTE: "NOTE",
    PT_TLS: "TLS", PT_GNU_EH_FRAME: "GNU_EH_FRAME", PT_GNU_STACK: "GNU_STACK",
    PT_GNU_RELRO: "GNU_RELRO", 6: "PHDR",
}

# bionic symbol-version node -> first Android API level that has it
BIONIC_VERSION_API = {
    "LIBC": 21, "LIBC_N": 24, "LIBC_O": 26, "LIBC_P": 28, "LIBC_Q": 29,
    "LIBC_R": 30, "LIBC_S": 31, "LIBC_T": 33, "LIBC_U": 34, "LIBC_V": 35,
    "LIBC_W": 36, "LIBC_X": 37,
    "LIBLOG_O": 26, "LIBLOG_P": 28, "LIBLOG_Q": 29, "LIBLOG_R": 30,
    "LIBDL": 21, "LIBDL_N": 24,
}


class ElfError(Exception):
    pass


def page(x):
    return x // PAGE_16K


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- ELF reader
class Elf:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if len(d) < 64:
            raise ElfError("file is shorter than an ELF64 header")
        if d[:4] != b"\x7fELF":
            raise ElfError("not an ELF file (bad magic)")
        self.ei_class = d[4]
        self.ei_data = d[5]
        if self.ei_class != 2:
            raise ElfError("not ELF64 (EI_CLASS=%d)" % self.ei_class)
        if self.ei_data != 1:
            raise ElfError("not little-endian (EI_DATA=%d)" % self.ei_data)
        (self.e_ident, self.e_type, self.e_machine, self.e_version, self.e_entry,
         self.e_phoff, self.e_shoff, self.e_flags, self.e_ehsize, self.e_phentsize,
         self.e_phnum, self.e_shentsize, self.e_shnum, self.e_shstrndx) = \
            struct.unpack_from("<16sHHIQQQIHHHHHH", d, 0)
        self.phdrs = []
        if self.e_phnum:
            if self.e_phentsize != 56:
                raise ElfError("unexpected e_phentsize %d" % self.e_phentsize)
            end = self.e_phoff + self.e_phnum * 56
            if end > len(d):
                raise ElfError("program headers run past the end of the file")
            for i in range(self.e_phnum):
                p = struct.unpack_from("<IIQQQQQQ", d, self.e_phoff + i * 56)
                self.phdrs.append(dict(type=p[0], flags=p[1], offset=p[2], vaddr=p[3],
                                       paddr=p[4], filesz=p[5], memsz=p[6], align=p[7]))
        self.shdrs = []
        self.sections_ok = False
        self.section_error = None
        try:
            self._read_sections()
            self.sections_ok = True
        except ElfError as e:
            self.section_error = str(e)
        self.dyn = []
        self._read_dynamic()

    # --- helpers
    def vaddr_to_offset(self, vaddr):
        for p in self.phdrs:
            if p["type"] == PT_LOAD and p["vaddr"] <= vaddr < p["vaddr"] + p["filesz"]:
                return p["offset"] + (vaddr - p["vaddr"])
        return None

    def cstr(self, off):
        if off is None or off < 0 or off >= len(self.data):
            return None
        end = self.data.find(b"\0", off)
        if end < 0:
            return None
        return self.data[off:end].decode("utf-8", "replace")

    def _read_sections(self):
        d = self.data
        if self.e_shnum == 0 or self.e_shoff == 0:
            raise ElfError("no section headers")
        if self.e_shentsize != 64:
            raise ElfError("unexpected e_shentsize %d" % self.e_shentsize)
        if self.e_shoff + self.e_shnum * 64 > len(d):
            raise ElfError("section headers run past the end of the file")
        for i in range(self.e_shnum):
            s = struct.unpack_from("<IIQQQQIIQQ", d, self.e_shoff + i * 64)
            self.shdrs.append(dict(name_off=s[0], type=s[1], flags=s[2], addr=s[3], offset=s[4],
                                   size=s[5], link=s[6], info=s[7], addralign=s[8], entsize=s[9],
                                   name=None))
        if not (0 < self.e_shstrndx < self.e_shnum):
            raise ElfError("invalid e_shstrndx")
        shstr = self.shdrs[self.e_shstrndx]
        if shstr["offset"] + shstr["size"] > len(d):
            raise ElfError("section name table runs past the end of the file")
        for s in self.shdrs:
            if s["type"] != 8 and s["size"] and s["offset"] + s["size"] > len(d):  # 8 = SHT_NOBITS
                raise ElfError("a section runs past the end of the file")
            s["name"] = self.cstr(shstr["offset"] + s["name_off"])

    def section(self, name):
        for s in self.shdrs:
            if s["name"] == name:
                return s
        return None

    def _read_dynamic(self):
        dyn_ph = [p for p in self.phdrs if p["type"] == PT_DYNAMIC]
        if not dyn_ph:
            return
        p = dyn_ph[0]
        off, size = p["offset"], p["filesz"]
        if off + size > len(self.data):
            raise ElfError("dynamic segment runs past the end of the file")
        for i in range(size // 16):
            tag, val = struct.unpack_from("<qQ", self.data, off + i * 16)
            if tag == DT_NULL:
                break
            self.dyn.append((tag, val))

    def dyn_values(self, tag):
        return [v for t, v in self.dyn if t == tag]

    def dyn_value(self, tag):
        v = self.dyn_values(tag)
        return v[0] if v else None

    def dynstr(self, off):
        base = self.dyn_value(DT_STRTAB)
        if base is None:
            return None
        foff = self.vaddr_to_offset(base)
        if foff is None:
            return None
        return self.cstr(foff + off)

    # --- symbols
    def dynsyms(self):
        s = self.section(".dynsym") if self.sections_ok else None
        if s is None:
            return []
        strtab = self.shdrs[s["link"]] if s["link"] < len(self.shdrs) else None
        if strtab is None:
            return []
        n = s["size"] // 24
        versyms = self._versyms(n)
        vneeds = self._verneed_index()
        out = []
        for i in range(n):
            st_name, st_info, st_other, st_shndx, st_value, st_size = \
                struct.unpack_from("<IBBHQQ", self.data, s["offset"] + i * 24)
            name = self.cstr(strtab["offset"] + st_name) or ""
            ver = None
            if versyms is not None and i < len(versyms):
                vidx = versyms[i] & 0x7FFF
                if vidx in vneeds:
                    ver = vneeds[vidx]
            out.append(dict(name=name, bind=st_info >> 4, type=st_info & 0xF,
                            vis=st_other & 3, shndx=st_shndx, value=st_value, size=st_size,
                            version=ver))
        return out

    def _versyms(self, n):
        s = self.section(".gnu.version") if self.sections_ok else None
        if s is None:
            return None
        cnt = min(n, s["size"] // 2)
        return list(struct.unpack_from("<%dH" % cnt, self.data, s["offset"]))

    def _verneed_index(self):
        """version index -> (file, version name) for needed versions"""
        res = {}
        s = self.section(".gnu.version_r") if self.sections_ok else None
        if s is None:
            return res
        strtab = self.shdrs[s["link"]]
        off = s["offset"]
        end = s["offset"] + s["size"]
        while off < end:
            vn_version, vn_cnt, vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", self.data, off)
            fname = self.cstr(strtab["offset"] + vn_file)
            aoff = off + vn_aux
            for _ in range(vn_cnt):
                vna_hash, vna_flags, vna_other, vna_name, vna_next = \
                    struct.unpack_from("<IHHII", self.data, aoff)
                res[vna_other] = (fname, self.cstr(strtab["offset"] + vna_name))
                if vna_next == 0:
                    break
                aoff += vna_next
            if vn_next == 0:
                break
            off += vn_next
        return res

    def version_requirements(self):
        req = {}
        for (fname, vname) in self._verneed_index().values():
            req.setdefault(fname, set()).add(vname)
        return {k: sorted(v) for k, v in sorted(req.items())}

    # --- notes
    def notes(self):
        out = []
        regions = []
        if self.sections_ok:
            regions = [(s["offset"], s["size"]) for s in self.shdrs if s["type"] == SHT_NOTE]
        if not regions:
            regions = [(p["offset"], p["filesz"]) for p in self.phdrs if p["type"] == PT_NOTE]
        for off, size in regions:
            pos, end = off, off + size
            while pos + 12 <= end:
                namesz, descsz, ntype = struct.unpack_from("<III", self.data, pos)
                pos += 12
                name = self.data[pos:pos + namesz].rstrip(b"\0").decode("ascii", "replace")
                pos += (namesz + 3) & ~3
                desc = self.data[pos:pos + descsz]
                pos += (descsz + 3) & ~3
                out.append((name, ntype, desc))
        return out


# ---------------------------------------------------------------- rules
def load_rules(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    info = {"source": os.path.basename(path)}
    if "supported_profiles" in doc:
        prof = None
        for p in doc["supported_profiles"]:
            if p.get("id") == "cpp-native-v1":
                prof = p
        if prof is None:
            raise SystemExit("rules: environment.json has no cpp-native-v1 profile")
        np_ = prof["native_prebuilt"]
        info["builder_version"] = doc.get("builder", {}).get("version")
        info["sdk_limits"] = prof.get("sdk_limits", {})
    else:
        np_ = doc["native_prebuilt"]
        info["builder_version"] = doc.get("builder_version")
        info["sdk_limits"] = doc.get("sdk_limits", {})
    info["allowed_system_libraries"] = dict(np_["allowed_system_libraries"]["libraries"])
    info["limits"] = dict(np_["limits"])
    info["extension_version"] = np_.get("version")
    return info


# ---------------------------------------------------------------- checks
LIBCXX_RUNTIME_PREFIXES = (
    "__cxa_", "__gxx_personality", "_Unwind_", "__cxxabiv1", "_ZTVN10__cxxabiv1", "_ZTIN10__cxxabiv1",
    "_ZTSN10__cxxabiv1", "_Znw", "_Zna", "_Zdl", "_Zda", "_ZSt9terminatev", "_ZSt13set_terminate",
    "_ZSt15set_new_handler", "_ZSt14get_terminate", "_ZSt15get_new_handler", "_ZNSt9exception",
    "_ZNKSt9exception", "_ZTISt9exception", "_ZTVSt9exception", "_ZTSSt9exception", "_ZNSt9bad_alloc",
    "_ZNKSt9bad_alloc", "_ZTISt9bad_alloc", "_ZTVSt9bad_alloc", "_ZNSt20bad_array_new_length",
    "_ZTISt20bad_array_new_length", "_ZNSt8bad_cast", "_ZTISt8bad_cast", "_ZNSt10bad_typeid",
    "_ZTISt10bad_typeid", "__emutls_", "__dynamic_cast", "__aarch64_",
)


def classify_cxx(name):
    if name.startswith(LIBCXX_RUNTIME_PREFIXES):
        return "runtime"
    if "St6__ndk1" in name or "NSt6__ndk1" in name:
        return "std_ndk1"
    return None


class Report:
    def __init__(self):
        self.checks = []

    def add(self, cid, status, detail, required=True):
        self.checks.append(dict(id=cid, status=status, detail=detail, required=required))

    def ok(self):
        return all(c["status"] != "FAIL" for c in self.checks if c["required"])


def check_library(path, rules, min_sdk, bundle_names):
    rep = Report()
    fname = os.path.basename(path)
    result = dict(file=fname, path=path, bytes=os.path.getsize(path), sha256=sha256_file(path))
    try:
        elf = Elf(path)
    except ElfError as e:
        rep.add("ELF_READABLE", "FAIL", str(e))
        result["checks"] = rep.checks
        result["ok"] = False
        return result, None
    rep.add("ELF_READABLE", "PASS", "ELF64 little-endian file parsed")

    # identity
    rep.add("ELF_MACHINE_AARCH64", "PASS" if elf.e_machine == EM_AARCH64 else "FAIL",
            "e_machine=%d (AArch64 is 183)" % elf.e_machine)
    rep.add("ELF_TYPE_SHARED_OBJECT", "PASS" if elf.e_type == ET_DYN and not any(
        p["type"] == PT_INTERP for p in elf.phdrs) else "FAIL",
        "e_type=%d, PT_INTERP %s" % (elf.e_type, "present" if any(p["type"] == PT_INTERP for p in elf.phdrs) else "absent"))

    lim = rules["limits"]
    rep.add("SIZE_WITHIN_LIMIT", "PASS" if result["bytes"] <= lim["max_library_bytes"] else "FAIL",
            "%d bytes (limit %d)" % (result["bytes"], lim["max_library_bytes"]))

    reserved = set(rules["allowed_system_libraries"]) | {"libc++_shared.so"}
    rep.add("NAME_NOT_RESERVED", "FAIL" if fname in reserved else "PASS",
            "file name %s" % fname)
    ok_chars = all(c.isalnum() or c in "._-" for c in fname)
    rep.add("FILE_NAME_FORM", "PASS" if fname.startswith("lib") and fname.endswith(".so") and ok_chars else "FAIL",
            "lib<name>.so with letters, digits, . _ -")

    # dynamic section
    soname_off = elf.dyn_value(DT_SONAME)
    soname = elf.dynstr(soname_off) if soname_off is not None else None
    needed = [elf.dynstr(v) for v in elf.dyn_values(DT_NEEDED)]
    runpath = [elf.dynstr(v) for v in elf.dyn_values(DT_RUNPATH)]
    rpath = [elf.dynstr(v) for v in elf.dyn_values(DT_RPATH)]
    result.update(soname=soname, needed=needed, runpath=runpath, rpath=rpath)
    rep.add("DYNAMIC_SEGMENT", "PASS" if elf.dyn else "FAIL",
            "%d dynamic entries" % len(elf.dyn))
    rep.add("SONAME_EQUALS_FILE_NAME", "PASS" if soname == fname else "FAIL",
            "SONAME=%r, file=%r" % (soname, fname))
    bad_needed = [n for n in needed if n is None or "/" in n or not n]
    rep.add("NEEDED_PLAIN_NAMES", "PASS" if not bad_needed else "FAIL",
            "DT_NEEDED=%s" % needed if not bad_needed else "path or unreadable entries: %s" % bad_needed)
    rep.add("NO_RPATH_RUNPATH", "PASS" if not runpath and not rpath else "WARN",
            "RUNPATH=%s RPATH=%s" % (runpath, rpath), required=False)

    # dependencies
    allowed = rules["allowed_system_libraries"]
    dep_status = []
    dep_fail = False
    for n in needed:
        if n is None:
            continue
        if n in bundle_names:
            dep_status.append((n, "bundle"))
        elif n == "libc++_shared.so":
            dep_status.append((n, "APK Builder pinned libc++_shared.so"))
        elif n in allowed:
            api = allowed[n]
            if api > min_sdk:
                dep_status.append((n, "system library from API %d > min_sdk %d" % (api, min_sdk)))
                dep_fail = True
            else:
                dep_status.append((n, "public system library (API %d)" % api))
        else:
            dep_status.append((n, "MISSING: not in bundle and not an allowed system library"))
            dep_fail = True
    result["dependencies"] = dep_status
    rep.add("DEPENDENCIES_SATISFIED", "FAIL" if dep_fail else "PASS",
            "; ".join("%s -> %s" % d for d in dep_status) or "no DT_NEEDED")
    result["needs_libcxx_shared"] = "libc++_shared.so" in needed

    # segments / 16 KiB
    loads = sorted([p for p in elf.phdrs if p["type"] == PT_LOAD], key=lambda p: p["vaddr"])
    segs = []
    for p in elf.phdrs:
        fl = ("R" if p["flags"] & PF_R else "-") + ("W" if p["flags"] & PF_W else "-") + ("X" if p["flags"] & PF_X else "-")
        segs.append(dict(type=PT_NAMES.get(p["type"], hex(p["type"])), offset=hex(p["offset"]),
                         vaddr=hex(p["vaddr"]), filesz=hex(p["filesz"]), memsz=hex(p["memsz"]),
                         flags=fl, align=hex(p["align"])))
    result["program_headers"] = segs
    if not loads:
        rep.add("PAGE16K_LOAD_ALIGN", "FAIL", "no PT_LOAD segments")
    else:
        bad = [p for p in loads if p["align"] < PAGE_16K or (p["align"] & (p["align"] - 1))]
        rep.add("PAGE16K_LOAD_ALIGN", "PASS" if not bad else "FAIL",
                "PT_LOAD p_align = %s" % sorted({hex(p["align"]) for p in loads}))
        badc = [p for p in loads if p["align"] and (p["vaddr"] % p["align"]) != (p["offset"] % p["align"])]
        rep.add("PAGE16K_OFFSET_CONGRUENT", "PASS" if not badc else "FAIL",
                "p_vaddr == p_offset (mod p_align) for every PT_LOAD" if not badc else
                "incongruent segments at %s" % [hex(p["vaddr"]) for p in badc])
        shared = []
        for a, b in zip(loads, loads[1:]):
            a_end = a["vaddr"] + a["memsz"]
            if a["memsz"] and b["memsz"] and page(a_end - 1) >= page(b["vaddr"]):
                shared.append("%s..%s and %s" % (hex(a["vaddr"]), hex(a_end), hex(b["vaddr"])))
        rep.add("PAGE16K_NO_SHARED_PAGE", "PASS" if not shared else "FAIL",
                "no two PT_LOAD segments share a 16 KiB page" if not shared else "; ".join(shared))
    relros = [p for p in elf.phdrs if p["type"] == PT_GNU_RELRO]
    if not relros:
        rep.add("PAGE16K_RELRO", "PASS", "no PT_GNU_RELRO segment")
    else:
        problems = []
        for r in relros:
            r_start, r_end = r["vaddr"], r["vaddr"] + r["memsz"]
            if r_end % PAGE_16K == 0:
                continue
            last = page(r_end - 1)
            for p in loads:
                if not (p["flags"] & PF_W):
                    continue
                w_start, w_end = p["vaddr"], p["vaddr"] + p["memsz"]
                # writable bytes outside RELRO
                parts = []
                if w_start < r_start:
                    parts.append((w_start, min(w_end, r_start)))
                if w_end > r_end:
                    parts.append((max(w_start, r_end), w_end))
                for s, e in parts:
                    if e > s and page(s) <= last <= page(e - 1):
                        problems.append("RELRO end %s shares 16 KiB page %s with writable data %s..%s"
                                        % (hex(r_end), hex(last * PAGE_16K), hex(s), hex(e)))
        rep.add("PAGE16K_RELRO", "PASS" if not problems else "FAIL",
                "GNU_RELRO end does not share its last 16 KiB page with writable data" if not problems
                else "; ".join(problems))
    wx = [p for p in loads if (p["flags"] & PF_W) and (p["flags"] & PF_X)]
    rep.add("NO_WRITABLE_EXECUTABLE_SEGMENT", "PASS" if not wx else "FAIL",
            "no PT_LOAD is both writable and executable" if not wx else "W+X at %s" % [hex(p["vaddr"]) for p in wx])
    stacks = [p for p in elf.phdrs if p["type"] == PT_GNU_STACK]
    rep.add("NON_EXECUTABLE_STACK", "PASS" if stacks and not (stacks[0]["flags"] & PF_X) else "WARN",
            "PT_GNU_STACK %s" % (("flags " + segs[elf.phdrs.index(stacks[0])]["flags"]) if stacks else "absent"),
            required=False)
    tls = [p for p in elf.phdrs if p["type"] == PT_TLS]
    rep.add("TLS_SEGMENT", "INFO", "PT_TLS absent (no ELF TLS)" if not tls else
            "PT_TLS present, p_align=%s" % hex(tls[0]["align"]), required=False)

    # text relocations
    flags = elf.dyn_value(DT_FLAGS) or 0
    textrel = bool(elf.dyn_values(DT_TEXTREL)) or bool(flags & DF_TEXTREL)
    rep.add("NO_TEXT_RELOCATIONS", "FAIL" if textrel else "PASS",
            "DT_TEXTREL %s, DF_TEXTREL %s" % ("present" if elf.dyn_values(DT_TEXTREL) else "absent",
                                              "set" if flags & DF_TEXTREL else "clear"))
    has_hash = elf.dyn_value(DT_HASH) is not None or elf.dyn_value(DT_GNU_HASH) is not None
    rep.add("SYMBOL_HASH_TABLE", "PASS" if has_hash else "FAIL",
            "DT_HASH %s, DT_GNU_HASH %s" % ("present" if elf.dyn_value(DT_HASH) is not None else "absent",
                                            "present" if elf.dyn_value(DT_GNU_HASH) is not None else "absent"))

    # section headers consistent with the dynamic segment
    if not elf.sections_ok:
        rep.add("SECTION_HEADERS_CONSISTENT", "FAIL", elf.section_error or "section headers unreadable")
    else:
        issues = []
        dyn_ph = [p for p in elf.phdrs if p["type"] == PT_DYNAMIC]
        dsec = elf.section(".dynamic")
        if dsec is None:
            issues.append(".dynamic section missing")
        elif dyn_ph and (dsec["addr"] != dyn_ph[0]["vaddr"] or dsec["offset"] != dyn_ph[0]["offset"]):
            issues.append(".dynamic section does not match PT_DYNAMIC")
        for tag, sname in ((DT_STRTAB, ".dynstr"), (DT_SYMTAB, ".dynsym"), (DT_GNU_HASH, ".gnu.hash"), (DT_HASH, ".hash")):
            v = elf.dyn_value(tag)
            if v is None:
                continue
            s = elf.section(sname)
            if s is None:
                issues.append("%s section missing" % sname)
            elif s["addr"] != v:
                issues.append("%s address %s != dynamic %s" % (sname, hex(s["addr"]), hex(v)))
        rep.add("SECTION_HEADERS_CONSISTENT", "PASS" if not issues else "FAIL",
                "%d section headers; .dynamic/.dynstr/.dynsym/hash match the dynamic segment" % len(elf.shdrs)
                if not issues else "; ".join(issues))

    # notes: API level, NDK, build id
    api_note = None
    ndk_note = None
    build_id = None
    for name, ntype, desc in elf.notes():
        if name == "Android" and ntype == 1 and len(desc) >= 4:
            api_note = struct.unpack_from("<I", desc, 0)[0]
            if len(desc) >= 4 + 64:
                ndk_note = desc[4:68].split(b"\0")[0].decode("ascii", "replace")
                if len(desc) >= 132:
                    bn = desc[68:132].split(b"\0")[0].decode("ascii", "replace")
                    ndk_note = "%s (build %s)" % (ndk_note, bn)
        elif name == "GNU" and ntype == 3:
            build_id = desc.hex()
    vreq = elf.version_requirements()
    ver_api = 0
    unknown_versions = []
    for lib, vers in vreq.items():
        for v in vers:
            if v in BIONIC_VERSION_API:
                ver_api = max(ver_api, BIONIC_VERSION_API[v])
            else:
                unknown_versions.append("%s@%s" % (v, lib))
    result.update(android_api_note=api_note, ndk_note=ndk_note, build_id=build_id,
                  version_requirements=vreq, api_from_symbol_versions=ver_api or None)
    need_api = max(api_note or 0, ver_api or 0)
    result["min_api_required"] = need_api or None
    sdk_max = rules.get("sdk_limits", {}).get("sdk_maximum")
    if need_api == 0:
        rep.add("ANDROID_API_LEVEL", "WARN", "no Android API information found", required=False)
    else:
        status = "PASS" if need_api <= min_sdk and (sdk_max is None or need_api <= sdk_max) else "FAIL"
        rep.add("ANDROID_API_LEVEL", status,
                "needs API %d (ident note %s, symbol versions %s); project min_sdk %d; builder maximum %s"
                % (need_api, api_note, ver_api or "none", min_sdk, sdk_max))
    if unknown_versions:
        rep.add("SYMBOL_VERSIONS_KNOWN", "WARN", "unrecognised version nodes: %s" % unknown_versions, required=False)

    # symbols
    syms = elf.dynsyms()
    exported = [s for s in syms if s["name"] and s["shndx"] != SHN_UNDEF and
                s["bind"] in (STB_GLOBAL, STB_WEAK, STB_GNU_UNIQUE) and s["vis"] in (STV_DEFAULT, STV_PROTECTED)]
    undefined = [s for s in syms if s["name"] and s["shndx"] == SHN_UNDEF]
    funcs = sorted({s["name"] for s in exported if s["type"] in (STT_FUNC, STT_GNU_IFUNC)})
    runtime = sorted({s["name"] for s in exported if classify_cxx(s["name"]) == "runtime"})
    stdx = sorted({s["name"] for s in exported if classify_cxx(s["name"]) == "std_ndk1"})
    jni = sorted({s["name"] for s in exported if s["name"].startswith("Java_") or s["name"] in ("JNI_OnLoad", "JNI_OnUnload")})
    result.update(exported_symbols=len(exported), exported_functions=len(funcs),
                  undefined_symbols=len(undefined), jni_entries=jni,
                  cxx_runtime_exports=runtime, std_ndk1_exports=len(stdx))
    rep.add("NO_CXX_RUNTIME_EXPORTS", "PASS" if not runtime else "FAIL",
            "no exported C++ runtime symbols (__cxa_*, _Unwind_*, operator new/delete, ...)" if not runtime
            else "%d exported, e.g. %s" % (len(runtime), runtime[:8]))
    rep.add("STD_TEMPLATE_EXPORTS", "INFO",
            "%d exported std::__ndk1 symbols emitted by the library's own code (template instantiations)" % len(stdx),
            required=False)
    result["checks"] = rep.checks
    result["ok"] = rep.ok()
    result["_exported_names"] = sorted({s["name"] for s in exported})
    result["_undefined"] = [(s["name"], s["bind"], s["version"]) for s in undefined]
    return result, elf


def load_system_exports(sysdir):
    exports = {}
    if not sysdir:
        return exports
    for fn in sorted(os.listdir(sysdir)):
        if not fn.endswith(".so"):
            continue
        p = os.path.join(sysdir, fn)
        try:
            e = Elf(p)
        except ElfError:
            continue
        names = set()
        for s in e.dynsyms():
            if s["name"] and s["shndx"] != SHN_UNDEF and s["bind"] in (STB_GLOBAL, STB_WEAK, STB_GNU_UNIQUE):
                names.add(s["name"])
        exports[fn] = names
    return exports


def toposort(libs):
    """libs: name -> list of bundle dependencies. Returns (order deps-first, cycle or None)."""
    order, state = [], {}
    cycle = []

    def visit(n, stack):
        st = state.get(n)
        if st == 2:
            return True
        if st == 1:
            cycle.extend(stack[stack.index(n):] + [n])
            return False
        state[n] = 1
        for d in libs.get(n, []):
            if not visit(d, stack + [n]):
                return False
        state[n] = 2
        order.append(n)
        return True

    for n in sorted(libs):
        if not visit(n, []):
            return order, cycle
    return order, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", required=True, help="APK Builder environment.json or extracted rules JSON")
    ap.add_argument("--min-sdk", type=int, required=True, help="the project's build.min_sdk")
    ap.add_argument("--system-libs", help="directory of NDK system stub libraries for one API level")
    ap.add_argument("--api-names", help="file with one function name per line that must be exported")
    ap.add_argument("--require-jni", action="store_true", help="fail unless some library exports a JNI entry")
    ap.add_argument("--json", help="write the full report as JSON")
    ap.add_argument("--markdown", help="write a Markdown summary")
    ap.add_argument("libs", nargs="+")
    a = ap.parse_args()

    rules = load_rules(a.rules)
    sdk = rules.get("sdk_limits", {})
    if sdk.get("min_sdk_minimum") and a.min_sdk < sdk["min_sdk_minimum"]:
        print("min_sdk %d is below the builder minimum %d" % (a.min_sdk, sdk["min_sdk_minimum"]), file=sys.stderr)
        return 2
    names = [os.path.basename(p) for p in a.libs]
    if len(set(names)) != len(names):
        print("duplicate library file names", file=sys.stderr)
        return 2

    results, elves = [], {}
    for p in a.libs:
        r, e = check_library(p, rules, a.min_sdk, set(names))
        results.append(r)
        elves[r["file"]] = e

    bundle_checks = Report()
    lim = rules["limits"]
    bundle_checks.add("LIBRARY_COUNT", "PASS" if len(results) <= lim["max_libraries"] else "FAIL",
                      "%d libraries (limit %d)" % (len(results), lim["max_libraries"]))
    graph = {r["file"]: [n for n in (r.get("needed") or []) if n in names] for r in results}
    order, cycle = toposort(graph)
    bundle_checks.add("NO_DEPENDENCY_CYCLE", "PASS" if not cycle else "FAIL",
                      "load order (dependencies first): %s" % " -> ".join(order) if not cycle
                      else "cycle: %s" % " -> ".join(cycle))
    needs_cxx = sorted(r["file"] for r in results if r.get("needs_libcxx_shared"))
    bundle_checks.add("LIBCXX_SHARED_NEEDED", "INFO",
                      "no library needs libc++_shared.so" if not needs_cxx else
                      "needs APK Builder's pinned libc++_shared.so: %s" % needs_cxx, required=False)

    # undefined symbol resolution
    exports = {r["file"]: set(r.get("_exported_names", [])) for r in results}
    sysexp = load_system_exports(a.system_libs)
    unresolved_all = {}
    needed_of = {r["file"]: [n for n in (r.get("needed") or []) if n] for r in results}

    def dependency_closure(lib):
        # Android's linker resolves a library's symbols in its local group:
        # the library and all of its DT_NEEDED dependencies, breadth first.
        seen, queue = [], list(needed_of.get(lib, []))
        while queue:
            n = queue.pop(0)
            if n in seen:
                continue
            seen.append(n)
            queue.extend(needed_of.get(n, []))
        return seen

    if sysexp:
        for r in results:
            if "_undefined" not in r:
                continue
            providers = dependency_closure(r["file"])
            unresolved = []
            for name, bind, ver in r["_undefined"]:
                found = False
                for prov in providers:
                    if name in exports.get(prov, ()) or name in sysexp.get(prov, ()):
                        found = True
                        break
                if not found and bind != STB_WEAK:
                    unresolved.append(name)
            unresolved_all[r["file"]] = sorted(set(unresolved))
        total = sum(len(v) for v in unresolved_all.values())
        bundle_checks.add("UNDEFINED_SYMBOLS_RESOLVE", "PASS" if total == 0 else "FAIL",
                          "every non-weak undefined symbol is provided by a DT_NEEDED library (system stubs from %s)"
                          % a.system_libs if total == 0 else
                          "; ".join("%s: %s" % (k, v[:12]) for k, v in unresolved_all.items() if v))
    else:
        bundle_checks.add("UNDEFINED_SYMBOLS_RESOLVE", "NOT TESTED",
                          "no --system-libs directory given; system-library symbols not checked", required=False)

    api_result = None
    if a.api_names:
        with open(a.api_names, "r", encoding="utf-8") as f:
            wanted = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        all_exp = set().union(*exports.values()) if exports else set()
        missing = sorted(n for n in wanted if n not in all_exp)
        where = {}
        for n in wanted:
            for lib in names:
                if n in exports.get(lib, ()):
                    where[lib] = where.get(lib, 0) + 1
                    break
        api_result = dict(declared=len(wanted), missing=missing, provided_by=where)
        bundle_checks.add("HEADER_FUNCTIONS_EXPORTED", "PASS" if not missing else "FAIL",
                          "%d of %d header-declared functions exported (%s)" %
                          (len(wanted) - len(missing), len(wanted),
                           ", ".join("%s: %d" % kv for kv in sorted(where.items())))
                          if not missing else "missing %d: %s" % (len(missing), missing[:20]))

    jni_libs = sorted(r["file"] for r in results if r.get("jni_entries"))
    if a.require_jni:
        bundle_checks.add("JNI_ENTRY_PRESENT", "PASS" if jni_libs else "FAIL",
                          "JNI entries in %s" % jni_libs if jni_libs else "no library exports Java_* or JNI_OnLoad")
    else:
        bundle_checks.add("JNI_ENTRY_PRESENT", "INFO",
                          ("JNI entries in %s" % jni_libs) if jni_libs else
                          "no JNI entry: these are engine libraries; APK Builder needs a JNI bridge beside them "
                          "(otherwise PREBUILT_NO_JNI_ENTRY)", required=False)

    all_ok = all(r["ok"] for r in results) and bundle_checks.ok()
    out = dict(
        tool="verify_elf.py", rules=rules["source"], builder_version=rules.get("builder_version"),
        native_prebuilt_extension_version=rules.get("extension_version"), min_sdk=a.min_sdk,
        system_libs=a.system_libs, overall="PASS" if all_ok else "FAIL",
        bundle=dict(checks=bundle_checks.checks, load_order=order, unresolved=unresolved_all,
                    header_api=api_result),
        libraries=[{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
    )
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, sort_keys=False)
            f.write("\n")
    md = render_markdown(out)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 0 if all_ok else 1


def render_markdown(out):
    L = []
    L.append("# ELF verification against APK Builder %s native-prebuilt rules" % (out.get("builder_version") or "?"))
    L.append("")
    L.append("- Rules file: `%s` (extension version %s)" % (out["rules"], out.get("native_prebuilt_extension_version")))
    L.append("- Project min_sdk assumed: %d" % out["min_sdk"])
    L.append("- System stub libraries: %s" % (out["system_libs"] or "not provided"))
    L.append("- **Overall: %s**" % out["overall"])
    L.append("")
    L.append("## Bundle")
    L.append("")
    L.append("| Check | Status | Detail |")
    L.append("|---|---|---|")
    for c in out["bundle"]["checks"]:
        L.append("| %s | %s | %s |" % (c["id"], c["status"], c["detail"].replace("|", "\\|")))
    for r in out["libraries"]:
        L.append("")
        L.append("## %s" % r["file"])
        L.append("")
        L.append("- Size: %d bytes" % r["bytes"])
        L.append("- SHA-256: `%s`" % r["sha256"])
        if "soname" in r:
            L.append("- SONAME: `%s`" % r.get("soname"))
            L.append("- DT_NEEDED: %s" % ", ".join("`%s`" % n for n in r.get("needed") or []))
            L.append("- GNU build-id: `%s`" % r.get("build_id"))
            L.append("- Android ident note: API %s, NDK %s" % (r.get("android_api_note"), r.get("ndk_note")))
            L.append("- Symbol versions needed: %s" % json.dumps(r.get("version_requirements")))
            L.append("- Exported symbols: %d (functions %d); undefined: %d; std::__ndk1 exports: %d"
                     % (r.get("exported_symbols", 0), r.get("exported_functions", 0),
                        r.get("undefined_symbols", 0), r.get("std_ndk1_exports", 0)))
            L.append("")
            L.append("| Type | Offset | VirtAddr | FileSiz | MemSiz | Flags | Align |")
            L.append("|---|---|---|---|---|---|---|")
            for s in r.get("program_headers", []):
                L.append("| %s | %s | %s | %s | %s | %s | %s |" % (s["type"], s["offset"], s["vaddr"], s["filesz"],
                                                                s["memsz"], s["flags"], s["align"]))
        L.append("")
        L.append("| Check | Status | Detail |")
        L.append("|---|---|---|")
        for c in r["checks"]:
            L.append("| %s | %s | %s |" % (c["id"], c["status"], str(c["detail"]).replace("|", "\\|")))
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
