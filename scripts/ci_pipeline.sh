#!/usr/bin/env bash
# ci_pipeline.sh - the complete, ordered recipe for LLAMA_CPP_ANDROID_ARM64_ENGINE_001.
#
# Stages (run in this order; the GitHub workflow runs one stage per step):
#   env        record the build machine
#   fetch      verify the upstream v1.0.0 tag and commit, make two clean checkouts
#   toolchain  install and verify the official NDK, SDK CMake/Ninja, android-29 platform
#   build-a    first clean build
#   build-b    second clean build in a different directory (reproducibility)
#   compare    compare the two builds byte for byte
#   finalize   strip, collect headers, verify against APK Builder 004 rules,
#              link tests, Java check, licences, build information
#   seal       copy the stage logs into the bundle and write SHA256SUMS.txt
#
# Works on GitHub's ubuntu-24.04 runner and on any Linux x86_64 machine with an
# Android SDK (ANDROID_HOME) that has cmdline-tools/latest/bin/sdkmanager.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"
# shellcheck source=engine.conf
. "$HERE/engine.conf"

WORK="${ENGINE_WORK:-${RUNNER_TEMP:-/tmp}/llama-engine}"
BUNDLE="$WORK/bundle/${PACKAGE_ID}-ci"
STATE="$WORK/state.env"
STAGE_LOGS="$WORK/stage-logs"
RULES="$REPO_ROOT/rules/apkbuilder-004-native-prebuilt.json"
mkdir -p "$WORK" "$BUNDLE" "$STAGE_LOGS"

die() { echo "ci_pipeline.sh: ERROR: $*" >&2; exit 1; }
note() { echo "ci_pipeline.sh: $*"; }
load_state() { [ -f "$STATE" ] || die "state file missing (run the stages in order)"; . "$STATE"; }
put_state() { echo "$1=$(printf '%q' "$2")" >> "$STATE"; }

stage_env() {
    : > "$STATE"
    mkdir -p "$BUNDLE/provenance"
    {
        echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "uname: $(uname -a)"
        echo "--- /etc/os-release"; cat /etc/os-release
        echo "--- runner image"
        echo "ImageOS=${ImageOS:-}"; echo "ImageVersion=${ImageVersion:-}"
        echo "--- resources"; echo "nproc=$(nproc)"; free -m || true; df -h "$WORK" || true
        echo "--- tools"
        git --version; python3 --version; bash --version | sed -n '1p'
        java -version 2>&1 | sed -n '1,3p'; javac -version 2>&1 | sed -n '1p'
        echo "--- GitHub Actions run (empty outside GitHub)"
        env | grep -E '^(GITHUB_(REPOSITORY|REPOSITORY_ID|REPOSITORY_OWNER|SHA|REF|RUN_ID|RUN_NUMBER|RUN_ATTEMPT|WORKFLOW|WORKFLOW_REF|WORKFLOW_SHA|SERVER_URL|EVENT_NAME|JOB|ACTOR)|RUNNER_(OS|ARCH|NAME|ENVIRONMENT))=' | sort || true
    } > "$BUNDLE/provenance/runner.txt"
    cat "$BUNDLE/provenance/runner.txt"
}

