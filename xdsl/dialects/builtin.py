from __future__ import annotations

import math
import struct
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum
from math import prod
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    TypeAlias,
    cast,
    overload,
)

from immutabledict import immutabledict
from typing_extensions import Self, TypeVar, deprecated

from xdsl.ir import (
    Attribute,
    AttributeCovT,
    AttributeInvT,
    Block,
    BlockOps,
    BuiltinAttribute,
    Data,
    Dialect,
    Operation,
    ParametrizedAttribute,
    Region,
    SSAValue,
    TypeAttribute,
    TypedAttribute,
)
from xdsl.ir.affine import (
    AffineConstantExpr,
    AffineDimExpr,
    AffineMap,
    AffineSet,
    AffineSymExpr,
)
from xdsl.irdl import (
    AnyAttr,
    AnyOf,
    AttrConstraint,
    BaseAttr,
    ConstraintContext,
    GenericAttrConstraint,
    GenericData,
    GenericRangeConstraint,
    IntConstraint,
    IRDLAttrConstraint,
    IRDLGenericAttrConstraint,
    IRDLOperation,
    MessageConstraint,
    ParamAttrConstraint,
    ParameterDef,
    RangeOf,
    attr_constr_coercion,
    base,
    irdl_attr_definition,
    irdl_op_definition,
    irdl_to_attr_constraint,
    opt_prop_def,
    region_def,
    traits_def,
    var_operand_def,
    var_result_def,
)
from xdsl.traits import (
    IsolatedFromAbove,
    NoMemoryEffect,
    NoTerminator,
    OptionalSymbolOpInterface,
    OpTrait,
    SymbolTable,
)
from xdsl.utils.comparisons import (
    signed_upper_bound,
    signed_value_range,
    signless_value_range,
    unsigned_upper_bound,
    unsigned_value_range,
)
from xdsl.utils.exceptions import DiagnosticException, VerifyException
from xdsl.utils.hints import isa

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer, WriteableBuffer

    from xdsl.parser import AttrParser, Parser
    from xdsl.printer import Printer


DYNAMIC_INDEX = -1
"""
A constant value denoting a dynamic index in a shape.
"""


class ShapedType(Attribute, ABC):
    @abstractmethod
    def get_num_dims(self) -> int: ...

    @abstractmethod
    def get_shape(self) -> tuple[int, ...]: ...

    def element_count(self) -> int:
        return prod(self.get_shape())

    @staticmethod
    def strides_for_shape(shape: Sequence[int], factor: int = 1) -> tuple[int, ...]:
        import operator
        from itertools import accumulate

        return tuple(accumulate(reversed(shape), operator.mul, initial=factor))[-2::-1]


_ContainerElementTypeT = TypeVar(
    "_ContainerElementTypeT", bound=Attribute | None, covariant=True
)


class ContainerType(Generic[_ContainerElementTypeT], ABC):
    @abstractmethod
    def get_element_type(self) -> _ContainerElementTypeT:
        pass


@irdl_attr_definition
class NoneAttr(ParametrizedAttribute, BuiltinAttribute):
    """An attribute representing the absence of an attribute."""

    name = "none"


@irdl_attr_definition
class ArrayAttr(
    GenericData[tuple[AttributeCovT, ...]], BuiltinAttribute, Iterable[AttributeCovT]
):
    name = "array"

    def __init__(self, param: Iterable[AttributeCovT]) -> None:
        super().__init__(tuple(param))

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> tuple[AttributeCovT, ...]:
        with parser.in_angle_brackets():
            data = parser.parse_comma_separated_list(
                parser.Delimiter.SQUARE, parser.parse_attribute
            )
            # the type system can't ensure that the elements are of type _ArrayAttrT
            result = cast(tuple[AttributeCovT, ...], tuple(data))
            return result

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string("[")
        printer.print_list(self.data, printer.print_attribute)
        printer.print_string("]")

    @staticmethod
    def generic_constraint_coercion(args: tuple[Any]) -> AttrConstraint:
        assert len(args) == 1
        return ArrayOfConstraint(RangeOf(irdl_to_attr_constraint(args[0])))

    def __len__(self):
        return len(self.data)

    def __iter__(self) -> Iterator[AttributeCovT]:
        return iter(self.data)

    @staticmethod
    def constr(
        constr: (
            IRDLGenericAttrConstraint[AttributeInvT]
            | GenericRangeConstraint[AttributeInvT]
        ),
    ) -> GenericAttrConstraint[ArrayAttr[AttributeInvT]]:
        return ArrayOfConstraint(constr)


@dataclass(frozen=True)
class ArrayOfConstraint(GenericAttrConstraint[ArrayAttr[AttributeCovT]]):
    elem_range_constraint: GenericRangeConstraint[AttributeCovT]
    """
    A constraint that enforces an ArrayData whose elements satisfy
    the underlying range constraint.
    """

    def __init__(
        self,
        constr: (
            IRDLGenericAttrConstraint[Attribute] | GenericRangeConstraint[AttributeCovT]
        ),
    ):
        if isinstance(constr, GenericRangeConstraint):
            object.__setattr__(self, "elem_range_constraint", constr)
        else:
            object.__setattr__(
                self, "elem_range_constraint", RangeOf(irdl_to_attr_constraint(constr))
            )

    def verify(
        self,
        attr: Attribute,
        constraint_context: ConstraintContext,
    ) -> None:
        if not isa(attr, ArrayAttr):
            raise VerifyException(
                f"expected ArrayAttr attribute, but got '{type(attr)}'"
            )
        self.elem_range_constraint.verify(attr.data, constraint_context)

    def can_infer(self, var_constraint_names: AbstractSet[str]) -> bool:
        return self.elem_range_constraint.can_infer(
            var_constraint_names, length_known=False
        )

    def infer(self, context: ConstraintContext) -> ArrayAttr[AttributeCovT]:
        return ArrayAttr(self.elem_range_constraint.infer(context, length=None))

    def get_bases(self) -> set[type[Attribute]] | None:
        return {ArrayAttr}

    def variables(self) -> set[str]:
        return self.elem_range_constraint.variables()


@irdl_attr_definition
class StringAttr(Data[str], BuiltinAttribute):
    name = "string"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> str:
        with parser.in_angle_brackets():
            return parser.parse_str_literal()

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string(f'"{self.data}"')


@irdl_attr_definition
class BytesAttr(Data[bytes], BuiltinAttribute):
    name = "bytes"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> bytes:
        with parser.in_angle_brackets():
            return parser.parse_bytes_literal()

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string(f'"{self.data}"')


@irdl_attr_definition
class SymbolNameAttr(ParametrizedAttribute, BuiltinAttribute):
    name = "symbol_name"
    data: ParameterDef[StringAttr]

    def __init__(self, data: str | StringAttr) -> None:
        if isinstance(data, str):
            data = StringAttr(data)
        super().__init__([data])


@irdl_attr_definition
class SymbolRefAttr(ParametrizedAttribute, BuiltinAttribute):
    name = "symbol_ref"
    root_reference: ParameterDef[StringAttr]
    nested_references: ParameterDef[ArrayAttr[StringAttr]]

    def __init__(
        self,
        root: str | StringAttr,
        nested: Sequence[str] | Sequence[StringAttr] | ArrayAttr[StringAttr] = [],
    ) -> None:
        if isinstance(root, str):
            root = StringAttr(root)
        if not isinstance(nested, ArrayAttr):
            nested = ArrayAttr(
                [StringAttr(x) if isinstance(x, str) else x for x in nested]
            )
        super().__init__([root, nested])

    def string_value(self):
        root = self.root_reference.data
        for ref in self.nested_references.data:
            root += "." + ref.data
        return root


class EmptyArrayAttrConstraint(AttrConstraint):
    """
    Constrain attribute to be empty ArrayData
    """

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        if not isinstance(attr, ArrayAttr):
            raise VerifyException(f"expected ArrayData attribute, but got {attr}")
        attr = cast(ArrayAttr[Attribute], attr)
        if attr.data:
            raise VerifyException(f"expected empty array, but got {attr}")


FlatSymbolRefAttrConstraint = MessageConstraint(
    ParamAttrConstraint(SymbolRefAttr, [AnyAttr(), EmptyArrayAttrConstraint()]),
    "Unexpected nested symbols in FlatSymbolRefAttr.",
)
"""Constrain SymbolRef to be FlatSymbolRef"""

FlatSymbolRefAttr = Annotated[SymbolRefAttr, FlatSymbolRefAttrConstraint]
"""SymbolRef constrained to have an empty `nested_references` property."""

FlatSymbolRefAttrConstr = base(SymbolRefAttr) & FlatSymbolRefAttrConstraint


@irdl_attr_definition
class IntAttr(Data[int]):
    name = "builtin.int"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> int:
        with parser.in_angle_brackets():
            data = parser.parse_integer()
            return data

    def print_parameter(self, printer: Printer) -> None:
        with printer.in_angle_brackets():
            printer.print_string(f"{self.data}")

    def __bool__(self) -> bool:
        """Returns True if value is non-zero."""
        return bool(self.data)


@dataclass(frozen=True)
class IntAttrConstraint(GenericAttrConstraint[IntAttr]):
    """
    Constrains the value of an IntAttr.
    """

    int_constraint: IntConstraint

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        if not isinstance(attr, IntAttr):
            raise VerifyException(f"attribute {attr} expected to be an IntAttr")
        self.int_constraint.verify(attr.data, constraint_context)

    def variables(self) -> set[str]:
        return self.int_constraint.variables()

    def can_infer(self, var_constraint_names: AbstractSet[str]) -> bool:
        return self.int_constraint.can_infer(var_constraint_names)

    def infer(self, context: ConstraintContext) -> IntAttr:
        return IntAttr(self.int_constraint.infer(context))

    def get_bases(self) -> set[type[Attribute]] | None:
        return {IntAttr}


