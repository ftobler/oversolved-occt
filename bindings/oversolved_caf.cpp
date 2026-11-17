// OversolvedStepCaf: a STEP read that keeps part colours and names.
//
// The XCAF classes are not bound generically: their members trade in labels,
// label sequences, attribute handles and GUIDs the kernel never needs. This
// class owns the CAF reader and its document, and hands JS only what the
// kernel reads: the shape, the transfer map (through the same Model and WS the
// plain STEPControl_Reader exposes, so face -> `#N` naming is unchanged), and
// per-face and per-solid colours and names as plain arrays.
#include <emscripten/bind.h>
#include <emscripten/val.h>

#include <IFSelect_ReturnStatus.hxx>
#include <Interface_InterfaceModel.hxx>
#include <Message_ProgressRange.hxx>
#include <NCollection_DataMap.hxx>
#include <NCollection_IndexedDataMap.hxx>
#include <NCollection_Sequence.hxx>
#include <Quantity_Color.hxx>
#include <STEPCAFControl_Reader.hxx>
#include <STEPCAFControl_Writer.hxx>
#include <STEPControl_Reader.hxx>
#include <TCollection_AsciiString.hxx>
#include <TCollection_ExtendedString.hxx>
#include <TDF_Label.hxx>
#include <TDataStd_Name.hxx>
#include <TDocStd_Document.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_ShapeMapHasher.hxx>
#include <TopoDS_Iterator.hxx>
#include <TopoDS_Shape.hxx>
#include <UnitsMethods_LengthUnit.hxx>
#include <XCAFDoc_ColorTool.hxx>
#include <XCAFDoc_ColorType.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <XCAFPrs.hxx>
#include <XCAFPrs_Style.hxx>
#include <XSControl_WorkSession.hxx>

#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

using namespace emscripten;

