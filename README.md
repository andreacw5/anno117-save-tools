# Anno 117 save tools

Read **Anno 117: Pax Romana** save games (`.a8s`) and game data (`.rda`) with a single Python file.
No dependencies, Python 3.8 or newer.

- **`extract`**: turn a save into one JSON file per province, listing every participant's ships
  (type, installed modules, map position, squadrons) and land units, pirate factions, and every
  island (owner, fertilities and deposits, warehouse stock, income, city status, trade routes).
- **`assets`**: pull the official GUID → name map out of your own copy of the game.
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
python anno117.py extract "path/to/Autosave_0001.a8s" -o sessions \n    --texts extracted/config/data/base/config/gui/texts_english.xml
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
| `extract <save> [-o DIR] [--names FILE]… [--texts FILE]… [--radius R]` | One JSON per session. `--texts` takes a `texts_<lang>.xml` (as extracted by `assets`) to name the islands' default cities. `--names` can be repeated to layer maps (e.g. your own hand-written guesses, then the official `guid_names.json`): later files win. `--radius` sets the distance used to group ships into squadrons (default 40). |
| `assets <path> [-o FILE] [--lang LANG] [--all] [--extract-dir DIR]` | Finds `assets*.xml` and `texts_<LANG>.xml` in the `.rda` archives (or in a folder of extracted `.xml`) and writes `guid_names.json`. Without `--all` it keeps only naval, military, participant, session, fertility and city status assets. |
| `list <file.rda or dir> [FILTER]` | Lists archive contents, optionally filtered by substring. |
| `guid-table <guid_names.json> [-o FILE]` | Writes a Markdown table (default `docs/guids.md`). |

## Status

| Part | Status |
|---|---|
| Save container (RDA v2.2) and FileDB v2 | ✅ tested on real saves |
| Ships, modules, positions, owners, pirates | ✅ tested on real saves |
| Reading game `.rda` archives / `assets.xml` | ⚠️ implemented after the Anno 1800 layout, **not yet verified on Anno 117** |
| Buildings, goods, islands | ❌ not mapped yet |

Reports and pull requests are welcome, especially output of `list` on your game's `data` folder.

## How it works

See [docs/save-format.md](docs/save-format.md) and [docs/game-data.md](docs/game-data.md).
This work builds on the Anno 1800 formats documented by the [anno-mods](https://github.com/anno-mods)
community (RDAExplorer, FileDBReader).

## License

[MIT](LICENSE).
