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

    def test_residence_needs(self):
        xml = ('<AssetList><Asset><Template>ResidenceBuilding</Template><Values><Standard><GUID>30</GUID></Standard>'
               '<Residence7><NeedsList><Item><Need>40</Need><NeedConsumptionRate>0.02</NeedConsumptionRate></Item>'
               '<Item><Need>41</Need></Item><Item><Need>42</Need><NeedConsumptionRate>0.01</NeedConsumptionRate>'
               '<IsOnlyAvailableThroughBuff>1</IsOnlyAvailableThroughBuff></Item></NeedsList></Residence7></Values></Asset>'
               '<Asset><BaseAssetGUID>30</BaseAssetGUID><Values><Standard><GUID>31</GUID></Standard></Values></Asset>'
               '<Asset><Template>Need</Template><Values><Standard><GUID>40</GUID><Name>Need Roman Food Fish</Name></Standard>'
               '<Need><NeedProduct>50</NeedProduct><NeedCategoryType>Food</NeedCategoryType></Need></Values></Asset>'
               '<Asset><Template>Need</Template><Values><Standard><GUID>41</GUID><Name>Need Roman Celtic Public Market</Name>'
               '</Standard><Need><NeedProduct>51</NeedProduct></Need></Values></Asset>'
               '<Asset><Template>Need</Template><Values><Standard><GUID>42</GUID><Name>Need Roman Food Bread</Name></Standard>'
               '<Need><NeedProduct>52</NeedProduct><NeedCategoryType>Food</NeedCategoryType></Need></Values></Asset>'
               '</AssetList>')
        m = self.assets_run(xml, '<Properties/>')
        needs = {n['product']: n for n in m['30']['needs']}
        self.assertEqual(needs['50'], {'need': '40', 'rate': '0.02', 'product': '50', 'category': 'Food'})
        self.assertEqual(needs['51']['category'], 'Public')           # from the name: culture words skipped
        self.assertNotIn('rate', needs['51'])                         # a service, not a good
        self.assertTrue(needs['52']['buff_only'])
        self.assertEqual(m['31']['needs'], m['30']['needs'])         # inherited from the base asset

    def test_unit_spawners(self):
        xml = ('<AssetList><Asset><Template>MilitaryTowerUnitSpawner</Template><Values><Standard><GUID>60</GUID></Standard>'
               '<UnitSpawner><Stages><Item><UnitAmount>6</UnitAmount><UnitTypes><Item><Unit>70</Unit></Item>'
               '<Item><Unit>71</Unit></Item></UnitTypes></Item><Item><UnitAmount>2</UnitAmount><UnitTypes>'
               '<Item><Unit>70</Unit></Item></UnitTypes></Item></Stages><UnitLimit>6</UnitLimit></UnitSpawner>'
               '<UnitSpawnerTrigger/></Values></Asset>'
               '<Asset><Template>UnitSpawnerTrigger</Template><Values><Standard><GUID>61</GUID></Standard>'
               '<UnitSpawner/><UnitSpawnerTrigger><DetectionRadius>5</DetectionRadius><Cooldown>14400000</Cooldown>'
               '<AdditionalUnitSpawnerList><Item><UnitSpawner>60</UnitSpawner></Item></AdditionalUnitSpawnerList>'
               '</UnitSpawnerTrigger></Values></Asset>'
               '<Asset><BaseAssetGUID>60</BaseAssetGUID><Values><Standard><GUID>62</GUID></Standard></Values></Asset>'
               '</AssetList>')
        m = self.assets_run(xml, '<Properties/>')
        self.assertEqual(m['60']['spawner'], {'units': 8, 'unit_types': ['70', '71'], 'limit': 6})
        self.assertEqual(m['61']['spawner'], {'radius': 5, 'cooldown_s': 14400, 'triggers': ['60']})
        self.assertEqual(m['62']['spawner'], m['60']['spawner'])     # inherited from the base asset

    def test_military_goods(self):
        xml = ('<AssetList>'
               '<Asset><Template>LandUnit</Template><Values><Standard><GUID>80</GUID></Standard><Cost><Costs>'
               '<Item><Ingredient>1010017</Ingredient><Amount>5000</Amount></Item>'
               '<Item><Ingredient>90</Ingredient><Amount>10</Amount></Item>'
               '<Item><Ingredient>91</Ingredient><Amount>5</Amount></Item>'
               '<Item><Ingredient>92</Ingredient></Item></Costs></Cost></Values></Asset>'
               + ''.join(f'<Asset><Template>Building{i}</Template><Values><Standard><GUID>{81 + i}</GUID></Standard>'
                         '<Cost><Costs><Item><Ingredient>91</Ingredient><Amount>3</Amount></Item></Costs></Cost>'
                         '</Values></Asset>' for i in range(3))
               + '<Asset><Template>Product</Template><Values><Standard><GUID>90</GUID></Standard></Values></Asset>'
               '<Asset><Template>Product</Template><Values><Standard><GUID>91</GUID></Standard></Values></Asset>'
               '</AssetList>')
        m = self.assets_run(xml, '<Properties/>')
        self.assertTrue(m['90'].get('military'))                    # only units pay with it
        self.assertNotIn('military', m['91'])                        # three building types use it too: timber
        self.assertNotIn('1010017', [k for k, v in m.items() if v.get('military')])   # money is not a good
        self.assertNotIn('cost_in', m['80'])

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


