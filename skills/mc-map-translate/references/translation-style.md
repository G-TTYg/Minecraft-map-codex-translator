# Translation Style

Use this reference before translating player-facing text.

## Translation Engine Boundary

- Use Codex as the translator. Do not call external machine-translation APIs, browser translators, or third-party localization services by default.
- Translate from the local map context: workpack rows, source summaries, nearby command/function order, glossary, repeated strings, notes, and QA findings.
- Do not treat a literal draft as finished. Revise for player comprehension, tone, UI length, consistency, and Minecraft semantics before writing `translation`.
- If the user explicitly requests an external translation service, keep it as a draft source only; Codex must still review, adapt, and QA every accepted translation against the map context.

## Voice

- Use the target language and Java locale requested by the user. Do not default to Chinese.
- If the user gives only a language name, choose a standard Java locale code and record it in `project.json` or notes.
- Prefer natural, concise, game-native phrasing in the target language.
- Preserve the intended player emotion: urgency, mystery, comedy, horror, tutorial clarity, or epic narration.
- Avoid stiff machine-translation phrasing.
- Prefer a polished localization that a native player would accept over word-for-word source structure.

## Consistency

Maintain a glossary for:

- Map title and subtitle.
- Character names, factions, races, classes, places, regions, dungeons.
- Quest items, keys, weapons, spells, mechanics.
- UI verbs, buttons, failure messages, success messages.
- Puzzle vocabulary and recurring hints.

If a term appears in lore and gameplay instructions, choose one translation that works in both contexts unless there is a deliberate in-world distinction.

## Completeness Standard

- Translate every player-facing unit in the current workpack that can be safely translated with available context.
- Do not skip difficult jokes, lore, signs, bossbars, books, or puzzle text merely because they require adaptation. Translate them carefully, or flag a concrete blocker and proposed rewrite.
- Preserve deliberate untranslated names, IDs, brand-like terms, or stylistic source-language fragments only when they are intentional, and note that choice when it could be mistaken for missed coverage.
- For low-confidence or risky units, prefer a cautious contextual translation plus a QA note over silent omission, unless applying it could break commands or puzzle mechanics.

## Minecraft-Specific Rules

- Keep vanilla Minecraft item/block/entity names aligned with official target-language names when they clearly refer to vanilla content and official names are known.
- Do not translate internal IDs such as `minecraft:diamond_sword`.
- Keep selectors, placeholders, color codes, keybind IDs, score expressions, and command syntax unchanged.
- Preserve newline shape unless a UI-length issue requires a controlled change.
- Preserve escape shape. A JSONL file may show a real newline as `\n` because JSON serializes it that way; keep it as a real newline in the decoded value. If the decoded source text contains literal backslash+n (`\\n`), keep those two characters unless the command/JSON/SNBT layer is being deliberately rewritten.
- Treat backslash escapes such as `\n`, `\t`, `\"`, `\\`, and `\uXXXX` as protected syntax until proven to be ordinary prose. Do not double-escape them and do not let a spreadsheet, shell, or model rewrite turn them into another layer.
- Preserve JSON text component styling and events.

## Multilingual Encoding

- Treat all translation text as Unicode and all project artifacts as UTF-8 unless a source format explicitly says otherwise.
- Do not judge non-ASCII text only from PowerShell, cmd, or shell output. If display looks corrupt, inspect the JSON/JSONL/TSV file with an explicit UTF-8 reader or compare escaped code points.
- Do not paste terminal-mojibake back into translation files. Re-open the original UTF-8 artifact and repair the affected rows before validation or export.
- Do not repair apparent `\n` sequences by hand without checking the decoded JSON value. In JSON/JSONL display, `\n` often means an actual line break; in SNBT or command strings it may be a literal escape that Minecraft/JSON will interpret later.
- Preserve intentional full-width punctuation, combining marks, accents, kana/han/hangul, right-to-left text, emoji, and Minecraft section sign formatting. Do not normalize Unicode unless the target language or a QA finding requires it.
- Watch custom font providers and bitmap fonts in resource packs. A string can be valid UTF-8 but still render as missing glyphs in-game; report that as a font/rendering QA issue, not as a translation success.

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
- Did any escape sequence or newline shape change accidentally?
- Did the translation become too long for the expected display surface?
- Did every target-language character survive UTF-8 read/write and table import/export without mojibake?
