"""Tests for the IOP symbol indexer."""

import os
import tempfile
import unittest

from iop_lsp.indexer import Indexer
from iop_lsp.symbols import SymbolKind


class TestIndexer(unittest.TestCase):
    def setUp(self):
        self.indexer = Indexer()

    def _index_source(self, source: str, filename: str = '/test.iop'):
        self.indexer.index_source(filename, source.encode('utf-8'))

    def test_package_extraction(self):
        self._index_source('package foo;\nstruct Bar {};')
        self.assertEqual(
            self.indexer.index.package_of_file['/test.iop'], 'foo'
        )

    def test_struct(self):
        self._index_source(
            'package foo;\n'
            'struct MyStruct {\n'
            '    int x;\n'
            '    string? name;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.MyStruct']
        self.assertEqual(sym.name, 'MyStruct')
        self.assertEqual(sym.kind, SymbolKind.STRUCT)
        self.assertEqual(sym.package, 'foo')
        self.assertEqual(len(sym.fields), 2)
        self.assertEqual(sym.fields[0].name, 'x')
        self.assertIsNone(sym.fields[0].type_ref)  # int is builtin
        self.assertEqual(sym.fields[1].name, 'name')
        self.assertEqual(sym.fields[1].specifier, '?')

    def test_union(self):
        self._index_source(
            'package foo;\n'
            'union MyUnion {\n'
            '    int a;\n'
            '    string b;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.MyUnion']
        self.assertEqual(sym.kind, SymbolKind.UNION)

    def test_enum(self):
        self._index_source(
            'package foo;\n'
            'enum Color {\n'
            '    RED = 0,\n'
            '    GREEN = 1,\n'
            '    BLUE = 2,\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Color']
        self.assertEqual(sym.kind, SymbolKind.ENUM)
        self.assertEqual(len(sym.enum_values), 3)
        self.assertEqual(sym.enum_values[0].name, 'RED')
        self.assertEqual(sym.enum_values[0].value, '0')
        self.assertEqual(sym.enum_values[2].name, 'BLUE')

    def test_class_with_inheritance(self):
        self._index_source(
            'package foo;\n'
            'class Base : 1 {\n'
            '    int x;\n'
            '};\n'
            'class Child : 2 : Base {\n'
            '    int y;\n'
            '};'
        )
        base = self.indexer.index.by_qualified_name['foo.Base']
        self.assertEqual(base.kind, SymbolKind.CLASS)
        self.assertIsNone(base.parent_class)

        child = self.indexer.index.by_qualified_name['foo.Child']
        self.assertEqual(child.parent_class, 'Base')

    def test_interface_with_rpcs(self):
        self._index_source(
            'package foo;\n'
            'interface MyIface {\n'
            '    doStuff\n'
            '        in (int x)\n'
            '        out void;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.MyIface']
        self.assertEqual(sym.kind, SymbolKind.INTERFACE)
        self.assertEqual(len(sym.rpcs), 1)
        self.assertEqual(sym.rpcs[0].name, 'doStuff')

    def test_typedef(self):
        self._index_source(
            'package foo;\n'
            'typedef int[] IntArray;'
        )
        sym = self.indexer.index.by_qualified_name['foo.IntArray']
        self.assertEqual(sym.kind, SymbolKind.TYPEDEF)

    def test_module(self):
        self._index_source(
            'package foo;\n'
            'interface Log {};\n'
            'module MyMod {\n'
            '    Log log;\n'
            '};'
        )
        mod = self.indexer.index.by_qualified_name['foo.MyMod']
        self.assertEqual(mod.kind, SymbolKind.MODULE)
        self.assertEqual(len(mod.fields), 1)
        self.assertEqual(mod.fields[0].type_ref, 'Log')

    def test_resolve_simple_name(self):
        self._index_source(
            'package foo;\n'
            'struct Bar {};',
            '/a.iop'
        )
        sym = self.indexer.index.resolve('Bar', 'foo')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.name, 'Bar')

    def test_resolve_qualified_name(self):
        self._index_source(
            'package foo;\nstruct Bar {};', '/a.iop'
        )
        self._index_source(
            'package baz;\nstruct Qux {};', '/b.iop'
        )
        sym = self.indexer.index.resolve('foo.Bar')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'foo.Bar')

    def test_resolve_prefers_same_package(self):
        self._index_source(
            'package foo;\nstruct Common {};', '/a.iop'
        )
        self._index_source(
            'package bar;\nstruct Common {};', '/b.iop'
        )
        sym = self.indexer.index.resolve('Common', 'bar')
        self.assertEqual(sym.package, 'bar')

    def test_resolve_enum_value(self):
        self._index_source(
            'package foo;\n'
            'enum Level {\n'
            '    LOW = 0,\n'
            '    HIGH = 1,\n'
            '};'
        )
        result = self.indexer.index.resolve_enum_value('HIGH', 'foo')
        self.assertIsNotNone(result)
        enum_sym, ev = result
        self.assertEqual(enum_sym.name, 'Level')
        self.assertEqual(ev.name, 'HIGH')
        self.assertEqual(ev.value, '1')

    def test_resolve_builtin_returns_none(self):
        sym = self.indexer.index.resolve('int')
        self.assertIsNone(sym)

    def test_reindex_file(self):
        self._index_source('package foo;\nstruct A {};', '/a.iop')
        self.assertIn('foo.A', self.indexer.index.by_qualified_name)

        # Re-index with different content
        self._index_source('package foo;\nstruct B {};', '/a.iop')
        self.assertNotIn('foo.A', self.indexer.index.by_qualified_name)
        self.assertIn('foo.B', self.indexer.index.by_qualified_name)

    def test_doc_comment(self):
        self._index_source(
            'package foo;\n'
            '/** A test struct. */\n'
            'struct Documented {};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Documented']
        self.assertEqual(sym.doc, 'A test struct.')

    def test_index_real_core_iop(self):
        """Integration test: index the real core.iop file."""
        core_iop = os.path.join(
            os.path.dirname(__file__), '..', '..', '..',
            'src', 'core', 'core.iop'
        )
        if not os.path.exists(core_iop):
            self.skipTest('core.iop not found')

        self.indexer.index_file(core_iop)

        # Verify LogLevel enum exists
        sym = self.indexer.index.by_qualified_name.get('core.LogLevel')
        self.assertIsNotNone(sym, 'core.LogLevel not found')
        self.assertEqual(sym.kind, SymbolKind.ENUM)
        self.assertTrue(len(sym.enum_values) > 0)

        # Verify EMERG enum value
        emerg = next(
            (v for v in sym.enum_values if v.name == 'EMERG'), None
        )
        self.assertIsNotNone(emerg)
        self.assertEqual(emerg.value, '0')

        # Verify LoggerConfiguration struct
        lc = self.indexer.index.by_qualified_name.get(
            'core.LoggerConfiguration'
        )
        self.assertIsNotNone(lc)
        self.assertEqual(lc.kind, SymbolKind.STRUCT)

        # Verify field type references
        level_field = next(
            (f for f in lc.fields if f.name == 'level'), None
        )
        self.assertIsNotNone(level_field)
        self.assertEqual(level_field.type_ref, 'LogLevel')

        # Verify Log interface
        log_iface = self.indexer.index.by_qualified_name.get('core.Log')
        self.assertIsNotNone(log_iface)
        self.assertEqual(log_iface.kind, SymbolKind.INTERFACE)
        self.assertTrue(len(log_iface.rpcs) > 0)

    def test_index_workspace(self):
        """Test workspace indexing with temp directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test .iop files
            with open(os.path.join(tmpdir, 'a.iop'), 'w') as f:
                f.write('package a;\nstruct Foo {};')
            sub = os.path.join(tmpdir, 'sub')
            os.makedirs(sub)
            with open(os.path.join(sub, 'b.iop'), 'w') as f:
                f.write('package sub.b;\nenum Bar { X, };')

            self.indexer.index_workspace(tmpdir)

            self.assertIn(
                'a.Foo', self.indexer.index.by_qualified_name
            )
            self.assertIn(
                'sub.b.Bar', self.indexer.index.by_qualified_name
            )

    def test_field_type_reference(self):
        """Test that fields referencing custom types have type_ref set."""
        self._index_source(
            'package foo;\n'
            'enum Color { RED, };\n'
            'struct Painted {\n'
            '    Color color;\n'
            '    int count;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Painted']
        color_field = sym.fields[0]
        self.assertEqual(color_field.type_ref, 'Color')
        count_field = sym.fields[1]
        self.assertIsNone(count_field.type_ref)  # int is builtin

    def test_rpc_type_references(self):
        """Test RPC with single type in/out/throw."""
        self._index_source(
            'package foo;\n'
            'struct Req {};\n'
            'struct Resp {};\n'
            'struct Err {};\n'
            'interface Svc {\n'
            '    call\n'
            '        in Req\n'
            '        out Resp\n'
            '        throw Err;\n'
            '};'
        )
        svc = self.indexer.index.by_qualified_name['foo.Svc']
        rpc = svc.rpcs[0]
        self.assertEqual(rpc.name, 'call')
        self.assertEqual(rpc.in_type, 'Req')
        self.assertEqual(rpc.out_type, 'Resp')
        self.assertEqual(rpc.throw_type, 'Err')


class TestCIdentifierResolution(unittest.TestCase):
    def setUp(self):
        self.indexer = Indexer()

    def _index_source(self, source: str, filename: str = '/test.iop'):
        self.indexer.index_source(filename, source.encode('utf-8'))

    def test_resolve_with_t_suffix(self):
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_struct_a__t'
        )
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'tstiop.MyStructA')

    def test_resolve_with_s_suffix(self):
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_struct_a__s'
        )
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'tstiop.MyStructA')

    def test_resolve_enum_with_e_suffix(self):
        self._index_source(
            'package tstiop;\n'
            'enum MyEnum { A, };'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_enum__e'
        )
        self.assertIsNotNone(sym)
        self.assertEqual(sym.kind, SymbolKind.ENUM)

    def test_resolve_array_suffix(self):
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_struct_a__array_t'
        )
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'tstiop.MyStructA')

    def test_resolve_opt_suffix(self):
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_struct_a__opt_t'
        )
        self.assertIsNotNone(sym)

    def test_resolve_no_suffix(self):
        """Bare C name without suffix should also resolve."""
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'tstiop__my_struct_a'
        )
        self.assertIsNotNone(sym)

    def test_resolve_unknown_returns_none(self):
        self._index_source(
            'package tstiop;\n'
            'struct MyStructA {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'unknown__type__t'
        )
        self.assertIsNone(sym)

    def test_resolve_nested_package(self):
        self._index_source(
            'package test.dso;\n'
            'struct ClassDso {};'
        )
        sym = self.indexer.index.resolve_c_identifier(
            'test__dso__class_dso__t'
        )
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'test.dso.ClassDso')

    def test_ctype_override(self):
        self._index_source(
            'package core;\n'
            '@ctype(http_code__t)\n'
            'enum HttpCode {\n'
            '    OK = 200,\n'
            '};'
        )
        # Should be resolvable by the @ctype name
        sym = self.indexer.index.resolve_c_identifier('http_code__t')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'core.HttpCode')
        self.assertEqual(sym.ctype, 'http_code__t')

    def test_ctype_override_also_resolves_normal(self):
        """Normal C name should still work alongside @ctype."""
        self._index_source(
            'package core;\n'
            '@ctype(http_code__t)\n'
            'enum HttpCode {\n'
            '    OK = 200,\n'
            '};'
        )
        sym = self.indexer.index.resolve_c_identifier('core__http_code__e')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'core.HttpCode')

    def test_remove_file_cleans_c_name(self):
        self._index_source(
            'package foo;\n'
            'struct Bar {};',
            '/a.iop',
        )
        self.assertIsNotNone(
            self.indexer.index.resolve_c_identifier('foo__bar__t')
        )
        self.indexer.index.remove_file('/a.iop')
        self.assertIsNone(
            self.indexer.index.resolve_c_identifier('foo__bar__t')
        )


class TestDocRef(unittest.TestCase):
    """Tests for doc_ref parsing and resolution."""

    def test_parse_doc_ref_p_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\p GridSquare')
        self.assertEqual(tag, 'p')
        self.assertEqual(ident, 'GridSquare')

    def test_parse_doc_ref_ref_tag_qualified(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\ref geoutils.GridSquare')
        self.assertEqual(tag, 'ref')
        self.assertEqual(ident, 'geoutils.GridSquare')

    def test_parse_doc_ref_see_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\see Foo')
        self.assertEqual(tag, 'see')
        self.assertEqual(ident, 'Foo')

    def test_parse_doc_ref_struct_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\struct Foo')
        self.assertEqual(tag, 'struct')
        self.assertEqual(ident, 'Foo')

    def test_parse_doc_ref_enum_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\enum Color')
        self.assertEqual(tag, 'enum')
        self.assertEqual(ident, 'Color')

    def test_parse_doc_ref_class_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\class Base')
        self.assertEqual(tag, 'class')
        self.assertEqual(ident, 'Base')

    def test_parse_doc_ref_c_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\c MyType')
        self.assertEqual(tag, 'c')
        self.assertEqual(ident, 'MyType')

    def test_parse_doc_ref_a_tag(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref(r'\a param')
        self.assertEqual(tag, 'a')
        self.assertEqual(ident, 'param')

    def test_parse_doc_ref_invalid(self):
        from iop_lsp.server import _parse_doc_ref
        tag, ident = _parse_doc_ref('not a doc ref')
        self.assertIsNone(tag)
        self.assertIsNone(ident)

    def test_resolve_simple_name_via_doc_ref(self):
        """Test that a simple name from doc_ref resolves in the index."""
        indexer = Indexer()
        indexer.index_source(
            '/test.iop',
            b'package geoutils;\nstruct GridSquare {};',
        )
        sym = indexer.index.resolve('GridSquare', 'geoutils')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.name, 'GridSquare')

    def test_resolve_qualified_name_via_doc_ref(self):
        """Test that a qualified name from doc_ref resolves."""
        indexer = Indexer()
        indexer.index_source(
            '/test.iop',
            b'package geoutils;\nstruct GridSquare {};',
        )
        sym = indexer.index.resolve('geoutils.GridSquare')
        self.assertIsNotNone(sym)
        self.assertEqual(sym.qualified_name, 'geoutils.GridSquare')

    def test_tag_kind_filtering_struct_matches(self):
        """\\struct Foo should match when Foo is a struct."""
        from iop_lsp.server import _TAG_KIND_MAP
        indexer = Indexer()
        indexer.index_source(
            '/test.iop', b'package foo;\nstruct Bar {};',
        )
        sym = indexer.index.resolve('Bar', 'foo')
        self.assertIsNotNone(sym)
        required_kind = _TAG_KIND_MAP.get('struct')
        self.assertEqual(sym.kind, required_kind)

    def test_tag_kind_filtering_struct_rejects_enum(self):
        """\\struct Foo should NOT match when Foo is an enum."""
        from iop_lsp.server import _TAG_KIND_MAP
        indexer = Indexer()
        indexer.index_source(
            '/test.iop', b'package foo;\nenum Bar { X, };',
        )
        sym = indexer.index.resolve('Bar', 'foo')
        self.assertIsNotNone(sym)
        required_kind = _TAG_KIND_MAP.get('struct')
        self.assertNotEqual(sym.kind, required_kind)

    def test_unknown_identifier_returns_none(self):
        """Unknown identifier should return None from resolve."""
        indexer = Indexer()
        indexer.index_source(
            '/test.iop', b'package foo;\nstruct Bar {};',
        )
        sym = indexer.index.resolve('NonExistent', 'foo')
        self.assertIsNone(sym)

    def test_node_context_doc_ref(self):
        """Test that _get_node_context_at_position detects doc_ref."""
        import tree_sitter as ts
        import tree_sitter_iop as tsiop
        from iop_lsp.indexer import IOP_LANGUAGE
        from iop_lsp.server import _get_node_context_at_position

        parser = ts.Parser(IOP_LANGUAGE)
        source = (
            b'package foo;\n'
            b'/** See \\p GridSquare for details. */\n'
            b'struct Bar {};'
        )
        tree = parser.parse(source)

        # Find the doc_ref node in the tree
        root = tree.root_node
        doc_ref_node = None
        queue = [root]
        while queue:
            n = queue.pop(0)
            if n.type == 'doc_ref':
                doc_ref_node = n
                break
            queue.extend(n.children)

        if doc_ref_node is None:
            self.skipTest('tree-sitter-iop does not produce doc_ref nodes')

        line = doc_ref_node.start_point[0]
        col = doc_ref_node.start_point[1]
        node, context = _get_node_context_at_position(tree, line, col)
        self.assertEqual(context, 'doc_ref')
        self.assertIsNotNone(node)


class TestDocComments(unittest.TestCase):
    def setUp(self):
        self.indexer = Indexer()

    def _index_source(self, source: str, filename: str = '/test.iop'):
        self.indexer.index_source(filename, source.encode('utf-8'))

    def test_multiline_doc(self):
        self._index_source(
            'package foo;\n'
            '/** Configuration of a specific logger.\n'
            ' */\n'
            'struct LoggerConfig {};'
        )
        sym = self.indexer.index.by_qualified_name['foo.LoggerConfig']
        self.assertEqual(sym.doc, 'Configuration of a specific logger.')

    def test_trailing_doc_on_enum(self):
        self._index_source(
            'package foo;\n'
            'enum Level {\n'
            '    LOW = 0, /**< low level */\n'
            '    HIGH = 1, /**< high level */\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Level']
        self.assertEqual(sym.enum_values[0].doc, 'low level')
        self.assertEqual(sym.enum_values[1].doc, 'high level')

    def test_preceding_doc_on_field(self):
        self._index_source(
            'package foo;\n'
            'struct S {\n'
            '    /** The name. */\n'
            '    string name;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.S']
        self.assertEqual(sym.fields[0].doc, 'The name.')

    def test_no_doc(self):
        self._index_source(
            'package foo;\n'
            'struct S {};'
        )
        sym = self.indexer.index.by_qualified_name['foo.S']
        self.assertIsNone(sym.doc)


class TestDocumentSymbols(unittest.TestCase):
    """Tests for document symbol generation."""

    def setUp(self):
        self.indexer = Indexer()

    def _index_source(self, source: str, filename: str = '/test.iop'):
        self.indexer.index_source(filename, source.encode('utf-8'))

    def test_full_range_populated_on_symbol(self):
        self._index_source(
            'package foo;\n'
            'struct Bar {\n'
            '    int x;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Bar']
        self.assertIsNotNone(sym.full_range)
        # full_range should span the entire struct definition
        self.assertGreater(
            sym.full_range.end_line - sym.full_range.start_line,
            0,
        )
        # range (identifier) should be within full_range
        self.assertGreaterEqual(
            sym.range.start_line, sym.full_range.start_line,
        )

    def test_full_range_populated_on_field(self):
        self._index_source(
            'package foo;\n'
            'struct Bar {\n'
            '    int x;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Bar']
        field = sym.fields[0]
        self.assertIsNotNone(field.full_range)

    def test_full_range_populated_on_enum_value(self):
        self._index_source(
            'package foo;\n'
            'enum Color {\n'
            '    RED = 0,\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Color']
        ev = sym.enum_values[0]
        self.assertIsNotNone(ev.full_range)

    def test_full_range_populated_on_rpc(self):
        self._index_source(
            'package foo;\n'
            'interface Svc {\n'
            '    doStuff\n'
            '        in void\n'
            '        out void;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Svc']
        rpc = sym.rpcs[0]
        self.assertIsNotNone(rpc.full_range)

    def test_document_symbol_conversion(self):
        """Test _symbol_to_document_symbol produces correct hierarchy."""
        from iop_lsp.server import _symbol_to_document_symbol
        self._index_source(
            'package foo;\n'
            'struct MyStruct {\n'
            '    int x;\n'
            '    string name;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.MyStruct']
        doc_sym = _symbol_to_document_symbol(sym)
        self.assertEqual(doc_sym.name, 'MyStruct')
        from lsprotocol import types as lsp
        self.assertEqual(doc_sym.kind, lsp.SymbolKind.Struct)
        self.assertIsNotNone(doc_sym.children)
        self.assertEqual(len(doc_sym.children), 2)
        self.assertEqual(doc_sym.children[0].name, 'x')
        self.assertEqual(doc_sym.children[0].kind, lsp.SymbolKind.Field)
        self.assertEqual(doc_sym.children[1].name, 'name')

    def test_document_symbol_enum_children(self):
        from iop_lsp.server import _symbol_to_document_symbol
        from lsprotocol import types as lsp
        self._index_source(
            'package foo;\n'
            'enum Color {\n'
            '    RED = 0,\n'
            '    GREEN = 1,\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Color']
        doc_sym = _symbol_to_document_symbol(sym)
        self.assertEqual(doc_sym.kind, lsp.SymbolKind.Enum)
        self.assertEqual(len(doc_sym.children), 2)
        self.assertEqual(
            doc_sym.children[0].kind, lsp.SymbolKind.EnumMember,
        )

    def test_document_symbol_interface_children(self):
        from iop_lsp.server import _symbol_to_document_symbol
        from lsprotocol import types as lsp
        self._index_source(
            'package foo;\n'
            'interface Svc {\n'
            '    doStuff\n'
            '        in void\n'
            '        out void;\n'
            '};'
        )
        sym = self.indexer.index.by_qualified_name['foo.Svc']
        doc_sym = _symbol_to_document_symbol(sym)
        self.assertEqual(doc_sym.kind, lsp.SymbolKind.Interface)
        self.assertEqual(len(doc_sym.children), 1)
        self.assertEqual(
            doc_sym.children[0].kind, lsp.SymbolKind.Method,
        )

    def test_document_symbol_no_children(self):
        from iop_lsp.server import _symbol_to_document_symbol
        self._index_source(
            'package foo;\n'
            'typedef int[] IntArray;'
        )
        sym = self.indexer.index.by_qualified_name['foo.IntArray']
        doc_sym = _symbol_to_document_symbol(sym)
        self.assertIsNone(doc_sym.children)


class TestWorkspaceSymbols(unittest.TestCase):
    """Tests for workspace symbol search."""

    def setUp(self):
        self.indexer = Indexer()

    def _index_source(self, source: str, filename: str = '/test.iop'):
        self.indexer.index_source(filename, source.encode('utf-8'))

    def test_empty_query_returns_all(self):
        from iop_lsp.server import _IOP_TO_LSP_KIND
        self._index_source(
            'package foo;\n'
            'struct Bar {};\n'
            'enum Baz { X, };',
        )
        # Simulate workspace symbol search
        results = self._search('')
        self.assertEqual(len(results), 2)

    def test_substring_match(self):
        self._index_source(
            'package foo;\n'
            'struct MyStruct {};\n'
            'struct OtherThing {};',
        )
        results = self._search('struct')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, 'MyStruct')

    def test_case_insensitive(self):
        self._index_source(
            'package foo;\n'
            'struct MyStruct {};',
        )
        results = self._search('MYSTRUCT')
        self.assertEqual(len(results), 1)

    def test_prefix_matches_first(self):
        self._index_source(
            'package foo;\n'
            'struct ABar {};\n'
            'struct BarA {};',
        )
        results = self._search('bar')
        self.assertEqual(len(results), 2)
        # BarA starts with 'bar', so it should come first
        self.assertEqual(results[0].name, 'BarA')

    def test_limit_100(self):
        # Create 150 symbols
        lines = ['package foo;']
        for i in range(150):
            lines.append(f'struct S{i} {{}};')
        self._index_source('\n'.join(lines))
        results = self._search('')
        self.assertLessEqual(len(results), 100)

    def test_no_match_returns_none(self):
        self._index_source('package foo;\nstruct Bar {};')
        results = self._search('zzzzz')
        self.assertIsNone(results)

    def test_container_name_is_package(self):
        self._index_source('package mypackage;\nstruct Foo {};')
        results = self._search('foo')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].container_name, 'mypackage')

    def _search(self, query: str):
        """Simulate workspace symbol search using the indexer."""
        from lsprotocol import types as lsp
        from iop_lsp.server import _IOP_TO_LSP_KIND

        query_lower = query.lower()
        results = []
        prefix_matches = []

        for sym in self.indexer.index.by_qualified_name.values():
            name_lower = sym.name.lower()
            if query_lower and query_lower not in name_lower:
                continue

            lsp_kind = _IOP_TO_LSP_KIND.get(
                sym.kind, lsp.SymbolKind.Object,
            )
            info = lsp.SymbolInformation(
                name=sym.name,
                kind=lsp_kind,
                location=lsp.Location(
                    uri=f'file://{sym.file}',
                    range=lsp.Range(
                        start=lsp.Position(
                            line=sym.range.start_line,
                            character=sym.range.start_col,
                        ),
                        end=lsp.Position(
                            line=sym.range.end_line,
                            character=sym.range.end_col,
                        ),
                    ),
                ),
                container_name=sym.package,
            )

            if query_lower and name_lower.startswith(query_lower):
                prefix_matches.append(info)
            else:
                results.append(info)

        combined = prefix_matches + results
        return combined[:100] if combined else None


if __name__ == '__main__':
    unittest.main()
