# Minecraft Map Codex Translator

A local Codex plugin for professional Minecraft Java Edition map localization.

This plugin teaches Codex how to translate Minecraft Java maps like a translator-engineer: scan the whole map into an indexed project, translate with map context instead of isolated strings, preserve commands and JSON/NBT structure, keep a progress TODO, validate the result, and export safe resource-pack-first outputs.

It is not a wrapper around machine-translation APIs. The translation work is done by Codex using local scan results, source summaries, glossary decisions, and QA reports.

## What It Is For

Use this plugin when you want to localize a Minecraft Java Edition adventure map, puzzle map, minigame, datapack-heavy map, or map-specific resource pack.

It can help with:

- Java world folders and Java map zip packages;
- map-owned `resources.zip` and standalone resource packs;
- `assets/<namespace>/lang/*.json` language files;
- JSON text components in commands, datapacks, NBT, books, signs, titles, bossbars, and scoreboards;
- `.mcfunction` files, including common `execute ... run ...` command chains;
- datapack JSON, storage-like JSON/SNBT, item names, lore, books, and sign text;
- supported `.dat` NBT and `.mca` region/chunk text;
- residual-English QA after export.

Bedrock Edition is not supported.

## Safety Model

Original maps are never edited in place.

The default approach is resource-pack-first:

1. scan the map;
2. build an indexed translation project;
3. translate in contextual workpacks;
4. export a resource pack;
5. only patch copied map outputs when hardcoded text must be made localizable.

The scripts use exact anchors and reports. They do not globally replace strings in binary world data.

## Installation

This repository is a Codex plugin package. The required plugin manifest is at `.codex-plugin/plugin.json`, and the bundled skill lives under `skills/mc-map-translate/`.

For local testing, use Codex's plugin creator / plugin tooling to wire this folder into a personal marketplace and install it. The important package shape is this repository root plus `.codex-plugin/plugin.json` and `skills/`.

Typical local workflow:

```bash
git clone https://github.com/G-TTYg/Minecraft-map-codex-translator.git
cd Minecraft-map-codex-translator
python path/to/plugin-creator/scripts/validate_plugin.py .
```

Then in Codex, install it from your local/personal marketplace. In this repo's development setup, the command is:

```bash
codex plugin add minecraft-map-codex-translator@personal
```

If your marketplace is named differently, use that marketplace name instead of `personal`.

After installing or updating the plugin, start a new Codex task so the refreshed skill instructions are loaded.

## How To Use In Codex

Start a task with a Java map folder or zip and a target language/locale.

Example prompts:

```text
Use $mc-map-translate to localize this Java map to zh_cn:
D:/Downloads/My Map.zip
```

```text
Use $mc-map-translate to scan this map first, explain the export modes, then translate it to ja_jp.
```

```text
Use $mc-map-translate to QA this translated workdir and build the safest full localization export.
```

If you provide a language name instead of a locale code, Codex should choose a standard Java locale code such as `zh_cn`, `ja_jp`, `ko_kr`, `fr_fr`, or `es_es` and record the assumption.

## Standard Workflow

Codex should follow this flow for real maps:

1. Inspect the package to confirm it is Java Edition.
2. Scan the map into `translation_units.jsonl`.
3. Create the indexed multi-file project layout.
4. Read `scan_review.md` and `scan_report.json`.
5. Explain export modes and ask what output bundle to produce.
6. Build or update `glossary.md`.
7. Maintain `translation_progress.md` as the persistent TODO list.
8. Translate one contextual workpack at a time.
9. Merge staged translations.
10. Validate units, encoding, escapes, placeholders, and Minecraft structures.
11. Export the selected output bundle.
12. Run apply reports and residual-English QA where relevant.

Codex should not load the entire map into the model context. The scanner creates a searchable/indexed project so Codex can load only the relevant workpack, source summaries, and nearby context for each translation batch.

## Export Modes

The plugin distinguishes internal unit support from user-facing output bundles. These are the modes Codex should explain after the first scan.

| Mode | Changes world data? | Main output | Best for | Limits |
| --- | --- | --- | --- | --- |
| `resource-pack-only` | No | Standalone resource-pack zip | Existing language keys and resource-pack-localizable text | Cannot cover hardcoded command/sign/book/entity/NBT text |
| `embedded-pack-copy` | Copy only | Copied map/world with `resources.zip` beside `level.dat` | Shipping a map where players should not install a separate pack manually | Same text coverage as `resource-pack-only` |
| `hybrid-keyed-copy` | Copied map patched | Copied map/world plus matching resource pack or embedded `resources.zip` | Hardcoded JSON text components that can become `{"translate":"<key>"}` | Does not cover direct-only plain strings or image/font text |
| `direct-text-copy` | Copied map patched | Copied map/world with direct literal replacements | Plain command/SNBT/datapack JSON/NBT strings that cannot be key-injected | Higher risk; requires explicit user confirmation |

