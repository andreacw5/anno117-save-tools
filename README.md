# Anno 117 save tools

Read **Anno 117: Pax Romana** save games (`.a8s`) and game data (`.rda`) with a single Python file.
No dependencies, Python 3.8 or newer.

- **`extract`**: turn a save into one JSON file per province, listing every participant's ships
  (type, name, installed modules, map position, squadrons) and land units, pirate factions, every
  island (owner, fertilities and deposits, warehouse stock, buildings, population, production per
  minute, income, city status) and the trade routes with their ships and stops.
- **`assets`**: pull the official GUID → name map out of your own copy of the game.
- **`map`**: draw a province as a PNG, from the save's map template and the game's own map tiles,
  with islands, buildings, ships and units coloured by owner (warships as black-rimmed triangles, civil and cargo
  ships as white-rimmed circles, troops as black-rimmed squares, all scaled to stay readable) and
  pirate structures marked. Unit spawners are drawn too: camps as filled red diamonds, hidden ambushes
  as hollow orange diamonds (both with how many units they can send) and the trigger points that set
  ambushes off as orange rings, with lines to the ambushes they wake up. Needs a `guid_names.json`
  from a recent `assets` run.
- **`report`**: one HTML page (and optionally Markdown) on your state. It opens with the goods
  running dry, placed on a 0–60 minute scale with a countdown each; then what changed since an
  earlier save, the other alarms grouped by kind (idle buildings, workforce shortages, falling
  income, routes without ships or stock) with a hint, "where to boost" (per island and good:
  production, factory inputs and ship loads against use, with what to do: build N more, fix idle
  buildings, change a route, ship spare output to an island in need), a summary per province with the warships
  around you (pirates in red), forces per player across all provinces, and the full per-province
  ledgers: your islands, players, fleets, troops, routes, warehouses and the map. Traders and
  imperial envoys (NPCs that cannot be attacked) are left out.
- **`list`**: browse the contents of `.rda` archives.
- **`guid-table`**: render the GUID map as a Markdown page.

📊 The report page, step by step: [`docs/report.md`](docs/report.md).

📖 Documentation: see [`docs/`](docs/index.md), also publishable as a website with GitHub Pages.

> Unofficial fan project, not affiliated with or endorsed by Ubisoft. *Anno* is a trademark of Ubisoft.
> This repository contains **no game files**: you generate the name map from your own installation.

## Quick start

```sh
# 1. Build the GUID name map from your game installation (once per game update)
python anno117.py assets "<Anno 117 install>/maindata/config.rda" --lang english

# 2. Extract a save (uses guid_names.json from step 1 if present;
#    --texts adds the islands' default city names)
python anno117.py extract "path/to/Autosave_0001.a8s" -o sessions \
    --texts extracted/config/data/base/config/gui/texts_english.xml
```

The game's installation path depends on your launcher. Point `assets` at the folder that contains the `.rda` archives.

Output (`sessions/<province>.json`, abridged):

```json
{
  "session": "nile",
  "session_guid": 149679,
  "participants": [
    {
      "participant": 41,
      "name": "Your company name",
      "ship_count": 32,
      "ships": [{"type": "Quinquereme", "guid": 37224, "count": 17, "ships": [
        {"modules": ["..."], "module_guids": [31648], "position": [1210, 845]}]}],
      "squadrons": [{"position": [1200, 850], "ships": 5}],
      "units": [{"type": "...", "guid": 92904, "count": 4}]
    }
  ],
  "pirates": [{"participant": 153069, "activity_center": [431, 215], "max_warships_reached": 23}],
  "areas": [
    {"area": 25668, "owner": 41, "city": "Honey Lotus Nome", "route_code": "Mem", "custom_name": "...",
     "fertilities": ["..."], "fertility_guids": [2202],
     "storage": {"...": 1949}, "storage_guids": {"2072": 1949},
     "income_per_minute": 2763, "city_status": "...", "objects": 793,
     "center": [1344, 319], "extent": [1229, 230, 1458, 418], "trade_routes": {"Flax Kan - Mem": 41}}
  ]
}
```

## Commands

