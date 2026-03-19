"""Symbol data structures for the IOP LSP."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SymbolKind(Enum):
    STRUCT = 'struct'
    UNION = 'union'
    CLASS = 'class'
    ENUM = 'enum'
    INTERFACE = 'interface'
    MODULE = 'module'
    TYPEDEF = 'typedef'
    SNMP_OBJ = 'snmpObj'
    SNMP_TBL = 'snmpTbl'
    SNMP_IFACE = 'snmpIface'


@dataclass
class Range:
    start_line: int  # 0-indexed
    start_col: int
    end_line: int
    end_col: int


@dataclass
class FieldSymbol:
    name: str
    type_ref: Optional[str]  # Referenced type name (None for built-ins)
    specifier: Optional[str]  # '?', '&', '[]', or None
    default_value: Optional[str]
    range: Range
    doc: Optional[str]
    full_range: Optional[Range] = None  # Range of the entire field node
    type_range: Optional[Range] = None  # Range of the type name token


@dataclass
class RpcSymbol:
    name: str
    in_type: Optional[str]  # Single type ref, or None if arg list/void
    out_type: Optional[str]
    throw_type: Optional[str]
    range: Range
    doc: Optional[str]
    full_range: Optional[Range] = None  # Range of the entire rpc node
    in_type_range: Optional[Range] = None
    out_type_range: Optional[Range] = None
    throw_type_range: Optional[Range] = None


@dataclass
class EnumValueSymbol:
    name: str
    value: Optional[str]
    range: Range
    doc: Optional[str]
    full_range: Optional[Range] = None  # Range of the entire enum_field node


@dataclass
class Symbol:
    name: str  # Simple name (e.g., 'LogLevel')
    qualified_name: str  # Package-qualified (e.g., 'core.LogLevel')
    kind: SymbolKind
    file: str  # Absolute file path
    range: Range
    doc: Optional[str]
    package: str
    parent_class: Optional[str]  # For classes, the parent class name
    full_range: Optional[Range] = None  # Range of the entire definition node
    fields: list[FieldSymbol] = field(default_factory=list)
    enum_values: list[EnumValueSymbol] = field(default_factory=list)
    rpcs: list[RpcSymbol] = field(default_factory=list)
    # For typedef: the source type
    typedef_source: Optional[str] = None
    typedef_source_range: Optional[Range] = None
    # For class: range of the parent class name token
    parent_class_range: Optional[Range] = None
    # @ctype override (e.g., 'http_code__t')
    ctype: Optional[str] = None
    # @prefix override for enums (e.g., 'A' for @prefix(A))
    enum_prefix: Optional[str] = None
