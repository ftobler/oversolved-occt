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
    that name in header order, deprecated ones included. A member that is not
    bound leaves a gap in the numbering, never a shift.

Every member binds through a wrapper lambda with its parameter types spelled
out, which selects the overload and lets a C string travel as a JS string.

A member whose signature names a type that is not bound would register but
throw when called, so it is skipped instead. That covers a class missing from
classes.txt, a handle to one, a nested type, primitive out-parameters, streams
and rvalues. Deprecated members are skipped too. Every skip is listed at the
end of the output with its reason. bindings/skip.txt drops further members by
JS name (`Class::Method_N`, `Class_N` for constructor N, `Class` for a single
constructor) and a line that matches nothing is an error. A handle to a bound
class is registered as Handle_<Class> whenever a bound member uses it.

A member the AST cannot see (OCCT 8 dropped TopoDS_Shape::HashCode) or that
only exists on a subclass handed back as its base is listed in MANUAL_MEMBERS:
manual.cpp writes the C++, and the generator registers it on the class.

Usage: bindgen.py --include <occt include dir> --out <bindings.cpp>
"""

import argparse
import json
import re
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLASSES = REPO / "bindings" / "classes.txt"
MANUAL = REPO / "bindings" / "manual.txt"
SKIP = REPO / "bindings" / "skip.txt"

PRIMITIVE = re.compile(
    r"^(void|bool|char|signed char|unsigned char|short|unsigned short|int|unsigned|"
    r"unsigned int|long|unsigned long|long long|unsigned long long|float|double|size_t|"
    r"Standard_(Boolean|Integer|Real|ShortReal|Character|ExtCharacter|Byte|Size|Address)|"
    r"Poly_MeshPurpose)$"  # a typedef of unsigned int, with its values as plain constants
)
C_STRING = re.compile(r"^(const )?(Standard_CString|(const )?char \*( ?const)?)$")
HANDLE = re.compile(r"^(?:opencascade|occ)::handle<(\w+)>$")
NOISE = {"DumpJson", "InitFromJson", "Dump", "DynamicType", "get_type_name",
         "get_type_descriptor"}
NESTED = {"CXXRecordDecl", "TypedefDecl", "TypeAliasDecl", "EnumDecl"}


@dataclass
class Fn:
    name: str
    ret: str
    params: list[str]
    const: bool = False
    static: bool = False
    index: int = 0  # overload position, 0 when the name is not overloaded
    local: bool = False  # typed with a name nested in its class
    deprecated: bool = False
    delegate: str = ""  # a hand-written member: the manual.cpp function to call

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


# Members the kernel calls that the AST cannot supply: OCCT 7.8 replaced
# TopoDS_Shape::HashCode with std::hash, and the transfer map and the reader
# hand their objects back as a base class, where the subclass member does not
# exist. manual.cpp writes the C++ and manual.h declares it; the generator
# registers it on the class, because embind allows one registration per class
# and the rest of the members come from the AST.
MANUAL_MEMBERS = {
    "TopoDS_Shape": [
        Fn("HashCode", "int", ["int"], const=True, delegate="occt_manual::ShapeHashCode"),
    ],
    "Transfer_Binder": [
        Fn("Result", "std::optional<TopoDS_Shape>", [], const=True,
           delegate="occt_manual::BinderShape"),
    ],
    "Interface_InterfaceModel": [
        Fn("IdentLabel", "int", ["const opencascade::handle<Standard_Transient>&"], const=True,
           delegate="occt_manual::ModelIdentLabel"),
    ],
}


# ─── reading the AST ───


def dump_ast(name: str, include: Path) -> list[dict]:
    """The top-level declarations named exactly `name`, from its own header."""
    cmd = ["em++", "-x", "c++", "-std=c++17", "-fsyntax-only", f"-I{include}",
           "-Xclang", "-ast-dump=json", "-Xclang", f"-ast-dump-filter={name}", "-"]
    run = subprocess.run(cmd, input=f"#include <{name}.hxx>\n", capture_output=True,
                         text=True, check=False)  # clang's own message is reported below
    if run.returncode != 0:
        raise SystemExit(f"{name}: em++ failed on {name}.hxx\n{run.stderr}")
    decoder, pos, found = json.JSONDecoder(), 0, []
    while (pos := run.stdout.find("{", pos)) != -1:
        node, pos = decoder.raw_decode(run.stdout, pos)
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


def is_deprecated(member: dict) -> bool:
    """Upstream marks what it is about to remove; binding it would only invite
    the kernel to depend on it."""
    return any(a["kind"] == "DeprecatedAttr" for a in member.get("inner", []))


def make_fn(member: dict, static: bool = False) -> Fn:
    params = [p["type"]["qualType"] for p in member.get("inner", []) if p["kind"] == "ParmVarDecl"]
    ret, const = split_signature(member["type"]["qualType"])
    return Fn(member["name"], ret, params, const,
              static or member.get("storageClass") == "static",
              deprecated=is_deprecated(member))


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
                or member.get("isImplicit") or member.get("explicitlyDeleted")):
            continue
        fn = make_fn(member)
        fn.local = bool(local and any(local.search(t) for t in [fn.ret, *fn.params]))
        if kind == "CXXConstructorDecl":
            rec.ctors.append(fn)
        elif not fn.name.startswith("operator") and fn.name not in NOISE:
            rec.methods.append(fn)
    number_overloads(rec.ctors)
    number_overloads(rec.methods)
    return rec


def parse_namespace(name: str, nodes: list[dict]) -> Record:
    """OCCT 8 turned static-only classes (TopoDS, BRep_Tool) into namespaces.

    Their functions bind as class functions of an empty tag class, as
    oc.TopoDS.Edge_1(shape). A namespace may be reopened, so every declaration
    of it contributes in header order; a redeclaration of a function does not
    count twice.
    """
    rec = Record(name, tag=True)
    for node in nodes:
        for member in node.get("inner", []):
            if (member["kind"] != "FunctionDecl" or member["name"] in NOISE
                    or member.get("previousDecl") or member.get("explicitlyDeleted")):
                continue
            rec.methods.append(make_fn(member, static=True))
    number_overloads(rec.methods)
    return rec


def number_overloads(fns: list[Fn]) -> None:
    total = Counter(fn.name for fn in fns)
    seen: Counter[str] = Counter()
    for fn in fns:
        if total[fn.name] > 1:
            seen[fn.name] += 1
            fn.index = seen[fn.name]


# ─── what can be bound ───


def core(t: str) -> str:
    """The named type under const, references, pointers and qualifiers."""
    t = re.sub(r"\b(const|volatile|struct|class|enum)\b", "", t)
    return re.sub(r"\s+", " ", t.replace("&", " ").replace("*", " ")).strip()


def no_js_form(t: str) -> bool:
    """A type with no JS form, or one that only works as an out-parameter."""
    if C_STRING.match(t):
        return False
    if "&&" in t or "..." in t or "Stream" in t or "stream" in t or "*&" in t:
        return True
    if t.endswith("*") or (t.endswith("&") and not t.startswith("const ")):
        return bool(PRIMITIVE.match(core(t)))
    return False


class Binder:
    """Decides what is bound, and accumulates what the output needs to say."""

    def __init__(self, include: Path, records: dict[str, Record], enums: dict[str, list[str]],
                 manual: set[str], skip: set[str]) -> None:
        self.include, self.records, self.enums = include, records, enums
        self.manual, self.skip = manual, skip
        self.considered: set[str] = set()
        self.skipped: list[tuple[str, str]] = []
        self.handles: set[str] = set()  # targets to register as Handle_<T>
        self.words: set[str] = set()  # identifiers in bound signatures, for includes

    def type_problem(self, t: str) -> str | None:
        if C_STRING.match(t):
            return None
        if no_js_form(t):
            return "no JS form"
        c = core(t)
        compact = c.replace(" ", "")
        if PRIMITIVE.match(c):
            return None
        if m := HANDLE.match(compact):
            target = self.records.get(m.group(1))
            return None if target and not target.hidden_dtor else f"unbound {compact}"
        rec = self.records.get(c)
        if (rec and not rec.hidden_dtor) or c in self.enums or compact in self.manual:
            return None
        # Anything that is not a builtin, a bound class or a bound enum is unbound,
        # whether or not it has a header of its own (NCollection_ForwardRangeSentinel
        # does not), so the list of accepted types is the whitelist above.
        return f"unbound {compact}"

    def problem(self, fn: Fn) -> str | None:
        """The first reason this member cannot be bound, or None."""
        if fn.deprecated:
            return "deprecated"
        if fn.local:
            return "nested type"
        for t in [fn.ret, *fn.params]:
            if reason := self.type_problem(t):
                return reason
        return None

    def accept(self, fn: Fn) -> None:
        for t in [fn.ret, *fn.params]:
            self.words.update(re.findall(r"\b\w+\b", t))
            if m := HANDLE.match(core(t).replace(" ", "")):
                self.handles.add(m.group(1))

    def drop(self, key: str, reason: str) -> None:
        self.skipped.append((key, reason))

    def usable(self, key: str, fn: Fn) -> bool:
        """Whether fn binds; if not, the skip is recorded under key."""
        self.considered.add(key)
        reason = "skip.txt" if key in self.skip else self.problem(fn)
        if reason:
            self.drop(key, reason)
            return False
        self.accept(fn)
        return True


# ─── emitting ───


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
    OCCT hides pointers behind typedefs, so the spelled type does not tell, and
    the policy is inert where nothing is a pointer."""
    args, call = args_and_call(fn)
    if not fn.static:
        args.insert(0, f"{'const ' if fn.const else ''}{rec}& self")
    if fn.delegate:  # manual.cpp owns the signature; the lambda only forwards
        passed = ([] if fn.static else ["self"]) + call
        ret = fn.ret
        body = f"return {fn.delegate}({', '.join(passed)});"
        kind = "class_function" if fn.static else "function"
        return (f"    .{kind}(\"{fn.js}\", optional_override([]({', '.join(args)}) -> {ret} "
                f"{{ {body} }}))")
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


