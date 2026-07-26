# Java Resource-Pack-First Export

Use this reference when the target is Java Edition and the user wants a non-invasive export.

## Export Modes

`resource-pack`:

- Modify no world data.
- Translate existing `assets/<namespace>/lang/*.json` entries and any text already expressed through JSON text component `translate` keys.
- Export a standalone resource pack directory or zip.
- Optionally embed it into a copied Java save as `resources.zip`.
- Report hardcoded text as uncovered unless the workflow continues into hybrid key injection.
- Report visual text in PNG/font/model assets as separate QA work; language JSON cannot translate pixels or bitmap font glyph art.

`hybrid-key-injection`:

- Patch a copied map so hardcoded player-facing text becomes `{"translate":"<key>"}` while style and events remain intact.
- Export matching language files in a resource pack.
- Prefer this for maps with many hardcoded command-block or function texts when the user still wants resource-pack-driven localization.
- Treat it as low-to-medium risk because map data changes, even though translations live in the pack.
- The bundled `apply-hybrid-keys` tool performs exact-anchor key injection in a copied world or copied map zip.
- `make-resource-pack` includes these generated hardcoded-text keys only when called with `--include-hybrid-keys`.

`apply-hybrid-keys` is conservative but segment-aware. Single-node hardcoded components use the unit key. Multi-node hardcoded components use `segments[]` and `--multi-text-mode split-nodes` to inject one key per original `text` node, preserving styles and dynamic sibling components. If segment anchors or source text do not match, the unit is skipped and reported.
Aggregated sign faces also use `segments[]`: translate the full sign first, then fill each segment so the apply step can replace each original sign line/text node with a generated key.

`embedded-direct`:

- Replace literal text directly inside copied map data.
- Use only when the user explicitly does not want a resource pack.
- Treat as high risk and require full backup, precise anchors, and validation.
- For plain command, SNBT, datapack JSON, or NBT strings that are not JSON text components, use `apply-direct-text`; it requires exact anchors such as `command_plain_span`, `command_string_span`, `command_json_path`, `json_string_path`, or `nbt_path`, and exact source-text matches in copied data.

## Resource Pack Layout

Write Java language files as:

```text
pack.mcmeta
assets/<namespace>/lang/en_us.json
assets/<namespace>/lang/<target_locale>.json
```

For map-specific Java world embedding, zip the resource-pack contents with `pack.mcmeta` at the zip root and place the zip in the copied world as `resources.zip`.

## Coverage Rule

A resource pack can override language entries and resources. It cannot translate an arbitrary hardcoded literal in a command block, sign, book, or function unless that text is already referenced by a language key or the workflow patches a copied map to use one.
It also cannot translate English baked into images, custom font textures, or map art without separate asset localization.

## Key Naming

Prefer stable, map-scoped keys:

```text
mcmap.<map_slug>.<source_kind>.<stable_id>
```

Examples:

```text
mcmap.castle_escape.book.a1b2c3d4
mcmap.castle_escape.command_block.f3e9a7b1
mcmap.castle_escape.bossbar.dragon_warning
```

Keep generated keys lowercase and limited to `a-z`, digits, `_`, `.`, and `-`.

For existing map-owned language files, keep the original key and namespace so the resource pack overrides the map cleanly.

## Pack Format

Do not hardcode `pack_format` unless the user specifies the Minecraft version. If unknown, ask or infer from `DataVersion` and report uncertainty.

## Embedded World Export

For Java worlds, a copied world can include `resources.zip` at the world root. This makes the map prompt/load its intended resource pack without asking every player to manually install a standalone pack. Still treat this as an export of a copied world, not as an in-place modification.

For zip input, `apply-hybrid-keys` and `apply-direct-text` can write copied output zips directly. For directory input, they can write copied directories; `apply-hybrid-keys` can also embed the generated resource pack as `resources.zip`.
