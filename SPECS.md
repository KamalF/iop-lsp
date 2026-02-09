# IOP LSP Server — Development Plan

## Goal

An LSP server for `.iop` files providing:
1. **Go-to-definition** — jump from a type reference to its definition
2. **Hover documentation** — show doc comments and type info on hover

Integrated with **Helix** editor. Tree-sitter-iop handles syntax highlighting
separately (already configured).

## Approach

- **Language:** Python with `pygls` (LSP framework)
- **Parsing:** `tree-sitter` + `tree-sitter-iop` Python bindings for
  fault-tolerant, incremental parsing
- **Indexing:** Recursively scan all `.iop` files in the workspace, build a
  symbol table mapping type names to locations and doc comments
- **Location:** `tools/iop-lsp/` within this repo

## IOP Language Model

### File structure

Each `.iop` file has:
- A `package` declaration (namespace, e.g., `package core;`)
- Type definitions: `struct`, `union`, `class`, `enum`, `interface`,
  `module`, `typedef`, `snmpObj`, `snmpTbl`, `snmpIface`

### Cross-package type references

Types within the same package are referenced by simple name (`LogLevel`).
Types from other packages use qualified names where the prefix is the
**package name** (not file path): `tstiop.MyStructA`, `core.LogLevel`.

The package name maps to a filesystem path: dots become directory separators,
`.iop` extension is appended. Examples:
- `tstiop` → `tstiop.iop`
- `tstiop_backward_compat` → `tstiop_backward_compat.iop`
- `foo.bar` → `foo/bar.iop`

The iopc compiler resolves these using include paths. For the LSP, we
build a `package name → file path` mapping from the `package` declarations
found during recursive indexing — no include path configuration needed.

Real examples from this repo:
```
tstiop.MyStructA                    # in tstiop2.iop, referencing tstiop.iop
tstiop_backward_compat.BasicUnion   # in tstiop_typedef.iop
tstiop_void_type.VoidRequired       # in tstiop.iop
```

### Type references

Types are referenced in:
- **Field types:** `LogLevel level;` or `MyClass1? nextClass;`
- **Class inheritance:** `class MyClass2 : 2 : MyClass1 { ... }`
- **RPC parameters:** `in (LogLevel level, string name)`
- **RPC in/out/throw with single type:** `in MyStruct`, `throw MyError`
- **Module fields:** `Log log;` (interface type + instance name)
- **Typedef source type:** `typedef tstiop.BasicStruct RemoteStruct;`
- **Default values:** `LogLevel rootLevel = LOG_LEVEL_DEFAULT;` (enum values)

References are either:
- **Simple name:** `LogLevel` — resolved within the same package first,
  then across all packages (ambiguity resolved by same-package preference)
- **Qualified name:** `pkg.TypeName` — resolved by looking up the package
  in the `package → file` mapping, then the type within that package

### Doc comments

Two styles (Doxygen-like):
- **Before:** `/** Multi-line doc comment */` preceding a type/field
- **Trailing:** `/**< inline doc */` after an enum value or field

### Scale

~230 `.iop` files, ~7700 lines total in lib-common. Small enough to index
everything at startup with no performance concerns.

## Directory Structure

```
tools/iop-lsp/
├── pyproject.toml
├── README.md
├── iop_lsp/
│   ├── __init__.py
│   ├── server.py          # LSP server, capabilities, request handlers
│   ├── indexer.py          # Parse .iop files, build symbol table
│   ├── symbols.py          # Symbol data structures (types, fields, etc.)
│   └── doc_comments.py     # Extract and format doc comments from AST
└── tests/
    ├── __init__.py
    ├── test_indexer.py
    └── test_doc_comments.py
```

## Implementation Phases

### Phase 1: Project Setup + Symbol Indexer

#### 1.1 Project skeleton
- `pyproject.toml` with dependencies: `pygls`, `tree-sitter`, `tree-sitter-iop`
- Minimal `server.py` that starts, connects to editor, logs

#### 1.2 `symbols.py` — Data structures
```python
@dataclass
class Symbol:
    name: str               # Simple name (e.g., "LogLevel")
    qualified_name: str     # Package-qualified (e.g., "core.LogLevel")
    kind: SymbolKind        # struct, union, class, enum, interface, etc.
    file: str               # Absolute file path
    range: Range            # Location in file (line, col)
    doc: str | None         # Extracted doc comment
    package: str            # Package name from the file
    children: list[FieldSymbol]  # Fields, enum values, RPCs

@dataclass
class FieldSymbol:
    name: str
    type_ref: str | None    # Referenced type name (None for built-ins)
    specifier: str | None   # "?", "&", "[]", or None
    default_value: str | None
    range: Range
    doc: str | None
```

#### 1.3 `doc_comments.py` — Doc comment extraction
- Given a tree-sitter node, find the preceding `/** ... */` comment
- For enum values / fields, also check for trailing `/**< ... */`
- Strip comment delimiters (`/**`, `*/`, leading ` * `)
- Return clean text

#### 1.4 `indexer.py` — Symbol table builder
- Find all `.iop` files recursively from workspace root
- Parse each file with tree-sitter-iop
- Walk the AST, extract:
  - Package name from `package_definition`
  - All type definitions → `Symbol` entries
  - Fields / enum values / RPCs → `FieldSymbol` children
