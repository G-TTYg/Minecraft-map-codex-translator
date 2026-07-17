# Minecraft Map Codex Translator

A local Codex plugin for professional Minecraft Java Edition map localization.

The plugin provides the `mc-map-translate` skill plus deterministic scripts for:

- inspecting Java map folders or zip packages;
- scanning language JSON, datapack JSON text components, `.mcfunction` commands, `.dat` NBT, and supported `.mca` chunks;
- creating translation workpacks and TSV review tables;
- exporting resource-pack language files;
- safely applying hybrid translation-key injection to copied worlds or copied map zips.

## Safety Model

The default workflow is resource-pack-first. Original maps are never edited in place.

Hybrid key injection is available when hardcoded JSON text components must be made resource-pack-addressable. The apply tool copies or extracts the source map, then changes supported hardcoded text nodes to generated `translate` keys. Multi-`text` components and ambiguous anchors are skipped and reported instead of being flattened.

Bedrock Edition is not supported yet.

## Common Commands

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect path/to/world-or-map.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world-or-map.zip --out work/map --target zh_cn --source-locale en_us --map-slug map
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/map/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-workpacks work/map/translation_units.jsonl --out-dir work/map/workpacks --dedupe-raw
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/map/translations.jsonl --out work/map/exports/hybrid-resource-pack --pack-format 34 --target zh_cn --include-hybrid-keys
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world-or-map.zip --translations work/map/translations.jsonl --out work/map/exports/world-keyed.zip
```

## Validation

```bash
python -m py_compile skills/mc-map-translate/scripts/mcmap_java_tools.py skills/mc-map-translate/scripts/mcmap_contract.py
python path/to/skill-creator/scripts/quick_validate.py skills/mc-map-translate
python path/to/plugin-creator/scripts/validate_plugin.py .
```
