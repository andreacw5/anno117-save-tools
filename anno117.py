#!/usr/bin/env python3
"""
anno117.py - read Anno 117: Pax Romana save games and game data. Python 3.8+, no dependencies.

Commands:
  extract <save.a8s> [-o DIR] [--names FILE ...] [--texts FILE ...] [--radius R]
        One JSON file per session (province): ships with names, modules and positions, land
        units and pirate factions, grouped by owner (participant); every area (island) with its
        owner, fertilities, warehouse, buildings, population, production per minute, income,
        city status and city name; and the trade routes stopping there (ships, stations, goods).
  assets <path> [-o FILE] [--lang LANG] [--all] [--extract-dir DIR]
        Find assets*.xml, texts_<LANG>.xml and export/properties.xml inside the game's .rda
        archives (or in a folder of already-extracted .xml files) and write a GUID -> name map
        (default guid_names.json), with templates and production data (inputs, outputs, cycle).
        By default only military, naval, participant, session, fertility, city status and
        production assets are kept; --all keeps everything.
  map <save.a8s> <maindata> [--session S] [-o FILE] [--color WHO=COLOUR ...] [--legend]
        PNG map of a province: the game's map tiles placed as in the save's MapTemplate, plus
        buildings, ships and units coloured by owner and island names with their owner.
        Needs Pillow (the only optional dependency).
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
class Archive(list):
    """[(name, getter)] of an RDA archive; close() releases the memory map (and the file lock on
    Windows). Getters stop working after close()."""
    mm = None
    def close(self):
        if self.mm is not None: self.mm.close(); self.mm = None
    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

def rda_entries(path):
    """Read only the index of an RDA v2.2 archive (works on multi-GB files via mmap).
    Returns [(name, getter)]; file data is read and decompressed only when getter() is called."""
    with open(path, 'rb') as f:           # the mmap keeps its own handle to the file
        d = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    if d[:18] != b'Resource File V2.2':
        d.close()
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
    out = Archive(out); out.mm = d
    return out

BAD_ARCHIVE = (ValueError, struct.error, zlib.error, OSError)   # corrupt, truncated or unreadable

def rda_files(path):
    """All files of the archive in memory (fine for save games, which are small)."""
    try:
        with rda_entries(path) as entries:
            return {n: get() for n, get in entries}
    except BAD_ARCHIVE as e:
        raise SystemExit(f'{path}: unreadable archive ({e})')

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

# Helpers are None-safe, so a missing branch of the tree gives None/[] instead of an exception.
def ch(n, name): return [c for c in n.children if c.name == name] if n is not None else []
def one(n, name):
    for c in (n.children if n is not None else ()):
        if c.name == name: return c
def u(v): return int.from_bytes(v, 'little')

def unpack(n, fmt):
    """struct.unpack of a node's value, or None when the node is missing or has another size."""
    v = n.value if n is not None else None
    return struct.unpack(fmt, v) if v is not None and len(v) == struct.calcsize(fmt) else None

def meta_root(data_root):
    """MetaGameManager, the first child of data.a7s."""
    return data_root.children[0] if data_root is not None and data_root.children else None

def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)

def write_json(path, data, indent):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)

