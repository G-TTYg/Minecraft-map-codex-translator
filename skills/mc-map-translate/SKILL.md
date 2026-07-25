---
name: mc-map-translate
description: Professional Minecraft Java Edition map localization workflow for Codex. Use when translating, localizing, scanning, QAing, exporting, or patching Java maps; when working with Java resource packs, language JSON files, text components, command blocks, signs, books, bossbars, scoreboards, functions, datapacks, NBT/SNBT/JSON text, .mca/.dat world data, Java map .zip packages, resources.zip, or map-specific resource packs; and when the user wants resource-pack-first non-invasive translation to a user-specified target locale with optional copied-map patching.
---

# MC Map Translate

## Overview

Localize Minecraft Java Edition maps as a professional translator-engineer: preserve gameplay semantics, infer story context before translating, keep terminology consistent, and export a resource pack first whenever possible.

This skill is not a wrapper around machine-translation services. The translator is Codex itself, using the scanned project index, source summaries, surrounding map context, glossary, progress TODO, and QA feedback to produce careful human-quality localization. Do not send map text to external translation APIs or browser-based translators unless the user explicitly asks for that exception.

The target language is not fixed. Always use the locale or language requested by the user, such as `zh_cn`, `ja_jp`, `ko_kr`, `fr_fr`, or `es_es`. If the user gives a language name instead of a locale code, choose a standard Java locale code and record the assumption.

Treat every translation artifact as Unicode data. Read and write JSON, JSONL, TSV, and language files as UTF-8; do not trust terminal display for non-ASCII text, especially on Windows. If text looks like mojibake, replacement characters, or question marks, verify the file bytes or JSON values before applying/exporting.

The default output is non-invasive `resource-pack` mode. Use `hybrid-key-injection` only when hardcoded text must be converted to translation keys in a copied world. Use `embedded-direct` only when the user explicitly rejects a resource pack and accepts the higher risk of editing world data directly.

## Decision Tree

1. Confirm Java Edition package shape.
   - Java: `level.dat`, `region/*.mca`, `entities/*.mca`, `data/*.dat`, `datapacks/`, `resources.zip`, `assets/*/lang/*.json`.
   - If Bedrock-only markers appear, stop and report that this skill currently supports Java only.
   - If unknown, run `mcmap_java_tools.py inspect` first and report the confidence level.

2. Choose export mode.
   - Prefer `resource-pack`: translate existing language keys and map-owned resource-pack language files without world edits.
   - Use `hybrid-key-injection`: create translation keys and patch a copied map so hardcoded text components reference those keys; ship `resources.zip` or a standalone resource pack.
   - Use `embedded-direct`: replace literal text in a copied world only after explicit user confirmation.

3. Load the minimum required references.
   - For Java export modes, read `references/java-resource-pack-first.md`.
   - For scanning Java world text, read `references/java-text-sources.md`.
   - For source-kind semantics and Minecraft Java text systems, read `references/java-edition-text-map.md`.
   - Before creating or validating JSONL workpacks, read `references/text-unit-contract.md`.
   - Before staged translation of a real map, read `references/project-layout.md`.
   - Before using bundled CLI tools, read `references/tooling.md`.
   - Before translation, read `references/translation-style.md`.
   - Before apply/export, read `references/qa-rules.md`.

## Standard Workflow

1. Create a work folder.
   - Run `python skills/mc-map-translate/scripts/mcmap_contract.py init-workspace <world-or-package> --out <workdir> --target <target_locale>`.
   - Keep original map files read-only unless the user explicitly asks to patch them. Patch copies only.

2. Scan and classify text.
   - Produce `translation_units.jsonl` following `references/text-unit-contract.md`.
   - For normal real-map work, also produce the indexed multi-file project layout. Use `scan --project-layout` or run `make-project-files` after scanning.
   - Record exact anchors, not only raw strings or hashes.
   - Never treat regex over `.mca` bytes as authoritative; use a parser or mark results as low confidence.
   - Preserve command syntax, selectors, score names, NBT paths, JSON text component structure, colors, click events, hover events, newlines, and placeholders.

