#!/usr/bin/env python3
"""
anno117.py - read Anno 117: Pax Romana save games and game data. Python 3.8+, no dependencies.

Commands:
  extract <save.a8s> [-o DIR] [--names FILE ...] [--texts FILE ...] [--city-names FILE] [--radius R]
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
  map <save.a8s> <maindata> [--session S] [-o FILE] [--color WHO=COLOUR ...] [--legend] [--no-routes]
        PNG map of a province: the game's map tiles placed as in the save's MapTemplate, plus
        buildings, ships (warships triangles, civil ships circles) and units coloured by owner, island names with their owner, and the
        human players' trade routes as dashed lines.
        Needs Pillow (the only optional dependency).
  report <save.a8s | DIR> [--compare PREVIOUS] [-o FILE.html] [--md FILE.md] [--maps DIR] [--player WHO]
        One self-contained HTML page (and optionally Markdown) on the human player's state:
        alarms first (goods running out, idle buildings, workforce shortages, routes with no
        ships or no stock, goods shipped to full warehouses, losses since the previous save),
        then per province the player's islands, all players, fleets, routes and warehouses.
  list <file.rda | dir> [FILTER]
        List the files contained in .rda archives (handy when `assets` finds nothing).
  guid-table <guid_names.json> [-o FILE] [--template T ...] [--title TEXT]
        Render the GUID map as Markdown, one table per template (e.g. for the GitHub Pages site).

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

RE_WEAPON_MODULE = re.compile(r'archer|catapult|scorpion|torcher', re.I)
RE_MILITARY_SHIP = re.compile(r'military|raider|warship|pirate', re.I)

def ship_modules(smo):
    """GUIDs of the modules installed on a ship (ShipModuleOwner/InstalledShipModules)."""
    return sorted(u(m.value) for ism in ch(smo, 'InstalledShipModules') for t in ism.children for m in ch(t, 'ShipModuleGuid'))

def ship_role(g, mods, info, gmap):
    """'war' when the ship carries a weapon module (archers, catapult, scorpion, torchers) or its
    asset is a military one (e.g. 'Egyptian RiverShip Dahabiya Military'); otherwise 'civil'
    (cargo ships with sails only, river boats, start ships). Uses internal asset names."""
    iname = lambda x: (info.get(x) or {}).get('name') or gmap.get(x) or ''
    return 'war' if any(RE_WEAPON_MODULE.search(iname(m)) for m in mods) or RE_MILITARY_SHIP.search(iname(g)) else 'civil'

def session(sroot, owners, names, gmap, radius, texts=None, info=None):
    info = info or {}
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
            mods = ship_modules(smo)
            nm = ship_name(ob, texts or {})
            ships[owner][g].append({**({'name': nm} if nm else {}), 'role': ship_role(g, mods, info, gmap),
                                    'modules': [name(m) for m in mods],
                                    'module_guids': mods,
                                    'position': [round(pos[0]), round(pos[2])]})
        elif one(ob, 'Unit') is not None:
            units[owner][g] += 1
    parts = []
    for owner in sorted(set(ships) | set(units), key=lambda x: (x is None, x or 0)):
        sh = ships.get(owner, {})
        un = units.get(owner, {})
        troop = lambda g: (info.get(g) or {}).get('template') == 'LandUnit'
        parts.append({
            'participant': owner,
            'name': names.get(owner) or (gmap.get(owner) if owner is not None else 'unknown owner'),
            'ship_count': sum(len(v) for v in sh.values()),
            'warships': sum(s['role'] == 'war' for v in sh.values() for s in v),
            'civil_ships': sum(s['role'] == 'civil' for v in sh.values() for s in v),
            'troops': sum(n for g, n in un.items() if troop(g)),
            'ships': [{'type': name(g), 'guid': g, 'count': len(v), 'ships': v} for g, v in sorted(sh.items())],
            'squadrons': cluster([tuple(s['position']) for v in sh.values() for s in v], radius),
            'units': [{'type': name(g), 'guid': g, 'count': n, **({'troop': True} if troop(g) else {})}
                      for g, n in sorted(un.items())],
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

def apply_city_names(sessions, path):
    """City names you typed in the game, from a JSON file: {"<area id>": "name"} or
    {"<session>:<area id>": "name"} when an id exists in several sessions. They win over the names
    inferred from trade routes. The save stores a renamed city's text but no link to its area."""
    if not path: return
    m = read_json(path)
    for label, area_list in sessions:
        for e in area_list:
            nm = m.get(f"{label}:{e['area']}") or m.get(str(e['area']))
            if nm:
                e['custom_name'] = nm; e['custom_name_source'] = 'city-names file'
                e.pop('custom_name_candidates', None)

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

SHIP_STEP = 15          # a stock change this big between two samples is a ship loading / unloading
ICON_STORAGE_FULL = 2001976   # InfoLayerIcon "StorageFull": the building's output is not collected
TREND_STEP_S = 10  # StorageTrends samples every 10 game seconds (two saves 180 s apart: series shifted by 18)

def stock_history(tr):
    """AreaEconomy/StorageTrends: per good, the last 120 stock samples (oldest first, 20 minutes),
    the data behind the game's warehouse chart. Returns, per good GUID: the stock that left and
    entered per minute (sum of falls / rises: consumption and ships loading / production and ships
    unloading), their difference (net change per minute) and the highest sample."""
    out = {}
    for k, t in zip(tr.children[0::2], tr.children[1::2]) if tr is not None else []:
        pts = [u(c.value) for c in ch(one(t, 'Points'), 'a8000') if c.value is not None]
        if k.value is None or len(pts) < 2 or not any(pts): continue
        minutes = (len(pts) - 1) * TREND_STEP_S / 60
        steps = [b - a for a, b in zip(pts, pts[1:])]
        out[u(k.value)] = {'net_per_minute': round((pts[-1] - pts[0]) / minutes, 2),
                           'outflow_per_minute': round(-sum(s for s in steps if s < 0) / minutes, 2),
                           'inflow_per_minute': round(sum(s for s in steps if s > 0) / minutes, 2),
                           'max': max(pts), 'first': pts[0], 'last': pts[-1], 'minutes': round(minutes, 1),
                           # a ship loading or unloading moves a whole cargo within one 10 s sample
                           'loaded': -sum(x for x in steps if x <= -SHIP_STEP), 'unloaded': sum(x for x in steps if x >= SHIP_STEP)}
    return out

def storage_limits(peaks):
    """Storage limits are not in the save, and they differ between goods of the same area (e.g.
    450 for most goods, 1950 for some raw materials). Goods held at a limit all peak at the same
    value, so every peak shared by at least three goods is taken as a limit."""
    c = collections.Counter(p for p in peaks if p >= 20)
    return sorted(v for v, n in c.items() if n >= 3)

def at_limit(stock, trend, limits):
    """True when a good's stock is within 2% of one of its area's storage limits and its own peak
    did not go past that limit (so a 450 good is not 'at limit' in an area whose limits are 450 and 1950
    only because of the 450)."""
    return any(abs(stock - lim) <= 0.02 * lim and (not trend or trend['max'] <= lim * 1.02) for lim in limits)

def f32(n):
    return struct.unpack('<f', n.value)[0] if n is not None and n.value is not None and len(n.value) == 4 else None

