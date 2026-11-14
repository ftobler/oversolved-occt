"""Generate the embind source for the classes listed in bindings/classes.txt.

Each listed name is looked up in clang's JSON AST of its own header, dumped by
the same em++ that later compiles the bindings, so the generator and the
compiler never disagree about a declaration. Public ancestors of a listed class
are bound as well, so inherited members (Shape(), IsDone()) resolve.

Naming follows opencascade.js, which the Oversolved kernel was written against:
  - a class with one public constructor binds it on the class itself;
  - with several, constructor N becomes its own JS class C_N deriving from C;
  - an overloaded method name M is bound as M_1, M_2, ...;
  - N is the 1-based position among the public, non-deleted declarations of
    that name in header order, so a skipped overload leaves a gap, never a shift.

Every member binds through a wrapper lambda with its parameter types spelled
out, which selects the overload and lets a C string travel as a JS string.
Members embind has no form for (primitive out-parameters, primitive pointers,
streams, rvalues) are skipped and listed at the end of the output, and
bindings/skip.txt drops members by JS name when they fail to compile.

Usage: bindgen.py --include <occt include dir> --out <bindings.cpp>
"""

import argparse
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLASSES = REPO / "bindings" / "classes.txt"
SKIP = REPO / "bindings" / "skip.txt"

PRIMITIVE = re.compile(
    r"^(bool|char|short|int|long|unsigned|signed|float|double|size_t|void|"
    r"Standard_(Boolean|Integer|Real|ShortReal|Character|Byte|Size|Address))\b"
)
C_STRING = re.compile(r"^(const )?(Standard_CString|(const )?char \*( ?const)?)$")
NOISE = {"DumpJson", "InitFromJson", "Dump", "DynamicType", "get_type_name",
         "get_type_descriptor"}


@dataclass
class Fn:
    name: str
    ret: str
    params: list[str]
    const: bool = False
    static: bool = False
    index: int = 0  # overload position, 0 when the name is not overloaded
    local: bool = False  # typed with a name nested in its class

    @property
    def js(self) -> str:
        return f"{self.name}_{self.index}" if self.index else self.name


@dataclass
class Record:
    name: str
    bases: list[str] = field(default_factory=list)
    abstract: bool = False
    hidden_ctor: bool = False
    hidden_dtor: bool = False  # embind cannot own it; descendants skip over it
    tag: bool = False  # a namespace, bound through the empty struct Bind_<name>
    ctors: list[Fn] = field(default_factory=list)
    methods: list[Fn] = field(default_factory=list)


def dump_ast(name: str, include: Path) -> list[dict]:
    """The top-level declarations named exactly `name`, from its own header."""
    cmd = ["em++", "-x", "c++", "-std=c++17", "-fsyntax-only", f"-I{include}",
           "-Xclang", "-ast-dump=json", "-Xclang", f"-ast-dump-filter={name}", "-"]
    text = subprocess.run(cmd, input=f"#include <{name}.hxx>\n", capture_output=True,
                          text=True, check=True).stdout
    decoder, pos, found = json.JSONDecoder(), 0, []
    while (pos := text.find("{", pos)) != -1:
        node, pos = decoder.raw_decode(text, pos)
        if node.get("name") == name:
            found.append(node)
    return found


def split_signature(qual: str) -> tuple[str, bool]:
    """Return type and constness of a function type such as `R (A, B) const`."""
    close = qual.rfind(")")
    depth, i = 0, close
    while i >= 0:
        depth += {")": 1, "(": -1}.get(qual[i], 0)
        if depth == 0:
            break
        i -= 1
    return qual[:i].strip(), "const" in qual[close:]


def unbindable_type(t: str) -> bool:
    """A type with no JS form, or one that only works as an out-parameter."""
    if C_STRING.match(t):
        return False
    if "&&" in t or "..." in t or "Stream" in t or "stream" in t or "*&" in t:
        return True
    if t.endswith("*") or (t.endswith("&") and not t.startswith("const ")):
        return bool(PRIMITIVE.match(t.removeprefix("const ").rstrip("&* ")))
    return False


