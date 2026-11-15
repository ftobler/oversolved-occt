// Hand-written bindings for the template instantiations the kernel uses, and
// for the members registered on a generated class (see manual.h).
//
// The generator walks plain classes only. The JS names keep the classic
// TColgp/TColStd/TopTools aliases (OCCT 8 moved those typedefs to Deprecated/)
// and BRepLProp_SLProps (now an alias of GeomLProp_SLPropsBase), and the
// members keep the suffixes the kernel already calls: First_1 is the const
// First, Append_1 the copying one.
#include <emscripten/bind.h>

#include <BRepAdaptor_Surface.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRepOffset_Interval.hxx>
#include <NCollection_Array1.hxx>
#include <NCollection_List.hxx>
#include <StepData_StepModel.hxx>
#include <TopoDS_Shape.hxx>
#include <Transfer_Binder.hxx>
#include <TransferBRep_BinderOfShape.hxx>
#include <gp_Pnt.hxx>

#include <functional>

#include "manual.h"

using namespace emscripten;

namespace {

// A list the kernel fills or drains front to back; the empty one is built as _1.
template <typename T>
struct List : NCollection_List<T>
{
};

template <typename T>
void bindList(const char* name, const char* ctorName)
{
  using L = NCollection_List<T>;
  class_<List<T>, base<L>>(ctorName).template constructor<>();
  class_<L>(name)
    .function("Size", optional_override([](const L& self) { return self.Size(); }))
    .function("First_1", optional_override([](const L& self) { return self.First(); }))
    .function("Append_1", optional_override([](L& self, const T& item) { self.Append(item); }))
    .function("RemoveFirst", optional_override([](L& self) { self.RemoveFirst(); }))
    .function("Clear", optional_override([](L& self) { self.Clear(); }));
}

// A 1-based fixed array; only the (lower, upper) constructor is bound, as _2.
template <typename T>
struct Array1 : NCollection_Array1<T>
{
  Array1(int lower, int upper) : NCollection_Array1<T>(lower, upper) {}
};

template <typename T>
void bindArray1(const char* name, const char* ctorName)
{
  using A = NCollection_Array1<T>;
  class_<A>(name)
    .function("SetValue", optional_override([](A& self, int i, const T& v) { self.SetValue(i, v); }))
    .function("Value", optional_override([](const A& self, int i) { return self.Value(i); }))
    .function("Length", optional_override([](const A& self) { return self.Length(); }))
    .function("Lower", optional_override([](const A& self) { return self.Lower(); }))
    .function("Upper", optional_override([](const A& self) { return self.Upper(); }));
  class_<Array1<T>, base<A>>(ctorName).template constructor<int, int>();
}

// Surface properties at (u, v); only the full constructor is bound, as _1.
struct SLProps : BRepLProp_SLProps
{
  SLProps(const BRepAdaptor_Surface& s, double u, double v, int n, double resolution)
      : BRepLProp_SLProps(s, u, v, n, resolution)
  {
  }
};

} // namespace

namespace occt_manual
{

// The 7.7 contract: TShape and location, orientation ignored, in [1, upper].
int ShapeHashCode(const TopoDS_Shape& theShape, int theUpperBound)
{
  const std::size_t aBound = theUpperBound > 0 ? static_cast<std::size_t>(theUpperBound) : 1;
  return static_cast<int>(std::hash<TopoDS_Shape>{}(theShape) % aBound) + 1;
}

std::optional<TopoDS_Shape> BinderShape(const Transfer_Binder& theBinder)
{
  // MapItem hands every binder back as Transfer_Binder; only a shape binder
  // has a result the kernel can read.
  const auto* aShapeBinder = dynamic_cast<const TransferBRep_BinderOfShape*>(&theBinder);
  if (aShapeBinder == nullptr)
  {
    return {};
  }
  return aShapeBinder->Result();
}

int ModelIdentLabel(const Interface_InterfaceModel& theModel,
                    const opencascade::handle<Standard_Transient>& theEntity)
{
  // The reader knows the model as Interface_InterfaceModel; STEP labels live
  // on StepData_StepModel.
  const auto* aStepModel = dynamic_cast<const StepData_StepModel*>(&theModel);
  return aStepModel == nullptr ? 0 : aStepModel->IdentLabel(theEntity);
}

} // namespace occt_manual

EMSCRIPTEN_BINDINGS(occt_manual)
{
  // Result is bound as std::optional<TopoDS_Shape>: undefined for a binder of
  // another kind, an owned shape copy for a shape binder.
  register_optional<TopoDS_Shape>();
  class_<BRepLProp_SLProps>("BRepLProp_SLProps")
    .function("IsNormalDefined", optional_override([](BRepLProp_SLProps& self) { return self.IsNormalDefined(); }))
    .function("Normal", optional_override([](BRepLProp_SLProps& self) { return self.Normal(); }));
  class_<SLProps, base<BRepLProp_SLProps>>("BRepLProp_SLProps_1")
    .constructor<const BRepAdaptor_Surface&, double, double, int, double>();
  bindList<TopoDS_Shape>("TopTools_ListOfShape", "TopTools_ListOfShape_1");
  bindList<BRepOffset_Interval>("BRepOffset_ListOfInterval", "BRepOffset_ListOfInterval_1");
  bindArray1<gp_Pnt>("TColgp_Array1OfPnt", "TColgp_Array1OfPnt_2");
  bindArray1<double>("TColStd_Array1OfReal", "TColStd_Array1OfReal_2");
  bindArray1<int>("TColStd_Array1OfInteger", "TColStd_Array1OfInteger_2");
}