def economy(objs, name, info):
    """Buildings, residents and production of one area's objects. Production per minute is
    amount x 60 / cycle time x current productivity (Factory7/CurrentProductivity, 1.0 = 100%)."""
    buildings, residents = collections.Counter(), collections.Counter()
    fac = collections.defaultdict(lambda: {'count': 0, 'working': 0, 'prod': 0.0, 'out': collections.Counter(),
                                           'in': collections.Counter(), 'cap': collections.Counter(),
                                           'idle': collections.Counter(), 'full': 0,
                                           'other': collections.Counter()})
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
        if not prod:                        # the status icons the game shows over an idle building
            ig = {u(i.value) for t in ch(one(one(ob, 'Infolayer'), 'Icons'), 't1')
                  for i in [one(t, 'GUID')] if i is not None and i.value}
            x['idle'][' + '.join(sorted(name(i) for i in ig)) or '(no icon)'] += 1
            if ICON_STORAGE_FULL in ig: x['full'] += 1
            else: x['other'][' + '.join(sorted(name(i) for i in ig)) or '(no icon)'] += 1
        if ps is not None and one(ps, 'InProgress') is not None: x['working'] += 1
        spec = (info.get(g) or {}).get('factory') or {}
        try: cyc = float(spec.get('cycle') or 0)
        except ValueError: cyc = 0
        if cyc:
            for k in ('out', 'in'):
                for good, amt in spec.get(k, {}).items():
                    if str(good).isdigit(): x[k][int(good)] += float(amt) * 60 / cyc * prod
            for good, amt in spec.get('out', {}).items():   # what the building makes at 100%
                if str(good).isdigit(): x['cap'][int(good)] += float(amt) * 60 / cyc
    res = {}
    if buildings: res['buildings'] = {name(g): n for g, n in buildings.most_common()}
    if residents:
        res['population'] = round(sum(residents.values()))
        res['residents'] = {name(g): round(n) for g, n in residents.most_common()}
        needs = {}                      # good or service -> who asks for it (from the game's NeedsList)
        for g in residents:
            for nd in (info.get(g) or {}).get('needs', []):
                pr = nd.get('product')
                if not str(pr or '').isdigit(): continue
                x = needs.setdefault(name(int(pr)), {'category': nd.get('category'), 'houses': [],
                                                     'kind': 'good' if nd.get('rate') else 'service'})
                if name(g) not in x['houses']: x['houses'].append(name(g))
                x['buff_only'] = x.get('buff_only', True) and bool(nd.get('buff_only'))   # for every house asking
        for x in needs.values():
            if not x.pop('buff_only'): continue
            x['buff_only'] = True
        if needs: res['needs'] = needs
    if fac:
        rows, net, cap, idle, full = [], collections.Counter(), collections.Counter(), collections.Counter(), 0
        other = collections.Counter()
        for g, x in sorted(fac.items(), key=lambda kv: -kv[1]['count']):
            row = {'building': name(g), 'guid': g, 'count': x['count'], 'working': x['working'],
                   'productivity': round(x['prod'] / x['count'], 2)}
            if x['out']: row['output_per_minute'] = {name(k): round(v, 2) for k, v in x['out'].items()}
            if x['in']: row['input_per_minute'] = {name(k): round(v, 2) for k, v in x['in'].items()}
            if x['cap']: row['capacity_per_minute'] = {name(k): round(v, 2) for k, v in x['cap'].items()}
            if x['idle']: row['idle_reasons'] = dict(x['idle'].most_common())
            if x['full']: row['idle_output_full'] = x['full']
            rows.append(row)
            idle.update(x['idle']); full += x['full']; other.update(x['other'])
            cap.update(x['cap'])
            net.update(x['out']); net.subtract(x['in'])
        res['production'] = rows
        res['output_capacity'] = {name(k): round(v, 2) for k, v in cap.most_common()}
        if idle: res['idle_reasons'] = dict(idle.most_common())
        if full: res['idle_output_full'] = full     # idle with the "production storage full" icon
        if other: res['idle_other_reasons'] = dict(other.most_common())   # idle for anything else
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
            wm = one(one(am, 'AreaWorkforceManager'), 'DeltaStorage')
            if wm is not None:              # share of each workforce type's demand that is met
                wf = {name(u(k.value)): round(f32(one(v, 'Saturation')), 2)
                      for k, v in zip(wm.children[0::2], wm.children[1::2])
                      if k.value and f32(one(v, 'Saturation')) is not None}
                if wf: e['workforce_saturation'] = wf
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
        hist = stock_history(one(eco, 'StorageTrends'))
        if hist:                    # StrgLrg reads 0 for many goods that the chart shows full: trust the chart
            raw = {int(g): n for g, n in e.get('storage_guids', {}).items()}
            raw.update({g: t['last'] for g, t in hist.items()})
            goods = sorted(((g, n) for g, n in raw.items() if n), key=lambda x: -x[1])
            e['storage'] = {name(g): n for g, n in goods}
            e['storage_guids'] = {str(g): n for g, n in goods}
            e['stock_trends'] = {name(g): t for g, t in sorted(hist.items(), key=lambda kv: kv[1]['net_per_minute'])}
            lim = storage_limits([t['max'] for t in hist.values()])
            if lim: e['storage_limits_estimate'] = lim
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
    ships when its id exists in several.
    Also fills each area's `supplies` / `receives` ({good: [cities]}). The route itself does not say
    which stop loads a good; the warehouse history does: a ship loading takes a whole cargo within one
    sample (`stock_trends.loaded`), unloading adds one (`unloaded`). Goods no ship moved in the last
    20 minutes go *from* the stop with the largest production capacity for them (output_capacity:
    its buildings at 100%, so idle farms still count) *to* the other stops."""
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
    city = lambda e: e.get('custom_name') or e.get('city') or f"area {e.get('area')}"
    flows = collections.defaultdict(lambda: {'supplies': collections.defaultdict(set),
                                             'receives': collections.defaultdict(set)})
    areas_by_id = {}
    for rt in routes:
        sh = [ships.get(m) for m in rt['ships']]
        ship_sessions = {x[0] for x in sh if x}
        stops, where, visited = [], set(), []
        for st in rt['stations']:
            pick = best(by_area.get(st['area'], []), rt, ship_sessions)
            e = pick[1] if pick else {}
            if pick:
                where.add(pick[0]); visited.append((e, {name(g) for g in st['goods']}))
                areas_by_id[id(e)] = e
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
        capacity = lambda e, g: (e.get('output_capacity') or {}).get(g, 0)
        moved = lambda e, g, k: ((e.get('stock_trends') or {}).get(g) or {}).get(k, 0)
        for g in sorted({g for _, goods in visited for g in goods}):
            stops_g = [e for e, goods in visited if g in goods]
            loads = [e for e in stops_g if moved(e, g, 'loaded') and not moved(e, g, 'unloaded')]
            if loads:                       # seen in the warehouse history: ships took a cargo here
                srcs = loads                # (unloads are not used: they may come from another route)
            else:                           # nothing loaded in the last 20 minutes: largest producer
                best_cap = max(capacity(x, g) for x, _ in visited)
                srcs = [e for e, _ in visited if best_cap > 0 and capacity(e, g) >= best_cap]
            dests = [e for e in stops_g if e not in srcs] or [e for e, _ in visited if e not in srcs]
            for e in srcs:
                for e2 in dests:
                    flows[id(e)]['supplies'][g].add(city(e2))
                    flows[id(e2)]['receives'][g].add(city(e))
    for k, f in flows.items():
        for key in ('supplies', 'receives'):
            if f[key]: areas_by_id[k][key] = {g: sorted(v) for g, v in sorted(f[key].items())}

def participant_kind(g, info, humans):
    """'human', 'rival' (AI competitors), 'pirate', or 'npc' (traders, imperial envoys and neutral
    parties: one unconquerable harbour, not attackable), from the participant asset's template."""
    t = (info.get(g) or {}).get('template') or ''
    if g in humans or 'Human' in t: return 'human'
    if 'Rival' in t or '2ndParty' in t: return 'rival'
    if 'Pirate' in t: return 'pirate'
    return 'npc' if t.startswith('Participant') else None

def name_paths(a):
    return a.names if a.names else ['guid_names.json'] if os.path.exists('guid_names.json') else []

def extract_results(save, paths, texts_files=None, city_names=None, radius=40):
    """Everything `extract` writes, as one dict per session."""
    gmap, info = load_names(paths), load_info(paths)
    data_root = load_save(save)
    owners, names = meta_map(data_root)
    humans = sorted(p for p, n in names.items() if n)
    utexts = user_texts(data_root)
    kinds = {str(g): k for g in set(owners.values()) | set(names)
             for k in [participant_kind(g, info, set(humans))] if k}
    texts = read_texts(texts_files) if texts_files else {}   # resolves the areas' default city names
    results, ships, seen = [], {}, set()
    for sg, sroot in save_sessions(data_root):
        label = session_label(sg, gmap)
        if label in seen: label = f'{label}_{sg}'          # two sessions with the same name
        seen.add(label)
        ships.update({m: (label,) + v for m, v in ship_index(sroot).items()})
        st = one(one(sroot, 'GameSessionManager'), 'SessionTotalTime')
        results.append({'session': label, 'session_guid': sg, 'save': os.path.basename(save),
                        **({'session_time_ms': u(st.value)} if st is not None and st.value else {}),
                        'human_players': humans, 'participant_kinds': kinds,
                        **session(sroot, owners, names, gmap, radius, texts, info),
                        'areas': areas(sroot, names, gmap, texts, info)})
    # trade routes cross provinces, so cities are matched to route codes over the whole save
    name_cities([e for r in results for e in r['areas']], utexts, set(humans))
    apply_city_names([(r['session'], r['areas']) for r in results], city_names)
    attach_routes(results, trade_routes(data_root), ships, names, gmap)
    return results

def cmd_extract(a):
    os.makedirs(a.output, exist_ok=True)
    results = extract_results(a.save, name_paths(a), a.texts, a.city_names, a.radius)
    for res in results:
        label = res['session']
        p = os.path.join(a.output, label + '.json')
        write_json(p, res, 2)
        print(f'{label}: {sum(x["ship_count"] for x in res["participants"])} ships, '
              f'{len(res["participants"])} owners, {len(res["pirates"])} pirate factions, '
              f'{len(res["areas"])} areas -> {p}')

# ---------------- report ----------------
def load_results(path, a):
    """Sessions from a save game (extracted now), a folder of `extract` JSON files, or one of them."""
    if os.path.isdir(path):
        res = [d for d in (read_json(os.path.join(path, f)) for f in sorted(os.listdir(path)) if f.endswith('.json'))
               if isinstance(d, dict) and 'session' in d and 'areas' in d]
        if not res: sys.exit(f'{path}: no JSON written by `extract` found')
        return res
    if path.lower().endswith('.json'): return [read_json(path)]
    return extract_results(path, name_paths(a), a.texts, a.city_names, a.radius)

def city_of(e):
    return e.get('custom_name') or e.get('city') or f"area {e.get('area')}"

def fnum(v, nd=0):
    if v is None: return ''
    return f'{v:,.{nd}f}' if isinstance(v, float) and nd else f'{round(v):,}'

def cell(v, old=None, nd=0, pct=False):
    """(text, sort key) for a number, with the change since the previous save when there is one."""
    if v is None: return ('', None)
    text = f'{v:.0%}' if pct else fnum(v, nd)
    if old is not None and round(v - old, 2 if pct or nd else 0):
        d = v - old
        text += f' ({d:+.0%})' if pct else f' ({"+" if d > 0 else "-"}{fnum(abs(d), nd)})'
    return (text, v)

def production_counts(e):
    total = sum(r['count'] for r in e.get('production', []))
    idle = sum((e.get('idle_reasons') or {}).values())
    return total, idle

ROLE_LABEL = {'war': 'war', 'civil': 'civil', None: '?'}

def fleet(p):
    """(role, ship type) -> count; role is None in JSON written before roles existed."""
    c = collections.Counter()
    for t in p.get('ships', []):
        for s in t.get('ships', []): c[(s.get('role'), t['type'])] += 1
        if not t.get('ships'): c[(None, t['type'])] += t['count']
    return c

def troops(p):
    c = collections.Counter()
    for un in p.get('units', []):
        if un.get('troop'): c[un['type']] += un['count']
    return c

def forces(p):
    """(warships, civil ships, troops); None where the JSON predates these fields."""
    return [p.get('warships'), p.get('civil_ships'), p.get('troops')]

def force_cells(p, old):
    """Cells for forces(); an empty participant (no ships or units there) counts as zero."""
    f = forces(p) if p else [0, 0, 0]
    o = [None] * 3 if old is None else forces(old) if old else [0, 0, 0]
    return [cell(v, ov) if v is not None else ('', None) for v, ov in zip(f, o)]

