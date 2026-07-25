# QA Rules

Use this reference before apply or export.

## Structural QA

- Validate every JSON and JSONL file.
- Validate language files under `assets/<namespace>/lang/*.json`.
- Validate `pack.mcmeta`.
- Validate target locale filenames use Java locale format such as `ja_jp`, `fr_fr`, or `zh_cn`.
- Validate TSV imports by running `validate-units` on the merged `translations.jsonl`.
- Confirm all generated JSON, JSONL, TSV, and language files are valid UTF-8 and contain no replacement characters or mojibake from terminal/table round-trips.
- For patched worlds, validate each changed NBT/SNBT/JSON file with the appropriate parser.
- Ensure exported zip roots are correct: `pack.mcmeta` must be at the resource-pack root.
- For copied worlds with embedded packs, ensure `resources.zip` is in the same directory as the copied world's `level.dat`; for map zips with a top-level containing folder, the correct path is usually `<folder>/resources.zip`, not archive-root `resources.zip`.
- After `apply-hybrid-keys`, inspect `mcmap_hybrid_apply_report.json` and rescan the copied world or copied zip when practical.
- After `apply-direct-nbt-strings`, inspect `mcmap_direct_nbt_apply_report.json` and rescan the copied world or copied zip when practical.

## Translation QA

- Translations were produced or reviewed by Codex against map context, not accepted blindly from an external machine-translation service.
- Protected tokens are unchanged.
- Selectors, placeholders, color codes, click/hover events, keybinds, and newlines are preserved or intentionally changed with notes.
- No accidental translation of internal IDs or command syntax.
- No untranslated English remains except deliberate names, IDs, or stylistic choices.
- Glossary terms are consistent.
- Duplicate language keys have identical intended meaning or are split.
- Grouped text components are translated as complete messages, not as isolated style fragments.
- For `segments[]`, the full unit `translation` and each segment translation should agree semantically; segment translations should not read like unedited word-by-word fragments.
- Target-language scripts, accents, punctuation width, right-to-left text, emoji, and Minecraft section sign formatting survive scan, edit, merge, export, and apply without corruption.
- Stiff literal phrasing, context-inconsistent terminology, untranslated player-facing residues, and unexplained skipped difficult text are treated as QA failures.

## Coverage QA

Report counts by:

- Total units.
- Units translated.
- Units skipped.
- Units covered by `resource-pack`.
- Units requiring `hybrid-key-injection`.
- Units requiring `embedded-direct`.
- Low-confidence anchors needing manual review.
- Player-facing units intentionally left untranslated, with concrete reasons.
- Files reported as pending binary parser coverage.
- Top repeated raw strings and top source files from `scan_review.md`.
- Any encoding or font-rendering risks found during table round-trip, resource-pack export, copied-world apply, or in-game review.

## Risk Report

For any world patch, report:

- Files changed.
- Number of anchors changed.
- Backup/copy path.
- Parser confidence.
- Known unhandled source kinds.
- Commands or text components that could not be safely transformed.
- `segment_count_mismatch`, `segment_source_text_mismatch`, `existing_translate_conflict`, `multiple_text_nodes`, and other skip reasons from the hybrid apply report.
- `source_text_mismatch`, `translation_too_long_for_nbt_string`, `missing_region_chunk_anchor`, and other skip reasons from the direct NBT apply report.

## Workpack QA

- Use `make-workpacks --dedupe-raw` for glossary and first-pass translation.
- Use non-deduped workpacks when final translations must preserve context per occurrence.
- Use `export-table`/`import-table` only with the stable `id` column intact.
- Do not sort or regenerate IDs manually.
