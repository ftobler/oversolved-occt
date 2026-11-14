# oversolved-occt

[Open CASCADE Technology](https://github.com/Open-Cascade-SAS/OCCT) compiled to
a single WebAssembly module with JavaScript bindings, built for
[Oversolved](https://github.com/ftobler/oversolved). The repo holds no OCCT
source: it pins one upstream release, and its own sources are a symbol list, a
binding generator and the build recipe.

## Source of truth

The OCCT source is **the source archive of an upstream GitHub release**, named
in [`occt.pin.json`](occt.pin.json):

| field     | meaning                                                         |
|-----------|-----------------------------------------------------------------|
| `release` | the upstream release page, the human-readable source of truth   |
| `tag`     | its git tag                                                     |
| `commit`  | the commit that tag points at                                   |
| `archive` | the tag's source archive (`archive/refs/tags/<tag>.tar.gz`)     |
| `sha256`  | the archive's digest; a fetch that does not match it fails      |
| `root`    | the directory the archive unpacks to                            |

Only the source archive is used. The binaries attached to the upstream release
are Windows builds and play no part here.

## Build

Needs `git`, `python3`, `cmake`, `ninja`, `node` and [`just`](https://github.com/casey/just).
Everything else (the pinned emsdk, the source, the build trees) lands in `tmp/`.

```bash
just build   # toolchain, fetch, occt, bindings, link -> tmp/dist
just smoke   # load tmp/dist in node: a boolean, a STEP round trip, a throw
just clean   # drop tmp/
```

The OCCT build takes about 35 minutes on 12 cores. It builds the
FoundationClasses, ModelingData and ModelingAlgorithms modules plus the STEP
and STL toolkits and what they depend on. Draw, the viewer and OCAF are left out.

`tmp/dist` then holds:

- `occt.js`: an ES module whose default export instantiates the module (`await occt()`)
- `occt.wasm`
- `LICENSE_LGPL_21.txt`, `OCCT_LGPL_EXCEPTION.txt`: OCCT's license, copied from the source archive
- `manifest.json`: the pin, the emsdk version, this repo's build commit and the sha256 of each file

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
  that name in header order
- static-only namespaces (OCCT 8 turned `TopoDS` and others into namespaces)
  bind as class functions: `oc.TopoDS.Edge(shape)`
- `Handle_X` binds `opencascade::handle<X>`, built empty (`_1`) or from a pointer (`_2`)

The generator skips any member it cannot express, such as primitive
out-parameters, streams or deprecated API, and lists each one at the end of the
generated `tmp/build/bindings.cpp`. A member that fails to compile can be
dropped by its JS name in `bindings/skip.txt` (`Class::Method_N`, one per line).
Template instantiations (the NCollection containers, `BRepLProp_SLProps`) are
bound by hand in [`bindings/manual.cpp`](bindings/manual.cpp).

## Release

CI builds and smoke-tests every push to `main` or `dev` and every pull request.
Only `main` releases. A tag `occt-<occt version>-r<n>`, for example
`occt-8.0.1-r1`, on a commit that is on `main` publishes everything in
`tmp/dist` as a GitHub release of that name; a tag elsewhere fails the release
step. `n` counts builds of the same OCCT version: bump it for a
binding or flag change, and reset it to 1 on a re-pin.

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