def island_views(cur, prev, humans, alarms):
    """One panel per island of the human players: what is short, what the residents ask for, what is
    overproduced, what is shipped out and brought in. Built from the same data as the alarms."""
    old = {(r['session'], e['area']): e for r in prev or [] for e in r['areas']}
    by_place = collections.defaultdict(list)
    for a in alarms:
        if a['kind'] == 'island': by_place[a['place']].append(a)
    out = []
    for r in cur:
        label = r['session']
        for e in sorted((e for e in r['areas'] if e.get('owner') in humans), key=lambda e: -(e.get('population') or 0)):
            place, pe = f'{city_of(e)} ({label})', old.get((label, e['area'])) or {}
            st, tr, made = e.get('storage') or {}, e.get('stock_trends') or {}, e.get('output_capacity') or {}
            lims, ost = e.get('storage_limits_estimate') or [], pe.get('storage') or {}
            sup, rec, built = e.get('supplies') or {}, e.get('receives') or {}, e.get('buildings') or {}
            stock = lambda g: cell(st.get(g, 0), ost.get(g, 0) if prev else None)
            trend = lambda g: cell((tr.get(g) or {}).get('net_per_minute'), nd=2)
            full = lambda g: at_limit(st.get(g, 0), tr.get(g), lims)
            wf = e.get('workforce_saturation') or {}
            facts = [x for x in (e.get('city_status'),
                                 f"{e['population']:,} residents" if e.get('population') else None,
                                 f"income {e['income_per_minute']:,}/min" if e.get('income_per_minute') is not None else None,
                                 f"workforce {min(wf.values()):.0%}" if wf else None) if x]
            if e.get('residents'):
                facts.append(', '.join(f'{k} {v:,}' for k, v in e['residents'].items()))
            tables = []
            # residents' needs: is each good stocked, made here or brought in; is each service built
            rows = []
            for g, nd in sorted((e.get('needs') or {}).items(), key=lambda kv: (kv[1].get('kind') != 'good',
                                                                                 kv[1].get('category') or '', kv[0])):
                if nd.get('kind') == 'service':
                    ok = g in built
                    rows.append([g, 'ok' if ok else 'not built', ('', None), ('', None),
                                 'built here' if ok else 'not built here', nd.get('category') or '', ', '.join(nd['houses'])])
                    continue
                src = ('produced here' if made.get(g) else '') + (
                    ('; ' if made.get(g) else '') + 'from ' + ', '.join(rec[g]) if rec.get(g) else '')
                t, n = tr.get(g) or {}, st.get(g, 0)
                supplied = made.get(g) or rec.get(g)
                state = ('out of stock' if not n and supplied else 'not supplied' if not n
                         else 'running low' if t.get('net_per_minute', 0) < 0 and t.get('first', n) - n >= 10
                         else 'at limit' if full(g) else 'ok')
                rows.append([g + (' (buff only)' if nd.get('buff_only') else ''), state, stock(g), trend(g),
                             src or 'nobody brings it', nd.get('category') or '', ', '.join(nd['houses'])])
            rank = {'out of stock': 0, 'running low': 1, 'not built': 2, 'not supplied': 3, 'ok': 4, 'at limit': 5}
            rows.sort(key=lambda x: rank.get(x[1], 9))
            if rows:
                tables.append({'title': "Residents' needs", 'rows': rows,
                               'headers': ['Need', 'State', 'Stock', 'Trend/min', 'Supply', 'Category', 'For'],
                               'note': 'From the game\'s needs list of each house type present, which also holds '
                                       'optional needs and needs not unlocked yet: "not supplied" may be fine, '
                                       '"out of stock" (made here or shipped in, but none left) is not. Amounts per '
                                       'minute are not shown (their unit is not confirmed).'})
            # overproduction: building types idle because their output is not collected
            rows = []
            for row in e.get('production', []):
                if not row.get('idle_output_full'): continue
                outs = row.get('capacity_per_minute') or {}
                rows.append([row['building'], (f"{row['idle_output_full']} / {row['count']}", row['idle_output_full']),
                             ', '.join(f'{g} {v:g}/min' for g, v in outs.items()),
                             ', '.join(f'{g} {st.get(g, 0)}' + (' (limit)' if full(g) else '') for g in outs),
                             ', '.join(sorted({c for g in outs for c in sup.get(g, [])})) or 'not shipped'])
            if rows:
                rows.sort(key=lambda x: -x[1][1])
                tables.append({'title': 'Overproduced', 'rows': rows,
                               'headers': ['Building', 'Idle (output full)', 'Makes at 100%', 'Stock', 'Shipped to'],
                               'note': 'Buildings stopped because nobody collects their output: pause some, or '
                                       'ship the goods where they are needed.'})
            for title, flows, who in (('Exported', sup, 'To'), ('Imported', rec, 'From')):
                rows = [[g, ('at limit' if full(g) else 'out of stock' if not st.get(g) else ''), stock(g), trend(g),
                         ', '.join(c)] for g, c in sorted(flows.items())]
                if rows:
                    tables.append({'title': title, 'rows': rows, 'headers': ['Good', 'State', 'Stock', 'Trend/min', who]})
            out.append({'id': f"{label}-{e['area']}", 'label': place, 'prov': label, 'facts': facts,
                        'alarms': by_place.get(place, []), 'tables': tables})
    return out

