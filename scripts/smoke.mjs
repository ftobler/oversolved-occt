// Load the built module in node and drive the paths Oversolved depends on:
// construction, a boolean, mass properties, a STEP round trip through the
// in-memory FS with the transfer map's per-face ids, a C++ exception crossing
// into JS, and a probe that no registered member names an unbound type. A green
// run means the artifact links, instantiates and computes, not that every
// binding is right; that is the consumer's test suite's job.
//
// Usage: node scripts/smoke.mjs <dist dir>

import assert from 'node:assert/strict'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const dist = path.resolve(process.argv[2] ?? 'tmp/dist')
const { default: occt } = await import(pathToFileURL(path.join(dist, 'occt.js')).href)
const oc = await occt()

// The link's -sSTACK_SIZE is the r2 fix for deep modeling calls (see justfile);
// read it back from the module, so dropping the flag cannot stay green.
const stackBytes = oc._emscripten_stack_get_base() - oc._emscripten_stack_get_end()
assert.equal(stackBytes, 5 * 1024 * 1024, `stack is 5 MB (found ${stackBytes})`)
console.log(`stack: ${stackBytes / 1024 / 1024} MB`)

function volume(shape) {
  const props = new oc.GProp_GProps_1()
  oc.BRepGProp.VolumeProperties_1(shape, props, false, false, false)
  const mass = props.Mass()
  props.delete()
  return mass
}

// A 10 x 20 x 30 box minus a radius 2 cylinder standing on its corner edge:
// a quarter of the cylinder lies inside the box.
const progress = new oc.Message_ProgressRange_1()
const box = new oc.BRepPrimAPI_MakeBox_2(10, 20, 30)
const cylinder = new oc.BRepPrimAPI_MakeCylinder_1(2, 30)
const cut = new oc.BRepAlgoAPI_Cut_3(box.Shape(), cylinder.Shape(), progress)
const expected = 10 * 20 * 30 - (Math.PI * 2 * 2 * 30) / 4
assert.ok(Math.abs(volume(cut.Shape()) - expected) < 1e-6, 'box minus cylinder volume')
console.log(`boolean: volume ${volume(cut.Shape()).toFixed(6)} as expected`)

// The identity hash the kernel buckets sub-shapes by: a stable, bounded int.
// OCCT 7.8 dropped the member, so the binding is hand-written (manual.cpp).
// It follows IsSame: orientation is ignored, the location is not.
const SHAPE_HASH_UPPER = 2147483647
const cutShape = cut.Shape()
const hash = cutShape.HashCode(SHAPE_HASH_UPPER)
assert.ok(Number.isInteger(hash) && hash >= 1 && hash <= SHAPE_HASH_UPPER,
  `TopoDS_Shape.HashCode in [1, ${SHAPE_HASH_UPPER}]: ${hash}`)
assert.equal(cutShape.HashCode(SHAPE_HASH_UPPER), hash, 'HashCode is stable')
assert.equal(cutShape.Reversed().HashCode(SHAPE_HASH_UPPER), hash, 'HashCode ignores orientation')
const movedTrsf = new oc.gp_Trsf_1()
movedTrsf.SetTranslation_1(new oc.gp_Vec_4(1, 2, 3))
const movedShape = cutShape.Moved(new oc.TopLoc_Location_4(movedTrsf), false)
assert.ok(!movedShape.IsSame(cutShape), 'the moved shape is not the same')
assert.notEqual(movedShape.HashCode(SHAPE_HASH_UPPER), hash, 'HashCode folds in the location')
console.log(`hash: ${hash} in [1, ${SHAPE_HASH_UPPER}], stable, orientation-free, location-aware`)

