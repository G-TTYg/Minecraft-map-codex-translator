# Tooling

Use this reference before running bundled scripts.

## Scope

The plugin includes its own standard tools. Do not depend on MCC-i18n or copy its code into this plugin. The useful standardized pattern is: inspect, scan, create an indexed project, translate one contextual workpack at a time, merge staged translations, validate, export a resource pack, and optionally embed that resource pack into a copied Java world.

## Tools

`scripts/mcmap_contract.py`:

- `init-workspace`: create the standard project folder and `project.json`.
- `validate-units`: validate `translation_units.jsonl` or `translations.jsonl`, including protected-token and UTF-8 replacement-character checks.
- `summarize-units`: summarize translation coverage.
- `make-project-files`: create the indexed multi-file project layout for staged AI translation.
- `make-workpacks`: split units into stable JSONL translation batches.
- `merge-translations`: merge translated JSONL files/directories back into one canonical translations JSONL by stable `id`.
- `translation-status`: report coverage by unit, source kind, source file, and segment slots.
- `write-progress-todo`: write or refresh `translation_progress.md`, the persistent workpack TODO list.
- `prepare-segments`: add `segments[]` translation slots for grouped components with multiple hardcoded `text` nodes.
- `export-table`: export selected units to UTF-8 TSV.
- `import-table`: merge TSV translations back into JSONL by `id`.
- `export-segment-table`: export multi-text segment slots to UTF-8 TSV.
- `import-segment-table`: merge translated segment TSV files back into JSONL.
- `make-resource-pack`: create `pack.mcmeta` and target language JSON files from translations. By default it exports only units that can work in resource-pack mode; pass `--include-hybrid-keys` only when a copied-map key-injection apply step will use those generated keys.

`scripts/mcmap_java_tools.py`:

- `inspect`: detect Java map/package markers and Bedrock-only markers.
- `scan`: scan Java resource-pack language JSON, datapack JSON text components, `.mcfunction` JSON text components, supported `.dat` NBT, and supported `.mca` region chunks into `translation_units.jsonl`. Pass `--project-layout` to also create the indexed multi-file layout. NBT strings are decoded as strict UTF-8; invalid bytes are reported instead of converted to replacement characters.
- `apply-hybrid-keys`: copy/extract a Java world or map zip and inject generated `translate` keys into supported hardcoded JSON text components in the copy. Blocks selected rows that contain Unicode replacement characters.
- `apply-direct-nbt-strings`: copy/extract a Java world or map zip and directly replace translated plain NBT strings in `.dat` and `.mca` files when exact anchors match. Blocks selected rows that contain Unicode replacement characters.
- `zip-resource-pack`: zip a resource pack directory with the correct root.
- `embed-resource-pack`: copy a Java world and add `resources.zip` to the copy.

## Recommended Order

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect <world-or-zip>
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan <world-or-zip> --out <workdir> --target <target_locale> --source-locale en_us --map-slug <slug> --project-layout --max-workpack-units 120
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py summarize-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status <workdir>
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo <workdir>
```

## Encoding Discipline

The bundled scripts read and write JSON, JSONL, TSV, language JSON, and reports with UTF-8. Keep that invariant when editing or generating files outside the scripts.

- Prefer the bundled import/export commands for TSV and JSONL instead of ad hoc shell redirection.
- When scripting translation edits, open files with `encoding="utf-8"` or `encoding="utf-8-sig"` for uncertain input and write UTF-8 output.
- On Windows, terminal output may misrender valid Unicode. Treat mojibake in the console as a display warning, not proof that the file is corrupt; verify with an explicit UTF-8 reader before changing data.
- Do not use a lossy console, clipboard, spreadsheet save, or shell pipeline as the only copy of translated non-ASCII text.
- Treat `U+FFFD` as a blocking data-loss signal. `validate-units`, table export/import, translation merge, resource-pack export, and copied-world apply commands reject rows containing it.
- After table round-trips, rerun `validate-units` and spot-check representative rows for target-language characters, accents, right-to-left text, emoji, section signs, and placeholders.

Codex then translates staged batches:

- read `index/manifest.json`;
- read and maintain `translation_progress.md` as the persistent workpack TODO list;
- load one `workpacks/contextual/workpack_###.jsonl`;
- load only the `context_summaries` listed for that workpack;
- write translations to the matching `translations/parts/workpack_###.jsonl`;
- refresh `translation_progress.md` after each workpack;
- update `glossary.md` when terminology decisions are made.