def report_data(cur, prev, humans, horizon=60, _inner=False):
    """Alarms and tables of the report, as plain data (rendered as Markdown or HTML)."""
    prev_s = {r['session']: r for r in prev or []}
    alarms, sections, seen_routes = [], [], set()
    is_human = lambda o: o in humans
    kinds = {k: v for r in cur + (prev or []) for k, v in (r.get('participant_kinds') or {}).items()}
    npc = lambda o: kinds.get(str(o)) == 'npc'          # traders and envoys: left out of players and forces
    def alarm(level, kind, place, text, hint='', key=None, provs=(), vs_prev=False):
        code = f"{kind}|{key or place}|{re.sub(r'[-+~0-9.,]+', '#', text)}"   # stable across saves: no numbers
        alarms.append({'level': level, 'kind': kind, 'place': place, 'key': key or place, 'text': text, 'hint': hint,
                       'code': code, 'provs': list(provs), 'vs_prev': vs_prev})   # vs_prev: a change between saves
    details = {}                                    # (kind, place) -> line under the place's name
    area_at = {(x['session'], e['area']): e for x in cur for e in x['areas']}
    for r in cur:
        pr = prev_s.get(r['session'])
        old_area = {e['area']: e for e in (pr or {}).get('areas', [])}
        label = r['session']
        mine = sorted((e for e in r['areas'] if is_human(e.get('owner'))), key=lambda e: -(e.get('population') or 0))
        # ---- islands of the human players
        for e in mine:
            place, pe = f'{city_of(e)} ({label})', old_area.get(e['area'])
            isl = lambda level, text, hint='', place=place, d=False: alarm(level, 'island', place, text, hint,
                                                                          provs=[label], vs_prev=d)
            details[('island', place)] = ', '.join(x for x in (e.get('city_status'), f"{e['population']:,} residents"
                                                     if e.get('population') else None) if x)
            for wf, sat in (e.get('workforce_saturation') or {}).items():
                if sat < 0.9: isl('medium', f'{wf}: only {sat:.0%} of the demand is met',
                                  'build houses for this workforce or close buildings that need it')
            total, idle = production_counts(e)
            full, other = e.get('idle_output_full', 0), e.get('idle_other_reasons') or {}
            if sum(other.values()) >= 3:
                why = ', '.join(f'{k} ×{n}' for k, n in list(other.items())[:3])
                isl('medium', f'{sum(other.values())} production buildings stopped: {why}',
                    'missing inputs or paused buildings: check their supply chain')
            if total >= 4 and full >= total / 2:
                isl('info', f'overproduction: {full} of {total} production buildings idle, nobody collects their output',
                    'pause or demolish the surplus, or ship it where it is needed')
            st, made = e.get('storage') or {}, e.get('output_capacity') or {}
            for g, t in sorted((e.get('stock_trends') or {}).items()):
                stock, net = st.get(g, 0), t['net_per_minute']
                drop = t.get('first', stock - net * t['minutes']) - stock
                if stock > 0 and net <= -0.1 and drop >= max(10, 0.2 * t['max']) and stock / -net < horizon:
                    mins = stock / -net
                    src, dst = (e.get('receives') or {}).get(g), (e.get('supplies') or {}).get(g)
                    isl('high' if mins < horizon / 3 else 'medium',
                        f'{g}: {stock} left, down {drop:g} in {t["minutes"]:g} min, empty in ~{mins:.0f} min',
                        f'arrives from {", ".join(src)}: add ships or raise the amount on that route' if src else
                        f'shipped to {", ".join(dst)}: lower the amount on that route or build more' if dst else
                        'produced here: build more' if g in made else 'nothing brings it here: add a route or build production')
                elif not stock and t['max'] >= 10 and t['outflow_per_minute'] and g not in made:
                    isl('medium', f'{g}: ran out in the last {t["minutes"]:g} min (it was {t["max"]})',
                        'consumed here but not produced: check the routes that should bring it')
            if pe:
                p0, p1 = pe.get('population') or 0, e.get('population') or 0
                if p0 - p1 >= max(50, p0 * 0.02):
                    isl('high', f'population fell from {p0:,} to {p1:,}', 'look for unmet needs or missing goods above', d=True)
                i0, i1 = pe.get('income_per_minute'), e.get('income_per_minute')
                if i0 and i1 is not None and i0 - i1 > abs(i0) * 0.1:
                    isl('medium', f'income fell from {i0:,} to {i1:,} per minute', d=True)
        for e in r['areas']:
            pe = old_area.get(e['area'])
            if pe and is_human(pe.get('owner')) and not is_human(e.get('owner')):
                alarm('high', 'island', f'{city_of(pe)} ({label})', f'lost: now owned by {e.get("owner_name") or "nobody"}',
                      provs=[label], vs_prev=True)
            if pe and is_human(e.get('owner')) and not is_human(pe.get('owner')):
                alarm('info', 'island', f'{city_of(e)} ({label})', f'new island, was {pe.get("owner_name") or "unowned"}',
                      provs=[label], vs_prev=True)
        # ---- trade routes of the human players
        routes = [rt for rt in r.get('trade_routes', []) if is_human(rt['owner'])]
        for rt in routes:
            if rt['id'] in seen_routes: continue
            seen_routes.add(rt['id'])
            place, key = rt['name'] or f"route {rt['id']}", f"route {rt['id']}"
            stops = [(st, area_at.get((st['session'], st['area']), {})) for st in rt['stations']]
            details[('route', key)] = (' → '.join(st['city'] or f"area {st['area']}" for st, _ in stops)
                                         + f' · {len(rt["ships"])} ship' + ('' if len(rt['ships']) == 1 else 's'))
            rprov = sorted({st['session'] for st in rt['stations'] if st.get('session')})
            ra = lambda level, text, hint='', place=place, key=key, rprov=rprov: alarm(level, 'route', place, text, hint, key, rprov)
            goods = sorted({g for st in rt['stations'] for g in st['goods']})
            if not rt['ships']: ra('high', 'no ships assigned', 'assign ships or delete the route')
            if not goods: ra('medium', 'no goods set at any stop', 'the ships sail empty: set goods or delete the route')
            known = all(e and is_human(e.get('owner')) for _, e in stops)   # else a stop's stock is unknown
            dry = [] if not known else [g for g in goods if not any((e.get('output_capacity') or {}).get(g) or (e.get('storage') or {}).get(g)
                                               for _, e in stops)]
            if goods and len(dry) == len(goods):
                ra('medium', f'none of its goods ({", ".join(dry)}) is produced or stocked at any stop',
                   'the ships carry nothing: fix the source or reuse the ships')
            elif dry:
                ra('medium', f'{", ".join(dry)}: not produced or stocked at any stop', 'nothing to load for these goods')
            at_lim = []
            for g in goods:
                caps = [((e.get('output_capacity') or {}).get(g, 0), i) for i, (_, e) in enumerate(stops)]
                src = max(caps)[1] if caps and max(caps)[0] > 0 else None
                full = [f'{st["city"] or st["area"]} ({(e.get("storage") or {}).get(g, 0)})' for i, (st, e) in enumerate(stops)
                        if i != src and g in st['goods'] and e and at_limit((e.get('storage') or {}).get(g, 0),
                           (e.get('stock_trends') or {}).get(g), e.get('storage_limits_estimate') or [])]
                if full and src is not None: at_lim.append(f'{g} at {", ".join(full)}')
            if at_lim:                      # one line per route, however many goods
                ra('info', f'{len(at_lim)} good{"s" if len(at_lim) > 1 else ""} already at the storage limit: '
                           + '; '.join(at_lim),
                   'if the route unloads them there, nothing is delivered: lower the amounts or reuse the ships')
        # ---- tables
        tables = []
        rows = []
        for e in mine:
            pe = old_area.get(e['area']) or {}
            total, idle = production_counts(e)
            wf = e.get('workforce_saturation') or {}
            st, lims, tr = e.get('storage') or {}, e.get('storage_limits_estimate') or [], e.get('stock_trends') or {}
            full = sum(at_limit(v, tr.get(g), lims) for g, v in st.items())
            other = sum((e.get('idle_other_reasons') or {}).values())
            rows.append([city_of(e), e.get('city_status') or '', cell(e.get('population'), pe.get('population')),
                         cell(e.get('income_per_minute'), pe.get('income_per_minute')),
                         cell(min(wf.values()), min((pe.get('workforce_saturation') or {'': None}).values()), pct=True) if wf else ('', None),
                         (f'{total - idle} / {total}', (total - idle) / total if total else -1),
                         cell(e.get('idle_output_full', 0)), cell(other),
                         (f'{full} / {len(st)}', full), (str(len(e.get('trade_routes') or {})), len(e.get('trade_routes') or {}))])
        if rows:
            tables.append({'title': 'Your islands', 'headers': ['City', 'Status', 'Population', 'Income/min',
                           'Workforce (lowest)', 'Production active', 'Idle: output full', 'Idle: other', 'Goods at limit', 'Routes'],
                           'rows': rows, 'note': 'Idle: output full = nobody collects the output (overproduction). '
                           'Goods at limit: stock at a storage limit, estimated from the last 20 minutes of the warehouse chart.'})
        owners = collections.defaultdict(lambda: {'n': 0, 'pop': 0, 'inc': 0, 'name': None})
        for e in r['areas']:
            if e.get('owner') is None or npc(e['owner']): continue
            o = owners[e['owner']]; o['n'] += 1; o['pop'] += e.get('population') or 0
            o['inc'] += e.get('income_per_minute') or 0; o['name'] = e.get('owner_name') or str(e['owner'])
        old_owners, old_inc = collections.Counter(), collections.Counter()
        for e in (pr or {}).get('areas', []):
            if e.get('owner') is not None:
                old_owners[e['owner']] += e.get('population') or 0
                old_inc[e['owner']] += e.get('income_per_minute') or 0
        parts = {p['participant']: p for p in r['participants']}
        old_parts = {p['participant']: p for p in (pr or {}).get('participants', [])}
        rows = []
        for o, x in sorted(owners.items(), key=lambda kv: -kv[1]['pop']):
            p, op = parts.get(o) or {}, old_parts.get(o)
            rows.append([x['name'] + (' (you)' if is_human(o) else ''), (str(x['n']), x['n']),
                         cell(x['pop'], old_owners[o] if pr else None), cell(x['inc'], old_inc[o] if pr else None),
                         *force_cells(p, (op or {}) if pr else None)])
        if rows:
            tables.append({'title': 'Players', 'headers': ['Owner', 'Islands', 'Population', 'Income/min',
                                                           'Warships', 'Civil ships', 'Troops'], 'rows': rows})
        rows = []
        for p in r['participants']:                 # variants with the same name (e.g. AI ships) are merged
            if npc(p['participant']): continue
            now, before = fleet(p), fleet(old_parts.get(p['participant']) or {})
            keys = set(now) | (set(before) if is_human(p['participant']) else set())
            for role, t in sorted(keys, key=lambda k: (k[0] != 'war', k[1], str(k[0]))):
                rows.append([p['name'] or 'unknown owner', ROLE_LABEL.get(role, role), t,
                             cell(now[(role, t)], before[(role, t)] if pr else None)])
        if rows: tables.append({'title': 'Fleets', 'headers': ['Owner', 'Role', 'Ship type', 'Ships'], 'rows': rows,
                                'note': 'Warships carry weapon modules (archers, catapult, scorpion, torchers) or are '
                                        'military by design; the others are civil (cargo, river boats).'})
        rows = []
        for p in r['participants']:
            if npc(p['participant']): continue
            now, before = troops(p), troops(old_parts.get(p['participant']) or {})
            for t in sorted(set(now) | (set(before) if is_human(p['participant']) else set())):
                rows.append([p['name'] or 'unknown owner', t, cell(now[t], before[t] if pr else None)])
        if rows: tables.append({'title': 'Troops', 'headers': ['Owner', 'Unit', 'Count'], 'rows': rows})
        rows = []
        for rt in routes:
            stops = ' → '.join(st['city'] or f"area {st['area']}" for st in rt['stations'])
            goods = sorted({g for st in rt['stations'] for g in st['goods']})
            rows.append([rt['name'] or '', (str(len(rt['ships'])), len(rt['ships'])), stops, ', '.join(goods)])
        if rows: tables.append({'title': 'Your trade routes', 'headers': ['Route', 'Ships', 'Stops', 'Goods'], 'rows': rows})
        for e in mine:
            st, tr, lims, made = e.get('storage') or {}, e.get('stock_trends') or {}, e.get('storage_limits_estimate') or [], e.get('output_capacity') or {}
            ost = (old_area.get(e['area']) or {}).get('storage') or {}
            rows = []
            for g in sorted(set(st) | set(tr), key=lambda g: (-st.get(g, 0), g)):
                t = tr.get(g) or {}
                state = 'at limit' if at_limit(st.get(g, 0), t, lims) else ''
                rows.append([g, cell(st.get(g, 0), ost.get(g, 0) if pr else None),
                             cell(t.get('net_per_minute'), nd=2), cell(t.get('outflow_per_minute'), nd=2),
                             cell(t.get('inflow_per_minute'), nd=2), cell(made.get(g), nd=2), state])
            if rows:
                tables.append({'title': f'Warehouse: {city_of(e)}', 'collapsed': True,
                               'headers': ['Good', 'Stock', 'Trend/min', 'Out/min', 'In/min', 'Production capacity/min', ''],
                               'rows': rows})
        t0, t1 = (pr or {}).get('session_time_ms'), r.get('session_time_ms')
        sections.append({'id': label, 'title': label.capitalize(), 'tables': tables,
                         'elapsed_min': (t1 - t0) / 60000 if t0 and t1 else None})
    # ---- all provinces together: forces per participant
    total, total_before, pname = {}, {}, {}
    for res, acc in ((cur, total), (prev or [], total_before)):
        for r in res:
            for p in r['participants']:
                if p['participant'] is None or npc(p['participant']): continue
                pname[p['participant']] = p['name']
                f = forces(p)
                a = acc.setdefault(p['participant'], [0, 0, 0, set()])
                for i in range(3): a[i] = None if a[i] is None or f[i] is None else a[i] + f[i]
                if any(f[:3]): a[3].add(r['session'])
    rows = []
    for o, a in sorted(total.items(), key=lambda kv: (not is_human(kv[0]), -(kv[1][0] or 0))):
        if not any(a[:3]): continue
        b = total_before.get(o) if prev else None
        rows.append([(pname[o] or str(o)) + (' (you)' if is_human(o) else '')]
                    + [cell(a[i], (b[i] if b else 0) if prev else None) for i in range(3)] + [', '.join(sorted(a[3]))])
        if is_human(o) and b:
            for i, what, level in ((0, 'warships', 'high'), (1, 'civil ships', 'medium'), (2, 'troops', 'high')):
                if a[i] is not None and b[i] is not None and a[i] < b[i]:
                    alarm(level, 'player', pname[o], f'{b[i] - a[i]} {what} fewer than in the previous save '
                                                     f'({b[i]} → {a[i]})', 'lost in battle, sold or disbanded', vs_prev=True)
    if rows:
        sections.insert(0, {'id': 'forces', 'title': 'All provinces', 'elapsed_min': None, 'tables': [{
            'title': 'Forces per player', 'headers': ['Player', 'Warships', 'Civil ships', 'Troops', 'Present in'],
            'rows': rows}]})
    order = {'high': 0, 'medium': 1, 'info': 2}
    resolved = 0
    if prev and not _inner:                 # NEW = not raised by the previous save; resolved = gone since
        before = {x['code'] for x in report_data(prev, None, humans, horizon, _inner=True)['alarms']}
        for x in alarms: x['new'] = not x['vs_prev'] and x['code'] not in before
        resolved = len(before - {x['code'] for x in alarms})
    alarms.sort(key=lambda x: (order[x['level']], not x.get('new')))
    groups = collections.OrderedDict()            # one card per island / route / player, worst first
    for x in alarms:
        g = groups.setdefault((x['kind'], x['key']), {'kind': x['kind'], 'place': x['place'], 'level': x['level'],
                                                       'detail': details.get((x['kind'], x['key']), ''),
                                                       'provs': x['provs'], 'items': []})
        g['items'].append(x)
    cards = sorted(groups.values(), key=lambda g: (order[g['level']], not any(i.get('new') for i in g['items']),
                                                   -sum(order[i['level']] < 2 for i in g['items'])))
    return {'save': cur[0].get('save'), 'previous': (prev or [{}])[0].get('save'), 'resolved': resolved,
            'alarms': alarms, 'groups': cards, 'sections': sections,
            'islands': [] if _inner else island_views(cur, prev, humans, alarms)}

