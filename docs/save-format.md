---
title: Save game format
---

# Anno 117 save game format

Ubisoft does not document this format. Everything below was reverse-engineered from real
save games and cross-checked against the Anno 1800 formats documented by the
[anno-mods](https://github.com/anno-mods) community (RDAExplorer, FileDBReader).
Game patches may change it.

## 1. Container: RDA v2.2 archive

A `.a8s` file starts with the ASCII string `Resource File V2.2`: it is the RDA archive format
already used by Anno 2205 and Anno 1800.

| Offset | Content |
|---|---|
| 0 | `Resource File V2.2` (18 bytes) |
| 784 | `uint64` offset of the first block |

Each **block** has a 32-byte header: `uint32 flags`, `uint32 numFiles`, `uint64 directorySize`,
`uint64 decompressedSize`, `uint64 nextBlockOffset`. The directory sits **right before** the block
header. Each directory entry is 560 bytes: a 520-byte UTF-16 name, then `offset`, `compressed`,
`size`, `timestamp` and an unknown field, all `uint64`.

Block flags: `1` compressed (zlib), `2` encrypted, `4` memory-resident, `8` deleted.
In the saves we analysed every flag is `0`.

A save contains four files:

| File | Decompressed size | Content |
|---|---|---|
| `meta.a7s` | ~1 KB | version |
| `header.a7s` | ~300 KB | settings and preview |
| `gamesetup.a7s` | ~33 KB | game setup (difficulty, pirates, DLCs…) |
| `data.a7s` | ~140 MB | **the whole game state** |

Each one is **zlib**-compressed (starts with `78 DA`).

## 2. Data format: FileDB v2

The decompressed `.a7s` files are **FileDB version 2**: a binary tree of tags (nodes) and
attributes (values), like binary XML.

- **End of file:** magic `08 00 00 00 FD FF FF FF`, preceded by two `uint32`: the offset of the
  tag dictionary and the offset of the attribute dictionary.
- **Dictionary:** `uint32 n`, then `n` `uint16` ids, then `n` zero-terminated names.
- **Nodes:** a sequence of 8-byte headers, `int32 size` and `uint32 id`.
  - `id == 0`: closes the current tag.
  - `id < 0x8000`: opens a tag (name in the tag dictionary).
  - `id >= 0x8000`: attribute, followed by `size` bytes of value padded to a multiple of 8.

Values are raw bytes: little-endian integers, 32-bit floats, UTF-16LE strings. Their meaning has
to be inferred from the field name and context.

## 3. Provinces are nested saves

In `data.a7s`, the path `MetaGameManager/GameSessions/t1/SessionData/BinaryData` appears once per
province (session). Each `BinaryData` is itself a complete FileDB v2.

The province is identified by `SessionDesc/SessionGUID`:

| SessionGUID | Province | Clue |
|---|---|---|
| 3245 | Latium | starting session |
| 6627 | Albion | `region = 2` |
| 149679 | Nile / Egypt (DLC *Dawn of the Delta*) | `SessionMap = data/dlc03/provinces/egypt/...` |

## 4. Ships, units and pirates

Inside each province, objects live in:

```
GameSessionManager/AreaManagers/AreaManager_<id>/AreaObjectManager/GameObject/objects/t1
```

Every object has `guid` (its type), `ID`, `Position` (3 floats x, y, z; the map uses x and z)
and a set of components:

| Component | Meaning |
|---|---|
| `ShipModuleOwner/InstalledShipModules/t1/ShipModuleGuid` | it is a **ship**; lists installed modules |
| `Unit` without ship modules | **land unit** |
| `Pirate` | a pirate faction (raids, area of activity, base, warship count) |
| `Health/LastDamageDealerOwner` | who last damaged the object |
| `Nameable/VehicleTextGuid` | ship name: `int64` `LineId` in `texts_<lang>.xml`; `VehicleTextIterator` counts reuses of the name (1 = none, 3 = "III", inferred) |

### Owner

Objects do not store their owner directly. The link is:

1. the object's `MetaPersistent/MetaID`, in the province file;
2. the entry with the same `ID` in `MetaGameManager/MetaGameObjectManager/MetaObjectMap/t1`, in `data.a7s`;
3. that entry's `ParticipantGUID`.

A participant's custom name, if any, is in the same map under `MetaParticipant/CustomCompanyName`.

In the saves we analysed, participant **41** was the human player. AI factions and pirates use
other GUIDs (31099, 31100, 153067…). The pirate faction's participant GUID equals the `guid` of its
`Pirate` object (for example 153069 for the Kashta in the Nile).

### Ship roles, troops and kinds of participant

- Each ship gets `role`: `war` when it carries a weapon module (internal names with Archers,
  Catapult, Scorpion or Torchers) or its asset is military by design (`Military`, `Raider`,
  `Warship`, `Pirate` in the internal name, e.g. *Egyptian RiverShip Dahabiya Military*); otherwise
  `civil` (sails only, river boats such as the Felluca, *Dahabiya Civil*, start ships). Each
  participant gets `warships` and `civil_ships`.
- Units whose asset template is `LandUnit` are troops (`"troop": true`, and `troops` per
  participant); the other units are towers, gates and harbours.
- `participant_kinds` maps each participant to `human`, `rival` (template `2ndParty (Rival)`),
  `pirate` (`3rdParty Pirate`) or `npc` (other `Participant` templates: traders, imperial envoys
  and neutral parties, who hold one harbour that cannot be conquered). `report` leaves `npc` out.

## 5. Islands (areas)

Each island, or district (*nome*) on the Nile, is an **area**. Two places describe it:

- `GameSessionManager/AreaInfo`: a list of pairs, the area id (`uint16`) then its data:

  | Field | Meaning |
  |---|---|
  | `OwnerProfile` | participant GUID of the owner (missing if nobody owns it) |
  | `Fertility` | GUIDs of the island's fertilities and deposits (template `Fertility`) |
  | `CityNameGuid` | `int64` text id: the `LineId` of the default city name in `texts_<lang>.xml` |
  | `PassiveTrade/History/TradeRouteEntries/…/RouteName` | UTF-16 names of the trade routes that served the area |
  | `AreaEconomy/StrgLrg` | warehouse content, see below |
  | `AreaEconomy/StorageTrends` | per good, 120 history points (`int32`); not the current stock |

- `GameSessionManager/AreaManagers/AreaManager_<area id>`: the area's simulation.
  `AreaMoneyManager/TotalMoneyIncomePerMinute` is a `float32` balance;
  `AreaPopulationManager/CurrentCityStatusGUID` is the city status (template `CityStatus`, whose
  name is under `Values/CityStatus/CityStatusName`); the area's buildings are in
  `AreaObjectManager/GameObject/objects`, and their positions give the island's extent.

### Buildings, residents and production

Every object of an area with a `Building` component is counted by type (`buildings`). Two
components carry the economy:

| Component | Field | Meaning |
|---|---|---|
| `Residence7` | `ResidentCount` (`float32`) | residents of the house; `AverageNeedSaturation`, `ConsumptionStates` per good |
| `Factory7` | `CurrentProductivity` (`float32`, 1.0 = 100%) | 0 when the building is idle |
| `Factory7` | `ProductionState/InProgress`, `RemainingTime` | a cycle is running |

What a building makes comes from the game data: `Values/FactoryBase` in `assets.xml`
(`FactoryInputs`/`FactoryOutputs` items with `Product` and `Amount`, and `CycleTime` in seconds;
when missing, the default is `FactoryBase/CycleTime` in `export/properties.xml`, 30 s). `assets`
stores it as `factory` and `extract` computes, per area:

- `population` and `residents` (by house type);
- `needs`: per good or service asked for by the house types present (from the `needs` that
  `assets` writes), its `category`, the `houses` asking for it, `kind` (`good` or `service`) and
  `buff_only`;
- `production`: per building type, `count`, `working` (cycle in progress), mean `productivity`,
  `output_per_minute` and `input_per_minute` = amount × 60 / cycle time × productivity;
- `output_capacity`: what the area's production buildings make per minute at 100% productivity;
- `idle_reasons` (per building type and per area): for buildings at 0% productivity, the status
  icons the game shows over them (`Infolayer/Icons/t1/GUID`, template `InfoLayerIcon`), such as
  "StorageFull" (nobody collects the output), "missing goods", a paused building or, on the Nile,
  the flood and harvest seasons;
- `idle_output_full` (per building type and per area): how many idle buildings carry the StorageFull icon (GUID 2001976),
  i.e. overproduction; `idle_other_reasons`: the idle buildings without it, by icons;
- `workforce_saturation`: `AreaWorkforceManager/DeltaStorage`, per workforce type the share of
  demand that is met (1.0 = every building has its workers);
- `goods_per_minute`: production minus factory consumption. Residents' consumption and trade
  routes are **not** included.

`PausedReasons` is not set on idle buildings; the reason is in the status icons, see `idle_reasons`.

### Warehouse (`StrgLrg`)

A flat list of `uint32`: N good GUIDs, then N amounts. The first GUID is absolute; each next one
is stored as the number of GUIDs skipped since the previous (`next = previous + value + 1`).
Example, `2085 16 0 149600 | 23 21 25 19`: charcoal (2085) 23, mud (2102) 21, reed (2103) 25,
mud bricks (151704) 19. Goods with amount 0 can be listed. `extract` writes the non-zero ones as
`storage` (names) and `storage_guids`. `AreaEconomy/TradeStorage/StrgLrg` uses the same layout.

### Warehouse history (`StorageTrends`)

`AreaEconomy/StorageTrends` holds the data of the game's warehouse chart: pairs of a good GUID and
a record with `Points` (`capacity` and `size` = 120, then 120 `uint32` stock samples, oldest
first, the last one being the current stock), `LastPointTime` (game time in ms) and
`Estimation` (0 in every save seen). Samples are **10 game seconds** apart, so a save holds the
last 20 minutes: two saves 180 s apart had `LastPointTime` 180000 ms apart and series shifted by
18 samples. `extract` writes, per good, `stock_trends`: `outflow_per_minute` and
`inflow_per_minute` (sum of the falls and of the rises between samples: consumption and ships
loading, production and ships unloading), `net_per_minute` (last minus first sample, per minute),
`first`, `last` and `max` (the highest sample).

`StrgLrg` and the chart disagree for many goods held at a storage limit: in the saves we checked,
`StrgLrg` reads 0 (or omits the good) while the chart's last samples sit at the limit (749, 750,
749). When the chart exists, `extract` therefore writes its last sample as the good's `storage`.

