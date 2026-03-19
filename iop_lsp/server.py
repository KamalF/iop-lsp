"""IOP Language Server Protocol implementation."""

from __future__ import annotations

import argparse
import logging
import os
import re
from typing import Optional

import tree_sitter as ts
import tree_sitter_iop as tsiop
from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from .c_mapping import camelcase_to_c
from .indexer import BUILTIN_TYPES, IOP_LANGUAGE, Indexer, _find_child
from .symbols import (
    EnumValueSymbol, FieldSymbol, RpcSymbol, Symbol, SymbolKind,
)

log = logging.getLogger(__name__)

server = LanguageServer('iop-lsp', 'v0.1.0')
indexer = Indexer()
_parser = ts.Parser(IOP_LANGUAGE)

# Pre-compiled tree-sitter query for finding type references across files
_REFERENCE_QUERY = ts.Query(IOP_LANGUAGE, """
(variable (type (identifier) @ref))
(class_inheritance (identifier) @ref)
(rpc_in (identifier) @ref)
(rpc_out (identifier) @ref)
(rpc_throw (identifier) @ref)
(module_field (identifier) @ref)
""")


def _get_tree(uri: str) -> Optional[ts.Tree]:
    """Parse the current document content."""
    doc = server.workspace.get_text_document(uri)
    source = doc.source.encode('utf-8')
    return _parser.parse(source)


def _get_package_for_uri(uri: str) -> Optional[str]:
    """Get the package name for a document URI."""
    path = _uri_to_path(uri)
    return indexer.index.package_of_file.get(path)


def _uri_to_path(uri: str) -> str:
    """Convert a file URI to an absolute path."""
    if uri.startswith('file://'):
        return uri[7:]
    return uri


_C_WORD_RE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')

# Known IOP attributes for completion
_IOP_ATTRIBUTES = [
    'strict', 'nonEmpty', 'nonZero', 'min', 'max',
    'minLength', 'maxLength', 'length', 'pattern',
    'ctype', 'prefix', 'allow', 'disallow',
    'private', 'deprecated', 'cdata', 'noReorder',
    'alias', 'minOccurs', 'maxOccurs', 'forceFieldName',
]

# Known doc comment tags for completion
_DOC_TAGS = [
    'ref', 'p', 'c', 'a', 'see',
    'class', 'struct', 'enum', 'typedef', 'union',
    'brief', 'param', 'return', 'returns',
    'note', 'warning', 'deprecated',
]

# Patterns for completion context detection
_ATTR_RE = re.compile(r'@(\w*)$')
_DOC_TAG_RE = re.compile(r'\\(\w*)$')
_DOC_REF_PARTIAL_RE = re.compile(
    r'\\(?:p|c|a|ref|see|class|struct|enum|typedef|union)\s+(\w[\w.]*)$'
)
_QUALIFIED_TYPE_RE = re.compile(r'(\w+)\.(\w*)$')
_ENUM_VALUE_RE = re.compile(r'=\s*(\w*)$')
_FIELD_TYPE_RE = re.compile(r'^\s*(\w*)$')

_DOC_REF_RE = re.compile(
    r'\\(p|c|a|ref|see|class|struct|enum|typedef|union)\s+(.*)'
)

# Tags that constrain the symbol kind
_TAG_KIND_MAP: dict[str, SymbolKind] = {
    'struct': SymbolKind.STRUCT,
    'enum': SymbolKind.ENUM,
    'class': SymbolKind.CLASS,
    'union': SymbolKind.UNION,
    'typedef': SymbolKind.TYPEDEF,
}