def hints(card, seen=None):
    """(alarm, hint, repeated): a hint is written out the first time it appears (in the card, or in
    the page when `seen` is shared); later repeats are only flagged, to keep cards short."""
    seen = set() if seen is None else seen
    for a in card['items']:
        rep = a['hint'] in seen
        yield a, a['hint'], rep
        if a['hint']: seen.add(a['hint'])

KIND_LABEL = {'player': 'Empire', 'island': 'Island', 'route': 'Route'}

def md_cell(c):
    return str(c[0] if isinstance(c, tuple) else c).replace('|', '\\|')

def render_md(rep):
    out = [f"# Report: {rep['save']}", '']
    if rep['previous']: out += [f"Compared with **{rep['previous']}**: changes since then are in brackets; "
                                f"**NEW** marks alarms that were not there. Resolved since then: {rep.get('resolved', 0)}.", '']
    out += ['## Alarms', '']
    if not rep['groups']: out.append('No alarms.')
    for g in rep['groups']:
        out.append(f"**{g['place']}** ({KIND_LABEL.get(g['kind'], g['kind'])})" + (f" · {g['detail']}" if g['detail'] else ''))
        out += [f"- **{a['level']}**{' **NEW**' if a.get('new') else ''} {a['text']}" + (f" → *{hint}*" if hint and not again else '')
                for a, hint, again in hints(g)]
        out.append('')
    if rep.get('islands'): out += ['## Islands', '']
    for isl in rep.get('islands', []):
        out += [f"### {isl['label']}", '', ' · '.join(isl['facts']), '']
        out += [f"- **{a['level']}** {a['text']}" for a in isl['alarms']]
        if isl['alarms']: out.append('')
        for t in isl['tables']:
            out += [f"**{t['title']}**", '', '| ' + ' | '.join(t['headers']) + ' |', '|' + '---|' * len(t['headers'])]
            out += ['| ' + ' | '.join(md_cell(c) for c in row) + ' |' for row in t['rows']]
            out.append('')
    for s in rep['sections']:
        out += ['', f"## {s['title']}", '']
        if s['elapsed_min'] is not None: out += [elapsed_text(s['elapsed_min']), '']
        for t in s['tables']:
            out += [f"### {t['title']}", '']
            if t.get('note'): out += [t['note'], '']
            out += ['| ' + ' | '.join(t['headers']) + ' |', '|' + '---|' * len(t['headers'])]
            out += ['| ' + ' | '.join(md_cell(c) for c in row) + ' |' for row in t['rows']]
            out.append('')
    return '\n'.join(out) + '\n'

def elapsed_text(m):
    return (f'Game time in this province since the previous save: {m:.1f} min (each province keeps its own clock).'
            + (' The compared save is the newer one.' if m < 0 else ''))

HTML_STYLE = """
:root{--bg:#14130f;--panel:#1e1d18;--text:#ece8de;--muted:#a39d90;--line:#36342b;--accent:#e08a57;
--high:#ff8a80;--high-bg:#3a1714;--medium:#f2c14e;--medium-bg:#352a0e;--info:#8cbcf5;--info-bg:#132739;--new:#7ee2a8}
:root[data-theme=light]{--bg:#f6f4ef;--panel:#fff;--text:#1d1b16;--muted:#6b665c;--line:#e2ddd2;--accent:#8a3b12;
--high:#b42318;--high-bg:#fde8e6;--medium:#9a6700;--medium-bg:#fff4d6;--info:#1f5fa8;--info-bg:#e6f0fb;--new:#127a3f}
*{box-sizing:border-box}html{scroll-padding-top:64px}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:8px 12px 40px}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:6px 12px;
display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.bar .title{font-weight:700}.bar .save{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:40ch}
.bar .grow{flex:1}.age{font-size:12px;color:var(--muted)}.age.stale{color:var(--medium);font-weight:600}
button{font:inherit;font-size:12px;color:var(--text);background:var(--panel);border:1px solid var(--line);
border-radius:999px;padding:2px 10px;cursor:pointer}button[aria-pressed=false]{opacity:.45}
button.lvl.high{color:var(--high)}button.lvl.medium{color:var(--medium)}button.lvl.info{color:var(--info)}
button.prov[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
h2{font-size:17px;margin:0;color:var(--accent);display:inline}.sec{margin-top:18px}
.sec>summary{cursor:pointer;padding:6px 0;list-style-position:inside}
h3{font-size:14px;margin:14px 0 6px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:8px;margin-top:8px}
.card{background:var(--panel);border:1px solid var(--line);border-left-width:4px;border-radius:6px;padding:6px 10px}
.card[data-level=high]{border-left-color:var(--high)}.card[data-level=medium]{border-left-color:var(--medium)}
.card[data-level=info]{border-left-color:var(--info)}
.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.place{font-weight:700}
.kind{font-size:11px;text-transform:uppercase;color:var(--muted)}.detail{font-size:12px;color:var(--muted)}
.card ul{list-style:none;padding:0;margin:4px 0 0;display:grid;gap:4px}
.card li{padding-left:12px;position:relative}.card li::before{content:"";position:absolute;left:0;top:.55em;
width:6px;height:6px;border-radius:50%;background:var(--info)}
.card li[data-level=high]::before{background:var(--high)}.card li[data-level=medium]::before{background:var(--medium)}
.hint{font-size:12px;color:var(--muted)}.new{font-size:10px;font-weight:700;color:var(--new);border:1px solid var(--new);
border-radius:4px;padding:0 4px;margin-right:4px}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:4px 8px;text-align:left;
border-bottom:1px solid var(--line);white-space:nowrap}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
th{cursor:pointer;user-select:none;background:var(--panel)}tr:last-child td{border-bottom:0}
td:first-child,th:first-child{position:sticky;left:0;background:var(--panel)}
details{margin:6px 0}summary{cursor:pointer;font-weight:600}img{max-width:100%;border-radius:6px;border:1px solid var(--line)}
.note{font-size:12px;color:var(--muted);margin:4px 0}
select{font:inherit;font-size:12px;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:2px 6px;max-width:100%}.place[data-island]{cursor:pointer;text-decoration:underline dotted}
.isl h2{display:block;margin:6px 0 2px}.facts{color:var(--muted);font-size:12px;margin-bottom:6px}
.isl ul.al{list-style:none;padding:0;margin:6px 0;display:grid;gap:3px}.isl ul.al li{padding-left:12px;position:relative}
.isl ul.al li::before{content:"";position:absolute;left:0;top:.55em;width:6px;height:6px;border-radius:50%;background:var(--info)}
.isl ul.al li[data-level=high]::before{background:var(--high)}.isl ul.al li[data-level=medium]::before{background:var(--medium)}
td.st-out,td.st-low,td.st-nb{color:var(--high);font-weight:600}td.st-ns{color:var(--muted)}td.st-lim{color:var(--info)}
footer{margin:30px 0;font-size:12px}kbd{font-size:11px;
border:1px solid var(--line);border-radius:3px;padding:0 3px}
"""