def read_text(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()

def load_save(path):
    """Open a save game and parse data.a7s."""
    files = rda_files(path)
    if 'data.a7s' not in files:
        sys.exit(f'{path}: not an Anno 117 save game (no data.a7s inside)')
    return filedb(zlib.decompress(files['data.a7s']))

def save_sessions(data_root):
    """(session GUID, session root) for every province in the save."""
    for s in getattr(one(meta_root(data_root), 'GameSessions'), 'children', []):
        bd, sg = one(one(s, 'SessionData'), 'BinaryData'), one(one(s, 'SessionDesc'), 'SessionGUID')
        if bd is not None and sg is not None:
            yield u(sg.value), filedb(bd.value)

# ---------------- save extraction ----------------
# Session GUIDs observed so far (used when no name map is available).
KNOWN_SESSIONS = {3245: 'latium', 6627: 'albion', 149679: 'nile'}

def session_label(sg, gmap):
    return KNOWN_SESSIONS.get(sg) or re.sub(r'\W+', '_', gmap.get(sg, f'session_{sg}')).lower()

def load_names(paths):
    """Merge GUID name maps; later files override earlier ones. Values may be plain strings
    (hand-written maps) or objects as written by `assets`."""
    m = {}
    for path in paths:
        if not os.path.exists(path):
            print(f'  ! name map {path} not found, skipped', file=sys.stderr); continue
        for k, v in read_json(path).items():
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
    mom = one(one(meta_root(data_root), 'MetaGameObjectManager'), 'MetaObjectMap')
    owners, names = {}, {}
    for e in getattr(mom, 'children', []):
        i, p = one(e, 'ID'), one(e, 'ParticipantGUID')
        if i is not None and p is not None:
            owners[u(i.value)] = u(p.value)
        mp = one(e, 'MetaParticipant')
        if mp is not None and p is not None and one(mp, 'CustomCompanyName') is not None:
            v = [c.value for c in one(mp, 'CustomCompanyName').children]
            if len(v) > 1: names[u(p.value)] = v[1].decode('utf-16le', 'replace')
    return owners, names

def roman(n):
    out = ''
    for v, r in ((1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'), (50, 'L'),
                 (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):
        while n >= v: out += r; n -= v
    return out

def ship_name(ob, texts):
    """Nameable/VehicleTextGuid is the LineId of a name in texts_<lang>.xml; VehicleTextIterator
    counts reuses of the same name (1 = none, 3 = 'III')."""
    nb = one(ob, 'Nameable'); g = one(nb, 'VehicleTextGuid'); i = one(nb, 'VehicleTextIterator')
    if g is None or len(g.value) != 8: return None
    t = texts.get(struct.unpack('<q', g.value)[0])
    n = u(i.value) if i is not None else 1
    return (f'{t} {roman(n)}' if n > 1 else t) if t else None

def area_managers(sroot):
    return getattr(one(one(sroot, 'GameSessionManager'), 'AreaManagers'), 'children', [])

def area_objects(am):
    """Game objects of one AreaManager_<id>."""
    return getattr(one(one(one(am, 'AreaObjectManager'), 'GameObject'), 'objects'), 'children', [])

def session_objects(sroot):
    for am in area_managers(sroot):
        yield from area_objects(am)

def session(sroot, owners, names, gmap, radius, texts=None):
    name = lambda g: gmap.get(g, f'GUID {g}')
    ships = collections.defaultdict(lambda: collections.defaultdict(list))
    units = collections.defaultdict(collections.Counter)
    pirates = []
    for ob in session_objects(sroot):
        gn = one(ob, 'guid')
        if gn is None: continue
        g = u(gn.value)
        p = one(ob, 'Pirate')
        if p is not None:
            c = unpack(one(one(p, 'AreaOfActivity'), 'ptCenter'), '<ff')
            mw, k = one(p, 'MaxEverReachedWarships'), one(one(p, 'Kontor'), 'ObjectID')
            pirates.append({'participant': g, 'name': name(g),
                            'activity_center': [round(x) for x in c] if c else None,
                            'max_warships_reached': u(mw.value) if mw is not None else None,
                            'base_object_id': k.value.hex() if k is not None else None})
        mid = one(one(ob, 'MetaPersistent'), 'MetaID')
        owner = owners.get(u(mid.value)) if mid is not None else None
        pos = unpack(one(ob, 'Position'), '<fff') or (0, 0, 0)
        smo = one(ob, 'ShipModuleOwner')
        if smo is not None:
            mods = sorted(u(m.value) for ism in ch(smo, 'InstalledShipModules')
                          for t in ism.children for m in ch(t, 'ShipModuleGuid'))
            nm = ship_name(ob, texts or {})
            ships[owner][g].append({**({'name': nm} if nm else {}), 'modules': [name(m) for m in mods],
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
    m = one(meta_root(data_root), 'MetaUserGeneratedTextManager')
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
    for i, e in enumerate(all_areas):
        for r in e.get('trade_routes', {}): by_route[r].add(i)
    for r, ids in sorted(by_route.items()):
        m = RE_ROUTE_CODE.search(r)
        if m:
            for code in sorted(set(m.groups())): users[code].append(ids)
    cand = {c: set.intersection(*v) for c, v in sorted(users.items())}
    defaults = {e['city'][:3] for e in all_areas if e.get('city')}
    starts = lambda i, c: (all_areas[i].get('city') or '').startswith(c)
    assigned = {}
    for c, s in cand.items():                       # 1. codes confirmed by a default city name
        own = sorted(i for i in s if starts(i, c))
        if len(own) == 1 and own[0] not in assigned.values(): assigned[c] = own[0]
    changed = True
    while changed:                                  # 2. elimination, in a fixed order
        changed = False
        for c, s in cand.items():
            if c in assigned: continue
            s = sorted(s - set(assigned.values()))
            own = [i for i in s if starts(i, c)]
            if len(own) == 1 or len(s) == 1:
                assigned[c] = (own or s)[0]; changed = True
    for c, i in sorted(assigned.items()):
        e = all_areas[i]
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
            for k, v in read_json(path).items():
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
        gn = one(ob, 'guid')
        if one(ob, 'Building') is None or gn is None: continue
        g = u(gn.value)
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
        try: cyc = float(spec.get('cycle') or 0)
        except ValueError: cyc = 0
        if cyc:
            for k in ('out', 'in'):
                for good, amt in spec.get(k, {}).items():
                    if str(good).isdigit(): x[k][int(good)] += float(amt) * 60 / cyc * prod
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
        if k.value is None: continue
        aid = u(k.value)
        o, cn, fert = one(v, 'OwnerProfile'), one(v, 'CityNameGuid'), one(v, 'Fertility')
        owner = u(o.value) if o is not None else None
        rs = collections.Counter()
        def walk(n):
            for c in n.children:
                if c.name == 'RouteName' and c.value: rs[c.value.decode('utf-16le', 'replace').split(chr(0))[0]] += 1
                walk(c)
        walk(one(v, 'PassiveTrade') or Node('x'))
        e = {'area': aid, 'owner': owner,
             'owner_name': names.get(owner) or (gmap.get(owner) if owner is not None else None),
             'city': texts.get((unpack(cn, '<q') or (None,))[0])}
        e['fertility_guids'] = [u(c.value) for c in getattr(fert, 'children', []) if c.value]
        e['fertilities'] = [name(g) for g in e['fertility_guids']]
        am = one(managers, f'AreaManager_{aid}') if managers is not None else None
        if am is not None:
            mm = one(am, 'AreaMoneyManager')
            inc = f32(one(mm, 'TotalMoneyIncomePerMinute'))
            if inc is not None: e['income_per_minute'] = round(inc)
            pm = one(am, 'AreaPopulationManager')
            st = one(pm, 'CurrentCityStatusGUID') if pm is not None else None
            if st is not None and st.value: e['city_status'] = name(u(st.value))
            objs = area_objects(am)
            ps = [p for p in (unpack(one(ob, 'Position'), '<fff') for ob in objs) if p]
            e.update(economy(objs, name, ainfo or {}))
            if ps:
                xs, zs = [p[0] for p in ps], [p[2] for p in ps]
                e['objects'] = len(ps)
                e['center'] = [round(sum(xs) / len(xs)), round(sum(zs) / len(zs))]
                e['extent'] = [round(min(xs)), round(min(zs)), round(max(xs)), round(max(zs))]
        eco = one(v, 'AreaEconomy')
        sl = one(eco, 'StrgLrg') if eco is not None else None
        if sl is not None and sl.value:
            goods = sorted(((g, n) for g, n in storage(sl.value).items() if n), key=lambda x: -x[1])
            e['storage'] = {name(g): n for g, n in goods}
            e['storage_guids'] = {str(g): n for g, n in goods}
        if rs: e['trade_routes'] = dict(rs.most_common())
        out.append(e)
    return out

def ship_index(sroot):
    """MetaID -> (ship GUID, x, z) for every ship of a session."""
    out = {}
    for ob in session_objects(sroot):
        mid, gn = one(one(ob, 'MetaPersistent'), 'MetaID'), one(ob, 'guid')
        if one(ob, 'ShipModuleOwner') is not None and mid is not None and gn is not None:
            x, _, z = unpack(one(ob, 'Position'), '<fff') or (0, 0, 0)
            out[u(mid.value)] = (u(gn.value), round(x), round(z))
    return out

def trade_routes(data_root):
    """Trade routes of all players, from MetaGameManager/SessionTradeRouteManager/RouteMap.
    Ships are listed by MetaID (uint64 each); stations by area id with the goods set there."""
    rm = one(one(meta_root(data_root), 'SessionTradeRouteManager'), 'RouteMap')
    out = []
    for k, t in zip(rm.children[0::2], rm.children[1::2]) if rm is not None else []:
        nm, sh, ow = one(t, 'Name'), one(t, 'Ships'), one(t, 'Owner')
        sv = sh.value if sh is not None and sh.value else b''
        stations = []
        for st in getattr(one(t, 'Stations'), 'children', []):
            goods = {}
            for g in getattr(one(st, 'GoodInfos'), 'children', []):
                pg, am = one(g, 'ProductGUID'), unpack(one(g, 'Amount'), '<i')
                if pg is not None and pg.value: goods[u(pg.value)] = am[0] if am else None
            ar = one(st, 'AreaID')
            stations.append({'area': u(ar.value) if ar is not None and ar.value else None, 'goods': goods})
        if k.value is None: continue
        out.append({'id': u(k.value), 'name': nm.value.decode('utf-16le', 'replace').rstrip(chr(0)) if nm is not None else None,
                    'owner': u(ow.value) if ow is not None else None,
                    'ships': [u(sv[i:i + 8]) for i in range(0, len(sv) - 7, 8)], 'stations': stations})
    return out

def attach_routes(results, routes, ships, names, gmap):
    """Name each route's ships and stations and add it to every session it stops in.
    Area ids are only unique within a session: a station goes to the session of the route's
    ships when its id exists in several."""
    by_area = collections.defaultdict(list)
    for r in results:
        for e in r['areas']: by_area[e['area']].append((r['session'], e))
    def best(cands, rt, ship_sessions):
        # the area that recorded this route by name, else the owner's area, else the ships' session
        for test in (lambda c: rt['name'] in c[1].get('trade_routes', {}),
                     lambda c: c[1].get('owner') == rt['owner'] and c[0] in ship_sessions,
                     lambda c: c[1].get('owner') == rt['owner'],
                     lambda c: c[0] in ship_sessions):
            hit = [c for c in cands if test(c)]
            if len(hit) == 1: return hit[0]
        return cands[0] if len(cands) == 1 else None
    name = lambda g: gmap.get(g, f'GUID {g}')
    for rt in routes:
        sh = [ships.get(m) for m in rt['ships']]
        ship_sessions = {x[0] for x in sh if x}
        stops, where = [], set()
        for st in rt['stations']:
            pick = best(by_area.get(st['area'], []), rt, ship_sessions)
            e = pick[1] if pick else {}
            if pick: where.add(pick[0])
            stops.append({'area': st['area'], 'session': pick[0] if pick else None,
                          'city': e.get('custom_name') or e.get('city'),
                          'goods': {name(g): n for g, n in st['goods'].items()}})
        row = {'id': rt['id'], 'name': rt['name'], 'owner': rt['owner'],
               'owner_name': names.get(rt['owner']) or gmap.get(rt['owner']),
               'ships': [{'type': name(x[1]), 'guid': x[1], 'session': x[0], 'position': [x[2], x[3]]} if x
                         else {'meta_id': m} for m, x in zip(rt['ships'], sh)],
               'stations': stops}
        for r in results:                   # no station resolved: show it where its ships are
            if r['session'] in (where or ship_sessions): r.setdefault('trade_routes', []).append(row)

def cmd_extract(a):
    os.makedirs(a.output, exist_ok=True)
    paths = a.names if a.names else ['guid_names.json'] if os.path.exists('guid_names.json') else []
    gmap, info = load_names(paths), load_info(paths)
    data_root = load_save(a.save)
    owners, names = meta_map(data_root)
    utexts = user_texts(data_root)
    texts = read_texts(a.texts) if a.texts else {}   # resolves the areas' default city names
    results, ships, seen = [], {}, set()
    for sg, sroot in save_sessions(data_root):
        label = session_label(sg, gmap)
        if label in seen: label = f'{label}_{sg}'          # two sessions with the same name
        seen.add(label)
        ships.update({m: (label,) + v for m, v in ship_index(sroot).items()})
        results.append({'session': label, 'session_guid': sg, 'save': os.path.basename(a.save),
                        **session(sroot, owners, names, gmap, a.radius, texts),
                        'areas': areas(sroot, names, gmap, texts, info)})
    # trade routes cross provinces, so cities are matched to route codes over the whole save
    name_cities([e for r in results for e in r['areas']], utexts, {p for p, n in names.items() if n})
    attach_routes(results, trade_routes(data_root), ships, names, gmap)
    for res in results:
        label = res['session']
        p = os.path.join(a.output, label + '.json')
        write_json(p, res, 2)
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
            with open(out, 'wb') as fo: fo.write(data)
            print(f'  + {n}  ({len(data) / 1e6:.1f} MB, from {os.path.basename(rda)})')
            found[k].append(out)
        entries.close()
    return found

def txt(el, tag):
    x = el.find(tag) if el is not None else None
    return x.text.strip() if x is not None and x.text else None

def num(v, default=None):
    """Number from XML text: int when integral, float otherwise, default when missing or invalid."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return int(f) if f.is_integer() else f

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
                        io_ = lambda tag: {txt(i, 'Product'): num(txt(i, 'Amount'), 1)
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
        m = re.search(r'<FactoryBase>(?:(?!</FactoryBase>).)*?<CycleTime>\s*([\d.]+)\s*</CycleTime>', read_text(f), re.S)
        if m: default_cycle = m.group(1)

    def base_of(x):
        b = x.get('base')
        return int(b) if b and b.isdigit() else None

    def inherited(g, key, depth=0):   # assets with a BaseAssetGUID inherit missing values from the base
        x = assets.get(g, {})
        if x.get(key) or depth > 20 or base_of(x) is None: return x.get(key)
        return inherited(base_of(x), key, depth + 1)

    def factory(g, depth=0):          # merged per key: a derived asset may override only CycleTime
        x = assets.get(g, {})
        own = dict(x.get('factory') or {})
        if depth <= 20 and base_of(x) is not None:
            for k, v in factory(base_of(x), depth + 1).items(): own.setdefault(k, v)
        return own

    out = {}
    for g, x in assets.items():
        x = {k: v for k, v in x.items() if k not in ('base', 'text_id')}
        tid = inherited(g, 'text_id')
        local = texts.get(int(tid)) if tid and tid.lstrip('-').isdigit() else texts.get(g)
        if local: x['name_local'] = local
        if inherited(g, 'template'): x['template'] = inherited(g, 'template')
        fac = factory(g)
        if fac:
            x['factory'] = fac
            if 'cycle' not in fac and default_cycle: fac['cycle'] = default_cycle
        if a.all or 'factory' in x or RE_USEFUL.search(x.get('template') or '') or RE_USEFUL.search(x.get('name') or ''):
            out[str(g)] = x
    out = dict(sorted(out.items(), key=lambda kv: int(kv[0])))
    write_json(a.output, out, 1)
    print(f'\n{len(out)} GUIDs written to {a.output} ({len(assets)} assets, {len(texts)} {a.lang} texts)')

def cmd_list(a):
    f = (a.filter or '').lower()
    for rda in find_rda(a.path):
        try:
            with rda_entries(rda) as entries:
                for n, _ in entries:
                    if f in n.lower(): print(f'{os.path.basename(rda)}: {n}')
        except Exception as e:
            print(f'  ! {rda}: {e}', file=sys.stderr)

def cmd_guid_table(a):
    m = read_json(a.names)
    if os.path.dirname(a.output): os.makedirs(os.path.dirname(a.output), exist_ok=True)
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
    with open(a.output, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
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
        r, pos = one(e, 'Rotation90'), unpack(one(e, 'Position'), '<ii')
        if pos is None: continue
        out.append({'pos': pos,
                    'dir': os.path.dirname(f.value.decode('utf-16le').rstrip('\0').replace('\\', '/')).lower(),
                    'rot': r.value[0] if r is not None and r.value else 0})
    return out

def tile_layers(tiles, game):
    """Find each tile's MapSize, active grid and map image in the game's archives."""
    from PIL import Image
    dirs = collections.defaultdict(list)           # a tile file may be placed several times
    for t in tiles: dirs[t['dir']].append(t)
    natural = lambda p: [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', os.path.basename(p).lower())]
    rdas = [p for p in find_rda(game) if re.search(r'provinces|patch', os.path.basename(p), re.I)]
    rdas.sort(key=lambda p: ('patch' in os.path.basename(p).lower(), natural(p)))   # patches last: they win
    for rda in rdas:
        print(f'  reading {os.path.basename(rda)} ...')
        try:
            entries = rda_entries(rda)
        except BAD_ARCHIVE as e:
            print(f'  ! {rda}: {e}', file=sys.stderr); continue
        for n, get in entries:
            nl = n.lower()
            d = nl.rsplit('/_gamedata/', 1)[0] if '/_gamedata/' in nl else os.path.dirname(nl)
            if d not in dirs: continue
            try:
                if nl.endswith('.a7minfo'):
                    r = filedb(get())
                    size, g = unpack(one(r, 'MapSize'), '<ii'), one(r, 'ActiveMapGrid')
                    gx, gy, bits = one(g, 'x'), one(g, 'y'), one(g, 'bits')
                    for t in dirs[d]:
                        if size: t['size'] = size
                        if None not in (gx, gy, bits): t['grid'] = (u(gx.value), u(gy.value), bits.value)
                elif nl.endswith('/mapimage_0.dds'):
                    img = Image.open(io.BytesIO(get())).convert('RGB')
                    for t in dirs[d]: t['img'] = img
            except (BAD_ARCHIVE + (IndexError, KeyError)) as e:
                print(f'  ! {n}: {e}', file=sys.stderr)
        entries.close()
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
    data_root = load_save(a.save)
    owners, names = meta_map(data_root)
    texts = read_texts(a.texts) if a.texts else {}
    sessions = [(session_label(sg, gmap), sg, root, areas(root, names, gmap, texts, info))
                for sg, root in save_sessions(data_root)]
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
    for am in area_managers(sroot):
        aid = int(am.name.split('_')[-1]) if re.fullmatch(r'AreaManager_\d+', am.name) else None
        for ob in area_objects(am):
            gn = one(ob, 'guid')
            if one(ob, 'Pirate') is not None and gn is not None: pirates.add(u(gn.value))
            p = unpack(one(ob, 'Position'), '<fff')
            if p is None: continue
            x, _, z = p
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
    taken += [(255, 215, 0)] if any(p not in chosen for p in humans) else []   # default human gold
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
        bw = max(d.textlength(t, font=lf) for t in [f'{label}: ships = triangles, units = squares'] + [r[1] for r in rows]) + 40
        d.rectangle((bx - 10, by - 10, bx + bw, by + lh * (len(rows) + 2)), fill=(0, 0, 0))
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
    s.add_argument('--all', action='store_true', help='keep every asset (default: military, naval, participant, session, fertility, city status, production)')
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