- Build lookup tables:
  - `by_name: dict[str, list[Symbol]]` — simple name → symbols
    (may have duplicates across packages)
  - `by_qualified_name: dict[str, Symbol]` — `pkg.Name` → symbol
  - `by_package: dict[str, list[Symbol]]` — package name → symbols
  - `by_file: dict[str, list[Symbol]]` — file → symbols defined there
  - `package_of_file: dict[str, str]` — file → package name
- Re-index a single file on `textDocument/didSave` or `textDocument/didChange`

### Phase 2: Go-to-Definition

#### 2.1 Cursor context resolution
- On `textDocument/definition` request, parse the current file, find the
  tree-sitter node at the cursor position
- Determine if it's a type reference by checking the node's parent context:
  - `type` node inside a `variable` → field type reference
  - `identifier` in `class_inheritance` → parent class reference
  - `identifier` in `rpc_in`, `rpc_out`, `rpc_throw` → RPC type reference
  - `identifier` in `module_field` (first identifier) → interface reference

#### 2.2 Symbol resolution
- Extract the identifier text at cursor
- If qualified (`pkg.TypeName`):
  1. Split into package prefix and type name
  2. Look up package in `by_package: dict[str, list[Symbol]]`
  3. Find the type within that package's symbols
- If simple (`TypeName`):
  1. Look in same-package symbols first
  2. Then look in `by_name` across all packages
  3. If ambiguous, prefer same-package matches
- For enum value references (e.g., `LOG_LEVEL_DEFAULT` in default values):
  1. Search enum children across the package
- Return the symbol's file + range as the definition location

#### 2.3 `server.py` — Register `textDocument/definition`

### Phase 3: Hover Documentation

#### 3.1 Hover on type references
- Same cursor context resolution as go-to-definition
- Resolve the symbol, format hover content:
  ```
  **struct LoggerConfiguration**
  (package: core)

  Configuration of a specific logger.
  ```

#### 3.2 Hover on field names
- If cursor is on a field identifier (inside a `field` or `variable` node),
  show field info:
  ```
  **fullName** (string)

  Name of the logger to configure.
  ```

#### 3.3 Hover on enum values
- Show enum name, numeric value, and doc:
  ```
  **EMERG** = 0

  system is unusable
  ```

#### 3.4 `server.py` — Register `textDocument/hover`

### Phase 4: Future Enhancements

- **Diagnostics:** Report unknown type references, duplicate definitions
- **Completion:** Suggest type names, field types, enum values
- **Workspace symbols:** List all types for quick navigation (`<Space>S` in Helix)
- **Find references:** Find all usages of a type
- **Rename:** Rename a type across all files
- **JSON/YAML support:** Original plan — validate/complete IOP JSON/YAML
  files using iopy introspection

## Helix Integration

In `~/.config/helix/languages.toml`:

```toml
[language-server.iop-lsp]
command = "python"
args = ["-m", "iop_lsp.server", "--stdio"]

[[language]]
name = "iop"
scope = "source.iop"
file-types = ["iop"]
comment-token = "//"
block-comment-tokens = { start = "/*", end = "*/" }
indent = { tab-width = 4, unit = "    " }
language-servers = ["iop-lsp"]
```

## Key tree-sitter-iop Node Types

| IOP construct | Tree-sitter node type | Children of interest |
|---------------|----------------------|---------------------|
| `package foo;` | `package_definition` | `identifier` |
| `struct Foo { ... }` | `data_structure_definition` | `data_structure_type`, `identifier`, `data_structure_block` |
| `union Foo { ... }` | `data_structure_definition` | (same, type is "union") |
| `class Foo : 1 : Bar { ... }` | `class_definition` | `identifier`, `class_inheritance`, `data_structure_block` |
| `enum Foo { A, B }` | `enum_definition` | `identifier`, `enum_block` → `enum_field` |
| `interface Foo { ... }` | `interface_definition` | `identifier`, `rpc_block` → `rpc` |
| `module Foo { ... }` | `module_definition` | `identifier`, `module_block` → `module_field` |
| `typedef int[] Arr;` | `typedef_definition` | `variable` → `type`, `identifier` |
| `snmpObj Foo : Bar { }` | `snmp_object_definition` | `identifier`, `class_inheritance`, `data_structure_block` |
| `snmpTbl Foo : Bar { }` | `snmp_table_definition` | `identifier`, `class_inheritance`, `data_structure_block` |
| `snmpIface Foo { }` | `snmp_interface_definition` | `identifier`, `snmp_rpc_block` |
| `int? fieldName;` | `field` → `variable` | `type`, `type_specifier`, `identifier` |
| `/** doc */` | `comment` | (text content) |

## Verification

1. **Unit tests:** Test indexer with small `.iop` snippets parsed by
   tree-sitter
2. **Integration test:** Index `src/core/core.iop`, verify symbols are
   found, go-to-def resolves `LogLevel` from a field type
3. **Manual test:** Open `.iop` files in Helix, verify `gd` (go-to-def)
   and hover work
