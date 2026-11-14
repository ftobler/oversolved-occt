// Load the built module in node and drive the paths Oversolved depends on:
// construction, a boolean, mass properties, a STEP round trip through the
// in-memory FS, a C++ exception crossing into JS, and a probe that no
// registered member names an unbound type. A green run means the artifact
// links, instantiates and computes, not that every binding is right; that is
// the consumer's test suite's job.
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
