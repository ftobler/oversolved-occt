// Bulk readers for Oversolved's render pass: a whole face's or shape's mesh,
// and a table of per-face facts, each in one call returning JS typed arrays.
//
// The per-call API costs several embind crossings per node and per triangle;
// these do the same reads in C++. Every value must equal what the consumer's
// per-call reader computes, bit for bit: its face sort order is derived from
// them. So each read below is the same OCCT call the consumer makes, in the
// same order, with the same arguments; scripts/smoke.mjs compares the two.
//
// These are free-standing classes with only class functions, not members of a
// generated class, so they have their own binding block. The Oversolved prefix
// keeps them apart from OCCT API.
#include <emscripten/bind.h>
#include <emscripten/val.h>

#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <Poly_Triangulation.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Sphere.hxx>
#include <gp_Torus.hxx>
#include <gp_Vec.hxx>

#include <cmath>
#include <limits>
#include <vector>

using emscripten::val;

namespace
{

// A fresh JS array holding a copy of the data: a view straight into the wasm
// heap would dangle once the vector dies or the memory grows.
template <typename T>
val jsCopy(const char* theArrayType, const std::vector<T>& theData)
{
  return val::global(theArrayType).new_(emscripten::typed_memory_view(theData.size(), theData.data()));
}

// One face's block appended to the outputs, as the consumer's tessellateFace
// reads it: nodes moved by the face location, triangles 0-based within the
// face, wound the other way on a REVERSED face. False when the face has no
// triangulation, and then nothing is appended.
bool appendFace(const TopoDS_Face& theFace, std::vector<double>& theNodes, std::vector<int>& theTriangles)
{
  TopLoc_Location aLoc;
  const occ::handle<Poly_Triangulation>& aPoly = BRep_Tool::Triangulation(theFace, aLoc, Poly_MeshPurpose_NONE);
  if (aPoly.IsNull())
  {
    return false;
  }
  const gp_Trsf aTrsf = aLoc.Transformation();
  const int aNbNodes = aPoly->NbNodes();
  theNodes.reserve(theNodes.size() + 3 * static_cast<std::size_t>(aNbNodes));
  for (int i = 1; i <= aNbNodes; i++)
  {
    const gp_Pnt aPnt = aPoly->Node(i).Transformed(aTrsf);
    theNodes.push_back(aPnt.X());
    theNodes.push_back(aPnt.Y());
    theNodes.push_back(aPnt.Z());
  }
  const bool isReversed = theFace.Orientation() == TopAbs_REVERSED;
  const int aNbTriangles = aPoly->NbTriangles();
  theTriangles.reserve(theTriangles.size() + 3 * static_cast<std::size_t>(aNbTriangles));
  for (int i = 1; i <= aNbTriangles; i++)
  {
    int a = 0, b = 0, c = 0;
    aPoly->Triangle(i).Get(a, b, c);
    theTriangles.push_back(a - 1);
    theTriangles.push_back((isReversed ? c : b) - 1);
    theTriangles.push_back((isReversed ? b : c) - 1);
  }
  return true;
}

constexpr double THE_NAN = std::numeric_limits<double>::quiet_NaN();

// The facts table layout; Stride() hands the width to JS so it is never
// hard coded there.
enum FactSlot
{
  FACT_TYPE = 0,
  FACT_CENTROID = 1,  // x, y, z
  FACT_AREA = 4,
  FACT_NORMAL = 5,  // x, y, z
  FACT_REVERSED = 8,
  FACT_AXIS = 9,     // x, y, z
  FACT_ORIGIN = 12,  // x, y, z
  FACT_RADIUS = 15,
  FACT_SEMI_ANGLE = 16,
  FACT_MINOR_RADIUS = 17,
  FACT_CYLINDER_DV = 18,  // x, y, z
  FACT_STRIDE = 21
};

void putXYZ(double* theRow, int theSlot, double theX, double theY, double theZ)
{
  theRow[theSlot] = theX;
  theRow[theSlot + 1] = theY;
  theRow[theSlot + 2] = theZ;
}

void putAxis(double* theRow, const gp_Ax1& theAxis)
{
  const gp_Dir aDir = theAxis.Direction();
  putXYZ(theRow, FACT_AXIS, aDir.X(), aDir.Y(), aDir.Z());
}

void putOrigin(double* theRow, const gp_Ax3& thePosition)
{
  const gp_Pnt aLoc = thePosition.Location();
  putXYZ(theRow, FACT_ORIGIN, aLoc.X(), aLoc.Y(), aLoc.Z());
}

// One face's row, every slot starting NaN ("absent"). The adaptor is built
// once and read in the order the consumer's faceSurfaceTypeAndNormal,
// faceSurfaceFrame and faceCylinderAxis read their own.
void readFacts(const TopoDS_Face& theFace, double* theRow)
{
  for (int i = 0; i < FACT_STRIDE; i++)
  {
    theRow[i] = THE_NAN;
  }
  const BRepAdaptor_Surface anAdaptor(theFace, true);
  const GeomAbs_SurfaceType aType = anAdaptor.GetType();
  const bool isReversed = theFace.Orientation() == TopAbs_REVERSED;
  theRow[FACT_TYPE] = static_cast<double>(aType);
  theRow[FACT_REVERSED] = isReversed ? 1.0 : 0.0;

  // The (bool, bool) overload, not the eps one: the consumer's area and volume
  // rule depends on which of the two runs.
  GProp_GProps aProps;
  BRepGProp::SurfaceProperties(theFace, aProps, false, false);
  const gp_Pnt aCentre = aProps.CentreOfMass();
  putXYZ(theRow, FACT_CENTROID, aCentre.X(), aCentre.Y(), aCentre.Z());
  theRow[FACT_AREA] = aProps.Mass();

  const double u = (anAdaptor.FirstUParameter() + anAdaptor.LastUParameter()) / 2;
  const double v = (anAdaptor.FirstVParameter() + anAdaptor.LastVParameter()) / 2;
  BRepLProp_SLProps aSurfProps(anAdaptor, u, v, 1, 1e-9);
  if (aSurfProps.IsNormalDefined())
  {
    const gp_Dir aNormal = aSurfProps.Normal();
    if (isReversed)
    {
      putXYZ(theRow, FACT_NORMAL, -aNormal.X(), -aNormal.Y(), -aNormal.Z());
    }
    else
    {
      putXYZ(theRow, FACT_NORMAL, aNormal.X(), aNormal.Y(), aNormal.Z());
    }
  }

  switch (aType)
  {
    case GeomAbs_Cylinder: {
      const gp_Cylinder aCyl = anAdaptor.Cylinder();
      putAxis(theRow, aCyl.Axis());
      putOrigin(theRow, aCyl.Position());
      theRow[FACT_RADIUS] = aCyl.Radius();
      // The axis as the consumer's faceCylinderAxis reads it: dv at the UV
      // midpoint, normalised the way its JS does (sqrt of the sum of squares,
      // then divide), not gp_Vec::Normalized.
      gp_Pnt aPnt;
      gp_Vec aDu, aDv;
      anAdaptor.D1(u, v, aPnt, aDu, aDv);
      const double x = aDv.X();
      const double y = aDv.Y();
      const double z = aDv.Z();
      const double aLen = std::sqrt(x * x + y * y + z * z);
      if (!(aLen < 1e-12))
      {
        putXYZ(theRow, FACT_CYLINDER_DV, x / aLen, y / aLen, z / aLen);
      }
      break;
    }
    case GeomAbs_Cone: {
      const gp_Cone aCone = anAdaptor.Cone();
      putAxis(theRow, aCone.Axis());
      putOrigin(theRow, aCone.Position());
      theRow[FACT_RADIUS] = aCone.RefRadius();
      theRow[FACT_SEMI_ANGLE] = std::abs(aCone.SemiAngle());
      break;
    }
    case GeomAbs_Sphere: {
      const gp_Sphere aSphere = anAdaptor.Sphere();
      putXYZ(theRow, FACT_AXIS, 0.0, 0.0, 1.0);  // a sphere has no axis; the consumer's convention
      putOrigin(theRow, aSphere.Position());
      theRow[FACT_RADIUS] = aSphere.Radius();
      break;
    }
    case GeomAbs_Torus: {
      const gp_Torus aTorus = anAdaptor.Torus();
      putAxis(theRow, aTorus.Axis());
      putOrigin(theRow, aTorus.Position());
      theRow[FACT_RADIUS] = aTorus.MajorRadius();
      theRow[FACT_MINOR_RADIUS] = aTorus.MinorRadius();
      break;
    }
    default:
      break;
  }
}

struct OversolvedMesh
{
  // Meshes the whole shape once, so each shared edge is discretised once. No
  // parallel meshing: this build has no threads.
  static bool MeshShape(const TopoDS_Shape& theShape, double theLinear, bool isRelative, double theAngular)
  {
    BRepMesh_IncrementalMesh aMesher(theShape, theLinear, isRelative, theAngular, false);
    return aMesher.IsDone();
  }

