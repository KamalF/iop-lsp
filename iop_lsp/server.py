"""IOP Language Server Protocol implementation."""

from __future__ import annotations

import argparse
import logging
import os
from typing import Optional

import tree_sitter as ts
import tree_sitter_iop as tsiop
from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from .indexer import BUILTIN_TYPES, IOP_LANGUAGE, Indexer, _find_child
from .symbols import EnumValueSymbol, Symbol, SymbolKind

log = logging.getLogger(__name__)

server = LanguageServer('iop-lsp', 'v0.1.0')
indexer = Indexer()
_parser = ts.Parser(IOP_LANGUAGE)


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


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def goto_definition(
    params: lsp.DefinitionParams,
) -> Optional[lsp.Location]:
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