def unbindable(fn: Fn) -> bool:
    return fn.local or any(unbindable_type(t) for t in [fn.ret, *fn.params])


NESTED = {"CXXRecordDecl", "TypedefDecl", "TypeAliasDecl", "EnumDecl"}


def deprecated(member: dict) -> bool:
    """Upstream marks what it is about to remove; binding it would only invite
    the kernel to depend on it."""
    return any(a["kind"] == "DeprecatedAttr" for a in member.get("inner", []))


def parse_record(node: dict) -> Record:
    rec = Record(node["name"])
    rec.abstract = node.get("definitionData", {}).get("isAbstract", False)
    rec.bases = [b["type"]["qualType"] for b in node.get("bases", [])
                 if b.get("access") == "public"]
    # A member typed with a name declared inside the class (a nested struct or
    # typedef) cannot be spelled at namespace scope, where the wrappers live.
    nested = {m["name"] for m in node.get("inner", [])
              if m["kind"] in NESTED and m.get("name") and m["name"] != rec.name}
    local = re.compile(r"\b(" + "|".join(map(re.escape, nested)) + r")\b") if nested else None
    access = "public" if node.get("tagUsed") == "struct" else "private"
    for member in node.get("inner", []):
        kind = member["kind"]
        if kind == "AccessSpecDecl":
            access = member["access"]
        if kind == "CXXConstructorDecl" and access != "public" and not member.get("isImplicit"):
            rec.hidden_ctor = True  # a protected-only constructor means no default one
        # The AST lists an implicit destructor after the last access label, but
        # an implicit destructor is always public.
        if kind == "CXXDestructorDecl" and access != "public" and not member.get("isImplicit"):
            rec.hidden_dtor = True
        if (kind not in ("CXXConstructorDecl", "CXXMethodDecl") or access != "public"
                or member.get("isImplicit") or member.get("explicitlyDeleted")
                or deprecated(member)):
            continue
        params = [p["type"]["qualType"] for p in member.get("inner", [])
                  if p["kind"] == "ParmVarDecl"]
        ret, const = split_signature(member["type"]["qualType"])
        fn = Fn(member["name"], ret, params, const, member.get("storageClass") == "static")
        fn.local = bool(local and any(local.search(t) for t in [ret, *params]))
        if kind == "CXXConstructorDecl":
            rec.ctors.append(fn)
        elif not fn.name.startswith("operator") and fn.name not in NOISE:
            rec.methods.append(fn)
    number_overloads(rec.ctors)
    number_overloads(rec.methods)
    return rec


def parse_namespace(name: str, nodes: list[dict]) -> Record:
    """OCCT 8 turned static-only classes (TopoDS, BRep_Tool) into namespaces.

    Their functions bind as class functions of an empty tag class, so JS keeps
    calling oc.TopoDS.Edge(shape). A namespace may be reopened, so every
    declaration of it contributes, in header order.
    """
    rec = Record(name, tag=True)
    for node in nodes:
        for member in node.get("inner", []):
            if member["kind"] != "FunctionDecl" or member["name"] in NOISE:
                continue
            params = [p["type"]["qualType"] for p in member.get("inner", [])
                      if p["kind"] == "ParmVarDecl"]
            ret, _ = split_signature(member["type"]["qualType"])
            rec.methods.append(Fn(member["name"], ret, params, static=True))
    number_overloads(rec.methods)
    return rec


def number_overloads(fns: list[Fn]) -> None:
    total: dict[str, int] = {}
    for fn in fns:
        total[fn.name] = total.get(fn.name, 0) + 1
    seen: dict[str, int] = {}
    for fn in fns:
        if total[fn.name] > 1:
            seen[fn.name] = fn.index = seen.get(fn.name, 0) + 1


def args_and_call(fn: Fn) -> tuple[list[str], list[str]]:
    """Wrapper parameters and the call arguments; a C string arrives as std::string."""
    args, call = [], []
    for i, t in enumerate(fn.params):
        if C_STRING.match(t):
            args.append(f"const std::string& a{i}")
            call.append(f"a{i}.c_str()")
        else:
            args.append(f"{t} a{i}")
            call.append(f"a{i}")
    return args, call