3. Build context before translating.
   - Do not load the whole map into model context. Use `index/manifest.json`, `index/unit_index.jsonl`, `index/source_index.jsonl`, source summaries, and one workpack at a time.
   - Cluster text by source file, coordinates, command-chain order, function call chain, book page order, dialogue speaker, quest, and repeated terminology.
   - Create or update `glossary.md` before translating substantial text.
   - Maintain translation progress as a TODO list. Use the Codex task checklist for the active session when available, and keep the durable project TODO at `translation_progress.md` updated with `write-progress-todo`.
   - Translate in batches small enough to keep local context visible. Write staged translations to `translations/parts/workpack_###.jsonl` as UTF-8, then merge by stable `id`.

4. Translate like a localization editor.
   - Translate with Codex reasoning over map context; do not call external translation APIs or paste batches into web translators.
   - Prefer natural player-facing target-language phrasing over literal source-language phrasing.
   - Keep gameplay instructions unambiguous.
   - Keep role names, place names, item names, puzzle terms, factions, and UI verbs consistent.
   - Strive for the most complete safe localization possible: translate every player-facing unit that can be safely handled by the selected export modes, and record any remaining uncovered or risky text explicitly.
   - Preserve all protected tokens exactly unless a reference says they are safe to translate.
   - For puzzles, riddles, rhymes, lore, and jokes, preserve player experience over word-for-word meaning.
   - For units with `segments[]`, translate `raw` as the complete message first, then fill each `segments[].translation` so the preserved Minecraft component order still reads naturally.

5. Export safely.
   - In `resource-pack` mode, generate only language/resource files and a QA report.
   - In `hybrid-key-injection` mode, first build a resource pack with `--include-hybrid-keys`, then run `apply-hybrid-keys` to patch a copied map so supported hardcoded JSON text components use generated `translate` keys.
   - In `embedded-direct` mode, create a full backup or copied output, patch only exact anchors in the copy, validate every changed file, and report each anchor changed. Use `apply-direct-nbt-strings` for translated plain NBT strings that are not JSON text components.

6. QA before final delivery.
   - Run schema validation on JSONL units and translation files.
   - Validate JSON text components and language JSON.
   - Check placeholders, selectors, color codes, newline counts, key coverage, untranslated residues, duplicate key conflicts, multilingual encoding integrity, and command breakage risk.
   - Produce a short report with coverage by source kind and export mode.

## Script Helpers

