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
| `Nameable/VehicleTextGuid` | ship name, as a **reference** to the game's name list, not text |

### Owner

Objects do not store their owner directly. The link is:

1. the object's `MetaPersistent/MetaID`, in the province file;
2. the entry with the same `ID` in `MetaGameManager/MetaGameObjectManager/MetaObjectMap/t1`, in `data.a7s`;
3. that entry's `ParticipantGUID`.

A participant's custom name, if any, is in the same map under `MetaParticipant/CustomCompanyName`.

In the saves we analysed, participant **41** was the human player. AI factions and pirates use
other GUIDs (31099, 31100, 153067…). The pirate faction's participant GUID equals the `guid` of its
`Pirate` object (for example 153069 for the Kashta in the Nile).

## 5. Islands (areas)

Each island, or district (*nome*) on the Nile, is an **area**. Two places describe it:

- `GameSessionManager/AreaInfo`: a list of pairs, the area id (`uint16`) then its data:

  | Field | Meaning |
  |---|---|
  | `OwnerProfile` | participant GUID of the owner (missing if nobody owns it) |
  | `Fertility` | GUIDs of the island's fertilities and deposits (template `Fertility`) |
  | `CityNameGuid` | `int64` text id: the `LineId` of the default city name in `texts_<lang>.xml` |
  | `PassiveTrade/History/TradeRouteEntries/…/RouteName` | UTF-16 names of the trade routes that served the area |
  | `AreaEconomy/StrgLrg` | warehouse content (not decoded yet) |

- `GameSessionManager/AreaManagers/AreaManager_<area id>`: the area's simulation.
  `AreaMoneyManager/TotalMoneyIncomePerMinute` is a `float32` balance;
  `AreaPopulationManager/CurrentCityStatusGUID` is the city status (template `CityStatus`, whose
  name is under `Values/CityStatus/CityStatusName`); the area's buildings are in
  `AreaObjectManager/GameObject/objects`, and their positions give the island's extent.

### City names

A renamed city keeps its default `CityNameGuid`. The typed name is stored, with ship and army
names, in `MetaGameManager/MetaUserGeneratedTextManager/UserGeneratedTexts` (pairs of an `int64` id
and `RawText`/`SanitizedText`/`DisplayText`), but we found no reference from the area to that id.

`extract` therefore infers it from the trade routes. Route names end with `Abc - Xyz`, the first
three letters of both cities, and routes may cross provinces. A code goes to the area that appears
in every route using it, by elimination. It is kept only when confirmed: either the default city
name starts with it, or the area belongs to a player with a custom company name and exactly one
typed text starts with it (`custom_name`; several matches give `custom_name_candidates`). Treat it
as a guess.

## 6. What cannot be read from the save alone

- **Type names.** Saves contain only numeric GUIDs. Names live in the game data:
  see [Game data and GUID names](game-data.md).
- **Ship and city names.** They are text references (`VehicleTextGuid`, `CityNameGuid`): see above for city names.
  Trade route names (`RouteName`), however, are plain UTF-16 text and contain the first three
  letters of each city (for example `Hemp Kan - Mem`).
- **Buildings and goods.** Building positions are read (to size islands); warehouse contents are not decoded yet.

## 7. Known limits of `extract`

- Squadrons are a greedy distance clustering (`--radius`), not the game's own fleet groups.
- Objects whose `MetaID` is not in the `MetaObjectMap` are reported under an `unknown owner`.
