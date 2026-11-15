#!/usr/bin/env python3
"""
anno117.py - read Anno 117: Pax Romana save games and game data. Python 3.8+, no dependencies.

Commands:
  extract <save.a8s> [-o DIR] [--names FILE ...]
        One JSON file per session (province): ships with modules and positions, land units
        and pirate factions, grouped by owner (participant).
  assets <path> [-o FILE] [--lang LANG] [--all]
        Find assets*.xml and texts_<LANG>.xml inside the game's .rda archives (or in a folder
        of already-extracted .xml files) and write a GUID -> name map (default guid_names.json).
        By default only military/naval/participant/session assets are kept; --all keeps everything.
  list <file.rda | dir> [FILTER]
        List the files contained in .rda archives (handy when `assets` finds nothing).
  guid-table <guid_names.json> [-o FILE]
        Render the GUID map as a Markdown table (e.g. for the GitHub Pages site).

The save layout was reverse-engineered by comparing with the Anno 1800 formats documented by
the anno-mods community (RDAExplorer, FileDBReader). See docs/ for details.
"""
import argparse, collections, io, json, math, mmap, os, re, struct, sys, zlib
import xml.etree.ElementTree as ET

# ---------------- RDA v2.2 ----------------
# Block flags (as in RDAExplorer): 1 = compressed, 2 = encrypted, 4 = memory-resident, 8 = deleted.
def rda_entries(path):
    """Read only the index of an RDA v2.2 archive (works on multi-GB files via mmap).
    Returns [(name, getter)]; file data is read and decompressed only when getter() is called."""
    f = open(path, 'rb')
    d = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    if d[:18] != b'Resource File V2.2':
        raise ValueError(f'{path}: unrecognised format (expected RDA V2.2)')
    off = struct.unpack_from('<Q', d, 784)[0]
    out, seen = [], set()
    while 0 < off < len(d) and off not in seen:
        seen.add(off)
        flags, n, dsz, _dusz, nxt = struct.unpack_from('<IIQQQ', d, off)
        if flags & 8 or n == 0:          # deleted or empty block
            off = nxt; continue
        if flags & 2:
            print(f'  ! {os.path.basename(path)}: encrypted block at {off}, skipped', file=sys.stderr)
            off = nxt; continue
        raw = d[off - dsz: off]
        if flags & 1:
            raw = zlib.decompress(raw)
        resident = None
        if flags & 4:
            # memory-resident: two uint64 (compressed size, real size) sit right before the directory,
            # preceded by a single data blob holding every file of the block.
            csz, _rsz = struct.unpack_from('<QQ', d, off - dsz - 16)
            resident = (off - dsz - 16 - csz, csz, bool(flags & 1))
        cache = {}
        for i in range(n):
            e = raw[i * 560:(i + 1) * 560]
            name = e[:520].decode('utf-16le', 'replace').split('\x00')[0]
            fo, comp, size, _ts, _u = struct.unpack_from('<QQQQQ', e, 520)
            if resident:
                def get(fo=fo, size=size, r=resident, cache=cache):
                    if 'buf' not in cache:
                        b = d[r[0]: r[0] + r[1]]
                        cache['buf'] = zlib.decompress(b) if r[2] else b
                    return cache['buf'][fo: fo + size]
            else:
                def get(fo=fo, comp=comp, z=bool(flags & 1)):
                    b = d[fo: fo + comp]
                    return zlib.decompress(b) if z else b
            out.append((name, get))
        off = nxt
    return out

def rda_files(path):
    """All files of the archive in memory (fine for save games, which are small)."""
    try:
        return {n: get() for n, get in rda_entries(path)}
    except ValueError as e:
        raise SystemExit(str(e))

# ---------------- FileDB v2 ----------------
class Node:
    __slots__ = ('name', 'children', 'value')
    def __init__(s, name, value=None):
        s.name, s.children, s.value = name, [], value

