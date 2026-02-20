"""Parse .iop files and build a symbol table."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import tree_sitter as ts
import tree_sitter_iop as tsiop

from .doc_comments import (
    get_doc_comment,
    get_field_doc_comment,
    get_trailing_doc_comment,
)
from .c_mapping import C_TYPE_SUFFIXES, iop_type_to_c
from .symbols import (
    EnumValueSymbol,
    FieldSymbol,
    Range,
    RpcSymbol,
    Symbol,
    SymbolKind,
)

log = logging.getLogger(__name__)

IOP_LANGUAGE = ts.Language(tsiop.language())

# Built-in IOP types that should not be resolved as references
BUILTIN_TYPES = frozenset({
    'int', 'uint', 'long', 'ulong', 'byte', 'ubyte',
    'short', 'ushort', 'bool', 'double', 'bytes', 'string',
    'xml', 'void',
})

# Map tree-sitter node types to SymbolKind
_NODE_TYPE_TO_KIND = {
    'data_structure_definition': None,  # determined by child
    'class_definition': SymbolKind.CLASS,
    'enum_definition': SymbolKind.ENUM,
    'interface_definition': SymbolKind.INTERFACE,
    'module_definition': SymbolKind.MODULE,
    'typedef_definition': SymbolKind.TYPEDEF,
    'snmp_object_definition': SymbolKind.SNMP_OBJ,
    'snmp_table_definition': SymbolKind.SNMP_TBL,
    'snmp_interface_definition': SymbolKind.SNMP_IFACE,
}


def _node_range(node: ts.Node) -> Range:
    sp = node.start_point
    ep = node.end_point
    return Range(sp.row, sp.column, ep.row, ep.column)


def _find_child(node: ts.Node, type_name: str) -> Optional[ts.Node]:
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _find_children(node: ts.Node, type_name: str) -> list[ts.Node]:
    return [c for c in node.children if c.type == type_name]


def _node_text(node: Optional[ts.Node]) -> Optional[str]:
    if node is None:
        return None
    return node.text.decode('utf-8')


def _find_identifier(node: ts.Node) -> Optional[ts.Node]:
    """Find the first identifier child of a node."""
    return _find_child(node, 'identifier')


@dataclass
class SymbolIndex:
    """Index of all IOP symbols in the workspace."""

    # simple name -> list of symbols (may have duplicates across pkgs)
    by_name: dict[str, list[Symbol]] = field(default_factory=dict)
    # 'pkg.Name' -> symbol
    by_qualified_name: dict[str, Symbol] = field(default_factory=dict)
    # package name -> symbols
    by_package: dict[str, list[Symbol]] = field(default_factory=dict)
    # file path -> symbols
    by_file: dict[str, list[Symbol]] = field(default_factory=dict)
    # file path -> package name
    package_of_file: dict[str, str] = field(default_factory=dict)
    # C identifier base name -> symbol (e.g., 'tstiop__my_struct_a' -> Symbol)
    by_c_name: dict[str, Symbol] = field(default_factory=dict)

    def resolve(
        self,
        name: str,
        current_package: Optional[str] = None,
    ) -> Optional[Symbol]:
        """Resolve a type reference to a symbol.

        Args:
            name: Simple or qualified type name.
            current_package: Package of the file where reference appears.
        """
        if name in BUILTIN_TYPES:
            return None

        # Qualified name: pkg.TypeName
        if '.' in name:
            # Could be a qualified name or a dotted identifier
            sym = self.by_qualified_name.get(name)
            if sym is not None:
                return sym
            # Try splitting at last dot for nested package names
            # e.g., 'foo.bar.Type' -> package 'foo.bar', type 'Type'
            parts = name.rsplit('.', 1)
            if len(parts) == 2:
                pkg, type_name = parts
                pkg_symbols = self.by_package.get(pkg, [])
                for s in pkg_symbols:
                    if s.name == type_name:
                        return s
            return None

        # Simple name: prefer same-package, then global
        candidates = self.by_name.get(name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Prefer same-package match
        if current_package:
            for c in candidates:
                if c.package == current_package:
                    return c
        return candidates[0]

    def resolve_enum_value(
        self,
        value_name: str,
        current_package: Optional[str] = None,
    ) -> Optional[tuple[Symbol, EnumValueSymbol]]:
        """Resolve an enum value reference (e.g., LOG_LEVEL_DEFAULT)."""
        # Search in same package first, then globally
        if current_package:
            for sym in self.by_package.get(current_package, []):
                if sym.kind == SymbolKind.ENUM:
                    for ev in sym.enum_values:
                        if ev.name == value_name:
                            return (sym, ev)
        for syms in self.by_name.values():
            for sym in syms:
                if sym.kind == SymbolKind.ENUM:
                    for ev in sym.enum_values:
                        if ev.name == value_name:
                            return (sym, ev)
        return None

    def add_symbol(self, sym: Symbol) -> None:
        self.by_name.setdefault(sym.name, []).append(sym)
        self.by_qualified_name[sym.qualified_name] = sym
        self.by_package.setdefault(sym.package, []).append(sym)
        self.by_file.setdefault(sym.file, []).append(sym)
        # Index by C name
        c_name = iop_type_to_c(sym.qualified_name)
        self.by_c_name[c_name] = sym
        # Also index @ctype override if present
        if sym.ctype:
            ctype_base = sym.ctype
            for suffix in C_TYPE_SUFFIXES:
                if ctype_base.endswith(suffix):
                    ctype_base = ctype_base[:-len(suffix)]
                    break
            self.by_c_name[ctype_base] = sym

    def remove_file(self, filepath: str) -> None:
        """Remove all symbols from a file."""
        symbols = self.by_file.pop(filepath, [])
        pkg = self.package_of_file.pop(filepath, None)
        for sym in symbols:
            # Remove from by_name
            name_list = self.by_name.get(sym.name, [])
            self.by_name[sym.name] = [
                s for s in name_list if s.file != filepath
            ]
            if not self.by_name[sym.name]:
                del self.by_name[sym.name]
            # Remove from by_qualified_name
            self.by_qualified_name.pop(sym.qualified_name, None)
            # Remove from by_package
            if pkg:
                pkg_list = self.by_package.get(pkg, [])
                self.by_package[pkg] = [
                    s for s in pkg_list if s.file != filepath
                ]
                if not self.by_package[pkg]:
                    del self.by_package[pkg]
            # Remove from by_c_name
            c_name = iop_type_to_c(sym.qualified_name)
            self.by_c_name.pop(c_name, None)
            if sym.ctype:
                ctype_base = sym.ctype
                for suffix in C_TYPE_SUFFIXES:
                    if ctype_base.endswith(suffix):
                        ctype_base = ctype_base[:-len(suffix)]
                        break
                self.by_c_name.pop(ctype_base, None)

    def resolve_c_identifier(self, c_ident: str) -> Optional[Symbol]:
        """Resolve a C identifier like 'tstiop__my_struct_a__t' to an IOP symbol."""
        # Strip known C type suffixes
        base = c_ident
        for suffix in C_TYPE_SUFFIXES:
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        return self.by_c_name.get(base)


class Indexer:
    """Indexes .iop files in a workspace."""

    def __init__(self) -> None:
        self.parser = ts.Parser(IOP_LANGUAGE)
        self.index = SymbolIndex()

    def index_workspace(self, root_path: str) -> None:
        """Recursively find and index all .iop files under root_path."""
        for dirpath, _dirnames, filenames in os.walk(root_path):
            for fname in filenames:
                if fname.endswith('.iop'):
                    filepath = os.path.join(dirpath, fname)
                    self.index_file(filepath)
        log.info(
            'Indexed %d symbols in %d files',
            len(self.index.by_qualified_name),
            len(self.index.by_file),
        )

    def index_file(self, filepath: str) -> None:
        """Parse and index a single .iop file."""
        filepath = os.path.abspath(filepath)
        # Remove old symbols for this file first (re-index case)
        self.index.remove_file(filepath)

        try:
            with open(filepath, 'rb') as f:
                source = f.read()
        except OSError as e:
            log.warning('Cannot read %s: %s', filepath, e)
            return

        self._index_source(filepath, source)

    def index_source(self, filepath: str, source: bytes) -> None:
        """Index from source bytes (for open documents)."""
        filepath = os.path.abspath(filepath)
        self.index.remove_file(filepath)
        self._index_source(filepath, source)

    def _index_source(self, filepath: str, source: bytes) -> None:
        tree = self.parser.parse(source)
        root = tree.root_node

        # Extract package name
        package = None
        for child in root.children:
            if child.type == 'package_definition':
                id_node = _find_identifier(child)
                if id_node:
                    package = _node_text(id_node)
                break

        if package is None:
            log.warning('No package declaration in %s', filepath)
            return

        self.index.package_of_file[filepath] = package

        # Extract type definitions
        for child in root.children:
            sym = self._extract_symbol(child, filepath, package, source)
            if sym is not None:
                self.index.add_symbol(sym)

    def _extract_symbol(
        self,
        node: ts.Node,
        filepath: str,
        package: str,
        source: bytes,
    ) -> Optional[Symbol]:
        kind = _NODE_TYPE_TO_KIND.get(node.type)
        if kind is None and node.type != 'data_structure_definition':
            return None

        if node.type == 'data_structure_definition':
            ds_type = _find_child(node, 'data_structure_type')
            if ds_type:
                type_text = _node_text(ds_type)
                kind = (
                    SymbolKind.STRUCT if type_text == 'struct'
                    else SymbolKind.UNION
                )
            else:
                return None

        # For typedef, the identifier is inside the variable child
        if node.type == 'typedef_definition':
            var = _find_child(node, 'variable')
            id_node = _find_identifier(var) if var else None
        else:
            id_node = _find_identifier(node)
        if id_node is None:
            return None

        name = _node_text(id_node)
        if name is None:
            return None

        qualified_name = f'{package}.{name}'
        doc = get_doc_comment(node)

        # Parent class for class definitions
        parent_class = None
        if node.type == 'class_definition':
            for inh in _find_children(node, 'class_inheritance'):
                inh_id = _find_identifier(inh)
                if inh_id:
                    parent_class = _node_text(inh_id)

        # Parse @ctype attribute if present
        ctype = self._extract_ctype(node)

        sym = Symbol(
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            file=filepath,
            range=_node_range(id_node),
            doc=doc,
            package=package,
            parent_class=parent_class,
            ctype=ctype,
        )

        # Extract children based on kind
        if kind in (
            SymbolKind.STRUCT, SymbolKind.UNION,
            SymbolKind.SNMP_OBJ, SymbolKind.SNMP_TBL,
        ):
            block = _find_child(node, 'data_structure_block')
            if block:
                sym.fields = self._extract_fields(block)
        elif kind == SymbolKind.CLASS:
            block = _find_child(node, 'data_structure_block')
            if block:
                sym.fields = self._extract_fields(block)
        elif kind == SymbolKind.ENUM:
            block = _find_child(node, 'enum_block')
            if block:
                sym.enum_values = self._extract_enum_values(block)
        elif kind == SymbolKind.INTERFACE:
            block = _find_child(node, 'rpc_block')
            if block:
                sym.rpcs = self._extract_rpcs(block)
        elif kind == SymbolKind.MODULE:
            block = _find_child(node, 'module_block')
            if block:
                sym.fields = self._extract_module_fields(block)
        elif kind == SymbolKind.TYPEDEF:
            var = _find_child(node, 'variable')
            if var:
                type_node = _find_child(var, 'type')
                if type_node:
                    sym.typedef_source = _node_text(type_node)

        return sym

    def _extract_ctype(self, node: ts.Node) -> Optional[str]:
        """Extract @ctype(name) attribute value from a definition node."""
        for child in node.children:
            if child.type != 'attribute':
                continue
            # attribute children: '@', identifier?, attribute_argument_list?
            attr_id = _find_child(child, 'identifier')
            if attr_id and _node_text(attr_id) == 'ctype':
                arg_list = _find_child(child, 'attribute_argument_list')
                if arg_list:
                    content = _find_child(arg_list, 'attribute_content')
                    if content:
                        return _node_text(content).strip()
        return None

    def _extract_fields(
        self, block: ts.Node
    ) -> list[FieldSymbol]:
        fields = []
        for child in block.children:
            if child.type == 'field':
                var = _find_child(child, 'variable')
                if var is None:
                    continue
                type_node = _find_child(var, 'type')
                type_spec = _find_child(var, 'type_specifier')
                id_node = _find_identifier(var)
                default = _find_child(var, 'default_value')

                type_text = _node_text(type_node)
                type_ref = (
                    type_text if type_text
                    and type_text not in BUILTIN_TYPES
                    else None
                )

                fields.append(FieldSymbol(
                    name=_node_text(id_node) or '',
                    type_ref=type_ref,
                    specifier=_node_text(type_spec),
                    default_value=_node_text(default),
                    range=_node_range(id_node) if id_node else _node_range(
                        child
                    ),
                    doc=get_field_doc_comment(child),
                ))
        return fields

    def _extract_enum_values(
        self, block: ts.Node,
    ) -> list[EnumValueSymbol]:
        values = []
        for child in block.children:
            if child.type == 'enum_field':
                id_node = _find_identifier(child)
                default = _find_child(child, 'default_value')

                doc = get_field_doc_comment(child)
                # Also check trailing comment on the enum_field
                if doc is None:
                    doc = get_trailing_doc_comment(child)

                values.append(EnumValueSymbol(
                    name=_node_text(id_node) or '',
                    value=(
                        _node_text(default).lstrip('= ').strip()
                        if default else None
                    ),
                    range=_node_range(id_node) if id_node else _node_range(
                        child
                    ),
                    doc=doc,
                ))
        return values

    def _extract_rpcs(self, block: ts.Node) -> list[RpcSymbol]:
        rpcs = []
        for child in block.children:
            if child.type == 'rpc':
                id_node = _find_identifier(child)
                doc = get_doc_comment(child)

                rpc_in = _find_child(child, 'rpc_in')
                rpc_out = _find_child(child, 'rpc_out')
                rpc_throw = _find_child(child, 'rpc_throw')

                rpcs.append(RpcSymbol(
                    name=_node_text(id_node) or '',
                    in_type=self._extract_rpc_type_ref(rpc_in),
                    out_type=self._extract_rpc_type_ref(rpc_out),
                    throw_type=self._extract_rpc_type_ref(rpc_throw),
                    range=_node_range(id_node) if id_node else _node_range(
                        child
                    ),
                    doc=doc,
                ))
        return rpcs

    def _extract_rpc_type_ref(
        self, rpc_clause: Optional[ts.Node],
    ) -> Optional[str]:
        """Extract a single-type reference from rpc in/out/throw."""
        if rpc_clause is None:
            return None
        # If it has an argument_list, it's an inline struct, no single ref
        if _find_child(rpc_clause, 'argument_list'):
            return None
        id_node = _find_identifier(rpc_clause)
        if id_node:
            text = _node_text(id_node)
            if text and text not in BUILTIN_TYPES and text not in (
                'null', 'void'
            ):
                return text
        return None

    def _extract_module_fields(
        self, block: ts.Node,
    ) -> list[FieldSymbol]:
        fields = []
        for child in block.children:
            if child.type == 'module_field':
                ids = _find_children(child, 'identifier')
                if len(ids) >= 2:
                    type_id = ids[0]  # interface type
                    name_id = ids[1]  # field name
                    fields.append(FieldSymbol(
                        name=_node_text(name_id) or '',
                        type_ref=_node_text(type_id),
                        specifier=None,
                        default_value=None,
                        range=_node_range(name_id),
                        doc=get_field_doc_comment(child),
                    ))
        return fields
