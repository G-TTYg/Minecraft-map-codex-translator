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
- Text Display / Display / Interaction-era entity text fields, especially entity or block-entity paths ending in `text`.
- Resource-pack visual assets that may contain text: PNG textures, custom font provider JSON, and model JSON that points at custom textures.

## Extraction Rules

- Capture exact source anchors: file, chunk, block/entity position when available, NBT path, JSON path, function line, and command argument span.
- For commands, accept optional leading `/` and scan `.Command` NBT values even when the command name is not recognized. High-value command families include `tellraw`, `title`, `bossbar`, `scoreboard`, `team`, `summon`, `data`, `item`, `loot`, `give`, `setblock`, `execute`, `say`, and `tell`.
- Scan command strings for direct JSON text component spans, quoted JSON text components inside SNBT strings, and low-confidence plain SNBT strings under player-facing keys such as `CustomName`, `display.Name`, `display.Lore`, `pages`, and sign `messages`.
- Exclude command block `LastOutput` by default. It usually contains command execution logs such as `commands.setblock.success` and inflates coverage stats without representing primary player-facing text. Use `--include-last-output` only for audit/debug.
- Aggregate sign and hanging-sign faces when possible. Treat the four `front_text.messages[]`, `back_text.messages[]`, or legacy `Text1`-`Text4` lines as one complete sign unit with per-line segments for apply.
- Keep a snippet of surrounding command or JSON text component for context.
- Classify text by source kind before translation.
- Deduplicate for glossary work, but keep every occurrence anchor for apply/QA.
- Mark confidence. Parsed AST/NBT anchors are high confidence; byte-regex guesses are low confidence.
- Group styled JSON text fragments into complete player-facing messages whenever possible. Do not ask the translator to translate isolated `extra` fragments unless the fragment is truly independent.
- Resource-pack image/font/model text is not covered by language JSON or hybrid key injection. The scanner should report visual asset hints; full localization requires manual inspection, OCR/image editing, or explicit visual-asset localization work.

## Do Not Translate

- Command names and syntax.
- Selectors such as `@p`, `@a`, `@e`, `@s`, selector arguments, and target tags unless visibly player-facing.
- Minecraft identifiers such as `minecraft:stone`, block IDs, item IDs, entity IDs, sounds, particles, recipes, loot tables, dimensions, predicates, tags, storage paths, and objective IDs.
- NBT keys and JSON text component field names: `text`, `translate`, `with`, `extra`, `color`, `bold`, `italic`, `clickEvent`, `hoverEvent`, `score`, `selector`, `keybind`, `nbt`.
- Scoreboard objective internal names. Translate only display names if clearly player-facing.

## Parser Expectations

Use real parsers for NBT, JSON, SNBT, and command-aware structures whenever available. A scanner may fall back to text heuristics for `.mcfunction`, `.json`, and `.snbt`, but binary `.mca` world data needs region/NBT parsing for reliable apply.

The bundled `mcmap_java_tools.py scan` covers Java language JSON, datapack JSON text components, `.mcfunction` JSON text components, command/NBT JSON spans, quoted command/SNBT JSON text components, aggregated sign faces, supported `.dat` NBT strings, and supported `.mca` region chunks. It reports unsupported or failed binary files as pending coverage instead of guessing from raw bytes.

For apply, only parsed JSON text component anchors are eligible for hybrid key injection. Plain NBT strings from broad path hints are not safe resource-pack key-injection targets; apply them only with explicit copied-world direct replacement through `apply-direct-nbt-strings`.
