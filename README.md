# Minecraft Map Codex Translator

A local Codex plugin for professional Minecraft Java Edition map localization.

The plugin provides the `mc-map-translate` skill plus deterministic scripts for:

- inspecting Java map folders or zip packages;
- scanning language JSON, datapack JSON text components, `.mcfunction` commands, `.dat` NBT, and supported `.mca` chunks;
- creating indexed multi-file translation projects, contextual workpacks, and TSV review tables;
- maintaining a persistent workpack TODO list in `translation_progress.md`;
- merging staged translation parts back into a canonical translations JSONL;
- preparing and importing multi-text segment translations for AI-assisted fine localization;
- exporting resource-pack language files;
- safely applying hybrid translation-key injection to copied worlds or copied map zips.
- directly applying translated plain NBT strings to copied worlds or copied map zips when explicit `embedded-direct` output is needed.

## Safety Model

The default workflow is resource-pack-first. Original maps are never edited in place.

Hybrid key injection is available when hardcoded JSON text components must be made resource-pack-addressable. The apply tool copies or extracts the source map, then changes supported hardcoded text nodes to generated `translate` keys. Multi-`text` components are handled through `segments[]`: Codex translates the full message with context, fills per-segment translations, and the tool injects one key per original text node while preserving component styling and dynamic siblings.

Bedrock Edition is not supported yet.

## Common Commands

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect path/to/world-or-map.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world-or-map.zip --out work/map --target zh_cn --source-locale en_us --map-slug map --project-layout --max-workpack-units 120
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/map/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status work/map
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo work/map
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations work/map/translations/parts --base work/map/translation_units.jsonl --out work/map/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/map --out work/map/exports/hybrid-resource-pack --pack-format 34 --target zh_cn --include-hybrid-keys
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world-or-map.zip --translations work/map --out work/map/exports/world-keyed.zip --resource-pack work/map/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-nbt-strings path/to/world-or-map.zip --translations work/map --out work/map/exports/world-direct-nbt.zip
```

For large maps, Codex should treat `index/manifest.json` as the entry point, maintain `translation_progress.md` as the workpack TODO list, translate one `workpacks/contextual/workpack_###.jsonl` at a time, read only that workpack's listed source summaries, and write results into the matching `translations/parts/workpack_###.jsonl`.

## Validation

```bash
python -m py_compile skills/mc-map-translate/scripts/mcmap_java_tools.py skills/mc-map-translate/scripts/mcmap_contract.py
python path/to/skill-creator/scripts/quick_validate.py skills/mc-map-translate
python path/to/plugin-creator/scripts/validate_plugin.py .
```
