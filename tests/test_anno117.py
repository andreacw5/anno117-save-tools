"""Tests for anno117.py. Standard library only, no game files needed.

Run from the repository root:  python -m unittest discover -s tests -v
"""
import contextlib
import gc
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import warnings
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import anno117 as A  # noqa: E402


def quiet():
    """Silence the progress lines some functions print."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class TempDirMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='anno117_test_')
        # anno117.py leaves files open (see test_archive_file_is_closed): keep the output clean
        w = warnings.catch_warnings()
        w.__enter__()
        self.addCleanup(w.__exit__, None, None, None)
        warnings.simplefilter('ignore', ResourceWarning)

    def tearDown(self):
        # rda_entries keeps the archive mmapped, so Windows may refuse to delete it right away
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        mode = 'wb' if isinstance(data, bytes) else 'w'
        with open(path, mode, **({} if isinstance(data, bytes) else {'encoding': 'utf-8'})) as f:
            f.write(data)
        return path


# ---------------- FileDB v2 builder ----------------
def build_filedb(tree, tag_ids=None, attr_ids=None, extra_tags=None, extra_attrs=None):
    """Build a FileDB v2 blob from a nested structure.

    tree: list of (name, value) where value is bytes (attribute) or a list (tag with children).
    Names are given ids automatically (tags from 1, attributes from 0x8000) unless passed in
    tag_ids / attr_ids. Ids passed as ints in the tree are written as-is (to test unknown ids).
    extra_tags / extra_attrs: {id: name} entries added to the dictionaries.
    """
    tags = dict(tag_ids or {})
    attrs = dict(attr_ids or {})
    body = bytearray()

    def node(name, value):
        if isinstance(value, (bytes, bytearray)):
            i = name if isinstance(name, int) else attrs.setdefault(name, 0x8000 + len(attrs))
            body.extend(struct.pack('<iI', len(value), i))
            body.extend(value)
            body.extend(b'\0' * (-len(value) % 8))
        else:
            i = name if isinstance(name, int) else tags.setdefault(name, 1 + len(tags))
            body.extend(struct.pack('<iI', 0, i))
            for n, v in value:
                node(n, v)
            body.extend(struct.pack('<iI', 0, 0))

    for n, v in tree:
        node(n, v)

    def dictionary(m):
        items = sorted(m.items(), key=lambda kv: kv[1])
        out = struct.pack('<I', len(items))
        out += b''.join(struct.pack('<H', i) for _, i in items)
        out += b''.join(n.encode('utf-8') + b'\0' for n, _ in items)
        return out

    tagmap = {n: i for n, i in tags.items()}
    attrmap = {n: i for n, i in attrs.items()}
    for i, n in (extra_tags or {}).items(): tagmap[n] = i
    for i, n in (extra_attrs or {}).items(): attrmap[n] = i
    tagoff = len(body)
    blob = bytes(body) + dictionary(tagmap)
    attoff = len(blob)
    blob += dictionary(attrmap)
    return blob + struct.pack('<II', tagoff, attoff) + b'\x08\x00\x00\x00\xfd\xff\xff\xff'


class TestFileDB(unittest.TestCase):
    def test_nesting_and_values(self):
        blob = build_filedb([
            ('GameSessions', [
                ('t1', [('guid', struct.pack('<I', 149679)), ('Name', 'Nile'.encode('utf-16le'))]),
                ('t1', [('guid', struct.pack('<I', 3245))]),
            ]),
            ('Version', b'\x02'),
        ])
        root = A.filedb(blob)
        self.assertEqual(root.name, 'root')
        self.assertEqual([c.name for c in root.children], ['GameSessions', 'Version'])
        sessions = A.ch(A.one(root, 'GameSessions'), 't1')
        self.assertEqual(len(sessions), 2)
        self.assertEqual(A.u(A.one(sessions[0], 'guid').value), 149679)
        self.assertEqual(A.one(sessions[0], 'Name').value.decode('utf-16le'), 'Nile')
        self.assertEqual(A.u(A.one(sessions[1], 'guid').value), 3245)
        self.assertEqual(A.one(root, 'Version').value, b'\x02')

    def test_padding_of_odd_sized_values(self):
        blob = build_filedb([('a', b'xyz'), ('b', b''), ('c', b'12345678'), ('d', b'123456789')])
        root = A.filedb(blob)
        self.assertEqual([(c.name, c.value) for c in root.children],
                         [('a', b'xyz'), ('b', b''), ('c', b'12345678'), ('d', b'123456789')])

    def test_tags_have_no_value(self):
        root = A.filedb(build_filedb([('T', [])]))
        self.assertIsNone(root.children[0].value)
        self.assertEqual(root.children[0].children, [])

    def test_unknown_ids(self):
        blob = build_filedb([(0x42, [(0x9001, b'\x07\x00')])], tag_ids={}, attr_ids={})
        root = A.filedb(blob)
        self.assertEqual(root.children[0].name, 't42')
        self.assertEqual(root.children[0].children[0].name, 'a9001')
        self.assertEqual(root.children[0].children[0].value, b'\x07\x00')

    def test_dictionary_names_used_for_raw_ids(self):
        blob = build_filedb([(5, [(0x8005, b'\x01')])], extra_tags={5: 'Ship'}, extra_attrs={0x8005: 'HP'})
        root = A.filedb(blob)
        self.assertEqual(root.children[0].name, 'Ship')
        self.assertEqual(root.children[0].children[0].name, 'HP')

    def test_extra_close_at_top_level_is_ignored(self):
        blob = build_filedb([('A', [])])
        tagoff, attoff = struct.unpack_from('<II', blob, len(blob) - 16)
        # insert one stray close node before the dictionaries and shift the offsets
        body, rest = blob[:tagoff], blob[tagoff:len(blob) - 16]
        blob2 = body + struct.pack('<iI', 0, 0) + rest + struct.pack('<II', tagoff + 8, attoff + 8) + blob[-8:]
        root = A.filedb(blob2)
        self.assertEqual([c.name for c in root.children], ['A'])


class TestTreeHelpers(unittest.TestCase):
    def setUp(self):
        self.n = A.Node('root')
        self.n.children = [A.Node('x', b'\x01'), A.Node('y', b'\x02'), A.Node('x', b'\x03')]

    def test_ch(self):
        self.assertEqual([c.value for c in A.ch(self.n, 'x')], [b'\x01', b'\x03'])
        self.assertEqual(A.ch(self.n, 'missing'), [])

    def test_one(self):
        self.assertEqual(A.one(self.n, 'x').value, b'\x01')
        self.assertIsNone(A.one(self.n, 'missing'))

    def test_none_safety(self):
        self.assertEqual(A.ch(None, 'x'), [])
        self.assertIsNone(A.one(None, 'x'))
        self.assertIsNone(A.one(A.one(self.n, 'missing'), 'x'))

    def test_u(self):
        self.assertEqual(A.u(b'\x01\x02'), 0x0201)
        self.assertEqual(A.u(struct.pack('<Q', 2 ** 40 + 5)), 2 ** 40 + 5)
        self.assertEqual(A.u(b''), 0)


# ---------------- RDA v2.2 builder ----------------
def build_rda(files, compressed=False):
    """Minimal RDA v2.2: header, offset of the first block at 784, file data, directory, block header."""
    out = bytearray(b'Resource File V2.2'.ljust(784, b'\0'))
    out += b'\0' * 8                                  # offset of the first block, patched below
    directory = bytearray()
    for name, data in files.items():
        stored = zlib.compress(data) if compressed else data
        fo = len(out)
        out += stored
        entry = name.encode('utf-16le').ljust(520, b'\0')
        entry += struct.pack('<QQQQQ', fo, len(stored), len(data), 0, 0)
        directory += entry
    dir_stored = zlib.compress(bytes(directory)) if compressed else bytes(directory)
    out += dir_stored
    block = len(out)
    out += struct.pack('<IIQQQ', 1 if compressed else 0, len(files), len(dir_stored), len(directory), 0)
    struct.pack_into('<Q', out, 784, block)
    return bytes(out)


class TestRDA(TempDirMixin, unittest.TestCase):
    FILES = {'data.a7s': b'hello world' * 10, 'dir/meta.a7s': b'\x00\x01\x02'}

    def test_uncompressed(self):
        path = self.write('plain.rda', build_rda(self.FILES))
        entries = A.rda_entries(path)
        self.assertEqual([n for n, _ in entries], list(self.FILES))
        self.assertEqual({n: g() for n, g in entries}, self.FILES)

    def test_compressed(self):
        path = self.write('zip.rda', build_rda(self.FILES, compressed=True))
        self.assertEqual(A.rda_files(path), self.FILES)

    def test_not_rda(self):
        path = self.write('not.rda', b'PK\x03\x04 this is a zip, not an RDA' + b'\0' * 64)
        with self.assertRaises(ValueError):
            A.rda_entries(path)
        with self.assertRaises(SystemExit):
            A.rda_files(path)

    def test_load_save_parses_data_a7s(self):
        blob = build_filedb([('MetaGameManager', [('Version', b'\x01')])])
        path = self.write('game.a8s', build_rda({'data.a7s': zlib.compress(blob)}))
        root = A.load_save(path)
        self.assertEqual(A.one(A.one(root, 'MetaGameManager'), 'Version').value, b'\x01')

    def test_load_save_without_data_a7s(self):
        path = self.write('other.a8s', build_rda({'meta.a7s': b'x'}))
        with self.assertRaises(SystemExit):
            A.load_save(path)

    # rda_entries must not leak the file object (the mmap keeps its own handle).
    def test_archive_file_is_closed(self):
        path = self.write('closed.rda', build_rda(self.FILES))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            A.rda_files(path)
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])


class TestStorage(unittest.TestCase):
    def words(self, *w):
        return struct.pack('<%dI' % len(w), *w)

    def test_real_example(self):
        b = self.words(2085, 16, 0, 149600, 23, 21, 25, 19)
        self.assertEqual(A.storage(b), {2085: 23, 2102: 21, 2103: 25, 151704: 19})

    def test_single_good(self):
        self.assertEqual(A.storage(self.words(2085, 7)), {2085: 7})

    def test_empty_and_odd(self):
        self.assertEqual(A.storage(b''), {})
        self.assertEqual(A.storage(self.words(2085)), {})
        self.assertEqual(A.storage(self.words(1, 2, 3)), {})

    def test_trailing_bytes_ignored(self):
        self.assertEqual(A.storage(self.words(2085, 7) + b'\x01\x02'), {2085: 7})


class TestNameCities(unittest.TestCase):
    HUMAN = 41

    def area(self, aid, city, routes, owner=41):
        return {'area': aid, 'owner': owner, 'city': city, 'trade_routes': {r: 1 for r in routes}}

    def test_elimination_with_default_names(self):
        a = self.area(1, 'Memphis Nome', ['Flax Kan - Mem', 'Sesame Pods Gif - Mem'])
        b = self.area(2, 'Kanopos Nome', ['Flax Kan - Mem', 'Olives Hap - Kan'])
        c = self.area(3, 'Giftu Nome', ['Sesame Pods Gif - Mem'])
        d = self.area(4, 'Hapi Nome', ['Olives Hap - Kan'])
        A.name_cities([a, b, c, d], [], {self.HUMAN})
        self.assertEqual([e.get('route_code') for e in (a, b, c, d)], ['Mem', 'Kan', 'Gif', 'Hap'])
        self.assertFalse(any('custom_name' in e for e in (a, b, c, d)))

    def test_elimination_with_renamed_cities(self):
        # only A and B have matching default names; C and D are renamed by the human player
        a = self.area(1, 'Memphis Nome', ['Flax Kan - Mem', 'Sesame Pods Gif - Mem'])
        b = self.area(2, 'Kanopos Nome', ['Flax Kan - Mem', 'Olives Hap - Kan'])
        c = self.area(3, 'Sais Nome', ['Sesame Pods Gif - Mem'])
        d = self.area(4, 'Buto Nome', ['Olives Hap - Kan'])
        A.name_cities([a, b, c, d], ['Giftown', 'Hapsburg', 'Trireme Alpha'], {self.HUMAN})
        self.assertEqual([e.get('route_code') for e in (a, b, c, d)], ['Mem', 'Kan', 'Gif', 'Hap'])
        self.assertEqual(c['custom_name'], 'Giftown')
        self.assertEqual(d['custom_name'], 'Hapsburg')

    def test_several_typed_names_give_candidates(self):
        a = self.area(1, 'Memphis Nome', ['Flax Gif - Mem'])
        c = self.area(3, 'Sais Nome', ['Flax Gif - Mem'])
        A.name_cities([a, c], ['Giftown', 'Gifford', 'Giftown'], {self.HUMAN})
        self.assertEqual(c['route_code'], 'Gif')
        self.assertNotIn('custom_name', c)
        self.assertEqual(c['custom_name_candidates'], ['Gifford', 'Giftown'])

    def test_not_kept_when_owner_is_not_human(self):
        a = self.area(1, 'Memphis Nome', ['Flax Gif - Mem'])
        c = self.area(3, 'Sais Nome', ['Flax Gif - Mem'], owner=31099)
        A.name_cities([a, c], ['Giftown'], {self.HUMAN})
        self.assertEqual(a['route_code'], 'Mem')
        self.assertNotIn('route_code', c)
        self.assertNotIn('custom_name', c)

    def test_not_kept_without_matching_typed_text(self):
        a = self.area(1, 'Memphis Nome', ['Flax Gif - Mem'])
        c = self.area(3, 'Sais Nome', ['Flax Gif - Mem'])
        A.name_cities([a, c], ['Trireme Alpha'], {self.HUMAN})
        self.assertNotIn('route_code', c)

    def test_not_kept_when_code_is_another_default_name(self):
        # 'Kan' is the default name of a city elsewhere, so it is not taken as a renamed city
        a = self.area(1, 'Memphis Nome', ['Flax Kan - Mem'])
        c = self.area(3, 'Sais Nome', ['Flax Kan - Mem'])
        other = self.area(9, 'Kanopos Nome', [], owner=31099)
        A.name_cities([a, c, other], ['Kanata'], {self.HUMAN})
        self.assertEqual(a['route_code'], 'Mem')
        self.assertNotIn('route_code', c)

    def test_ambiguous_code_is_not_assigned(self):
        # 'Gif' used by a single route served by two areas, neither with a matching default name
        a = self.area(1, 'Sais Nome', ['Flax Gif - Xoi'])
        c = self.area(3, 'Buto Nome', ['Flax Gif - Xoi'])
        A.name_cities([a, c], ['Giftown', 'Xoisville'], {self.HUMAN})
        self.assertNotIn('route_code', a)
        self.assertNotIn('route_code', c)

    def test_routes_without_code_are_ignored(self):
        a = self.area(1, 'Memphis Nome', ['My route', 'Wine'])
        A.name_cities([a, {'area': 2, 'owner': None, 'city': None}], [], {self.HUMAN})
        self.assertNotIn('route_code', a)


class TestXml(TempDirMixin, unittest.TestCase):
    def test_read_texts_anno117_layout(self):
        f = self.write('texts_english.xml', '<TextExport><Texts>'
                       '<Text><LineId>-123</LineId><Text>Memphis Nome</Text></Text>'
                       '<Text><LineId>456</LineId><Text>Quinquereme</Text></Text>'
                       '<Text><LineId>789</LineId><Text></Text></Text>'
                       '</Texts></TextExport>')
        with quiet():
            t = A.read_texts([f])
        self.assertEqual(t, {-123: 'Memphis Nome', 456: 'Quinquereme'})

    def test_read_texts_anno1800_layout(self):
        f = self.write('texts_italian.xml', '<TextExport><Texts>'
                       '<Text><GUID>5</GUID><Text>Trireme</Text></Text>'
                       '</Texts></TextExport>')
        with quiet():
            self.assertEqual(A.read_texts([f]), {5: 'Trireme'})

    def test_read_texts_later_file_wins_and_bad_xml_is_skipped(self):
        f1 = self.write('a.xml', '<T><Text><GUID>5</GUID><Text>old</Text></Text></T>')
        f2 = self.write('b.xml', '<T><Text><GUID>5</GUID><Text>new</Text></Text></T>')
        bad = self.write('c.xml', '<T><Text>')
        with quiet():
            self.assertEqual(A.read_texts([f1, f2, bad]), {5: 'new'})

    ASSETS = '''<AssetList><Groups><Group><Assets>
  <Asset>
    <Template>Ship</Template>
    <Values>
      <Standard><GUID>100</GUID><Name>quinquereme</Name></Standard>
      <Text><OasisId>-555</OasisId></Text>
    </Values>
  </Asset>
  <Asset>
    <BaseAssetGUID>100</BaseAssetGUID>
    <Values><Standard><GUID>101</GUID><Name>quinquereme_dlc</Name></Standard></Values>
  </Asset>
  <Asset>
    <Template>CityStatus</Template>
    <Values>
      <Standard><GUID>200</GUID><Name>city status</Name></Standard>
      <Text><OasisId>1</OasisId></Text>
      <CityStatus><CityStatusName>-777</CityStatusName></CityStatus>
    </Values>
  </Asset>
  <Asset>
    <Template>FactoryBuilding</Template>
    <Values>
      <Standard><GUID>300</GUID><Name>mud brick maker</Name></Standard>
      <FactoryBase>
        <FactoryInputs><Item><Product>2102</Product><Amount>2</Amount></Item>
                       <Item><Product>2103</Product></Item>
                       <Item><Amount>3</Amount></Item></FactoryInputs>
        <FactoryOutputs><Item><Product>151704</Product></Item></FactoryOutputs>
        <CycleTime>45</CycleTime>
      </FactoryBase>
    </Values>
  </Asset>
  <Asset>
    <Template>Farm</Template>
    <Values><Standard><GUID>301</GUID><Name>empty factory</Name></Standard><FactoryBase/></Values>
  </Asset>
  <Asset><Values><Standard><GUID>not-a-number</GUID></Standard></Values></Asset>
</Assets></Group></Groups></AssetList>'''

    def test_read_assets(self):
        f = self.write('assets.xml', self.ASSETS)
        with quiet():
            a = A.read_assets([f])
        self.assertEqual(sorted(a), [100, 101, 200, 300, 301])
        self.assertEqual(a[100], {'name': 'quinquereme', 'template': 'Ship', 'text_id': '-555'})
        self.assertEqual(a[101], {'name': 'quinquereme_dlc', 'base': '100'})
        self.assertEqual(a[200]['text_id'], '-777')        # CityStatusName wins over OasisId
        self.assertEqual(a[300]['factory'], {'in': {'2102': 2, '2103': 1}, 'out': {'151704': 1}, 'cycle': '45'})
        self.assertNotIn('factory', a[301])                 # empty FactoryBase leaves nothing


class TestLoadNames(TempDirMixin, unittest.TestCase):
    def test_merge(self):
        plain = self.write('guid_map.json', json.dumps({
            '41': 'Player', '100': 'Quinquereme?', '7': 'old', 'comment': 'ignored'}))
        assets = self.write('guid_names.json', json.dumps({
            '100': {'name': 'ship_quinquereme', 'name_local': 'Quinquereme', 'template': 'Ship'},
            '200': {'name': 'ship_trireme'},
            '7': {'template': 'NoName'}}))
        with quiet():
            m = A.load_names([plain, assets, os.path.join(self.tmp, 'missing.json')])
        self.assertEqual(m, {41: 'Player', 100: 'Quinquereme', 200: 'ship_trireme', 7: 'old'})

    def test_plain_map_overrides_assets_when_later(self):
        assets = self.write('a.json', json.dumps({'100': {'name': 'x', 'name_local': 'Y'}}))
        plain = self.write('b.json', json.dumps({'100': 'Mine'}))
        self.assertEqual(A.load_names([assets, plain]), {100: 'Mine'})

    def test_load_info_keeps_only_records(self):
        f = self.write('n.json', json.dumps({'100': {'factory': {'cycle': '30'}}, '41': 'Player'}))
        self.assertEqual(A.load_info([f]), {100: {'factory': {'cycle': '30'}}})


class TestRegressions(TempDirMixin, unittest.TestCase):
    """Bugs found in review; each test fails on the code before the fix."""

    def test_name_cities_is_deterministic(self):
        # Only area A served route 'x Mem - Kan'; its default name confirms Mem, so Kan must not
        # steal it through the typed text 'Kanopus', whatever the hash seed.
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import anno117 as A; "
                "a = {'area': 1, 'owner': 41, 'city': 'Memphis', 'trade_routes': {'x Mem - Kan': 1}}; "
                "A.name_cities([a], ['Kanopus'], {41}); print(a.get('route_code'), a.get('custom_name'))") % ROOT
        seen = set()
        for seed in range(6):
            env = dict(os.environ, PYTHONHASHSEED=str(seed))
            seen.add(subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True).stdout.strip())
        self.assertEqual(seen, {'Mem None'})

    def assets_run(self, assets_xml, props_xml):
        os.makedirs(os.path.join(self.tmp, 'x', 'export'))
        with open(os.path.join(self.tmp, 'x', 'assets.xml'), 'w', encoding='utf-8') as f: f.write(assets_xml)
        with open(os.path.join(self.tmp, 'x', 'export', 'properties.xml'), 'w', encoding='utf-8') as f: f.write(props_xml)
        out = os.path.join(self.tmp, 'names.json')
        import argparse
        with quiet():
            A.cmd_assets(argparse.Namespace(path=os.path.join(self.tmp, 'x'), output=out, lang='english',
                                            all=True, extract_dir=os.path.join(self.tmp, 'ex')))
        with open(out, encoding='utf-8') as f:
            return json.load(f)

    def test_factory_inherits_per_key(self):
        xml = ('<AssetList><Asset><Template>Production</Template><Values><Standard><GUID>10</GUID></Standard>'
               '<FactoryBase><FactoryInputs><Item><Product>1</Product><Amount>2</Amount></Item></FactoryInputs>'
               '<FactoryOutputs><Item><Product>2</Product><Amount>0.5</Amount></Item></FactoryOutputs>'
               '<CycleTime>60</CycleTime></FactoryBase></Values></Asset>'
               '<Asset><BaseAssetGUID>10</BaseAssetGUID><Values><Standard><GUID>11</GUID></Standard>'
               '<FactoryBase><CycleTime>20</CycleTime></FactoryBase></Values></Asset>'
               '<Asset><Template>Production</Template><Values><Standard><GUID>12</GUID></Standard>'
               '<FactoryBase><FactoryOutputs><Item><Product>3</Product></Item></FactoryOutputs></FactoryBase>'
               '</Values></Asset></AssetList>')
        props = ('<Properties><FactoryBase><FuelStorageAmount>5</FuelStorageAmount></FactoryBase>'
                 '<Other><CycleTime>7</CycleTime></Other>'
                 '<Defaults><FactoryBase><CycleTime>30</CycleTime></FactoryBase></Defaults></Properties>')
        m = self.assets_run(xml, props)
        self.assertEqual(m['10']['factory'], {'in': {'1': 2}, 'out': {'2': 0.5}, 'cycle': '60'})
        self.assertEqual(m['11']['factory'], {'in': {'1': 2}, 'out': {'2': 0.5}, 'cycle': '20'})
        self.assertEqual(m['12']['factory'], {'out': {'3': 1}, 'cycle': '30'})   # not the 7 of <Other>

    def test_num(self):
        self.assertEqual([A.num('3'), A.num('0.5'), A.num('2.0'), A.num(None, 1), A.num('x', 1)], [3, 0.5, 2, 1, 1])

    def test_truncated_archive_exits_cleanly(self):
        f = self.write('bad.a8s', b'Resource File V2.2' + bytes(100))
        with quiet(), self.assertRaises(SystemExit):
            A.rda_files(f)


class TestGuidTable(TempDirMixin, unittest.TestCase):
    def test_sections_per_template_in_given_order(self):
        import argparse
        names = self.write('n.json', json.dumps({
            '1': {'name': 'Troop B', 'template': 'LandUnit', 'name_local': 'Bee'},
            '2': {'name': 'Troop A', 'template': 'LandUnit'},
            '3': {'name': 'Ship', 'template': 'NavalUnit', 'name_local': 'Ship | One'},
            '4': {'name': 'Farm', 'template': 'Production'}}))
        out = os.path.join(self.tmp, 'sub', 'g.md')
        with quiet():
            A.cmd_guid_table(argparse.Namespace(names=names, output=out, title='T',
                                                template=['NavalUnit', 'LandUnit']))
        with open(out, encoding='utf-8') as f:
            md = f.read()
        self.assertLess(md.index('## Ships (1)'), md.index('## Land units (2)'))
        self.assertLess(md.index('| 2 | Troop A |'), md.index('| 1 | Troop B | Bee |'))
        self.assertIn('Ship \\| One', md)
        self.assertNotIn('Farm', md)


class TestCityNames(TempDirMixin, unittest.TestCase):
    def test_file_names_win_and_session_prefix(self):
        a = {'area': 10, 'owner': 41, 'city': 'Old', 'custom_name_candidates': ['X', 'Y']}
        b = {'area': 10, 'owner': 41, 'city': 'Other'}
        c = {'area': 11, 'owner': 41, 'city': 'Kept', 'custom_name': 'From routes'}
        f = self.write('names.json', json.dumps({'nile:10': 'Memphis', '10': 'Fallback'}))
        A.apply_city_names([('nile', [a, c]), ('latium', [b])], f)
        self.assertEqual((a['custom_name'], b['custom_name'], c['custom_name']), ('Memphis', 'Fallback', 'From routes'))
        self.assertNotIn('custom_name_candidates', a)

    def test_no_file_is_a_no_op(self):
        a = {'area': 10, 'owner': 41}
        A.apply_city_names([('nile', [a])], None)
        self.assertEqual(a, {'area': 10, 'owner': 41})


class TestRouteFlows(unittest.TestCase):
    def test_supplier_is_the_stop_that_produces_the_good(self):
        # the hub also has a few sheep farms; the farm island has more, all idle at save time
        hub = {'area': 1, 'owner': 41, 'custom_name': 'Portus', 'output_capacity': {'Sheep': 4.0}}
        farm = {'area': 2, 'owner': 41, 'city': 'Aeretia', 'output_capacity': {'Flax': 3.0, 'Sheep': 8.0}}
        results = [{'session': 'latium', 'areas': [hub, farm]}]
        # route named with the hub first, as in the game: the name does not give the direction
        routes = [{'id': 7, 'name': 'Flax Por - Aer', 'owner': 41, 'ships': [],
                   'stations': [{'area': 1, 'goods': {10: 50}}, {'area': 2, 'goods': {10: 50, 11: 50}}]}]
        A.attach_routes(results, routes, {}, {41: 'Me'}, {10: 'Flax', 11: 'Sheep'})
        self.assertEqual(farm['supplies'], {'Flax': ['Portus'], 'Sheep': ['Portus']})
        self.assertEqual(hub['receives'], {'Flax': ['Aeretia'], 'Sheep': ['Aeretia']})
        self.assertNotIn('supplies', hub)
        self.assertEqual(results[0]['trade_routes'][0]['stations'][0]['city'], 'Portus')


class TestEconomyStatus(unittest.TestCase):
    def node(self, name, value=None, kids=()):
        n = A.Node(name, value); n.children = list(kids); return n

    def test_idle_reasons_from_status_icons(self):
        f32 = lambda v: struct.pack('<f', v)
        def building(prod, icons=()):
            return self.node('t1', None, [
                self.node('guid', struct.pack('<I', 500)), self.node('Building'),
                self.node('Factory7', None, [self.node('CurrentProductivity', f32(prod))]),
                self.node('Infolayer', None, [self.node('Icons', None, [
                    self.node('t1', None, [self.node('GUID', struct.pack('<I', i))]) for i in icons])])])
        objs = [building(1.0), building(0.0, [7]), building(0.0, [7]), building(0.0)]
        names = {500: 'Sheep farm', 7: 'StorageFull'}
        info = {500: {'factory': {'out': {'9': 1}, 'cycle': '60'}}}
        res = A.economy(objs, lambda g: names.get(g, str(g)), info)
        row = res['production'][0]
        self.assertEqual(row['idle_reasons'], {'StorageFull': 2, '(no icon)': 1})
        self.assertEqual(res['idle_reasons'], {'StorageFull': 2, '(no icon)': 1})
        self.assertEqual(res['output_capacity'], {'9': 4.0})   # every farm at 100%


if __name__ == '__main__':
    unittest.main()
