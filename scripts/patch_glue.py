"""Defer embind's invoker construction in the linked glue to each member's first call.

Usage: python3 scripts/patch_glue.py <occt.js>

While the module instantiates, embind builds one JS invoker per bound member and
constructor with `new Function`; most are never called. This rewrites
`craftInvokerFunction` in place so it returns a stub that builds the real
invoker on first call and keeps it. The real invoker still does the argument
count check, `this`, the return value and the destructors. The glue is
emscripten's link output, not OCCT source.

The anchor must match exactly once: after an emsdk bump that renamed or
reshaped the function, the link fails here instead of shipping unpatched glue.
"""

import re
import sys

# The minified definition the pinned emsdk emits; the parameter list is reused
# verbatim for the deferred call, so a changed signature still forwards.
ANCHOR = re.compile(r"function craftInvokerFunction\(([A-Za-z_$][\w$]*(?:,[A-Za-z_$][\w$]*)*)\)\{")
REAL = "craftInvokerFunctionNow"


def patch(text: str) -> str:
    found = ANCHOR.findall(text)
    if len(found) != 1:
        sys.exit(f"patch_glue: expected one craftInvokerFunction definition, found {len(found)}")
    if f"function {REAL}(" in text:
        sys.exit(f"patch_glue: {REAL} already present, the glue is patched or changed")
    params = found[0]
    stub = (f"function craftInvokerFunction({params}){{var made=null;"
            f"return function(){{return(made||(made={REAL}({params}))).apply(this,arguments)}}}}"
            f"function {REAL}({params}){{")
    return ANCHOR.sub(lambda _: stub, text, count=1)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip().splitlines()[2])
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        patched = patch(f.read())
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    print(f"patch_glue: {path}: craftInvokerFunction deferred to first call")


if __name__ == "__main__":
    main()