  // { nodes, triangles } for a face already meshed, or null when it has no
  // triangulation.
  static val FaceTriangulation(const TopoDS_Face& theFace)
  {
    std::vector<double> aNodes;
    std::vector<int> aTriangles;
    if (!appendFace(theFace, aNodes, aTriangles))
    {
      return val::null();
    }
    val aResult = val::object();
    aResult.set("nodes", jsCopy("Float64Array", aNodes));
    aResult.set("triangles", jsCopy("Int32Array", aTriangles));
    return aResult;
  }

  // Every face's block in explorer order, concatenated; triangle indices stay
  // local to their face, and nodeStart/triStart (faceCount + 1 entries) are
  // the prefix offsets in nodes and triangles. A face with no triangulation is
  // an empty block. Null for a null shape.
  static val ShapeTriangulation(const TopoDS_Shape& theShape)
  {
    if (theShape.IsNull())
    {
      return val::null();
    }
    std::vector<double> aNodes;
    std::vector<int> aTriangles;
    std::vector<int> aNodeStart{0};
    std::vector<int> aTriStart{0};
    for (TopExp_Explorer anExp(theShape, TopAbs_FACE); anExp.More(); anExp.Next())
    {
      appendFace(TopoDS::Face(anExp.Current()), aNodes, aTriangles);
      aNodeStart.push_back(static_cast<int>(aNodes.size() / 3));
      aTriStart.push_back(static_cast<int>(aTriangles.size() / 3));
    }
    val aResult = val::object();
    aResult.set("faceCount", static_cast<int>(aNodeStart.size()) - 1);
    aResult.set("nodes", jsCopy("Float64Array", aNodes));
    aResult.set("triangles", jsCopy("Int32Array", aTriangles));
    aResult.set("nodeStart", jsCopy("Int32Array", aNodeStart));
    aResult.set("triStart", jsCopy("Int32Array", aTriStart));
    return aResult;
  }
};

struct OversolvedFaceFacts
{
  static int Stride() { return FACT_STRIDE; }

