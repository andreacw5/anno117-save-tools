#!/usr/bin/env python3
"""
anno117.py - read Anno 117: Pax Romana save games and game data. Python 3.8+, no dependencies.

Commands:
  extract <save.a8s> [-o DIR] [--names FILE ...] [--texts FILE ...]
        One JSON file per session (province): ships with modules and positions, land units
        and pirate factions, grouped by owner (participant), plus every area (island) with its
        owner, fertilities, warehouse, buildings, population, production per minute, income,
        city status, trade routes and city name.
  assets <path> [-o FILE] [--lang LANG] [--all]
        Find assets*.xml and texts_<LANG>.xml inside the game's .rda archives (or in a folder
        of already-extracted .xml files) and write a GUID -> name map (default guid_names.json).
        By default only military/naval/participant/session assets are kept; --all keeps everything.
  map <save.a8s> <maindata> [--session S] [-o FILE]
        PNG map of a province: the game's map tiles placed as in the save's MapTemplate, plus
        buildings, ships and units coloured by owner. Needs Pillow (the only optional dependency).
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

def user_texts(data_root):
    """Texts typed by players (city, ship and army names), from MetaUserGeneratedTextManager."""
    m = one(data_root.children[0], 'MetaUserGeneratedTextManager')
    lst = one(m, 'UserGeneratedTexts') if m is not None else None
    out = []
    for v in (lst.children[1::2] if lst is not None else []):
        t = one(v, 'DisplayText')
        if t is not None: out.append(t.value.decode('utf-16le', 'replace').rstrip('\x00'))
    return out

RE_ROUTE_CODE = re.compile(r'\b([A-Z][a-z]{2}) - ([A-Z][a-z]{2})$')

def name_cities(all_areas, utexts, humans):
    """Trade route names end with 'Abc - Xyz', the first three letters of the two cities' names, and
    each area records the routes that served it (routes may cross provinces). A code goes to the
    area found in every route that uses it, by elimination, and is kept only when confirmed:
    the area's default city name starts with it, or the area belongs to a human player and exactly
    one player-typed text starts with it (a renamed city)."""
    users = collections.defaultdict(list)                     # code -> [areas of each route]
    by_route = collections.defaultdict(set)
    for e in all_areas:
        for r in e.get('trade_routes', {}): by_route[r].add(id(e))
    for r, ids in by_route.items():
        m = RE_ROUTE_CODE.search(r)
        if m:
            for code in set(m.groups()): users[code].append(ids)
    cand = {c: set.intersection(*v) for c, v in users.items()}
    byid = {id(e): e for e in all_areas}
    defaults = {e['city'][:3] for e in all_areas if e.get('city')}
    assigned = {}
    for _ in range(len(cand) + 1):
        for c, s in cand.items():
            if c in assigned: continue
            s = s - set(assigned.values())
            own = [i for i in s if (byid[i].get('city') or '').startswith(c)]
            if len(own) == 1 or len(s) == 1: assigned[c] = (own or list(s))[0]
    for c, i in assigned.items():
        e = byid[i]
        if (e.get('city') or '').startswith(c):
            e['route_code'] = c
        elif e['owner'] in humans and c not in defaults:
            hits = sorted({t for t in utexts if t.startswith(c)})
            if hits:
                e['route_code'] = c
                if len(hits) == 1: e['custom_name'] = hits[0]
                else: e['custom_name_candidates'] = hits

def storage(b):
    """Decode a StrgLrg value: N good GUIDs then N amounts, all uint32. The first GUID is
    absolute, each following one is stored as the number of GUIDs skipped since the previous."""
    w = struct.unpack('<%dI' % (len(b) // 4), b[:len(b) // 4 * 4])
    n = len(w) // 2
    if not n or len(w) != 2 * n: return {}
    keys, g = [], None
    for x in w[:n]:
        g = x if g is None else g + x + 1
        keys.append(g)
    return dict(zip(keys, w[n:]))

def load_info(paths):
    """GUID -> asset record (template, factory...) from maps written by `assets`."""
    m = {}
    for path in paths:
        if os.path.exists(path):
            for k, v in json.load(open(path, encoding='utf-8')).items():
                if k.isdigit() and isinstance(v, dict): m[int(k)] = v
    return m

def f32(n):
    return struct.unpack('<f', n.value)[0] if n is not None and n.value is not None and len(n.value) == 4 else None

def economy(objs, name, info):
    """Buildings, residents and production of one area's objects. Production per minute is
    amount x 60 / cycle time x current productivity (Factory7/CurrentProductivity, 1.0 = 100%)."""
    buildings, residents = collections.Counter(), collections.Counter()
    fac = collections.defaultdict(lambda: {'count': 0, 'working': 0, 'prod': 0.0, 'out': collections.Counter(),
                                           'in': collections.Counter()})
    for ob in objs:
        if one(ob, 'Building') is None: continue
        g = u(one(ob, 'guid').value)
        buildings[g] += 1
        r = one(ob, 'Residence7')
        if r is not None: residents[g] += f32(one(r, 'ResidentCount')) or 0
        f = one(ob, 'Factory7')
        if f is None: continue
        prod = f32(one(f, 'CurrentProductivity')) or 0.0
        ps = one(f, 'ProductionState')
        x = fac[g]; x['count'] += 1; x['prod'] += prod
        if ps is not None and one(ps, 'InProgress') is not None: x['working'] += 1
        spec = (info.get(g) or {}).get('factory') or {}
        cyc = float(spec.get('cycle') or 0)
        if cyc:
            for k, d in (('out', 'out'), ('in', 'in')):
                for good, amt in spec.get(k, {}).items(): x[d][int(good)] += amt * 60 / cyc * prod
    res = {}
    if buildings: res['buildings'] = {name(g): n for g, n in buildings.most_common()}
    if residents:
        res['population'] = round(sum(residents.values()))
        res['residents'] = {name(g): round(n) for g, n in residents.most_common()}
    if fac:
        rows, net = [], collections.Counter()
        for g, x in sorted(fac.items(), key=lambda kv: -kv[1]['count']):
            row = {'building': name(g), 'guid': g, 'count': x['count'], 'working': x['working'],
                   'productivity': round(x['prod'] / x['count'], 2)}
            if x['out']: row['output_per_minute'] = {name(k): round(v, 2) for k, v in x['out'].items()}
            if x['in']: row['input_per_minute'] = {name(k): round(v, 2) for k, v in x['in'].items()}
            rows.append(row)
            net.update(x['out']); net.subtract(x['in'])
        res['production'] = rows
        res['goods_per_minute'] = {name(k): round(v, 2) for k, v in sorted(net.items(), key=lambda kv: -kv[1]) if abs(v) > 0.005}
    return res

def areas(sroot, names, gmap, texts, ainfo=None):
    """One entry per area (island or Nile district): owner, default city name, fertilities and
    deposits, income, status, extent of its buildings, trade routes and the city's route code."""
    gsm = one(sroot, 'GameSessionManager')
    info, managers = one(gsm, 'AreaInfo'), one(gsm, 'AreaManagers')
    if info is None: return []
    name = lambda g: gmap.get(g, f'GUID {g}')
    out = []
    for k, v in zip(info.children[0::2], info.children[1::2]):
        aid = u(k.value)
        o, cn, fert = one(v, 'OwnerProfile'), one(v, 'CityNameGuid'), one(v, 'Fertility')
        owner = u(o.value) if o is not None else None
        rs = collections.Counter()
        def walk(n):
            for c in n.children:
                if c.name == 'RouteName': rs[c.value.decode('utf-16le', 'replace').split('\x00')[0]] += 1
                walk(c)
        walk(one(v, 'PassiveTrade') or Node('x'))
        e = {'area': aid, 'owner': owner,
             'owner_name': names.get(owner) or (gmap.get(owner) if owner is not None else None),
             'city': texts.get(struct.unpack('<q', cn.value)[0]) if cn is not None and len(cn.value) == 8 else None,
             'fertilities': [name(u(c.value)) for c in fert.children] if fert is not None else [],
             'fertility_guids': [u(c.value) for c in fert.children] if fert is not None else []}
        am = one(managers, f'AreaManager_{aid}') if managers is not None else None
        if am is not None:
            mm = one(am, 'AreaMoneyManager')
            inc = one(mm, 'TotalMoneyIncomePerMinute') if mm is not None else None
            if inc is not None: e['income_per_minute'] = round(struct.unpack('<f', inc.value)[0])
            pm = one(am, 'AreaPopulationManager')
            st = one(pm, 'CurrentCityStatusGUID') if pm is not None else None
            if st is not None: e['city_status'] = name(u(st.value))
            aom = one(am, 'AreaObjectManager'); go = one(aom, 'GameObject') if aom is not None else None
            objs = one(go, 'objects') if go is not None else None
            ps = [struct.unpack('<fff', one(ob, 'Position').value) for ob in (objs.children if objs else [])
                  if one(ob, 'Position') is not None]
            e.update(economy(objs.children if objs else [], name, ainfo or {}))
            if ps:
                xs, zs = [p[0] for p in ps], [p[2] for p in ps]
                e['objects'] = len(ps)
                e['center'] = [round(sum(xs) / len(xs)), round(sum(zs) / len(zs))]
                e['extent'] = [round(min(xs)), round(min(zs)), round(max(xs)), round(max(zs))]
        eco = one(v, 'AreaEconomy')
        sl = one(eco, 'StrgLrg') if eco is not None else None
        if sl is not None:
            goods = sorted(((g, n) for g, n in storage(sl.value).items() if n), key=lambda x: -x[1])
            e['storage'] = {name(g): n for g, n in goods}
            e['storage_guids'] = {str(g): n for g, n in goods}
        if rs: e['trade_routes'] = dict(rs.most_common())
        out.append(e)
    return out

