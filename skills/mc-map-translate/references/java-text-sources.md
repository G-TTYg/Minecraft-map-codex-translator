# Java Text Sources

Use this reference when scanning Java Edition maps.

## High-Value Sources

- Resource pack language files: `assets/<namespace>/lang/*.json`.
- Datapack function files: `datapacks/*/data/*/function/**/*.mcfunction` and older `functions/**/*.mcfunction`.
- Advancements, predicates, loot tables, item modifiers, recipes, worldgen, and tags under `datapacks/*/data/*/`.
- World NBT files: `level.dat`, `data/*.dat`, `playerdata/*.dat`. The bundled scanner has a basic NBT string walker.
- Region and entity region data: `region/*.mca`, `entities/*.mca`. The bundled scanner reads standard gzip, zlib, and raw chunk payloads and records chunk anchors.
- Command blocks and command block minecarts.
- Signs and hanging signs.
- Written books and writable books.
- Entity `CustomName` and item display names/lore.
- Bossbars, scoreboard display names, team names, titles, subtitles, actionbars, tellraw output, and dialogue-like command chains.

## Extraction Rules

- Capture exact source anchors: file, chunk, block/entity position when available, NBT path, JSON path, function line, and command argument span.
- Keep a snippet of surrounding command or JSON text component for context.
- Classify text by source kind before translation.
- Deduplicate for glossary work, but keep every occurrence anchor for apply/QA.
- Mark confidence. Parsed AST/NBT anchors are high confidence; byte-regex guesses are low confidence.
- Group styled JSON text fragments into complete player-facing messages whenever possible. Do not ask the translator to translate isolated `extra` fragments unless the fragment is truly independent.

## Do Not Translate

- Command names and syntax.
- Selectors such as `@p`, `@a`, `@e`, `@s`, selector arguments, and target tags unless visibly player-facing.
- Minecraft identifiers such as `minecraft:stone`, block IDs, item IDs, entity IDs, sounds, particles, recipes, loot tables, dimensions, predicates, tags, storage paths, and objective IDs.
- NBT keys and JSON text component field names: `text`, `translate`, `with`, `extra`, `color`, `bold`, `italic`, `clickEvent`, `hoverEvent`, `score`, `selector`, `keybind`, `nbt`.
- Scoreboard objective internal names. Translate only display names if clearly player-facing.

## Parser Expectations

Use real parsers for NBT, JSON, SNBT, and command-aware structures whenever available. A scanner may fall back to text heuristics for `.mcfunction`, `.json`, and `.snbt`, but binary `.mca` world data needs region/NBT parsing for reliable apply.

The bundled `mcmap_java_tools.py scan` covers Java language JSON, datapack JSON text components, `.mcfunction` JSON text components, `.dat` NBT strings, and supported `.mca` region chunks. It reports unsupported or failed binary files as pending coverage instead of guessing from raw bytes.

For apply, only parsed JSON text component anchors are eligible for hybrid key injection. Plain NBT strings from broad path hints are retained for review or explicit direct patching, but they are not safe resource-pack key-injection targets.
