# Project: oversolved-occt

A build repo: it turns one pinned upstream OCCT release into `occt.js` +
`occt.wasm` for Oversolved. README.md says what it builds and how; this file
says how to work on it.

## Layout
- `occt.pin.json` - the upstream source: release, tag, commit, archive, sha256
- `bindings/classes.txt` - the OCCT classes and enums to bind
- `bindings/manual.cpp` - hand bindings for template instantiations
- `bindings/skip.txt` - optional; members that fail to compile, by JS name
- `scripts/pin.py` - fetch and verify the pinned source, or re-pin
- `scripts/bindgen.py` - classes.txt + clang JSON AST -> embind source
- `scripts/manifest.py`, `scripts/smoke.mjs` - describe and check the artifact
- `justfile` - every step; CI calls the same recipes

## Commands
```bash
just build   # toolchain, fetch, occt, bindings, link -> tmp/dist
just smoke   # node loads the artifact and computes with it
just repin <tag>
```
A bindings-only change needs `just bindings link smoke`, not the 35 minute OCCT
build.

## Rules
- The upstream release's source archive is the only OCCT input. Never vendor,
  patch or edit OCCT source; steer the build with CMake options and compiler
  flags in the justfile, with a comment saying why.
- Every pin is exact: the OCCT archive by sha256, emsdk by version. A pin
  changes only by `just repin` or an edit to `emsdk_version`, never as a side
  effect.
- Generated files (`tmp/build/bindings.cpp`) are never edited. Fix the
  generator, `classes.txt` or `manual.cpp`.
- Prefer a generator rule over a `skip.txt` line; `skip.txt` is for one-off
  members, with a comment saying why.
- Everything generated lives in `tmp/` (gitignored). No system-wide `/tmp`.
- Python is stdlib only, so the host needs nothing beyond python3.
- Agents do not commit, tag or push unless the user asks.

## Style
Same conventions as Oversolved:
- no em or en dashes
- comments carry intent, not a restatement of the code
- two spaces before an inline comment
- the only divider is one line `# ─── {text} ───`
- commit messages start as a normal sentence, a few lines at most