class TestCamped(unittest.TestCase):
    def test_disposition_two_is_camped(self):
        n = lambda name, value=None, kids=(): setattr(x := A.Node(name, value), 'children', list(kids)) or x
        unit = lambda v: n('o', None, [n('Unit', None, [n('MirroredUnitState', None, [n('SerializableUnitState', None, [
            n('DispositionState', None, [n('value', None, [n('id', struct.pack('<H', v))])])])])])])
        self.assertTrue(A.camped(unit(2)))
        self.assertFalse(A.camped(unit(0)))
        self.assertIsNone(A.camped(n('o')))


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

    def test_ship_loads_seen_in_the_history_win_over_capacity(self):
        # the capital bakes more bread, but the history shows ships loading it at the small city
        cap = {'area': 1, 'owner': 41, 'city': 'Cinis', 'output_capacity': {'Bread': 20.0},
               'stock_trends': {'Bread': {'loaded': 0, 'unloaded': 90}}}
        small = {'area': 2, 'owner': 41, 'city': 'Memphis', 'output_capacity': {'Bread': 4.0},
                 'stock_trends': {'Bread': {'loaded': 97, 'unloaded': 0}}}
        results = [{'session': 'x', 'areas': [cap, small]}]
        routes = [{'id': 1, 'name': 'Bread Mem - Cin', 'owner': 41, 'ships': [],
                   'stations': [{'area': 2, 'goods': {10: 50}}, {'area': 1, 'goods': {10: 50}}]}]
        A.attach_routes(results, routes, {}, {41: 'Me'}, {10: 'Bread'})
        self.assertEqual(small['supplies'], {'Bread': ['Cinis']})
        self.assertEqual(cap['receives'], {'Bread': ['Memphis']})

    def test_route_ships_carry_meta_id_and_name(self):
        results = [{'session': 'nile', 'areas': [{'area': 1, 'owner': 41}]}]
        routes = [{'id': 1, 'name': 'Bread Mem - Cin', 'owner': 41, 'ships': [7, 8, 9],
                   'stations': [{'area': 1, 'goods': {}}]}]
        ships = {7: ('nile', 37224, 10, 20, 'Annona'), 8: ('nile', 37224, 11, 21, None)}
        A.attach_routes(results, routes, ships, {41: 'Me'}, {37224: 'Quinquereme'})
        got = results[0]['trade_routes'][0]['ships']
        self.assertEqual(got[0], {'meta_id': 7, 'name': 'Annona', 'type': 'Quinquereme', 'guid': 37224,
                                  'session': 'nile', 'position': [10, 20]})
        self.assertNotIn('name', got[1])
        self.assertEqual(got[2], {'meta_id': 9})

    def test_names_file_names_ships_and_land_units(self):
        ship = {'meta_id': 7, 'name': 'Zenobia II'}
        route_ship = {'meta_id': 7, 'name': 'Zenobia II'}
        legion = {'meta_id': 9, 'name': 'Lupa III'}
        results = [{'participants': [{'ships': [{'ships': [ship, {'meta_id': 8}]}],
                                      'units': [{'units': [legion]}]}],
                    'trade_routes': [{'ships': [route_ship]}]}]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'_nota': 'x', '7': 'Annona I', '8': '', '9': 'Legio XXII Deiotariana'}, f)
        A.apply_unit_names(results, f.name)
        os.unlink(f.name)
        self.assertEqual(ship['custom_name'], 'Annona I')
        self.assertEqual(route_ship['custom_name'], 'Annona I')
        self.assertEqual(legion['custom_name'], 'Legio XXII Deiotariana')
        self.assertNotIn('custom_name', results[0]['participants'][0]['ships'][0]['ships'][1])

    def test_cross_province_legs_and_border(self):
        routes = [{'owner': 41, 'stations': [{'area': 1, 'session': 'nile', 'city': 'Memphis'},
                                             {'area': 9, 'session': 'latium', 'city': 'Cinis'}]},
                  {'owner': 41, 'stations': [{'area': 1, 'session': 'nile'}, {'area': 2, 'session': 'nile'}]},
                  {'owner': 7, 'stations': [{'area': 1, 'session': 'nile'}, {'area': 9, 'session': 'latium'}]}]
        legs = A.cross_legs(routes, 'nile', {1: (100, 100), 2: (500, 500)}, {41})
        self.assertEqual(legs, {(1, 'latium', 41): ['Cinis']})
        self.assertEqual(A.edge_point(100, 950, 1000), (100, 1000))
        self.assertEqual(A.edge_point(30, 500, 1000), (0, 500))

    def test_army_view_groups_forces_and_finds_spare_ships(self):
        war = lambda m, x: {'meta_id': m, 'role': 'war', 'modules': ['Remi'], 'position': [x, 100]}
        r = {'session': 'nile', 'participants': [
                {'participant': 41, 'troops': 2,
                 'ships': [{'type': 'Trireme', 'ships': [war(1, 100), war(2, 110), war(3, 900)]},
                           {'type': 'Aba', 'ships': [{'meta_id': 4, 'role': 'civil', 'position': [5, 5]},
                                                     {'meta_id': 5, 'role': 'civil', 'position': [6, 6]}]}],
                 'units': [{'type': 'Arcieri', 'troop': True, 'units': [
                     {'meta_id': 6, 'custom_name': 'XXII Sagittarii I', 'position': [50, 50], 'camped': True},
                     {'meta_id': 7, 'position': [52, 50], 'camped': False}]}]},
                {'participant': 99, 'warships': 1, 'ships': [{'type': 'Aba', 'ships': [war(8, 500)]}]}],
             'areas': [{'area': 1, 'custom_name': 'Memphis', 'center': [100, 90]}],
             'trade_routes': [{'name': 'Annona Mem - Cin', 'owner': 41, 'ships': [{'meta_id': 4, 'session': 'nile',
                                'type': 'Aba'}], 'stations': [{'area': 1, 'city': 'Memphis'}, {'area': 9, 'city': 'Cinis'}]}]}
        a = A.army_view([r], {41}, {'99': 'pirate'})[0]
        self.assertEqual((a['warships'], a['pirates'], a['spare'], a['merchant'], a['on_routes'], a['idle'], a['state']),
                         (3, 1, 1, 2, 1, 1, 'can spare ships'))
        fleet = {t['title']: t['rows'] for t in a['fleet']}
        self.assertEqual([r[1][1] for r in fleet['Warships']], [2, 1])   # two near each other, one apart
        self.assertEqual(fleet['Warships'][0][0], ['Memphis', '105, 100'])
        self.assertEqual(fleet['Warships'][1][0], ['off Memphis', '900, 100'])
        self.assertEqual(a['fleet'][0]['headers'], ['Where', 'Ships', 'Ships and modules'])   # no names: no column
        self.assertEqual(fleet['Merchant ships on your trade routes'][0],
                         ['Annona Mem - Cin', ('1', 1), ['1 × Aba'], 'Memphis → Cinis'])
        troops = a['army'][0]['rows']
        self.assertEqual(troops[0][2:], [('1', 1), ['2 × Arcieri'], ['XXII Sagittarii I']])
        self.assertEqual(a['camped'], 1)
        r['participants'][1]['ships'][0]['ships'] += [war(9, 500), war(10, 500), war(11, 500)]
        self.assertEqual(A.army_view([r], {41}, {'99': 'pirate'})[0]['state'], 'needs reinforcements')

    def test_history_records_ship_steps(self):
        n = lambda name, value=None, kids=(): setattr(x := A.Node(name, value), 'children', list(kids)) or x
        pts = [100, 99, 49, 48, 98, 97]               # a ship loads 50, another unloads 50
        tr = n('StorageTrends', None, [n('a8000', struct.pack('<I', 5)), n('t1', None, [n('Points', None,
               [n('a8000', struct.pack('<I', v)) for v in pts])])])
        t = A.stock_history(tr)[5]
        self.assertEqual((t['loaded'], t['unloaded']), (50, 50))


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
        self.assertNotIn('idle_output_full', res)               # 7 is not the StorageFull icon
        self.assertEqual(res['idle_other_reasons'], {'StorageFull': 2, '(no icon)': 1})

    def test_storage_full_icon_is_counted_apart(self):
        f32 = lambda v: struct.pack('<f', v)
        def building(icons):
            return self.node('t1', None, [
                self.node('guid', struct.pack('<I', 500)), self.node('Building'),
                self.node('Factory7', None, [self.node('CurrentProductivity', f32(0.0))]),
                self.node('Infolayer', None, [self.node('Icons', None, [
                    self.node('t1', None, [self.node('GUID', struct.pack('<I', i))]) for i in icons])])])
        full = A.ICON_STORAGE_FULL
        res = A.economy([building([full]), building([full, 8]), building([8])], str, {})
        self.assertEqual(res['idle_output_full'], 2)
        self.assertEqual(res['idle_other_reasons'], {'8': 1})