def _parse_doc_ref(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse doc_ref text into (tag, identifier)."""
    m = _DOC_REF_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    return None, None

# File extensions treated as C files for IOP identifier resolution
_C_EXTENSIONS = ('.c', '.h', '.blk')


def _is_c_file(uri: str) -> bool:
    """Check if a URI refers to a C/header/blk file."""
    path = _uri_to_path(uri).lower()
    return any(path.endswith(ext) for ext in _C_EXTENSIONS)


def _is_inside_doc_comment(text: str, line: int) -> bool:
    """Check if the cursor line is inside a /** */ doc comment."""
    lines = text.split('\n')
    in_doc = False
    for i, ln in enumerate(lines):
        if i > line:
            break
        if '/**' in ln:
            in_doc = True
        if '*/' in ln:
            # If */ is on the cursor line, check column-wise later;
            # for simplicity, if the line has both /** and */, treat
            # it as inside if /** comes first
            if i < line:
                in_doc = False
    return in_doc


def _is_inside_block(text: str, line: int, col: int) -> bool:
    """Check if cursor position is inside a {} block by counting braces."""
    lines = text.split('\n')
    depth = 0
    for i, ln in enumerate(lines):
        end = col if i == line else len(ln)
        for ch in ln[:end]:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        if i == line:
            break
    return depth > 0


def _get_completion_context(
    doc_text: str, line: int, col: int,
) -> tuple[str, str, Optional[str]]:
    """Determine the completion context at cursor position.

    Returns (context_kind, partial_text, package_prefix) where:
    - context_kind: 'attribute', 'doc_tag', 'doc_ref', 'qualified_type',
                    'enum_value', 'field_type', or 'none'
    - partial_text: what the user has typed so far
    - package_prefix: package name when context is 'qualified_type'
    """
    lines = doc_text.split('\n')
    if line < 0 or line >= len(lines):
        return ('none', '', None)

    before_cursor = lines[line][:col]

    # 1. Attribute: @partial
    m = _ATTR_RE.search(before_cursor)
    if m:
        return ('attribute', m.group(1), None)

    in_doc = _is_inside_doc_comment(doc_text, line)

    if in_doc:
        # 2. Doc ref: \ref Partial, \see Partial, etc.
        m = _DOC_REF_PARTIAL_RE.search(before_cursor)
        if m:
            return ('doc_ref', m.group(1), None)

        # 3. Doc tag: \partial
        m = _DOC_TAG_RE.search(before_cursor)
        if m:
            return ('doc_tag', m.group(1), None)

    in_block = _is_inside_block(doc_text, line, col)

    if in_block:
        # 4. Qualified type: pkg.Partial
        m = _QUALIFIED_TYPE_RE.search(before_cursor)
        if m:
            return ('qualified_type', m.group(2), m.group(1))

        # 5. Enum value: = PARTIAL
        m = _ENUM_VALUE_RE.search(before_cursor)
        if m:
            return ('enum_value', m.group(1), None)

        # 6. Field type: identifier at start of line inside block
        m = _FIELD_TYPE_RE.match(before_cursor)
        if m is not None:
            return ('field_type', m.group(1), None)

    return ('none', '', None)


def _get_word_at_position(
    text: str, line: int, col: int,
) -> Optional[str]:
    """Extract the C identifier at (line, col) from raw text."""
    lines = text.split('\n')
    if line < 0 or line >= len(lines):
        return None
    ln = lines[line]
    for m in _C_WORD_RE.finditer(ln):
        if m.start() <= col <= m.end():
            return m.group()
    return None


def _node_at_position(
    tree: ts.Tree, line: int, col: int,
) -> Optional[ts.Node]:
    """Find the named node at the given position."""
    root = tree.root_node
    node = root.named_descendant_for_point_range(
        (line, col), (line, col)
    )
    return node


def _is_type_reference_context(node: ts.Node) -> bool:
    """Check if a node is in a type reference context."""
    if node.type != 'identifier' and node.type != 'type':
        return False

    parent = node.parent
    if parent is None:
        return False

    # identifier inside a 'type' node (field type)
    if node.type == 'identifier' and parent.type == 'type':
        return True

    # 'type' node inside a 'variable' (field/arg type)
    if node.type == 'type' and parent.type == 'variable':
        return True

    # class_inheritance -> identifier (parent class ref)
    if parent.type == 'class_inheritance':
        return True

    # rpc_in/rpc_out/rpc_throw -> identifier (single type ref)
    if parent.type in ('rpc_in', 'rpc_out', 'rpc_throw'):
        return True

    # module_field: first identifier is the type
    if parent.type == 'module_field':
        ids = [c for c in parent.children if c.type == 'identifier']
        if ids and node.id == ids[0].id:
            return True

    return False


def _get_type_ref_at_position(
    tree: ts.Tree, line: int, col: int,
) -> Optional[str]:
    """Get the type reference text at the given position."""
    node = _node_at_position(tree, line, col)
    if node is None:
        return None

    text = node.text.decode('utf-8') if node.text else None
    if text is None:
        return None

    # Check if it's in a type context
    if _is_type_reference_context(node):
        return text

    # Check if the node is an identifier that could be an enum value
    # in a default_value context
    if node.type == 'identifier' and node.parent:
        if node.parent.type == 'value':
            return text
        # value can contain repeat1(choice(..., identifier, ...))
        # Check grandparent too
        gp = node.parent.parent if node.parent else None
        if gp and gp.type == 'default_value':
            return text

    return None


def _get_node_context_at_position(
    tree: ts.Tree, line: int, col: int,
) -> tuple[Optional[ts.Node], str]:
    """Get node and its context type at position.

    Returns (node, context_type) where context_type is one of:
    'type_ref', 'enum_value', 'field_name', 'enum_value_def',
    'type_def', 'rpc_name', or 'unknown'.
    """
    node = _node_at_position(tree, line, col)
    if node is None:
        return None, 'unknown'

    # doc_ref is an atomic token with no children
    if node.type == 'doc_ref':
        return node, 'doc_ref'

    parent = node.parent

    if _is_type_reference_context(node):
        return node, 'type_ref'

    if node.type == 'identifier' and parent:
        # Enum value in default_value
        if parent.type == 'value' or (
            parent.parent and parent.parent.type == 'default_value'
        ):
            return node, 'enum_value'

        # Field name (identifier in a variable, but not the type)
        if parent.type == 'variable':
            type_node = _find_child(parent, 'type')
            if type_node and node.id != type_node.id:
                # This is the field name identifier
                return node, 'field_name'

        # Enum value definition (identifier in enum_field)
        if parent.type == 'enum_field':
            return node, 'enum_value_def'

        # Type name definition
        if parent.type in (
            'data_structure_definition', 'class_definition',
            'enum_definition', 'interface_definition',
            'module_definition', 'snmp_object_definition',
            'snmp_table_definition', 'snmp_interface_definition',
        ):
            return node, 'type_def'

        # RPC name
        if parent.type == 'rpc':
            return node, 'rpc_name'

    if node.type == 'type':
        # 'type' node that's a builtin - could still be hovered
        return node, 'type_ref'

    return node, 'unknown'


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
    path = _uri_to_path(params.text_document.uri)
    source = params.text_document.text.encode('utf-8')
    indexer.index_source(path, source)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
    path = _uri_to_path(params.text_document.uri)
    indexer.index_file(path)


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
    # Re-index from the latest content
    doc = server.workspace.get_text_document(
        params.text_document.uri
    )
    path = _uri_to_path(params.text_document.uri)
    source = doc.source.encode('utf-8')
    indexer.index_source(path, source)


def _find_references_in_file(
    filepath: str,
    source: bytes,
    target_names: set[str],
) -> list[lsp.Location]:
    """Find all references to target_names in a single file's source."""
    tree = _parser.parse(source)
    root = tree.root_node
    locations: list[lsp.Location] = []
    cursor = ts.QueryCursor(_REFERENCE_QUERY)

    for pattern_idx, match in cursor.matches(root):
        for capture_name, nodes in match.items():
            for node in nodes:
                text = node.text.decode('utf-8') if node.text else None
                if text is None or text not in target_names:
                    continue
                # For module_field (pattern index 5), skip the field name
                # (second identifier) — only the first is the type reference
                if pattern_idx == 5 and node.parent:
                    ids = [
                        c for c in node.parent.children
                        if c.type == 'identifier'
                    ]
                    if len(ids) >= 2 and node.id == ids[1].id:
                        continue
                sp = node.start_point
                ep = node.end_point
                locations.append(lsp.Location(
                    uri=f'file://{filepath}',
                    range=lsp.Range(
                        start=lsp.Position(line=sp.row, character=sp.column),
                        end=lsp.Position(line=ep.row, character=ep.column),
                    ),
                ))
    return locations


def _find_all_references(
    sym: Symbol,
    include_declaration: bool,
) -> list[lsp.Location]:
    """Find all references to a symbol across all indexed files."""
    target_names = {sym.name, sym.qualified_name}
    locations: list[lsp.Location] = []

    if include_declaration:
        locations.append(_symbol_to_location(sym))

    for filepath in indexer.index.by_file:
        # Try to get source from open documents first
        uri = f'file://{filepath}'
        try:
            doc = server.workspace.get_text_document(uri)
            source = doc.source.encode('utf-8')
        except Exception:
            try:
                with open(filepath, 'rb') as f:
                    source = f.read()
            except OSError:
                continue
        locations.extend(
            _find_references_in_file(filepath, source, target_names)
        )
    return locations


@server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
def find_references(
    params: lsp.ReferenceParams,
) -> Optional[list[lsp.Location]]:
    uri = params.text_document.uri
    line = params.position.line
    col = params.position.character

    tree = _get_tree(uri)
    if tree is None:
        return None

    node, context = _get_node_context_at_position(tree, line, col)
    if node is None:
        return None

    text = node.text.decode('utf-8') if node.text else None
    if text is None:
        return None

    # For type nodes, get the identifier text
    if node.type == 'type':
        for child in node.children:
            if child.type == 'identifier':
                text = child.text.decode('utf-8')
                break

    current_package = _get_package_for_uri(uri)
    sym: Optional[Symbol] = None

    if context == 'type_def':
        # On a type definition — find who references this type
        qname = f'{current_package}.{text}' if current_package else text
        sym = indexer.index.by_qualified_name.get(qname)

    elif context in ('type_ref', 'field_name'):
        sym = indexer.index.resolve(text, current_package)

    elif context == 'enum_value_def' or context == 'enum_value':
        # Resolve to parent enum symbol
        result = indexer.index.resolve_enum_value(text, current_package)
        if result:
            sym = result[0]

    elif context == 'rpc_name':
        # Resolve to parent interface symbol
        if node.parent and node.parent.type == 'rpc':
            # Walk up to interface_definition
            iface_node = node.parent.parent  # rpc_block
            if iface_node:
                iface_node = iface_node.parent  # interface_definition
            if iface_node:
                iface_id = None
                for child in iface_node.children:
                    if child.type == 'identifier':
                        iface_id = child
                        break
                if iface_id:
                    iface_name = iface_id.text.decode('utf-8')
                    sym = indexer.index.resolve(
                        iface_name, current_package,
                    )

    if sym is None:
        return None

    include_declaration = params.context.include_declaration
    locations = _find_all_references(sym, include_declaration)
    return locations or None


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    params: lsp.DefinitionParams,
) -> Optional[lsp.Location]:
    uri = params.text_document.uri
    line = params.position.line
    col = params.position.character

    # C file path: extract word at cursor, resolve as IOP C identifier
    if _is_c_file(uri):
        doc = server.workspace.get_text_document(uri)
        word = _get_word_at_position(doc.source, line, col)
        if word:
            sym = indexer.index.resolve_c_identifier(word)
            if sym:
                return _symbol_to_location(sym)
        return None

    tree = _get_tree(uri)
    if tree is None:
        return None

    node, context = _get_node_context_at_position(tree, line, col)
    if node is None:
        return None

    text = node.text.decode('utf-8') if node.text else None
    if text is None:
        return None

    # For type nodes, get the text content
    if node.type == 'type':
        # Get the identifier child if any
        for child in node.children:
            if child.type == 'identifier':
                text = child.text.decode('utf-8')
                break

    current_package = _get_package_for_uri(uri)

    if context == 'type_ref':
        sym = indexer.index.resolve(text, current_package)
        if sym:
            return _symbol_to_location(sym)

    elif context == 'enum_value':
        result = indexer.index.resolve_enum_value(text, current_package)
        if result:
            enum_sym, enum_val = result
            return lsp.Location(
                uri=f'file://{enum_sym.file}',
                range=_range_to_lsp(enum_val.range),
            )

    elif context == 'doc_ref':
        tag, identifier = _parse_doc_ref(text)
        if identifier:
            sym = indexer.index.resolve(identifier, current_package)
            if sym:
                # If the tag constrains the kind, filter
                required_kind = _TAG_KIND_MAP.get(tag)
                if required_kind is None or sym.kind == required_kind:
                    return _symbol_to_location(sym)

    elif context == 'field_name':
        # Go to the type of this field
        if node.parent and node.parent.type == 'variable':
            type_node = _find_child(node.parent, 'type')
            if type_node:
                type_text = type_node.text.decode('utf-8')
                sym = indexer.index.resolve(type_text, current_package)
                if sym:
                    return _symbol_to_location(sym)

    return None


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def hover(params: lsp.HoverParams) -> Optional[lsp.Hover]:
    uri = params.text_document.uri
    line = params.position.line
    col = params.position.character

    # C file path: extract word at cursor, resolve as IOP C identifier
    if _is_c_file(uri):
        doc = server.workspace.get_text_document(uri)
        word = _get_word_at_position(doc.source, line, col)
        if word:
            sym = indexer.index.resolve_c_identifier(word)
            if sym:
                return lsp.Hover(
                    contents=lsp.MarkupContent(
                        kind=lsp.MarkupKind.Markdown,
                        value=_format_symbol_hover(sym),
                    ),
                )
        return None

    tree = _get_tree(uri)
    if tree is None:
        return None

    node, context = _get_node_context_at_position(tree, line, col)
    if node is None:
        return None

    text = node.text.decode('utf-8') if node.text else None
    if text is None:
        return None

    # For type nodes, get the actual type text
    if node.type == 'type':
        for child in node.children:
            if child.type == 'identifier':
                text = child.text.decode('utf-8')
                break

    current_package = _get_package_for_uri(uri)

    if context == 'type_ref':
        if text in BUILTIN_TYPES:
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown,
                    value=f'**{text}** (built-in type)',
                ),
            )
        sym = indexer.index.resolve(text, current_package)
        if sym:
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown,
                    value=_format_symbol_hover(sym),
                ),
            )

    elif context == 'doc_ref':
        tag, identifier = _parse_doc_ref(text)
        if identifier:
            sym = indexer.index.resolve(identifier, current_package)
            if sym:
                required_kind = _TAG_KIND_MAP.get(tag)
                if required_kind is None or sym.kind == required_kind:
                    return lsp.Hover(
                        contents=lsp.MarkupContent(
                            kind=lsp.MarkupKind.Markdown,
                            value=_format_symbol_hover(sym),
                        ),
                    )

    elif context == 'enum_value':
        result = indexer.index.resolve_enum_value(text, current_package)
        if result:
            enum_sym, enum_val = result
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown,
                    value=_format_enum_value_hover(enum_sym, enum_val),
                ),
            )

    elif context == 'field_name':
        # Show field info
        hover_text = _format_field_hover_from_node(
            node, tree, current_package,
        )
        if hover_text:
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown,
                    value=hover_text,
                ),
            )

    elif context == 'enum_value_def':
        # Hovering on an enum value definition
        hover_text = _format_enum_value_def_hover(
            node, current_package,
        )
        if hover_text:
            return lsp.Hover(
                contents=lsp.MarkupContent(
                    kind=lsp.MarkupKind.Markdown,
                    value=hover_text,
                ),
            )

    elif context == 'type_def':
        # Hovering on a type name definition
        if text:
            qname = f'{current_package}.{text}' if current_package else text
            sym = indexer.index.by_qualified_name.get(qname)
            if sym:
                return lsp.Hover(
                    contents=lsp.MarkupContent(
                        kind=lsp.MarkupKind.Markdown,
                        value=_format_symbol_hover(sym),
                    ),
                )

    return None