def cmd_extract(a):
    os.makedirs(a.output, exist_ok=True)
    paths = a.names if a.names else ['guid_names.json'] if os.path.exists('guid_names.json') else []
    gmap, info = load_names(paths), load_info(paths)
    files = rda_files(a.save)
    data_root = filedb(zlib.decompress(files['data.a7s']))
    owners, names = meta_map(data_root)
    utexts = user_texts(data_root)
    texts = read_texts(a.texts) if a.texts else {}   # resolves the areas' default city names
    results = []
    for s in one(data_root.children[0], 'GameSessions').children:
        sd, desc = one(s, 'SessionData'), one(s, 'SessionDesc')
        if sd is None: continue
        sg = u(one(desc, 'SessionGUID').value)
        label = KNOWN_SESSIONS.get(sg) or re.sub(r'\W+', '_', gmap.get(sg, f'session_{sg}')).lower()
        sroot = filedb(one(sd, 'BinaryData').value)
        results.append({'session': label, 'session_guid': sg, 'save': os.path.basename(a.save),
                        **session(sroot, owners, names, gmap, a.radius),
                        'areas': areas(sroot, names, gmap, texts, info)})
    # trade routes cross provinces, so cities are matched to route codes over the whole save
    name_cities([e for r in results for e in r['areas']], utexts, {p for p, n in names.items() if n})
    for res in results:
        label = res['session']
        p = os.path.join(a.output, label + '.json')
        json.dump(res, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        print(f'{label}: {sum(x["ship_count"] for x in res["participants"])} ships, '
              f'{len(res["participants"])} owners, {len(res["pirates"])} pirate factions, '
              f'{len(res["areas"])} areas -> {p}')

# ---------------- game data (assets) ----------------
# assets.xml = <Asset><Template/><Values><Standard><GUID/><Name/>...</Standard><Text><OasisId/></Text>...
# Anno 117: texts_<language>.xml = <Text><LineId/><Text/></Text>, where LineId = the asset's OasisId.
# Anno 1800 (same engine): <Text><GUID/><Text/></Text>. Files are matched by name, wherever they are.
RE_ASSETS = re.compile(r'(^|/)assets[^/]*\.xml$', re.I)
RE_PROPS = re.compile(r'(^|/)export/properties\.xml$', re.I)   # default values of components
RE_USEFUL = re.compile(r'ship|module|unit|soldier|troop|military|legion|warship|pirate|participant|'
                       r'session|weapon|siege|onager|fleet|army|naval|galley|boat|fertility|deposit|'
                       r'citystatus', re.I)

def find_rda(path):
    if os.path.isfile(path):
        return [path]
    return sorted(os.path.join(r, f) for r, _, fs in os.walk(path) for f in fs if f.lower().endswith('.rda'))

def collect_xml(path, dest, lang):
    re_texts = re.compile(r'(^|/)texts?_%s[^/]*\.xml$' % re.escape(lang), re.I)
    found = {'assets': [], 'texts': [], 'props': []}
    def kind(n):
        n = n.replace('\\', '/')
        return ('assets' if RE_ASSETS.search(n) else 'texts' if re_texts.search(n)
                else 'props' if RE_PROPS.search(n) else None)
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
                         # Anno 117: id of the name in texts_*.xml (city statuses have a more specific one)
                         'text_id': txt(el, 'Values/CityStatus/CityStatusName') or txt(el, 'Values/Text/OasisId')}
                    fb = el.find('Values/FactoryBase')
                    if fb is not None:   # production: goods per cycle, cycle time in seconds
                        io_ = lambda tag: {txt(i, 'Product'): int(txt(i, 'Amount') or 1)
                                           for i in fb.findall(tag + '/Item') if txt(i, 'Product')}
                        fac = {'in': io_('FactoryInputs'), 'out': io_('FactoryOutputs'), 'cycle': txt(fb, 'CycleTime')}
                        a['factory'] = {k: v for k, v in fac.items() if v}
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
    default_cycle = None                 # FactoryBase/CycleTime when an asset does not set it
    for f in xml['props']:
        m = re.search(r'<FactoryBase>.*?<CycleTime>(\d+)</CycleTime>', open(f, encoding='utf-8', errors='replace').read(), re.S)
        if m: default_cycle = m.group(1)

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
        if inherited(g, 'factory'):
            x['factory'] = dict(inherited(g, 'factory'))
            if 'cycle' not in x['factory'] and default_cycle: x['factory']['cycle'] = default_cycle
        if a.all or 'factory' in x or RE_USEFUL.search(x.get('template') or '') or RE_USEFUL.search(x.get('name') or ''):
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