Use `scripts/mcmap_contract.py` for deterministic project scaffolding, JSONL validation, and resource-pack language export:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py init-workspace path/to/world --out work/mymap --target ja_jp
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/mymap/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-project-files work/mymap/translation_units.jsonl --out-dir work/mymap --max-units 120
python skills/mc-map-translate/scripts/mcmap_contract.py translation-status work/mymap
python skills/mc-map-translate/scripts/mcmap_contract.py write-progress-todo work/mymap
python skills/mc-map-translate/scripts/mcmap_contract.py make-workpacks work/mymap/translation_units.jsonl --out-dir work/mymap/workpacks --max-units 200
python skills/mc-map-translate/scripts/mcmap_contract.py prepare-segments work/mymap/translation_units.jsonl --out work/mymap/translations.segmented.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py export-table work/mymap/translation_units.jsonl --out work/mymap/translations.tsv
python skills/mc-map-translate/scripts/mcmap_contract.py import-table work/mymap/translations.tsv --base work/mymap/translation_units.jsonl --out work/mymap/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py export-segment-table work/mymap/translations.segmented.jsonl --out work/mymap/segments.tsv
python skills/mc-map-translate/scripts/mcmap_contract.py import-segment-table work/mymap/segments.tsv --base work/mymap/translations.segmented.jsonl --out work/mymap/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py merge-translations work/mymap/translations/parts --base work/mymap/translation_units.jsonl --out work/mymap/translations/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/mymap/translations/translations.jsonl --out work/mymap/export/resource-pack --pack-format 34 --namespace mcmap --target ja_jp
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/mymap --out work/mymap/export/hybrid-resource-pack --pack-format 34 --namespace mcmap --target ja_jp --include-hybrid-keys
```

Use `scripts/mcmap_java_tools.py` for Java-specific inspection, scanning, packaging, and copied-world resource embedding:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect path/to/world
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world --out work/mymap --target ja_jp --map-slug mymap
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world --out work/mymap --target ja_jp --map-slug mymap --project-layout --max-workpack-units 120
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world --out work/mymap --target ja_jp --no-binary
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world --translations work/mymap/translations/translations.jsonl --out work/mymap/exports/world-keyed --resource-pack work/mymap/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world --translations work/mymap --out work/mymap/exports/world-keyed --resource-pack work/mymap/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world.zip --translations work/mymap/translations/translations.jsonl --out work/mymap/exports/world-keyed.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-direct-nbt-strings path/to/world.zip --translations work/mymap --out work/mymap/exports/world-direct-nbt.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack work/mymap/exports/resource-pack --out work/mymap/exports/mymap-ja_jp-resourcepack.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py embed-resource-pack path/to/world --resource-pack work/mymap/exports/resource-pack --out work/mymap/exports/world-with-resources
```

Use scanner output files directly: `translation_units.jsonl`, `scan_report.json`, `scan_review.md`, `glossary.md`, `translation_progress.md`, and the indexed project layout under `index/`, `context/`, `workpacks/contextual/`, and `translations/parts/`. If a parser is not yet available for a source kind, report missing parser coverage instead of pretending low-confidence extraction is reliable.

For staged AI translation, treat `index/manifest.json` as the entry point. Load one workpack and its listed source summaries at a time, update the matching translation part, refresh `translation_progress.md`, and use `merge-translations` before final validation/export. Export and apply commands can accept a merged JSONL file, a translation-parts directory, or the project root.

`apply-hybrid-keys` is intentionally conservative. It copies/extracts the world, patches the copy, and writes `mcmap_hybrid_apply_report.json`. For single-node text components it injects the unit `translation_key`. For multi-node grouped components it uses `segments[]` and the default `--multi-text-mode split-nodes` to replace each hardcoded `text` node with its segment `translation_key`, preserving sibling selectors, scores, colors, events, keybinds, and `extra`.

`apply-direct-nbt-strings` is separate from hybrid key injection. It handles plain NBT strings with `mode_support=["embedded-direct"]`, `address.nbt_path`, no `json_path`, and a filled `translation`. It copies/extracts the world, replaces only exact matching NBT string values in `.dat` and `.mca` files, and writes `mcmap_direct_nbt_apply_report.json`.

## Hard Rules

- Do not edit the original map in place.
- Do not globally replace raw strings in binary world data.
- Do not call external translation APIs, browser translators, or third-party localization services by default. Use Codex plus local project context unless the user explicitly asks otherwise.
- Do not translate command keywords, selectors, scoreboard objectives, storage paths, entity IDs, item IDs, block IDs, NBT keys, or JSON text component field names.
- Do not remove formatting, click events, hover events, insertion text, fonts, or keybind references.
- Do not claim resource-pack-only coverage for hardcoded text unless the map already uses translation keys or the output mode includes key injection.
- Do not translate a real map without maintaining `translation_progress.md` or an equivalent user-approved persistent progress TODO.
- Do not apply or export translations that show mojibake, replacement characters, or `?` in place of target-language characters; re-read and repair the UTF-8 source artifact first.
- Ask for explicit confirmation before `embedded-direct` output.

## Current Scope

Java Edition only. Do not present Bedrock support as available in this plugin.