Storage limits are not stored, and they differ between goods of one area (for example 450 for
most goods and 1950 for some raw materials). Goods held at a limit all peak at the same value, so
`storage_limits_estimate` lists every peak shared by at least three goods; a good is "at limit"
when its stock is within 2% of one of them (and its own peak did not go past it).

### Game time

`GameSessionManager/SessionTotalTime` (per province, ms of game time) is written by `extract` as
`session_time_ms`. Each province keeps its own clock: between the same two saves we measured 29.8,
18.5 and 7.7 minutes in three provinces, so the difference is the time that province ran, not the
time between the saves.

### City names

A renamed city keeps its default `CityNameGuid`. The typed name is stored, with ship and army
names, in `MetaGameManager/MetaUserGeneratedTextManager/UserGeneratedTexts` (pairs of an `int64` id
and `RawText`/`SanitizedText`/`DisplayText`), but we found no reference from the area to that id.

An experiment settled where the link is: renaming one city between two saves added **only** the
new entry to `UserGeneratedTexts`. Nothing in the area, in the session or anywhere else in the
save changed or referred to the new id. The game therefore derives the text id from the area
with its own hash (ids of areas whose ids share the low byte share the top byte of the text id);
it is none of the common 64-bit hashes (CRC-64 variants, xxHash64, MurmurHash64A, FNV-1/1a, MD5,
SHA-1) over the obvious encodings of area id, session GUID, owner or `CityNameGuid`.