# Map IOP SymbolKind to LSP SymbolKind
_IOP_TO_LSP_KIND: dict[SymbolKind, lsp.SymbolKind] = {
    SymbolKind.STRUCT: lsp.SymbolKind.Struct,
    SymbolKind.UNION: lsp.SymbolKind.Struct,
    SymbolKind.CLASS: lsp.SymbolKind.Class,
    SymbolKind.ENUM: lsp.SymbolKind.Enum,
    SymbolKind.INTERFACE: lsp.SymbolKind.Interface,
    SymbolKind.MODULE: lsp.SymbolKind.Module,
    SymbolKind.TYPEDEF: lsp.SymbolKind.TypeParameter,
    SymbolKind.SNMP_OBJ: lsp.SymbolKind.Object,
    SymbolKind.SNMP_TBL: lsp.SymbolKind.Object,
    SymbolKind.SNMP_IFACE: lsp.SymbolKind.Interface,
}


def _symbol_to_document_symbol(sym: Symbol) -> lsp.DocumentSymbol:
    """Convert an IOP Symbol to an LSP DocumentSymbol with children."""
    children: list[lsp.DocumentSymbol] = []

    for f in sym.fields:
        children.append(lsp.DocumentSymbol(
            name=f.name,
            kind=lsp.SymbolKind.Field,
            range=_range_to_lsp(f.full_range or f.range),
            selection_range=_range_to_lsp(f.range),
        ))

    for ev in sym.enum_values:
        children.append(lsp.DocumentSymbol(
            name=ev.name,
            kind=lsp.SymbolKind.EnumMember,
            range=_range_to_lsp(ev.full_range or ev.range),
            selection_range=_range_to_lsp(ev.range),
        ))

    for rpc in sym.rpcs:
        children.append(lsp.DocumentSymbol(
            name=rpc.name,
            kind=lsp.SymbolKind.Method,
            range=_range_to_lsp(rpc.full_range or rpc.range),
            selection_range=_range_to_lsp(rpc.range),
        ))

    lsp_kind = _IOP_TO_LSP_KIND.get(sym.kind, lsp.SymbolKind.Object)

    return lsp.DocumentSymbol(
        name=sym.name,
        kind=lsp_kind,
        range=_range_to_lsp(sym.full_range or sym.range),
        selection_range=_range_to_lsp(sym.range),
        children=children or None,
    )