  // { faceCount, facts }: one row of Stride() values per face, in explorer
  // order. NaN marks a slot that does not apply to the face.
  static val Read(const TopoDS_Shape& theShape)
  {
    std::vector<double> aFacts;
    int aFaceCount = 0;
    for (TopExp_Explorer anExp(theShape, TopAbs_FACE); anExp.More(); anExp.Next())
    {
      aFacts.resize(aFacts.size() + FACT_STRIDE);
      readFacts(TopoDS::Face(anExp.Current()), aFacts.data() + aFacts.size() - FACT_STRIDE);
      aFaceCount++;
    }
    val aResult = val::object();
    aResult.set("faceCount", aFaceCount);
    aResult.set("facts", jsCopy("Float64Array", aFacts));
    return aResult;
  }
};

}  // namespace

EMSCRIPTEN_BINDINGS(oversolved_mesh)
{
  using namespace emscripten;
  class_<OversolvedMesh>("OversolvedMesh")
    .class_function("MeshShape", &OversolvedMesh::MeshShape)
    .class_function("FaceTriangulation", &OversolvedMesh::FaceTriangulation)
    .class_function("ShapeTriangulation", &OversolvedMesh::ShapeTriangulation);
  class_<OversolvedFaceFacts>("OversolvedFaceFacts")
    .class_function("Stride", &OversolvedFaceFacts::Stride)
    .class_function("Read", &OversolvedFaceFacts::Read);
}