You can give `extract` and `map` the names yourself with `--city-names FILE`, a JSON object
`{"<area id>": "name"}` (or `"<session>:<area id>"` when the id exists in several sessions);
those names win. Without it, `extract` infers them from the trade routes. Route names end with `Abc - Xyz`, the first
three letters of both cities, and routes may cross provinces. A code goes to the area that appears
in every route using it, by elimination. It is kept only when confirmed: either the default city
name starts with it, or the area belongs to a player with a custom company name and exactly one
typed text starts with it (`custom_name`; several matches give `custom_name_candidates`). Treat it
as a guess.

### Residents' consumption (not computed)

Each house type lists its needs in `assets.xml` under `Values/Residence7/NeedsList` (`Need` +
`NeedConsumptionRate`), and each `Need` asset points to its good (`Values/Need/NeedProduct`). The
unit of `NeedConsumptionRate` is not confirmed: read as "per resident per minute" it gives about 5×
what the game's own production and full warehouses suggest, and Anno 117's optional needs (one
good *or* another satisfies the same need) split consumption between goods. Until this is
verified, `extract` does not include residents in `goods_per_minute`. The warehouse history
(`stock_trends.outflow_per_minute`) measures what actually leaves a warehouse, residents included,
but mixed with factories and ships loading.

## 6. Trade routes