class TestStockTrends(unittest.TestCase):
    def trends(self, series):
        n = lambda name, value=None, kids=(): setattr(x := A.Node(name, value), 'children', list(kids)) or x
        kids = []
        for g, pts in series.items():
            kids.append(n('a8000', struct.pack('<I', g)))
            kids.append(n('t1', None, [n('Points', None, [n('capacity', struct.pack('<Q', 120))]
                                         + [n('a8000', struct.pack('<I', p)) for p in pts])]))
        return A.stock_history(n('StorageTrends', None, kids))

    def test_slope_flows_and_peak(self):
        # 13 samples = 2 minutes; falls by 1 every sample except one delivery of +10
        pts = [100 - i for i in range(13)]
        pts[6:] = [p + 10 for p in pts[6:]]
        t = self.trends({5: pts, 6: [0] * 13})
        self.assertNotIn(6, t)                                  # never stocked: skipped
        self.assertEqual(t[5]['minutes'], 2.0)
        self.assertEqual(t[5]['outflow_per_minute'], 5.5)       # 11 units fell in 2 minutes
        self.assertEqual(t[5]['inflow_per_minute'], 4.5)        # one rise of 9 (10 - 1)
        self.assertEqual(t[5]['max'], 104)                      # 94 + the delivery
        self.assertEqual(t[5]['net_per_minute'], -1.0)          # 100 -> 98 in 2 minutes

    def test_storage_limits(self):
        peaks = [450, 450, 450, 1950, 1950, 1950, 300, 40, 40, 25, 25, 25, 10, 10, 10]
        self.assertEqual(A.storage_limits(peaks), [25, 450, 1950])     # two goods at 40 are a coincidence
        self.assertEqual(A.storage_limits([450, 450, 300]), [])
        self.assertTrue(A.at_limit(445, {'max': 450}, [450]))
        self.assertTrue(A.at_limit(445, {'max': 448}, [450]))           # its own peak need not be exact
        self.assertFalse(A.at_limit(300, {'max': 450}, [450]))
        self.assertFalse(A.at_limit(445, {'max': 1950}, [450, 1950]))   # a 1950 good on its way down

    def test_chart_overrides_storage(self):
        # StrgLrg reads 0 for goods the warehouse chart shows full: areas() trusts the chart's last sample
        n = lambda name, value=None, kids=(): setattr(x := A.Node(name, value), 'children', list(kids)) or x
        tr = n('StorageTrends', None, [n('a8000', struct.pack('<I', 5)), n('t1', None, [n('Points', None,
               [n('a8000', struct.pack('<I', v)) for v in (749, 750, 749)])])])
        h = A.stock_history(tr)
        self.assertEqual((h[5]['first'], h[5]['last']), (749, 749))