def bind_method(rec: str, fn: Fn) -> str:
    """One member as a typed lambda. allow_raw_pointers is passed throughout:
    OCCT hides pointers behind typedefs (BOPAlgo_PPaveFiller), so the spelled
    type does not tell, and the policy is inert where nothing is a pointer."""
    args, call = args_and_call(fn)
    if not fn.static:
        args.insert(0, f"{'const ' if fn.const else ''}{rec}& self")
    target = f"{rec}::{fn.name}" if fn.static else f"self.{fn.name}"
    ret = "std::string" if C_STRING.match(fn.ret) else fn.ret
    body = f"{target}({', '.join(call)})"
    body = f"{body};" if ret == "void" else f"return {body};"
    kind = "class_function" if fn.static else "function"
    return (f"    .{kind}(\"{fn.js}\", optional_override([]({', '.join(args)}) -> {ret} "
            f"{{ {body} }}), allow_raw_pointers())")


def bind_ctor(cls: str, fn: Fn) -> str:
    args, call = args_and_call(fn)
    return (f".constructor(optional_override([]({', '.join(args)}) "
            f"{{ return new {cls}({', '.join(call)}); }}), allow_raw_pointers())")


def bound_base(name: str, records: dict[str, Record]) -> str | None:
    """The nearest registered ancestor, through any that cannot be registered."""
    for base in records[name].bases if name in records else []:
        if base in records and not records[base].hidden_dtor:
            return base
        if found := bound_base(base, records):
            return found
    return None


def emit_record(rec: Record, records: dict[str, Record], skip: set[str],
                skipped: list[str]) -> tuple[list[str], str]:
    """The overload subclasses and the class_ registrations of one record."""
    structs: list[str] = []
    if rec.tag:
        structs.append(f"struct Bind_{rec.name} {{}};")
        lines = [f"class_<Bind_{rec.name}>(\"{rec.name}\")"]
    else:
        base = bound_base(rec.name, records)
        lines = [f"class_<{rec.name}{f', base<{base}>' if base else ''}>(\"{rec.name}\")"]
    ctors = [] if rec.abstract or rec.tag else rec.ctors
    if not rec.abstract and not rec.tag and not rec.ctors and not rec.hidden_ctor:
        lines.append("    .constructor<>()")
    elif len(ctors) == 1 and not unbindable(ctors[0]) and rec.name not in skip:
        lines.append("    " + bind_ctor(rec.name, ctors[0]))
    for fn in rec.methods:
        key = f"{rec.name}::{fn.js}"
        if key in skip or unbindable(fn):
            skipped.append(key)
        else:
            lines.append(bind_method(rec.name, fn))
    lines[-1] += ";"

    # Constructor N is its own class; its constructor is written out, since an
    # inheriting `using` would not carry the default or the copy constructor.
    for fn in ctors if len(ctors) > 1 else []:
        js = f"{rec.name}_{fn.index}"
        if js in skip or unbindable(fn):
            skipped.append(js)
            continue
        cls = f"Bind_{js}"
        params = ", ".join(f"{t} a{i}" for i, t in enumerate(fn.params))
        names = ", ".join(f"a{i}" for i in range(len(fn.params)))
        structs.append(f"struct {cls} : {rec.name} {{ {cls}({params}) : {rec.name}({names}) {{}} }};")
        lines.append(f"class_<{cls}, base<{rec.name}>>(\"{js}\"){bind_ctor(cls, fn)};")
    return structs, "\n".join(lines)


def emit_handle(target: str) -> tuple[list[str], str]:
    """opencascade::handle<T> as Handle_T, built empty (_1) or from a pointer (_2)."""
    h = f"opencascade::handle<{target}>"
    structs = [f"struct Bind_Handle_{target}_1 : {h} {{}};",
               f"struct Bind_Handle_{target}_2 : {h} {{ using {h}::handle; }};"]
    reg = (f"class_<{h}>(\"Handle_{target}\")\n"
           f"    .function(\"IsNull\", optional_override([](const {h}& self) {{ return self.IsNull(); }}))\n"
           f"    .function(\"Nullify\", optional_override([]({h}& self) {{ self.Nullify(); }}))\n"
           f"    .function(\"get\", optional_override([](const {h}& self) {{ return self.get(); }}),"
           f" allow_raw_pointers());\n"
           f"class_<Bind_Handle_{target}_1, base<{h}>>(\"Handle_{target}_1\").constructor<>();\n"
           f"class_<Bind_Handle_{target}_2, base<{h}>>(\"Handle_{target}_2\")"
           f".constructor(optional_override([]({target}* p) {{ return new Bind_Handle_{target}_2(p); }}),"
           f" allow_raw_pointers());")
    return structs, reg


