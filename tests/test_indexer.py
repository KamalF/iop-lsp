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


if __name__ == '__main__':
    unittest.main()
