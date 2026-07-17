# Translation Style

Use this reference before translating player-facing text.

## Voice

- Use the target language and Java locale requested by the user. Do not default to Chinese.
- If the user gives only a language name, choose a standard Java locale code and record it in `project.json` or notes.
- Prefer natural, concise, game-native phrasing in the target language.
- Preserve the intended player emotion: urgency, mystery, comedy, horror, tutorial clarity, or epic narration.
- Avoid stiff machine-translation phrasing.

## Consistency

Maintain a glossary for:

- Map title and subtitle.
- Character names, factions, races, classes, places, regions, dungeons.
- Quest items, keys, weapons, spells, mechanics.
- UI verbs, buttons, failure messages, success messages.
- Puzzle vocabulary and recurring hints.

If a term appears in lore and gameplay instructions, choose one translation that works in both contexts unless there is a deliberate in-world distinction.

## Minecraft-Specific Rules

- Keep vanilla Minecraft item/block/entity names aligned with official target-language names when they clearly refer to vanilla content and official names are known.
- Do not translate internal IDs such as `minecraft:diamond_sword`.
- Keep selectors, placeholders, color codes, keybind IDs, score expressions, and command syntax unchanged.
- Preserve newline shape unless a UI-length issue requires a controlled change.
- Preserve JSON text component styling and events.

## Multi-Text Components

Some JSON text components split one visible message across several `text` nodes so each part can have a different color, event, selector, or score insertion. For rows with `segments[]`:

- Translate the full `raw` message first and store that in `translation`.
- Fill `segments[].translation` from the full-message translation, not from isolated word-by-word translation.
- Preserve the original component order because `split-nodes` keeps selectors, scores, and styled fragments in place.
- If the target language cannot read naturally in the original segment order, leave the difficult segment translations empty and note that the unit needs a future compose/direct rewrite instead of forcing bad phrasing.

## Creative Text

- Riddles, jokes, poems, acronyms, and wordplay may need adaptation rather than direct translation.
- Preserve solvability of puzzles. If a puzzle depends on English spelling, flag it and propose a localized puzzle rewrite.
- For lore, prefer literary fluency while keeping facts and foreshadowing intact.

## Review Questions

Before finalizing a batch, ask internally:

- Does a player know what to do?
- Does the tone match the scene?
- Are names and terms consistent with the glossary?
- Did any protected token change?
- Did the translation become too long for the expected display surface?
