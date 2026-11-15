---
title: Game data and GUID names
---

# Game data and GUID names

Save games only contain numeric GUIDs (`37224`, `31648`…). Official names, templates and
translations are in the game's own data, inside the `.rda` archives of the installation's
`maindata` folder, which use the same RDA v2.2 container as saves. Everything the tool needs is in
`config.rda`: `data/base/config/export/assets.xml` and `data/base/config/gui/texts_<language>.xml`
for all 13 languages, so passing that single archive takes a couple of seconds.

```sh
python anno117.py assets "<Anno 117 install>/maindata/config.rda" --lang english
```

Language archives such as `en_us0.rda` only hold voice-over sound banks; they are not needed.

## What `assets` does

1. Opens each `.rda` with `mmap` and reads **only the index**, so multi-GB archives are fine.
2. Extracts only files matching `assets*.xml` and `texts_<lang>*.xml`, wherever they are in the
   archive, into `extracted/`.
3. Parses them and writes `guid_names.json`:

   ```json
   {"37224": {"name": "…", "template": "…", "name_local": "…"}}
   ```

   Assets without `<Template>` inherit it from their `BaseAssetGUID`. Without `--all`, only naval,
   military, participant and session assets are kept.
4. `extract` then picks `guid_names.json` up automatically (`--names` to point elsewhere).

You can also point `assets` at a folder of `.xml` files already extracted with another tool,
such as RDAExplorer.

## File layout (verified on Anno 117, November 2026)

- `assets.xml` (~46 MB, ~42,000 assets):
  `<Asset><Template/><Values><Standard><GUID/><Name/></Standard><Text><OasisId/></Text>…</Values></Asset>`.
  Derived assets use `<BaseAssetGUID>` and inherit the template, the text id and other values
  from their base.
- `texts_<language>.xml`: `<TextExport><Texts><Text><LineId/><Text/></Text>…`. `LineId` is a signed
  64-bit id equal to the asset's `Values/Text/OasisId`, not the GUID. The tool also still accepts
  the Anno 1800 layout, `<Text><GUID/><Text/></Text>`.
- Combat values are plain fields in `assets.xml`, such as `Values/Movement/BaseSpeed` and
  `Values/Health/BaseHealth`.
- Memory-resident blocks (flag `4`): two `uint64` (compressed size, real size) right before the
  directory, preceded by one data blob holding all files of the block; file offsets are relative
  to that blob.
- Encrypted blocks (flag `2`) are skipped with a warning.

If `assets` finds nothing, run `python anno117.py list "<install>/maindata" xml` and open an issue
with the output.

## Sharing names

`guid_names.json` and the extracted `.xml` are derived from Ubisoft's game files and are not
included in this repository. `guid-table` turns your map into a Markdown page
([GUID reference](guids.md)). Consider keeping the published table to the GUIDs the tool actually
needs (ships, modules, units, sessions) rather than the whole asset database.
