# QA Rules

Use this reference before apply or export.

## Structural QA

- Validate every JSON and JSONL file.
- Validate language files under `assets/<namespace>/lang/*.json`.
- Validate `pack.mcmeta`.
- Validate target locale filenames use Java locale format such as `ja_jp`, `fr_fr`, or `zh_cn`.
- Validate TSV imports by running `validate-units` on the merged `translations.jsonl`.
- For patched worlds, validate each changed NBT/SNBT/JSON file with the appropriate parser.
- Ensure exported zip roots are correct: `pack.mcmeta` must be at the resource-pack root.
- After `apply-hybrid-keys`, inspect `mcmap_hybrid_apply_report.json` and rescan the copied world or copied zip when practical.

## Translation QA

- Protected tokens are unchanged.
- Selectors, placeholders, color codes, click/hover events, keybinds, and newlines are preserved or intentionally changed with notes.
- No accidental translation of internal IDs or command syntax.
- No untranslated English remains except deliberate names, IDs, or stylistic choices.
- Glossary terms are consistent.
- Duplicate language keys have identical intended meaning or are split.
- Grouped text components are translated as complete messages, not as isolated style fragments.

## Coverage QA

Report counts by:

- Total units.
- Units translated.
- Units skipped.
- Units covered by `resource-pack`.
- Units requiring `hybrid-key-injection`.
- Units requiring `embedded-direct`.
- Low-confidence anchors needing manual review.
- Files reported as pending binary parser coverage.
- Top repeated raw strings and top source files from `scan_review.md`.

## Risk Report

For any world patch, report:

- Files changed.
- Number of anchors changed.
- Backup/copy path.
- Parser confidence.
- Known unhandled source kinds.
- Commands or text components that could not be safely transformed.
- `multiple_text_nodes`, `source_text_mismatch`, `existing_translate_conflict`, and other skip reasons from the hybrid apply report.

## Workpack QA

- Use `make-workpacks --dedupe-raw` for glossary and first-pass translation.
- Use non-deduped workpacks when final translations must preserve context per occurrence.
- Use `export-table`/`import-table` only with the stable `id` column intact.
- Do not sort or regenerate IDs manually.