| Command | What it does |
|---|---|
| `extract <save> [-o DIR] [--names FILE]… [--texts FILE]… [--city-names FILE] [--unit-names FILE] [--radius R]` | One JSON per session. `--city-names` takes a JSON `{"<area id>": "name"}` with the names you gave your cities (the save does not link them to the island). `--unit-names` (alias `--ship-names`) does the same for ships and land units, keyed by the `meta_id` written for each of them. `--texts` takes a `texts_<lang>.xml` (as extracted by `assets`) to name the islands' default cities. `--names` can be repeated to layer maps (e.g. your own hand-written guesses, then the official `guid_names.json`): later files win. `--radius` sets the distance used to group ships into squadrons (default 40). |
| `assets <path> [-o FILE] [--lang LANG] [--all] [--extract-dir DIR] [--texts-out FILE]` | Finds `assets*.xml` and `texts_<LANG>.xml` in the `.rda` archives (or in a folder of extracted `.xml`) and writes `guid_names.json`. Without `--all` it keeps only naval, military, participant, session, fertility, city status and production assets. Production buildings also get `factory` (inputs, outputs, cycle time), read from `assets.xml` and `properties.xml`. `--texts-out` also writes every text of the language as a JSON file, which `--texts` accepts in place of `texts_<LANG>.xml`: `extract` and `report` then name cities and ships on a computer without the game. |
| `map <save> <maindata> [--session S] [-o FILE] [--names FILE]… [--texts FILE]… [--width W] [--color WHO=COLOUR]… [--legend] [--no-routes]` | PNG map of one province (default `nile`). Each island shows its name and, below, its owner; human players' trade routes between islands of the province are dashed lines (thicker when several routes share a leg), and routes to other provinces are finer dashed lines from the island to the nearest map border, labelled with the province and cities they reach (`--no-routes` hides both); `--legend` adds a legend box in the top-left corner. `--color` sets a participant's colour by name or GUID (`--color "Dorian=yellow" --color 41=#2ecc40`); human players default to gold, pirates are always red. Needs **Pillow** (`pip install pillow`), the only command with a dependency. |
| `report <save or extract DIR> [--compare PREVIOUS] [-o FILE.html] [--md FILE.md] [--maps DIR] [--player WHO]… [--horizon MIN] [--stale MIN]` | Self-contained HTML page (and optional Markdown) made to stay open next to the game: goods running dry on a minute scale, alarms grouped by kind and sorted by severity, NEW since the previous save, a summary per province with pirates marked, an island view (needs, overproduction, exports, imports), tabs for the fleet (warships, merchant ships by trade route and idle ones, in every province, grouped by place and named after the nearest island, with pirate warships and where ships can be spared; key `f`) and the troops (key `u`, with how many are camped), forces per player, per-province tables. It reloads itself when you regenerate it. Takes a save (with the same `--names`/`--texts`/`--city-names` options as `extract`) or a folder written by `extract`. Details: [docs/report.md](docs/report.md). |
| `list <file.rda or dir> [FILTER]` | Lists archive contents, optionally filtered by substring. |
| `guid-table <guid_names.json> [-o FILE] [--template T]… [--title TEXT]` | Writes a Markdown page (default `docs/guids.md`), one table per template; `--template` keeps only those templates, in that order. `docs/guids.md` is made with `--template LandUnit --template NavalUnit --template ShipModule --template MilitaryTowerUnit` from an English map. |

## Status

| Part | Status |
|---|---|
| Save container (RDA v2.2) and FileDB v2 | ✅ tested on real saves |
| Ships, modules, positions, owners, pirates | ✅ tested on real saves |
| Game `.rda` archives, `assets.xml`, `texts_<lang>.xml` | ✅ tested on Anno 117 (Steam, with *Dawn of the Delta*) |
| Islands: owner, fertilities and deposits, income, city status, extent | ✅ tested on real saves |
| Warehouse stock per island | ✅ tested on real saves |
| City names | ⚠️ default names exact; renamed cities from `--city-names`, otherwise **inferred** from trade route codes |
| Ship and unit names | ⚠️ default names exact; renamed ships and land units only from `--unit-names` |
| Province maps (`map`) | ✅ tested on three provinces; tile seams are visible |
| Buildings, population, production per minute | ✅ tested on real saves; residents' consumption not included |
| Trade routes: ships, stops, goods | ✅ tested on real saves; direction read from ship loads in the warehouse history, else estimated |
| Ship names | ✅ from `texts_<lang>.xml` (pass `--texts`) |
| Warehouse history (last 20 minutes), game time | ✅ tested on real saves; storage limits are estimated |
| Report with alarms (`report`) | ✅ tested on real saves |

Reports and pull requests are welcome, especially output of `list` on your game's `maindata` folder.

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests build small RDA archives, FileDB trees and XML files in memory, so no game files are
needed. Python 3.8 or newer.

## How it works

See [docs/save-format.md](docs/save-format.md) and [docs/game-data.md](docs/game-data.md).
This work builds on the Anno 1800 formats documented by the [anno-mods](https://github.com/anno-mods)
community (RDAExplorer, FileDBReader).

## License

[MIT](LICENSE).
