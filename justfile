# justfile
#
# Build OCCT to a single WASM module with JS bindings for Oversolved.
#
#   build     build chains toolchain, fetch, occt, bindings and link into tmp/dist;
#             each of the five also runs on its own
#   smoke     load tmp/dist in node and exercise it
#   repin     point occt.pin.json at another upstream tag
#   clean     drop tmp/
#
# Everything lands in tmp/: the emsdk, the unpacked source, the build trees and
# the artifact. The host needs git, python3 (3.12 or newer), cmake, ninja and
# node; CI runs these same recipes on a stock runner. A bare `just` lists them.

set shell := ["bash", "-cuo", "pipefail"]

# Listing is the safe default: a bare `just` must not start a long build.
default:
    @just --list

# The toolchain pin. Bumping it is a rebuild of everything, like a re-pin.
emsdk_version := "6.0.13"

root := justfile_directory()
emsdk := root / "tmp" / "emsdk"
occt_src := root / "tmp" / "src" / `python3 -c "import json; print(json.load(open('occt.pin.json'))['root'])"`
occt_build := root / "tmp" / "build" / "occt"
occt_install := occt_build / "install"
occt_inc := occt_install / "include" / "opencascade"
dist := root / "tmp" / "dist"

# Each emscripten step sources the pinned emsdk's environment first.
em := "source " + emsdk / "emsdk_env.sh" + " > /dev/null 2>&1 &&"

# Exceptions are WASM-native: OCCT signals failure by throwing Standard_Failure,
# and the kernel turns those into named errors, so they must cross into JS.
cxxflags := "-fwasm-exceptions -O3"

# OCCT's CMake defines OCC_CONVERT_SIGNALS on every non-MSVC build, which turns
# OCC_CATCH_SIGNALS into setjmp. Combined with WASM exceptions that emits
# invalid code (br_table arity, ShapeUpgrade_ShapeDivide::Perform, emsdk 4.0.10
# and 6.0.13 alike), and a browser raises no signals to convert anyway. The -U
# lands after CMake's -D on the command line, so it wins without a source patch.
occt_cxxflags := cxxflags + " -UOCC_CONVERT_SIGNALS"

# A Release build of OCCT defines No_Exception by default, which compiles out
# the range and null checks (Standard_OutOfRange, Standard_NoSuchObject). Header
# inline code such as NCollection_Array1::Value is also compiled into the
# bindings, which do not define it, and the linker keeps one copy, so the checks
# would depend on link order. The kernel indexes arrays from JS, where an
# unchecked index is silent memory corruption, so the checks stay on everywhere.
occt_checks := "-DBUILD_RELEASE_DISABLE_EXCEPTIONS=OFF"


# ─── build ───

# The full pipeline; the result is tmp/dist.
build: toolchain fetch occt bindings link

# Install the pinned emsdk into tmp/emsdk (no-op once that version is active).
toolchain:
    [ -d {{emsdk}} ] || git clone --quiet https://github.com/emscripten-core/emsdk.git {{emsdk}}
    git -C {{emsdk}} fetch --quiet --tags
    git -C {{emsdk}} checkout --quiet {{emsdk_version}}
    {{emsdk}}/emsdk install {{emsdk_version}}
    {{emsdk}}/emsdk activate {{emsdk_version}}
    {{em}} test "$(emcc --version | grep -cF ') {{emsdk_version}} (' || true)" = 1 \
        || { echo "emcc is not {{emsdk_version}}: emsdk keeps one shared upstream/, reinstall it"; exit 1; }

# Download the pinned source archive, verify its hash and commit, unpack it.
fetch:
    python3 scripts/pin.py fetch

# Only the modeling modules and the STEP and STL toolkits (with what they
# depend on) are built: no Draw, no viewer.

# Configure and build the static OCCT libraries.
occt:
    {{em}} emcmake cmake -S {{occt_src}} -B {{occt_build}} -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        {{occt_checks}} \
        -DCMAKE_CXX_FLAGS="{{occt_cxxflags}}" \
        -DBUILD_LIBRARY_TYPE=Static \
        -DINSTALL_DIR={{occt_install}} \
        -DBUILD_MODULE_FoundationClasses=ON \
        -DBUILD_MODULE_ModelingData=ON \
        -DBUILD_MODULE_ModelingAlgorithms=ON \
        -DBUILD_MODULE_Visualization=OFF \
        -DBUILD_MODULE_ApplicationFramework=OFF \
        -DBUILD_MODULE_DataExchange=OFF \
        -DBUILD_MODULE_Draw=OFF \
        -DBUILD_ADDITIONAL_TOOLKITS="TKDESTEP TKDESTL" \
        -DUSE_FREETYPE=OFF -DUSE_TK=OFF -DUSE_OPENGL=OFF -DUSE_GLES2=OFF \
        -DBUILD_DOC_Overview=OFF -DBUILD_GTEST=OFF
    {{em}} cmake --build {{occt_build}} --target install

# Generate the embind source from bindings/classes.txt against the built headers.
bindings:
    {{em}} python3 scripts/bindgen.py --include {{occt_inc}} --out tmp/build/bindings.cpp

# A C++ throw reaches JS as a WebAssembly.Exception; the exception helpers give
# the consumer getExceptionMessage(e) -> [type, what()] to name it, and
# decrementExceptionRefcount(e) to free it once handled.

# Emscripten's default stack is 64 KB, which deep modeling calls overflow: a
# fillet over two fused cylinders overruns it, and a wasm stack overflow traps
# as "memory access out of bounds" instead of raising. 5 MB leaves room for the
# deepest OCCT path; -I bindings makes manual.h visible to the generated file.

# Compile the bindings and link them with the OCCT libraries into tmp/dist.
link:
    rm -rf {{dist}}
    mkdir -p {{dist}}
    {{em}} em++ {{cxxflags}} -std=c++17 -I{{occt_inc}} -I{{root}}/bindings \
        tmp/build/bindings.cpp bindings/manual.cpp \
        -Wl,--start-group {{occt_install}}/lib/*.a -Wl,--end-group \
        -lembind \
        -sSTACK_SIZE=5MB \
        -sMODULARIZE=1 -sEXPORT_ES6=1 -sEXPORT_NAME=occt \
        -sENVIRONMENT=web,worker,node \
        -sALLOW_MEMORY_GROWTH=1 -sMAXIMUM_MEMORY=4GB \
        -sEXPORTED_RUNTIME_METHODS=FS,getExceptionMessage,decrementExceptionRefcount \
        -o {{dist}}/occt.js
    cp {{occt_src}}/LICENSE_LGPL_21.txt {{occt_src}}/OCCT_LGPL_EXCEPTION.txt {{dist}}/
    {{em}} python3 scripts/manifest.py {{dist}} {{emsdk_version}}


# ─── check ───

# It runs a boolean, a STEP round trip and a throw, then probes that no
# registered member names an unbound type.

# Load tmp/dist in node and exercise it.
smoke:
    node scripts/smoke.mjs {{dist}}


# ─── chore ───

# Point occt.pin.json at another upstream release tag, e.g. `just repin V8.0.2`.
repin tag:
    python3 scripts/pin.py repin {{tag}}


# ─── cleanup ───

# Drop everything generated; the next build starts from the download.
clean:
    rm -rf tmp
