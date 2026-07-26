# QA Rules

Use this reference before apply or export.

## Structural QA

- Validate every JSON and JSONL file.
- Validate language files under `assets/<namespace>/lang/*.json`.
- Validate `pack.mcmeta`.
- Validate target locale filenames use Java locale format such as `ja_jp`, `fr_fr`, or `zh_cn`.
- Validate TSV imports by running `validate-units` on the merged `translations.jsonl`.
- Confirm all generated JSON, JSONL, TSV, and language files are valid UTF-8 and contain no replacement characters or mojibake from terminal/table round-trips.
- Confirm escape shape survived scan, edit, merge, export, and apply: real newlines remain real line breaks; literal `\\n` remains literal backslash+n; JSON/SNBT escapes are not double-escaped or consumed.
- For patched worlds, validate each changed NBT/SNBT/JSON file with the appropriate parser.
- Ensure exported zip roots are correct: `pack.mcmeta` must be at the resource-pack root.
- For copied worlds with embedded packs, ensure `resources.zip` is in the same directory as the copied world's `level.dat`; for map zips with a top-level containing folder, the correct path is usually `<folder>/resources.zip`, not archive-root `resources.zip`.
- If the source map already had `resources.zip`, verify embedded exports merged into the copied existing pack rather than replacing it. Confirm original non-language assets such as textures, sounds, fonts, models, and custom item assets still exist in the exported `resources.zip`, and that the original `pack.mcmeta` was preserved unless replacement was intentional.
- After `apply-hybrid-keys`, inspect `mcmap_hybrid_apply_report.json` and rescan the copied world or copied zip when practical.
- After `apply-direct-text` or legacy `apply-direct-nbt-strings`, inspect `mcmap_direct_text_apply_report.json` and rescan the copied world or copied zip when practical.
- After export/apply, run `audit-english` on the copied world or copied zip when the target language is not English and the map has hardcoded text. Treat findings in `.mcfunction`, command `Command`, datapack JSON storage/dialogue paths, sign `messages`, `CustomName`, display/lore, and book/page paths as high-priority QA leads.
- Pass `--target-locale` to `audit-english`. Source-language and unrelated locale lang files are excluded by default so they cannot exhaust the finding limit before hardcoded world text. Review sign findings as whole faces, not isolated lines.
- Run `qa-translations` before export and require `status: pass` with `remaining_units: 0` and `allow_incomplete: false`. `--allow-incomplete` is for interim review only and cannot authorize delivery.

## Translation QA

- Translations were produced or reviewed by Codex against map context, not accepted blindly from an external machine-translation service.
- Every scanner candidate received a semantic disposition based on player visibility, source context, gameplay role, identity coupling, and apply safety. Scanner inclusion or English-looking text alone was not treated as proof that it should be translated.
- Command-backed units were reviewed as structured commands: rendered message/component payloads were localized where appropriate, while grammar and logic operands remained exact. Neither "translate the whole command" nor "preserve everything from commands" is acceptable.
- Protected tokens are unchanged.
- Selectors, placeholders, color codes, click/hover events, keybinds, and newlines are preserved or intentionally changed with notes.
- Backslash escapes such as `\n`, `\t`, `\"`, `\\`, and `\uXXXX` are preserved at the correct layer. If changed, the QA report explains which source layer was intentionally rewritten.
- No accidental translation of internal IDs or command syntax.
- No visible command-generated prose was incorrectly left untranslated merely because its source was a command, function, SNBT string, or command block.
- No untranslated English remains except deliberate names, IDs, or stylistic choices.
- Any source-equal translation has one approved status (`intentional_name`, `code`, `ascii_art`, or `puzzle_token`) and a concrete reason. A non-empty copied source string without that evidence is `unreviewed_same_as_source`, not translated.
- Glossary terms are consistent.
- Duplicate language keys have identical intended meaning or are split.
- Grouped text components are translated as complete messages, not as isolated style fragments.
- For `segments[]`, the full unit `translation` and each segment translation should agree semantically; segment translations should not read like unedited word-by-word fragments.
- For aggregated sign units, the four-line `raw` is the translation source of truth. Segment translations should preserve readable sign layout in the target language, not mechanically translate each source line.
- Every player-text sign face is aggregated, including one-line faces. Compare `sign_faces_seen`, `aggregated_sign_groups`, and `sign_faces_without_player_text`; unexplained isolated sign-line rows or a large aggregation gap is a scanner QA failure.
- Identity-coupled groups have one canonical unit key and one key per segment slot, plus consistent translations. Any conflict blocks delivery.
- Selector-coupled entity names and NBT selector literals remain source-equal, have an `intentional_name` reason, and expose neither `hybrid-key-injection` nor `embedded-direct`. Any changed unit/segment or restored copied-world patch mode blocks delivery.
- Target-language scripts, accents, punctuation width, right-to-left text, emoji, and Minecraft section sign formatting survive scan, edit, merge, export, and apply without corruption.
- Stiff literal phrasing, context-inconsistent terminology, untranslated player-facing residues, and unexplained skipped difficult text are treated as QA failures.

## Coverage QA

Report counts by:

- Total units.
- Units translated.
- Units skipped.
- Selected user-facing export mode: `resource-pack-only`, `embedded-pack-copy`, `hybrid-keyed-copy`, or `direct-text-copy`.
- Units covered by `resource-pack`.
- Units requiring `hybrid-key-injection`.
- Units requiring `embedded-direct`.
- Low-confidence anchors needing manual review.
- Excluded `LastOutput` count and whether `--include-last-output` was intentionally used.
- Aggregated sign groups and segment coverage.
- Sign faces discovered, aggregated, without player text, complete, changed, no-op, already applied, and skipped.
- Identity-coupled unit/group counts, repeated groups, role coverage, and key/translation conflicts.
- `@e[name=...]` and `@e[nbt={CustomName:...}]` reference counts, matched/protected units, unmatched static references, and dynamic macro references from `selector_identity.json`.
- Residual-English audit findings after export/apply.
- Datapack function call graph coverage and suspicious text hints reviewed.
- Player-facing units intentionally left untranslated, with concrete reasons.
- Unresolved semantic decisions retained in the progress TODO rather than guessed or counted as complete.
- Files reported as pending binary parser coverage.
- Resource-pack visual text asset hints, especially PNG textures and font provider JSON that language JSON cannot cover.
- PNG inventory versus path-filtered visual-text candidates. Do not present the full PNG inventory as confirmed untranslated text; record OCR/visual-review decisions for candidates.
- Existing map `resources.zip` paths and whether the export preserved/merged them.
- Top repeated raw strings and top source files from `scan_review.md`.
- Any encoding or font-rendering risks found during table round-trip, resource-pack export, copied-world apply, or in-game review.
- Any escape-shape risks found during JSONL/TSV round-trip, resource-pack export, copied-world apply, or in-game review.

When reporting "full translation", state the exact export mode delivered. Full translation is not a fifth mode: it means the selected mode is the least invasive one that covers the scanned player-facing text. `hybrid-keyed-copy` is usually the safest complete mode for hardcoded JSON text; `direct-text-copy` is the maximum-coverage mode and must be named explicitly and confirmed by the user.

## Risk Report

For any world patch, report:

- Files changed.
- Number of anchors changed.
- Backup/copy path.
- Resource-pack merge mode, base pack path, and overwritten entry count when `resources.zip` was embedded.
- Parser confidence.
- Known unhandled source kinds.
- Commands or text components that could not be safely transformed.
- Apply counts by type (`sign_face`, `item_name`, `item_lore`, and other source kinds): selected, changed, no-op, already, and skipped.
- `segment_count_mismatch`, `segment_source_text_mismatch`, `existing_translate_conflict`, `multiple_text_nodes`, and other skip reasons from the hybrid apply report.
- `sign_segment_source_text_mismatch`, `sign_line_nbt_path_missing`, `source_text_mismatch`, `translation_too_long_for_nbt_string`, `missing_direct_command_span`, `command_json_path_missing`, `json_string_path_missing`, `missing_region_chunk_anchor`, and other skip reasons from apply reports.

## Identity Gameplay QA

`qa-translations` writes a separate `identity_qa.json`. Final delivery is blocked when it finds:

- an `identity_coupled` row without a parsed structural identity or reviewed manual identity;
- multiple item fingerprints or text slots inside one canonical identity group;
- different translation keys or translations inside one identity slot group;
- a `trade_input`, `consumer`, or `predicate` item with no structurally equal scanned `producer`, `container`, or `trade_output`;
- a manual group or external-source exception without a concrete review reason.
- an entity name or NBT selector literal marked `selector_identity_coupled` whose translation/segment differs from source, whose preserve-source strategy is missing, or whose copied-world patch modes were re-enabled.

The report also lists equal visible wording found on different item fingerprints. This is diagnostic, not automatically an error: same-named items with different lore/custom data should remain separate.

Static identity QA proves parsed structure and scanned relationships, not runtime behavior. It protects recognized static `@e[name=...]` and NBT `CustomName` selectors, but cannot resolve arbitrary macros, storage-built commands, plugins, or selectors constructed at runtime. For currencies, quest items, keys, named NPCs, and predicate-driven rewards, also test a fresh save in game:

1. obtain each item from every relevant producer;
2. use it in each villager trade or menu consumer;
3. test `clear`, `execute if items`, predicates, quest completion, and rewards;
4. reload the map and repeat one representative path;
5. verify similarly named items with different lore/custom data were not merged.
6. verify each named NPC can still be selected, excluded by negated selectors, triggered, killed/replaced, and found again after reload.

Use `resolve-item-identities` only after reading all referenced anchors and non-text evidence. Approve an external source only for a documented macro/storage/plugin/dynamic-loot path that the scanner cannot materialize. Do not use the exception merely to make QA green.

Do not generalize a raw global string replacement as the fix for identity failures. Repair canonical keys through exact scanner anchors and regenerate the copied map.

## Workpack QA

- Use `make-workpacks --dedupe-raw` for glossary and first-pass translation.
- Use non-deduped workpacks when final translations must preserve context per occurrence.
- Use `export-table`/`import-table` only with the stable `id` column intact.
- Do not sort or regenerate IDs manually.