class Signedness(Enum):
    "Signedness semantics for integer"

    SIGNLESS = 0
    "No signedness semantics"

    SIGNED = 1
    UNSIGNED = 2

    def value_range(self, bitwidth: int) -> tuple[int, int]:
        """
        For a given bitwidth, returns (min, max+1), where min and max are the smallest and
        largest representable values.

        Signless integers are bit patterns, so the representable range is the union of the
        signed and unsigned representable ranges.
        """
        match self:
            case Signedness.SIGNLESS:
                return signless_value_range(bitwidth)
            case Signedness.SIGNED:
                return signed_value_range(bitwidth)
            case Signedness.UNSIGNED:
                return unsigned_value_range(bitwidth)


@irdl_attr_definition
class SignednessAttr(Data[Signedness]):
    name = "builtin.signedness"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> Signedness:
        with parser.in_angle_brackets():
            if parser.parse_optional_keyword("signless") is not None:
                return Signedness.SIGNLESS
            if parser.parse_optional_keyword("signed") is not None:
                return Signedness.SIGNED
            if parser.parse_optional_keyword("unsigned") is not None:
                return Signedness.UNSIGNED
            parser.raise_error("`signless`, `signed`, or `unsigned` expected")

    def print_parameter(self, printer: Printer) -> None:
        with printer.in_angle_brackets():
            data = self.data
            if data == Signedness.SIGNLESS:
                printer.print_string("signless")
            elif data == Signedness.SIGNED:
                printer.print_string("signed")
            elif data == Signedness.UNSIGNED:
                printer.print_string("unsigned")
            else:
                raise ValueError(f"Invalid signedness {data}")


class CompileTimeFixedBitwidthType(TypeAttribute, ABC):
    """
    A type attribute whose runtime bitwidth is fixed, but may be target-dependent.
    """

    name = "abstract.compile_time_fixed_bitwidth_type"

    @property
    @abstractmethod
    def compile_time_size(self) -> int:
        """
        Contiguous memory footprint of the value during compilation.
        """
        raise NotImplementedError()


class FixedBitwidthType(CompileTimeFixedBitwidthType, ABC):
    """
    A type attribute whose runtime bitwidth is target-independent.
    """

    name = "abstract.fixed_bitwidth_type"

    @property
    @abstractmethod
    def bitwidth(self) -> int:
        """
        Contiguous memory footprint in bits
        """
        raise NotImplementedError()

    @property
    def size(self) -> int:
        """
        Contiguous memory footprint in bytes, defaults to `ceil(bitwidth / 8)`
        """
        return (self.bitwidth + 7) >> 3


_PyT = TypeVar("_PyT")


class PackableType(Generic[_PyT], CompileTimeFixedBitwidthType, ABC):
    """
    Abstract base class for xDSL types whose values can be encoded and decoded as bytes.
    """

    @abstractmethod
    def iter_unpack(self, buffer: ReadableBuffer, /) -> Iterator[_PyT]:
        """
        Yields unpacked values one at a time, starting at the beginning of the buffer.
        """
        raise NotImplementedError()

    @abstractmethod
    def unpack(self, buffer: ReadableBuffer, num: int, /) -> tuple[_PyT, ...]:
        """
        Unpack `num` values from the beginning of the buffer.
        """
        raise NotImplementedError()

    @abstractmethod
    def pack_into(self, buffer: WriteableBuffer, offset: int, value: _PyT) -> None:
        """
        Pack a value at a given offset into a buffer.
        """
        raise NotImplementedError()

    @abstractmethod
    def pack(self, values: Sequence[_PyT]) -> bytes:
        """
        Create a new buffer containing the input `values`.
        """
        raise NotImplementedError()


class StructPackableType(Generic[_PyT], PackableType[_PyT], ABC):
    """
    Abstract base class for xDSL types that can be packed and unpacked using Python's
    `struct` package, using a format string.
    """

    @property
    @abstractmethod
    def format(self) -> str:
        """
        Format to be used when decoding and encoding bytes.

        See external [documentation](https://docs.python.org/3/library/struct.html).
        """
        raise NotImplementedError()

    def iter_unpack(self, buffer: ReadableBuffer, /) -> Iterator[_PyT]:
        return (values[0] for values in struct.iter_unpack(self.format, buffer))

    def unpack(self, buffer: ReadableBuffer, num: int, /) -> tuple[_PyT, ...]:
        fmt = self.format[0] + str(num) + self.format[1:]
        return struct.unpack(fmt, buffer)

    def pack_into(self, buffer: WriteableBuffer, offset: int, value: _PyT) -> None:
        struct.pack_into(self.format, buffer, offset, value)

    def pack(self, values: Sequence[_PyT]) -> bytes:
        fmt = self.format[0] + str(len(values)) + self.format[1:]
        return struct.pack(fmt, *values)

    @property
    def compile_time_size(self) -> int:
        return struct.calcsize(self.format)


_SIGNED_INTEGER_FORMATS = ("<b", "<h", "<i", "<i", "<q", "<q", "<q", "<q")
"""
Formats for the struct module to use to process signed and signless integers.
Bitwidths: `<b`: 1-8, `<h`: 9-16, `<i`: 17-32, `<q`: 33-64.
"""
_UNSIGNED_INTEGER_FORMATS = ("<B", "<H", "<I", "<I", "<Q", "<Q", "<Q", "<Q")
"""
Formats for the struct module to use to process unsigned integers.
Bitwidths: `<B`: 1-8, `<H`: 9-16, `<I`: 17-32, `<Q`: 33-64.
"""


@irdl_attr_definition
class IntegerType(
    ParametrizedAttribute, StructPackableType[int], FixedBitwidthType, BuiltinAttribute
):
    name = "integer_type"
    width: ParameterDef[IntAttr]
    signedness: ParameterDef[SignednessAttr]

    def __init__(
        self,
        data: int | IntAttr,
        signedness: Signedness | SignednessAttr = Signedness.SIGNLESS,
    ) -> None:
        if isinstance(data, int):
            data = IntAttr(data)
        if isinstance(signedness, Signedness):
            signedness = SignednessAttr(signedness)
        super().__init__([data, signedness])

    def __repr__(self):
        width = self.width.data
        signedness = self.signedness.data
        if signedness == Signedness.SIGNLESS:
            return f"IntegerType({width})"
        else:
            return f"IntegerType({width}, {signedness})"

    def verify(self):
        if self.width.data < 0:
            raise VerifyException(
                f"integer type bitwidth should be nonnegative (got {self.width.data})"
            )

    def value_range(self) -> tuple[int, int]:
        return self.signedness.data.value_range(self.width.data)

    def verify_value(self, value: int):
        min_value, max_value = self.value_range()

        if not (min_value <= value < max_value):
            raise VerifyException(
                f"Integer value {value} is out of range for type {self} which supports "
                f"values in the range [{min_value}, {max_value})"
            )

    def normalized_value(
        self, value: int, *, truncate_bits: bool = False
    ) -> int | None:
        """
        Signless values can represent integers from both the signed and unsigned ranges
        for a given bitwidth.
        We choose to normalize values that are not in the intersection of the two ranges
        to the signed version (meaning ambiguous values will always be negative).
        For example, the bitpattern of all ones will always be represented as `-1` at
        runtime.
        If the input value is outside of the valid range, return `None` if `truncate_bits`
        is false, otherwise returns a value in range by truncating the bits of the input.
        """
        min_value, max_value = self.value_range()
        if not (min_value <= value < max_value):
            if not truncate_bits:
                return None
            value = value % (2**self.bitwidth)

        if self.signedness.data != Signedness.UNSIGNED:
            signed_ub = signed_upper_bound(self.bitwidth)
            unsigned_ub = unsigned_upper_bound(self.bitwidth)
            if signed_ub <= value:
                return value - unsigned_ub

        return value

    def get_normalized_value(self, value: int) -> int:
        """
        Normalises an integer value similarly to the `normalized_value` function,
        but throws a ValueError when the value falls outside the type's range.
        """
        v = self.normalized_value(value)
        if v is None:
            min_value, max_value = self.value_range()
            raise ValueError(
                f"Integer value {value} is out of range for type {self} which supports "
                f"values in the range [{min_value}, {max_value})"
            )
        return v

    @property
    def bitwidth(self) -> int:
        return self.width.data

    def print_value_without_type(self, value: int, printer: Printer):
        """
        Prints the value, printing `true` or `false` if `self.width.data == 1`.
        """
        if self.width.data == 1:
            printer.print_string("true" if value else "false", indent=0)
        else:
            printer.print_string(f"{value}")

    @property
    def format(self) -> str:
        format_index = ((self.bitwidth + 7) >> 3) - 1  #  = ceil(bw / 8) - 1
        if format_index >= 8:
            raise NotImplementedError(f"Format not implemented for {self}")

        unsigned = self.signedness.data == Signedness.UNSIGNED
        f = _UNSIGNED_INTEGER_FORMATS if unsigned else _SIGNED_INTEGER_FORMATS
        return f[format_index]


i64 = IntegerType(64)
i32 = IntegerType(32)
i16 = IntegerType(16)
i8 = IntegerType(8)
i1 = IntegerType(1)
I64 = Annotated[IntegerType, i64]
I32 = Annotated[IntegerType, i32]
I16 = Annotated[IntegerType, i16]
I8 = Annotated[IntegerType, i8]
I1 = Annotated[IntegerType, i1]

_IntegerTypeInvT = TypeVar("_IntegerTypeInvT", bound=IntegerType, default=IntegerType)

SignlessIntegerConstraint = ParamAttrConstraint(
    IntegerType, [IntAttr, SignednessAttr(Signedness.SIGNLESS)]
)
"""Type constraint for signless IntegerType."""

AnySignlessIntegerType: TypeAlias = Annotated[IntegerType, SignlessIntegerConstraint]
"""Type alias constrained to signless IntegerType."""


@irdl_attr_definition
class UnitAttr(ParametrizedAttribute, BuiltinAttribute):
    name = "unit"


@irdl_attr_definition
class LocationAttr(ParametrizedAttribute, BuiltinAttribute):
    """
    An attribute representing source code location.
    Only supports unknown locations for now.
    """

    name = "loc"


