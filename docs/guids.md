---
title: GUID reference
---

# GUID reference

A short list of GUIDs the tool deals with, checked against the game's `assets.xml` (November 2026).
Internal names are the `Values/Standard/Name` of each asset. Generate the full map from your own
installation with `python anno117.py assets`, and a table of it with `python anno117.py guid-table guid_names.json`.

## Ships

| GUID | Internal name | Template |
|---:|---|---|
| 37222 | Roman ModularShip Penteconter | NavalUnit |
| 37223 | Roman ModularShip Trireme | NavalUnit |
| 37224 | Roman ModularShip Quinquireme | NavalUnit |
| 39570 / 39571 / 39572 | Celtic ModularShip Quinquireme / Trireme / Penteconter | NavalUnit |
| 154038 | Egyptian RiverShip Felluca | NavalUnit |
| 157051 | Egyptian RiverShip Dahabiya Military | NavalUnit |
| 178644 | Egyptian RiverShip Dahabiya Civil | NavalUnit |
| 178639 | Egyptian RiverShip Dahabiya Raider (Kashta pirate ships, DLC *Dawn of the Delta*) | NavalUnit |

## Ship modules

| GUID | Internal name |
|---:|---|
| 31647 | Roman ShipModule Mast |
| 31648 | Roman ShipModule Rows |
| 31649 | Roman ShipModule Hull |
| 31651 | Roman ShipModule Archers |
| 31652 | Roman ShipModule Scorpion |
| 31653 | Roman ShipModule Catapult |
| 166548 | Egyptian ShipModule Archers |

## Land units

| GUID | Internal name |
|---:|---|
| 30456 | Troop Roman Legionary |
| 92904 | Troop Roman Archer |
| 31821 | Troop Roman Onager |
| 37483 | Troop Roman Equites Cavalry |
| 37485 | Troop Roman Pretorian Cavalry |
| 174563 | Troop Egpytian Raider Spearmen |

## Sessions and participants

| GUID | Name | Notes |
|---:|---|---|
| 3245 | Province Roman Italia (Latium) | starting session |
| 6627 | Province Celtic Britannia (Albion) | `region = 2` |
| 149679 | Province Egyptian Aegyptus | `SessionMap = data/dlc03/provinces/egypt/...` |
| 153069 | Pirate_Aegyptus_01 (Kandis of Kashta) | participant GUID of the Nile pirate faction |