# ---------------- map ----------------
# A session's terrain is the MapTemplate stored in the save: tiles (.a7m) placed at a position.
# Each tile folder in the game's *provinces*.rda archives holds <tile>.a7minfo (MapSize, and
# ActiveMapGrid: one bit per 8x8 cell telling which part belongs to the tile) and
# _gamedata/<tile>/mapimage_0.dds (BC7, north up). Pillow is needed to decode it.
OWNER_COLORS = [(66, 135, 245), (46, 184, 92), (245, 166, 35), (171, 71, 188), (0, 172, 193),
                (255, 112, 67), (124, 179, 66), (236, 64, 122)]

def template_tiles(sroot):
    mt = one(one(sroot, 'GameSessionManager'), 'MapTemplate')
    out = []
    for t in ch(mt, 'TemplateElement') if mt is not None else []:
        e = one(t, 'Element')
        f = one(e, 'MapFilePath') if e is not None else None
        if f is None: continue
        r = one(e, 'Rotation90')
        out.append({'pos': struct.unpack('<ii', one(e, 'Position').value),
                    'dir': os.path.dirname(f.value.decode('utf-16le').rstrip('\0').replace('\\', '/')).lower(),
                    'rot': r.value[0] if r is not None and r.value else 0})
    return out

def tile_layers(tiles, game):
    """Find each tile's MapSize, active grid and map image in the game's archives."""
    from PIL import Image
    dirs = {t['dir']: t for t in tiles}
    rdas = [p for p in find_rda(game) if re.search(r'provinces|patch', os.path.basename(p), re.I)]
    for rda in rdas:
        print(f'  reading {os.path.basename(rda)} ...')
        for n, get in rda_entries(rda):
            nl = n.lower()
            d = nl.rsplit('/_gamedata/', 1)[0] if '/_gamedata/' in nl else os.path.dirname(nl)
            t = dirs.get(d)
            if t is None: continue
            if nl.endswith('.a7minfo'):
                r = filedb(get())
                t['size'] = struct.unpack('<ii', one(r, 'MapSize').value)
                g = one(r, 'ActiveMapGrid')
                if g is not None:
                    t['grid'] = (u(one(g, 'x').value), u(one(g, 'y').value), one(g, 'bits').value)
            elif nl.endswith('/mapimage_0.dds'):
                t['img'] = Image.open(io.BytesIO(get())).convert('RGB')   # later archives (patches) win
    return [t for t in tiles if 'size' in t and 'img' in t]