@irdl_attr_definition
class IndexType(ParametrizedAttribute, BuiltinAttribute, StructPackableType[int]):
    name = "index"

    def print_value_without_type(self, value: int, printer: Printer):
        """
        Prints the value.
        """
        printer.print_string(f"{value}")

    @property
    def format(self) -> str:
        # index types are always packable as int64
        return "<q"


IndexTypeConstr = BaseAttr(IndexType)

_IntegerAttrType = TypeVar(
    "_IntegerAttrType",
    bound=IntegerType | IndexType,
    covariant=True,
    default=IntegerType | IndexType,
)
_IntegerAttrTypeInvT = TypeVar("_IntegerAttrTypeInvT", bound=IntegerType | IndexType)
IntegerAttrTypeConstr = IndexTypeConstr | BaseAttr(IntegerType)
AnySignlessIntegerOrIndexType: TypeAlias = Annotated[
    Attribute, AnyOf([IndexType, SignlessIntegerConstraint])
]
"""Type alias constrained to IndexType or signless IntegerType."""


@irdl_attr_definition
class IntegerAttr(
    Generic[_IntegerAttrType],
    BuiltinAttribute,
    TypedAttribute,
):
    name = "integer"
    value: ParameterDef[IntAttr]
    type: ParameterDef[_IntegerAttrType]

    @overload
    def __init__(
        self,
        value: int | IntAttr,
        value_type: _IntegerAttrType,
        *,
        truncate_bits: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self: IntegerAttr[IntegerType],
        value: int | IntAttr,
        value_type: int,
        *,
        truncate_bits: bool = False,
    ) -> None: ...

    def __init__(
        self,
        value: int | IntAttr,
        value_type: int | IntegerType | IndexType,
        *,
        truncate_bits: bool = False,
    ) -> None:
        if isinstance(value_type, int):
            value_type = IntegerType(value_type)
        if isinstance(value, IntAttr):
            value = value.data
        if not isinstance(value_type, IndexType):
            normalized_value = value_type.normalized_value(
                value, truncate_bits=truncate_bits
            )
            if normalized_value is not None:
                value = normalized_value
        super().__init__([IntAttr(value), value_type])

    @staticmethod
    def from_int_and_width(value: int, width: int) -> IntegerAttr[IntegerType]:
        return IntegerAttr(value, width)

    @staticmethod
    def from_index_int_value(value: int) -> IntegerAttr[IndexType]:
        return IntegerAttr(value, IndexType())

    @staticmethod
    def from_bool(value: bool) -> BoolAttr:
        return IntegerAttr(value, 1)

    def verify(self) -> None:
        if isinstance(int_type := self.type, IndexType):
            return

        int_type.verify_value(self.value.data)

    @staticmethod
    def parse_with_type(
        parser: AttrParser,
        type: Attribute,
    ) -> TypedAttribute:
        assert isinstance(type, IntegerType | IndexType)
        return IntegerAttr(parser.parse_integer(allow_boolean=(type == i1)), type)

    def print_without_type(self, printer: Printer):
        self.type.print_value_without_type(self.value.data, printer)

    def get_type(self) -> Attribute:
        return self.type

    @classmethod
    def constr(
        cls,
        *,
        value: AttrConstraint | None = None,
        type: GenericAttrConstraint[_IntegerAttrType] = IntegerAttrTypeConstr,
    ) -> GenericAttrConstraint[IntegerAttr[_IntegerAttrType]]:
        if value is None and type == AnyAttr():
            return BaseAttr[IntegerAttr[_IntegerAttrType]](IntegerAttr)
        return ParamAttrConstraint[IntegerAttr[_IntegerAttrType]](
            IntegerAttr,
            (
                value,
                type,
            ),
        )

    def __bool__(self) -> bool:
        """Returns True if value is non-zero."""
        return bool(self.value)

    @staticmethod
    def iter_unpack(
        type: _IntegerAttrTypeInvT, buffer: ReadableBuffer, /
    ) -> Iterator[IntegerAttr[_IntegerAttrTypeInvT]]:
        """
        Yields unpacked values one at a time, starting at the beginning of the buffer.
        """
        for value in type.iter_unpack(buffer):
            yield IntegerAttr(value, type)

    @staticmethod
    def unpack(
        type: _IntegerAttrTypeInvT, buffer: ReadableBuffer, num: int, /
    ) -> tuple[IntegerAttr[_IntegerAttrTypeInvT], ...]:
        """
        Unpack `num` values from the beginning of the buffer.
        """
        return tuple(IntegerAttr(value, type) for value in type.unpack(buffer, num))


BoolAttr: TypeAlias = IntegerAttr[Annotated[IntegerType, IntegerType(1)]]


class _FloatType(StructPackableType[float], FixedBitwidthType, ABC):
    @property
    @abstractmethod
    def bitwidth(self) -> int:
        raise NotImplementedError()


@irdl_attr_definition
class BFloat16Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "bf16"

    @property
    def bitwidth(self) -> int:
        return 16

    @property
    def format(self) -> str:
        raise NotImplementedError()


@irdl_attr_definition
class Float16Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "f16"

    @property
    def bitwidth(self) -> int:
        return 16

    @property
    def format(self) -> str:
        return "<e"


@irdl_attr_definition
class Float32Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "f32"

    @property
    def bitwidth(self) -> int:
        return 32

    @property
    def format(self) -> str:
        return "<f"


@irdl_attr_definition
class Float64Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "f64"

    @property
    def bitwidth(self) -> int:
        return 64

    @property
    def format(self) -> str:
        return "<d"


@irdl_attr_definition
class Float80Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "f80"

    @property
    def bitwidth(self) -> int:
        return 80

    @property
    def format(self) -> str:
        raise NotImplementedError()


@irdl_attr_definition
class Float128Type(ParametrizedAttribute, BuiltinAttribute, _FloatType):
    name = "f128"

    @property
    def bitwidth(self) -> int:
        return 128

    @property
    def format(self) -> str:
        raise NotImplementedError()


AnyFloat: TypeAlias = (
    BFloat16Type | Float16Type | Float32Type | Float64Type | Float80Type | Float128Type
)
AnyFloatConstr = (
    BaseAttr(BFloat16Type)
    | BaseAttr(Float16Type)
    | BaseAttr(Float32Type)
    | BaseAttr(Float64Type)
    | BaseAttr(Float80Type)
    | BaseAttr(Float128Type)
)


@irdl_attr_definition
class FloatData(Data[float]):
    name = "builtin.float_data"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> float:
        with parser.in_angle_brackets():
            return float(parser.parse_number())

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string(f"{self.data}")

    def __eq__(self, other: object):
        # avoid triggering `float('nan') != float('nan')` inequality
        return isinstance(other, FloatData) and (
            math.isnan(self.data) and math.isnan(other.data) or self.data == other.data
        )

    def __hash__(self):
        return hash(self.data)


_FloatAttrType = TypeVar(
    "_FloatAttrType", bound=AnyFloat, covariant=True, default=AnyFloat
)
_FloatAttrTypeInvT = TypeVar("_FloatAttrTypeInvT", bound=AnyFloat)


@irdl_attr_definition
class FloatAttr(Generic[_FloatAttrType], BuiltinAttribute, TypedAttribute):
    name = "float"

    value: ParameterDef[FloatData]
    type: ParameterDef[_FloatAttrType]

    @overload
    def __init__(self, data: float | FloatData, type: _FloatAttrType) -> None: ...

    @overload
    def __init__(self, data: float | FloatData, type: int) -> None: ...

    def __init__(
        self, data: float | FloatData, type: int | _FloatAttrType | AnyFloat
    ) -> None:
        if isinstance(type, int):
            if type == 16:
                type = Float16Type()
            elif type == 32:
                type = Float32Type()
            elif type == 64:
                type = Float64Type()
            elif type == 80:
                type = Float80Type()
            elif type == 128:
                type = Float128Type()
            else:
                raise ValueError(f"Invalid bitwidth: {type}")

        value: float = data.data if isinstance(data, FloatData) else data
        # for supported types, constrain value to precision of floating point type
        # else, allow full python float precision
        if isinstance(type, Float64Type | Float32Type | Float16Type):
            value = type.unpack(type.pack((value,)), 1)[0]

        data_attr = FloatData(value)

        super().__init__([data_attr, type])

    @staticmethod
    def parse_with_type(
        parser: AttrParser,
        type: Attribute,
    ) -> TypedAttribute:
        assert isinstance(type, AnyFloat)
        return FloatAttr(parser.parse_float(), type)

    def print_without_type(self, printer: Printer):
        return printer.print_float_attr(self)

    @staticmethod
    def iter_unpack(
        type: _FloatAttrTypeInvT, buffer: ReadableBuffer, /
    ) -> Iterator[FloatAttr[_FloatAttrTypeInvT]]:
        """
        Yields unpacked values one at a time, starting at the beginning of the buffer.
        """
        for value in type.iter_unpack(buffer):
            yield FloatAttr(value, type)

    @staticmethod
    def unpack(
        type: _FloatAttrTypeInvT, buffer: ReadableBuffer, num: int, /
    ) -> tuple[FloatAttr[_FloatAttrTypeInvT], ...]:
        """
        Unpack `num` values from the beginning of the buffer.
        """
        return tuple(FloatAttr(value, type) for value in type.unpack(buffer, num))


ComplexElementCovT = TypeVar(
    "ComplexElementCovT",
    bound=IntegerType | AnyFloat,
    default=IntegerType | AnyFloat,
    covariant=True,
)


