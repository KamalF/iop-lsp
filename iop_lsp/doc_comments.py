"""Extract and format doc comments from tree-sitter AST nodes."""

from __future__ import annotations

import re
from typing import Optional

from tree_sitter import Node


def get_doc_comment(node: Node) -> Optional[str]:
    """Get the doc comment preceding a definition node.

    Looks for a /** ... */ comment immediately before the node
    (possibly with attributes in between). Skips trailing doc
    comments (/**< ... */) which belong to the previous node.
    """
    candidate = node.prev_named_sibling
    # Skip past attributes to find the comment
    while candidate is not None and candidate.type == 'attribute':
        candidate = candidate.prev_named_sibling
    if candidate is not None and candidate.type == 'comment':
        text = candidate.text.decode('utf-8')
        if (text.startswith('/**')
                and not text.startswith('/***')
                and not text.startswith('/**<')):
            return _clean_doc_comment(text)
    return None


def get_trailing_doc_comment(node: Node) -> Optional[str]:
    """Get a trailing doc comment (/**< ... */) after a node.

    Used for enum values and fields. The trailing comment is on
    the same line as the node.
    """
    # Look for comment among siblings after this node on same line
    sibling = node.next_named_sibling
    if sibling is not None and sibling.type == 'comment':
        text = sibling.text.decode('utf-8')
        if text.startswith('/**<'):
            if sibling.start_point.row == node.end_point.row:
                return _clean_trailing_doc_comment(text)
    # Also check non-named siblings (comments are extras)
    # Walk through all children of parent to find trailing comment
    if node.parent is not None:
        found_node = False
        for child in node.parent.children:
            if child.id == node.id:
                found_node = True
                continue
            if found_node and child.type == 'comment':
                text = child.text.decode('utf-8')
                if (text.startswith('/**<')
                        and child.start_point.row == node.end_point.row):
                    return _clean_trailing_doc_comment(text)
            if found_node and child.start_point.row > node.end_point.row:
                break
    return None


def get_field_doc_comment(field_node: Node) -> Optional[str]:
    """Get doc comment for a field/enum value.

    Checks for preceding /** ... */ first, then trailing /**< ... */.
    """
    doc = get_doc_comment(field_node)
    if doc is not None:
        return doc
    return get_trailing_doc_comment(field_node)


def _clean_doc_comment(text: str) -> str:
    """Clean a /** ... */ doc comment, stripping delimiters."""
    # Remove /** and */
    text = text[3:]
    if text.endswith('*/'):
        text = text[:-2]
    # Split into lines and clean each
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        # Remove leading ' * ' or ' *'
        if line.startswith('* '):
            line = line[2:]
        elif line.startswith('*'):
            line = line[1:]
        cleaned.append(line)
    # Join and strip
    result = '\n'.join(cleaned).strip()
    return result


def _clean_trailing_doc_comment(text: str) -> str:
    """Clean a /**< ... */ trailing doc comment."""
    # Remove /**< and */
    text = text[4:]
    if text.endswith('*/'):
        text = text[:-2]
    return text.strip()