stage_fetch() {
    : >> "$STATE"
    local P="$BUNDLE/provenance" UP="$WORK/upstream"
    mkdir -p "$P"
    note "checking the tag on the official repository: $LLAMA_REPO_URL"
    git ls-remote "$LLAMA_REPO_URL" "refs/tags/$LLAMA_TAG" "refs/tags/$LLAMA_TAG^{}" | tee "$P/upstream-ls-remote.txt"
    local tagobj peeled
    tagobj="$(awk -v r="refs/tags/$LLAMA_TAG" '$2==r {print $1}' "$P/upstream-ls-remote.txt")"
    peeled="$(awk -v r="refs/tags/$LLAMA_TAG^{}" '$2==r {print $1}' "$P/upstream-ls-remote.txt")"
    [ "$tagobj" = "$LLAMA_TAG_OBJECT" ] || die "remote tag object is '$tagobj', expected $LLAMA_TAG_OBJECT"
    [ "$peeled" = "$LLAMA_COMMIT" ] || die "remote tag points at '$peeled', expected $LLAMA_COMMIT"

    rm -rf "$UP"
    git clone --filter=blob:none --no-checkout --single-branch --branch "$LLAMA_DEFAULT_BRANCH" "$LLAMA_REPO_URL" "$UP"
    git -C "$UP" fetch --filter=blob:none origin "refs/tags/$LLAMA_TAG:refs/tags/$LLAMA_TAG"
    [ "$(git -C "$UP" cat-file -t "refs/tags/$LLAMA_TAG")" = "tag" ] || die "$LLAMA_TAG is not an annotated tag"
    [ "$(git -C "$UP" rev-parse "refs/tags/$LLAMA_TAG")" = "$LLAMA_TAG_OBJECT" ] || die "local tag object mismatch"
    [ "$(git -C "$UP" rev-parse "refs/tags/$LLAMA_TAG^{commit}")" = "$LLAMA_COMMIT" ] || die "local tag commit mismatch"
    git -C "$UP" merge-base --is-ancestor "$LLAMA_COMMIT" "origin/$LLAMA_DEFAULT_BRANCH" \
        || die "$LLAMA_COMMIT is not on $LLAMA_DEFAULT_BRANCH"
    {
        echo "repository: $LLAMA_REPO_URL"
        echo "tag: $LLAMA_TAG (annotated tag object $LLAMA_TAG_OBJECT)"
        echo "commit: $LLAMA_COMMIT"
        echo "commit is an ancestor of origin/$LLAMA_DEFAULT_BRANCH: yes"
        echo "origin/$LLAMA_DEFAULT_BRANCH at fetch time: $(git -C "$UP" rev-parse "origin/$LLAMA_DEFAULT_BRANCH")"
        echo "upstream alias tags pointing at the commit:"
        git ls-remote --tags "$LLAMA_REPO_URL" | awk -v c="$LLAMA_COMMIT" '$1==c {print "  " $2}'
    } | tee "$P/upstream-verification.txt"
    git -C "$UP" cat-file -p "refs/tags/$LLAMA_TAG" > "$P/upstream-tag-object.txt"
    if git -C "$UP" verify-tag "refs/tags/$LLAMA_TAG" > "$P/upstream-tag-signature.txt" 2>&1; then
        echo "tag signature: verified" >> "$P/upstream-verification.txt"
    else
        echo "tag signature: none or not verifiable here (the tag is created by github-actions[bot]; see upstream-tag-object.txt)" >> "$P/upstream-verification.txt"
    fi

    local A="$WORK/a/llama.cpp" B="$WORK/second-build-b/src/llama.cpp"
    rm -rf "$WORK/a" "$WORK/second-build-b"
    git -C "$UP" worktree prune
    git -C "$UP" worktree add --detach "$A" "$LLAMA_COMMIT"
    git -C "$UP" worktree add --detach "$B" "$LLAMA_COMMIT"
    for d in "$A" "$B"; do
        [ "$(git -C "$d" rev-parse HEAD)" = "$LLAMA_COMMIT" ] || die "checkout $d is not at $LLAMA_COMMIT"
        [ -z "$(git -C "$d" status --porcelain --untracked-files=all)" ] || die "checkout $d is not clean"
    done
    git -C "$UP" show -s --format=fuller "$LLAMA_COMMIT" > "$P/upstream-commit.txt"
    echo "tree: $(git -C "$UP" rev-parse "$LLAMA_COMMIT^{tree}")" >> "$P/upstream-commit.txt"
    git -C "$A" archive --format=tar --prefix=llama.cpp/ "$LLAMA_COMMIT" | sha256sum | awk '{print $1 "  git-archive-tar-of-" c}' c="$LLAMA_COMMIT" > "$P/upstream-git-archive.sha256"
    put_state SRC_A "$A"
    put_state SRC_B "$B"
    note "fetch OK"
}