@irdl_attr_definition
class ComplexType(
    Generic[ComplexElementCovT],
    PackableType[tuple[float, float] | tuple[int, int]],
    ParametrizedAttribute,
    BuiltinAttribute,
    ContainerType[ComplexElementCovT],
    TypeAttribute,
):
    name = "complex"
    element_type: ParameterDef[ComplexElementCovT]

    def __init__(self, element_type: ComplexElementCovT):
        super().__init__([element_type])

    def get_element_type(self) -> ComplexElementCovT:
        return self.element_type

    @property
    def compile_time_size(self) -> int:
        return 2 * self.element_type.compile_time_size

    @property
    def size(self) -> int:
        return 2 * self.element_type.size

    def iter_unpack(self, buffer: ReadableBuffer, /):
        values = (value for value in self.element_type.iter_unpack(buffer))
        return ((real, imag) for real, imag in zip(values, values))

    def unpack(self, buffer: ReadableBuffer, num: int, /):
        values = (value for value in self.element_type.unpack(buffer, 2 * num))
        return tuple((real, imag) for real, imag in zip(values, values))

    @overload
    def pack_into(
        self: ComplexType[IntegerType],
        buffer: WriteableBuffer,
        offset: int,
        value: tuple[int, int],
    ) -> None: ...

    @overload
    def pack_into(
        self: ComplexType[AnyFloat],
        buffer: WriteableBuffer,
        offset: int,
        value: tuple[float, float],
    ) -> None: ...

    def pack_into(
        self,
        buffer: WriteableBuffer,
        offset: int,
        value: tuple[float, float] | tuple[int, int],
    ) -> None:
        self.element_type.pack_into(buffer, 2 * offset, value[0])  # pyright: ignore[reportArgumentType]
        self.element_type.pack_into(buffer, 2 * offset + 1, value[1])  # pyright: ignore[reportArgumentType]
        return

    @overload
    def pack(
        self: ComplexType[AnyFloat], values: Sequence[tuple[float, float]]
    ) -> bytes: ...

    @overload
    def pack(
        self: ComplexType[IntegerType], values: Sequence[tuple[int, int]]
    ) -> bytes: ...

    @overload
    def pack(
        self, values: Sequence[tuple[int, int]] | Sequence[tuple[float, float]]
    ) -> bytes: ...

    def pack(self, values: Sequence[tuple[float, float] | tuple[int, int]]) -> bytes:
        return self.element_type.pack(tuple(val for vals in values for val in vals))  # pyright: ignore[reportArgumentType]


@irdl_attr_definition
class DictionaryAttr(GenericData[immutabledict[str, Attribute]], BuiltinAttribute):
    name = "dictionary"

    def __init__(self, value: Mapping[str, Attribute]):
        if not isinstance(value, immutabledict):
            value = immutabledict(value)
        super().__init__(value)

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> immutabledict[str, Attribute]:
        return immutabledict(parser.parse_optional_dictionary_attr_dict())

    def print_parameter(self, printer: Printer) -> None:
        printer.print_attr_dict(self.data)

    @staticmethod
    def generic_constraint_coercion(args: tuple[Any]) -> AttrConstraint:
        raise Exception(f"Unsupported operation on {DictionaryAttr.name}")

    def verify(self) -> None:
        return super().verify()


@irdl_attr_definition
class TupleType(ParametrizedAttribute, BuiltinAttribute):
    name = "tuple"

    types: ParameterDef[ArrayAttr[Attribute]]

    def __init__(self, types: list[Attribute] | ArrayAttr[Attribute]) -> None:
        if isinstance(types, list):
            types = ArrayAttr(types)
        super().__init__([types])


@irdl_attr_definition
class VectorType(
    Generic[AttributeCovT],
    BuiltinAttribute,
    ParametrizedAttribute,
    TypeAttribute,
    ShapedType,
    ContainerType[AttributeCovT],
):
    name = "vector"

    shape: ParameterDef[ArrayAttr[IntAttr]]
    element_type: ParameterDef[AttributeCovT]
    scalable_dims: ParameterDef[ArrayAttr[BoolAttr]]

    def __init__(
        self,
        element_type: AttributeCovT,
        shape: Iterable[int | IntAttr],
        scalable_dims: ArrayAttr[BoolAttr] | None = None,
    ) -> None:
        shape = ArrayAttr(
            [IntAttr(dim) if isinstance(dim, int) else dim for dim in shape]
        )
        if scalable_dims is None:
            false = BoolAttr(False, i1)
            scalable_dims = ArrayAttr(false for _ in shape)
        super().__init__([shape, element_type, scalable_dims])

    def get_num_dims(self) -> int:
        return len(self.shape.data)

    def get_num_scalable_dims(self) -> int:
        return sum(bool(d.value) for d in self.scalable_dims)

    def get_shape(self) -> tuple[int, ...]:
        return tuple(i.data for i in self.shape)

    def get_element_type(self) -> AttributeCovT:
        return self.element_type

    def get_scalable_dims(self) -> tuple[bool, ...]:
        return tuple(bool(i) for i in self.scalable_dims)

    def verify(self):
        num_dims = len(self.shape)
        num_scalable_dims = len(self.scalable_dims)
        if num_dims != num_scalable_dims:
            raise VerifyException(
                f"Number of scalable dimension specifiers {num_scalable_dims} must "
                f"equal to number of dimensions {num_dims}."
            )

    @classmethod
    def constr(
        cls,
        element_type: IRDLGenericAttrConstraint[AttributeCovT] | None = None,
        *,
        shape: IRDLGenericAttrConstraint[ArrayAttr[IntAttr]] | None = None,
        scalable_dims: IRDLGenericAttrConstraint[ArrayAttr[BoolAttr]] | None = None,
    ) -> GenericAttrConstraint[VectorType[AttributeCovT]]:
        if element_type is None and shape is None and scalable_dims is None:
            return BaseAttr[VectorType[AttributeCovT]](VectorType)
        shape_constr = AnyAttr() if shape is None else shape
        scalable_dims_constr = AnyAttr() if scalable_dims is None else scalable_dims
        return ParamAttrConstraint[VectorType[AttributeCovT]](
            VectorType,
            (
                shape_constr,
                element_type,
                scalable_dims_constr,
            ),
        )


AnyVectorType: TypeAlias = VectorType[Attribute]


@irdl_attr_definition
class TensorType(
    Generic[AttributeCovT],
    ParametrizedAttribute,
    BuiltinAttribute,
    TypeAttribute,
    ShapedType,
    ContainerType[AttributeCovT],
):
    name = "tensor"

    shape: ParameterDef[ArrayAttr[IntAttr]]
    element_type: ParameterDef[AttributeCovT]
    encoding: ParameterDef[Attribute]

    def __init__(
        self,
        element_type: AttributeCovT,
        shape: Iterable[int | IntAttr],
        encoding: Attribute = NoneAttr(),
    ):
        shape = ArrayAttr(
            [IntAttr(dim) if isinstance(dim, int) else dim for dim in shape]
        )
        super().__init__([shape, element_type, encoding])

    def get_num_dims(self) -> int:
        return len(self.shape.data)

    def get_shape(self) -> tuple[int, ...]:
        return tuple(i.data for i in self.shape.data)

    def get_element_type(self) -> AttributeCovT:
        return self.element_type


AnyTensorType: TypeAlias = TensorType[Attribute]
AnyTensorTypeConstr = BaseAttr[TensorType[Attribute]](TensorType)


@irdl_attr_definition
class UnrankedTensorType(
    Generic[AttributeCovT],
    ParametrizedAttribute,
    BuiltinAttribute,
    TypeAttribute,
    ContainerType[AttributeCovT],
):
    name = "unranked_tensor"

    element_type: ParameterDef[AttributeCovT]

    def __init__(self, element_type: AttributeCovT) -> None:
        super().__init__([element_type])

    def get_element_type(self) -> AttributeCovT:
        return self.element_type


AnyUnrankedTensorType: TypeAlias = UnrankedTensorType[Attribute]
AnyUnrankedTensorTypeConstr = BaseAttr[AnyUnrankedTensorType](UnrankedTensorType)


@dataclass(frozen=True, init=False)
class ContainerOf(
    Generic[AttributeCovT],
    GenericAttrConstraint[
        AttributeCovT | VectorType[AttributeCovT] | TensorType[AttributeCovT]
    ],
):
    """A type constraint that can be nested once in a vector or a tensor."""

    elem_constr: GenericAttrConstraint[AttributeCovT]

    def __init__(
        self,
        elem_constr: (
            AttributeCovT | type[AttributeCovT] | GenericAttrConstraint[AttributeCovT]
        ),
    ) -> None:
        object.__setattr__(self, "elem_constr", attr_constr_coercion(elem_constr))

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        if isinstance(attr, VectorType) or isinstance(attr, TensorType):
            attr = cast(VectorType[Attribute] | TensorType[Attribute], attr)
            self.elem_constr.verify(attr.element_type, constraint_context)
        else:
            self.elem_constr.verify(attr, constraint_context)


VectorOrTensorOf: TypeAlias = (
    VectorType[AttributeCovT]
    | TensorType[AttributeCovT]
    | UnrankedTensorType[AttributeCovT]
)


@dataclass(frozen=True)
class VectorRankConstraint(AttrConstraint):
    """
    Constrain a vector to be of a given rank.
    """

    expected_rank: int
    """The expected vector rank."""

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        if not isinstance(attr, VectorType):
            raise VerifyException(f"{attr} should be of type VectorType.")
        if attr.get_num_dims() != self.expected_rank:
            raise VerifyException(
                f"Expected vector rank to be {self.expected_rank}, got {attr.get_num_dims()}."
            )


@dataclass(frozen=True)
class VectorBaseTypeConstraint(AttrConstraint):
    """
    Constrain a vector to be of a given base type.
    """

    expected_type: Attribute
    """The expected vector base type."""

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        if not isinstance(attr, VectorType):
            raise VerifyException(f"{attr} should be of type VectorType.")
        attr = cast(VectorType[Attribute], attr)
        if attr.element_type != self.expected_type:
            raise VerifyException(
                f"Expected vector type to be {self.expected_type}, got {attr.element_type}."
            )


@dataclass(frozen=True)
class VectorBaseTypeAndRankConstraint(AttrConstraint):
    """
    Constrain a vector to be of a given rank and base type.
    """

    expected_type: Attribute
    """The expected vector base type."""

    expected_rank: int
    """The expected vector rank."""

    def verify(self, attr: Attribute, constraint_context: ConstraintContext) -> None:
        constraint = VectorBaseTypeConstraint(
            self.expected_type
        ) & VectorRankConstraint(self.expected_rank)
        constraint.verify(attr, constraint_context)