class TestReport(TempDirMixin, unittest.TestCase):
    def session(self, pop=1000, stock=10, ships=3, owner=41):
        trend = {'net_per_minute': -1.0, 'outflow_per_minute': 1.0, 'inflow_per_minute': 0.0, 'max': 30, 'minutes': 20}
        full = {'net_per_minute': 0.0, 'outflow_per_minute': 1.0, 'inflow_per_minute': 1.0, 'max': 450, 'minutes': 20}
        return {'session': 'latium', 'save': 's.a8s', 'session_time_ms': 600000, 'human_players': [41],
                'participants': [{'participant': 41, 'name': 'Me', 'ship_count': ships + 1,
                                  'warships': ships, 'civil_ships': 1, 'troops': ships * 2,
                                  'units': [{'type': 'Legion', 'guid': 3, 'count': ships * 2, 'troop': True},
                                            {'type': 'Tower', 'guid': 4, 'count': 9}],
                                  'ships': [{'type': 'Trireme', 'guid': 1, 'count': ships,
                                             'ships': [{'role': 'war'}] * ships},
                                            {'type': 'Quinquereme', 'guid': 2, 'count': 1, 'ships': [{'role': 'civil'}]}]},
                                 {'participant': 50, 'name': None, 'ship_count': 0, 'units': [], 'ships': []}],
                'areas': [{'area': 7, 'owner': owner, 'owner_name': 'Me', 'city': 'Roma', 'population': pop,
                           'income_per_minute': 100, 'workforce_saturation': {'Plebs': 0.5},
                           'production': [{'count': 4}], 'idle_output_full': 2, 'idle_other_reasons': {'Missing': 3},
                           'storage': {'Bread': stock, 'Wine': 449, 'Oil': 450}, 'output_capacity': {},
                           'stock_trends': {'Bread': trend, 'Wine': full, 'Oil': full},
                           'storage_limits_estimate': [450], 'receives': {'Wine': ['Ostia']}},
                          {'area': 8, 'owner': 41, 'owner_name': 'Me', 'city': 'Ostia', 'output_capacity': {'Wine': 2.0}}],
                'trade_routes': [{'id': 1, 'name': 'Empty', 'owner': 41, 'ships': [], 'stations': [
                    {'area': 7, 'session': 'latium', 'city': 'Roma', 'goods': {'Fish': 0}}]},
                                 {'id': 2, 'name': 'Wine Ost - Rom', 'owner': 41, 'ships': [5], 'stations': [
                    {'area': 8, 'session': 'latium', 'city': 'Ostia', 'goods': {'Wine': 0}},
                    {'area': 7, 'session': 'latium', 'city': 'Roma', 'goods': {'Wine': 0}}]}]}

    def test_alarms(self):
        rep = A.report_data([self.session()], [self.session(pop=2000, ships=5)], {41})
        text = '\n'.join(f"{a['level']} {a['place']}: {a['text']}" for a in rep['alarms'])
        self.assertIn('high Roma (latium): Bread: 10 left', text)        # 10 min < horizon / 3
        self.assertIn('Plebs: only 50%', text)
        self.assertIn('overproduction: 2 of 4 production buildings idle', text)
        self.assertIn('3 production buildings stopped: Missing ×3', text)
        self.assertIn('info Wine Ost - Rom: 1 good already at the storage limit: Wine at Roma (449)', text)
        self.assertIn('population fell from 2,000 to 1,000', text)
        self.assertIn('no ships assigned', text)
        self.assertIn('none of its goods (Fish)', text)
        self.assertIn('2 warships fewer than in the previous save (5 → 3)', text)
        self.assertIn('4 troops fewer', text)
        self.assertNotIn('civil ships fewer', text)
        forces = rep['sections'][0]
        self.assertEqual(forces['id'], 'forces')
        self.assertEqual(forces['tables'][0]['rows'][0][1], ('3 (-2)', 3))
        tables = {t['title']: t for t in rep['sections'][1]['tables']}
        self.assertEqual([r[1:3] for r in tables['Fleets']['rows']], [['war', 'Trireme'], ['civil', 'Quinquereme']])
        self.assertEqual([r[1] for r in tables['Troops']['rows']], ['Legion'])            # towers are not troops
        self.assertEqual([a['level'] for a in rep['alarms']], sorted((a['level'] for a in rep['alarms']),
                         key=['high', 'medium', 'info'].index))
        self.assertEqual(rep['sections'][1]['elapsed_min'], 0)
        cards = {(g['kind'], g['place']): g for g in rep['groups']}
        self.assertEqual(cards[('route', 'Empty')]['detail'], 'Roma · 0 ships')
        self.assertEqual(cards[('island', 'Roma (latium)')]['level'], 'high')
        self.assertIn('nothing brings it here: add a route or build production', [i['hint'] for i in cards[('island', 'Roma (latium)')]['items']])
        self.assertEqual(rep['groups'][0]['level'], 'high')

    def test_military_goods_are_quiet_unless_used_here(self):
        s = self.session()
        s['military_goods'] = ['Bread']                        # Bread drains fast in this fixture
        text = '\n'.join(a['text'] for a in A.report_data([s], None, {41})['alarms'])
        self.assertNotIn('Bread:', text)                       # nobody here needs it: the player spends it
        s['areas'][0]['needs'] = {'Bread': {'kind': 'good', 'houses': ['Plebs house']}}
        text = '\n'.join(a['text'] for a in A.report_data([s], None, {41})['alarms'])
        self.assertIn('Bread: 10 left', text)                  # residents eat it: the alarm stays

    def test_lost_island_and_no_previous(self):
        rep = A.report_data([self.session(owner=99)], [self.session()], {41})
        self.assertTrue(any('lost' in a['text'] for a in rep['alarms']))
        rep = A.report_data([self.session()], None, {41})
        self.assertFalse(any('fell' in a['text'] or 'fewer' in a['text'] for a in rep['alarms']))

    def test_renderers(self):
        rep = A.report_data([self.session()], [self.session(pop=900)], {41})
        md = A.render_md(rep)
        self.assertIn('| Roma |', md)
        self.assertIn('1,000 (+100)', md)
        page = A.render_html(rep, {})
        self.assertTrue(page.startswith('<!doctype html>'))
        self.assertIn('data-v="1000"', page)
        self.assertNotIn('http', page.split('<footer')[0].replace('http-equiv', ''))  # nothing loaded from outside

    def test_report_command_on_extract_folder(self):
        d = os.path.join(self.tmp, 'json')
        os.makedirs(d)
        A.write_json(os.path.join(d, 'latium.json'), self.session(), 2)
        out = os.path.join(self.tmp, 'r.html')
        args = A.argparse.Namespace(current=d, compare=d, output=out, md=out + '.md', maps=None, player=None,
                                    horizon=60, names=None, texts=None, city_names=None, radius=40, stale=45)
        with quiet(): A.cmd_report(args)
        self.assertTrue(os.path.getsize(out) > 1000)
        with open(out + '.md', encoding='utf-8') as f: self.assertIn('Compared with', f.read())
        with open(out + '.stamp.js', encoding='utf-8') as f: stamp = f.read()      # the open page polls it
        with open(out, encoding='utf-8') as f: page = f.read()
        build = stamp.split('"')[1]
        self.assertIn(f'"build": "{build}"', page)
        self.assertIn('"stamp": "r.html.stamp.js"', page)
        self.assertFalse(os.path.exists(out + '.tmp'))

    def test_new_and_resolved(self):
        old = self.session()
        old['areas'][0]['workforce_saturation'] = {'Plebs': 1.0}           # no workforce alarm before
        old['areas'][0]['idle_other_reasons'] = {}
        old['trade_routes'] = old['trade_routes'][1:]                        # the empty route did not exist
        old['areas'][0]['storage']['Bread'] = 7                               # same alarm, other numbers
        rep = A.report_data([self.session()], [old], {41})
        new = {a['text'].split(':')[0] for a in rep['alarms'] if a.get('new')}
        self.assertIn('Plebs', new)
        self.assertIn('no ships assigned', new)
        self.assertNotIn('Bread', new)                                       # numbers changed, still the same alarm
        rep = A.report_data([old], [self.session()], {41})
        self.assertGreaterEqual(rep['resolved'], 3)
        self.assertEqual(rep['groups'][0]['provs'], ['latium'])

    def test_html_controls(self):
        page = A.render_html(A.report_data([self.session()], None, {41}), {})
        for needle in ('class="bar"', 'data-level="high"', 'data-prov="latium"', 'id="theme"', 'localStorage'):
            self.assertIn(needle, page)
        self.assertIn('data-theme="dark"', page)

    def test_event_over(self):
        self.assertIsNone(A.event_over({'festival_meters': {'3': 0.4}}, {'festival_meters': {'3': 0.45}}))
        self.assertEqual(A.event_over({'festival_meters': {'7': 0.0}}, {'festival_meters': {'7': 0.03}}), 'a festival')
        self.assertIsNone(A.event_over({'festival_meters': {'6': 0.0}}, {'festival_meters': {'6': 0.0}}))  # never charged
        self.assertEqual(A.event_over({'timed_effects': [{'guid': 9, 'name': 'Games'}]}, {}), 'the event Games')
        s, p = self.session(pop=900), self.session(pop=1000)
        p['areas'][0]['festival_active'] = 'Feast'
        rep = A.report_data([s], [p], {41})
        pop = [a for a in rep['alarms'] if a['cat'] == 'population'][0]
        self.assertEqual((pop['level'], pop['text']), ('info', 'population fell from 1,000 to 900 after the festival (Feast): temporary'))

    def test_boost_advice(self):
        t = lambda net, loaded=0: {'net_per_minute': net, 'outflow_per_minute': 1.0, 'inflow_per_minute': 0.0,
                                   'max': 30, 'minutes': 20, 'loaded': loaded, 'unloaded': 0}
        bakery = {'building': 'Bakery', 'count': 2, 'productivity': 1.0, 'output_per_minute': {'Bread': 1.0},
                  'capacity_per_minute': {'Bread': 1.0}}
        e = {'production': [bakery], 'stock_trends': {'Bread': t(-1.5)}, 'supplies': {}, 'receives': {}}
        b = A.island_balance(e)['Bread']
        self.assertEqual(round(b['resid'], 2), 2.5)                      # 1.0 made + 1.5 drawn from stock
        self.assertEqual(A.boost_advice(b, e, {}), ('build', 'build 3 more Bakery (+1.5/min)'))
        e['stock_trends']['Bread'] = t(-1.5, loaded=40)                  # ships take 2/min: the route is the cause
        e['supplies'] = {'Bread': ['Capua']}
        self.assertEqual(A.boost_advice(A.island_balance(e)['Bread'], e, {})[0], 'route')
        e['production'] = [dict(bakery, productivity=0.25, output_per_minute={'Bread': 0.25})]
        e['stock_trends']['Bread'] = t(-0.6)                             # running at 100% would cover it
        self.assertEqual(A.boost_advice(A.island_balance(e)['Bread'], e, {})[0], 'fix')
        e['storage'] = {'Bread': 400}                                    # lasts 11 hours: no advice
        self.assertIsNone(A.boost_advice(A.island_balance(e)['Bread'], e, {}))
        e = {'production': [], 'stock_trends': {'Fish': t(-0.5)}, 'receives': {}}
        self.assertEqual(A.boost_advice(A.island_balance(e)['Fish'], e, {'Fish': [('Ostia', 0.8)]}),
                         ('source', 'nothing brings it: add a route from Ostia (+0.8/min)'))

    def test_running_dry_and_pirates(self):
        s = self.session()
        s['participants'].append({'participant': 60, 'name': 'Raiders', 'warships': 4, 'civil_ships': 0, 'troops': 0,
                                  'units': [], 'ships': []})
        s['participant_kinds'] = {'60': 'pirate'}
        rep = A.report_data([s], None, {41})
        dr = rep['drains'][0]
        self.assertEqual((dr['good'], round(dr['mins']), dr['alarm']['level'], dr['alarm']['cat']), ('Bread', 10, 'high', 'stock'))
        self.assertEqual(rep['hostile'], ['Raiders'])
        self.assertIn(('Raiders', 4, False, True), rep['sections'][-1]['summary']['sea'])
        page = A.render_html(rep, {})
        for needle in ('id="scale"', 'data-good="Bread"', '<span class="hostile">Raiders', '<tr class="hostile">', 'Ledgers'):
            self.assertIn(needle, page)


