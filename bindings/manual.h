// Members registered on a generated class but written by hand.
//
// A class can only be registered once, so a member that the AST cannot supply
// cannot live in a binding block of its own. manual.cpp implements these and
// scripts/bindgen.py registers them on the class through MANUAL_MEMBERS.
#ifndef OCCT_MANUAL_MEMBERS_H
#define OCCT_MANUAL_MEMBERS_H

#include <optional>

#include <Standard_Handle.hxx>
#include <TopoDS_Shape.hxx>

class Interface_InterfaceModel;
class Standard_Transient;
class Transfer_Binder;

namespace occt_manual
{

// TopoDS_Shape::HashCode(int), which OCCT 7.8 replaced with std::hash: a value
// in [1, theUpperBound] computed from the TShape and the location.
int ShapeHashCode(const TopoDS_Shape& theShape, int theUpperBound);

// The shape a transfer binder holds, or nothing for any other binder kind.
std::optional<TopoDS_Shape> BinderShape(const Transfer_Binder& theBinder);

// StepData_StepModel::IdentLabel, reached through the base model the reader
// hands back: the entity's `#N` label, 0 when it has none.
int ModelIdentLabel(const Interface_InterfaceModel& theModel,
                    const opencascade::handle<Standard_Transient>& theEntity);

} // namespace occt_manual

#endif // OCCT_MANUAL_MEMBERS_H
