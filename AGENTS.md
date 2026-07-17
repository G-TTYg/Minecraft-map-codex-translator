# Agent Instructions

This repository contains a Codex plugin for Minecraft Java Edition map localization.

## Boundaries

- Keep the plugin Java Edition only unless Bedrock tooling is explicitly implemented and validated.
- Do not edit user map sources in place. Tools must write copied worlds, copied zips, resource packs, or reports.
- Prefer deterministic bundled scripts over ad hoc one-off translation/apply code.
- Do not vendor or depend on MCC-i18n. It may be used only as conceptual reference.

## Validation

Run these before publishing plugin changes:

```bash
python -m py_compile skills/mc-map-translate/scripts/mcmap_java_tools.py skills/mc-map-translate/scripts/mcmap_contract.py
python path/to/skill-creator/scripts/quick_validate.py skills/mc-map-translate
python path/to/plugin-creator/scripts/validate_plugin.py .
```

For apply tooling, also smoke-test `scan` and `apply-hybrid-keys` against a small fixture or a real Java map copy.