stage_toolchain() {
    load_state
    local P="$BUNDLE/provenance" L="$BUNDLE/logs"
    mkdir -p "$P" "$L"
    local SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
    [ -n "$SDK" ] && [ -d "$SDK" ] || die "ANDROID_HOME is not set to an Android SDK"
    local SDKM="$SDK/cmdline-tools/latest/bin/sdkmanager"
    [ -x "$SDKM" ] || die "sdkmanager not found at $SDKM"
    note "sdkmanager $("$SDKM" --version 2>/dev/null | tail -1)"
    # The Android SDK licence must be accepted to install SDK packages (upstream's CI does the same).
    (yes 2>/dev/null | "$SDKM" --licenses) > "$L/sdkmanager-licences.log" 2>&1 || true
    "$SDKM" --install "ndk;$NDK_VERSION" "cmake;$SDK_CMAKE_VERSION" "platforms;$JAVA_CHECK_PLATFORM" \
        > "$L/sdkmanager-install.log" 2>&1 || { tail -50 "$L/sdkmanager-install.log"; die "sdkmanager install failed"; }
    tail -5 "$L/sdkmanager-install.log"
    "$SDKM" --list_installed > "$P/sdk-list-installed.txt" 2>&1 || true

    local NDK="$SDK/ndk/$NDK_VERSION"
    local rev
    rev="$(sed -n 's/^Pkg\.Revision *= *//p' "$NDK/source.properties" | tr -d '\r')"
    [ "$rev" = "$NDK_VERSION" ] || die "NDK Pkg.Revision is '$rev', expected $NDK_VERSION"
    local TC="$NDK/toolchains/llvm/prebuilt/linux-x86_64"
    local CMB="$SDK/cmake/$SDK_CMAKE_VERSION/bin"
    [ -x "$TC/bin/clang" ] && [ -x "$CMB/cmake" ] && [ -x "$CMB/ninja" ] || die "toolchain binaries missing"
    local SYSLIB="$TC/sysroot/usr/lib/aarch64-linux-android"
    [ -f "$SDK/platforms/$JAVA_CHECK_PLATFORM/android.jar" ] || die "android.jar for $JAVA_CHECK_PLATFORM missing"
    {
        echo "android_sdk: $SDK"
        echo "ndk_dir: $NDK"
        echo "--- $NDK/source.properties"; cat "$NDK/source.properties"; echo
        echo "--- clang"; "$TC/bin/clang" --version
        [ -f "$TC/AndroidVersion.txt" ] && { echo "--- AndroidVersion.txt"; cat "$TC/AndroidVersion.txt"; }
        echo "--- ld.lld"; "$TC/bin/ld.lld" --version
        echo "--- llvm-strip"; "$TC/bin/llvm-strip" --version
        echo "--- cmake"; "$CMB/cmake" --version
        echo "--- ninja"; "$CMB/ninja" --version
        echo "--- sha256 of key toolchain files"
        local f
        for f in "$NDK/source.properties" "$(readlink -f "$TC/bin/clang")" "$(readlink -f "$TC/bin/ld.lld")" \
                 "$(readlink -f "$TC/bin/llvm-strip")" "$NDK/build/cmake/android.toolchain.cmake" \
                 "$SYSLIB/libc++_static.a" "$SYSLIB/libc++abi.a" \
                 "$SYSLIB/$ANDROID_API/libc.so" "$SYSLIB/$ANDROID_API/libm.so" "$SYSLIB/$ANDROID_API/libdl.so" \
                 "$SYSLIB/$ANDROID_API/liblog.so" "$SYSLIB/$ANDROID_API/crtbegin_so.o" "$SYSLIB/$ANDROID_API/crtend_so.o" \
                 "$CMB/cmake" "$CMB/ninja" "$SDK/platforms/$JAVA_CHECK_PLATFORM/android.jar"; do
            if [ -f "$f" ]; then sha256sum "$f"; else echo "(not present) $f"; fi
        done
        echo "--- libunwind archives in the toolchain"
        find "$TC/lib" -name 'libunwind.a' -path '*aarch64*' -exec sha256sum {} \; || true
        echo "--- NDK notice files"
        ls -l "$NDK"/NOTICE* 2>/dev/null || echo "(no NOTICE files at the NDK root)"
    } | tee "$P/toolchain.txt"
    put_state SDK "$SDK"
    put_state NDK "$NDK"
    put_state TC "$TC"
    put_state CMB "$CMB"
    put_state SYSLIB "$SYSLIB"
    note "toolchain OK"
}

stage_build() {
    load_state
    local which="$1" src build prefix logs
    case "$which" in
        a) src="$SRC_A"; build="$WORK/a/build"; prefix="$WORK/a/install"; logs="$BUNDLE/logs/build-a" ;;
        b) src="$SRC_B"; build="$WORK/second-build-b/out/build"; prefix="$WORK/second-build-b/out/install"; logs="$BUNDLE/logs/build-b" ;;
        *) die "unknown build $which" ;;
    esac
    rm -rf "$build" "$prefix"
    ANDROID_NDK="$NDK" CMAKE_BIN="$CMB" bash "$HERE/build_engine.sh" "$src" "$build" "$prefix" "$logs"
    put_state "LIBDIR_$(echo "$which" | tr a-z A-Z)" "$(cat "$logs/libdir.txt")"
}

