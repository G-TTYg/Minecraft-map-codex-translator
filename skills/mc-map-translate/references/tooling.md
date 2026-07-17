# Tooling

Use this reference before running bundled scripts.

## Scope

The plugin includes its own standard tools. Do not depend on MCC-i18n or copy its code into this plugin. The useful standardized pattern is: inspect, scan, create a workpack, translate with context, validate, export a resource pack, and optionally embed that resource pack into a copied Java world.

## Tools

`scripts/mcmap_contract.py`:

- `init-workspace`: create the standard project folder and `project.json`.
- `validate-units`: validate `translation_units.jsonl` or `translations.jsonl`.
- `summarize-units`: summarize translation coverage.
- `make-workpacks`: split units into stable JSONL translation batches.
- `export-table`: export selected units to UTF-8 TSV.
- `import-table`: merge TSV translations back into JSONL by `id`.
- `make-resource-pack`: create `pack.mcmeta` and target language JSON files from translations. By default it exports only units that can work in resource-pack mode; pass `--include-hybrid-keys` only when a copied-map key-injection apply step will use those generated keys.

`scripts/mcmap_java_tools.py`:

- `inspect`: detect Java map/package markers and Bedrock-only markers.
- `scan`: scan Java resource-pack language JSON, datapack JSON text components, `.mcfunction` JSON text components, supported `.dat` NBT, and supported `.mca` region chunks into `translation_units.jsonl`.
- `apply-hybrid-keys`: copy/extract a Java world or map zip and inject generated `translate` keys into supported hardcoded JSON text components in the copy.
- `zip-resource-pack`: zip a resource pack directory with the correct root.
- `embed-resource-pack`: copy a Java world and add `resources.zip` to the copy.

## Recommended Order

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect <world-or-zip>
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan <world-or-zip> --out <workdir> --target <target_locale> --source-locale en_us --map-slug <slug>
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py summarize-units <workdir>/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-workpacks <workdir>/translation_units.jsonl --out-dir <workdir>/workpacks --max-units 200 --dedupe-raw
python skills/mc-map-translate/scripts/mcmap_contract.py export-table <workdir>/translation_units.jsonl --out <workdir>/translations.tsv
```

After Codex fills translations:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir>/translations.jsonl --out <workdir>/exports/resource-pack --pack-format <pack_format> --target <target_locale>
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack <workdir>/exports/resource-pack --out <workdir>/exports/resource-pack.zip
```

For hybrid key-injection preparation:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack <workdir>/translations.jsonl --out <workdir>/exports/hybrid-resource-pack --pack-format <pack_format> --target <target_locale> --include-hybrid-keys
```

Then patch a copied world or copied map zip:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir>/translations.jsonl --out <workdir>/exports/world-keyed --resource-pack <workdir>/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys <world-or-zip> --translations <workdir>/translations.jsonl --out <workdir>/exports/world-keyed.zip
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

- Only hardcoded JSON text component units with exactly one `context.text_nodes[]` entry are injected automatically.
- The target `text` value must still exactly equal `raw`; otherwise the unit is skipped.
- Existing `translate` conflicts, missing paths, unsafe paths, nested `resources.zip!` paths, and multi-`text` components are skipped and reported.
- Plain NBT strings without JSON text component context are not hybrid-key-injection targets; they require explicit `embedded-direct` handling.

## Coverage Limits

The bundled scanner parses gzip NBT `.dat` files and `.mca` chunks using standard gzip, zlib, and raw NBT compression. It reports unsupported compression, too-small region files, or parse failures under `pending_binary_parser_coverage` and `warnings`.

Use `--no-binary` when the map is huge, when a fast first pass is enough, or when binary parsing is producing too much technical noise.

## Output Files

- `project.json`: target locale, source path, namespace, and mode.
- `translation_units.jsonl`: canonical units for translation.
- `scan_report.json`: machine-readable counts, warnings, top files, repeated text, and binary coverage.
- `scan_review.md`: human-readable triage summary.
- `glossary.md`: seed glossary file for Codex to update before translation.
- `workpacks/*.jsonl`: batch files produced by `make-workpacks`.