def filedb(d):
    tagoff, attoff = struct.unpack_from('<II', d, len(d) - 16)
    def rd(off):
        n = struct.unpack_from('<I', d, off)[0]; off += 4
        ids = struct.unpack_from('<%dH' % n, d, off); off += 2 * n
        names = []
        for _ in range(n):
            e = d.index(b'\0', off); names.append(d[off:e].decode('utf-8', 'replace')); off = e + 1
        return dict(zip(ids, names))
    tags, attrs = rd(tagoff), rd(attoff)
    root = Node('root'); stack = [root]; off = 0
    while off < tagoff:
        size, i = struct.unpack_from('<iI', d, off); off += 8
        if i == 0:
            if len(stack) > 1: stack.pop()
        elif i >= 0x8000:
            stack[-1].children.append(Node(attrs.get(i, 'a%x' % i), d[off:off + size]))
            off += (size + 7) // 8 * 8
        else:
            n = Node(tags.get(i, 't%x' % i)); stack[-1].children.append(n); stack.append(n)
    return root

def ch(n, name): return [c for c in n.children if c.name == name]
def one(n, name):
    for c in n.children:
        if c.name == name: return c
def u(v): return int.from_bytes(v, 'little')

def ch(n, name): return [c for c in n.children if c.name == name]
def one(n, name):
    for c in n.children:
        if c.name == name: return c
def u(v): return int.from_bytes(v, 'little')

# ---------------- save extraction ----------------
# Session GUIDs observed so far (used when no name map is available).
KNOWN_SESSIONS = {3245: 'latium', 6627: 'albion', 149679: 'nile'}

def load_names(paths):
    """Merge GUID name maps; later files override earlier ones. Values may be plain strings
    (hand-written maps) or objects as written by `assets`."""
    m = {}
    for path in paths:
        if not os.path.exists(path):
            print(f'  ! name map {path} not found, skipped', file=sys.stderr); continue
        for k, v in json.load(open(path, encoding='utf-8')).items():
            if k.isdigit():
                v = (v.get('name_local') or v.get('name')) if isinstance(v, dict) else v
                if v: m[int(k)] = v
    return m

def cluster(points, radius=40):
    """Greedy distance clustering: ships closer than `radius` count as one squadron."""
    groups = []
    for p in points:
        for gr in groups:
            if math.dist(gr['c'], p) < radius:
                gr['p'].append(p)
                gr['c'] = [sum(x) / len(gr['p']) for x in zip(*gr['p'])]
                break
        else:
            groups.append({'c': list(p), 'p': [p]})
    return [{'position': [round(c) for c in g['c']], 'ships': len(g['p'])} for g in groups]

def meta_map(data_root):
    """MetaID -> participant GUID, and participant GUID -> custom company name."""
    mom = one(one(data_root.children[0], 'MetaGameObjectManager'), 'MetaObjectMap')
    owners, names = {}, {}
    for e in mom.children:
        i, p = one(e, 'ID'), one(e, 'ParticipantGUID')
        if i is not None and p is not None:
            owners[u(i.value)] = u(p.value)
        mp = one(e, 'MetaParticipant')
        if mp is not None and p is not None and one(mp, 'CustomCompanyName') is not None:
            v = [c.value for c in one(mp, 'CustomCompanyName').children]
            if len(v) > 1: names[u(p.value)] = v[1].decode('utf-16le', 'replace')
    return owners, names

