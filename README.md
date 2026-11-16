# Anno 117 save tools

Read **Anno 117: Pax Romana** save games (`.a8s`) and game data (`.rda`) with a single Python file.
No dependencies, Python 3.8 or newer.

- **`extract`**: turn a save into one JSON file per province, listing every participant's ships
  (type, name, installed modules, map position, squadrons) and land units, pirate factions, every
  island (owner, fertilities and deposits, warehouse stock, buildings, population, production per
  minute, income, city status) and the trade routes with their ships and stops.
- **`assets`**: pull the official GUID → name map out of your own copy of the game.
- **`map`**: draw a province as a PNG, from the save's map template and the game's own map tiles,
  with islands, buildings, ships and units coloured by owner and pirate structures marked.
- **`report`**: one HTML page (and optionally Markdown) on your state: alarms first (goods running
  out, idle buildings, workforce shortages, routes without ships or stock, losses since an earlier
  save) grouped per island and per route with a hint, warships, civil ships and troops per player
  across all provinces, then per province your islands, players, fleets, troops, routes and
  warehouses. Traders and imperial envoys (NPCs that cannot be attacked) are left out.
- **`list`**: browse the contents of `.rda` archives.
- **`guid-table`**: render the GUID map as a Markdown page.

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
| `extract <save> [-o DIR] [--names FILE]… [--texts FILE]… [--city-names FILE] [--radius R]` | One JSON per session. `--city-names` takes a JSON `{"<area id>": "name"}` with the names you gave your cities (the save does not link them to the island). `--texts` takes a `texts_<lang>.xml` (as extracted by `assets`) to name the islands' default cities. `--names` can be repeated to layer maps (e.g. your own hand-written guesses, then the official `guid_names.json`): later files win. `--radius` sets the distance used to group ships into squadrons (default 40). |
| `assets <path> [-o FILE] [--lang LANG] [--all] [--extract-dir DIR]` | Finds `assets*.xml` and `texts_<LANG>.xml` in the `.rda` archives (or in a folder of extracted `.xml`) and writes `guid_names.json`. Without `--all` it keeps only naval, military, participant, session, fertility, city status and production assets. Production buildings also get `factory` (inputs, outputs, cycle time), read from `assets.xml` and `properties.xml`. |
| `map <save> <maindata> [--session S] [-o FILE] [--names FILE]… [--texts FILE]… [--width W] [--color WHO=COLOUR]… [--legend] [--no-routes]` | PNG map of one province (default `nile`). Each island shows its name and, below, its owner; human players' trade routes between islands of the province are dashed lines (thicker when several routes share a leg; `--no-routes` hides them); `--legend` adds a legend box in the top-left corner. `--color` sets a participant's colour by name or GUID (`--color "Dorian=yellow" --color 41=#2ecc40`); human players default to gold, pirates are always red. Needs **Pillow** (`pip install pillow`), the only command with a dependency. |
| `report <save or extract DIR> [--compare PREVIOUS] [-o FILE.html] [--md FILE.md] [--maps DIR] [--player WHO]… [--horizon MIN]` | Self-contained HTML page (no internet needed, sortable tables, light and dark theme) and optional Markdown. Takes a save (extracted on the fly, with the same `--names`/`--texts`/`--city-names` options as `extract`) or a folder written by `extract`. `--compare` adds the changes since an earlier save and alarms for population, income, islands and ships lost. `--maps` embeds PNGs named `<session>.png` or `<session>_map.png`. Your player is found automatically (participants with a company name), or named with `--player`. |
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
| Province maps (`map`) | ✅ tested on three provinces; tile seams are visible |
| Buildings, population, production per minute | ✅ tested on real saves; residents' consumption not included |
| Trade routes: ships, stops, goods | ✅ tested on real saves; load/unload direction not decoded |
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