def read_list(path: Path) -> list[str]:
    lines = path.read_text().splitlines() if path.exists() else []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    opts = ap.parse_args()

    wanted = read_list(CLASSES)
    skip = set(read_list(SKIP))
    handles = [n.removeprefix("Handle_") for n in wanted if n.startswith("Handle_")]
    pending = [n for n in wanted if not n.startswith("Handle_")]
    records: dict[str, Record] = {}
    enums: dict[str, list[str]] = {}

    # Layer by layer, so each ancestor is parsed once and a layer runs in parallel.
    with ThreadPoolExecutor() as pool:
        while pending:
            layer = [n for n in dict.fromkeys(pending) if n not in records and n not in enums]
            pending = []
            for name, nodes in zip(layer, pool.map(lambda n: dump_ast(n, opts.include), layer)):
                namespaces = [n for n in nodes if n["kind"] == "NamespaceDecl"]
                if namespaces:
                    records[name] = parse_namespace(name, namespaces)
                    continue
                node = next((n for n in nodes if n["kind"] == "EnumDecl"
                             or n.get("completeDefinition")), None)
                if node is None:
                    raise SystemExit(f"{name}: no class or enum definition in {name}.hxx")
                if node["kind"] == "EnumDecl":
                    enums[name] = [c["name"] for c in node.get("inner", [])
                                   if c["kind"] == "EnumConstantDecl"]
                else:
                    records[name] = parse_record(node)
                    pending += records[name].bases

    # embind needs a base registered before the classes deriving from it.
    order: list[str] = []

    def visit(name: str) -> None:
        if name in records and name not in order:
            for base in records[name].bases:
                visit(base)
            order.append(name)

    for name in records:
        visit(name)

    structs: list[str] = []
    registrations: list[str] = []
    skipped: list[str] = []
    for name in order:
        if records[name].hidden_dtor:
            skipped.append(f"{name} (protected destructor)")
            continue
        s, reg = emit_record(records[name], records, skip, skipped)
        structs += s
        registrations.append(reg)
    for target in handles:
        s, reg = emit_handle(target)
        structs += s
        registrations.append(reg)
    for name, values in enums.items():
        registrations.append(f"enum_<{name}>(\"{name}\")"
                             + "".join(f"\n    .value(\"{v}\", {v})" for v in values) + ";")

    # A bound member may return or take a type its class only forward-declares,
    # so every OCCT type named in a signature brings in its own header.
    named = {word for rec in records.values() for fn in [*rec.ctors, *rec.methods]
             for t in [fn.ret, *fn.params] for word in re.findall(r"\b\w+\b", t)}
    headers = sorted({*records, *enums, *handles,
                      *(w for w in named if (opts.include / f"{w}.hxx").exists())})
    out = ["// Generated by scripts/bindgen.py from bindings/classes.txt. Do not edit.",
           "#include <emscripten/bind.h>",
           "#include <string>",
           *[f"#include <{h}.hxx>" for h in headers],
           "",
           "using namespace emscripten;",
           "",
           *structs,
           "",
           "EMSCRIPTEN_BINDINGS(occt_generated) {",
           *registrations,
           "}",
           "",
           "// Skipped: no JS form, or listed in bindings/skip.txt.",
           *[f"//   {s}" for s in sorted(set(skipped))]]
    opts.out.parent.mkdir(parents=True, exist_ok=True)
    opts.out.write_text("\n".join(out) + "\n")
    print(f"{len(records)} classes, {len(enums)} enums, {len(handles)} handles, "
          f"{len(set(skipped))} members skipped -> {opts.out}")


if __name__ == "__main__":
    main()