def session(sroot, owners, names, gmap, radius):
    name = lambda g: gmap.get(g, f'GUID {g}')
    ships = collections.defaultdict(lambda: collections.defaultdict(list))
    units = collections.defaultdict(collections.Counter)
    pirates = []
    am = one(one(sroot, 'GameSessionManager'), 'AreaManagers')
    for area in am.children:
        aom = one(area, 'AreaObjectManager')
        go = one(aom, 'GameObject') if aom else None
        objs = one(go, 'objects') if go else None
        if not objs: continue
        for ob in objs.children:
            g = u(one(ob, 'guid').value)
            p = one(ob, 'Pirate')
            if p is not None:
                c = struct.unpack('<ff', one(one(p, 'AreaOfActivity'), 'ptCenter').value)
                mw, k = one(p, 'MaxEverReachedWarships'), one(p, 'Kontor')
                pirates.append({'participant': g, 'name': name(g),
                                'activity_center': [round(x) for x in c],
                                'max_warships_reached': u(mw.value) if mw else None,
                                'base_object_id': one(k, 'ObjectID').value.hex() if k else None})
            mp = one(ob, 'MetaPersistent')
            mid = one(mp, 'MetaID') if mp is not None else None
            owner = owners.get(u(mid.value)) if mid is not None else None
            pos = struct.unpack('<fff', one(ob, 'Position').value) if one(ob, 'Position') else (0, 0, 0)
            smo = one(ob, 'ShipModuleOwner')
            if smo is not None:
                mods = sorted(u(m.value) for ism in ch(smo, 'InstalledShipModules')
                              for t in ism.children for m in ch(t, 'ShipModuleGuid'))
                ships[owner][g].append({'modules': [name(m) for m in mods],
                                        'module_guids': mods,
                                        'position': [round(pos[0]), round(pos[2])]})
            elif one(ob, 'Unit') is not None:
                units[owner][g] += 1
    parts = []
    for owner in sorted(set(ships) | set(units), key=lambda x: (x is None, x or 0)):
        sh = ships.get(owner, {})
        parts.append({
            'participant': owner,
            'name': names.get(owner) or (gmap.get(owner) if owner is not None else 'unknown owner'),
            'ship_count': sum(len(v) for v in sh.values()),
            'ships': [{'type': name(g), 'guid': g, 'count': len(v), 'ships': v} for g, v in sorted(sh.items())],
            'squadrons': cluster([tuple(s['position']) for v in sh.values() for s in v], radius),
            'units': [{'type': name(g), 'guid': g, 'count': n} for g, n in sorted(units.get(owner, {}).items())],
        })
    return {'participants': parts, 'pirates': pirates}

