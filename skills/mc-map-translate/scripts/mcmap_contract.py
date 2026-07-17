#!/usr/bin/env python3
"""Contract helpers for the mc-map-translate skill.

This script intentionally stays dependency-free. It defines the project
contract, validates JSONL workpacks, and builds language-only resource packs.
Java world scanning/apply tools can evolve behind the same JSONL contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REQUIRED_UNIT_FIELDS = {
    "id",
    "edition",
    "source_kind",
    "source_file",
    "address",
    "raw",
    "mode_support",
}

VALID_MODES = {"resource-pack", "hybrid-key-injection", "embedded-direct"}
VALID_CONFIDENCE = {"high", "medium", "low"}
KEY_RE = re.compile(r"^[a-z0-9_.-]+$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}_[a-z0-9]{2,8}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(*parts: str) -> str:
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            value["_line_no"] = line_no
            rows.append(value)
    return rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            clean = {key: value for key, value in row.items() if key != "_line_no"}
            handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")


def require_locale(value: str, field: str) -> None:
    if not LOCALE_RE.match(value):
        raise ValueError(f"{field} must be a Java locale code like zh_cn, ja_jp, fr_fr, or es_es: {value}")


def validate_unit(unit: dict[str, Any]) -> list[str]:
    line = unit.get("_line_no", "?")
    errors: list[str] = []
    missing = sorted(REQUIRED_UNIT_FIELDS - set(unit))
    if missing:
        errors.append(f"line {line}: missing required fields: {', '.join(missing)}")

    if "id" in unit and not str(unit["id"]).strip():
        errors.append(f"line {line}: id is empty")
    if "raw" in unit and not str(unit["raw"]).strip():
        errors.append(f"line {line}: raw is empty")
    if "address" in unit and not isinstance(unit["address"], dict):
        errors.append(f"line {line}: address must be an object")

    modes = unit.get("mode_support")
    if not isinstance(modes, list) or not modes:
        errors.append(f"line {line}: mode_support must be a non-empty list")
    else:
        invalid = sorted({str(mode) for mode in modes} - VALID_MODES)
        if invalid:
            errors.append(f"line {line}: invalid mode_support values: {', '.join(invalid)}")
        if "resource-pack" in modes and not unit.get("translation_key"):
            errors.append(f"line {line}: resource-pack mode requires translation_key")

    confidence = unit.get("confidence")
    if confidence is not None and confidence not in VALID_CONFIDENCE:
        errors.append(f"line {line}: confidence must be high, medium, or low")

    key = unit.get("translation_key")
    if key and not KEY_RE.match(str(key)):
        errors.append(f"line {line}: translation_key contains unsupported characters: {key}")

    namespace = unit.get("resource_namespace")
    if namespace and not KEY_RE.match(str(namespace)):
        errors.append(f"line {line}: resource_namespace contains unsupported characters: {namespace}")

    edition = unit.get("edition")
    if edition is not None and edition != "java":
        errors.append(f"line {line}: only edition=java is supported")

    protected = unit.get("protected", [])
    translation = unit.get("translation", "")
    if protected and translation:
        if not isinstance(protected, list):
            errors.append(f"line {line}: protected must be a list")
        else:
            for token in protected:
                if str(token) not in str(translation):
                    errors.append(f"line {line}: protected token missing from translation: {token}")

    return errors


def validate_units(path: Path) -> int:
    rows = read_jsonl(path)
    errors: list[str] = []
    seen: dict[str, int] = {}

    for row in rows:
        unit_id = str(row.get("id", ""))
        if unit_id:
            previous = seen.get(unit_id)
            if previous is not None:
                errors.append(f"line {row.get('_line_no')}: duplicate id {unit_id} first seen on line {previous}")
            else:
                seen[unit_id] = int(row.get("_line_no", 0))
        errors.extend(validate_unit(row))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"invalid: {len(errors)} error(s), {len(rows)} unit(s)", file=sys.stderr)
        return 1

    print(f"valid: {len(rows)} unit(s)")
    return 0


def init_workspace(args: argparse.Namespace) -> int:
    require_locale(args.target, "--target")
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    for dirname in ["scan", "workpacks", "translations", "qa", "exports"]:
        (out / dirname).mkdir(exist_ok=True)

    project = {
        "schema": "mc-map-translate-project.v1",
        "created_at": utc_now(),
        "source": str(source),
        "edition": "java",
        "target_locale": args.target,
        "preferred_mode": args.mode,
        "notes": "Original map should remain read-only. Patch copies only.",
    }
    write_json(out / "project.json", project)

    units = out / "translation_units.jsonl"
    if not units.exists():
        units.write_text("", encoding="utf-8")

    glossary = out / "glossary.md"
    if not glossary.exists():
        glossary.write_text(
            "# Glossary\n\n"
            "| Source | Translation | Type | Notes |\n"
            "| --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

    print(f"workspace: {out}")
    print(f"project: {out / 'project.json'}")
    print(f"units: {units}")
    return 0


def normalize_key_piece(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_.-")
    return value or "text"


def iter_pack_entries(
    rows: Iterable[dict[str, Any]],
    generate_missing_keys: bool,
    namespace: str,
    include_hybrid_keys: bool,
) -> Iterable[tuple[str, str, str, str]]:
    for row in rows:
        modes = row.get("mode_support", [])
        if "resource-pack" not in modes and not include_hybrid_keys:
            continue
        raw = str(row.get("raw", ""))
        translation = str(row.get("translation", "")).strip()
        if not translation:
            continue

        resource_namespace = str(row.get("resource_namespace") or namespace)
        key = row.get("translation_key")
        if not key and generate_missing_keys:
            source_kind = normalize_key_piece(str(row.get("source_kind", "text")))
            row_id = normalize_key_piece(str(row.get("id") or stable_id(str(row.get("source_file", "")), raw)))
            key = f"{resource_namespace}.{source_kind}.{row_id}"

        if not key:
            continue

        yield resource_namespace, str(key), raw, translation


def make_resource_pack(args: argparse.Namespace) -> int:
    require_locale(args.target, "--target")
    if args.source_locale:
        require_locale(args.source_locale, "--source-locale")
    translations = Path(args.translations).resolve()
    out = Path(args.out).resolve()
    rows = read_jsonl(translations)

    errors: list[str] = []
    lang_by_namespace: dict[str, dict[str, str]] = {}
    source_by_namespace: dict[str, dict[str, str]] = {}

    for namespace, key, raw, translated in iter_pack_entries(rows, args.generate_missing_keys, args.namespace, args.include_hybrid_keys):
        if not KEY_RE.match(namespace):
            errors.append(f"invalid resource namespace: {namespace}")
            continue
        if not KEY_RE.match(key):
            errors.append(f"invalid translation key: {key}")
            continue
        lang = lang_by_namespace.setdefault(namespace, {})
        source_lang = source_by_namespace.setdefault(namespace, {})
        if key in lang and lang[key] != translated:
            errors.append(f"duplicate key with conflicting translation in namespace {namespace}: {key}")
            continue
        lang[key] = translated
        source_lang[key] = raw

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    pack = {
        "pack": {
            "pack_format": args.pack_format,
            "description": args.description,
        }
    }
    write_json(out / "pack.mcmeta", pack)

    for namespace, lang in sorted(lang_by_namespace.items()):
        lang_root = out / "assets" / namespace / "lang"
        write_json(lang_root / f"{args.target}.json", dict(sorted(lang.items())))
        if args.source_locale:
            write_json(lang_root / f"{args.source_locale}.json", dict(sorted(source_by_namespace.get(namespace, {}).items())))

    report = {
        "schema": "mc-map-translate-resource-pack-report.v1",
        "created_at": utc_now(),
        "translations_file": str(translations),
        "output": str(out),
        "namespace": args.namespace,
        "target_locale": args.target,
        "source_locale": args.source_locale,
        "namespace_count": len(lang_by_namespace),
        "entry_count": sum(len(items) for items in lang_by_namespace.values()),
        "skipped_without_translation_or_key": len(rows) - sum(len(items) for items in lang_by_namespace.values()),
        "include_hybrid_keys": args.include_hybrid_keys,
        "hardcoded_units_not_included": sum(
            1
            for row in rows
            if row.get("translation") and "resource-pack" not in row.get("mode_support", []) and not args.include_hybrid_keys
        ),
    }
    write_json(out / "mcmap_resource_pack_report.json", report)
    print(f"resource_pack: {out}")
    print(f"namespaces: {len(lang_by_namespace)}")
    print(f"entries: {sum(len(items) for items in lang_by_namespace.values())}")
    return 0


def summarize_units(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.units).resolve())
    by_kind: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    translated = 0
    low_confidence = 0

    for row in rows:
        by_kind[str(row.get("source_kind", "unknown"))] = by_kind.get(str(row.get("source_kind", "unknown")), 0) + 1
        for mode in row.get("mode_support", []):
            by_mode[str(mode)] = by_mode.get(str(mode), 0) + 1
        if row.get("translation"):
            translated += 1
        if row.get("confidence") == "low":
            low_confidence += 1

    summary = {
        "total": len(rows),
        "translated": translated,
        "untranslated": len(rows) - translated,
        "low_confidence": low_confidence,
        "by_source_kind": dict(sorted(by_kind.items())),
        "by_mode": dict(sorted(by_mode.items())),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def selected_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = rows
    if getattr(args, "mode", ""):
        selected = [row for row in selected if args.mode in row.get("mode_support", [])]
    if getattr(args, "source_kind", ""):
        kinds = {part.strip() for part in args.source_kind.split(",") if part.strip()}
        selected = [row for row in selected if row.get("source_kind") in kinds]
    if getattr(args, "source_file_regex", ""):
        pattern = re.compile(args.source_file_regex)
        selected = [row for row in selected if pattern.search(str(row.get("source_file", "")))]
    if getattr(args, "untranslated_only", False):
        selected = [row for row in selected if not str(row.get("translation", "")).strip()]
    return selected


def make_workpacks(args: argparse.Namespace) -> int:
    rows = selected_rows(read_jsonl(Path(args.units).resolve()), args)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dedupe_raw:
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for row in rows:
            key = (str(row.get("source_kind", "")), str(row.get("raw", "")))
            if key not in seen:
                seen.add(key)
                deduped.append(row)
        rows = deduped

    rows.sort(key=lambda row: (str(row.get("source_kind", "")), str(row.get("source_file", "")), str(row.get("id", ""))))
    packs: list[dict[str, Any]] = []
    for start in range(0, len(rows), args.max_units):
        chunk = rows[start : start + args.max_units]
        pack_no = len(packs) + 1
        path = out_dir / f"workpack_{pack_no:03d}.jsonl"
        write_jsonl(path, chunk)
        packs.append(
            {
                "file": path.name,
                "unit_count": len(chunk),
                "source_kinds": sorted({str(row.get("source_kind", "")) for row in chunk}),
                "first_id": chunk[0].get("id") if chunk else "",
                "last_id": chunk[-1].get("id") if chunk else "",
            }
        )

    index = {
        "schema": "mc-map-translate-workpack-index.v1",
        "created_at": utc_now(),
        "source_units": str(Path(args.units).resolve()),
        "total_selected_units": len(rows),
        "max_units": args.max_units,
        "packs": packs,
    }
    write_json(out_dir / "workpack_index.json", index)
    print(f"workpacks: {out_dir}")
    print(f"packs: {len(packs)}")
    print(f"units: {len(rows)}")
    return 0


def export_table(args: argparse.Namespace) -> int:
    rows = selected_rows(read_jsonl(Path(args.units).resolve()), args)
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "source_kind",
        "raw",
        "translation",
        "protected",
        "notes",
        "source_file",
        "translation_key",
        "resource_namespace",
        "confidence",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, dialect="excel-tab", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = dict(row)
            clean["protected"] = json.dumps(clean.get("protected", []), ensure_ascii=False)
            writer.writerow(clean)
    print(f"table: {out}")
    print(f"rows: {len(rows)}")
    return 0


def import_table(args: argparse.Namespace) -> int:
    base_rows = read_jsonl(Path(args.base).resolve())
    table_path = Path(args.table).resolve()
    out = Path(args.out).resolve()
    updates: dict[str, dict[str, str]] = {}

    with table_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect="excel-tab")
        for row in reader:
            row_id = str(row.get("id", "")).strip()
            if row_id:
                updates[row_id] = row

    changed = 0
    for row in base_rows:
        update = updates.get(str(row.get("id", "")))
        if not update:
            continue
        translation = update.get("translation", "")
        notes = update.get("notes", "")
        if translation or args.allow_empty_translation:
            if row.get("translation") != translation:
                changed += 1
            row["translation"] = translation
        if notes:
            row["notes"] = notes

    write_jsonl(out, base_rows)
    print(f"translations: {out}")
    print(f"updated_rows: {changed}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MC map localization contract helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init-workspace", help="create a localization work folder")
    init.add_argument("source", help="world folder, map zip, or package path")
    init.add_argument("--out", required=True, help="output work directory")
    init.add_argument("--target", required=True, help="target Java locale, for example zh_cn, ja_jp, fr_fr")
    init.add_argument("--mode", choices=sorted(VALID_MODES), default="resource-pack")
    init.set_defaults(func=init_workspace)

    validate = subparsers.add_parser("validate-units", help="validate translation unit JSONL")
    validate.add_argument("units", help="translation_units.jsonl or translations.jsonl")
    validate.set_defaults(func=lambda args: validate_units(Path(args.units).resolve()))

    pack = subparsers.add_parser("make-resource-pack", help="build a language resource pack directory from translations JSONL")
    pack.add_argument("translations", help="translations JSONL")
    pack.add_argument("--out", required=True, help="output resource pack directory")
    pack.add_argument("--namespace", default="mcmap", help="resource pack namespace")
    pack.add_argument("--target", required=True, help="target Java locale file")
    pack.add_argument("--source-locale", default="en_us", help="optional source locale file; pass empty string to disable")
    pack.add_argument("--pack-format", type=int, required=True, help="Java resource pack format")
    pack.add_argument("--description", default="Minecraft map translation resource pack")
    pack.add_argument("--generate-missing-keys", action="store_true", help="generate keys for translated units without translation_key")
    pack.add_argument("--include-hybrid-keys", action="store_true", help="include hardcoded units that need copied-map key injection to take effect")
    pack.set_defaults(func=make_resource_pack)

    summary = subparsers.add_parser("summarize-units", help="summarize unit coverage")
    summary.add_argument("units", help="translation units JSONL")
    summary.set_defaults(func=summarize_units)

    workpacks = subparsers.add_parser("make-workpacks", help="split units into translation workpack JSONL files")
    workpacks.add_argument("units", help="translation units JSONL")
    workpacks.add_argument("--out-dir", required=True, help="directory for generated workpacks")
    workpacks.add_argument("--max-units", type=int, default=200, help="maximum units per workpack")
    workpacks.add_argument("--mode", choices=sorted(VALID_MODES), default="", help="filter by supported export mode")
    workpacks.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    workpacks.add_argument("--source-file-regex", default="", help="regex filter for source_file")
    workpacks.add_argument("--untranslated-only", action="store_true", help="only include units with empty translation")
    workpacks.add_argument("--dedupe-raw", action="store_true", help="keep only first unit per source_kind/raw pair")
    workpacks.set_defaults(func=make_workpacks)

    export = subparsers.add_parser("export-table", help="export units to UTF-8 TSV for review or translation")
    export.add_argument("units", help="translation units JSONL")
    export.add_argument("--out", required=True, help="output TSV path")
    export.add_argument("--mode", choices=sorted(VALID_MODES), default="", help="filter by supported export mode")
    export.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    export.add_argument("--source-file-regex", default="", help="regex filter for source_file")
    export.add_argument("--untranslated-only", action="store_true", help="only include units with empty translation")
    export.set_defaults(func=export_table)

    import_cmd = subparsers.add_parser("import-table", help="merge TSV translations back into JSONL by id")
    import_cmd.add_argument("table", help="translated TSV from export-table")
    import_cmd.add_argument("--base", required=True, help="base translation units JSONL")
    import_cmd.add_argument("--out", required=True, help="output translations JSONL")
    import_cmd.add_argument("--allow-empty-translation", action="store_true", help="allow TSV empty translations to overwrite existing translations")
    import_cmd.set_defaults(func=import_table)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "source_locale", None) == "":
        args.source_locale = None
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
