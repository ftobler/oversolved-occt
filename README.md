# oversolved-occt

[Open CASCADE Technology](https://github.com/Open-Cascade-SAS/OCCT) compiled to
a single WebAssembly module with JavaScript bindings, built for
[Oversolved](https://github.com/ftobler/oversolved). The repo holds no OCCT
source: it pins one upstream release, and its own sources are a symbol list, a
binding generator and the build recipe.

## Source of truth

The OCCT source is **the source archive of an upstream GitHub release**, named
in [`occt.pin.json`](occt.pin.json):

| field     | meaning                                                              |
|-----------|----------------------------------------------------------------------|
| `release` | the upstream release page, the human-readable source of truth        |
| `tag`     | its git tag                                                          |
| `commit`  | the commit that tag points at; the archive must have been built from it |
| `archive` | the tag's source archive (`archive/refs/tags/<tag>.tar.gz`)          |
| `sha256`  | the digest of the archive's uncompressed tar; a mismatch fails the fetch |
| `root`    | the directory the archive unpacks to                                 |

The tar is hashed, not the `.tar.gz`, because GitHub does not promise
byte-stable compression. A fetch also reads the commit id GitHub writes into
the archive and compares it with `commit`. Only the source archive is used: the
binaries attached to the upstream release are Windows builds and play no part
here.

## Build

Needs `git`, `python3` (3.12 or newer), `cmake`, `ninja`, `node` and
[`just`](https://github.com/casey/just). Everything else (the pinned emsdk, the
source, the build trees) lands in `tmp/`. A bare `just` lists the recipes.

```bash
just build   # toolchain, fetch, occt, bindings, link -> tmp/dist
just smoke   # load tmp/dist in node and exercise it
just clean   # drop tmp/
```

`just occt` is the long step, tens of minutes even on a fast many-core machine.
A change to the bindings only needs `just bindings link smoke`. The build
covers the FoundationClasses, ModelingData and ModelingAlgorithms modules plus
the STEP and STL toolkits and what they depend on. Draw, the viewer and OCAF are
left out.

`tmp/dist` then holds:

- `occt.js`: an ES module whose default export instantiates the module (`await occt()`)
- `occt.wasm`
- `LICENSE_LGPL_21.txt`, `OCCT_LGPL_EXCEPTION.txt`: OCCT's license, copied from the source archive
- `manifest.json`: the pin, the `emcc` version that built it, this repo's build commit and the sha256 of each file

`just smoke` runs a boolean, a STEP round trip through the in-memory
filesystem, the STEP transfer map's per-face ids, and a C++ exception crossing
into JS. It also calls every registered member with no arguments: a member
whose signature names an unbound type throws "unbound types" at that call,
before any argument check, so the test fails by name instead of leaving the
problem for the first caller.

### Stack

The link sets `-sSTACK_SIZE=5MB`. Emscripten's default is 64 KB, which deep
modeling calls overflow: a fillet over two fused cylinders overruns it, and a
wasm stack overflow traps as "memory access out of bounds" rather than raising
something the caller can catch. The stack comes out of the module's initial
memory; `ALLOW_MEMORY_GROWTH` covers the heap beyond it.

### Exceptions

C++ exceptions are WebAssembly-native (`-fwasm-exceptions`). A throw reaches JS
as a `WebAssembly.Exception`; `oc.getExceptionMessage(e)` returns
`[type, what()]` and `oc.decrementExceptionRefcount(e)` frees it. OCCT's own
range and null checks stay compiled in (see `occt_checks` in the justfile), so
an out-of-range index throws `Standard_OutOfRange` rather than reading garbage.

## Bindings

[`bindings/classes.txt`](bindings/classes.txt) lists the OCCT classes and enums
Oversolved uses. [`scripts/bindgen.py`](scripts/bindgen.py) reads each one from
clang's JSON AST (dumped by the same `em++` that compiles the result) and emits
embind for its public members and its public ancestors. The naming follows
opencascade.js, which the Oversolved kernel was first written against:

- one public constructor binds on the class itself, `new oc.BRepAlgoAPI_Defeaturing()`
- several public constructors become one class each, `new oc.gp_Pnt_3(x, y, z)`
- an overloaded method `M` is bound as `M_1`, `M_2`, ...
- `N` is the 1-based position among the public, non-deleted declarations of
  that name in header order, deprecated ones included; a member that is not
  bound leaves a gap, never a shift
- static-only namespaces (OCCT 8 turned `TopoDS` and others into namespaces)
  bind as class functions, numbered like any overload: `oc.TopoDS.Edge_1(shape)`.
  Embind has no reference returns, so a non-const overload hands back a copy
- OCCT has no default arguments in embind: pass every argument, for example
  `BRep_Tool.Triangulation(face, loc, 0)`
- `Handle_X` binds `opencascade::handle<X>`, built empty (`_1`) or from a
  pointer (`_2`), and is registered for every class a bound member takes or
  returns a handle to

A member whose signature names a type that is not bound would register but
throw when called, so the generator skips it instead. That covers a class
missing from `classes.txt`, a handle to one, a nested type, primitive
out-parameters, streams and rvalues. Deprecated members are skipped too. Every
skip is listed with its reason at the end of the generated
`tmp/build/bindings.cpp`, followed by the unbound types ranked by how many
members need them: add a class to `classes.txt` to bind its members.

`bindings/skip.txt` drops further members by JS name, one per line, with an
optional `# reason`: `Class::Method_N` for a method, `Class_N` for constructor
N of a class that has several, `Class` for a class's only constructor. A line
that matches nothing is an error. Template instantiations (the NCollection
containers, `BRepLProp_SLProps`) are bound by hand in
[`bindings/manual.cpp`](bindings/manual.cpp), and
[`bindings/manual.txt`](bindings/manual.txt) lists the spellings it covers so
the generator counts them as bound.

A few members cannot come from the AST at all: OCCT 8 no longer declares
`TopoDS_Shape::HashCode` (7.8 replaced it with `std::hash`), and the STEP
transfer map and its model are handed back as their base classes, so the JS
base type lacks the subclass members the kernel reads
(`TransferBRep_BinderOfShape::Result`, `StepData_StepModel::IdentLabel`).
Embind allows one registration per class, so those are implemented in
`manual.cpp` and declared in [`bindings/manual.h`](bindings/manual.h);
`MANUAL_MEMBERS` in `scripts/bindgen.py` registers each on the generated class.
`HashCode` keeps the 7.7 contract: a value in `[1, upperBound]` computed from
the TShape and the location.

## Release

CI builds and smoke-tests every push to `main` or `dev` and every pull request.
Only `main` releases. A tag `occt-<occt version>-r<n>`, for example
`occt-8.0.1-r1`, publishes everything in `tmp/dist` as a GitHub release of that
name. Before building anything, the tag run checks that the tagged commit is on
`main` and that the version in the tag is the pinned one, and fails fast
otherwise. `n` counts builds of the same OCCT version: bump it for a binding or
flag change, and reset it to 1 on a re-pin.

The OCCT build is cached, keyed on the pin and every flag of the `occt` recipe,
and saved as soon as it succeeds, so a failure further on keeps it. A change to
the bindings or the link step reuses it.

## Re-pinning (chore)

```bash
just repin V8.0.2   # rewrite occt.pin.json from the upstream tag
just clean build smoke
```

Commit the new pin, then tag `occt-8.0.2-r1`. A new OCCT version can move
overload numbers and drop classes, so the consumer re-checks its binding
surface against the new release before switching to it.

## License

OCCT is licensed under the LGPL 2.1 with the Open CASCADE exception. This repo's
own scripts are under the same terms as Oversolved.