namespace
{

using StyleMap = NCollection_IndexedDataMap<TopoDS_Shape, XCAFPrs_Style, TopTools_ShapeMapHasher>;
using NameMap  = NCollection_DataMap<TopoDS_Shape, std::string, TopTools_ShapeMapHasher>;
using ColorMap = NCollection_DataMap<TopoDS_Shape, int, TopTools_ShapeMapHasher>;

constexpr int THE_NO_COLOR = -1;

// A fresh XCAF document. XCAFDoc_DocumentTool::Set is what XCAFApp_Application
// does to a new document; calling it directly keeps the application singleton,
// which would hold every document until closed, out of the picture.
opencascade::handle<TDocStd_Document> newXcafDocument()
{
  opencascade::handle<TDocStd_Document> aDoc = new TDocStd_Document("XmlXCAF");
  XCAFDoc_DocumentTool::Set(aDoc->Main(), false);
  // The plain reader the kernel used before scales to millimetre; the CAF
  // reader takes its unit from the document, so the two read the same sizes.
  XCAFDoc_DocumentTool::SetLengthUnit(aDoc, 1.0, UnitsMethods_LengthUnit_Millimeter);
  return aDoc;
}

// 0xRRGGBB from the colour's sRGB values. Red()/Green()/Blue() are linear RGB
// since OCCT 7.5, which would turn a file's #808080 into about #373737.
int toHex(const Quantity_Color& theColor)
{
  double aRgb[3] = {0.0, 0.0, 0.0};
  theColor.Values(aRgb[0], aRgb[1], aRgb[2], Quantity_TOC_sRGB);
  int aHex = 0;
  for (double aChannel : aRgb)
  {
    const int aByte = std::clamp(static_cast<int>(std::lround(aChannel * 255.0)), 0, 255);
    aHex            = (aHex << 8) | aByte;
  }
  return aHex;
}

Quantity_Color fromHex(int theHex)
{
  return Quantity_Color(((theHex >> 16) & 0xFF) / 255.0,
                        ((theHex >> 8) & 0xFF) / 255.0,
                        (theHex & 0xFF) / 255.0,
                        Quantity_TOC_sRGB);
}

// A shape's own colour in the collected styles: surface first, then curve. A
// ColorGen label sets both, so this is ColorSurf, ColorGen, ColorCurv in turn.
int ownColor(const StyleMap& theStyles, const TopoDS_Shape& theShape)
{
  const XCAFPrs_Style* aStyle = theStyles.Seek(theShape);
  if (aStyle == nullptr)
  {
    return THE_NO_COLOR;
  }
  if (aStyle->IsSetColorSurf())
  {
    return toHex(aStyle->GetColorSurf());
  }
  if (aStyle->IsSetColorCurv())
  {
    return toHex(aStyle->GetColorCurv());
  }
  return THE_NO_COLOR;
}

std::string labelName(const TDF_Label& theLabel)
{
  opencascade::handle<TDataStd_Name> aName;
  if (!theLabel.FindAttribute(TDataStd_Name::GetID(), aName))
  {
    return {};
  }
  // A zero replacement character converts to UTF-8 instead of replacing.
  return TCollection_AsciiString(aName->Get()).ToCString();
}

// The names of a label tree keyed by located shape, as CollectStyleSettings
// keys styles: a reference is located by its instance, and its referred
// product's name comes first, so it wins over the instance's own.
void collectNames(const TDF_Label& theLabel, const TopLoc_Location& theLoc, NameMap& theNames)
{
  TDF_Label aReferred;
  if (XCAFDoc_ShapeTool::GetReferredShape(theLabel, aReferred))
  {
    collectNames(aReferred, theLoc.Multiplied(XCAFDoc_ShapeTool::GetLocation(theLabel)), theNames);
  }
  NCollection_Sequence<TDF_Label> aComponents;
  if (XCAFDoc_ShapeTool::GetComponents(theLabel, aComponents))
  {
    for (NCollection_Sequence<TDF_Label>::Iterator anIter(aComponents); anIter.More(); anIter.Next())
    {
      collectNames(anIter.Value(), theLoc, theNames);
    }
  }
  const std::string aName = labelName(theLabel);
  if (aName.empty())
  {
    return;
  }
  TopoDS_Shape aShape = XCAFDoc_ShapeTool::GetShape(theLabel);
  if (aShape.IsNull())
  {
    return;
  }
  // An instance's own name lives at its located shape, not at the parent's
  // location, so compose the label's location before keying the name.
  aShape.Move(theLoc.Multiplied(XCAFDoc_ShapeTool::GetLocation(theLabel)), false);
  if (!theNames.IsBound(aShape))
  {
    theNames.Bind(aShape, aName);
  }
}

// Walks the shape top down so that each face and solid inherits the nearest
// colour and name above it: its own, else its shell's, its solid's, the part's,
// the instance's, the assembly's. The iterator composes locations, so each
// sub-shape matches the located keys the collectors produced.
struct Inheritance
{
  StyleMap Styles;
  NameMap  Names;
  ColorMap FaceColor;
  ColorMap SolidColor;
  NameMap  SolidName;

  void walk(const TopoDS_Shape& theShape, int theColor, const std::string& theName)
  {
    const int aOwnColor = ownColor(Styles, theShape);
    const int aColor    = aOwnColor != THE_NO_COLOR ? aOwnColor : theColor;
    const std::string* aOwnName = Names.Seek(theShape);
    const std::string& aName    = aOwnName != nullptr ? *aOwnName : theName;
    switch (theShape.ShapeType())
    {
      case TopAbs_FACE:
        // A face shared by two parents keeps the colour of the first, which is
        // the one TopExp_Explorer would reach first as well.
        if (!FaceColor.IsBound(theShape))
        {
          FaceColor.Bind(theShape, aColor);
        }
        return;
      case TopAbs_SOLID:
        if (!SolidColor.IsBound(theShape))
        {
          SolidColor.Bind(theShape, aColor);
          SolidName.Bind(theShape, aName);
        }
        break;
      default:
        break;
    }
    for (TopoDS_Iterator anIter(theShape); anIter.More(); anIter.Next())
    {
      walk(anIter.Value(), aColor, aName);
    }
  }
};

val toInt32Array(const std::vector<int>& theValues)
{
  // A copy owned by JS: a view into wasm memory would dangle on memory growth.
  val anArray = val::global("Int32Array").new_(theValues.size());
  anArray.call<void>("set", val(typed_memory_view(theValues.size(), theValues.data())));
  return anArray;
}

class OversolvedStepCaf
{
public:
  OversolvedStepCaf()
  {
    // Only what the kernel reads: fewer modes is a faster transfer.
    myReader.SetColorMode(true);
    myReader.SetNameMode(true);
    myReader.SetLayerMode(false);
    myReader.SetPropsMode(false);
    myReader.SetGDTMode(false);
    myReader.SetMatMode(false);
    myReader.SetViewMode(false);
  }