@server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
def document_symbol(
    params: lsp.DocumentSymbolParams,
) -> Optional[list[lsp.DocumentSymbol]]:
    path = _uri_to_path(params.text_document.uri)
    symbols = indexer.index.by_file.get(path, [])
    if not symbols:
        return None
    return [_symbol_to_document_symbol(s) for s in symbols]


@server.feature(lsp.WORKSPACE_SYMBOL)
def workspace_symbol(
    params: lsp.WorkspaceSymbolParams,
) -> Optional[list[lsp.SymbolInformation]]:
    query = params.query.lower()
    results: list[lsp.SymbolInformation] = []
    prefix_matches: list[lsp.SymbolInformation] = []

    for sym in indexer.index.by_qualified_name.values():
        name_lower = sym.name.lower()
        if query and query not in name_lower:
            continue

        lsp_kind = _IOP_TO_LSP_KIND.get(sym.kind, lsp.SymbolKind.Object)
        info = lsp.SymbolInformation(
            name=sym.name,
            kind=lsp_kind,
            location=lsp.Location(
                uri=f'file://{sym.file}',
                range=_range_to_lsp(sym.range),
            ),
            container_name=sym.package,
        )

        if query and name_lower.startswith(query):
            prefix_matches.append(info)
        else:
            results.append(info)

    # Prefix matches first, then substring matches
    combined = prefix_matches + results
    return combined[:100] if combined else None


