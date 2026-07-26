# Java Edition Text Map

Use this reference when deciding what a scanned unit means and how risky it is to translate.

## Text Systems

Java maps commonly expose player-facing text through these systems:

- Language JSON: `assets/<namespace>/lang/<locale>.json`. These are the safest resource-pack-only targets.
- JSON text components: objects or arrays containing `text`, `translate`, `extra`, `with`, `score`, `selector`, `keybind`, `nbt`, `clickEvent`, and `hoverEvent`.
- Commands: `tellraw`, `title`, `bossbar`, `scoreboard`, `team`, `summon`, `data merge`, `item`, `loot`, `give`, `setblock`, `execute`, `say`, and `tell` frequently embed JSON text components or SNBT containing text components.
- Datapack JSON: advancements, predicates, item modifiers, loot tables, recipes, and custom data may contain text components.
- NBT world data: command blocks, signs, books, entities, text display entities, item display names/lore, scoreboards, teams, storage, and map data can contain player-facing strings.
- Region files: `.mca` files hold chunks; player-facing text may live in block entities, entities, and item data inside chunk NBT.
- Resource packs: language JSON is directly localizable, but PNG textures, bitmap/custom font providers, map art, and model textures may contain visual text that requires separate asset localization.

## Source Kind Meaning

- `lang`: Existing resource-pack language entry. Prefer resource-pack export.
- `tellraw`: Chat/output message from a command.
- `title`: Title or subtitle command text.
- `actionbar`: Actionbar title command text.
- `bossbar`: Bossbar display text.
- `scoreboard`: Scoreboard/team display text. Be careful not to translate objective IDs or fake player names.
- `function`: Hardcoded JSON text component found in `.mcfunction` without a more specific command kind.
- `datapack_json`: JSON text component found in datapack JSON.
- `command_block`: Command text from NBT, usually needs copied-map key injection or direct patching.
- `sign`: Sign/hanging sign text from NBT.
- `book`: Written book page or title text.
- `entity_name`: Entity `CustomName` or nearby equivalent.
- `item_name`: Item display name.
- `item_lore`: Item lore line or component.
- `nbt_text`: Plain NBT string from a path that looks player-facing but is not otherwise classified.
- `text_display`: Text Display or display-entity text field.
- `text_component_translate`: Existing `translate` key reference. Translate the corresponding language key when available.

## Risk Levels

Low risk:

- `lang` entries in a map-owned namespace.
- Existing `translate` keys with known language JSON entries.

Medium risk:

- JSON text components in `.mcfunction` or datapack JSON.
- NBT text components with exact NBT paths.
- Aggregated sign faces with exact per-line `nbt_path` and segment anchors.
- Quoted command/SNBT JSON text components with exact `command_string_span` anchors.
- Scoreboard/team display text when the scanner identifies display-name paths.

High risk:

- Binary region edits without exact path and chunk anchor.
- Plain NBT strings from broad path hints.
- Plain SNBT strings inside command strings. These can be direct-patched only when the scanner records an exact `command_string_span` and the user accepts copied-world direct patching.
- Puzzle text, spelling-dependent clues, command-generated UI, or text mixed with selectors and score values.

Plain `nbt_text` units without JSON text component context are not hybrid key-injection targets. They can be applied only through explicit `embedded-direct` copied-world output with `apply-direct-nbt-strings`, or left as known uncovered text in a resource-pack-first workflow.
`sign` units may represent an entire sign face. Translate `raw` as the whole four-line sign, then fill `segments[]` so each line/text node can be written back safely.

## Translation Key Strategy

For generated keys, use:

```text
<namespace>.<map_slug>.<source_kind>.<stable_id>
```

Keep original keys for `lang` units. Generated keys are for hybrid key injection or future apply tools.

## Common False Positives

- Scoreboard fake player names and internal objective IDs.
- Generated animation/datapack framework diagnostics.
- Custom font glyph strings that decode as unusual Unicode.
- Storage keys, tag names, loot table IDs, recipe IDs, and predicate IDs.
- Empty vanilla language overrides, often used to hide UI labels.
- Command block `LastOutput`; scan excludes it by default because it is usually execution log text, not authored player-facing copy.
- Translation keys themselves during residual-English audit. A dotted key like `mcmap.example.sign.abc123` is not visible English copy.

When false positives dominate a source folder, use `make-workpacks --source-file-regex` or TSV filtering to isolate the useful units rather than editing scanner output by hand.