HTML_SCRIPT = r"""
const S={get(k,d){try{const v=localStorage.getItem('anno117:'+k);return v===null?d:JSON.parse(v)}catch(e){return d}},
set(k,v){try{localStorage.setItem('anno117:'+k,JSON.stringify(v))}catch(e){}}};
const R=document.documentElement,B=window.__REPORT;
R.dataset.theme=S.get('theme','dark');
let hidden=new Set(S.get('hidden',['info'])),prov=S.get('prov','all');
const cards=[...document.querySelectorAll('.card')],secs=[...document.querySelectorAll('.sec[data-prov]')];
function apply(){
  const n={high:0,medium:0,info:0};
  cards.forEach(c=>{const ok=prov==='all'||!c.dataset.prov||c.dataset.prov.split(' ').includes(prov);let vis=0;
    c.querySelectorAll('li').forEach(li=>{const l=li.dataset.level;if(ok)n[l]++;const v=ok&&!hidden.has(l);li.hidden=!v;vis+=v});
    c.hidden=!vis});
  secs.forEach(s=>s.hidden=!(prov==='all'||s.dataset.prov===prov));
  document.querySelectorAll('button.lvl').forEach(b=>{const l=b.dataset.level;b.setAttribute('aria-pressed',!hidden.has(l));
    b.textContent=n[l]+' '+l});
  document.querySelectorAll('button.prov').forEach(b=>b.setAttribute('aria-pressed',b.dataset.prov===prov));
  const empty=document.getElementById('none');if(empty)empty.hidden=cards.some(c=>!c.hidden);
  document.title=(n.high?'('+n.high+') ':'')+'Anno 117 report';
}
function toggle(l){hidden.has(l)?hidden.delete(l):hidden.add(l);S.set('hidden',[...hidden]);apply()}
document.querySelectorAll('button.lvl').forEach(b=>b.onclick=()=>toggle(b.dataset.level));
document.querySelectorAll('button.prov').forEach(b=>b.onclick=()=>{prov=b.dataset.prov;S.set('prov',prov);apply()});
document.getElementById('theme').onclick=()=>{R.dataset.theme=R.dataset.theme==='dark'?'light':'dark';S.set('theme',R.dataset.theme)};
const maps=()=>{const d=[...document.querySelectorAll('details.map')],o=!d.some(x=>x.open);d.forEach(x=>x.open=o);S.set('maps',o)};
document.getElementById('maps').onclick=maps;
if(S.get('maps',false))document.querySelectorAll('details.map').forEach(x=>x.open=true);
document.querySelectorAll('details[id]').forEach(d=>{const k='open:'+d.id,v=S.get(k,null);if(v!==null)d.open=v;
  d.addEventListener('toggle',()=>S.set(k,d.open))});
document.querySelectorAll('table').forEach(t=>t.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{
  const b=t.tBodies[0],rows=[...b.rows],dir=th.dataset.dir=th.dataset.dir==='asc'?'desc':'asc';
  const key=r=>{const c=r.cells[i];return c.dataset.v!==undefined?parseFloat(c.dataset.v):c.textContent.toLowerCase()};
  rows.sort((x,y)=>{const a=key(x),c=key(y);return (a>c?1:a<c?-1:0)*(dir==='asc'?1:-1)});rows.forEach(r=>b.appendChild(r))}));
function age(){const m=Math.round((Date.now()-B.generated)/60000),e=document.getElementById('age');
  e.textContent='updated '+new Date(B.generated).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+' · '+m+' min ago';
  e.classList.toggle('stale',m>=B.stale)}
age();setInterval(age,30000);
function go(d){const v=cards.filter(c=>!c.hidden);if(!v.length)return;const y=window.scrollY+70;
  let i=d>0?v.findIndex(c=>c.getBoundingClientRect().top+window.scrollY>y+1):v.length-1-[...v].reverse().findIndex(c=>c.getBoundingClientRect().top+window.scrollY<y-1);
  if(i<0||i>=v.length)i=d>0?v.length-1:0;v[i].scrollIntoView({block:'start'})}
document.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey||/input|select|textarea/i.test(e.target.tagName))return;
  const k=e.key;if(k==='1')toggle('high');else if(k==='2')toggle('medium');else if(k==='3')toggle('info');
  else if(k==='j')go(1);else if(k==='k')go(-1);else if(k==='m')maps();else if(k==='g')scrollTo(0,0);
  else if(k==='t')document.getElementById('theme').click();else if(k==='r')location.reload()});
try{const y=sessionStorage.getItem('anno117:y');if(y!==null){sessionStorage.removeItem('anno117:y');scrollTo(0,+y)}}catch(e){}
if(B.stamp){let wait=5000;const poll=()=>{const s=document.createElement('script');
  s.src=B.stamp+'?t='+Date.now();
  s.onload=()=>{s.remove();if(window.__annoStamp&&window.__annoStamp!==B.build){
    try{sessionStorage.setItem('anno117:y',String(window.scrollY))}catch(e){}location.reload()}else setTimeout(poll,wait)};
  s.onerror=()=>{s.remove();setTimeout(poll,30000)};document.head.appendChild(s)};setTimeout(poll,wait)}
const VI=document.getElementById('v-island'),VA=document.getElementById('v-alarms'),SEL=document.getElementById('isl'),VB=document.getElementById('view');
function showIsland(id){if(!SEL)return;if(id&&[...SEL.options].some(o=>o.value===id))SEL.value=id;S.set('island',SEL.value);
  document.querySelectorAll('.isl').forEach(x=>x.hidden=x.dataset.id!==SEL.value)}
function view(v){if(!VI)return;VI.hidden=v!=='island';VA.hidden=v==='island';VB.textContent=v==='island'?'alarms':'island';
  VB.title=v==='island'?'back to the alarms (i)':'island view (i)';S.set('view',v);scrollTo(0,0)}
if(SEL){showIsland(S.get('island',SEL.value));SEL.onchange=()=>{showIsland(SEL.value);view('island')};
  VB.onclick=()=>view(VI.hidden?'island':'alarms');view(S.get('view','alarms'));
  document.querySelectorAll('.place[data-island]').forEach(p=>p.onclick=()=>{showIsland(p.dataset.island);view('island')});
  document.addEventListener('keydown',e=>{if(e.key==='i'&&!/input|select|textarea/i.test(e.target.tagName))view(VI.hidden?'island':'alarms')})}
apply();
"""

def render_html(rep, images, stamp=None):
    import base64, html
    h = html.escape
    def td(c):
        if isinstance(c, tuple):
            v = '' if c[1] is None else f' data-v="{c[1]}"'
            return f'<td class="num"{v}>{h(c[0])}</td>'
        return f'<td>{h(str(c))}</td>'
    def table(t, sid):
        nums = [any(isinstance(r[i], tuple) for r in t['rows']) for i in range(len(t['headers']))]
        head = ''.join(f'<th class="num">{h(x)}</th>' if n else f'<th>{h(x)}</th>' for x, n in zip(t['headers'], nums))
        body = ''.join('<tr>' + ''.join(td(c) for c in r) + '</tr>' for r in t['rows'])
        note = f'<p class="note">{h(t["note"])}</p>' if t.get('note') else ''
        inner = f'{note}<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        if t.get('collapsed'):
            did = h(re.sub(r'\W+', '-', f'{sid}-{t["title"]}'))
            return f'<details id="{did}"><summary>{h(t["title"])} ({len(t["rows"])})</summary>{inner}</details>'
        return f'<h3>{h(t["title"])}</h3>{inner}'
    provs = [s['id'] for s in rep['sections'] if s['id'] != 'forces']
    meta = {'build': rep.get('build', ''), 'generated': rep.get('generated', 0), 'stale': rep.get('stale_minutes', 45),
            'stamp': stamp}
    bar = ('<div class="bar"><span class="title">Anno 117</span>'
           f'<span class="save" title="{h(rep["save"] or "")}">{h(rep["save"] or "")}</span>'
           + ''.join(f'<button class="lvl {l}" data-level="{l}" title="show/hide ({k})">{l}</button>'
                     for l, k in (('high', 1), ('medium', 2), ('info', 3)))
           + '<button class="prov" data-prov="all">all</button>'
           + ''.join(f'<button class="prov" data-prov="{h(p)}">{h(p)}</button>' for p in provs)
           + '<span class="grow"></span><span class="age" id="age"></span>'
           + ('<button id="view" title="island view (i)">island</button><select id="isl" aria-label="island">'
              + ''.join(f'<option value="{h(i["id"])}">{h(i["label"])}</option>' for i in rep.get('islands', []))
              + '</select>' if rep.get('islands') else '')
           + '<button id="maps" title="maps (m)">maps</button><button id="theme" title="theme (t)">◐</button></div>')
    out = ['<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f'<title>Anno 117 report</title><style>{HTML_STYLE}</style>',
           f'<script>window.__REPORT={json.dumps(meta)}</script></head><body>', bar, '<main>']
    sub = []
    if rep['previous']:
        sub.append(f'compared with {h(rep["previous"])} · resolved since then: {rep.get("resolved", 0)}')
    sub.append('keys: <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> levels · <kbd>j</kbd>/<kbd>k</kbd> next/previous · '
               '<kbd>i</kbd> island view · <kbd>m</kbd> maps · <kbd>g</kbd> top · <kbd>t</kbd> theme')
    out.append(f'<p class="muted" style="font-size:12px;margin:2px 0">{" · ".join(sub)}</p>')
    island_of = {i['label']: i['id'] for i in rep.get('islands', [])}
    if rep.get('islands'):
        state = {'out of stock': 'st-out', 'running low': 'st-low', 'not built': 'st-ns', 'not supplied': 'st-ns',
                 'at limit': 'st-lim', 'missing': 'st-out'}
        def isl_table(t):
            nums = [any(isinstance(r[i], tuple) for r in t['rows']) for i in range(len(t['headers']))]
            head = ''.join(f'<th class="num">{h(x)}</th>' if n else f'<th>{h(x)}</th>' for x, n in zip(t['headers'], nums))
            sc = t['headers'].index('State') if 'State' in t['headers'] else -1
            body = ''.join('<tr>' + ''.join(f'<td class="{state.get(c, "")}">{h(str(c))}</td>' if i == sc else td(c)
                                            for i, c in enumerate(r)) + '</tr>' for r in t['rows'])
            note = f'<p class="note">{h(t["note"])}</p>' if t.get('note') else ''
            return f'<h3>{h(t["title"])} ({len(t["rows"])})</h3>{note}<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        out.append('<div id="v-island" hidden>')
        for i in rep['islands']:
            al = ''.join(f'<li data-level="{a["level"]}">' + ('<span class="new">NEW</span>' if a.get('new') else '')
                         + h(a['text']) + '</li>' for a in i['alarms'])
            out.append(f'<section class="isl" data-id="{h(i["id"])}" hidden><h2>{h(i["label"])}</h2>'
                       f'<div class="facts">{h(" · ".join(i["facts"]))}</div>'
                       + (f'<ul class="al">{al}</ul>' if al else '<p class="muted">No alarms on this island.</p>')
                       + ''.join(isl_table(t) for t in i['tables']) + '</section>')
        out.append('</div>')
    out.append('<div id="v-alarms">')
    out.append('<div class="cards">')
    seen = set()
    for g in rep['groups']:
        items = ''.join(
            f'<li data-level="{a["level"]}"' + (f' title="{h(hint)}"' if hint and again else '') + '>'
            + ('<span class="new">NEW</span>' if a.get('new') else '') + h(a['text'])
            + (f'<div class="hint">{h(hint)}</div>' if hint and not again else '') + '</li>'
            for a, hint, again in hints(g, seen))
        out.append(f'<section class="card" data-level="{g["level"]}" data-prov="{h(" ".join(g.get("provs", [])))}">'
                   f'<div class="head"><span class="place"'
                   + (f' data-island="{h(island_of[g["place"]])}" title="open the island view"' if g['place'] in island_of else '')
                   + f'>{h(g["place"])}</span>'
                   f'<span class="kind">{h(KIND_LABEL.get(g["kind"], g["kind"]))}</span>'
                   + (f'<span class="detail">{h(g["detail"])}</span>' if g['detail'] else '')
                   + f'</div><ul>{items}</ul></section>')
    out.append('</div><p id="none" class="muted" hidden>No alarms with the current filters.</p>')
    for s in rep['sections']:
        prov = '' if s['id'] == 'forces' else f' data-prov="{h(s["id"])}"'
        out.append(f'<details class="sec" id="sec-{h(s["id"])}" open{prov}><summary><h2>{h(s["title"])}</h2></summary>')
        if s['elapsed_min'] is not None: out.append(f'<p class="muted">{h(elapsed_text(s["elapsed_min"]))}</p>')
        img = images.get(s['id'])
        if img:
            with open(img, 'rb') as f:
                out.append(f'<details class="map"><summary>Map</summary><img loading="lazy" decoding="async" '
                           f'alt="Map of {h(s["title"])}" src="data:image/png;base64,{base64.b64encode(f.read()).decode()}"></details>')
        out += [table(t, s['id']) for t in s['tables']]
        out.append('</details>')
    out.append('</div>')
    out.append('<footer class="muted">Made with anno117.py report. Trends come from the warehouse chart stored '
               'in the save (last 20 minutes); storage limits are estimated.</footer></main>')
    out.append(f'<script>{HTML_SCRIPT}</script></body></html>')
    return '\n'.join(out)

