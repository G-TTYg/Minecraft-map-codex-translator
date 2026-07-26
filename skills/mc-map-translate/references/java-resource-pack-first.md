# Java Resource-Pack-First Export

Use this reference when the target is Java Edition and the user wants a non-invasive export.

## Export Modes

Use these four names when explaining choices to the user. The internal unit `mode_support` values remain `resource-pack`, `hybrid-key-injection`, and `embedded-direct`.

| User-facing mode | World data changed? | Main output | Covers | Does not cover |
| --- | --- | --- | --- | --- |
| `resource-pack-only` | No | Standalone resource-pack zip | Existing language keys, map-owned lang JSON, resources already designed for localization | Hardcoded command/sign/book/entity text, plain NBT strings, image/font pixels |
| `embedded-pack-copy` | Copied world only | Copied map/world containing `resources.zip` | Same text coverage as `resource-pack-only`, but easier for players because the pack travels with the save. If the map already has `resources.zip`, generated language files are merged into that copied pack. | Hardcoded text unless it already uses translation keys |
| `hybrid-keyed-copy` | Copied world/map patched | Copied map/world zip plus matching resource pack or embedded `resources.zip` | Existing resource-pack units plus supported hardcoded JSON text components, command JSON spans, sign segments, books, titles, bossbars, and datapack JSON text components that can become `translate` keys. If embedded, preserve and merge any existing `resources.zip`. | Plain strings that are not JSON text components; image/font pixels |
| `direct-text-copy` | Copied world/map patched | Copied map/world zip with direct literal text replacements | Supported plain command messages, SNBT/datapack JSON strings, and NBT strings that cannot be key-injected; may start from a hybrid-keyed copy when both source kinds exist | Higher-risk or unsupported anchors; image/font pixels |

## What "Full Translation" Means

"Full translation" or "complete localization" is not a fifth export mode. It means choosing the least invasive one of the four modes that covers the scanned player-facing text.

Typical selection:

- Use `resource-pack-only` when all player-facing text is already reachable through language/resource keys.
- Use `embedded-pack-copy` when the same coverage is enough but the save should carry `resources.zip`.
- Use `hybrid-keyed-copy` as the safest complete mode when the scan finds hardcoded JSON text components.
- Use `direct-text-copy` as the maximum-coverage mode when direct-only plain command/SNBT/datapack JSON/NBT strings remain and the user explicitly accepts the risk.

Each mode may produce several artifacts, such as a map zip, a resource-pack zip, `resources.zip`, apply reports, residual-English audit, and visual asset findings. These are artifacts of the selected mode, not extra modes.

If a map stores player-visible text as hardcoded literals, full localization usually requires a modified copied map. That modified copy is still safer than editing the original, but it is no longer a pure vanilla-original world export. If the user refuses any copied-map patching, report the remaining hardcoded text as uncovered by resource-pack-only export.

## Maps With Existing `resources.zip`

Many Java maps already ship a map-specific resource pack as `resources.zip` beside `level.dat`. Treat that file as the base pack.

- Do not replace it with a translation-only pack by default; that would drop original textures, sounds, fonts, models, custom item models, and map UI art.
- For `embedded-pack-copy` and embedded `hybrid-keyed-copy`, merge generated language files and generated hybrid keys into the copied existing `resources.zip`.
- For `resource-pack-only`, decide whether to ship a small overlay pack or a merged full resource pack. Use an overlay only when players will also load the original map pack. Use a merged full pack when the translation pack is meant to replace or stand alone from the original pack.
- Preserve map-owned assets unless the user explicitly requests asset localization or replacement.
- Preserve the existing pack's `pack.mcmeta` during merge when it exists; it usually carries the map pack's compatibility metadata and description.
- If the existing `resources.zip` is corrupt or unreadable, fail loudly instead of overwriting it silently.
- For `apply-hybrid-keys`, a detected source `resources.zip` makes `--resource-pack` merged embedding the default hard requirement. Use `--allow-separate-resource-pack` only when the delivery intentionally requires players to load a separate translated pack and `DELIVERY.md` says so.

`resource-pack` unit support:

- Modify no world data.
- Translate existing `assets/<namespace>/lang/*.json` entries and any text already expressed through JSON text component `translate` keys.
- Export a standalone resource pack directory or zip.
- Optionally embed it into a copied Java save as `resources.zip`.
- Report hardcoded text as uncovered unless the workflow continues into hybrid key injection.
- Report visual text in PNG/font/model assets as separate QA work; language JSON cannot translate pixels or bitmap font glyph art.

`hybrid-key-injection` unit support:

- Patch a copied map so hardcoded player-facing text becomes `{"translate":"<key>"}` while style and events remain intact.
- Export matching language files in a resource pack.
- Prefer this for maps with many hardcoded command-block or function texts when the user still wants resource-pack-driven localization.
- Treat it as low-to-medium risk because map data changes, even though translations live in the pack.
- The bundled `apply-hybrid-keys` tool performs exact-anchor key injection in a copied world or copied map zip.
- `make-resource-pack` includes these generated hardcoded-text keys only when called with `--include-hybrid-keys`.

`apply-hybrid-keys` is conservative but segment-aware. Single-node hardcoded components use the unit key. Multi-node hardcoded components use `segments[]` and `--multi-text-mode split-nodes` to inject one key per original `text` node, preserving styles and dynamic sibling components. If segment anchors or source text do not match, the unit is skipped and reported.
Aggregated sign faces also use `segments[]`: translate the full sign first, then fill each segment so the apply step can replace each original sign line/text node with a generated key.
Structurally resolved item name/lore components marked `identity_coupled` use canonical keys per full item fingerprint and text slot instead of occurrence keys. Unresolved item identities block export; equal wording alone is never enough to merge. This is required when text-component equality participates in trades, predicates, `clear`, rewards, or quest logic.

`embedded-direct` unit support:

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

For map-specific Java world embedding, zip the resource-pack contents with `pack.mcmeta` at the zip root and place the zip in the copied world as `resources.zip`. If the copied world already has `resources.zip`, merge the generated pack over the existing copied pack by default.

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

For Java worlds, a copied world can include `resources.zip` at the world root. This makes the map prompt/load its intended resource pack without asking every player to manually install a standalone pack. Still treat this as an export of a copied world, not as an in-place modification. When the original world already includes `resources.zip`, the copied export should preserve it and overlay generated translation files.

For zip input, `apply-hybrid-keys` and `apply-direct-text` can write copied output zips directly. For directory input, they can write copied directories; `apply-hybrid-keys` can also embed the generated resource pack as `resources.zip`.

## Canonical Delivery

After translation QA, apply, and residual audit, run `write-delivery`. `exports/DELIVERY.md` must name exactly one primary artifact and one of the four export modes. When the scan found an original `resources.zip`, copied-map delivery requires an apply/embed report proving merge mode. Interim `qa-translations --allow-incomplete` reports are rejected.