  IFSelect_ReturnStatus ReadFile(const std::string& thePath)
  {
    return myReader.ReadFile(thePath.c_str());
  }

  bool Transfer()
  {
    myDoc = newXcafDocument();
    return myReader.Transfer(myDoc, Message_ProgressRange());
  }

  // The inner reader's results after the CAF transfer: the same shape and
  // transfer map a plain STEPControl_Reader gives, so the kernel walks them as
  // it always has.
  TopoDS_Shape OneShape() { return myReader.ChangeReader().OneShape(); }

  opencascade::handle<Interface_InterfaceModel> Model() { return myReader.ChangeReader().Model(); }

  opencascade::handle<XSControl_WorkSession> WS() { return myReader.ChangeReader().WS(); }

  // Per face in TopExp_Explorer order its effective colour, per solid its own
  // colour, else the nearest owner's: 0xRRGGBB, or -1 for none. The shape is
  // OneShape() or a sub-shape of it.
  val Colors(const TopoDS_Shape& theShape)
  {
    Inheritance anInheritance = inherit(true, false);
    val         aResult       = val::object();
    aResult.set("faceColor",
                toInt32Array(lookup(theShape, TopAbs_FACE, anInheritance.FaceColor, THE_NO_COLOR)));
    aResult.set("solidColor",
                toInt32Array(lookup(theShape, TopAbs_SOLID, anInheritance.SolidColor, THE_NO_COLOR)));
    return aResult;
  }

  // Per solid in TopExp_Explorer order the name of its product (else of the
  // nearest named owner) as OCCT decodes it, in UTF-8; empty for none.
  val Names(const TopoDS_Shape& theShape)
  {
    Inheritance anInheritance = inherit(false, true);
    val         aResult       = val::array();
    for (const std::string& aName :
         lookup(theShape, TopAbs_SOLID, anInheritance.SolidName, std::string()))
    {
      aResult.call<void>("push", aName);
    }
    return aResult;
  }