def compose_terrain(tiles):
    from PIL import Image
    H = max(max(t['pos'][0] + t['size'][0], t['pos'][1] + t['size'][1]) for t in tiles)
    im = Image.new('RGB', (H, H), (20, 24, 28))
    for t in tiles:
        S = t['size'][0]
        img = t['img'].resize((S, S))
        mask = None
        if 'grid' in t:
            X, Y, bits = t['grid']; rb = len(bits) // Y
            m = Image.new('L', (X, Y), 0); px = m.load()
            for gy in range(Y):
                row = int.from_bytes(bits[gy * rb:(gy + 1) * rb], 'little')
                for gx in range(X):
                    if row >> gx & 1: px[gx, Y - 1 - gy] = 255      # grid rows go north, image rows south
            mask = m.resize((S, S), Image.NEAREST)
        if t['rot']:
            img, mask = img.rotate(90 * t['rot']), mask.rotate(90 * t['rot']) if mask else None
        x, z = t['pos']
        im.paste(img, (x, H - z - S), mask)
    return im

def cmd_map(a):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit('The map command needs Pillow: pip install pillow')
    paths = a.names if a.names else ['guid_names.json'] if os.path.exists('guid_names.json') else []
    gmap, info = load_names(paths), load_info(paths)
    files = rda_files(a.save)
    data_root = filedb(zlib.decompress(files['data.a7s']))
    owners, names = meta_map(data_root)
    texts = read_texts(a.texts) if a.texts else {}
    sessions = []
    for s in one(data_root.children[0], 'GameSessions').children:
        sd, desc = one(s, 'SessionData'), one(s, 'SessionDesc')
        if sd is None: continue
        sg = u(one(desc, 'SessionGUID').value)
        label = KNOWN_SESSIONS.get(sg) or re.sub(r'\W+', '_', gmap.get(sg, f'session_{sg}')).lower()
        root = filedb(one(sd, 'BinaryData').value)
        sessions.append((label, sg, root, areas(root, names, gmap, texts, info)))
    name_cities([e for s in sessions for e in s[3]], user_texts(data_root), {p for p, n in names.items() if n})
    pick = [s for s in sessions if a.session in (s[0], str(s[1]))]
    if not pick: sys.exit('Unknown session; available: ' + ', '.join(f'{s[0]} ({s[1]})' for s in sessions))
    label, sg, sroot, area_list = pick[0]
    tiles = tile_layers(template_tiles(sroot), a.game)
    if not tiles: sys.exit('No map tiles found: point the second argument at the "maindata" folder')
    im = compose_terrain(tiles)
    H = im.size[1]
    im = Image.blend(im, Image.new('RGB', im.size, (0, 0, 0)), 0.25)    # darken so markers stand out
    d = ImageDraw.Draw(im)
    def font(sz):
        for f in ('arialbd.ttf', 'DejaVuSans-Bold.ttf', 'Arial Bold.ttf'):
            try: return ImageFont.truetype(f, sz)
            except OSError: pass
        return ImageFont.load_default()
    humans = {p for p, n in names.items() if n}
    gsm = one(sroot, 'GameSessionManager')
    pirates, objs = set(), []
    for am in one(gsm, 'AreaManagers').children:
        aid = int(am.name.split('_')[-1]) if am.name.startswith('AreaManager_') else None
        aom = one(am, 'AreaObjectManager'); go = one(aom, 'GameObject') if aom is not None else None
        ob_list = one(go, 'objects') if go is not None else None
        for ob in (ob_list.children if ob_list else []):
            if one(ob, 'Pirate') is not None: pirates.add(u(one(ob, 'guid').value))
            p = one(ob, 'Position')
            if p is None: continue
            x, _, z = struct.unpack('<fff', p.value)
            mp = one(ob, 'MetaPersistent'); mid = one(mp, 'MetaID') if mp is not None else None
            kind = 'ship' if one(ob, 'ShipModuleOwner') is not None else 'unit' if one(ob, 'Unit') is not None else 'building'
            objs.append((kind, owners.get(u(mid.value)) if mid is not None else None, aid, x, z))
    area_owner = {e['area']: e['owner'] for e in area_list}
    present = sorted({o for k, o, aid, x, z in objs if o is not None} | {o for o in area_owner.values() if o})
    others = [p for p in present if p not in humans and p not in pirates]
    color = {p: (255, 215, 0) for p in humans}
    color.update({p: (230, 40, 40) for p in pirates})
    label_of = lambda p: names.get(p) or gmap.get(p) or str(p)
    from PIL import ImageColor
    chosen = {}                                                        # --color NAME_OR_GUID=COLOUR
    for spec in a.color or []:
        who, _, col = spec.partition('=')
        try: rgb = ImageColor.getrgb(col.strip())[:3]
        except ValueError: sys.exit(f'--color {spec}: unknown colour')
        hits = [p for p in present if who.strip() == str(p) or who.strip().lower() == str(label_of(p)).lower()]
        if not hits: print(f'  ! --color {spec}: no such participant in {label}', file=sys.stderr)
        for p in hits: chosen[p] = rgb
    near = lambda c1, c2: sum((x - y) ** 2 for x, y in zip(c1, c2)) < 90 ** 2
    taken = list(chosen.values()) + [(230, 40, 40)]                   # pirate red stays unique
    free = [c for c in OWNER_COLORS if not any(near(c, x) for x in taken)] or OWNER_COLORS
    palette = {}                                                       # same name, same colour
    for p in others:
        if p not in chosen: color[p] = palette.setdefault(label_of(p), free[len(palette) % len(free)])
    color.update(chosen)
    grey = (170, 170, 170)
    for kind, o, aid, x, z in objs:                                   # buildings: small dots
        if kind != 'building': continue
        c = color.get(o or area_owner.get(aid), grey)
        d.rectangle((x - 1.5, H - z - 1.5, x + 1.5, H - z + 1.5), fill=c)
    for kind, o, aid, x, z in objs:                                   # pirate structures: crosses
        if kind == 'building' and (o or area_owner.get(aid)) in pirates:
            d.line((x - 6, H - z - 6, x + 6, H - z + 6), fill=(255, 60, 60), width=3)
            d.line((x - 6, H - z + 6, x + 6, H - z - 6), fill=(255, 60, 60), width=3)
    for kind, o, aid, x, z in objs:                                   # ships: triangles, units: squares
        c = color.get(o, grey); y = H - z
        if kind == 'ship':
            d.polygon([(x, y - 9), (x - 7, y + 6), (x + 7, y + 6)], fill=c, outline=(0, 0, 0))
        elif kind == 'unit' and o is not None:
            d.rectangle((x - 5, y - 5, x + 5, y + 5), fill=c, outline=(0, 0, 0))
    f, fs = font(max(14, H // 90)), font(max(11, H // 130))
    for e in area_list:
        if 'center' not in e: continue
        txt = e.get('custom_name') or (e.get('city') or f"area {e['area']}").replace(' Nome', '')
        x, y = e['center'][0], H - e['center'][1]
        c = color.get(e['owner'], grey)
        d.text((x, y - 22), txt, font=f, fill=c, anchor='ms', stroke_width=3, stroke_fill=(0, 0, 0))
        if e['owner'] is not None:                                     # owner under the island name
            d.text((x, y - 20), str(label_of(e['owner'])), font=fs, fill=c, anchor='mt',
                   stroke_width=2, stroke_fill=(0, 0, 0))
    if not a.legend:
        rows = []
    else:
        rows = list({label_of(p): (color[p], label_of(p)) for p in present if p in color}.values())
        rows += [((255, 60, 60), 'pirate structures (x)'), (grey, 'nobody / unknown')]
        lf = font(max(14, H // 100)); lh = lf.size + 8 if hasattr(lf, 'size') else 20
        bx, by = 20, 20
        d.rectangle((bx - 10, by - 10, bx + 420, by + lh * (len(rows) + 2)), fill=(0, 0, 0))
        d.text((bx, by), f'{label}: ships = triangles, units = squares', font=lf, fill=(255, 255, 255))
        for i, (c, t) in enumerate(rows, 1):
            d.rectangle((bx, by + i * lh + 3, bx + 16, by + i * lh + 19), fill=c)
            d.text((bx + 26, by + i * lh), t, font=lf, fill=(255, 255, 255))
    if a.width and a.width < H: im = im.resize((a.width, a.width), Image.LANCZOS)
    out = a.output or f'{label}_map.png'
    im.save(out)
    print(f'{label}: {len(tiles)} tiles, {sum(k == "ship" for k, *_ in objs)} ships, '
          f'{sum(k == "unit" for k, *_ in objs)} units -> {out}')

def main():
    ap = argparse.ArgumentParser(description='Anno 117: Pax Romana save game and game data tools.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('extract', help='save game -> one JSON per session')
    e.add_argument('save'); e.add_argument('-o', '--output', default='sessions')
    e.add_argument('--names', action='append', metavar='FILE',
                   help='GUID name map; repeatable, later files win (default: ./guid_names.json if present)')
    e.add_argument('--radius', type=float, default=40, help='squadron clustering radius (default 40)')
    e.add_argument('--texts', action='append', metavar='FILE',
                   help='texts_<lang>.xml (as extracted by `assets`) to name the areas\' default cities')
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
    mp = sub.add_parser('map', help='save game + game files -> PNG map of a province (needs Pillow)')
    mp.add_argument('save'); mp.add_argument('game', help='Anno 117 "maindata" folder')
    mp.add_argument('--session', default='nile', help='session label or GUID (default nile)')
    mp.add_argument('-o', '--output', help='PNG file (default <session>_map.png)')
    mp.add_argument('--names', action='append', metavar='FILE', help='GUID name map(s), as for extract')
    mp.add_argument('--texts', action='append', metavar='FILE', help='texts_<lang>.xml, as for extract')
    mp.add_argument('--width', type=int, default=1600, help='output width in pixels (default 1600)')
    mp.add_argument('--legend', action='store_true',
                   help='draw a legend box in the top-left corner (owners are always written under island names)')
    mp.add_argument('--color', action='append', metavar='WHO=COLOUR',
                   help='participant name or GUID = colour name or #rrggbb, e.g. "Dorian=yellow"; repeatable')
    a = ap.parse_args()
    {'extract': cmd_extract, 'assets': cmd_assets, 'list': cmd_list, 'guid-table': cmd_guid_table,
     'map': cmd_map}[a.cmd](a)

if __name__ == '__main__':
    main()