def cmd_extract(a):
    os.makedirs(a.output, exist_ok=True)
    gmap = load_names(a.names if a.names else ['guid_names.json'] if os.path.exists('guid_names.json') else [])
    files = rda_files(a.save)
    data_root = filedb(zlib.decompress(files['data.a7s']))
    owners, names = meta_map(data_root)
    for s in one(data_root.children[0], 'GameSessions').children:
        sd, desc = one(s, 'SessionData'), one(s, 'SessionDesc')
        if sd is None: continue
        sg = u(one(desc, 'SessionGUID').value)
        label = KNOWN_SESSIONS.get(sg) or re.sub(r'\W+', '_', gmap.get(sg, f'session_{sg}')).lower()
        res = {'session': label, 'session_guid': sg, 'save': os.path.basename(a.save),
               **session(filedb(one(sd, 'BinaryData').value), owners, names, gmap, a.radius)}
        p = os.path.join(a.output, label + '.json')
        json.dump(res, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print(f'{label}: {sum(x["ship_count"] for x in res["participants"])} ships, '
              f'{len(res["participants"])} owners, {len(res["pirates"])} pirate factions -> {p}')

# ---------------- game data (assets) ----------------
# assets.xml = <Asset><Template/><Values><Standard><GUID/><Name/>...</Standard><Text><OasisId/></Text>...
# Anno 117: texts_<language>.xml = <Text><LineId/><Text/></Text>, where LineId = the asset's OasisId.
# Anno 1800 (same engine): <Text><GUID/><Text/></Text>. Files are matched by name, wherever they are.
RE_ASSETS = re.compile(r'(^|/)assets[^/]*\.xml$', re.I)
RE_USEFUL = re.compile(r'ship|module|unit|soldier|troop|military|legion|warship|pirate|participant|'
                       r'session|weapon|siege|onager|fleet|army|naval|galley|boat', re.I)

def find_rda(path):
    if os.path.isfile(path):
        return [path]
    return sorted(os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs if f.lower().endswith('.rda'))

def collect_xml(path, dest, lang):
    re_texts = re.compile(r'(^|/)texts?_%s[^/]*\.xml$' % re.escape(lang), re.I)
    found = {'assets': [], 'texts': []}
    def kind(n):
        n = n.replace('\\', '/')
        return 'assets' if RE_ASSETS.search(n) else 'texts' if re_texts.search(n) else None
    if os.path.isdir(path):   # already-extracted .xml files (e.g. with RDAExplorer)
        for r, _, fs in os.walk(path):
            for f in fs:
                k = kind(os.path.join(r, f))
                if k: found[k].append(os.path.join(r, f))
    rdas = find_rda(path)
    if not rdas and not any(found.values()):
        sys.exit(f'No .rda or useful .xml files in {path}')
    for rda in rdas:
        try:
            entries = rda_entries(rda)
        except Exception as e:
            print(f'  ! {rda}: {e}', file=sys.stderr); continue
        for n, get in entries:
            k = kind(n)
            if not k: continue
            out = os.path.join(dest, os.path.splitext(os.path.basename(rda))[0], *n.replace('\\', '/').split('/'))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            try:
                data = get()
            except Exception as e:
                print(f'  ! {n} in {rda}: extraction failed ({e})', file=sys.stderr); continue
            open(out, 'wb').write(data)
            print(f'  + {n}  ({len(data) / 1e6:.1f} MB, from {os.path.basename(rda)})')
            found[k].append(out)
    return found

def txt(el, tag):
    x = el.find(tag) if el is not None else None
    return x.text.strip() if x is not None and x.text else None

def read_assets(files):
    assets = {}
    for f in files:
        n0 = len(assets)
        try:
            for _, el in ET.iterparse(f, events=('end',)):
                if el.tag != 'Asset': continue
                std = el.find('Values/Standard')
                g = txt(std, 'GUID')
                if g and g.isdigit():
                    a = {'name': txt(std, 'Name'), 'template': txt(el, 'Template'), 'base': txt(el, 'BaseAssetGUID'),
                         'text_id': txt(el, 'Values/Text/OasisId')}   # Anno 117: id of the name in texts_*.xml
                    assets[int(g)] = {k: v for k, v in a.items() if v}
                el.clear()
        except ET.ParseError as e:
            print(f'  ! {f}: invalid XML ({e})', file=sys.stderr)
        print(f'  {os.path.basename(f)}: {len(assets) - n0} assets')
    return assets

def read_texts(files):
    t = {}
    for f in files:
        try:
            for _, el in ET.iterparse(f, events=('end',)):
                if el.tag == 'Text':
                    # Anno 117: <Text><LineId/><Text/></Text>, Anno 1800: <Text><GUID/><Text/></Text>
                    g, v = txt(el, 'LineId') or txt(el, 'GUID'), txt(el, 'Text')
                    if g and g.lstrip('-').isdigit():
                        if v: t[int(g)] = v
                        el.clear()
        except ET.ParseError as e:
            print(f'  ! {f}: invalid XML ({e})', file=sys.stderr)
    return t

def cmd_assets(a):
    print(f'Searching {a.path} for assets and {a.lang} texts ...')
    xml = collect_xml(a.path, a.extract_dir, a.lang)
    if not xml['assets']:
        sys.exit('No assets*.xml found. Try: anno117.py list <dir> xml')
    if not xml['texts']:
        print(f'  ! no texts_{a.lang}*.xml found: only internal names will be available', file=sys.stderr)
    assets, texts = read_assets(xml['assets']), read_texts(xml['texts'])

    def inherited(g, key, depth=0):   # assets with a BaseAssetGUID inherit missing values from the base
        x = assets.get(g, {})
        if x.get(key) or depth > 20 or not x.get('base'): return x.get(key)
        return inherited(int(x['base']), key, depth + 1)

    out = {}
    for g, x in assets.items():
        x = {k: v for k, v in x.items() if k not in ('base', 'text_id')}
        tid = inherited(g, 'text_id')
        local = texts.get(int(tid)) if tid and tid.lstrip('-').isdigit() else texts.get(g)
        if local: x['name_local'] = local
        if inherited(g, 'template'): x['template'] = inherited(g, 'template')
        if a.all or RE_USEFUL.search(x.get('template') or '') or RE_USEFUL.search(x.get('name') or ''):
            out[str(g)] = x
    out = dict(sorted(out.items(), key=lambda kv: int(kv[0])))
    json.dump(out, open(a.output, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n{len(out)} GUIDs written to {a.output} ({len(assets)} assets, {len(texts)} {a.lang} texts)')

def cmd_list(a):
    f = (a.filter or '').lower()
    for rda in find_rda(a.path):
        try:
            for n, _ in rda_entries(rda):
                if f in n.lower(): print(f'{os.path.basename(rda)}: {n}')
        except Exception as e:
            print(f'  ! {rda}: {e}', file=sys.stderr)

def cmd_guid_table(a):
    m = json.load(open(a.names, encoding='utf-8'))
    esc = lambda s: (s or '').replace('|', '\\|')
    rows = sorted(((int(k), v) for k, v in m.items() if k.isdigit()),
                  key=lambda kv: ((kv[1].get('template') or '~') if isinstance(kv[1], dict) else '~', kv[0]))
    lines = ['---', 'title: GUID reference', '---', '', '# GUID reference', '',
             f'{len(rows)} GUIDs, generated with `anno117.py guid-table`. '
             'Names come from the game files of the person who generated this page.', '',
             '| GUID | Name | Local name | Template |', '|---:|---|---|---|']
    for g, v in rows:
        v = v if isinstance(v, dict) else {'name': v}
        lines.append(f'| {g} | {esc(v.get("name"))} | {esc(v.get("name_local"))} | {esc(v.get("template"))} |')
    open(a.output, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print(f'{len(rows)} rows -> {a.output}')

def main():
    ap = argparse.ArgumentParser(description='Anno 117: Pax Romana save game and game data tools.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('extract', help='save game -> one JSON per session')
    e.add_argument('save'); e.add_argument('-o', '--output', default='sessions')
    e.add_argument('--names', action='append', metavar='FILE',
                   help='GUID name map; repeatable, later files win (default: ./guid_names.json if present)')
    e.add_argument('--radius', type=float, default=40, help='squadron clustering radius (default 40)')
    s = sub.add_parser('assets', help='game files -> GUID name map')
    s.add_argument('path', help='Anno 117 "maindata" folder, a single .rda (config.rda is enough), or a folder of extracted .xml')
    s.add_argument('-o', '--output', default='guid_names.json')
    s.add_argument('--lang', default='english', help='text language, as in texts_<lang>.xml (default english)')
    s.add_argument('--all', action='store_true', help='keep every asset, not only military/naval ones')
    s.add_argument('--extract-dir', default='extracted', help='where to save the extracted .xml files')
    l = sub.add_parser('list', help='list files inside .rda archives')
    l.add_argument('path'); l.add_argument('filter', nargs='?')
    t = sub.add_parser('guid-table', help='GUID map -> Markdown table')
    t.add_argument('names'); t.add_argument('-o', '--output', default='docs/guids.md')
    a = ap.parse_args()
    {'extract': cmd_extract, 'assets': cmd_assets, 'list': cmd_list, 'guid-table': cmd_guid_table}[a.cmd](a)

if __name__ == '__main__':
    main()
