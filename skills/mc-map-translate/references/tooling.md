# Tooling

Use this reference before running bundled scripts.

## Scope

The plugin includes its own standard tools. Do not depend on MCC-i18n or copy its code into this plugin. The useful standardized pattern is: inspect, scan, create an indexed project, translate one contextual workpack at a time, merge staged translations, validate, choose one explicit export mode, export a resource pack, and optionally embed or apply it to a copied Java world.

The bundled tools do not call external translation services and do not translate text by themselves. They create reliable local inputs and outputs so Codex can perform context-aware expert localization, then validate and package the result.

## Tools

`scripts/mcmap_contract.py`:

- `init-workspace`: create the standard project folder and `project.json`.
- `validate-units`: validate `translation_units.jsonl` or `translations.jsonl`, including protected-token and UTF-8 replacement-character checks.
- `summarize-units`: summarize translation coverage.
- `make-project-files`: create the indexed multi-file project layout for staged AI translation.
- `make-workpacks`: split units into stable JSONL translation batches.
- `merge-translations`: merge translated JSONL files/directories back into one canonical translations JSONL by stable `id`.
- `translation-status`: report coverage by unit, source kind, source file, and segment slots.
- `qa-translations`: write blocking JSON/Markdown QA plus `identity_qa.json` for incomplete units, unexplained source-equal text, sign-face segment coverage, encoding/contract errors, unresolved item identities, canonical-key/translation/structure conflicts, and missing scanned item sources. `--allow-incomplete` is interim-only and does not bypass identity failures.
- `write-progress-todo`: write or refresh `translation_progress.md`, the persistent workpack TODO list.
- `prepare-segments`: add `segments[]` translation slots for grouped components with multiple hardcoded `text` nodes.
- `export-table`: export selected units to UTF-8 TSV.
- `import-table`: merge TSV translations back into JSONL by `id`.
- `export-segment-table`: export multi-text segment slots to UTF-8 TSV.
- `import-segment-table`: merge translated segment TSV files back into JSONL.
- `make-resource-pack`: create `pack.mcmeta` and target language JSON files from translations. By default it exports only units that can work in resource-pack mode; pass `--include-hybrid-keys` only when a copied-map key-injection apply step will use those generated keys.

`scripts/mcmap_java_tools.py`:

- `inspect`: detect Java map/package markers and Bedrock-only markers.
- `scan`: scan Java resource-pack language JSON, all datapack JSON text components, datapack JSON strings containing text components, `.mcfunction` JSON text components, `execute ... run ...` command chains, command JSON plain strings, quoted command/SNBT JSON text components, plain command messages, storage value strings, whole sign faces, supported `.dat` NBT, and supported `.mca` region chunks into `translation_units.jsonl`. Command-derived units include the complete original command plus its effective command, command word, offset, and execute-wrapper state so Codex can judge rendered payload versus logic without reconstructing the parser. Sign units retain four-line context and block coordinates when available. Parsed item stacks in NBT, villager offers, containers, `give`, `clear`, `item ... with`, and `execute if/unless items` receive structural fingerprints, roles, text slots, and canonical keys. Unparsed item text remains occurrence-keyed and unresolved. Static `@e[name=...]` and `@e[nbt={CustomName:...}]` references are indexed and linked to matching entity names; protected rows lose copied-world patch modes. Pass `--project-layout` to also create the indexed multi-file layout. `LastOutput` is excluded by default. NBT strings are strict UTF-8. Reports include selector identity, function-call context, suspicious strings, path-filtered visual text candidates, PNG inventory, and export recommendations.
- `resolve-item-identities`: apply a reviewed decisions JSON to unresolved item rows, assign one manual item fingerprint, re-canonicalize keys by name/lore slot, and record external/runtime source approvals with reasons. This is the deterministic alternative to ad hoc key editing.
- `apply-hybrid-keys`: copy/extract a Java world or map zip and inject generated `translate` keys into supported hardcoded JSON text components in the copy. It runs identity QA before copying, selects only complete/reviewed translations by default, and reports outcomes by type. If the source already has `resources.zip`, omitting `--resource-pack` is blocked unless `--allow-separate-resource-pack` explicitly documents manual separate-pack delivery. Existing copied packs are merged by default; replacement remains explicit.
- `apply-direct-text`: copy/extract a Java world or map zip and directly replace translated `embedded-direct` anchors in `.mcfunction`, datapack JSON, `.dat`, and `.mca` files when exact anchors match. It runs identity QA before copying, handles `command_plain_span`, plain `command_string_span`, `command_json_path`, datapack JSON `json_string_path`, and parsed NBT strings, and blocks selected rows that contain Unicode replacement characters.
- `apply-direct-nbt-strings`: legacy alias for `apply-direct-text`.
- `audit-english`: rescan an exported copied world or map zip for English-looking residual text in player-facing `.mcfunction`, datapack JSON, commands, whole sign faces, text displays, names/lore, and books. Pass `--target-locale`; unrelated/source lang files are excluded by default. High-priority world text is sorted ahead of lang rows, and PNG warnings are path-filtered candidates.
- `zip-resource-pack`: zip a resource pack directory with the correct root. Pass `--base-resource-pack <resources.zip>` to create a merged full pack from an existing map pack plus generated language files.
- `embed-resource-pack`: copy a Java world and add or merge `resources.zip` in the copy. Existing copied `resources.zip` is preserved as the base pack unless `--replace-existing-resource-pack` is passed.
- `write-delivery`: require complete passing translation QA and existing-pack merge evidence, then write one canonical `exports/DELIVERY.md` plus JSON manifest naming the exact mode and primary artifact.

## Recommended Order

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect <world-or-zip>
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan <world-or-zip> --out <workdir> --target <target_locale> --source-locale en_us --map-slug <slug> --project-layout --max-workpack-units 120
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py summarize-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status <workdir>
python skills/mc-map-translate/scripts/mcmap_contract.py qa-translations <workdir> --out <workdir>/qa/interim_translation_qa.json --allow-incomplete
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo <workdir>
```

If `scan_report.json.identity_coupled.unresolved_unit_count` is nonzero, inspect the anchors already listed in generated `identity_review.json`, fill its decisions, and apply it to merged translations before final QA:

```json
{
  "namespace": "mcmap",
  "map_slug": "example",
  "groups": [
    {
      "name": "quest_key",
      "item_id": "minecraft:tripwire_hook",
      "unit_ids": ["producer-unit-id", "consumer-unit-id"],
      "review_reason": "Both anchors have the same model data and custom quest id."
    }
  ],
  "external_sources": [
    {
      "unit_ids": ["trade-input-unit-id"],
      "reason": "The identical item is materialized from the reviewed runtime storage macro."
    }
  ]
}
```

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py resolve-item-identities <workdir>/translations/translations.jsonl --decisions <workdir>/identity_review.json --out <workdir>/translations/translations.identity-resolved.jsonl --namespace mcmap --map-slug <slug>
```

Do not create a manual group from wording alone. Compare item ID, custom/model data, damage, enchantments, lore, all non-text components, and producer/consumer intent. An external-source decision is an audited exception, not a general QA bypass.

After the first scan, read `scan_review.md` and `scan_report.json`. Explain the four export modes before spending major translation effort:

Also read `selector_identity.json`. For every protected row, preserve the source text and segments with `intentional_name` plus a concrete selector reason. Review unmatched static and dynamic macro references as runtime-test items. Do not attempt to localize a protected NPC name unless the map logic is first migrated to stable tags and fully retested.