if __name__ == '__main__':
    unittest.main()


class TestReportRoutes(unittest.TestCase):
    def test_same_name_routes_get_their_own_card(self):
        area = {'area': 7, 'owner': 41, 'city': 'Roma'}
        rt = lambda i: {'id': i, 'name': 'Same', 'owner': 41, 'ships': [], 'stations': [
            {'area': 7, 'session': 'x', 'city': 'Roma', 'goods': {}}]}
        r = {'session': 'x', 'participants': [], 'areas': [area], 'trade_routes': [rt(1), rt(2)]}
        rep = A.report_data([r], None, {41})
        self.assertEqual(len([g for g in rep['groups'] if g['kind'] == 'route']), 2)

    def test_foreign_stop_is_not_called_dry(self):
        r = {'session': 'x', 'participants': [], 'trade_routes': [{'id': 1, 'name': 'Beer', 'owner': 41, 'ships': [9],
             'stations': [{'area': 7, 'session': 'x', 'city': 'Roma', 'goods': {'Beer': 0}},
                          {'area': 8, 'session': 'x', 'city': 'Elsewhere', 'goods': {'Beer': 0}}]}],
             'areas': [{'area': 7, 'owner': 41, 'city': 'Roma'}, {'area': 8, 'owner': 99, 'city': 'Elsewhere'}]}
        rep = A.report_data([r], None, {41})
        self.assertFalse(any('not produced' in a['text'] for a in rep['alarms']))

    def test_npc_traders_are_left_out(self):
        self.assertEqual(A.participant_kind(5, {5: {'template': 'Participant 3rdParty'}}, set()), 'npc')
        self.assertEqual(A.participant_kind(6, {6: {'template': 'Participant 3rdParty Pirate'}}, set()), 'pirate')
        self.assertEqual(A.participant_kind(7, {7: {'template': 'Participant 2ndParty (Rival)'}}, set()), 'rival')
        r = {'session': 'x', 'participant_kinds': {'50': 'npc', '60': 'rival'}, 'trade_routes': [],
             'participants': [{'participant': 50, 'name': 'Trader', 'ship_count': 8, 'warships': 8, 'civil_ships': 0,
                               'troops': 0, 'units': [], 'ships': [{'type': 'Trireme', 'guid': 1, 'count': 8}]},
                              {'participant': 60, 'name': 'Rival', 'ship_count': 2, 'warships': 2, 'civil_ships': 0,
                               'troops': 0, 'units': [], 'ships': [{'type': 'Trireme', 'guid': 1, 'count': 2}]}],
             'areas': [{'area': 1, 'owner': 50, 'owner_name': 'Trader'}, {'area': 2, 'owner': 60, 'owner_name': 'Rival'}]}
        page = A.render_md(A.report_data([r], None, {41}))
        self.assertNotIn('Trader', page)
        self.assertIn('Rival', page)


