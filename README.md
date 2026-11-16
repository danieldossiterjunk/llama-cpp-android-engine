# llama-cpp-android-engine

Build recipe for **LLAMA_CPP_ANDROID_ARM64_ENGINE_001**: genuine llama.cpp
inference libraries for Android `arm64-v8a`, built from the official
[ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) source with the
official Android NDK, and verified against the `native-prebuilt` rules of
APK Builder 004 (v0.4.0).

This repository holds only the recipe. The libraries are produced by the
GitHub Actions workflow and published as a workflow artifact (with signed
build provenance when the repository is public).

## What is pinned (scripts/engine.conf)

| Input | Value |
|---|---|
| Source | `ggml-org/llama.cpp` tag `v1.0.0` (annotated tag object `a6d1a74d...`), commit `b1b770d90d94cad9e9ea5b993bdbc383a522fb28` |
| NDK | `29.0.14206865` (r29), installed by Google's `sdkmanager` |
| CMake / Ninja | Android SDK package `cmake;3.31.6` |
| Target | `arm64-v8a`, `android-28`, `c++_static` (C++ runtime hidden) |
| CPU backend | `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16`, `GGML_NATIVE=OFF`, `GGML_BACKEND_DL=OFF`, OpenMP / llamafile / KleidiAI off |
| Pages | `-Wl,-z,max-page-size=16384` (16 KiB) |
| Flags | passed as `-DCMAKE_C_FLAGS` / `-DCMAKE_CXX_FLAGS` / `-DCMAKE_SHARED_LINKER_FLAGS` with `ANDROID_USE_LEGACY_TOOLCHAIN_FILE=ON` (NDK r29's toolchain ignores `CFLAGS`/`LDFLAGS`); the build fails unless the prefix maps, `--exclude-libs,ALL` and `max-page-size=16384` appear on the real commands |
| Outputs | `libllama.so`, `libggml.so`, `libggml-base.so`, `libggml-cpu.so` |

## What the workflow does

1. Records the runner.
2. Checks with `git ls-remote` that the official `v1.0.0` tag object and commit
   are exactly the pinned ones, that the commit is on `master`, and makes two
   clean checkouts.
3. Installs NDK r29, SDK CMake 3.31.6 and the `android-29` platform with
   `sdkmanager` (this accepts the Android SDK licence, as upstream's CI does),
   and records versions and SHA-256 hashes of the toolchain files.
4. Builds twice in different directories and compares the results byte for byte.
5. Strips the libraries, copies the matching public headers, and verifies every
   library with `scripts/verify_elf.py`: ELF64 AArch64 shared object, SONAME =
   file name, plain DT_NEEDED, every PT_LOAD aligned to 16 KiB with no shared
   pages and a RELRO end that does not share a page with writable data, no text
   relocations, no W+X segment, section headers consistent with the dynamic
   segment, a symbol hash table, the Android API level (ident note and bionic
   symbol versions), undefined symbols resolved against the NDK API 28 system
   stubs, no exported C++ runtime, and every function the headers declare
   exported. A function that upstream declares but never defines anywhere in
   its source (v1.0.0 has one: `ggml_threadpool_get_n_threads`) is reported
   separately as an upstream header defect.
6. Link tests with NDK clang at API 26, the level APK Builder 004 compiles a
   bridge for: one file that references every header-declared function that
   upstream defines, and
   the self-test JNI bridge (`selftest/jni/llamabridge.c`, C17,
   `-O2 -fPIC -fstack-protector-strong -Wall -Wextra -Werror`).
7. Compiles the self-test Java sources at Java 8 level against `android-29`
   (with build-tools' `core-lambda-stubs.jar`, as Android's own build does).
8. Traces which source files went into each library, scans them for licence
   markers, and copies the llama.cpp and NDK licence/notice files.
9. Writes `BUILDINFO.json` and `SHA256SUMS.txt`, uploads the bundle as an
   artifact, and also pushes it as one tarball to branch
   `ci-output/run-<id>-<attempt>` of this repository (the verification
   workspace can fetch git but not GitHub's artifact storage).
10. Separate job (public repositories only): builds the same commit and CPU
    flags natively on GitHub's ARM64 Linux runner and greedy-generates from a
    tiny test model. That is supporting evidence about the source and CPU
    configuration, **not** an Android or Galaxy S25 test.

## Running it elsewhere

On any Linux x86_64 machine with an Android SDK (`ANDROID_HOME`, with
`cmdline-tools/latest/bin/sdkmanager`), Java 17, Python 3 and git:

```
for s in env fetch toolchain build-a build-b compare finalize seal; do
    bash scripts/ci_pipeline.sh "$s" || break
done
```

The bundle is written to `$ENGINE_WORK/bundle/` (default `/tmp/llama-engine`).

## Status

No Android device runs in CI. Device execution is **NOT TESTED** until the
owner runs the self-test app on the phone.

## Licences

llama.cpp and ggml: MIT (the ggml authors). The libraries statically contain
the NDK's LLVM libc++, libc++abi and libunwind (Apache-2.0 WITH LLVM-exception)
and bionic CRT objects; the NDK notice files are copied into the bundle.
