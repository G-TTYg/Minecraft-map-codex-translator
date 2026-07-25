# Text Unit Contract

Use JSON Lines (`.jsonl`). Each non-empty line is one text unit.

## Required Fields

```json
{
  "id": "stable-id",
  "edition": "java",
  "source_kind": "command_block",
  "source_file": "region/r.0.0.mca",
  "address": {},
  "raw": "Find the ancient key",
  "translation": "",
  "translation_key": "mcmap.example.command_block.stable-id",
  "resource_namespace": "mcmap",
  "source_locale": "en_us",
  "mode_support": ["hybrid-key-injection", "embedded-direct"],
  "protected": [],
  "segments": [],
  "context": {},
  "confidence": "high",
  "notes": ""
}
```

## Field Meanings

- `id`: Stable within the project. Prefer deterministic IDs from anchor plus raw text, not array index.
- `edition`: Always `java` in this plugin.
- `source_kind`: One of `lang`, `function`, `datapack_json`, `text_component_translate`, `command_block`, `sign`, `book`, `bossbar`, `scoreboard`, `entity_name`, `item_name`, `item_lore`, `advancement`, `title`, `tellraw`, `unknown`.
- `source_file`: Path relative to the map/package root when possible.
- `address`: Structured anchor. Include `block_pos`, `entity_uuid`, `chunk`, `nbt_path`, `json_path`, `function_line`, `command_span`, or `lang_key` as applicable.
- `raw`: Exact source string or exact player-facing segment.
- `translation`: Target language translation.
- `translation_key`: Language key used or proposed for resource-pack export.
- `resource_namespace`: Java resource-pack namespace to write under `assets/<resource_namespace>/lang/<target_locale>.json`.
- `source_locale`: Source locale for language JSON units, normally `en_us` unless the user specifies another source.
- `mode_support`: Which export modes can handle this unit: `resource-pack`, `hybrid-key-injection`, `embedded-direct`.
- `protected`: Tokens that must survive unchanged.
- `segments`: Optional per-`text` translation slots for grouped components with multiple hardcoded text nodes.
- `context`: Speaker, quest, nearby text, page order, command-chain group, or other context used for translation.
- `confidence`: `high`, `medium`, or `low`.
- `notes`: Human/agent notes, ambiguities, or QA concerns.

## Context For Grouped Components

For grouped JSON text components, `context` may include:

```json
{
  "text_nodes": [
    {"json_path": "$[0].text", "text": "Open "},
    {"json_path": "$[1].text", "text": "the door"}
  ],
  "translate_keys": [
    {"json_path": "$.translate", "key": "menu.start"}
  ],
  "selector_tokens": ["@p"],
  "keybinds": ["key.jump"]
}
```

Translate `raw` as the complete player-facing message. Use `text_nodes` to understand how the message is split across styled fragments.

For grouped components with more than one hardcoded `text` node, scanner output or `prepare-segments` may add:

```json
{
  "segments": [
    {
      "index": 0,
      "json_path": "$[0].text",
      "raw": "Open ",
      "translation": "",
      "translation_key": "mcmap.example.tellraw.abc123.part_0"
    },
    {
      "index": 1,
      "json_path": "$[1].text",
      "raw": "the door",
      "translation": "",
      "translation_key": "mcmap.example.tellraw.abc123.part_1"
    }
  ]
}
```

Codex should translate `raw` first as the whole message, then fill each `segments[].translation` according to that full-message translation and the surrounding component context. Do not translate segment text as isolated words when it is part of a sentence.

For `hybrid-key-injection`, `apply-hybrid-keys --multi-text-mode split-nodes` replaces each segment's original `text` node with its `translation_key`, preserving surrounding styles, selectors, scores, click/hover events, keybinds, and `extra`.

## Binary Anchors

For `.dat` and `.mca` sources, `address` may include:

- `nbt_path`: Path to the string inside the parsed NBT tree.
- `chunk`: Region/chunk metadata, including `region_x`, `region_z`, `chunk_x`, `chunk_z`, and `local_index`.

These anchors are sufficient for QA and copied-world apply tooling.

For JSON text component strings in `.dat` or `.mca`, `apply-hybrid-keys` can use `nbt_path`, optional `chunk`, `json_path`, and optional `command_span` to patch the copied NBT data. Plain NBT strings without `json_path` are not hybrid key-injection targets; `apply-direct-nbt-strings` can replace them directly in a copied world when the current NBT string still exactly equals `raw` and `translation` is filled.

## Translation JSONL

Final translation files may contain the same schema with `translation` filled. For resource-pack generation, entries without `translation_key` are skipped unless a key-generation mode is explicitly used.

## Validation Rules

- IDs must be unique.
- `raw` must be non-empty.
- `mode_support` must be non-empty.
- `resource-pack` units must include `translation_key`.
- `resource_namespace` should be present for every unit that can be exported to a resource pack.
- `translation` must preserve every protected token exactly.
- `segments[].json_path` and `segments[].raw` must match `context.text_nodes`.
- `segments[].translation_key` must use only lowercase `a-z`, digits, `_`, `.`, and `-`.
- `edition` must be `java`.
- JSON strings must remain valid UTF-8.