  // Test support for smoke, not consumer API: writes theShape as one part with
  // theSolidColor on it and theFaceColor on its theFaceIndex-th face (1-based,
  // TopExp_Explorer order, 0 for none), named theName. Colours are 0xRRGGBB,
  // -1 for none.
  static bool WriteColoredForTest(const TopoDS_Shape& theShape,
                                  const std::string&  thePath,
                                  int                 theSolidColor,
                                  int                 theFaceIndex,
                                  int                 theFaceColor,
                                  const std::string&  theName)
  {
    opencascade::handle<TDocStd_Document>  aDoc       = newXcafDocument();
    opencascade::handle<XCAFDoc_ShapeTool> aShapeTool = XCAFDoc_DocumentTool::ShapeTool(aDoc->Main());
    opencascade::handle<XCAFDoc_ColorTool> aColorTool = XCAFDoc_DocumentTool::ColorTool(aDoc->Main());
    const TDF_Label aPart = aShapeTool->AddShape(theShape, false);
    if (!theName.empty())
    {
      TDataStd_Name::Set(aPart, TCollection_ExtendedString(theName.c_str(), true));
    }
    if (theSolidColor != THE_NO_COLOR)
    {
      aColorTool->SetColor(aPart, fromHex(theSolidColor), XCAFDoc_ColorGen);
    }
    if (theFaceIndex > 0 && theFaceColor != THE_NO_COLOR)
    {
      int anIndex = 0;
      for (TopExp_Explorer anExp(theShape, TopAbs_FACE); anExp.More(); anExp.Next())
      {
        if (++anIndex == theFaceIndex)
        {
          const TDF_Label aFace = aShapeTool->AddSubShape(aPart, anExp.Current());
          if (aFace.IsNull())
          {
            return false;
          }
          aColorTool->SetColor(aFace, fromHex(theFaceColor), XCAFDoc_ColorSurf);
          break;
        }
      }
      if (anIndex != theFaceIndex)
      {
        return false;
      }
    }
    STEPCAFControl_Writer aWriter;
    aWriter.SetColorMode(true);
    aWriter.SetNameMode(true);
    if (!aWriter.Transfer(aDoc, STEPControl_AsIs, nullptr, Message_ProgressRange()))
    {
      return false;
    }
    return aWriter.Write(thePath.c_str()) == IFSelect_RetDone;
  }

private:
  // The styles and names of every free shape in the document, inherited down
  // OneShape(). XCAFPrs::CollectStyleSettings is OCCT's own style resolution,
  // instance and SHUO overrides included.
  Inheritance inherit(bool theWithStyles, bool theWithNames)
  {
    Inheritance anInheritance;
    if (!myDoc.IsNull())
    {
      NCollection_Sequence<TDF_Label> aFree;
      XCAFDoc_DocumentTool::ShapeTool(myDoc->Main())->GetFreeShapes(aFree);
      for (NCollection_Sequence<TDF_Label>::Iterator anIter(aFree); anIter.More(); anIter.Next())
      {
        if (theWithStyles)
        {
          XCAFPrs::CollectStyleSettings(anIter.Value(), TopLoc_Location(), anInheritance.Styles);
        }
        if (theWithNames)
        {
          collectNames(anIter.Value(), TopLoc_Location(), anInheritance.Names);
        }
      }
    }
    const TopoDS_Shape aRoot = OneShape();
    if (!aRoot.IsNull())
    {
      anInheritance.walk(aRoot, THE_NO_COLOR, std::string());
    }
    return anInheritance;
  }

  template <typename T>
  static std::vector<T> lookup(const TopoDS_Shape&                                    theShape,
                               TopAbs_ShapeEnum                                       theType,
                               const NCollection_DataMap<TopoDS_Shape, T, TopTools_ShapeMapHasher>& theMap,
                               const T&                                               theNone)
  {
    std::vector<T> aValues;
    if (theShape.IsNull())
    {
      return aValues;
    }
    for (TopExp_Explorer anExp(theShape, theType); anExp.More(); anExp.Next())
    {
      const T* aValue = theMap.Seek(anExp.Current());
      aValues.push_back(aValue != nullptr ? *aValue : theNone);
    }
    return aValues;
  }

  STEPCAFControl_Reader                 myReader;
  opencascade::handle<TDocStd_Document> myDoc;
};

}  // namespace
EMSCRIPTEN_BINDINGS(oversolved_caf)
{
  class_<OversolvedStepCaf>("OversolvedStepCaf")
    .constructor<>()
    .function("ReadFile", &OversolvedStepCaf::ReadFile)
    .function("Transfer", &OversolvedStepCaf::Transfer)
    .function("OneShape", &OversolvedStepCaf::OneShape)
    .function("Model", &OversolvedStepCaf::Model)
    .function("WS", &OversolvedStepCaf::WS)
    .function("Colors", &OversolvedStepCaf::Colors)
    .function("Names", &OversolvedStepCaf::Names)
    .class_function("WriteColoredForTest", &OversolvedStepCaf::WriteColoredForTest);
}
