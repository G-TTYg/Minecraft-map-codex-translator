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
- For commands, accept optional leading `/`, macro `$` lines, and `execute ... run ...` wrappers; scan `.Command` NBT values even when the command name is not recognized. High-value command families include `tellraw`, `title`, `bossbar`, `scoreboard`, `team`, `summon`, `data`, `item`, `loot`, `give`, `setblock`, `execute`, `say`, `tell`, `msg`, `w`, and `function`.
- Scan command strings for direct JSON text component spans, quoted JSON text components inside SNBT strings, low-confidence plain SNBT strings under player-facing keys such as `CustomName`, `display.Name`, `display.Lore`, `pages`, and sign `messages`, plus plain command messages and `data modify storage ... value` strings.
- Preserve escape layers when scanning commands and SNBT. A quoted command string may contain JSON that itself contains `\n`, `\"`, `\\`, or `\uXXXX`; keep enough span/quote metadata for apply to re-encode the same layer instead of consuming or double-escaping backslashes.
- Treat command JSON spans that are not text components as possible storage/menu/dialogue containers. Promote plain strings under player-facing JSON paths such as `dialogue`, `message`, `title`, `description`, `name`, `lore`, and `pages` to low-confidence `embedded-direct` units with `command_json_path`.
- Scan every datapack JSON file under `datapacks/*/data/**/*.json`, not only advancements. Recursively find JSON text components, strings that themselves contain JSON text components, and low-confidence plain strings under player-facing path hints.
- Record datapack function call edges and macro function lines. Use `function_call_graph` for contextual translation order, and inspect `suspicious_text_hints` for storage/custom JSON/macro text that needs human or AI confirmation before safe apply.
- Exclude command block `LastOutput` by default. It usually contains command execution logs such as `commands.setblock.success` and inflates coverage stats without representing primary player-facing text. Use `--include-last-output` only for audit/debug.
- Always aggregate a sign or hanging-sign face when any line has player text. Treat the four `front_text.messages[]`, `back_text.messages[]`, or legacy `Text1`-`Text4` lines as one complete sign unit with four-line context and per-line/per-node segments for apply. A one-line sign is still a face unit. Preserve exact `nbt_path` anchors and record block `x/y/z` as `address.block_pos` when available.
- Keep a snippet of surrounding command or JSON text component for context.
- Classify text by source kind before translation.
- Deduplicate for glossary work, but keep every occurrence anchor for apply/QA.
- Mark confidence. Parsed AST/NBT anchors are high confidence; byte-regex guesses are low confidence.
- Group styled JSON text fragments into complete player-facing messages whenever possible. Do not ask the translator to translate isolated `extra` fragments unless the fragment is truly independent.
- Resource-pack image/font/model text is not covered by language JSON or hybrid key injection. The scanner should report visual asset hints; full localization requires manual inspection, OCR/image editing, or explicit visual-asset localization work.
- Do not report every PNG as probable text. Keep a total PNG inventory, then prioritize OCR/visual review using path hints such as GUI/menu/title/sign/tutorial/poster/font and any assets referenced by custom font/model providers.

## Identity-Coupled Item Text

Treat item `custom_name`, `item_name`, and `lore` as identity-sensitive whenever it appears in or may be compared by:

- `Offers.Recipes[].buy`, `buyB`, or `sell`;
- containers, loot/reward definitions, `give`, `loot`, and `item replace` producers;
- `clear`, `execute if items`, item predicates, or component/NBT matching consumers;
- quest-key, currency, unlock, or advancement logic.

The scanner groups equal visible text-node shapes and assigns canonical group keys instead of occurrence keys. This prevents two originally equal items from becoming unequal merely because their translated display text uses different `translate` keys. Do not infer that a shared visible key proves the whole item is identical: item ID, count, custom data, model data, damage, enchantments, and other components still require gameplay-aware QA.

## Do Not Translate

- Command names and syntax.
- Selectors such as `@p`, `@a`, `@e`, `@s`, selector arguments, and target tags unless visibly player-facing.
- Minecraft identifiers such as `minecraft:stone`, block IDs, item IDs, entity IDs, sounds, particles, recipes, loot tables, dimensions, predicates, tags, storage paths, and objective IDs.
- NBT keys and JSON text component field names: `text`, `translate`, `with`, `extra`, `color`, `bold`, `italic`, `clickEvent`, `hoverEvent`, `score`, `selector`, `keybind`, `nbt`.
- Scoreboard objective internal names. Translate only display names if clearly player-facing.

## Parser Expectations

Use real parsers for NBT, JSON, SNBT, and command-aware structures whenever available. A scanner may fall back to text heuristics for `.mcfunction`, `.json`, and `.snbt`, but binary `.mca` world data needs region/NBT parsing for reliable apply.

The bundled `mcmap_java_tools.py scan` covers Java language JSON, all datapack JSON text components, datapack JSON strings containing text components, `.mcfunction` JSON text components, `execute ... run ...` command chains, command JSON plain string paths, quoted command/SNBT JSON text components, plain command messages, storage value strings, aggregated sign faces, supported `.dat` NBT strings, and supported `.mca` region chunks. It reports unsupported or failed binary files as pending coverage instead of guessing from raw bytes.

For apply, only parsed JSON text component anchors are eligible for hybrid key injection. Plain NBT/SNBT/command/datapack JSON strings from path hints are not safe resource-pack key-injection targets; apply them only with explicit copied-world direct replacement through `apply-direct-text` or leave them as known uncovered text.