@irdl_attr_definition
class DenseResourceAttr(ParametrizedAttribute, BuiltinAttribute):
    name = "dense_resource"

    resource_handle: ParameterDef[StringAttr]

    # Should be a ShapedType, but this is not defined yet in xDSL
    type: ParameterDef[Attribute]

    @staticmethod
    def from_params(handle: str | StringAttr, type: Attribute) -> DenseResourceAttr:
        if isinstance(handle, str):
            handle = StringAttr(handle)
        return DenseResourceAttr([handle, type])


DenseArrayT = TypeVar(
    "DenseArrayT",
    bound=IntegerType | AnyFloat,
    default=IntegerType | AnyFloat,
    covariant=True,
)


@irdl_attr_definition
class DenseArrayBase(
    Generic[DenseArrayT],
    ContainerType[DenseArrayT],
    ParametrizedAttribute,
    BuiltinAttribute,
):
    name = "array"

    elt_type: ParameterDef[DenseArrayT]
    data: ParameterDef[BytesAttr]

    def verify(self):
        data_len = len(self.data.data)
        elt_size = self.elt_type.size
        if data_len % elt_size:
            raise VerifyException(
                f"Data length of {self.name} ({data_len}) not divisible by element "
                f"size {elt_size}"
            )

    def get_element_type(self) -> DenseArrayT:
        return self.elt_type

    @staticmethod
    def create_dense_int(
        data_type: _IntegerTypeInvT, data: Sequence[int] | Sequence[IntAttr]
    ) -> DenseArrayBase[_IntegerTypeInvT]:
        if len(data) and isinstance(data[0], IntAttr):
            value_list = tuple(d.data for d in cast(Sequence[IntAttr], data))
        else:
            value_list = cast(Sequence[int], data)

        normalized_values = tuple(
            data_type.get_normalized_value(value) for value in value_list
        )

        bytes_data = data_type.pack(normalized_values)

        return DenseArrayBase([data_type, BytesAttr(bytes_data)])

    @staticmethod
    def create_dense_float(
        data_type: _FloatAttrTypeInvT, data: Sequence[float] | Sequence[FloatData]
    ) -> DenseArrayBase[_FloatAttrTypeInvT]:
        if len(data) and isinstance(data[0], int | float):
            vals = data
        else:
            vals = tuple(attr.data for attr in cast(Sequence[FloatData], data))

        fmt = data_type.format[0] + str(len(data)) + data_type.format[1:]

        bytes_data = struct.pack(fmt, *vals)

        return DenseArrayBase([data_type, BytesAttr(bytes_data)])

    @overload
    @staticmethod
    def from_list(
        data_type: IntegerType, data: Sequence[int] | Sequence[IntAttr]
    ) -> DenseArrayBase: ...

    @overload
    @staticmethod
    def from_list(
        data_type: Attribute, data: Sequence[int | float] | Sequence[FloatData]
    ) -> DenseArrayBase: ...

    @staticmethod
    def from_list(
        data_type: Attribute,
        data: (
            Sequence[int]
            | Sequence[int | float]
            | Sequence[IntAttr]
            | Sequence[FloatData]
        ),
    ) -> DenseArrayBase:
        if isinstance(data_type, IntegerType):
            _data = cast(Sequence[int] | Sequence[IntAttr], data)
            return DenseArrayBase.create_dense_int(data_type, _data)
        elif isinstance(data_type, AnyFloat):
            _data = cast(Sequence[int | float] | Sequence[FloatData], data)
            return DenseArrayBase.create_dense_float(data_type, _data)
        else:
            raise TypeError(f"Unsupported element type {data_type}")

    def iter_values(self) -> Iterator[float] | Iterator[int]:
        return self.elt_type.iter_unpack(self.data.data)

    def get_values(self) -> tuple[int, ...] | tuple[float, ...]:
        return self.elt_type.unpack(self.data.data, len(self))

    def iter_attrs(self) -> Iterator[IntegerAttr] | Iterator[FloatAttr]:
        if isinstance(self.elt_type, IntegerType):
            return IntegerAttr.iter_unpack(self.elt_type, self.data.data)
        else:
            return FloatAttr.iter_unpack(self.elt_type, self.data.data)

    def get_attrs(self) -> tuple[IntegerAttr, ...] | tuple[FloatAttr, ...]:
        if isinstance(self.elt_type, IntegerType):
            return IntegerAttr.unpack(self.elt_type, self.data.data, len(self))
        else:
            return FloatAttr.unpack(self.elt_type, self.data.data, len(self))

    def __len__(self) -> int:
        return len(self.data.data) // self.elt_type.size


DenseI64ArrayConstr = ParamAttrConstraint(DenseArrayBase, [i64, BytesAttr])
"""Type constraint for DenseArrays containing i64 integers."""

DenseI32ArrayConstr = ParamAttrConstraint(DenseArrayBase, [i32, BytesAttr])
"""Type constraint for DenseArrays containing i32 integers."""


@irdl_attr_definition
class FunctionType(ParametrizedAttribute, BuiltinAttribute, TypeAttribute):
    name = "fun"

    inputs: ParameterDef[ArrayAttr[Attribute]]
    outputs: ParameterDef[ArrayAttr[Attribute]]

    @staticmethod
    def from_lists(
        inputs: Sequence[Attribute], outputs: Sequence[Attribute]
    ) -> FunctionType:
        return FunctionType([ArrayAttr(inputs), ArrayAttr(outputs)])

    @staticmethod
    def from_attrs(
        inputs: ArrayAttr[Attribute], outputs: ArrayAttr[Attribute]
    ) -> FunctionType:
        return FunctionType([inputs, outputs])


@irdl_attr_definition
class OpaqueAttr(ParametrizedAttribute, BuiltinAttribute):
    name = "opaque"

    ident: ParameterDef[StringAttr]
    value: ParameterDef[StringAttr]
    type: ParameterDef[Attribute]

    @staticmethod
    def from_strings(name: str, value: str, type: Attribute = NoneAttr()) -> OpaqueAttr:
        return OpaqueAttr([StringAttr(name), StringAttr(value), type])


class MemRefLayoutAttr(Attribute, ABC):
    """
    Interface for any attribute acceptable as a memref layout.
    """

    name = "abstract.memref_layout_att"

    @abstractmethod
    def get_affine_map(self) -> AffineMap:
        """
        Return the affine mapping from the iteration space of this
        layout to the element offset in linear memory. The resulting
        affine map thus has only one result.
        """
        raise NotImplementedError()

    def get_strides(self) -> Sequence[int | None] | None:
        """
        (optional) Return the list of strides, representing the element offset
        in linear memory for every dimension in the iteration space of
        this memref layout attribute.

        Note: The dimension of the iteration space may differ from the dimension
        of the data it represents. For instance, this can occur in a tiled layout.

        This is only applicable to hyper-rectangular layouts.
        If this is not applicable for a given layout, returns None
        """
        return None


@irdl_attr_definition
class StridedLayoutAttr(MemRefLayoutAttr, BuiltinAttribute, ParametrizedAttribute):
    """
    An attribute representing a strided layout of a shaped type.
    See external [documentation](https://mlir.llvm.org/docs/Dialects/Builtin/#stridedlayoutattr).

    Contrary to MLIR, we represent dynamic offsets and strides with
    `NoneAttr`, and we do not restrict offsets and strides to 64-bits
    integers.
    """

    name = "strided"

    strides: ParameterDef[ArrayAttr[IntAttr | NoneAttr]]
    offset: ParameterDef[IntAttr | NoneAttr]

    def __init__(
        self,
        strides: (
            ArrayAttr[IntAttr | NoneAttr] | Sequence[int | None | IntAttr | NoneAttr]
        ),
        offset: int | None | IntAttr | NoneAttr = 0,
    ) -> None:
        if not isinstance(strides, ArrayAttr):
            strides_values: list[IntAttr | NoneAttr] = []
            for stride in strides:
                if isinstance(stride, int):
                    strides_values.append(IntAttr(stride))
                elif stride is None:
                    strides_values.append(NoneAttr())
                else:
                    strides_values.append(stride)
            strides = ArrayAttr(strides_values)

        if isinstance(offset, int):
            offset = IntAttr(offset)
        if offset is None:
            offset = NoneAttr()

        super().__init__([strides, offset])

    def get_strides(self) -> Sequence[int | None]:
        return tuple(
            None if isinstance(stride, NoneAttr) else stride.data
            for stride in self.strides
        )

    def get_offset(self) -> int | None:
        if isinstance(self.offset, NoneAttr):
            return None
        else:
            return self.offset.data

    def get_affine_map(self) -> AffineMap:
        """
        Return the affine mapping from the iteration space of this
        layout to the element offset in linear memory. The resulting
        affine map thus has only one result.

        For dynamic strides, this results in an affinemap with a number
        of symbols, ordered in the following manner:
            (1) Symbol for the dynamic offset of the layout
            (2) Symbols for every dynamic stride of the layout
        """

        # keep track of number of symbols
        nb_symbols = 0

        result = AffineConstantExpr(0)

        # add offset
        if isinstance(self.offset, IntAttr):
            result += AffineConstantExpr(self.offset.data)
        else:  # NoneAttr
            result += AffineSymExpr(nb_symbols)
            nb_symbols += 1

        for dim, stride in enumerate(self.strides.data):
            if isinstance(stride, IntAttr):
                stride_expr = AffineConstantExpr(stride.data)
            else:  # NoneAttr
                stride_expr = AffineSymExpr(nb_symbols)
                nb_symbols += 1
            result += AffineDimExpr(dim) * stride_expr

        return AffineMap(len(self.strides), nb_symbols, (result,))


@irdl_attr_definition
class AffineMapAttr(MemRefLayoutAttr, BuiltinAttribute, Data[AffineMap]):
    """An Attribute containing an AffineMap object."""

    name = "affine_map"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> AffineMap:
        with parser.in_angle_brackets():
            data = parser.parse_affine_map()
            return data

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string(f"{self.data}")

    @staticmethod
    def constant_map(value: int) -> AffineMapAttr:
        return AffineMapAttr(AffineMap.constant_map(value))

    def get_affine_map(self) -> AffineMap:
        return self.data