## What "Full Translation" Means

"Full translation" is not one script flag.

The default full/safest complete localization bundle is:

- standalone resource-pack zip;
- copied map/world with `resources.zip`;
- `hybrid-keyed-copy` when hardcoded JSON text components exist;
- QA/apply reports;
- residual-English audit;
- visual asset findings for PNG/font/model text.

For maximum coverage, Codex can additionally produce `direct-text-copy`, but only after the user explicitly accepts the risk of direct literal replacement in copied world data.

Resource-pack-only output is truly complete only when all player-facing text is already reachable through language/resource keys and no hardcoded, direct-only, or visual-image text remains.

## Common Commands

Inspect and scan:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect path/to/world-or-map.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world-or-map.zip --out work/map --target zh_cn --source-locale en_us --map-slug map --project-layout --max-workpack-units 120
```

Validate and manage progress:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/map/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status work/map
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo work/map
```

Merge translated workpacks:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations work/map/translations/parts --base work/map/translation_units.jsonl --out work/map/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/map/translations/translations.jsonl
```

Build a standalone resource pack:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/map --out work/map/exports/resource-pack --pack-format 34 --target zh_cn
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack work/map/exports/resource-pack --out work/map/exports/map-zh_cn-resourcepack.zip
```

Build a hybrid-keyed copied map:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/map --out work/map/exports/hybrid-resource-pack --pack-format 34 --target zh_cn --include-hybrid-keys
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world-or-map.zip --translations work/map --out work/map/exports/world-keyed.zip --resource-pack work/map/exports/hybrid-resource-pack
```

Apply direct text only when explicitly approved:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-text work/map/exports/world-keyed.zip --translations work/map --out work/map/exports/world-full-direct.zip --min-confidence low
```

Audit exported output:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py audit-english work/map/exports/world-keyed.zip --out work/map/qa/residual_english_audit.json
```

## Project Layout

A normal scan creates:

- `translation_units.jsonl`: canonical text units and anchors;
- `scan_report.json`: machine-readable counts, warnings, mode coverage, and full localization recommendation;
- `scan_review.md`: human-readable scan triage;
- `glossary.md`: terminology decisions;
- `translation_progress.md`: persistent TODO list;
- `index/manifest.json`: entry point for staged translation;
- `index/*.jsonl`: compact searchable indexes;
- `context/source-summaries/*.md`: source-level summaries;
- `workpacks/contextual/*.jsonl`: bounded context-preserving translation batches;
- `translations/parts/*.jsonl`: staged AI translation outputs;
- `translations/translations.jsonl`: merged canonical translation file;
- `exports/`: generated resource packs and copied map outputs;
- `qa/`: residual-English audits and QA reports.

## Translation Quality Rules

Codex should:

- translate with map context, not isolated strings;
- preserve command syntax, selectors, score names, NBT paths, JSON keys, placeholders, colors, click events, hover events, fonts, keybinds, and formatting;
- keep terminology consistent through `glossary.md`;
- translate grouped signs/components as complete messages before filling segment-level translations;
- preserve escape semantics such as real newlines versus literal `\\n`;
- treat UTF-8 and multilingual text carefully, especially on Windows terminals;
- report uncovered text honestly instead of claiming false coverage.

Codex should not call external translation APIs, browser translators, or third-party localization services unless the user explicitly asks for that exception.

## Known Limits

- Java Edition only.
- Resource packs cannot translate arbitrary hardcoded literals unless the copied map is patched to use translation keys.
- PNG textures, custom bitmap fonts, map art, and model textures may contain visual English that requires separate asset localization or manual QA.
- Direct text replacement is intentionally separate from hybrid key injection because it has a higher risk profile.
- Parser coverage for unusual binary/NBT forms may be reported as pending rather than guessed.

## Development Validation

Run these checks before publishing plugin changes:

```bash
python -m py_compile skills/mc-map-translate/scripts/mcmap_java_tools.py skills/mc-map-translate/scripts/mcmap_contract.py
python path/to/skill-creator/scripts/quick_validate.py skills/mc-map-translate
python path/to/plugin-creator/scripts/validate_plugin.py .
```

For apply tooling changes, also smoke-test `scan` and `apply-hybrid-keys` against a small fixture or a real copied Java map.

## License

MIT.
