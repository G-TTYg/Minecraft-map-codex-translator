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
  "review_status": "",
  "review_reason": "",
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
- `source_kind`: Common values include `lang`, `function`, `datapack_json`, `storage_text`, `say`, `text_component_translate`, `command_block`, `sign`, `book`, `bossbar`, `scoreboard`, `entity_name`, `item_name`, `item_lore`, `advancement`, `title`, `actionbar`, `tellraw`, `text_display`, `nbt_text`, and `unknown`.
- `source_file`: Path relative to the map/package root when possible.
- `address`: Structured anchor. Include `block_pos`, `entity_uuid`, `chunk`, `nbt_path`, `json_path`, `json_string_path`, `function_id`, `function_line`, `function_macro`, `command_span`, `command_json_path`, `command_string_span`, `command_plain_span`, `sign_lines`, or `lang_key` as applicable.
- `raw`: Exact source string or exact player-facing segment.
- `translation`: Target language translation.
- `review_status`: Review classification. Use `translated` for an optional explicit changed-text marker. When translation deliberately equals source, use exactly one of `intentional_name`, `code`, `ascii_art`, or `puzzle_token`. `unreviewed_same_as_source` is a blocking computed/interim state, not approval.
- `review_reason`: Concrete reason for deliberately retaining source text. Required when `translation == raw`; examples: "canonical player name", "redstone label consumed as a code", or "ASCII divider has no linguistic content".
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

For aggregated sign faces, one `sign` unit may represent four physical sign lines. Its `raw` is the complete sign face joined with newlines. `context.line_texts` preserves the four source lines, `address.sign_lines[]` stores the per-line `nbt_path`, and each segment may include:

```json
{
  "index": 0,
  "line_index": 0,
  "nbt_path": "root.front_text.messages[0]",
  "json_path": "$.lines[0].text",
  "component_json_path": "$.text",
  "raw": "This symbol",
  "translation": "",
  "translation_key": "mcmap.example.sign.abc123.part_0"
}
```

Codex should translate the complete sign first, then fill each segment so the copied-map apply step can replace each original line/text node with a `translate` key.

A face with only one non-empty player-text line is still one aggregated sign unit. `context.line_texts` must contain four slots, and `address.sign_lines[]` should preserve all four parsable physical line anchors, including blank lines. Record `address.block_pos` (`x`, `y`, `z`) when the surrounding block entity provides it; the NBT path remains the exact apply anchor while coordinates provide a stable cross-check.

## Identity-Coupled Text

Item display text can also participate in gameplay identity. Scanner output may add:

```json
{
  "context": {
    "identity_coupled": true,
    "identity_group": "stable-group-id",
    "identity_role": "trade_input",
    "identity_resolution": "structural",
    "identity_confidence": "high",
    "identity_item_id": "minecraft:slime_ball",
    "identity_item_fingerprint": "stable-item-structure-id",
    "identity_non_text_fingerprint": "stable-redacted-structure-id",
    "identity_item_root": "root.block_entities[0].Offers.Recipes[0].buy",
    "identity_slot": "name",
    "identity_text_shape": ["Kitatcho Coin"]
  }
}
```

Common roles are `trade_input`, `trade_output`, `container`, `producer`, `consumer`, `predicate`, and `item_component`.

`identity_item_fingerprint` is computed from the parsed item ID and canonical full item structure. It includes non-text components and custom data as well as source name/lore component structure, while excluding top-level stack count and container slot so differently sized stacks of the same logical item can match. `identity_non_text_fingerprint` redacts name/lore string values and exists for diagnosis; it is not sufficient by itself to merge items. The canonical `identity_group` combines the full item fingerprint with one text slot such as `name` or `lore[0]`.

Rows in the same identity group must use the same unit key and matching segment keys. Rows with equal visible text but different item fingerprints must remain separate. This prevents both occurrence-key divergence and accidental merging of same-named items with different lore, model data, damage, enchantments, or custom data.

If the scanner cannot recover a containing item structure, it sets `identity_resolution` to `unresolved`, preserves the occurrence key, and final QA blocks delivery. Resolve these rows through `resolve-item-identities` and a reviewed decisions JSON instead of editing keys ad hoc. A manual group must include a non-empty `review_reason`; an approved external/runtime source must include `identity_external_source_reason`.

Entity names that are also referenced by selectors use separate context metadata:

```json
{
  "context": {
    "selector_identity_coupled": true,
    "selector_identity_role": "entity_custom_name",
    "selector_identity_strategy": "preserve-source-custom-name",
    "selector_references": [
      {
        "reference_id": "stable-reference-id",
        "source_file": "datapacks/map/data/map/function/tick.mcfunction",
        "address": {"function_line": 12},
        "selector": "@e[name=Guide]",
        "selector_span": [11, 25],
        "argument": "name",
        "match_kind": "name",
        "name": "Guide",
        "negated": false,
        "dynamic": false
      }
    ]
  },
  "mode_support": []
}
```

