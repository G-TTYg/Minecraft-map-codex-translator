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
- After `apply-hybrid-keys`, inspect `mcmap_hybrid_apply_report.json` and rescan the copied world or copied zip when practical.
- After `apply-direct-text` or legacy `apply-direct-nbt-strings`, inspect `mcmap_direct_text_apply_report.json` and rescan the copied world or copied zip when practical.
- After export/apply, run `audit-english` on the copied world or copied zip when the target language is not English and the map has hardcoded text. Treat findings in `.mcfunction`, command `Command`, datapack JSON storage/dialogue paths, sign `messages`, `CustomName`, display/lore, and book/page paths as high-priority QA leads.

## Translation QA

- Translations were produced or reviewed by Codex against map context, not accepted blindly from an external machine-translation service.
- Protected tokens are unchanged.
- Selectors, placeholders, color codes, click/hover events, keybinds, and newlines are preserved or intentionally changed with notes.
- Backslash escapes such as `\n`, `\t`, `\"`, `\\`, and `\uXXXX` are preserved at the correct layer. If changed, the QA report explains which source layer was intentionally rewritten.
- No accidental translation of internal IDs or command syntax.
- No untranslated English remains except deliberate names, IDs, or stylistic choices.
- Glossary terms are consistent.
- Duplicate language keys have identical intended meaning or are split.
- Grouped text components are translated as complete messages, not as isolated style fragments.
- For `segments[]`, the full unit `translation` and each segment translation should agree semantically; segment translations should not read like unedited word-by-word fragments.
- For aggregated sign units, the four-line `raw` is the translation source of truth. Segment translations should preserve readable sign layout in the target language, not mechanically translate each source line.
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
- Residual-English audit findings after export/apply.
- Datapack function call graph coverage and suspicious text hints reviewed.
- Player-facing units intentionally left untranslated, with concrete reasons.
- Files reported as pending binary parser coverage.
- Resource-pack visual text asset hints, especially PNG textures and font provider JSON that language JSON cannot cover.
- Top repeated raw strings and top source files from `scan_review.md`.
- Any encoding or font-rendering risks found during table round-trip, resource-pack export, copied-world apply, or in-game review.
- Any escape-shape risks found during JSONL/TSV round-trip, resource-pack export, copied-world apply, or in-game review.

When reporting "full translation", state the exact export mode delivered. Full translation is not a fifth mode: it means the selected mode is the least invasive one that covers the scanned player-facing text. `hybrid-keyed-copy` is usually the safest complete mode for hardcoded JSON text; `direct-text-copy` is the maximum-coverage mode and must be named explicitly and confirmed by the user.

## Risk Report

For any world patch, report:

- Files changed.
- Number of anchors changed.
- Backup/copy path.
- Parser confidence.
- Known unhandled source kinds.
- Commands or text components that could not be safely transformed.
- `segment_count_mismatch`, `segment_source_text_mismatch`, `existing_translate_conflict`, `multiple_text_nodes`, and other skip reasons from the hybrid apply report.
- `sign_segment_source_text_mismatch`, `sign_line_nbt_path_missing`, `source_text_mismatch`, `translation_too_long_for_nbt_string`, `missing_direct_command_span`, `command_json_path_missing`, `json_string_path_missing`, `missing_region_chunk_anchor`, and other skip reasons from apply reports.

## Workpack QA

- Use `make-workpacks --dedupe-raw` for glossary and first-pass translation.
- Use non-deduped workpacks when final translations must preserve context per occurrence.
- Use `export-table`/`import-table` only with the stable `id` column intact.
- Do not sort or regenerate IDs manually.