stage_compare() {
    load_state
    local E="$BUNDLE/evidence"
    mkdir -p "$E"
    local all_same=1 lib ha hb
    {
        echo "Reproducibility: two clean builds of the same commit with the same toolchain,"
        echo "in different directories:"
        echo "  A: $SRC_A"
        echo "  B: $SRC_B"
        echo
        for lib in $ENGINE_LIBS; do
            ha="$(sha256sum "$LIBDIR_A/$lib" | cut -d' ' -f1)"
            hb="$(sha256sum "$LIBDIR_B/$lib" | cut -d' ' -f1)"
            if [ "$ha" = "$hb" ]; then
                echo "IDENTICAL  $lib  $ha"
            else
                all_same=0
                echo "DIFFERENT  $lib  A=$ha  B=$hb  ($(cmp -l "$LIBDIR_A/$lib" "$LIBDIR_B/$lib" 2>/dev/null | wc -l) bytes differ)"
            fi
        done
        echo
        if [ "$all_same" = 1 ]; then echo "RESULT: REPRODUCIBLE (bit-identical)"; else echo "RESULT: NOT REPRODUCIBLE"; fi
    } | tee "$E/reproducibility.txt"
    if [ "$all_same" = 1 ]; then put_state REPRO "REPRODUCIBLE"; else put_state REPRO "NOT_REPRODUCIBLE"; fi
}