class TestIslandView(unittest.TestCase):
    def test_needs_and_flows(self):
        f32 = lambda v: struct.pack('<f', v)
        n = lambda name, value=None, kids=(): setattr(x := A.Node(name, value), 'children', list(kids)) or x
        house = n('t1', None, [n('guid', struct.pack('<I', 30)), n('Building'),
                               n('Residence7', None, [n('ResidentCount', f32(10))])])
        info = {30: {'needs': [{'need': '40', 'rate': '0.02', 'product': '50', 'category': 'Food'},
                               {'need': '41', 'product': '51', 'category': 'Public'}]}}
        names = {30: 'House', 50: 'Fish', 51: 'Market', 52: 'Bread'}
        res = A.economy([house], lambda g: names.get(g, str(g)), info)
        self.assertEqual(res['needs'], {'Fish': {'category': 'Food', 'houses': ['House'], 'kind': 'good'},
                                        'Market': {'category': 'Public', 'houses': ['House'], 'kind': 'service'}})
        area = {'area': 1, 'owner': 41, 'city': 'Roma', 'population': 10, 'needs': res['needs'],
                'storage': {}, 'receives': {'Fish': ['Ostia']}, 'supplies': {'Bread': ['Capua']},
                'production': [{'building': 'Bakery', 'count': 4, 'idle_output_full': 3,
                                'capacity_per_minute': {'Bread': 2.0}}]}
        isl = A.island_views([{'session': 'x', 'areas': [area]}], None, {41}, [])[0]
        t = {x['title']: x for x in isl['tables']}
        self.assertEqual(isl['label'], 'Roma (x)')
        rows = {r[0]: r for r in t["Residents' needs"]['rows']}
        self.assertEqual(rows['Fish'][1], 'out of stock')          # shipped in, but none left
        self.assertEqual(rows['Market'][1], 'not built')
        self.assertEqual(t['Overproduced']['rows'][0][1], ('3 / 4', 3))
        self.assertEqual(t['Exported']['rows'][0][-1], 'Capua')
        self.assertEqual(t['Imported']['rows'][0][-1], 'Ostia')
        rep = {'save': 's', 'previous': None, 'alarms': [], 'groups': [], 'sections': [], 'islands': [isl]}
        page = A.render_html(rep, {})
        self.assertIn('<select id="isl"', page)
        self.assertIn('data-id="x-1"', page)
        self.assertIn('### Roma (x)', A.render_md(rep))