- `resource-pack-only`: standalone resource-pack zip; safest, no world edits, limited to text already reachable through resources/language keys.
- `embedded-pack-copy`: copied map/world with `resources.zip`; same text coverage as resource-pack-only, but players receive the pack with the save.
- `hybrid-keyed-copy`: copied map/world patched so supported hardcoded JSON text components become `translate` keys, with a matching resource pack. This is the default full/safest complete localization core when hardcoded JSON text exists.
- `direct-text-copy`: copied map/world with direct literal replacements for supported plain command/SNBT/datapack JSON/NBT strings. It may start from a hybrid-keyed copy when both source kinds exist. Treat as maximum-coverage/high-risk and ask for explicit confirmation.

If `full_localization_recommendation.suggest_full_translation_mode` is true, explain that "full translation" is not a fifth mode. Recommend the least invasive of the four modes that covers the scan: usually `hybrid-keyed-copy` when hardcoded JSON text exists, or `direct-text-copy` only when direct-only text remains and the user explicitly accepts the risk. Reports, audits, resource-pack zips, and copied-map zips are artifacts of the selected mode.

If `scan_report.json` lists `map_resource_packs`, tell the user the map already ships a resource pack. Embedded exports should merge generated translation files into that pack so original assets are preserved. A standalone `resource-pack-only` zip may be either a small overlay pack or a merged full pack; choose merged full pack when users should not manage two packs.

## Encoding Discipline

The bundled scripts read and write JSON, JSONL, TSV, language JSON, and reports with UTF-8. Keep that invariant when editing or generating files outside the scripts.

- Prefer the bundled import/export commands for TSV and JSONL instead of ad hoc shell redirection.
- When scripting translation edits, open files with `encoding="utf-8"` or `encoding="utf-8-sig"` for uncertain input and write UTF-8 output.
- On Windows, terminal output may misrender valid Unicode. Treat mojibake in the console as a display warning, not proof that the file is corrupt; verify with an explicit UTF-8 reader before changing data.
- Do not use a lossy console, clipboard, spreadsheet save, or shell pipeline as the only copy of translated non-ASCII text.
- Treat `U+FFFD` as a blocking data-loss signal. `validate-units`, table export/import, translation merge, resource-pack export, and copied-world apply commands reject rows containing it.
- Treat backslash escape shape as a blocking data-integrity surface. `validate-units`, resource-pack export, and copied-world apply commands reject obvious confusion between a real newline and literal `\\n`, and between a real tab and literal `\\t`.
- In JSONL, `\n` on disk can be JSON syntax for a real newline. In command/SNBT/JSON string content, `\\n` can be a literal escape intended for a later parser. Inspect decoded values when uncertain.
- After table round-trips, rerun `validate-units` and spot-check representative rows for target-language characters, accents, right-to-left text, emoji, section signs, and placeholders.
- Prefer JSONL editing over TSV for escape-heavy rows. If TSV is used, keep multiline fields quoted by the table tool and do not manually convert line breaks to visible `\\n` sequences.

Codex then translates staged batches:

- read `index/manifest.json`;
- read and maintain `translation_progress.md` as the persistent workpack TODO list;
- load one `workpacks/contextual/workpack_###.jsonl`;
- load only the `context_summaries` listed for that workpack;
- translate with Codex using the local context and glossary, not external machine-translation APIs;
- write translations to the matching `translations/parts/workpack_###.jsonl`;
- refresh `translation_progress.md` after each workpack;
- update `glossary.md` when terminology decisions are made.

After Codex fills one or more translation parts:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations <workdir>/translations/parts --base <workdir>/translation_units.jsonl --out <workdir>/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units <workdir>/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status <workdir>/translation_units.jsonl --translations <workdir>/translations/translations.jsonl --incomplete-only
python skills/mc-map-translate/scripts/mcmap_contract.py qa-translations <workdir> --out <workdir>/qa/translation_qa.json
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo <workdir>
```

If a translation part changes after `translations/translations.jsonl` exists, run `merge-translations` again before exporting from the project root.

For `resource-pack-only` export:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir> --out <workdir>/exports/resource-pack --pack-format <pack_format> --target <target_locale>
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack <workdir>/exports/resource-pack --out <workdir>/exports/resource-pack.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack <workdir>/exports/resource-pack --base-resource-pack <path-to-original-resources.zip> --out <workdir>/exports/merged-resource-pack.zip
```