stage_finalize() {
    load_state
    local B="$BUNDLE" E="$BUNDLE/evidence" fail=0
    local LT="$WORK/link-tests"
    rm -rf "$B/jniLibs" "$B/debug" "$B/include" "$B/licenses" "$E/readelf" "$E/link-tests" "$LT"
    mkdir -p "$B/jniLibs/$ANDROID_ABI" "$B/debug/$ANDROID_ABI" "$B/include" "$B/licenses" \
             "$E/readelf" "$E/link-tests" "$LT" "$B/build-recipe"

    note "libraries: unstripped copies to debug/, stripped copies to jniLibs/"
    local lib
    for lib in $ENGINE_LIBS; do
        cp "$LIBDIR_A/$lib" "$B/debug/$ANDROID_ABI/$lib"
        "$TC/bin/llvm-strip" --strip-unneeded -o "$B/jniLibs/$ANDROID_ABI/$lib" "$LIBDIR_A/$lib"
    done

    note "public headers from the same checkout"
    local h
    for h in $PUBLIC_HEADERS; do
        cp "$SRC_A/$h" "$B/include/"
    done

    note "header-declared C API"
    local hdrs=()
    for h in $C_API_HEADERS; do hdrs+=("$B/include/$h"); done
    python3 -I "$HERE/header_api.py" --out-names "$E/header-api-functions.txt" --out-c "$LT/apkb_header_refs.c" "${hdrs[@]}"
    cp "$LT/apkb_header_refs.c" "$E/link-tests/apkb_header_refs.c"

    note "verification against APK Builder 004 rules (API $ANDROID_API system stubs)"
    if python3 -I "$HERE/verify_elf.py" --rules "$RULES" --min-sdk "$ANDROID_API" \
            --system-libs "$SYSLIB/$ANDROID_API" --api-names "$E/header-api-functions.txt" \
            --json "$E/verify-engine.json" --markdown "$E/verify-engine.md" \
            "$B/jniLibs/$ANDROID_ABI/"*.so > "$E/verify-engine.stdout.txt"; then
        put_state VERIFY_ENGINE PASS
    else
        put_state VERIFY_ENGINE FAIL; fail=1
    fi
    note "probe: the same libraries against API 26 stubs (shows which symbols need API > 26; may FAIL by design)"
    python3 -I "$HERE/verify_elf.py" --rules "$RULES" --min-sdk 26 --system-libs "$SYSLIB/26" \
        --json "$E/probe-api26.json" --markdown "$E/probe-api26.md" \
        "$B/jniLibs/$ANDROID_ABI/"*.so > /dev/null || true
    note "unstripped debug copies"
    if python3 -I "$HERE/verify_elf.py" --rules "$RULES" --min-sdk "$ANDROID_API" \
            --system-libs "$SYSLIB/$ANDROID_API" --json "$E/verify-debug.json" --markdown "$E/verify-debug.md" \
            "$B/debug/$ANDROID_ABI/"*.so > /dev/null; then
        put_state VERIFY_DEBUG PASS
    else
        put_state VERIFY_DEBUG FAIL
    fi

    note "raw ELF dumps"
    for lib in $ENGINE_LIBS; do
        local f="$B/jniLibs/$ANDROID_ABI/$lib"
        "$TC/bin/llvm-readelf" --file-header --program-headers --section-headers --dynamic --notes --version-info --wide "$f" > "$E/readelf/$lib.readelf.txt"
        "$TC/bin/llvm-readelf" --dyn-syms --wide "$f" > "$E/readelf/$lib.dynsym.txt"
        "$TC/bin/llvm-objdump" -p "$f" > "$E/readelf/$lib.objdump-p.txt"
        file "$f" >> "$E/readelf/file.txt"
    done

    note "link test 1: every header-declared function resolves against the libraries (bridge API $APKB_BRIDGE_API)"
    if "$TC/bin/clang" --target="aarch64-linux-android$APKB_BRIDGE_API" -std=c17 -O2 -fPIC -fstack-protector-strong \
            -Wall -Werror -Wno-deprecated-declarations -I"$B/include" -shared \
            -o "$LT/libapkbheaderrefs.so" "$LT/apkb_header_refs.c" \
            -L"$B/jniLibs/$ANDROID_ABI" -lllama -lggml -lggml-base -lggml-cpu \
            -Wl,--no-undefined -Wl,-z,max-page-size=16384 -Wl,-soname,libapkbheaderrefs.so \
            > "$E/link-tests/header-refs.log" 2>&1; then
        echo "LINK OK" >> "$E/link-tests/header-refs.log"; put_state LINK_HEADERS PASS
    else
        echo "LINK FAILED" >> "$E/link-tests/header-refs.log"; put_state LINK_HEADERS FAIL; fail=1
    fi
    cat "$E/link-tests/header-refs.log"

    note "link test 2: the self-test JNI bridge, compiled like APK Builder 004 does (C17, -O2 -fPIC -fstack-protector-strong -Wall, API $APKB_BRIDGE_API) plus -Wextra -Werror"
    if "$TC/bin/clang" --target="aarch64-linux-android$APKB_BRIDGE_API" -std=c17 -O2 -fPIC -fstack-protector-strong \
            -Wall -Wextra -Werror -I"$REPO_ROOT/selftest/jni" -I"$B/include" -shared \
            -o "$LT/libllamabridge.so" "$REPO_ROOT/selftest/jni/llamabridge.c" \
            -L"$B/jniLibs/$ANDROID_ABI" -lllama -lggml -lggml-base -lggml-cpu -llog \
            -Wl,--no-undefined -Wl,-z,max-page-size=16384 -Wl,-soname,libllamabridge.so \
            > "$E/link-tests/bridge.log" 2>&1; then
        echo "COMPILE+LINK OK" >> "$E/link-tests/bridge.log"; put_state LINK_BRIDGE PASS
    else
        echo "COMPILE+LINK FAILED" >> "$E/link-tests/bridge.log"; put_state LINK_BRIDGE FAIL; fail=1
    fi
    cat "$E/link-tests/bridge.log"
    if [ -f "$LT/libllamabridge.so" ]; then
        "$TC/bin/llvm-readelf" --program-headers --dynamic --wide "$LT/libllamabridge.so" > "$E/link-tests/libllamabridge.readelf.txt"
        if python3 -I "$HERE/verify_elf.py" --rules "$RULES" --min-sdk "$ANDROID_API" --require-jni \
                --system-libs "$SYSLIB/$ANDROID_API" --json "$E/verify-with-selftest-bridge.json" \
                --markdown "$E/verify-with-selftest-bridge.md" \
                "$LT/libllamabridge.so" "$B/jniLibs/$ANDROID_ABI/"*.so > /dev/null; then
            put_state VERIFY_BRIDGE PASS
        else
            put_state VERIFY_BRIDGE FAIL; fail=1
        fi
    fi

    note "Java check: the self-test sources at Java 8 level against $JAVA_CHECK_PLATFORM android.jar"
    rm -rf "$LT/classes"; mkdir -p "$LT/classes"
    if javac -source 8 -target 8 -Xlint:all -Xlint:-options -encoding UTF-8 \
            -bootclasspath "$SDK/platforms/$JAVA_CHECK_PLATFORM/android.jar" -d "$LT/classes" \
            $(find "$REPO_ROOT/selftest/src" -name '*.java' | sort) > "$E/link-tests/javac.log" 2>&1; then
        echo "JAVAC OK" >> "$E/link-tests/javac.log"; put_state JAVAC PASS
    else
        echo "JAVAC FAILED" >> "$E/link-tests/javac.log"; put_state JAVAC FAIL; fail=1
    fi
    cat "$E/link-tests/javac.log"

    note "licences and notices"
    cp "$SRC_A/LICENSE" "$B/licenses/llama.cpp-LICENSE.txt"
    [ -f "$SRC_A/AUTHORS" ] && cp "$SRC_A/AUTHORS" "$B/licenses/llama.cpp-AUTHORS.txt"
    [ -f "$NDK/NOTICE" ] && cp "$NDK/NOTICE" "$B/licenses/android-ndk-NOTICE.txt"
    [ -f "$NDK/NOTICE.toolchain" ] && cp "$NDK/NOTICE.toolchain" "$B/licenses/android-ndk-NOTICE.toolchain.txt"
    python3 -I "$HERE/trace_sources.py" \
        --compile-commands "$BUNDLE/logs/build-a/compile_commands.json" \
        --ninja-commands "$BUNDLE/logs/build-a/ninja-commands.txt" \
        --ninja-deps "$BUNDLE/logs/build-a/ninja-deps.txt" --src "$SRC_A" \
        --out-sources "$E/compiled-sources.txt" --out-licences "$E/licence-scan.txt" \
        --out-json "$E/compiled-sources.json"

    note "build information"
    mkdir -p "$B/build-recipe"
    cp -r "$REPO_ROOT/scripts" "$REPO_ROOT/rules" "$REPO_ROOT/selftest" "$B/build-recipe/"
    mkdir -p "$B/build-recipe/.github/workflows"
    cp "$REPO_ROOT/.github/workflows/"*.yml "$B/build-recipe/.github/workflows/"
    [ -f "$REPO_ROOT/README.md" ] && cp "$REPO_ROOT/README.md" "$B/build-recipe/README.md"
    python3 -I - "$B" "$STATE" "$HERE/engine.conf" <<'PY'
import hashlib, json, os, re, subprocess, sys
B, state_file, conf = sys.argv[1:4]
def kv(path):
    # engine.conf holds only NAME="value" lines (optionally followed by a comment)
    d = {}
    for line in open(path):
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"', line.strip())
        if m:
            d[m.group(1)] = m.group(2)
    return d
pins = kv(conf)
state = {}
for line in open(state_file):
    if "=" in line:
        k, v = line.rstrip("\n").split("=", 1)
        state[k] = subprocess.run(["bash", "-c", "printf %s " + v], capture_output=True, text=True).stdout
def cache(path, keys):
    out = {}
    for line in open(path):
        m = re.match(r'^([A-Za-z0-9_]+):[A-Z]+=(.*)$', line.rstrip("\n"))
        if m and m.group(1) in keys:
            out[m.group(1)] = m.group(2)
    return out
keys = {"CMAKE_C_FLAGS","CMAKE_CXX_FLAGS","CMAKE_C_FLAGS_RELEASE","CMAKE_CXX_FLAGS_RELEASE",
        "CMAKE_SHARED_LINKER_FLAGS","CMAKE_SHARED_LINKER_FLAGS_RELEASE","CMAKE_BUILD_TYPE","ANDROID_ABI",
        "ANDROID_PLATFORM","ANDROID_STL","CMAKE_C_COMPILER","CMAKE_CXX_COMPILER","CMAKE_ANDROID_NDK",
        "GGML_CPU_ARM_ARCH","GGML_NATIVE","GGML_BACKEND_DL","GGML_OPENMP","GGML_LLAMAFILE","GGML_CPU_KLEIDIAI",
        "GGML_CPU_REPACK","BUILD_SHARED_LIBS","LLAMA_BUILD_IS_DEV","CMAKE_SKIP_RPATH"}
cm = cache(os.path.join(B, "logs/build-a/CMakeCache.txt"), keys)
def sha(p):
    h = hashlib.sha256(); h.update(open(p, "rb").read()); return h.hexdigest()
libs = []
for lib in pins["ENGINE_LIBS"].split():
    s = os.path.join(B, "jniLibs", pins["ANDROID_ABI"], lib)
    d = os.path.join(B, "debug", pins["ANDROID_ABI"], lib)
    libs.append({"file": lib, "stripped_bytes": os.path.getsize(s), "stripped_sha256": sha(s),
                 "debug_bytes": os.path.getsize(d), "debug_sha256": sha(d)})
verify = json.load(open(os.path.join(B, "evidence/verify-engine.json")))
for entry in libs:
    for r in verify["libraries"]:
        if r["file"] == entry["file"]:
            entry.update(soname=r.get("soname"), needed=r.get("needed"), build_id=r.get("build_id"),
                         min_api_required=r.get("min_api_required"), android_ident_api=r.get("android_api_note"),
                         ndk_note=r.get("ndk_note"))
toolchain = open(os.path.join(B, "provenance/toolchain.txt")).read()
clang_line = next((l for l in toolchain.splitlines() if "clang version" in l), None)
env = {k: os.environ.get(k) for k in ("GITHUB_SERVER_URL","GITHUB_REPOSITORY","GITHUB_SHA","GITHUB_RUN_ID",
       "GITHUB_RUN_ATTEMPT","GITHUB_WORKFLOW_REF","ImageOS","ImageVersion","RUNNER_OS","RUNNER_ARCH")}
run_url = None
if env["GITHUB_SERVER_URL"] and env["GITHUB_REPOSITORY"] and env["GITHUB_RUN_ID"]:
    run_url = "%s/%s/actions/runs/%s" % (env["GITHUB_SERVER_URL"], env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"])
inputs = open(os.path.join(B, "logs/build-a/build-inputs.txt")).read().split("--- cmake arguments\n")
info = {
  "package": pins["PACKAGE_ID"],
  "upstream": {"repository": pins["LLAMA_REPO_URL"], "tag": pins["LLAMA_TAG"], "tag_object": pins["LLAMA_TAG_OBJECT"],
               "commit": pins["LLAMA_COMMIT"], "nightly_alias": pins["LLAMA_NIGHTLY_ALIAS"]},
  "toolchain": {"android_ndk": pins["NDK_VERSION"], "clang": clang_line, "sdk_cmake": pins["SDK_CMAKE_VERSION"],
                "details": "provenance/toolchain.txt"},
  "target": {"abi": pins["ANDROID_ABI"], "android_platform": pins["ANDROID_API"], "stl": "c++_static (hidden)",
             "cpu_arch": pins["CPU_ARCH"], "page_size_link_flag": "-Wl,-z,max-page-size=16384"},
  "environment_flags": [l for l in inputs[0].splitlines() if l.startswith(("CFLAGS=","CXXFLAGS=","LDFLAGS=","SOURCE_DATE_EPOCH="))],
  "cmake_arguments": [a for a in inputs[1].splitlines() if a] if len(inputs) > 1 else [],
  "cmake_cache": cm,
  "libraries": libs,
  "results": {k: state.get(k) for k in ("REPRO","VERIFY_ENGINE","VERIFY_DEBUG","LINK_HEADERS","LINK_BRIDGE","VERIFY_BRIDGE","JAVAC")},
  "ci": dict(env, run_url=run_url),
  "device_execution": "NOT TESTED (no Android device in CI)",
}
json.dump(info, open(os.path.join(B, "BUILDINFO.json"), "w"), indent=2)
print(json.dumps(info["results"], indent=2))
PY
    [ "$fail" = 0 ] || die "one or more required checks failed (see evidence/)"
    note "finalize OK"
}

stage_seal() {
    mkdir -p "$BUNDLE/logs/stages"
    cp "$STAGE_LOGS/"*.log "$BUNDLE/logs/stages/" 2>/dev/null || true
    ( cd "$BUNDLE" && find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum ) > "$BUNDLE/SHA256SUMS.txt"
    note "sealed $(wc -l < "$BUNDLE/SHA256SUMS.txt") files"
}

case "${1:-}" in
    env) stage_env ;;
    fetch) stage_fetch ;;
    toolchain) stage_toolchain ;;
    build-a) stage_build a ;;
    build-b) stage_build b ;;
    compare) stage_compare ;;
    finalize) stage_finalize ;;
    seal) stage_seal ;;
    *) echo "usage: $0 env|fetch|toolchain|build-a|build-b|compare|finalize|seal" >&2; exit 2 ;;
esac