// A circular face pcurve hands back a gp_Circ2d: the cut's planar caps carry
// the radius 2 circle of the cylindrical face.
let pcurveCircle = null
const faceWalk = new oc.TopExp_Explorer_2(cutShape, oc.TopAbs_ShapeEnum.TopAbs_FACE,
  oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
for (; faceWalk.More() && pcurveCircle === null; faceWalk.Next()) {
  const face = oc.TopoDS.Face_1(faceWalk.Current())
  const edgeWalk = new oc.TopExp_Explorer_2(face, oc.TopAbs_ShapeEnum.TopAbs_EDGE,
    oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
  for (; edgeWalk.More() && pcurveCircle === null; edgeWalk.Next()) {
    const pcurve = new oc.BRepAdaptor_Curve2d_2(oc.TopoDS.Edge_1(edgeWalk.Current()), face)
    if (pcurve.GetType() === oc.GeomAbs_CurveType.GeomAbs_Circle) pcurveCircle = pcurve.Circle()
    pcurve.delete()
  }
}
assert.ok(pcurveCircle !== null, 'a circular face pcurve yields gp_Circ2d')
const pcurveRadius = pcurveCircle.Radius()
assert.ok(Math.abs(pcurveRadius - 2) < 1e-9, `pcurve radius ${pcurveRadius}`)
pcurveCircle.delete()

// ShapeFix_Shape's substitution context is a ShapeBuild_ReShape.
const fix = new oc.ShapeFix_Shape_2(cutShape)
const context = fix.Context()
assert.ok(!context.IsNull(), 'ShapeFix_Shape.Context() is a ShapeBuild_ReShape')
context.delete()
fix.delete()
console.log(`surface: gp_Circ2d radius ${pcurveRadius}, ShapeFix_Shape.Context() present`)

// Two fused cylinders with every edge filleted: the operation whose call chain
// needed the 5 MB stack rather than the 64 KB default.
const crossingAxis = new oc.gp_Ax2_4(new oc.gp_Pnt_3(0, 0, 10), new oc.gp_Dir_5(1, 0, 0))
const crossing = new oc.BRepPrimAPI_MakeCylinder_3(crossingAxis, 3, 30)
const fused = new oc.BRepAlgoAPI_Fuse_3(cylinder.Shape(), crossing.Shape(), progress).Shape()
const fillet = new oc.BRepFilletAPI_MakeFillet(fused, oc.ChFi3d_FilletShape.ChFi3d_Rational)
const filletEdges = new oc.TopExp_Explorer_2(fused, oc.TopAbs_ShapeEnum.TopAbs_EDGE,
  oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
let filletEdgeCount = 0
for (; filletEdges.More(); filletEdges.Next()) {
  fillet.Add_2(0.5, oc.TopoDS.Edge_1(filletEdges.Current()))
  filletEdgeCount++
}
fillet.Build(progress)
assert.ok(fillet.IsDone(), 'fillet over two fused cylinders')
assert.ok(volume(fillet.Shape()) > 0, 'the fillet result has volume')
console.log(`fillet: ${filletEdgeCount} edges over two fused cylinders`)

// STEP out and back in through MEMFS keeps the solid.
const writer = new oc.STEPControl_Writer_1()
writer.Transfer_1(cut.Shape(), oc.STEPControl_StepModelType.STEPControl_AsIs, true, progress)
assert.equal(writer.Write('/smoke.step'), oc.IFSelect_ReturnStatus.IFSelect_RetDone, 'STEP write')
const reader = new oc.STEPControl_Reader_1()
assert.equal(reader.ReadFile_1('/smoke.step'), oc.IFSelect_ReturnStatus.IFSelect_RetDone, 'STEP read')
reader.TransferRoots(progress)
const readShape = reader.OneShape()
assert.ok(Math.abs(volume(readShape) - expected) < 1e-3, 'STEP round trip volume')
console.log(`step: ${oc.FS.readFile('/smoke.step').length} bytes, round trip keeps the volume`)

// Every face the read produced has the `#N` it was written as. The transfer
// map hands the model and the binders back as their base classes; Result() and
// IdentLabel() are the hand-written widenings that read them.
const wsHandle = reader.WS()
const modelHandle = reader.Model()
const readerHandle = wsHandle.get().TransferReader()
const processHandle = readerHandle.get().TransientProcess()
const model = modelHandle.get()
const transferMap = processHandle.get()
const entityIds = []
let entries = 0
try {
  entries = transferMap.NbMapped()
  for (let i = 1; i <= entries; i++) {
    const binderHandle = transferMap.MapItem(i)
    if (binderHandle.IsNull()) {
      binderHandle.delete()
      continue
    }
    const binder = binderHandle.get()
    const result = binder.Result()
    if (result === undefined) {
      binderHandle.delete()
      continue
    }
    if (result.ShapeType() === oc.TopAbs_ShapeEnum.TopAbs_FACE) {
      const entity = transferMap.Mapped(i)
      if (!entity.IsNull()) entityIds.push(model.IdentLabel(entity))
      entity.delete()
    }
    result.delete()
    binderHandle.delete()
  }
} finally {
  // `get()` returns a borrowed view of the handle's object: never delete those.
  processHandle.delete()
  readerHandle.delete()
  modelHandle.delete()
  wsHandle.delete()
}
const faceExp = new oc.TopExp_Explorer_2(readShape, oc.TopAbs_ShapeEnum.TopAbs_FACE,
  oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
let faceCount = 0
for (; faceExp.More(); faceExp.Next()) faceCount++
assert.equal(entityIds.length, faceCount, 'every read face has a STEP entity id')
assert.equal(new Set(entityIds).size, entityIds.length, 'STEP entity ids are distinct')
assert.ok(entityIds.every((id) => id > 0), 'STEP entity ids are the file\'s #N')
console.log(`transfer map: ${faceCount} faces, ${entries} entries, ids ${entityIds.join(',')}`)

// ─── OversolvedStepCaf ───

// The colour-aware read (bindings/oversolved_caf.cpp) must hand back the same
// shape and transfer map as the plain reader: the kernel names imported faces
// by their `#N`, so those names may not change when it switches readers.

// Per face in explorer order the `#N` it was read from, through a reader's
// WS() and Model(), as the kernel walks them.
function faceLabelsInExplorerOrder(source, shape) {
  const ws = source.WS()
  const modelH = source.Model()
  const trH = ws.get().TransferReader()
  const tpH = trH.get().TransientProcess()
  const tp = tpH.get()
  const pairs = []
  try {
    for (let i = 1; i <= tp.NbMapped(); i++) {
      const binderH = tp.MapItem(i)
      const result = binderH.IsNull() ? undefined : binderH.get().Result()
      binderH.delete()
      if (result === undefined) continue
      if (result.ShapeType() !== oc.TopAbs_ShapeEnum.TopAbs_FACE) {
        result.delete()
        continue
      }
      const entity = tp.Mapped(i)
      pairs.push([result, entity.IsNull() ? 0 : modelH.get().IdentLabel(entity)])
      entity.delete()
    }
    const labels = []
    const exp = new oc.TopExp_Explorer_2(shape, oc.TopAbs_ShapeEnum.TopAbs_FACE,
      oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
    for (; exp.More(); exp.Next()) {
      const hit = pairs.find(([face]) => face.IsSame(exp.Current()))
      labels.push(hit ? hit[1] : 0)
    }
    exp.delete()
    return labels
  } finally {
    for (const [face] of pairs) face.delete()
    tpH.delete()
    trH.delete()
    modelH.delete()
    ws.delete()
  }
}

function readCaf(file) {
  const caf = new oc.OversolvedStepCaf()
  assert.equal(caf.ReadFile(file), oc.IFSelect_ReturnStatus.IFSelect_RetDone, `CAF read ${file}`)
  assert.ok(caf.Transfer(), `CAF transfer ${file}`)
  return caf
}

function countOf(shape, type) {
  const exp = new oc.TopExp_Explorer_2(shape, type, oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
  let n = 0
  for (; exp.More(); exp.Next()) n++
  exp.delete()
  return n
}

const hex = (c) => (c < 0 ? '-1' : `#${c.toString(16).padStart(6, '0')}`)

function facesOf(shape) {
  const faces = []
  const exp = new oc.TopExp_Explorer_2(shape, oc.TopAbs_ShapeEnum.TopAbs_FACE, oc.TopAbs_ShapeEnum.TopAbs_SHAPE)
  for (; exp.More(); exp.Next()) faces.push(exp.Current())
  exp.delete()
  return faces
}

function area(face) {
  const props = new oc.GProp_GProps_1()
  oc.BRepGProp.SurfaceProperties_1(face, props, false, false)
  const mass = props.Mass()
  props.delete()
  return mass
}

// The uncoloured round-trip file: same labels and volume as the plain read,
// and no colour anywhere.
{
  const plainLabels = faceLabelsInExplorerOrder(reader, readShape)
  const caf = readCaf('/smoke.step')
  const cafShape = caf.OneShape()
  assert.equal(cafShape.ShapeType(), readShape.ShapeType(), 'CAF and plain OneShape have the same type')
  assert.deepEqual(faceLabelsInExplorerOrder(caf, cafShape), plainLabels,
    'face -> #N through OversolvedStepCaf equals the plain reader, face for face')
  assert.ok(Math.abs(volume(cafShape) - volume(readShape)) < 1e-9, 'CAF and plain read give the same volume')
  const { faceColor, solidColor } = caf.Colors(cafShape)
  assert.ok(faceColor instanceof Int32Array && solidColor instanceof Int32Array, 'colours are Int32Arrays')
  assert.equal(faceColor.length, plainLabels.length, 'one colour per face')
  assert.equal(solidColor.length, countOf(cafShape, oc.TopAbs_ShapeEnum.TopAbs_SOLID), 'one colour per solid')
  assert.ok([...faceColor, ...solidColor].every((c) => c === -1), 'an uncoloured file reports -1 everywhere')
  cafShape.delete()
  caf.delete()
  console.log(`step caf: ${plainLabels.length} face labels equal the plain read, no colours`)
}

// A coloured file written through XCAF: a mid grey part, so a linear/sRGB
// mix-up cannot pass (linear would read back as about #373737), and one face
// in its own colour.
{
  const SOLID = 0x808080
  const FACE = 0x3366cc
  // A face whose area no other face shares, so the read can be matched to it.
  const writtenAreas = facesOf(cut.Shape()).map(area)
  const FACE_INDEX = 1 + writtenAreas.findIndex((a) => writtenAreas.filter((b) => Math.abs(a - b) < 1e-6).length === 1)
  assert.ok(FACE_INDEX > 0, 'the cut has a face of unique area')
  assert.ok(oc.OversolvedStepCaf.WriteColoredForTest(cut.Shape(), '/smoke-color.step', SOLID, FACE_INDEX,
    FACE, 'smoke part'), 'coloured STEP write')
  const text = new TextDecoder().decode(oc.FS.readFile('/smoke-color.step'))
  assert.match(text, /COLOUR_RGB\('[^']*',0\.50196/, 'the file carries the grey as sRGB 128/255')

  const caf = readCaf('/smoke-color.step')
  const shape = caf.OneShape()
  assert.ok(Math.abs(volume(shape) - expected) < 1e-3, 'coloured round trip keeps the volume')
  const labels = faceLabelsInExplorerOrder(caf, shape)
  assert.ok(labels.every((id) => id > 0) && new Set(labels).size === labels.length,
    'every face of the coloured read has its own #N')
  const { faceColor, solidColor } = caf.Colors(shape)
  assert.ok(solidColor.length > 0 && solidColor.every((c) => c === SOLID),
    `the solid colour comes back as written, sRGB: ${[...solidColor].map(hex)}`)
  // The read may order faces differently from the write, so the coloured face
  // is told apart by its area.
  const areas = facesOf(shape).map(area)
  const coloured = areas.findIndex((a) => Math.abs(a - writtenAreas[FACE_INDEX - 1]) < 1e-6)
  assert.ok(coloured >= 0, 'the coloured face is in the read')
  const wanted = areas.map((_, i) => hex(i === coloured ? FACE : SOLID))
  assert.deepEqual([...faceColor].map(hex), wanted, 'the coloured face has its colour, the others the solid\'s')
  assert.deepEqual(caf.Names(shape), [...solidColor].map(() => 'smoke part'), 'the solid carries its part name')
  shape.delete()
  caf.delete()
  console.log(`step caf: solid ${hex(SOLID)}, face ${FACE_INDEX} ${hex(FACE)}, others inherit; names read`)
}

// ─── end OversolvedStepCaf ───

// A degenerate edge throws StdFail_NotDone in C++; it must surface in JS as an
// exception the consumer can name.
const p = new oc.gp_Pnt_3(1, 2, 3)
const edge = new oc.BRepBuilderAPI_MakeEdge_3(p, p)
let thrown = null
try {
  edge.Edge()
} catch (e) {
  thrown = e
}
assert.ok(thrown instanceof WebAssembly.Exception, 'a C++ throw arrives as a WebAssembly.Exception')
const [type, message] = oc.getExceptionMessage(thrown)
oc.decrementExceptionRefcount(thrown)
assert.equal(type, 'StdFail_NotDone', 'the exception names its C++ type')
console.log(`exceptions: ${type} (${message}) surfaces in JS`)

// Embind registers a member whose signature names an unbound type, and only
// throws when it is called. The throw sits at the member's own argument count,
// ahead of any argument or `this` check, so each member is called at every
// count from 0 to MAX_ARGS with undefined arguments. A real binding fails with
// a different error (embind rejects undefined for a number before any C++ runs,
// and `this` is undefined for a method), an unbound one names its missing types.
// This runs last: a member taking only bools does execute, and could change
// global state the checks above rely on.
const MAX_ARGS = 12
const unbound = new Set()
const probe = (label, call) => {
  for (let n = 0; n <= MAX_ARGS; n++) {
    try {
      const result = call(Array(n).fill(undefined))
      if (result && typeof result.delete === 'function') result.delete()
    } catch (e) {
      const missing = /unbound types: (.*)$/s.exec(String(e?.message))
      if (missing) unbound.add(`${label}: ${missing[1]}`)
    }
  }
}
for (const [name, cls] of Object.entries(oc)) {
  if (typeof cls !== 'function' || !cls.prototype) continue
  probe(`new ${name}`, (args) => new cls(...args))
  for (const key of Object.getOwnPropertyNames(cls)) {
    if (typeof cls[key] === 'function' && !['length', 'name', 'prototype'].includes(key)) {
      probe(`${name}.${key}`, (args) => cls[key](...args))
    }
  }
  for (const key of Object.getOwnPropertyNames(cls.prototype)) {
    if (key !== 'constructor' && typeof cls.prototype[key] === 'function') {
      probe(`${name}#${key}`, (args) => cls.prototype[key].call(undefined, ...args))
    }
  }
}
assert.deepEqual([...unbound], [], `members that throw when called:\n  ${[...unbound].slice(0, 25).join('\n  ')}`)
console.log('bindings: no member names an unbound type')

console.log('smoke: ok')