`MetaGameManager/SessionTradeRouteManager/RouteMap` holds every player's routes as pairs (route
id, route):

| Field | Meaning |
|---|---|
| `Name` | UTF-16 route name (`"Flax Kan - Mem"`) |
| `Owner` | participant GUID |
| `Ships` | the ships' `MetaPersistent/MetaID`s, one `uint64` each |
| `Stations/t1` | stops: `AreaID` (`uint16`, an area id of some session), `AreaOwner`, `GoodInfos/t1` with `ProductGUID` and `Amount` (`int32`) |

`extract` resolves ships through their `MetaID` (all ships in the saves we tested were found) and
stations through the area ids. Area ids are unique only within a session: when an id exists in
several sessions, the station goes to the session of the route's ships. Each route is added, as
`trade_routes`, to every session it stops in. Whether a good is loaded or unloaded at a station is
**not decoded**: `Amount` is the value set for that good at that stop, and the route name does not
tell either (`"Flax Por - Aer"` carries flax *from* Aer *to* Por).

`extract` therefore reads the direction from the warehouse history (`StorageTrends`): a ship
loading takes a whole cargo between two 10 s samples, unloading adds one. Steps of 15 or more are
summed per good as `stock_trends.loaded` and `unloaded`; on a route, a good goes **from** the stops
where it was loaded **to** the others (unloads are not used for this: the cargo may have come by
another route). In one save, bread on a two-stop route showed -51 and -46
at the producing city and +47 and +43 at the receiving one. Goods that no ship moved in the last 20 minutes fall back to
production capacity: they go **from** the stop whose buildings can make the most of them
(`output_capacity`, every production building at 100%, so farms idle at save time still count)
**to** the route's other stops; that guess can be wrong when a large city also makes the good. Each area gets `supplies` and `receives`, `{good: [cities]}`, which shows at a glance
what small supply islands are for.

## 7. What cannot be read from the save alone

- **Type names.** Saves contain only numeric GUIDs. Names live in the game data:
  see [Game data and GUID names](game-data.md).
- **Renamed cities.** Default city and ship names resolve through `texts_<lang>.xml`; typed city names are inferred (see above).
  Trade route names (`RouteName`), however, are plain UTF-16 text and contain the first three
  letters of each city (for example `Hemp Kan - Mem`).
- Residents' consumption per minute. Load/unload direction on trade routes is not stored; it is
  read from the warehouse history when ships moved the good in the last 20 minutes.

## 8. Known limits of `extract`

- Squadrons are a greedy distance clustering (`--radius`), not the game's own fleet groups.
- Objects whose `MetaID` is not in the `MetaObjectMap` are reported under an `unknown owner`.