For `hybrid-keyed-copy` preparation:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir> --out <workdir>/exports/hybrid-resource-pack --pack-format <pack_format> --target <target_locale> --include-hybrid-keys
```

Then patch a copied world or copied map zip. Include `--resource-pack` when you want the copied directory output to contain `resources.zip`. If the copied world already contains `resources.zip`, the command merges the generated pack into it:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir> --out <workdir>/exports/world-keyed --resource-pack <workdir>/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir> --out <workdir>/exports/world-keyed.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-text <workdir>/exports/world-keyed.zip --translations <workdir> --out <workdir>/exports/world-full-direct.zip --min-confidence low
python skills/mc-map-translate/scripts/mcmap_java_tools.py audit-english <workdir>/exports/world-full-direct.zip --out <workdir>/qa/residual_english_audit.json --target-locale <target_locale> --source-locale en_us
python skills/mc-map-translate/scripts/mcmap_java_tools.py write-delivery <workdir> --mode direct-text-copy --primary-output <workdir>/exports/world-full-direct.zip --resource-pack-output <workdir>/exports/merged-resource-pack.zip --translation-qa <workdir>/qa/translation_qa.json --residual-audit <workdir>/qa/residual_english_audit.json --apply-report <workdir>/exports/world-keyed.zip.mcmap_hybrid_apply_report.json --apply-report <workdir>/exports/world-full-direct.zip.mcmap_direct_text_apply_report.json
```

For `direct-text-copy` translated text that cannot be key-injected:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-text <world-or-zip> --translations <workdir> --out <workdir>/exports/world-direct-text.zip --min-confidence low
```

For `embedded-pack-copy`, ship a copied world with the pack embedded. Existing `resources.zip` in the copied world is merged by default:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py embed-resource-pack <world> --resource-pack <workdir>/exports/resource-pack --out <workdir>/exports/world-with-resources
```

For copied map zips with a top-level containing folder, `resources.zip` must be placed in the actual Java world root, the same directory as `level.dat`, not necessarily the archive root. The apply report records `resource_pack_embed_path` for `apply-hybrid-keys --resource-pack`.

Use `--replace-existing-resource-pack` only when intentionally discarding the copied map's original resource pack. This is rarely correct for real maps.

## Apply-Hybrid-Keys Behavior

`apply-hybrid-keys` never edits the source path. It copies directories with `copytree` or extracts zip packages to a temporary/copy directory, applies exact anchored patches, and writes `mcmap_hybrid_apply_report.json` or a sidecar report for zip output.

Supported automatic patches:

- `.mcfunction` JSON command spans from `function_line` and `command_span`.
- Quoted JSON text components inside commands/SNBT from exact `command_string_span` anchors.
- Datapack JSON text components from `json_path`.
- JSON text components stored as strings inside datapack JSON from `json_string_path`.
- Gzip/plain `.dat` NBT strings containing JSON text components or commands with JSON spans.
- Aggregated sign faces whose `segments[]` include per-line `nbt_path` and `component_json_path`.
- Standard gzip, zlib, or raw `.mca` chunk NBT strings with exact chunk and NBT path anchors.

Safety limits:

- Single-node hardcoded JSON text components use the unit `translation_key`.
- Multi-node hardcoded JSON text components use `segments[]` by default through `--multi-text-mode split-nodes`.
- Aggregated sign units must be translated as complete sign faces first, then split into per-line/per-node segment translations. Apply verifies every sign segment before writing the group.
- Every target `text` value must still exactly equal the recorded source segment; otherwise the unit is skipped.
- Existing `translate` conflicts, missing paths, unsafe paths, nested `resources.zip!` paths, and missing/invalid segment keys are skipped and reported.
- Plain NBT strings without JSON text component context are not hybrid-key-injection targets; they require explicit `embedded-direct` handling.
- Identity-coupled item text keeps scanner-generated canonical keys shared across equivalent visible text shapes. Do not replace them with occurrence keys.
- Selector-coupled entity names and NBT `CustomName` predicate literals have copied-world patch modes removed. Identity QA rejects changed translations or manually re-enabled Hybrid/Direct modes.
- A source map with `resources.zip` requires merged embedding by default. `--allow-separate-resource-pack` is an explicit distribution exception.

Use `--multi-text-mode skip` only when you want the old conservative behavior for audit or comparison.

## Apply-Direct-Text Behavior

`apply-direct-text` never edits the source path. It selects only Java units with `embedded-direct`, no decoded-component `json_path`, a supported direct anchor, and a filled `translation` unless `--allow-empty-translation` is passed.

Safety limits:

- Every target string must still exactly equal the unit `raw`; otherwise it is skipped.
- For `.mcfunction` rows, direct apply patches only `command_plain_span`, plain `command_string_span`, or the string at `command_json_path` inside the recorded command JSON span.
- For datapack JSON rows, direct apply patches only the exact `json_string_path` value.
- For NBT rows, direct apply patches exact `nbt_path` values, or exact command-contained spans inside that NBT string. `.mca` rows must include a chunk `local_index` anchor.
- Translations that would exceed the Java NBT string limit are skipped for NBT-backed anchors.
- JSON text components are skipped here; use `apply-hybrid-keys` for those.
- The command writes `mcmap_direct_text_apply_report.json` or a sidecar report for zip output. `apply-direct-nbt-strings` remains as a legacy alias.

## Coverage Limits

The bundled scanner parses gzip NBT `.dat` files and `.mca` chunks using standard gzip, zlib, and raw NBT compression. It reports unsupported compression, too-small region files, or parse failures under `pending_binary_parser_coverage` and `warnings`.

Use `--no-binary` when the map is huge, when a fast first pass is enough, or when binary parsing is producing too much technical noise.

The scanner reports but does not automatically localize visual text in PNG textures, custom bitmap fonts, map art, or model textures. Treat those hints as a QA checklist for full localization.

## Output Files

- `project.json`: target locale, source path, namespace, and mode.
- `translation_units.jsonl`: canonical units for translation.
- `scan_report.json`: machine-readable counts, warnings, top files, repeated text, and binary coverage.
- `scan_review.md`: human-readable triage summary.
- `residual_english_audit.json` and `.md`: optional QA output from `audit-english` after export/apply.
- `glossary.md`: seed glossary file for Codex to update before translation.
- `translation_progress.md`: persistent workpack TODO list; keep it updated throughout translation.
- `index/manifest.json`: entry point for staged translation.
- `index/unit_index.jsonl`, `index/source_index.jsonl`, `index/kind_index.jsonl`, `index/raw_repeats.jsonl`: compact searchable indexes.
- `context/source-summaries/*.md`: per-source summaries for local context loading.
- `workpacks/contextual/*.jsonl`: bounded context-preserving batches.
- `translations/parts/*.jsonl`: editable staged translation parts.
- `translations/translations.jsonl`: merged canonical translation file.
- `identity_review.json`: scanner-generated unresolved item rows plus empty reviewed-decision sections accepted by `resolve-item-identities`.
- `selector_identity.json`: every static/dynamic `@e[name=...]` or NBT `CustomName` selector reference, match summary, and unresolved review list. Stable `tag/type/scores/predicate` arguments are intentionally absent.
- `qa/identity_qa.json`: blocking item fingerprint, slot-key, unresolved identity, producer/consumer relationship, and selector-coupled entity-name report.
- `*.identity_resolution_report.json`: audit trail for reviewed manual groups and external-source decisions.
