"""Mapping between IOP CamelCase names and C snake_case identifiers.

Mirrors the logic from lib-common's src/iop/iop.blk:
- t_camelcase_to_c (line ~686)
- t_iop_type_to_c (line ~707)
"""

from __future__ import annotations

import re


def camelcase_to_c(name: str) -> str:
    """Convert a CamelCase name to snake_case C identifier.

    Mirrors t_camelcase_to_c: MyStructA -> my_struct_a
    Rules:
    - Insert '_' before each uppercase letter that is preceded by a
      lowercase letter or digit.
    - Insert '_' before an uppercase letter followed by a lowercase letter
      when preceded by another uppercase letter (e.g., HTTPCode -> http_code).
    - Lowercase everything.
    """
    # Insert underscore between lower/digit and upper
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
    # Insert underscore between consecutive uppers followed by lower
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', s)
    return s.lower()


def c_to_camelcase(name: str) -> str:
    """Convert a snake_case C name to CamelCase.

    Reverse of camelcase_to_c: my_struct_a -> MyStructA
    """
    return ''.join(part.capitalize() for part in name.split('_'))


def iop_type_to_c(qualified_name: str) -> str:
    """Convert an IOP qualified name to its C type base name (without suffix).

    Mirrors t_iop_type_to_c: tstiop.MyStructA -> tstiop__my_struct_a

    Package dots become '__', then the CamelCase type name is converted
    to snake_case and appended with '__'.
    """
    parts = qualified_name.rsplit('.', 1)
    if len(parts) == 2:
        pkg, type_name = parts
        pkg_c = pkg.replace('.', '__')
        return f'{pkg_c}__{camelcase_to_c(type_name)}'
    return camelcase_to_c(parts[0])


# Suffixes used in C for IOP types, ordered longest first for matching
C_TYPE_SUFFIXES = (
    '__array_t',
    '__opt_t',
    '__sp',
    '__ep',
    '__t',
    '__s',
    '__e',
)