# Map IOP SymbolKind to LSP CompletionItemKind
_IOP_TO_COMPLETION_KIND: dict[SymbolKind, lsp.CompletionItemKind] = {
    SymbolKind.STRUCT: lsp.CompletionItemKind.Struct,
    SymbolKind.UNION: lsp.CompletionItemKind.Struct,
    SymbolKind.CLASS: lsp.CompletionItemKind.Class,
    SymbolKind.ENUM: lsp.CompletionItemKind.Enum,
    SymbolKind.INTERFACE: lsp.CompletionItemKind.Interface,
    SymbolKind.MODULE: lsp.CompletionItemKind.Module,
    SymbolKind.TYPEDEF: lsp.CompletionItemKind.TypeParameter,
    SymbolKind.SNMP_OBJ: lsp.CompletionItemKind.Struct,
    SymbolKind.SNMP_TBL: lsp.CompletionItemKind.Struct,
    SymbolKind.SNMP_IFACE: lsp.CompletionItemKind.Interface,
}


def _complete_field_type(
    partial: str, current_package: Optional[str],
) -> list[lsp.CompletionItem]:
    """Complete a type name in field position."""
    items: list[lsp.CompletionItem] = []
    partial_lower = partial.lower()

    # Builtins
    for bt in sorted(BUILTIN_TYPES):
        if bt.startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=bt,
                kind=lsp.CompletionItemKind.Keyword,
                sort_text=f'1:{bt}',
            ))

    seen = set()
    # Same-package types first
    if current_package:
        for sym in indexer.index.by_package.get(current_package, []):
            if sym.name.lower().startswith(partial_lower):
                items.append(lsp.CompletionItem(
                    label=sym.name,
                    kind=_IOP_TO_COMPLETION_KIND.get(
                        sym.kind, lsp.CompletionItemKind.Struct,
                    ),
                    detail=sym.package,
                    documentation=sym.doc,
                    sort_text=f'0:{sym.name}',
                ))
                seen.add(sym.qualified_name)

    # Cross-package types
    for qname, sym in indexer.index.by_qualified_name.items():
        if qname in seen:
            continue
        if sym.name.lower().startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=sym.qualified_name,
                filter_text=sym.name,
                kind=_IOP_TO_COMPLETION_KIND.get(
                    sym.kind, lsp.CompletionItemKind.Struct,
                ),
                detail=sym.package,
                documentation=sym.doc,
                sort_text=f'2:{sym.name}',
            ))

    return items


