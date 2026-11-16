#!/usr/bin/env bash
# build_engine.sh - build the four llama.cpp engine libraries for Android
# arm64-v8a with the official Android NDK. Used twice by ci_pipeline.sh (two
# clean builds in different directories) and usable on any Linux x86_64 host.
#
# usage: build_engine.sh SRC_DIR BUILD_DIR INSTALL_PREFIX LOG_DIR
# needs: ANDROID_NDK  = NDK root (Pkg.Revision must equal NDK_VERSION)
#        CMAKE_BIN    = directory holding cmake and ninja (Android SDK cmake package)
#        SRC_DIR      = a clean git checkout of llama.cpp at LLAMA_COMMIT
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=engine.conf
. "$HERE/engine.conf"

if [ "$#" -ne 4 ]; then
    echo "usage: $0 SRC_DIR BUILD_DIR INSTALL_PREFIX LOG_DIR" >&2
    exit 2
fi
: "${ANDROID_NDK:?ANDROID_NDK must point at the NDK root}"
: "${CMAKE_BIN:?CMAKE_BIN must point at the directory with cmake and ninja}"

SRC="$(realpath "$1")"
BUILD="$(realpath -m "$2")"
PREFIX="$(realpath -m "$3")"
LOGDIR="$(realpath -m "$4")"
mkdir -p "$BUILD" "$PREFIX" "$LOGDIR"

die() { echo "build_engine.sh: ERROR: $*" >&2; exit 1; }

# --- inputs must be exactly what engine.conf pins
ndk_rev="$(sed -n 's/^Pkg\.Revision *= *//p' "$ANDROID_NDK/source.properties" | tr -d '\r')"
[ "$ndk_rev" = "$NDK_VERSION" ] || die "NDK revision is '$ndk_rev', expected $NDK_VERSION"
[ "$(git -C "$SRC" rev-parse HEAD)" = "$LLAMA_COMMIT" ] || die "source is not at $LLAMA_COMMIT"
[ -z "$(git -C "$SRC" status --porcelain --untracked-files=all)" ] || die "source tree is not clean"
[ ! -e "$BUILD/CMakeCache.txt" ] || die "build directory is not empty: $BUILD"

# --- reproducibility: no absolute build paths in the output, fixed timestamp
SOURCE_DATE_EPOCH="$(git -C "$SRC" log -1 --format=%ct)"
export SOURCE_DATE_EPOCH
# CFLAGS/CXXFLAGS/LDFLAGS from the environment are ADDED to the NDK's own
# default flags by CMake (passing CMAKE_*_FLAGS on the command line would
# replace them).
export CFLAGS="-ffile-prefix-map=${SRC}=/llama.cpp -ffile-prefix-map=${BUILD}=/build"
export CXXFLAGS="$CFLAGS"
# 16 KiB pages (also the NDK r28+ default), and keep the statically linked
# C++ runtime (libc++, libc++abi, libunwind) out of the exported symbols.
export LDFLAGS="-Wl,-z,max-page-size=16384 -Wl,--exclude-libs,libc++_static.a -Wl,--exclude-libs,libc++abi.a -Wl,--exclude-libs,libunwind.a"