`selector_identity_role` is `entity_custom_name` for a visible entity-name producer and `selector_predicate_literal` for hardcoded text inside `@e[nbt={CustomName:...}]`. Both roles preserve the exact source value and remove copied-world patch modes. Fill the unit and all segments source-equal with `review_status: intentional_name` and a reason naming the selector dependency. `qa/identity_qa.json` blocks changed translations, missing preserve strategy, or re-enabled `hybrid-key-injection`/`embedded-direct`.

Static fingerprints and relationship checks do not replace fresh-save gameplay tests. Recognized static named-entity selectors are protected, but dynamic macros, storage-built items, loot pipelines, plugins, and runtime-built selectors still require explicit runtime validation.

## Binary Anchors

For `.dat` and `.mca` sources, `address` may include:

- `nbt_path`: Path to the string inside the parsed NBT tree.
- `chunk`: Region/chunk metadata, including `region_x`, `region_z`, `chunk_x`, `chunk_z`, and `local_index`.

These anchors are sufficient for QA and copied-world apply tooling.

For JSON text component strings in `.dat` or `.mca`, `apply-hybrid-keys` can use `nbt_path`, optional `chunk`, `json_path`, and optional `command_span` to patch the copied NBT data. Plain NBT strings without `json_path` are not hybrid key-injection targets; `apply-direct-text` can replace them directly in a copied world when the current NBT string still exactly equals `raw` and `translation` is filled.

For quoted JSON text components inside command/SNBT strings, `address.command_string_span` stores the exact quoted string span. `apply-hybrid-keys` decodes the quoted JSON string, injects the generated key, and writes the string back with the original quote style.

## Escape Semantics

JSONL stores decoded string values. When a `raw`, `translation`, or `protected` value appears on disk as `\n`, that may be JSON's serialization of a real newline. It is not automatically the literal two-character sequence backslash+n.

Interpret escape shape from the decoded value:

- A real newline in `raw` should remain a real newline in `translation` unless a controlled layout rewrite is documented.
- A literal `\\n` in `raw` should remain literal `\\n` in `translation` unless the command/JSON/SNBT layer is intentionally rewritten.
- Preserve other structural escapes such as `\t`, `\"`, `\\`, and `\uXXXX` when they appear in `protected`.
- Be extra careful after TSV or spreadsheet round-trips; table tools may display or import multiline fields differently from JSONL.

For JSON text components stored as strings inside datapack JSON, `address.json_string_path` points to the outer JSON string value, while `address.json_path` and `context.text_nodes[]` refer to the decoded component inside that string.

For plain `.mcfunction` messages, `address.command_plain_span` stores the exact unquoted message span. For plain JSON strings inside command JSON spans, `address.command_span` stores the JSON object/array span and `address.command_json_path` stores the string path inside that decoded JSON. For plain SNBT/storage strings inside command strings, `address.command_string_span` stores the exact quoted value. `apply-direct-text` can replace these direct anchors when the current source text still exactly equals `raw`.

Every unit extracted through the command scanner carries semantic review context: `context.command_text` is the complete original command line/string, `context.effective_command_text` is the command after optional `/`, macro prefix, and outer `execute ... run` wrappers are resolved, `context.command_word` names that effective command, `context.effective_command_offset` anchors it in the original string, and `context.execute_wrapped` records whether an execute wrapper was removed. Codex must use these fields to distinguish rendered payload from command grammar and logic operands before choosing whether to translate.

For datapack function context, `address.function_id`, `address.function_line`, and optional `address.function_macro` identify the function and macro line status. `scan_report.json` may also include `function_call_graph` and `suspicious_text_hints`; use them for context loading and QA, not as translation rows.

## Translation JSONL

Final translation files may contain the same schema with `translation` filled. For resource-pack generation, entries without `translation_key` are skipped unless a key-generation mode is explicitly used.

## Validation Rules

- IDs must be unique.
- `raw` must be non-empty.
- `mode_support` must be non-empty.
- `resource-pack` units must include `translation_key`.
- `resource_namespace` should be present for every unit that can be exported to a resource pack.
- `translation` must preserve every protected token exactly.
- A non-empty changed translation is `translated`. A source-equal translation is incomplete unless it has an approved preserve `review_status` and non-empty `review_reason`.
- `translation` must preserve escape shape: do not replace a real newline with literal `\\n`, and do not replace literal `\\n` with a real newline unless the row is deliberately rewritten and documented.
- `segments[].json_path` and `segments[].raw` must match `context.text_nodes`.
- Sign segments may also include `nbt_path`, `line_index`, and `component_json_path`; keep these unchanged during translation.
- Segment-level source-equal text follows the same status/reason rule as the full unit.
- Identity-coupled rows in one group must not contain conflicting unit/segment translation keys or translations.
- `segments[].translation_key` must use only lowercase `a-z`, digits, `_`, `.`, and `-`.
- `edition` must be `java`.
- JSON strings must remain valid UTF-8.
