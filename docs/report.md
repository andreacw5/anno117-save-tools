---
title: The report page
---

# The report page

`anno117.py report` turns a save game into **one HTML page** that tells you what needs fixing in
your empire. It is meant to stay open next to the game, for example on a second screen in a
narrow window, and to be regenerated after each save.

The page is a single file: styles, scripts and maps are inside it, nothing is loaded from the
internet, and it works opened straight from disk (`file://`) or from any local web server.

## Quick start

```sh
# once per game update: names, production and residents' needs from your installation
python anno117.py assets "<Anno 117 install>/maindata/config.rda" --lang english --all

# after each save: compare with the previous one
python anno117.py report "path/to/new.a8s" --compare "path/to/previous.a8s" \
    -o report.html --md report.md \
    --texts extracted/config/data/base/config/gui/texts_english.xml
```

Open `report.html` in a browser and leave it open. Each time you run the command again, the open
page notices within a few seconds and **reloads itself**, keeping your scroll position and
filters. It does so by polling `report.html.stamp.js`, a tiny file written next to the page; if
that file is missing the page simply stays as it is.

Useful options:

| Option | What it does |
|---|---|
| `--compare PREVIOUS` | An earlier save (or a folder written by `extract`): changes in brackets, **NEW** on alarms the previous save did not raise, the number of alarms resolved since then, and alarms for population, income, islands, warships and troops lost. |
| `--maps DIR` | Embeds the PNG maps made by `map`, named `<province>.png` or `<province>_map.png`. |
| `--md FILE` | Also writes the same content as Markdown (handy in git, or to paste somewhere). |
| `--city-names FILE` | The names you gave your cities (the save does not link them, see [save format](save-format.md#city-names)). |
| `--player WHO` | Your participant name or GUID, when the save has several human players. |
| `--horizon MIN` | Warn about goods that run out within this many minutes (default 60). |
| `--stale MIN` | After this many minutes the page marks its own data as old (default 45): time to save and regenerate. |

`report` also accepts a folder of JSON files written by `extract` instead of a save.

## Reading the page

### The bar

A bar stays at the top while you scroll:

- **`high` · `medium` · `info`**: how many alarms of each level; click one to show or hide that
  level. Info is hidden by default.
- **Province buttons**: show only one province (`all` to go back).
- **Updated HH:MM · N min ago**: turns amber when the data is older than `--stale`.
- **`island`** and the selector **Island (Province)**: switch to the island view.
- **`maps`**, **◐**: open or close the maps, switch between dark (default) and light theme.

The browser tab title shows the number of high alarms, e.g. *(3) Anno 117 report*, so you can see
it from the taskbar. Your choices (levels, province, theme, maps, open sections, selected island)
are remembered by the browser.

### Alarms

One list of cards, one per island, trade route or player, sorted by severity, then NEW first.
Each card lists its alarms with a short hint; a hint repeated on other cards is shown once, and as
a tooltip afterwards. Click an island's name to open its island view.

| Level | Examples |
|---|---|
| high | a good runs out within a third of `--horizon`; a route with no ships; population fell; an island, warships or troops lost |
| medium | a good runs out within `--horizon`, or ran out; a workforce below 90%; production buildings stopped for missing goods or paused; a route whose goods nobody makes or stocks; income fell |
| info | overproduction (at least half of an island's production buildings idle because nobody collects their output); goods a route brings to a warehouse that is already full |

How they are computed:

- **Running out** uses the warehouse chart stored in the save (the last 20 minutes, one sample
  every 10 s): the stock must have really dropped, not just wobbled near zero.
- **Military goods are left out** (weapons, armour, ropes, sails, horses... see `military` in
  [game-data.md](game-data.md)) on an island where no resident needs them and no factory uses them.
  There their stock only falls because you spend it on ships and troops, or ship it to a
  shipyard, so it raises no alarm, gets no advice and is not counted as residents' consumption.
  It still shows in the island's balance table.
- **Storage limits** are not in the save; they are estimated from the peaks the goods share.
- **Trade route direction** is not in the save either. Where the history shows a ship loading a
  good (a whole cargo leaving in one sample) that stop is the source; otherwise the stop that can
  produce the most of it is assumed to be. See [save format](save-format.md#6-trade-routes).
- Traders and imperial envoys (non-player characters with one harbour that cannot be attacked)
  are left out of players, fleets and forces.

### Island view

Press `i`, click `island`, or click an island's name in a card. For the island picked in the
selector it shows:

- its alarms;
- **Residents' needs**: every good and service the house types on the island ask for, with stock,
  trend and where it comes from. *Out of stock* (made here or shipped in, but none left) and
  *running low* come first. *Not supplied* may be fine: the game's needs lists also hold optional
  needs and needs not unlocked yet. Amounts per minute are not shown, since the unit of the game's
  consumption rate is not confirmed;
- **Overproduced**: building types idle because their output is not collected, what they make at
  100%, the stock, and where it is shipped;
- **Exported** and **Imported**: goods and the islands they go to or come from.

### Provinces

Below the alarms: forces per player across all provinces (warships, civil ships, troops), then per
province the map, your islands, all players, fleets (war / civil), troops, your trade routes and,
collapsed, the warehouse of each of your islands. Click a column header to sort.

## Keys

| Key | Action |
|---|---|
| `1` `2` `3` | show / hide high, medium, info |
| `j` / `k` | next / previous alarm card |
| `i` | island view / back to the alarms |
| `m` | open / close the maps |
| `g` | back to the top |
| `t` | dark / light theme |
| `r` | reload |

## Limits

- Everything comes from the save: what happened after it is not there. Save and regenerate.
- Each province keeps its own game clock, so "game time since the previous save" differs per
  province.
- Hints are suggestions from the numbers, not game rules.