After Codex fills one or more translation parts:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations <workdir>/translations/parts --base <workdir>/translation_units.jsonl --out <workdir>/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units <workdir>/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status <workdir>/translation_units.jsonl --translations <workdir>/translations/translations.jsonl --incomplete-only
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo <workdir>
```

If a translation part changes after `translations/translations.jsonl` exists, run `merge-translations` again before exporting from the project root.

For standalone resource-pack export:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir> --out <workdir>/exports/resource-pack --pack-format <pack_format> --target <target_locale>
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack <workdir>/exports/resource-pack --out <workdir>/exports/resource-pack.zip
```

For hybrid key-injection preparation:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir> --out <workdir>/exports/hybrid-resource-pack --pack-format <pack_format> --target <target_locale> --include-hybrid-keys
```

Then patch a copied world or copied map zip:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir> --out <workdir>/exports/world-keyed --resource-pack <workdir>/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir> --out <workdir>/exports/world-keyed.zip
```

For translated plain NBT strings that cannot be key-injected:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-nbt-strings <world-or-zip> --translations <workdir> --out <workdir>/exports/world-direct-nbt.zip
```

To ship a copied world with the pack embedded:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py embed-resource-pack <world> --resource-pack <workdir>/exports/resource-pack --out <workdir>/exports/world-with-resources
```

## Apply-Hybrid-Keys Behavior

`apply-hybrid-keys` never edits the source path. It copies directories with `copytree` or extracts zip packages to a temporary/copy directory, applies exact anchored patches, and writes `mcmap_hybrid_apply_report.json` or a sidecar report for zip output.

Supported automatic patches:

- `.mcfunction` JSON command spans from `function_line` and `command_span`.
- Datapack JSON text components from `json_path`.
- Gzip/plain `.dat` NBT strings containing JSON text components or commands with JSON spans.
- Standard gzip, zlib, or raw `.mca` chunk NBT strings with exact chunk and NBT path anchors.

Safety limits:

- Single-node hardcoded JSON text components use the unit `translation_key`.
- Multi-node hardcoded JSON text components use `segments[]` by default through `--multi-text-mode split-nodes`.
- Every target `text` value must still exactly equal the recorded source segment; otherwise the unit is skipped.
- Existing `translate` conflicts, missing paths, unsafe paths, nested `resources.zip!` paths, and missing/invalid segment keys are skipped and reported.
- Plain NBT strings without JSON text component context are not hybrid-key-injection targets; they require explicit `embedded-direct` handling.

Use `--multi-text-mode skip` only when you want the old conservative behavior for audit or comparison.

## Apply-Direct-NBT-Strings Behavior

`apply-direct-nbt-strings` never edits the source path. It selects only Java units with `embedded-direct`, an `address.nbt_path`, no `json_path`, a supported `.dat` or `.mca` source file, and a filled `translation` unless `--allow-empty-translation` is passed.

Safety limits:

- Every target NBT string must still exactly equal the unit `raw`; otherwise it is skipped.
- `.mca` rows must include a chunk `local_index` anchor.
- Translations longer than the Java NBT string limit are skipped.
- JSON text components and command JSON spans are skipped here; use `apply-hybrid-keys` for those.
- The command writes `mcmap_direct_nbt_apply_report.json` or a sidecar report for zip output.

## Coverage Limits

The bundled scanner parses gzip NBT `.dat` files and `.mca` chunks using standard gzip, zlib, and raw NBT compression. It reports unsupported compression, too-small region files, or parse failures under `pending_binary_parser_coverage` and `warnings`.

Use `--no-binary` when the map is huge, when a fast first pass is enough, or when binary parsing is producing too much technical noise.

## Output Files

- `project.json`: target locale, source path, namespace, and mode.
- `translation_units.jsonl`: canonical units for translation.
- `scan_report.json`: machine-readable counts, warnings, top files, repeated text, and binary coverage.
- `scan_review.md`: human-readable triage summary.
- `glossary.md`: seed glossary file for Codex to update before translation.
- `translation_progress.md`: persistent workpack TODO list; keep it updated throughout translation.
- `index/manifest.json`: entry point for staged translation.
- `index/unit_index.jsonl`, `index/source_index.jsonl`, `index/kind_index.jsonl`, `index/raw_repeats.jsonl`: compact searchable indexes.
- `context/source-summaries/*.md`: per-source summaries for local context loading.
- `workpacks/contextual/*.jsonl`: bounded context-preserving batches.
- `translations/parts/*.jsonl`: editable staged translation parts.
- `translations/translations.jsonl`: merged canonical translation file.
