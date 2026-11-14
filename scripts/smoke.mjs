// Load the built module in node and drive the paths Oversolved depends on:
// construction, a boolean, mass properties, a STEP round trip through the
// in-memory FS, and a C++ exception crossing into JS. A green run means the
// artifact links, instantiates and computes, not that every binding is right;
// that is the consumer's test suite's job.
//
// Usage: node scripts/smoke.mjs <dist dir>

import assert from 'node:assert/strict'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const dist = path.resolve(process.argv[2] ?? 'tmp/dist')
const { default: occt } = await import(pathToFileURL(path.join(dist, 'occt.js')).href)
const oc = await occt()

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

// STEP out and back in through MEMFS keeps the solid.
const writer = new oc.STEPControl_Writer_1()
writer.Transfer_1(cut.Shape(), oc.STEPControl_StepModelType.STEPControl_AsIs, true, progress)
assert.equal(writer.Write('/smoke.step'), oc.IFSelect_ReturnStatus.IFSelect_RetDone, 'STEP write')
const reader = new oc.STEPControl_Reader_1()
assert.equal(reader.ReadFile_1('/smoke.step'), oc.IFSelect_ReturnStatus.IFSelect_RetDone, 'STEP read')
reader.TransferRoots(progress)
assert.ok(Math.abs(volume(reader.OneShape()) - expected) < 1e-3, 'STEP round trip volume')
console.log(`step: ${oc.FS.readFile('/smoke.step').length} bytes, round trip keeps the volume`)

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

console.log('smoke: ok')
