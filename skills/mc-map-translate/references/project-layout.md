# Indexed Project Layout

Use this reference when a map is too large to fit into one model context, which is the normal case.

The goal is not to load the whole map at once. The goal is to make the whole map scanable, searchable, and addressable, then load only the context needed for the current translation batch.

This layout supports Codex expert translation, not external batch machine translation. The workpacks are sized so Codex can read enough local map context, reason about gameplay and tone, write polished translations, and then merge/QA the result.

## Standard Layout

`make-project-files` creates these files under the work directory:

- `index/manifest.json`: entry point for the project, workpack list, context summary links, counts, and directories.
- `index/unit_index.jsonl`: compact searchable index; one row per unit with id, source file, kind, raw preview, modes, confidence, and workpack path.
- `index/source_index.jsonl`: one row per source file with unit counts and links to its unit file and source summary.
- `index/kind_index.jsonl`: one row per source kind.
- `index/raw_repeats.jsonl`: repeated raw strings for glossary and consistency review.
- `units/by-source/*.jsonl`: full units grouped by original map/resource path.
- `units/by-kind/*.jsonl`: full units grouped by source kind.
- `context/source-summaries/*.md`: readable local context for each source file.
- `workpacks/contextual/workpack_###.jsonl`: bounded AI translation batches, sorted by source path so nearby text stays nearby.
- `translations/parts/workpack_###.jsonl`: editable translation parts matching workpacks.
- `translations/translations.jsonl`: merged canonical translation output after `merge-translations`.
- `translation_progress.md`: durable workpack TODO list and progress summary.
- `translation_instructions.md`: short local instructions for staged work.

`translation_units.jsonl` remains the canonical scan aggregate. The indexed layout is a working projection of it, not a replacement for stable unit IDs.

## AI Context Loading Rule

For each translation batch:

1. Read `index/manifest.json`.
2. Read or create `translation_progress.md`.
3. Choose one unchecked or in-progress workpack from the TODO.
4. Read the workpack JSONL.
5. Read only the `context_summaries` listed for that workpack.
6. Read `glossary.md` and relevant rows from `index/raw_repeats.jsonl` if terminology repeats.
7. If a unit still lacks context, read its `units/by-source/*.jsonl` group from `source_index.jsonl`.
8. Write translations only to the matching `translations/parts/workpack_###.jsonl`.
9. Refresh `translation_progress.md` after each batch.

When a workpack contains `context.identity_coupled`, load all rows sharing its item fingerprint and slot from the indexes before deciding terminology. Use one translation and keep scanner-provided canonical keys across producers, containers, villager offers, and consumers. For `identity_resolution: unresolved`, inspect the exact anchors and record a reviewed identity decision; do not infer a group from wording alone.

Do not load every file under `units/`, `workpacks/`, and `translations/parts/` at the same time.
Do not send the workpack to an external translation service by default; translate it with Codex using the loaded context.

## Progress TODO

Every real map translation must maintain `translation_progress.md`. This is the durable TODO list for the map, while the Codex task checklist may track the current session's active steps.

Create or refresh it with:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo work/map
```

Refresh the TODO:

- after generating the project layout;
- before starting a new workpack if the file is missing or stale;
- after writing each `translations/parts/workpack_###.jsonl`;
- after `merge-translations`;
- before final export/QA.

Do not mark a workpack complete until every unit has `translation` filled and every required `segments[].translation` slot is filled. Source-equal unit or segment translations remain incomplete until they carry an approved `review_status` and concrete `review_reason`.

## Segment-Aware Translation

Project layout scaffolds `segments[]` by default for grouped text components that have multiple hardcoded `text` nodes.

The translator should:

- translate `raw` as the complete message first;
- preserve selectors, scores, keybinds, hover/click events, newlines, and protected tokens;
- fill each `segments[].translation` so the preserved component sequence reads naturally;
- avoid word-by-word segment translation when a segment is part of a sentence.

## Merge And Export

After one or more parts are translated:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations work/map/translations/parts --base work/map/translation_units.jsonl --out work/map/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/map/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status work/map/translation_units.jsonl --translations work/map/translations/translations.jsonl --incomplete-only
python skills/mc-map-translate/scripts/mcmap_contract.py qa-translations work/map --out work/map/qa/translation_qa.json
```

Export commands accept a merged JSONL, a translation parts directory, or the project root. The project root prefers `translations/translations.jsonl` if it exists, then `translations/parts/*.jsonl`, then `translation_units.jsonl`.

After `translations/translations.jsonl` exists, keep it fresh. If any `translations/parts/*.jsonl` file changes, run `merge-translations` again before exporting from the project root.

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/map --out work/map/exports/hybrid-resource-pack --pack-format 34 --target zh_cn --include-hybrid-keys
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/map.zip --translations work/map --out work/map/exports/map-keyed.zip --resource-pack work/map/exports/hybrid-resource-pack
```

## Conflict Policy

`merge-translations` merges by stable `id`. Unknown ids are counted in the merge report. Conflicting non-empty translations for the same unit or segment block the merge unless `--allow-conflicts` is passed.

This keeps staged AI work auditable: a later batch cannot silently overwrite a different earlier translation.