def write_atomic(path, text):
    """Write next to the target and rename, so a page reloading meanwhile never reads half a file."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: f.write(text)
    os.replace(tmp, path)

def cmd_report(a):
    import time
    cur = load_results(a.current, a)
    prev = load_results(a.compare, a) if a.compare else None
    humans = set()
    for w in a.player or []:
        humans |= {p['participant'] for r in cur for p in r['participants']
                   if str(p['participant']) == w or (p['name'] or '').lower() == w.lower()}
        humans |= {e['owner'] for r in cur for e in r['areas'] if (e.get('owner_name') or '').lower() == w.lower()}
    if not a.player:
        humans = {h for r in cur for h in r.get('human_players', [])}
    if not humans: sys.exit('no human player found: name one with --player NAME_OR_GUID')
    rep = report_data(cur, prev, humans, a.horizon)
    if a.compare:
        rep['previous'] = os.path.basename(os.path.normpath(a.compare)) + (
            f" ({prev[0]['save']})" if os.path.isdir(a.compare) and prev[0].get('save') else '')
    now = time.time()
    rep['generated'], rep['build'], rep['stale_minutes'] = int(now * 1000), f'{now:.3f}', a.stale
    images = {}
    for s in rep['sections']:
        for f in (f"{s['id']}.png", f"{s['id']}_map.png"):
            if a.maps and os.path.exists(os.path.join(a.maps, f)): images[s['id']] = os.path.join(a.maps, f); break
    stamp = os.path.basename(a.output) + '.stamp.js'
    write_atomic(a.output, render_html(rep, images, stamp))
    # an open page polls this tiny file and reloads itself when the build id changes (works on file:// too)
    write_atomic(os.path.join(os.path.dirname(os.path.abspath(a.output)), stamp),
                 f'window.__annoStamp={json.dumps(rep["build"])};\n')
    if a.md: write_atomic(a.md, render_md(rep))
    lv = collections.Counter(x['level'] for x in rep['alarms'])
    print(f"{len(rep['alarms'])} alarms ({lv['high']} high, {lv['medium']} medium, {lv['info']} info, "
          f"{sum(1 for x in rep['alarms'] if x.get('new'))} new) -> {a.output}" + (f', {a.md}' if a.md else ''))

# ---------------- game data (assets) ----------------
# assets.xml = <Asset><Template/><Values><Standard><GUID/><Name/>...</Standard><Text><OasisId/></Text>...
# Anno 117: texts_<language>.xml = <Text><LineId/><Text/></Text>, where LineId = the asset's OasisId.
# Anno 1800 (same engine): <Text><GUID/><Text/></Text>. Files are matched by name, wherever they are.
RE_ASSETS = re.compile(r'(^|/)assets[^/]*\.xml$', re.I)
RE_PROPS = re.compile(r'(^|/)export/properties\.xml$', re.I)   # default values of components
RE_USEFUL = re.compile(r'ship|module|unit|soldier|troop|military|legion|warship|pirate|participant|'
                       r'session|weapon|siege|onager|fleet|army|naval|galley|boat|fertility|deposit|'
                       r'citystatus|infolayericon|workforce', re.I)

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
                    nl = el.find('Values/Residence7/NeedsList')
                    if nl is not None:   # what a house type asks for (rate: unit not confirmed, see docs)
                        a['needs'] = [{k: v for k, v in (('need', txt(i, 'Need')), ('rate', txt(i, 'NeedConsumptionRate')),
                                                         ('buff_only', txt(i, 'IsOnlyAvailableThroughBuff') == '1')) if v}
                                      for i in nl.findall('Item') if txt(i, 'Need')]
                    nd = el.find('Values/Need')
                    if nd is not None and txt(nd, 'NeedProduct'):
                        a['need'] = {k: v for k, v in (('product', txt(nd, 'NeedProduct')),
                                                       ('category', txt(nd, 'NeedCategoryType'))) if v}
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

CULTURE_WORDS = {'Tech', 'Need', 'Roman', 'Celtic', 'Egyptian', 'Greco', 'Greco-Roman'}

def need_category(name):
    """Category of a need asset that does not set NeedCategoryType, from its internal name:
    'Need Roman Celtic Public Market' -> 'Public'."""
    return next((w for w in (name or '').split() if w not in CULTURE_WORDS), None)

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
        for key in ('needs', 'need'):
            v = inherited(g, key)
            if v: x[key] = v
        if x.get('needs'):               # resolve each need to its good (or public building) right away
            for nd in x['needs']:
                info = (inherited(int(nd['need']), 'need') or {}) if str(nd.get('need', '')).isdigit() else {}
                if info.get('product'): nd['product'] = info['product']
                cat = info.get('category') or need_category(inherited(int(nd['need']), 'name'))
                if cat: nd['category'] = cat
        fac = factory(g)
        if fac:
            x['factory'] = fac
            if 'cycle' not in fac and default_cycle: fac['cycle'] = default_cycle
        if a.all or 'factory' in x or 'needs' in x or 'need' in x or RE_USEFUL.search(x.get('template') or '') or RE_USEFUL.search(x.get('name') or ''):
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

TEMPLATE_TITLES = {'LandUnit': 'Land units', 'NavalUnit': 'Ships', 'ShipModule': 'Ship modules',
                   'MilitaryTowerUnit': 'Defensive towers', 'Fertility': 'Fertilities and deposits',
                   'Product': 'Goods', 'CityStatus': 'City statuses'}

def cmd_guid_table(a):
    """Markdown page of a GUID map: one table per template, in the order given by --template
    (every template when none is given)."""
    m = read_json(a.names)
    if os.path.dirname(a.output): os.makedirs(os.path.dirname(a.output), exist_ok=True)
    esc = lambda s: str(s or '').replace('|', '\\|')
    rows = [(int(k), v if isinstance(v, dict) else {'name': v}) for k, v in m.items() if k.isdigit()]
    groups = collections.defaultdict(list)
    for g, v in sorted(rows, key=lambda r: ((r[1].get('name') or '').lower(), r[0])):
        groups[v.get('template') or '(no template)'].append((g, v))
    order = a.template or sorted(groups)
    total = sum(len(groups.get(t, [])) for t in order)
    lines = ['---', f'title: {a.title}', '---', '', f'# {a.title}', '',
             f'{total} GUIDs, generated with `anno117.py guid-table'
             + ''.join(f' --template {t}' for t in a.template or []) + '`. '
             'Internal names come from `assets.xml`, translated names from `texts_<lang>.xml` of the '
             'game files of the person who generated this page.', '']
    for t in order:
        if not groups.get(t): continue
        lines += [f'## {TEMPLATE_TITLES.get(t, t)} ({len(groups[t])})', '',
                  '| GUID | Internal name | Name |', '|---:|---|---|']
        lines += [f'| {g} | {esc(v.get("name"))} | {esc(v.get("name_local"))} |' for g, v in groups[t]]
        lines.append('')
    with open(a.output, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'{total} rows -> {a.output}')

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

MAP_SEA = (15, 47, 40)            # the colour the game's map images use for open water
MAP_FILLS = [(0, 0, 0), MAP_SEA, (34, 36, 40)]   # flat colours where a tile image has no terrain

def tile_alpha(img):
    """Transparency of a tile image: 0 on the flat fill colours (black, open water, dark grey) the
    images use where they hold no terrain, with isolated specks removed, so overlapping tiles do
    not cover each other with empty blocks."""
    from PIL import Image, ImageChops, ImageFilter
    out = None
    for c in MAP_FILLS:
        diff = ImageChops.difference(img, Image.new('RGB', img.size, c)).convert('L')
        a = diff.point(lambda v: 0 if v < 5 else 255 if v > 12 else (v - 5) * 36)
        out = a if out is None else ImageChops.darker(out, a)
    return out.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))

def grid_mask(grid, size):
    """ActiveMapGrid (one bit per 8x8 cell, rows padded to 64 bits) as a mask, smoothed when
    scaled up so the edges are not 8-pixel steps."""
    from PIL import Image
    X, Y, bits = grid; rb = len(bits) // Y
    m = Image.new('L', (X, Y), 0); px = m.load()
    for gy in range(Y):
        row = int.from_bytes(bits[gy * rb:(gy + 1) * rb], 'little')
        for gx in range(X):
            if row >> gx & 1: px[gx, Y - 1 - gy] = 255      # grid rows go north, image rows south
    return m.resize((size, size), Image.BILINEAR)

def compose_terrain(tiles):
    from PIL import Image, ImageChops, ImageFilter
    H = max(max(t['pos'][0] + t['size'][0], t['pos'][1] + t['size'][1]) for t in tiles)
    im = Image.new('RGB', (H, H), MAP_SEA)
    for t in tiles:
        S = t['size'][0]
        img = t['img'].resize((S, S), Image.LANCZOS)
        alpha = tile_alpha(img)
        if 'grid' in t: alpha = ImageChops.multiply(alpha, grid_mask(t['grid'], S))
        alpha = alpha.filter(ImageFilter.GaussianBlur(1.5))              # soft seams between tiles
        if t['rot']:
            img, alpha = img.rotate(90 * t['rot']), alpha.rotate(90 * t['rot'])
        x, z = t['pos']
        im.paste(img, (x, H - z - S), alpha)
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
    apply_city_names([(s[0], s[3]) for s in sessions], a.city_names)
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
            smo = one(ob, 'ShipModuleOwner')
            if smo is not None:                                       # warship or civil (cargo, river boat)
                kind = 'warship' if gn is not None and ship_role(u(gn.value), ship_modules(smo), info, gmap) == 'war' else 'civil'
            else:
                kind = 'unit' if one(ob, 'Unit') is not None else 'building'
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
    if not a.no_routes:                                               # human players' trade routes
        results = [{'session': s[0], 'areas': s[3]} for s in sessions]
        ships = {}
        for s in sessions: ships.update({m: (s[0],) + v for m, v in ship_index(s[2]).items()})
        attach_routes(results, trade_routes(data_root), ships, names, gmap)
        centers = {e['area']: e['center'] for e in area_list if 'center' in e}
        legs = collections.Counter()
        for rt in next(r for r in results if r['session'] == label).get('trade_routes', []):
            if rt['owner'] not in humans: continue
            stops = [st['area'] for st in rt['stations'] if st['session'] == label and st['area'] in centers]
            for i in range(len(stops) - 1 if len(stops) <= 2 else len(stops)):   # routes are loops
                p, q = stops[i], stops[(i + 1) % len(stops)]
                if p != q: legs[(min(p, q), max(p, q), rt['owner'])] += 1
        def dashed(p, q, fill, width, dash=18, gap=12):
            (x1, y1), (x2, y2) = p, q
            length = math.dist(p, q)
            if not length: return
            ux, uy, t = (x2 - x1) / length, (y2 - y1) / length, 0.0
            while t < length:
                t2 = min(t + dash, length)
                d.line((x1 + ux * t, y1 + uy * t, x1 + ux * t2, y1 + uy * t2), fill=fill, width=width)
                t = t2 + gap
        for (p, q, owner), n in sorted(legs.items()):
            pa = (centers[p][0], H - centers[p][1]); qa = (centers[q][0], H - centers[q][1])
            w = 3 + min(n, 4)                                          # busier legs are thicker
            dashed(pa, qa, (0, 0, 0), w + 3)
            dashed(pa, qa, color.get(owner, (255, 215, 0)), w)
    draw_order = {'civil': 0, 'unit': 1, 'warship': 2}                # warships on top
    k = max(1.0, H / max(a.width or 1600, 1))                          # icon scale: same size after resizing
    for kind, o, aid, x, z in sorted((ob for ob in objs if ob[0] in draw_order), key=lambda ob: draw_order[ob[0]]):
        c = color.get(o, grey); y = H - z
        # military icons are larger and drawn over a black shape, so they stand out on buildings of the same
        # colour; sizes follow the map size, which is scaled down to --width afterwards
        if kind == 'warship':                                          # triangles
            d.polygon([(x, y - 16 * k), (x - 13 * k, y + 10 * k), (x + 13 * k, y + 10 * k)], fill=(0, 0, 0))
            d.polygon([(x, y - 10 * k), (x - 8 * k, y + 6 * k), (x + 8 * k, y + 6 * k)], fill=c)
        elif kind == 'civil':                                          # circles with a white rim: merchant ships
            d.ellipse((x - 10 * k, y - 10 * k, x + 10 * k, y + 10 * k), fill=(255, 255, 255))
            d.ellipse((x - 7 * k, y - 7 * k, x + 7 * k, y + 7 * k), fill=c)
        elif o is not None:                                            # troops: squares
            d.rectangle((x - 10 * k, y - 10 * k, x + 10 * k, y + 10 * k), fill=(0, 0, 0))
            d.rectangle((x - 6 * k, y - 6 * k, x + 6 * k, y + 6 * k), fill=c)
    f, fs = font(max(14, H // 90)), font(max(11, H // 130))
    gap = max(6, H // 200)                                             # space between name and owner
    for e in area_list:
        if 'center' not in e: continue
        txt = e.get('custom_name') or (e.get('city') or f"area {e['area']}").replace(' Nome', '')
        x, y = e['center'][0], H - e['center'][1]
        c = color.get(e['owner'], grey)
        d.text((x, y - 22), txt, font=f, fill=c, anchor='md', stroke_width=3, stroke_fill=(0, 0, 0))
        if e['owner'] is not None:                                     # owner under the island name
            d.text((x, y - 22 + gap), str(label_of(e['owner'])), font=fs, fill=c, anchor='mt',
                   stroke_width=2, stroke_fill=(0, 0, 0))
    if not a.legend:
        rows = []
    else:
        rows = list({label_of(p): (color[p], label_of(p)) for p in present if p in color}.values())
        rows += [((255, 60, 60), 'pirate structures (x)'), (grey, 'nobody / unknown')]
        lf = font(max(14, H // 100)); lh = lf.size + 8 if hasattr(lf, 'size') else 20
        bx, by = 20, 20
        head = f'{label}: warships = black-rimmed triangles, civil ships = white-rimmed circles, troops = squares'
        bw = max(d.textlength(t, font=lf) for t in [head] + [r[1] for r in rows]) + 40
        d.rectangle((bx - 10, by - 10, bx + bw, by + lh * (len(rows) + 2)), fill=(0, 0, 0))
        d.text((bx, by), head, font=lf, fill=(255, 255, 255))
        for i, (c, t) in enumerate(rows, 1):
            d.rectangle((bx, by + i * lh + 3, bx + 16, by + i * lh + 19), fill=c)
            d.text((bx + 26, by + i * lh), t, font=lf, fill=(255, 255, 255))
    if a.width and a.width < H: im = im.resize((a.width, a.width), Image.LANCZOS)
    out = a.output or f'{label}_map.png'
    im.save(out)
    print(f'{label}: {len(tiles)} tiles, {sum(k == "warship" for k, *_ in objs)} warships, '
          f'{sum(k == "civil" for k, *_ in objs)} civil ships, {sum(k == "unit" for k, *_ in objs)} units -> {out}')

def main():
    ap = argparse.ArgumentParser(description='Anno 117: Pax Romana save game and game data tools.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('extract', help='save game -> one JSON per session')
    e.add_argument('save'); e.add_argument('-o', '--output', default='sessions')
    e.add_argument('--names', action='append', metavar='FILE',
                   help='GUID name map; repeatable, later files win (default: ./guid_names.json if present)')
    e.add_argument('--radius', type=float, default=40, help='squadron clustering radius (default 40)')
    e.add_argument('--city-names', metavar='FILE',
                   help='JSON {"<area id>" or "<session>:<area id>": "name"} with the names you gave your cities')
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
    t.add_argument('--template', action='append', metavar='T',
                   help='only assets of this template, one section each, in this order; repeatable '
                        '(e.g. LandUnit, NavalUnit, ShipModule)')
    t.add_argument('--title', default='GUID reference', help='page title')
    mp = sub.add_parser('map', help='save game + game files -> PNG map of a province (needs Pillow)')
    mp.add_argument('save'); mp.add_argument('game', help='Anno 117 "maindata" folder')
    mp.add_argument('--session', default='nile', help='session label or GUID (default nile)')
    mp.add_argument('-o', '--output', help='PNG file (default <session>_map.png)')
    mp.add_argument('--names', action='append', metavar='FILE', help='GUID name map(s), as for extract')
    mp.add_argument('--texts', action='append', metavar='FILE', help='texts_<lang>.xml, as for extract')
    mp.add_argument('--city-names', metavar='FILE', help='your city names, as for extract')
    mp.add_argument('--width', type=int, default=1600, help='output width in pixels (default 1600)')
    mp.add_argument('--no-routes', action='store_true',
                   help="don't draw human players' trade routes (dashed lines between their islands)")
    mp.add_argument('--legend', action='store_true',
                   help='draw a legend box in the top-left corner (owners are always written under island names)')
    mp.add_argument('--color', action='append', metavar='WHO=COLOUR',
                   help='participant name or GUID = colour name or #rrggbb, e.g. "Dorian=yellow"; repeatable')
    rp = sub.add_parser('report', help='save game -> HTML (and Markdown) report with alarms')
    rp.add_argument('current', help='save game (.a8s), or a folder of JSON files written by extract')
    rp.add_argument('--compare', metavar='PREVIOUS', help='an earlier save (or extract folder): show what changed')
    rp.add_argument('-o', '--output', default='report.html', help='HTML file (default report.html)')
    rp.add_argument('--md', metavar='FILE', help='also write the report as Markdown')
    rp.add_argument('--maps', metavar='DIR', help='folder with PNG maps named <session>.png or <session>_map.png to embed')
    rp.add_argument('--player', action='append', metavar='WHO',
                    help='your participant name or GUID; repeatable (default: every human player in the save)')
    rp.add_argument('--horizon', type=float, default=60,
                    help='warn about goods that run out within this many minutes (default 60)')
    rp.add_argument('--names', action='append', metavar='FILE', help='GUID name map(s), as for extract')
    rp.add_argument('--texts', action='append', metavar='FILE', help='texts_<lang>.xml, as for extract')
    rp.add_argument('--city-names', metavar='FILE', help='your city names, as for extract')
    rp.add_argument('--stale', type=float, default=45,
                    help='minutes after which the open page marks itself as old (default 45)')
    rp.add_argument('--radius', type=float, default=40, help=argparse.SUPPRESS)
    a = ap.parse_args()
    {'extract': cmd_extract, 'assets': cmd_assets, 'list': cmd_list, 'guid-table': cmd_guid_table,
     'map': cmd_map, 'report': cmd_report}[a.cmd](a)

if __name__ == '__main__':
    main()