@irdl_attr_definition
class AffineSetAttr(Data[AffineSet], BuiltinAttribute):
    """An attribute containing an AffineSet object."""

    name = "affine_set"

    @classmethod
    def parse_parameter(cls, parser: AttrParser) -> AffineSet:
        return parser.parse_affine_set()

    def print_parameter(self, printer: Printer) -> None:
        printer.print_string(f"{self.data}")


@irdl_op_definition
class UnrealizedConversionCastOp(IRDLOperation):
    name = "builtin.unrealized_conversion_cast"

    inputs = var_operand_def()
    outputs = var_result_def()

    traits = traits_def(NoMemoryEffect())

    @staticmethod
    def get(inputs: Sequence[SSAValue | Operation], result_type: Sequence[Attribute]):
        return UnrealizedConversionCastOp.build(
            operands=[inputs],
            result_types=[result_type],
        )

    @staticmethod
    def cast_one(
        input: SSAValue, result_type: AttributeInvT
    ) -> tuple[UnrealizedConversionCastOp, SSAValue[AttributeInvT]]:
        op = UnrealizedConversionCastOp(operands=(input,), result_types=(result_type,))
        res: SSAValue[AttributeInvT] = op.results[0]  # pyright: ignore[reportAssignmentType]
        return op, res

    @classmethod
    def parse(cls, parser: Parser) -> Self:
        if parser.parse_optional_characters("to") is None:
            args = parser.parse_comma_separated_list(
                parser.Delimiter.NONE,
                parser.parse_unresolved_operand,
            )
            parser.parse_punctuation(":")
            input_types = parser.parse_comma_separated_list(
                parser.Delimiter.NONE,
                parser.parse_type,
            )
            parser.parse_characters("to")
            inputs = parser.resolve_operands(args, input_types, parser.pos)
        else:
            inputs = list[SSAValue]()
        output_types = parser.parse_comma_separated_list(
            parser.Delimiter.NONE,
            parser.parse_type,
        )
        attributes = parser.parse_optional_attr_dict()
        return cls(
            operands=[inputs], result_types=[output_types], attributes=attributes
        )

    def print(self, printer: Printer):
        def print_fn(operand: SSAValue) -> None:
            return printer.print_attribute(operand.type)

        if self.inputs:
            printer.print(" ")
            printer.print_list(self.inputs, printer.print_operand)
            printer.print_string(" : ")
            printer.print_list(self.inputs, print_fn)
        printer.print_string(" to ")
        printer.print_list(self.outputs, print_fn)
        printer.print_op_attributes(self.attributes)


class UnregisteredOp(Operation, ABC):
    """
    An unregistered operation.

    Each unregistered op is registered as a subclass of `UnregisteredOp`,
    and op with different names have distinct subclasses.
    """

    name = "builtin.unregistered"
    traits = traits_def()

    @property
    def op_name(self) -> StringAttr:
        if "op_name__" not in self.attributes:
            raise ValueError("missing 'op_name__' attribute")
        op_name = self.attributes["op_name__"]
        if not isinstance(op_name, StringAttr):
            raise ValueError(
                f"'op_name__' is expected to have 'StringAttr' type, got {op_name}"
            )
        return op_name

    @classmethod
    def with_name(cls, name: str) -> type[Operation]:
        """
        Return a new unregistered operation type given a name.
        This function should not be called directly. Use methods from
        `Context` to get an `UnregisteredOp` type.
        """

        class UnregisteredOpWithNameOp(UnregisteredOp):
            @classmethod
            def create(
                cls,
                *,
                operands: Sequence[SSAValue] = (),
                result_types: Sequence[Attribute] = (),
                properties: Mapping[str, Attribute] = {},
                attributes: Mapping[str, Attribute] = {},
                successors: Sequence[Block] = (),
                regions: Sequence[Region] = (),
            ):
                op = super().create(
                    operands=operands,
                    result_types=result_types,
                    properties=properties,
                    attributes=attributes,
                    successors=successors,
                    regions=regions,
                )
                op.attributes["op_name__"] = StringAttr(name)
                return op

        return UnregisteredOpWithNameOp

    @classmethod
    def has_trait(
        cls,
        trait: type[OpTrait] | OpTrait,
        *,
        value_if_unregistered: bool = True,
    ) -> bool:
        return value_if_unregistered


class UnregisteredAttr(ParametrizedAttribute, BuiltinAttribute, ABC):
    """
    An unregistered attribute or type.

    Each unregistered attribute is registered as a subclass of
    `UnregisteredAttr`, and attribute with different names have
    distinct subclasses.

    Since attributes do not have a generic format, unregistered
    attributes represent their original parameters as a string,
    which is exactly the content parsed from the textual
    representation.
    """

    name = "builtin.unregistered"

    attr_name: ParameterDef[StringAttr]
    is_type: ParameterDef[IntAttr]
    is_opaque: ParameterDef[IntAttr]
    value: ParameterDef[StringAttr]
    """
    This parameter is non-null is the attribute is a type, and null otherwise.
    """

    def __init__(
        self,
        attr_name: str | StringAttr,
        is_type: bool | IntAttr,
        is_opaque: bool | IntAttr,
        value: str | StringAttr,
    ):
        if isinstance(attr_name, str):
            attr_name = StringAttr(attr_name)
        if isinstance(is_type, bool):
            is_type = IntAttr(int(is_type))
        if isinstance(is_opaque, bool):
            is_opaque = IntAttr(int(is_opaque))
        if isinstance(value, str):
            value = StringAttr(value)
        super().__init__([attr_name, is_type, is_opaque, value])

    @classmethod
    def with_name_and_type(cls, name: str, is_type: bool) -> type[UnregisteredAttr]:
        """
        Return a new unregistered attribute type given a name and a
        boolean indicating if the attribute can be a type.
        This function should not be called directly. Use methods from
        `Context` to get an `UnregisteredAttr` type.
        """

        @irdl_attr_definition
        class UnregisteredAttrWithName(UnregisteredAttr):
            def verify(self):
                if self.attr_name.data != name:
                    raise VerifyException("Unregistered attribute name mismatch")
                if self.is_type.data != int(is_type):
                    raise VerifyException("Unregistered attribute is_type mismatch")

        @irdl_attr_definition
        class UnregisteredAttrTypeWithName(UnregisteredAttr, TypeAttribute):
            def verify(self):
                if self.attr_name.data != name:
                    raise VerifyException("Unregistered attribute name mismatch")
                if self.is_type.data != int(is_type):
                    raise VerifyException("Unregistered attribute is_type mismatch")

        if is_type:
            return UnregisteredAttrTypeWithName
        else:
            return UnregisteredAttrWithName


@irdl_op_definition
class ModuleOp(IRDLOperation):
    name = "builtin.module"

    sym_name = opt_prop_def(StringAttr)

    body = region_def("single_block")

    traits = traits_def(
        IsolatedFromAbove(),
        NoTerminator(),
        OptionalSymbolOpInterface(),
        SymbolTable(),
    )

    def __init__(
        self,
        ops: list[Operation] | Region,
        attributes: Mapping[str, Attribute] | None = None,
        sym_name: StringAttr | None = None,
    ):
        if attributes is None:
            attributes = {}
        if isinstance(ops, Region):
            region = ops
        else:
            region = Region(Block(ops))
        properties: dict[str, Attribute | None] = {"sym_name": sym_name}
        super().__init__(regions=[region], attributes=attributes, properties=properties)

    @property
    def ops(self) -> BlockOps:
        return self.body.ops

    @classmethod
    def parse(cls, parser: Parser) -> ModuleOp:
        module_name = parser.parse_optional_symbol_name()

        attributes = parser.parse_optional_attr_dict_with_keyword()
        if attributes is not None:
            attributes = attributes.data
        region = parser.parse_region()

        # Add a block if the region is empty
        if not region.blocks:
            region.add_block(Block())

        return ModuleOp(region, attributes, module_name)

    def print(self, printer: Printer) -> None:
        if self.sym_name is not None:
            printer.print(f" @{self.sym_name.data}")

        if self.attributes:
            printer.print_op_attributes(self.attributes, print_keyword=True)

        if not self.body.block.ops:
            # Do not print the entry block if the region has an empty block
            printer.print(" {\n")
            printer.print("}")
        else:
            printer.print(" ", self.body)


# FloatXXType shortcuts
bf16 = BFloat16Type()
f16 = Float16Type()
f32 = Float32Type()
f64 = Float64Type()
f80 = Float80Type()
f128 = Float128Type()


_MemRefTypeElement = TypeVar(
    "_MemRefTypeElement", bound=Attribute, covariant=True, default=Attribute
)
_UnrankedMemRefTypeElems = TypeVar(
    "_UnrankedMemRefTypeElems", bound=Attribute, covariant=True, default=Attribute
)
_UnrankedMemRefTypeElemsInit = TypeVar("_UnrankedMemRefTypeElemsInit", bound=Attribute)


@irdl_attr_definition
class NoneType(ParametrizedAttribute, BuiltinAttribute, TypeAttribute):
    name = "none_type"