def _complete_qualified_type(
    package_prefix: str, partial: str,
) -> list[lsp.CompletionItem]:
    """Complete a type name after 'pkg.' prefix."""
    items: list[lsp.CompletionItem] = []
    partial_lower = partial.lower()

    for sym in indexer.index.by_package.get(package_prefix, []):
        if sym.name.lower().startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=sym.name,
                kind=_IOP_TO_COMPLETION_KIND.get(
                    sym.kind, lsp.CompletionItemKind.Struct,
                ),
                detail=sym.package,
                documentation=sym.doc,
                sort_text=f'0:{sym.name}',
            ))

    return items


def _enum_value_c_name(sym: Symbol, ev: EnumValueSymbol) -> str:
    """Build the C-style enum value name used in IOP default values.

    Convention: UPPER_SNAKE(enum_name)_VALUE_NAME
    With @prefix override: PREFIX_VALUE_NAME
    Examples:
        LogLevel + INFO -> LOG_LEVEL_INFO
        MyEnumA(@prefix(A)) + B -> A_B
    """
    if sym.enum_prefix:
        prefix = sym.enum_prefix
    else:
        prefix = camelcase_to_c(sym.name).upper()
    return f'{prefix}_{ev.name}'


def _complete_enum_value(
    partial: str, current_package: Optional[str],
) -> list[lsp.CompletionItem]:
    """Complete an enum value in default value position."""
    items: list[lsp.CompletionItem] = []
    partial_upper = partial.upper()

    # Collect all enum values, same-package first
    seen = set()
    packages_order: list[str] = []
    if current_package:
        packages_order.append(current_package)

    for pkg in packages_order:
        for sym in indexer.index.by_package.get(pkg, []):
            if sym.kind != SymbolKind.ENUM:
                continue
            for ev in sym.enum_values:
                c_name = _enum_value_c_name(sym, ev)
                if c_name.upper().startswith(partial_upper):
                    val_str = f' = {ev.value}' if ev.value else ''
                    items.append(lsp.CompletionItem(
                        label=c_name,
                        kind=lsp.CompletionItemKind.EnumMember,
                        detail=f'{sym.qualified_name}{val_str}',
                        documentation=ev.doc,
                        sort_text=f'0:{c_name}',
                    ))
                    seen.add((sym.qualified_name, ev.name))

    # Cross-package enum values
    for sym in indexer.index.by_qualified_name.values():
        if sym.kind != SymbolKind.ENUM:
            continue
        for ev in sym.enum_values:
            if (sym.qualified_name, ev.name) in seen:
                continue
            c_name = _enum_value_c_name(sym, ev)
            if c_name.upper().startswith(partial_upper):
                val_str = f' = {ev.value}' if ev.value else ''
                items.append(lsp.CompletionItem(
                    label=c_name,
                    kind=lsp.CompletionItemKind.EnumMember,
                    detail=f'{sym.qualified_name}{val_str}',
                    documentation=ev.doc,
                    sort_text=f'2:{c_name}',
                ))

    return items