CMAKE_ARGS=(
    -G Ninja
    "-DCMAKE_MAKE_PROGRAM=${CMAKE_BIN}/ninja"
    "-DCMAKE_TOOLCHAIN_FILE=${ANDROID_NDK}/build/cmake/android.toolchain.cmake"
    "-DANDROID_ABI=${ANDROID_ABI}"
    "-DANDROID_PLATFORM=android-${ANDROID_API}"
    -DANDROID_STL=c++_static
    -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    -DCMAKE_SKIP_RPATH=ON
    -DBUILD_SHARED_LIBS=ON
    -DLLAMA_BUILD_IS_DEV=OFF
    -DLLAMA_BUILD_COMMON=OFF
    -DLLAMA_BUILD_TESTS=OFF
    -DLLAMA_BUILD_TOOLS=OFF
    -DLLAMA_BUILD_EXAMPLES=OFF
    -DLLAMA_BUILD_SERVER=OFF
    -DLLAMA_BUILD_APP=OFF
    -DLLAMA_BUILD_UI=OFF
    -DLLAMA_USE_PREBUILT_UI=OFF
    -DLLAMA_BUILD_MTMD=OFF
    -DLLAMA_CONNECT=OFF
    -DLLAMA_OPENSSL=OFF
    -DLLAMA_LLGUIDANCE=OFF
    -DLLAMA_FATAL_WARNINGS=OFF
    -DGGML_NATIVE=OFF
    -DGGML_CPU=ON
    "-DGGML_CPU_ARM_ARCH=${CPU_ARCH}"
    -DGGML_BACKEND_DL=OFF
    -DGGML_CPU_ALL_VARIANTS=OFF
    -DGGML_CPU_REPACK=ON
    -DGGML_CPU_KLEIDIAI=OFF
    -DGGML_OPENMP=OFF
    -DGGML_LLAMAFILE=OFF
    -DGGML_CCACHE=OFF
    -DGGML_RPC=OFF
    -DGGML_VULKAN=OFF
    -DGGML_OPENCL=OFF
    -DGGML_HEXAGON=OFF
)

{
    echo "SRC=$SRC"
    echo "BUILD=$BUILD"
    echo "PREFIX=$PREFIX"
    echo "ANDROID_NDK=$ANDROID_NDK"
    echo "CMAKE_BIN=$CMAKE_BIN"
    echo "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
    echo "CFLAGS=$CFLAGS"
    echo "CXXFLAGS=$CXXFLAGS"
    echo "LDFLAGS=$LDFLAGS"
    echo "--- cmake arguments"
    printf '%s\n' "${CMAKE_ARGS[@]}"
} > "$LOGDIR/build-inputs.txt"

echo "=== configure"
"$CMAKE_BIN/cmake" -S "$SRC" -B "$BUILD" "${CMAKE_ARGS[@]}" 2>&1 | tee "$LOGDIR/configure.log"
echo "=== build"
"$CMAKE_BIN/cmake" --build "$BUILD" --config Release --verbose -j "$(nproc)" 2>&1 | tee "$LOGDIR/build.log"
echo "=== install"
"$CMAKE_BIN/cmake" --install "$BUILD" --prefix "$PREFIX" 2>&1 | tee "$LOGDIR/install.log"

"$CMAKE_BIN/ninja" -C "$BUILD" -t commands > "$LOGDIR/ninja-commands.txt"
"$CMAKE_BIN/ninja" -C "$BUILD" -t deps > "$LOGDIR/ninja-deps.txt"
cp "$BUILD/compile_commands.json" "$LOGDIR/compile_commands.json"
cp "$BUILD/CMakeCache.txt" "$LOGDIR/CMakeCache.txt"

# --- exactly the four expected libraries, regular files, no versioned symlinks
mapfile -t found < <(find "$PREFIX" \( -type f -o -type l \) -name '*.so*' -printf '%f\n' | sort)
mapfile -t expected < <(printf '%s\n' $ENGINE_LIBS | sort)
if [ "${found[*]}" != "${expected[*]}" ]; then
    die "installed libraries are [${found[*]}], expected [${expected[*]}]"
fi
if [ -n "$(find "$PREFIX" -type l -name '*.so*')" ]; then
    die "installed libraries include symlinks"
fi
libdir="$(dirname "$(find "$PREFIX" -type f -name libllama.so | head -1)")"
echo "$libdir" > "$LOGDIR/libdir.txt"
( cd "$libdir" && sha256sum $ENGINE_LIBS ) | tee "$LOGDIR/libs.sha256"
echo "build_engine.sh: OK ($libdir)"