@irdl_attr_definition
class MemRefType(
    Generic[_MemRefTypeElement],
    ParametrizedAttribute,
    BuiltinAttribute,
    TypeAttribute,
    ShapedType,
    ContainerType[_MemRefTypeElement],
):
    name = "memref"

    shape: ParameterDef[ArrayAttr[IntAttr]]
    element_type: ParameterDef[_MemRefTypeElement]
    layout: ParameterDef[MemRefLayoutAttr | NoneAttr]
    memory_space: ParameterDef[Attribute]

    def __init__(
        self,
        element_type: _MemRefTypeElement,
        shape: ArrayAttr[IntAttr] | Iterable[int | IntAttr],
        layout: MemRefLayoutAttr | NoneAttr = NoneAttr(),
        memory_space: Attribute = NoneAttr(),
    ):
        s: ArrayAttr[IntAttr]
        if isinstance(shape, ArrayAttr):
            # Temporary cast until Pyright is fixed to not infer ArrayAttr[int] as a
            # possible value for shape
            s = cast(ArrayAttr[IntAttr], shape)
        else:
            s = ArrayAttr(
                [IntAttr(dim) if isinstance(dim, int) else dim for dim in shape]
            )
        super().__init__(
            (
                s,
                element_type,
                layout,
                memory_space,
            )
        )

    def get_num_dims(self) -> int:
        return len(self.shape.data)

    def get_shape(self) -> tuple[int, ...]:
        return tuple(i.data for i in self.shape.data)

    def get_element_type(self) -> _MemRefTypeElement:
        return self.element_type

    @classmethod
    def parse_parameters(cls, parser: AttrParser) -> list[Attribute]:
        parser.parse_punctuation("<", " in memref attribute")
        shape = parser.parse_attribute()
        parser.parse_punctuation(",", " between shape and element type parameters")
        type = parser.parse_attribute()
        # If we have a layout or a memory space, parse both of them.
        if parser.parse_optional_punctuation(",") is None:
            parser.parse_punctuation(">", " at end of memref attribute")
            return [shape, type, NoneAttr(), NoneAttr()]
        layout = parser.parse_attribute()
        parser.parse_punctuation(",", " between layout and memory space")
        memory_space = parser.parse_attribute()
        parser.parse_punctuation(">", " at end of memref attribute")

        return [shape, type, layout, memory_space]

    def print_parameters(self, printer: Printer) -> None:
        printer.print("<", self.shape, ", ", self.element_type)
        if self.layout != NoneAttr() or self.memory_space != NoneAttr():
            printer.print(", ", self.layout, ", ", self.memory_space)
        printer.print(">")

    def get_affine_map(self) -> AffineMap:
        """
        Return the affine mapping from the iteration space of this
        memref's layout to the element offset in linear memory.
        """
        if isinstance(self.layout, NoneAttr):
            # empty shape not supported
            if self.get_shape() == ():
                raise DiagnosticException(
                    f"Unsupported empty shape in memref of type {self}"
                )

            strides = self.strides_for_shape(self.get_shape())
            map = StridedLayoutAttr(strides).get_affine_map()
        else:
            map = self.layout.get_affine_map()

        return map

    def get_affine_map_in_bytes(self) -> AffineMap:
        """
        Return the affine mapping from the iteration space of this
        memref's layout to the byte offset in linear memory.

        Unlike the get_affine_map, this function accounts for element width.
        """

        map = self.get_affine_map()

        # account for element width
        assert isinstance(self.element_type, FixedBitwidthType)

        return AffineMap(
            map.num_dims,
            map.num_symbols,
            tuple(result * self.element_type.size for result in map.results),
        )

    def get_strides(self) -> Sequence[int | None] | None:
        """
        Yields the strides of the memref for each dimension.
        The stride of a dimension is the number of elements that are skipped when
        incrementing the corresponding index by one.
        """
        match self.layout:
            case NoneAttr():
                return ShapedType.strides_for_shape(self.get_shape())
            case _:
                return self.layout.get_strides()

    @classmethod
    def constr(
        cls,
        *,
        shape: IRDLAttrConstraint | None = None,
        element_type: IRDLGenericAttrConstraint[_MemRefTypeElement] = AnyAttr(),
        layout: IRDLAttrConstraint | None = None,
        memory_space: IRDLAttrConstraint | None = None,
    ) -> GenericAttrConstraint[MemRefType[_MemRefTypeElement]]:
        if (
            shape is None
            and element_type == AnyAttr()
            and layout is None
            and memory_space is None
        ):
            return BaseAttr[MemRefType[_MemRefTypeElement]](MemRefType)
        return ParamAttrConstraint[MemRefType[_MemRefTypeElement]](
            MemRefType, (shape, element_type, layout, memory_space)
        )


@irdl_attr_definition
class UnrankedMemRefType(
    Generic[_UnrankedMemRefTypeElems],
    ParametrizedAttribute,
    BuiltinAttribute,
    TypeAttribute,
    ContainerType[_UnrankedMemRefTypeElems],
):
    name = "unranked_memref"

    element_type: ParameterDef[_UnrankedMemRefTypeElems]
    memory_space: ParameterDef[Attribute]

    @staticmethod
    def from_type(
        referenced_type: _UnrankedMemRefTypeElemsInit,
        memory_space: Attribute = NoneAttr(),
    ) -> UnrankedMemRefType[_UnrankedMemRefTypeElemsInit]:
        return UnrankedMemRefType([referenced_type, memory_space])

    def get_element_type(self) -> _UnrankedMemRefTypeElems:
        return self.element_type


AnyUnrankedMemRefType: TypeAlias = UnrankedMemRefType[Attribute]
AnyUnrankedMemRefTypeConstr = BaseAttr[AnyUnrankedMemRefType](UnrankedMemRefType)

RankedStructure: TypeAlias = (
    VectorType[AttributeCovT] | TensorType[AttributeCovT] | MemRefType[AttributeCovT]
)

AnyDenseElement: TypeAlias = IntegerType | IndexType | AnyFloat | ComplexType
DenseElementCovT = TypeVar(
    "DenseElementCovT", bound=AnyDenseElement, default=AnyDenseElement, covariant=True
)

DenseElementT = TypeVar("DenseElementT", bound=AnyDenseElement, default=AnyDenseElement)