def _complete_attribute(partial: str) -> list[lsp.CompletionItem]:
    """Complete an attribute name after @."""
    items: list[lsp.CompletionItem] = []
    partial_lower = partial.lower()

    for attr in _IOP_ATTRIBUTES:
        if attr.lower().startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=attr,
                kind=lsp.CompletionItemKind.Property,
                sort_text=f'0:{attr}',
            ))

    return items


def _complete_doc_tag(partial: str) -> list[lsp.CompletionItem]:
    """Complete a doc comment tag after backslash."""
    items: list[lsp.CompletionItem] = []
    partial_lower = partial.lower()

    for tag in _DOC_TAGS:
        if tag.lower().startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=tag,
                kind=lsp.CompletionItemKind.Keyword,
                sort_text=f'0:{tag}',
            ))

    return items


def _complete_doc_ref(
    partial: str, current_package: Optional[str],
) -> list[lsp.CompletionItem]:
    """Complete a type name in doc comment reference position."""
    items: list[lsp.CompletionItem] = []
    partial_lower = partial.lower()
    seen = set()

    # Same-package types first
    if current_package:
        for sym in indexer.index.by_package.get(current_package, []):
            if sym.name.lower().startswith(partial_lower):
                items.append(lsp.CompletionItem(
                    label=sym.name,
                    kind=_IOP_TO_COMPLETION_KIND.get(
                        sym.kind, lsp.CompletionItemKind.Struct,
                    ),
                    detail=sym.package,
                    sort_text=f'0:{sym.name}',
                ))
                seen.add(sym.qualified_name)

    # Cross-package types
    for qname, sym in indexer.index.by_qualified_name.items():
        if qname in seen:
            continue
        if sym.name.lower().startswith(partial_lower):
            items.append(lsp.CompletionItem(
                label=sym.qualified_name,
                kind=_IOP_TO_COMPLETION_KIND.get(
                    sym.kind, lsp.CompletionItemKind.Struct,
                ),
                detail=sym.package,
                sort_text=f'2:{sym.name}',
            ))

    return items


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=['.', '\\', '@']),
)
def completion(
    params: lsp.CompletionParams,
) -> Optional[list[lsp.CompletionItem]]:
    uri = params.text_document.uri
    line = params.position.line
    col = params.position.character

    doc = server.workspace.get_text_document(uri)
    doc_text = doc.source

    context_kind, partial, pkg_prefix = _get_completion_context(
        doc_text, line, col,
    )

    current_package = _get_package_for_uri(uri)

    if context_kind == 'attribute':
        items = _complete_attribute(partial)
    elif context_kind == 'doc_tag':
        items = _complete_doc_tag(partial)
    elif context_kind == 'doc_ref':
        items = _complete_doc_ref(partial, current_package)
    elif context_kind == 'qualified_type':
        items = _complete_qualified_type(pkg_prefix, partial)
    elif context_kind == 'enum_value':
        items = _complete_enum_value(partial, current_package)
    elif context_kind == 'field_type':
        items = _complete_field_type(partial, current_package)
    else:
        return None

    return items or None


