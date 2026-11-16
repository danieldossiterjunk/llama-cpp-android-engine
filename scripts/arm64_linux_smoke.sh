#!/usr/bin/env bash
# arm64_linux_smoke.sh - supporting evidence only, NOT an Android test.
#
# Builds the same llama.cpp commit with the same ggml CPU baseline
# (GGML_CPU_ARM_ARCH from engine.conf, OpenMP and llamafile off) natively on an
# ARM64 Linux machine, then greedy-generates from the tiny public test model
# that upstream's own CI uses. It shows that this source + CPU configuration
# runs real inference on an ARM64 CPU. It says nothing about Android, bionic,
# APK packaging or the Galaxy S25.
#
# usage: arm64_linux_smoke.sh OUT_DIR
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=engine.conf
. "$HERE/engine.conf"
OUT="$(realpath -m "${1:?usage: $0 OUT_DIR}")"
mkdir -p "$OUT"
cd "$OUT"

[ "$(uname -m)" = "aarch64" ] || { echo "not an ARM64 machine: $(uname -m)"; exit 1; }
if ! command -v cmake > /dev/null || ! command -v gcc > /dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y -qq cmake build-essential
fi
{
    echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    uname -a
    grep -m1 -E 'model name|CPU part' /proc/cpuinfo || true
    grep -m1 Features /proc/cpuinfo || true
    lscpu 2>/dev/null || true
    gcc --version | sed -n '1p'
    cmake --version | sed -n '1p'
} > machine.txt

git ls-remote "$LLAMA_REPO_URL" "refs/tags/$LLAMA_TAG" "refs/tags/$LLAMA_TAG^{}" > ls-remote.txt
[ "$(awk -v r="refs/tags/$LLAMA_TAG^{}" '$2==r {print $1}' ls-remote.txt)" = "$LLAMA_COMMIT" ] || { echo "tag mismatch"; exit 1; }
rm -rf llama.cpp
git clone --depth 1 --branch "$LLAMA_TAG" "$LLAMA_REPO_URL" llama.cpp
[ "$(git -C llama.cpp rev-parse HEAD)" = "$LLAMA_COMMIT" ] || { echo "commit mismatch"; exit 1; }

cmake -S llama.cpp -B build \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
    -DGGML_NATIVE=OFF "-DGGML_CPU_ARM_ARCH=${CPU_ARCH}" -DGGML_BACKEND_DL=OFF \
    -DGGML_OPENMP=OFF -DGGML_LLAMAFILE=OFF -DGGML_CPU_KLEIDIAI=OFF -DGGML_CCACHE=OFF \
    -DLLAMA_BUILD_COMMON=ON -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_TOOLS=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_APP=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_IS_DEV=OFF \
    > configure.log 2>&1
cmake --build build --target llama-simple -j "$(nproc)" > build.log 2>&1

curl -fsSL --retry 3 -o stories15M.gguf "$SMOKE_MODEL_URL"
sha256sum stories15M.gguf > model.txt
echo "url: $SMOKE_MODEL_URL" >> model.txt
ls -l stories15M.gguf >> model.txt

./build/bin/llama-simple -m stories15M.gguf -n 48 "Once upon a time" > generation.log 2>&1
cat generation.log
grep -q "decoded" generation.log || { echo "SMOKE: FAILED (no decode report)"; exit 1; }
echo "SMOKE: OK - llama.cpp $LLAMA_TAG generated text on Linux ARM64 with GGML_CPU_ARM_ARCH=$CPU_ARCH" | tee result.txt