@irdl_attr_definition
class DenseIntOrFPElementsAttr(
    Generic[DenseElementCovT],
    TypedAttribute,
    BuiltinAttribute,
    ContainerType[DenseElementCovT],
):
    name = "dense"
    type: ParameterDef[RankedStructure[DenseElementCovT]]
    data: ParameterDef[BytesAttr]

    # The type stores the shape data
    def get_shape(self) -> tuple[int, ...]:
        return self.type.get_shape()

    def get_element_type(self) -> DenseElementCovT:
        return self.type.get_element_type()

    def __len__(self) -> int:
        return len(self.data.data) // self.type.element_type.compile_time_size

    @property
    def shape_is_complete(self) -> bool:
        shape = self.get_shape()

        n = 1
        for dim in shape:
            if dim < 1:
                # Dimensions need to be greater or equal to one
                return False
            n *= dim

        # Product of dimensions needs to equal length
        return n == len(self)

    def verify(self) -> None:
        # zero rank type should only hold 1 value
        data_len = len(self.get_values())
        if not self.type.get_shape() and data_len != 1:
            raise VerifyException(
                f"A zero-rank {self.type.name} can only hold 1 value but {data_len} were given."
            )

    @staticmethod
    @deprecated("Please use `create_dense_int` instead.")
    def create_dense_index(
        type: RankedStructure[IndexType],
        data: Sequence[int] | Sequence[IntegerAttr[IndexType]],
    ) -> DenseIntOrFPElementsAttr[IndexType]:
        return DenseIntOrFPElementsAttr.create_dense_int(type, data)

    @staticmethod
    def create_dense_int(
        type: RankedStructure[_IntegerAttrType],
        data: int
        | IntegerAttr[_IntegerAttrType]
        | Sequence[int]
        | Sequence[IntegerAttr[_IntegerAttrType]],
    ) -> DenseIntOrFPElementsAttr[_IntegerAttrType]:
        # Splat case
        if isinstance(data, IntegerAttr):
            data = data.value.data
        if isinstance(data, int):
            if isinstance(type.element_type, IntegerType):
                value = type.element_type.get_normalized_value(data)
            else:
                value = data
            return DenseIntOrFPElementsAttr(
                [
                    type,
                    BytesAttr(
                        type.element_type.pack((value,)) * prod(type.get_shape())
                    ),
                ]
            )

        # Non-splat case
        if len(data) and isinstance(data[0], IntegerAttr):
            data = [el.value.data for el in cast(Sequence[IntegerAttr], data)]
        else:
            data = cast(Sequence[int], data)

        # ints are normalized
        if isinstance(type.element_type, IntegerType):
            normalized_values = tuple(
                type.element_type.get_normalized_value(value) for value in data
            )
        else:
            normalized_values = data

        return DenseIntOrFPElementsAttr(
            [type, BytesAttr(type.element_type.pack(normalized_values))]
        )

    @staticmethod
    def create_dense_float(
        type: RankedStructure[_FloatAttrType],
        data: float
        | FloatAttr[_FloatAttrType]
        | Sequence[float]
        | Sequence[FloatAttr[_FloatAttrType]],
    ) -> DenseIntOrFPElementsAttr[_FloatAttrType]:
        # Splat case
        if isinstance(data, FloatAttr):
            data = data.value.data
        if isinstance(
            data, float | int
        ):  # Pyright allows an int to be passed into this function
            return DenseIntOrFPElementsAttr(
                [
                    type,
                    BytesAttr(type.element_type.pack((data,)) * prod(type.get_shape())),
                ]
            )

        # Non-splat case
        if len(data) and isa(data[0], FloatAttr):
            data = [el.value.data for el in cast(Sequence[FloatAttr], data)]
        else:
            data = cast(Sequence[float], data)

        return DenseIntOrFPElementsAttr([type, BytesAttr(type.element_type.pack(data))])

    @overload
    @staticmethod
    def create_dense_complex(
        type: RankedStructure[ComplexType[_IntegerTypeInvT]],
        data: Sequence[tuple[int, int]],
    ) -> DenseIntOrFPElementsAttr[ComplexType[_IntegerTypeInvT]]: ...

    @overload
    @staticmethod
    def create_dense_complex(
        type: RankedStructure[ComplexType[_FloatAttrTypeInvT]],
        data: Sequence[tuple[float, float]],
    ) -> DenseIntOrFPElementsAttr[ComplexType[_FloatAttrTypeInvT]]: ...

    @staticmethod
    def create_dense_complex(
        type: RankedStructure[ComplexType[ComplexElementCovT]],
        data: Sequence[tuple[float, float]] | Sequence[tuple[int, int]],
    ) -> DenseIntOrFPElementsAttr[ComplexType[ComplexElementCovT]]:
        return DenseIntOrFPElementsAttr([type, BytesAttr(type.element_type.pack(data))])

    @overload
    @staticmethod
    def from_list(
        type: (
            RankedStructure[AnyFloat | IntegerType | IndexType]
            | RankedStructure[AnyFloat]
            | RankedStructure[IntegerType]
            | RankedStructure[IndexType]
        ),
        data: (
            Sequence[int]
            | Sequence[IntegerAttr[IndexType]]
            | Sequence[IntegerAttr[IntegerType]]
        ),
    ) -> DenseIntOrFPElementsAttr: ...

    @overload
    @staticmethod
    def from_list(
        type: (
            RankedStructure[AnyFloat | IntegerType | IndexType]
            | RankedStructure[AnyFloat]
            | RankedStructure[IntegerType]
            | RankedStructure[IndexType]
        ),
        data: Sequence[int | float] | Sequence[FloatAttr],
    ) -> DenseIntOrFPElementsAttr: ...

    @staticmethod
    @deprecated("Please use `create_dense_{int/float}` instead.")
    def from_list(
        type: (
            RankedStructure[AnyFloat | IntegerType | IndexType]
            | RankedStructure[AnyFloat]
            | RankedStructure[IntegerType]
            | RankedStructure[IndexType]
        ),
        data: Sequence[int | float] | Sequence[IntegerAttr] | Sequence[FloatAttr],
    ) -> DenseIntOrFPElementsAttr:
        # splat value given
        if len(data) == 1 and prod(type.get_shape()) != 1:
            new_data = data[0]
        else:
            new_data = data

        if isinstance(type.element_type, AnyFloat):
            new_type = cast(RankedStructure[AnyFloat], type)
            new_data = cast(
                float | FloatAttr | Sequence[float] | Sequence[FloatAttr[AnyFloat]],
                new_data,
            )
            return DenseIntOrFPElementsAttr.create_dense_float(new_type, new_data)
        else:
            new_type = cast(RankedStructure[IntegerType | IndexType], type)
            new_data = cast(
                int | IntegerAttr | Sequence[int] | Sequence[IntegerAttr], new_data
            )
            return DenseIntOrFPElementsAttr.create_dense_int(new_type, new_data)

    @staticmethod
    @deprecated("Please use `create_dense_{int/float}` instead.")
    def vector_from_list(
        data: Sequence[int] | Sequence[float],
        data_type: IntegerType | IndexType | AnyFloat,
        shape: Sequence[int] | None = None,
    ) -> DenseIntOrFPElementsAttr:
        if not shape:
            shape = [len(data)]
        if isinstance(data_type, AnyFloat):
            return DenseIntOrFPElementsAttr.create_dense_float(
                VectorType(data_type, shape), data
            )
        else:
            assert isinstance(data_type, IntegerType | IndexType)
            data = cast(Sequence[int], data)
            return DenseIntOrFPElementsAttr.create_dense_int(
                VectorType(data_type, shape), data
            )

    @staticmethod
    @deprecated("Please use `create_dense_{int/float}` instead.")
    def tensor_from_list(
        data: (
            Sequence[int]
            | Sequence[float]
            | Sequence[IntegerAttr[IndexType]]
            | Sequence[IntegerAttr[IntegerType]]
            | Sequence[FloatAttr]
        ),
        data_type: IntegerType | IndexType | AnyFloat,
        shape: Sequence[int],
    ) -> DenseIntOrFPElementsAttr:
        if isinstance(data_type, AnyFloat):
            new_data = cast(Sequence[float] | Sequence[FloatAttr], data)
            if len(new_data) == 1 and prod(shape) != 1:
                new_data = new_data[0]
            return DenseIntOrFPElementsAttr.create_dense_float(
                TensorType(data_type, shape), new_data
            )
        else:
            new_data = cast(Sequence[int] | Sequence[IntegerAttr], data)
            if len(new_data) == 1 and prod(shape) != 1:
                new_data = new_data[0]
            return DenseIntOrFPElementsAttr.create_dense_int(
                TensorType(data_type, shape), new_data
            )

    def iter_values(
        self,
    ) -> (
        Iterator[int]
        | Iterator[float]
        | Iterator[tuple[int, int]]
        | Iterator[tuple[float, float]]
    ):
        """
        Return an iterator over all the values of the elements in this DenseIntOrFPElementsAttr
        """
        return self.get_element_type().iter_unpack(self.data.data)

    def get_int_values(self) -> Sequence[int]:
        """
        Return all the values of the elements in this DenseIntOrFPElementsAttr,
        checking that the elements are integers.
        """
        el_type = self.get_element_type()
        assert isinstance(el_type, IntegerType | IndexType), el_type
        return el_type.unpack(self.data.data, len(self))

    def get_float_values(self) -> Sequence[float]:
        """
        Return all the values of the elements in this DenseIntOrFPElementsAttr,
        checking that the elements are floats.
        """
        el_type = self.get_element_type()
        assert isinstance(el_type, AnyFloat), el_type
        return el_type.unpack(self.data.data, len(self))

    def get_complex_values(
        self,
    ) -> Sequence[tuple[int, int]] | Sequence[tuple[float, float]]:
        """
        Return all the values of the elements in this DenseIntOrFPElementsAttr,
        checking that the elements are complex.
        """
        el_type = self.get_element_type()
        assert isinstance(el_type, ComplexType), el_type
        return el_type.unpack(self.data.data, len(self))

    def get_values(
        self,
    ) -> (
        Sequence[int]
        | Sequence[float]
        | Sequence[tuple[int, int]]
        | Sequence[tuple[float, float]]
    ):
        """
        Return all the values of the elements in this DenseIntOrFPElementsAttr
        """
        return self.get_element_type().unpack(self.data.data, len(self))

    def iter_attrs(self) -> Iterator[IntegerAttr] | Iterator[FloatAttr]:
        """
        Return an iterator over all elements of the dense attribute in their relevant
        attribute representation (IntegerAttr / FloatAttr)
        """
        if isinstance(eltype := self.get_element_type(), IntegerType | IndexType):
            return IntegerAttr.iter_unpack(eltype, self.data.data)
        elif isinstance(eltype, AnyFloat):
            return FloatAttr.iter_unpack(eltype, self.data.data)
        raise NotImplementedError()

    def get_attrs(self) -> Sequence[IntegerAttr] | Sequence[FloatAttr]:
        """
        Return all elements of the dense attribute in their relevant
        attribute representation (IntegerAttr / FloatAttr)
        """
        if isinstance(eltype := self.get_element_type(), IntegerType | IndexType):
            return IntegerAttr.unpack(eltype, self.data.data, len(self))
        elif isinstance(eltype, AnyFloat):
            return FloatAttr.unpack(eltype, self.data.data, len(self))
        raise NotImplementedError()

    def is_splat(self) -> bool:
        """
        Return whether or not this dense attribute is defined entirely
        by a single value (splat).
        """
        values = self.get_values()
        return values.count(values[0]) == len(values)

    @staticmethod
    def parse_with_type(parser: AttrParser, type: Attribute) -> TypedAttribute:
        assert isa(type, RankedStructure[AnyDenseElement])
        return parser.parse_dense_int_or_fp_elements_attr(type)

    def _print_one_elem(
        self, val: float | tuple[int, int] | tuple[float, float], printer: Printer
    ):
        if isinstance(val, int):
            element_type = cast(IntegerType | IndexType, self.get_element_type())
            element_type.print_value_without_type(val, printer)
        elif isinstance(val, float):
            printer.print_float(val, cast(AnyFloat, self.get_element_type()))
        else:
            raise NotImplementedError("Next PR")

    def _print_dense_list(
        self,
        array: Sequence[int]
        | Sequence[float]
        | Sequence[tuple[int, int]]
        | Sequence[tuple[float, float]],
        shape: Sequence[int],
        printer: Printer,
    ):
        printer.print_string("[")
        if len(shape) > 1:
            k = len(array) // shape[0]
            printer.print_list(
                (array[i : i + k] for i in range(0, len(array), k)),
                lambda subarray: self._print_dense_list(subarray, shape[1:], printer),
            )
        else:
            printer.print_list(
                array,
                lambda val: self._print_one_elem(val, printer),
            )
        printer.print_string("]")

    def print_without_type(self, printer: Printer):
        printer.print_string("dense<")
        data = self.get_values()
        shape = self.get_shape() if self.shape_is_complete else (len(data),)
        assert shape is not None, "If shape is complete, then it cannot be None"
        if len(data) == 0:
            pass
        elif self.is_splat():
            self._print_one_elem(data[0], printer)
        elif len(self) > 100:
            printer.print('"', "0x", self.data.data.hex().upper(), '"')
        else:
            self._print_dense_list(data, shape, printer)
        printer.print_string(">")


DenseIntElementsAttr: TypeAlias = DenseIntOrFPElementsAttr[IndexType | IntegerType]


Builtin = Dialect(
    "builtin",
    [
        ModuleOp,
        UnregisteredOp,
        UnrealizedConversionCastOp,
    ],
    [
        UnregisteredAttr,
        # Attributes
        StringAttr,
        SymbolRefAttr,
        SymbolNameAttr,
        IntAttr,
        IntegerAttr,
        ArrayAttr,
        DictionaryAttr,
        DenseIntOrFPElementsAttr,
        DenseResourceAttr,
        UnitAttr,
        FloatData,
        LocationAttr,
        NoneAttr,
        OpaqueAttr,
        # Types
        ComplexType,
        FunctionType,
        BFloat16Type,
        Float16Type,
        Float32Type,
        Float64Type,
        Float80Type,
        Float128Type,
        FloatAttr,
        SignednessAttr,
        TupleType,
        IntegerType,
        IndexType,
        NoneType,
        VectorType,
        TensorType,
        UnrankedTensorType,
        AffineMapAttr,
        AffineSetAttr,
        MemRefType,
        UnrankedMemRefType,
    ],
)