def emit_record(rec: Record, b: Binder) -> tuple[list[str], str]:
    """The overload subclasses and the class_ registrations of one record."""
    structs: list[str] = []
    if rec.tag:
        structs.append(f"struct Bind_{rec.name} {{}};")
        lines = [f"class_<Bind_{rec.name}>(\"{rec.name}\")"]
    else:
        base = bound_base(rec.name, b.records)
        lines = [f"class_<{rec.name}{f', base<{base}>' if base else ''}>(\"{rec.name}\")"]
    ctors = [] if rec.abstract or rec.tag else rec.ctors
    if not rec.abstract and not rec.tag and not rec.ctors and not rec.hidden_ctor:
        lines.append("    .constructor<>()")
    elif len(ctors) == 1 and b.usable(rec.name, ctors[0]):
        lines.append("    " + bind_ctor(rec.name, ctors[0]))
    for fn in rec.methods:
        if b.usable(f"{rec.name}::{fn.js}", fn):
            lines.append(bind_method(rec.name, fn))
    for fn in MANUAL_MEMBERS.get(rec.name, []):
        b.accept(fn)
        lines.append(bind_method(rec.name, fn))
    lines[-1] += ";"

    # Constructor N is its own class; its constructor is written out, since an
    # inheriting `using` would not carry the default or the copy constructor.
    for fn in ctors if len(ctors) > 1 else []:
        js = f"{rec.name}_{fn.index}"
        if not b.usable(js, fn):
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
    """One entry per line; `#` starts a comment, on its own line or after an entry."""
    lines = path.read_text().splitlines() if path.exists() else []
    return [entry for ln in lines if (entry := ln.split("#", 1)[0].strip())]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    opts = ap.parse_args()

    wanted = read_list(CLASSES)
    explicit_handles = [n.removeprefix("Handle_") for n in wanted if n.startswith("Handle_")]
    # A handle needs its target registered, so a listed handle lists its class too.
    pending = [n for n in wanted if not n.startswith("Handle_")] + explicit_handles
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

    skip = set(read_list(SKIP))
    manual = {t.replace(" ", "") for t in read_list(MANUAL)}
    if stale := sorted(set(MANUAL_MEMBERS) - set(records)):
        raise SystemExit("bindgen.py manual members name classes classes.txt does not list: "
                         + ", ".join(stale))
    binder = Binder(opts.include, records, enums, manual, skip)
    binder.handles.update(explicit_handles)

    structs: list[str] = []
    registrations: list[str] = []
    for name in order:
        if records[name].hidden_dtor:
            binder.drop(name, "protected destructor")
            continue
        s, reg = emit_record(records[name], binder)
        structs += s
        registrations.append(reg)
    if stale := sorted(skip - binder.considered):
        raise SystemExit("bindings/skip.txt matches nothing: " + ", ".join(stale))
    for target in sorted(binder.handles):
        s, reg = emit_handle(target)
        structs += s
        registrations.append(reg)
    for name, values in enums.items():
        registrations.append(f"enum_<{name}>(\"{name}\")"
                             + "".join(f"\n    .value(\"{v}\", {v})" for v in values) + ";")

    # A bound member may return or take a type its class only forward-declares,
    # so every OCCT type named in a bound signature brings in its own header.
    headers = sorted({*records, *enums, *binder.handles,
                      *(w for w in binder.words if (opts.include / f"{w}.hxx").exists())})
    unbound = Counter(r for _, r in binder.skipped if r.startswith("unbound "))
    out = ["// Generated by scripts/bindgen.py from bindings/classes.txt. Do not edit.",
           "#include <emscripten/bind.h>",
           "#include <string>",
           "#include \"manual.h\"",
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
           "// Skipped members, with the reason. List an unbound type in bindings/classes.txt",
           "// to bind the members that need it.",
           *[f"//   {k}: {r}" for k, r in sorted(binder.skipped)],
           "",
           "// Unbound types by the number of skipped members that name them.",
           *[f"//   {r.removeprefix('unbound ')}: {n}" for r, n in unbound.most_common()]]
    opts.out.parent.mkdir(parents=True, exist_ok=True)
    opts.out.write_text("\n".join(out) + "\n")
    print(f"{len(records)} classes, {len(enums)} enums, {len(binder.handles)} handles, "
          f"{len(binder.skipped)} members skipped, {len(unbound)} unbound types -> {opts.out}")


if __name__ == "__main__":
    main()
