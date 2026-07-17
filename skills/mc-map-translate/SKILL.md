---
name: mc-map-translate
description: Professional Minecraft Java Edition map localization workflow for Codex. Use when translating, localizing, scanning, QAing, exporting, or patching Java maps; when working with Java resource packs, language JSON files, text components, command blocks, signs, books, bossbars, scoreboards, functions, datapacks, NBT/SNBT/JSON text, .mca/.dat world data, Java map .zip packages, resources.zip, or map-specific resource packs; and when the user wants resource-pack-first non-invasive translation to a user-specified target locale with optional copied-map patching.
---

# MC Map Translate

## Overview

Localize Minecraft Java Edition maps as a professional translator-engineer: preserve gameplay semantics, infer story context before translating, keep terminology consistent, and export a resource pack first whenever possible.

The target language is not fixed. Always use the locale or language requested by the user, such as `zh_cn`, `ja_jp`, `ko_kr`, `fr_fr`, or `es_es`. If the user gives a language name instead of a locale code, choose a standard Java locale code and record the assumption.

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
   - Before using bundled CLI tools, read `references/tooling.md`.
   - Before translation, read `references/translation-style.md`.
   - Before apply/export, read `references/qa-rules.md`.

## Standard Workflow

1. Create a work folder.
   - Run `python skills/mc-map-translate/scripts/mcmap_contract.py init-workspace <world-or-package> --out <workdir> --target <target_locale>`.
   - Keep original map files read-only unless the user explicitly asks to patch them. Patch copies only.

2. Scan and classify text.
   - Produce `translation_units.jsonl` following `references/text-unit-contract.md`.
   - Record exact anchors, not only raw strings or hashes.
   - Never treat regex over `.mca` bytes as authoritative; use a parser or mark results as low confidence.
   - Preserve command syntax, selectors, score names, NBT paths, JSON text component structure, colors, click events, hover events, newlines, and placeholders.

3. Build context before translating.
   - Cluster text by source file, coordinates, command-chain order, function call chain, book page order, dialogue speaker, quest, and repeated terminology.
   - Create or update `glossary.md` before translating substantial text.
   - Translate in batches small enough to keep local context visible.

4. Translate like a localization editor.
   - Prefer natural player-facing target-language phrasing over literal source-language phrasing.
   - Keep gameplay instructions unambiguous.
   - Keep role names, place names, item names, puzzle terms, factions, and UI verbs consistent.
   - Preserve all protected tokens exactly unless a reference says they are safe to translate.
   - For puzzles, riddles, rhymes, lore, and jokes, preserve player experience over word-for-word meaning.

5. Export safely.
   - In `resource-pack` mode, generate only language/resource files and a QA report.
   - In `hybrid-key-injection` mode, first build a resource pack with `--include-hybrid-keys`, then run `apply-hybrid-keys` to patch a copied map so supported hardcoded JSON text components use generated `translate` keys.
   - In `embedded-direct` mode, create a full backup, patch a copy, validate every changed file, and report each anchor changed.

6. QA before final delivery.
   - Run schema validation on JSONL units and translation files.
   - Validate JSON text components and language JSON.
   - Check placeholders, selectors, color codes, newline counts, key coverage, untranslated residues, duplicate key conflicts, and command breakage risk.
   - Produce a short report with coverage by source kind and export mode.

## Script Helpers

Use `scripts/mcmap_contract.py` for deterministic project scaffolding, JSONL validation, and resource-pack language export:

```bash
python skills/mc-map-translate/scripts/mcmap_contract.py init-workspace path/to/world --out work/mymap --target ja_jp
python skills/mc-map-translate/scripts/mcmap_contract.py validate-units work/mymap/translation_units.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-workpacks work/mymap/translation_units.jsonl --out-dir work/mymap/workpacks --max-units 200
python skills/mc-map-translate/scripts/mcmap_contract.py export-table work/mymap/translation_units.jsonl --out work/mymap/translations.tsv
python skills/mc-map-translate/scripts/mcmap_contract.py import-table work/mymap/translations.tsv --base work/mymap/translation_units.jsonl --out work/mymap/translations.jsonl
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/mymap/translations.jsonl --out work/mymap/export/resource-pack --pack-format 34 --namespace mcmap --target ja_jp
python skills/mc-map-translate/scripts/mcmap_contract.py make-resource-pack work/mymap/translations.jsonl --out work/mymap/export/hybrid-resource-pack --pack-format 34 --namespace mcmap --target ja_jp --include-hybrid-keys
```

Use `scripts/mcmap_java_tools.py` for Java-specific inspection, scanning, packaging, and copied-world resource embedding:

```bash
python skills/mc-map-translate/scripts/mcmap_java_tools.py inspect path/to/world
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world --out work/mymap --target ja_jp --map-slug mymap
python skills/mc-map-translate/scripts/mcmap_java_tools.py scan path/to/world --out work/mymap --target ja_jp --no-binary
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world --translations work/mymap/translations.jsonl --out work/mymap/exports/world-keyed --resource-pack work/mymap/exports/hybrid-resource-pack
python skills/mc-map-translate/scripts/mcmap_java_tools.py apply-hybrid-keys path/to/world.zip --translations work/mymap/translations.jsonl --out work/mymap/exports/world-keyed.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py zip-resource-pack work/mymap/exports/resource-pack --out work/mymap/exports/mymap-ja_jp-resourcepack.zip
python skills/mc-map-translate/scripts/mcmap_java_tools.py embed-resource-pack path/to/world --resource-pack work/mymap/exports/resource-pack --out work/mymap/exports/world-with-resources
```

Use scanner output files directly: `translation_units.jsonl`, `scan_report.json`, `scan_review.md`, `glossary.md`, and generated `workpacks/*.jsonl`. If a parser is not yet available for a source kind, report missing parser coverage instead of pretending low-confidence extraction is reliable.

`apply-hybrid-keys` is intentionally conservative. It copies/extracts the world, patches the copy, and writes `mcmap_hybrid_apply_report.json`. It injects keys only when a unit has an exact JSON text component anchor and exactly one hardcoded `text` node, preserving the rest of that component node such as color, click/hover events, selectors, keybinds, and `extra`. It skips multi-`text` components instead of flattening or discarding style.

## Hard Rules

- Do not edit the original map in place.
- Do not globally replace raw strings in binary world data.
- Do not translate command keywords, selectors, scoreboard objectives, storage paths, entity IDs, item IDs, block IDs, NBT keys, or JSON text component field names.
- Do not remove formatting, click events, hover events, insertion text, fonts, or keybind references.
- Do not claim resource-pack-only coverage for hardcoded text unless the map already uses translation keys or the output mode includes key injection.
- Ask for explicit confirmation before `embedded-direct` output.

## Current Scope

Java Edition only. Do not present Bedrock support as available in this plugin.