def _format_symbol_hover(sym: Symbol) -> str:
    """Format a symbol for hover display."""
    kind_str = sym.kind.value
    parts = [f'**{kind_str} {sym.name}**']

    if sym.kind == SymbolKind.CLASS and sym.parent_class:
        parts[0] += f' : {sym.parent_class}'

    if sym.kind == SymbolKind.TYPEDEF and sym.typedef_source:
        parts[0] = f'**typedef** {sym.typedef_source} → **{sym.name}**'

    parts.append(f'*(package: {sym.package})*')

    if sym.doc:
        parts.append('')
        parts.append(sym.doc)

    # Show fields/values summary for small types
    if sym.enum_values:
        parts.append('')
        parts.append('```iop')
        for ev in sym.enum_values:
            val_str = f' = {ev.value}' if ev.value else ''
            parts.append(f'  {ev.name}{val_str},')
        parts.append('```')
    elif sym.fields and len(sym.fields) <= 10:
        parts.append('')
        parts.append('```iop')
        for f in sym.fields:
            spec = f.specifier or ''
            type_str = f.type_ref or 'builtin'
            parts.append(f'  {type_str}{spec} {f.name};')
        parts.append('```')

    return '\n'.join(parts)


def _format_enum_value_hover(
    enum_sym: Symbol, ev: EnumValueSymbol,
) -> str:
    val_str = f' = {ev.value}' if ev.value else ''
    parts = [f'**{ev.name}**{val_str}']
    parts.append(f'*(enum {enum_sym.qualified_name})*')
    if ev.doc:
        parts.append('')
        parts.append(ev.doc)
    return '\n'.join(parts)


def _format_field_hover_from_node(
    node: ts.Node,
    tree: ts.Tree,
    current_package: Optional[str],
) -> Optional[str]:
    """Format hover for a field name from its AST node."""
    if node.parent is None or node.parent.type != 'variable':
        return None

    var_node = node.parent
    type_node = _find_child(var_node, 'type')
    type_spec = _find_child(var_node, 'type_specifier')
    default = _find_child(var_node, 'default_value')

    field_name = node.text.decode('utf-8')
    type_text = type_node.text.decode('utf-8') if type_node else '?'
    spec_text = type_spec.text.decode('utf-8') if type_spec else ''

    parts = [f'**{field_name}** ({type_text}{spec_text})']
    if default:
        parts[0] += f' {default.text.decode("utf-8")}'

    # Try to find doc comment from the field node in the index
    # For now, just show the type info
    field_parent = var_node.parent  # the 'field' node
    if field_parent and field_parent.type == 'field':
        from .doc_comments import get_field_doc_comment
        doc = get_field_doc_comment(field_parent)
        if doc:
            parts.append('')
            parts.append(doc)

    return '\n'.join(parts)


def _format_enum_value_def_hover(
    node: ts.Node,
    current_package: Optional[str],
) -> Optional[str]:
    """Format hover for an enum value in its definition."""
    name = node.text.decode('utf-8') if node.text else None
    if name is None:
        return None

    result = indexer.index.resolve_enum_value(name, current_package)
    if result:
        enum_sym, ev = result
        return _format_enum_value_hover(enum_sym, ev)
    return None


def _symbol_to_location(sym: Symbol) -> lsp.Location:
    return lsp.Location(
        uri=f'file://{sym.file}',
        range=_range_to_lsp(sym.range),
    )


def _range_to_lsp(r) -> lsp.Range:
    from .symbols import Range as IopRange
    return lsp.Range(
        start=lsp.Position(line=r.start_line, character=r.start_col),
        end=lsp.Position(line=r.end_line, character=r.end_col),
    )


@server.feature(lsp.INITIALIZED)
def on_initialized(params: lsp.InitializedParams) -> None:
    """Index the workspace when the server is initialized."""
    for folder in server.workspace.folders.values():
        path = _uri_to_path(folder.uri)
        log.info('Indexing workspace folder: %s', path)
        indexer.index_workspace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description='IOP LSP Server')
    parser.add_argument(
        '--stdio', action='store_true', default=True,
        help='Use stdio transport (default)',
    )
    parser.add_argument(
        '--log-file', type=str, default=None,
        help='Log to file',
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='Enable verbose logging',
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    if args.log_file:
        logging.basicConfig(
            filename=args.log_file, level=level,
            format='%(asctime)s %(name)s %(levelname)s: %(message)s',
        )
    else:
        logging.basicConfig(
            level=level,
            format='%(asctime)s %(name)s %(levelname)s: %(message)s',
        )

    server.start_io()


if __name__ == '__main__':
    main()
