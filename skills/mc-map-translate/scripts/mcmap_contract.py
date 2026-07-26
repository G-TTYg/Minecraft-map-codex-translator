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
from collections import Counter, defaultdict
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
VALID_REVIEW_STATUSES = {
    "translated",
    "intentional_name",
    "code",
    "ascii_art",
    "puzzle_token",
    "unreviewed_same_as_source",
}
APPROVED_PRESERVE_STATUSES = {"intentional_name", "code", "ascii_art", "puzzle_token"}
KEY_RE = re.compile(r"^[a-z0-9_.-]+$")
LOCALE_RE = re.compile(r"^[a-z]{2,3}_[a-z0-9]{2,8}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_id(*parts: str) -> str:
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def resolve_jsonl_inputs(path: Path) -> list[Path]:
    path = path.resolve()
    if path.is_file():
        return [path]
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_dir():
        raise ValueError(f"expected a JSONL file or directory: {path}")

    # Project directory: prefer finalized merged translations, then editable
    # translation parts, then canonical scan units. This lets export/apply
    # commands accept the project root without accidentally reading indexes.
    if (path / "project.json").exists() or (path / "index" / "manifest.json").exists():
        finalized = path / "translations" / "translations.jsonl"
        if finalized.exists():
            return [finalized]
        manifest_path = path / "index" / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
            manifest_files: list[Path] = []
            for pack in manifest.get("workpacks", []):
                if not isinstance(pack, dict):
                    continue
                part = pack.get("translation_part")
                if isinstance(part, str) and part:
                    candidate = (path / part).resolve()
                    if candidate.exists() and candidate.is_file():
                        manifest_files.append(candidate)
            if manifest_files:
                return sorted(manifest_files)
        parts = path / "translations" / "parts"
        if parts.exists():
            files = sorted(parts.glob("*.jsonl"))
            if files:
                return files
        units = path / "translation_units.jsonl"
        if units.exists():
            return [units]

    # Direct workpack/parts directories should read only immediate JSONL files.
    files = sorted(
        item
        for item in path.glob("*.jsonl")
        if item.is_file() and item.name not in {"unit_index.jsonl", "source_index.jsonl", "raw_repeats.jsonl"}
    )
    if files:
        return files
    raise FileNotFoundError(f"no readable JSONL inputs found in {path}")


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
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
            value["_source_path"] = str(path)
            rows.append(value)
    return rows


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for input_path in resolve_jsonl_inputs(path):
        rows.extend(read_jsonl_file(input_path))
    return rows


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            clean = {key: value for key, value in row.items() if not key.startswith("_")}
            handle.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")


def require_locale(value: str, field: str) -> None:
    if not LOCALE_RE.match(value):
        raise ValueError(f"{field} must be a Java locale code like zh_cn, ja_jp, fr_fr, or es_es: {value}")


def replacement_character_errors(value: Any, field: str, line: Any) -> list[str]:
    if isinstance(value, str) and "\ufffd" in value:
        return [f"line {line}: {field} contains Unicode replacement character U+FFFD; check UTF-8 decoding before export"]
    return []


def visible_escape(value: str) -> str:
    return value.encode("unicode_escape").decode("ascii")


def escape_shape_errors(raw: Any, translation: Any, field: str, line: Any) -> list[str]:
    if not isinstance(raw, str) or not isinstance(translation, str) or not translation:
        return []
    errors: list[str] = []
    if "\n" in raw and "\n" not in translation and "\\n" in translation:
        errors.append(
            f"line {line}: {field} appears to contain literal \\\\n where the source has a real newline; keep the real line break or note a deliberate rewrite"
        )
    if "\\n" in raw and "\\n" not in translation and "\n" in translation:
        errors.append(
            f"line {line}: {field} converted literal \\\\n into a real newline; preserve the backslash escape unless the command/JSON layer is being deliberately rewritten"
        )
    if "\t" in raw and "\t" not in translation and "\\t" in translation:
        errors.append(
            f"line {line}: {field} appears to contain literal \\\\t where the source has a real tab"
        )
    if "\\t" in raw and "\\t" not in translation and "\t" in translation:
        errors.append(
            f"line {line}: {field} converted literal \\\\t into a real tab"
        )
    return errors


def translation_review_state(item: dict[str, Any]) -> str:
    raw = str(item.get("raw", ""))
    translation = str(item.get("translation", ""))
    if not translation.strip():
        return "untranslated"
    if translation != raw:
        return "translated"
    status = str(item.get("review_status", "")).strip()
    reason = str(item.get("review_reason", "")).strip()
    if status in APPROVED_PRESERVE_STATUSES and reason:
        return status
    return "unreviewed_same_as_source"


def translation_item_complete(item: dict[str, Any]) -> bool:
    return translation_review_state(item) not in {"untranslated", "unreviewed_same_as_source"}


def translation_review_errors(item: dict[str, Any], field: str, line: Any) -> list[str]:
    status = str(item.get("review_status", "")).strip()
    reason = str(item.get("review_reason", "")).strip()
    raw = str(item.get("raw", ""))
    translation = str(item.get("translation", ""))
    errors: list[str] = []
    if status and status not in VALID_REVIEW_STATUSES:
        errors.append(f"line {line}: {field}.review_status is invalid: {status}")
    if not translation.strip():
        return errors
    if translation == raw:
        if status not in APPROVED_PRESERVE_STATUSES or not reason:
            errors.append(
                f"line {line}: {field} equals the source; set review_status to intentional_name, code, ascii_art, or puzzle_token and write review_reason"
            )
    elif status in APPROVED_PRESERVE_STATUSES or status == "unreviewed_same_as_source":
        errors.append(f"line {line}: {field}.review_status={status} conflicts with a changed translation")
    return errors


def unit_encoding_errors(unit: dict[str, Any]) -> list[str]:
    line = unit.get("_line_no", "?")
    errors: list[str] = []
    errors.extend(replacement_character_errors(unit.get("raw", ""), "raw", line))
    errors.extend(replacement_character_errors(unit.get("translation", ""), "translation", line))
    errors.extend(replacement_character_errors(unit.get("notes", ""), "notes", line))
    errors.extend(replacement_character_errors(unit.get("review_reason", ""), "review_reason", line))
    errors.extend(escape_shape_errors(unit.get("raw", ""), unit.get("translation", ""), "translation", line))
    segments = unit.get("segments")
    if isinstance(segments, list):
        for offset, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            errors.extend(replacement_character_errors(segment.get("raw", ""), f"segments[{offset}].raw", line))
            errors.extend(replacement_character_errors(segment.get("translation", ""), f"segments[{offset}].translation", line))
            errors.extend(replacement_character_errors(segment.get("review_reason", ""), f"segments[{offset}].review_reason", line))
            errors.extend(
                escape_shape_errors(
                    segment.get("raw", ""),
                    segment.get("translation", ""),
                    f"segments[{offset}].translation",
                    line,
                )
            )
    return errors


def print_blocking_errors(errors: list[str], summary: str) -> None:
    for error in errors:
        print(error, file=sys.stderr)
    print(summary, file=sys.stderr)


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
    errors.extend(unit_encoding_errors(unit))
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
                    errors.append(f"line {line}: protected token missing from translation: {visible_escape(str(token))}")

    errors.extend(translation_review_errors(unit, "translation", line))
    segments = unit.get("segments")
    if isinstance(segments, list):
        for offset, segment in enumerate(segments):
            if isinstance(segment, dict):
                errors.extend(translation_review_errors(segment, f"segments[{offset}].translation", line))

    errors.extend(validate_segments(unit))
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
        print_blocking_errors(errors, f"invalid: {len(errors)} error(s), {len(rows)} unit(s)")
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


def default_segment_key(row: dict[str, Any], index: int) -> str:
    base_key = str(row.get("translation_key") or "")
    if not base_key:
        namespace = normalize_key_piece(str(row.get("resource_namespace") or "mcmap"))
        source_kind = normalize_key_piece(str(row.get("source_kind", "text")))
        row_id = normalize_key_piece(str(row.get("id") or stable_id(str(row.get("source_file", "")), str(row.get("raw", "")))))
        base_key = f"{namespace}.{source_kind}.{row_id}"
    return f"{base_key}.part_{index}"


def text_nodes_for_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    text_nodes = context.get("text_nodes") if isinstance(context, dict) else []
    if not isinstance(text_nodes, list):
        return []
    return [node for node in text_nodes if isinstance(node, dict)]


def ensure_segments(row: dict[str, Any], overwrite: bool = False) -> bool:
    text_nodes = text_nodes_for_row(row)
    if len(text_nodes) <= 1:
        return False
    if row.get("segments") and not overwrite:
        return False

    existing_by_path: dict[str, dict[str, Any]] = {}
    if isinstance(row.get("segments"), list):
        for item in row["segments"]:
            if isinstance(item, dict) and isinstance(item.get("json_path"), str):
                existing_by_path[item["json_path"]] = item

    segments: list[dict[str, Any]] = []
    for index, node in enumerate(text_nodes):
        json_path = str(node.get("json_path", ""))
        existing = existing_by_path.get(json_path, {})
        segments.append(
            {
                "index": index,
                "json_path": json_path,
                "raw": str(node.get("text", "")),
                "translation": str(existing.get("translation", "")),
                "review_status": str(existing.get("review_status", "")),
                "review_reason": str(existing.get("review_reason", "")),
                "translation_key": str(existing.get("translation_key") or default_segment_key(row, index)),
            }
        )
    row["segments"] = segments
    return True


def segment_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    segments = row.get("segments")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def validate_segments(unit: dict[str, Any]) -> list[str]:
    line = unit.get("_line_no", "?")
    errors: list[str] = []
    if "segments" not in unit:
        return errors
    segments = unit.get("segments")
    if not isinstance(segments, list):
        return [f"line {line}: segments must be a list when present"]

    text_nodes = text_nodes_for_row(unit)
    text_by_path = {str(node.get("json_path", "")): str(node.get("text", "")) for node in text_nodes}
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()

    for offset, segment in enumerate(segments):
        if not isinstance(segment, dict):
            errors.append(f"line {line}: segments[{offset}] must be an object")
            continue
        index = segment.get("index")
        if not isinstance(index, int) or index < 0:
            errors.append(f"line {line}: segments[{offset}].index must be a non-negative integer")
        json_path = segment.get("json_path")
        if not isinstance(json_path, str) or not json_path:
            errors.append(f"line {line}: segments[{offset}].json_path is required")
            continue
        if json_path in seen_paths:
            errors.append(f"line {line}: duplicate segment json_path: {json_path}")
        seen_paths.add(json_path)
        raw = str(segment.get("raw", ""))
        if json_path in text_by_path and raw != text_by_path[json_path]:
            errors.append(f"line {line}: segments[{offset}].raw does not match context.text_nodes for {json_path}")
        key = str(segment.get("translation_key", ""))
        if not key:
            errors.append(f"line {line}: segments[{offset}].translation_key is required")
        elif not KEY_RE.match(key):
            errors.append(f"line {line}: segments[{offset}].translation_key contains unsupported characters: {key}")
        elif key in seen_keys:
            errors.append(f"line {line}: duplicate segment translation_key: {key}")
        seen_keys.add(key)

    return errors


def iter_pack_entries(
    rows: Iterable[dict[str, Any]],
    generate_missing_keys: bool,
    namespace: str,
    include_hybrid_keys: bool,
) -> Iterable[tuple[str, str, str, str, str, str]]:
    for row in rows:
        modes = row.get("mode_support", [])
        if "resource-pack" not in modes and not include_hybrid_keys:
            continue

        resource_namespace = str(row.get("resource_namespace") or namespace)
        row_id = str(row.get("id", ""))

        if include_hybrid_keys:
            for segment in segment_entries(row):
                raw_segment = str(segment.get("raw", ""))
                translated_segment = str(segment.get("translation", "")).strip()
                key_segment = str(segment.get("translation_key", ""))
                if translated_segment and key_segment:
                    yield resource_namespace, key_segment, raw_segment, translated_segment, row_id, "segment"

        raw = str(row.get("raw", ""))
        translation = str(row.get("translation", "")).strip()
        if not translation:
            continue

        key = row.get("translation_key")
        if not key and generate_missing_keys:
            source_kind = normalize_key_piece(str(row.get("source_kind", "text")))
            row_id = normalize_key_piece(str(row.get("id") or stable_id(str(row.get("source_file", "")), raw)))
            key = f"{resource_namespace}.{source_kind}.{row_id}"

        if not key:
            continue

        yield resource_namespace, str(key), raw, translation, row_id, "unit"


def identity_consistency_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        if not context.get("identity_coupled"):
            continue
        group_id = str(context.get("identity_group", "")).strip()
        if group_id:
            groups[group_id].append(row)

    conflicts: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for group_id, group_rows in sorted(groups.items()):
        keys_by_slot: dict[str, set[str]] = defaultdict(set)
        translations_by_slot: dict[str, set[str]] = defaultdict(set)
        roles: Counter[str] = Counter()
        for row in group_rows:
            context = row.get("context") if isinstance(row.get("context"), dict) else {}
            roles[str(context.get("identity_role", "item_component"))] += 1
            key = str(row.get("translation_key", "")).strip()
            if key and "hybrid-key-injection" in row.get("mode_support", []):
                keys_by_slot["unit"].add(key)
            translation = str(row.get("translation", ""))
            if translation.strip():
                translations_by_slot["unit"].add(translation)
            for segment in segment_entries(row):
                slot = f"segment:{segment.get('index', '')}"
                segment_key = str(segment.get("translation_key", "")).strip()
                if segment_key:
                    keys_by_slot[slot].add(segment_key)
                segment_translation = str(segment.get("translation", ""))
                if segment_translation.strip():
                    translations_by_slot[slot].add(segment_translation)

        key_conflicts = {slot: sorted(values) for slot, values in keys_by_slot.items() if len(values) > 1}
        translation_conflicts = {
            slot: sorted(values) for slot, values in translations_by_slot.items() if len(values) > 1
        }
        if key_conflicts or translation_conflicts:
            conflicts.append(
                {
                    "identity_group": group_id,
                    "unit_ids": [str(row.get("id", "")) for row in group_rows],
                    "key_conflicts": key_conflicts,
                    "translation_conflicts": translation_conflicts,
                }
            )
        summaries.append(
            {
                "identity_group": group_id,
                "unit_count": len(group_rows),
                "source_kind": str(group_rows[0].get("source_kind", "")),
                "raw": str(group_rows[0].get("raw", "")),
                "roles": dict(sorted(roles.items())),
                "canonical_keys": {slot: sorted(values) for slot, values in sorted(keys_by_slot.items())},
            }
        )

    return {
        "group_count": len(groups),
        "unit_count": sum(len(group) for group in groups.values()),
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "groups": summaries,
        "scope_note": (
            "Static QA verifies canonical translation keys and translations within scanner identity groups. "
            "It does not replace in-game trade, clear, predicate, NPC-selector, or quest-item testing."
        ),
    }


def make_resource_pack(args: argparse.Namespace) -> int:
    require_locale(args.target, "--target")
    if args.source_locale:
        require_locale(args.source_locale, "--source-locale")
    translations = Path(args.translations).resolve()
    out = Path(args.out).resolve()
    rows = read_jsonl(translations)

    errors: list[str] = []
    for row in rows:
        errors.extend(unit_encoding_errors(row))
        errors.extend(translation_review_errors(row, "translation", row.get("_line_no", "?")))
        for offset, segment in enumerate(segment_entries(row)):
            errors.extend(
                translation_review_errors(
                    segment,
                    f"segments[{offset}].translation",
                    row.get("_line_no", "?"),
                )
            )
    identity_qa = identity_consistency_report(rows)
    if identity_qa["conflict_count"]:
        errors.append(f"identity-coupled translation/key conflicts: {identity_qa['conflict_count']} group(s)")
    lang_by_namespace: dict[str, dict[str, str]] = {}
    source_by_namespace: dict[str, dict[str, str]] = {}
    emitted_unit_ids: set[str] = set()
    segment_entry_count = 0

    for namespace, key, raw, translated, row_id, entry_kind in iter_pack_entries(
        rows,
        args.generate_missing_keys,
        args.namespace,
        args.include_hybrid_keys,
    ):
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
        if row_id:
            emitted_unit_ids.add(row_id)
        if entry_kind == "segment":
            segment_entry_count += 1

    if errors:
        print_blocking_errors(errors, f"resource-pack export blocked: {len(errors)} error(s)")
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
        "segment_entry_count": segment_entry_count,
        "rows_without_pack_entries": sum(1 for row in rows if str(row.get("id", "")) not in emitted_unit_ids),
        "include_hybrid_keys": args.include_hybrid_keys,
        "identity_qa": identity_qa,
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


def prepare_segments(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.units).resolve())
    out = Path(args.out).resolve()
    selected = selected_rows(rows, args)
    selected_ids = {str(row.get("id", "")) for row in selected}

    changed_units = 0
    segment_count = 0
    for row in rows:
        if str(row.get("id", "")) not in selected_ids:
            continue
        if "hybrid-key-injection" not in row.get("mode_support", []):
            continue
        if ensure_segments(row, overwrite=args.overwrite):
            changed_units += 1
        segment_count += len(segment_entries(row))

    write_jsonl(out, rows)
    report = {
        "schema": "mc-map-translate-segment-prepare-report.v1",
        "created_at": utc_now(),
        "source_units": str(Path(args.units).resolve()),
        "output": str(out),
        "selected_units": len(selected),
        "changed_units": changed_units,
        "segment_count": segment_count,
        "overwrite": args.overwrite,
    }
    write_json(out.with_suffix(out.suffix + ".segment_report.json"), report)
    print(f"segmented_units: {out}")
    print(f"changed_units: {changed_units}")
    print(f"segments: {segment_count}")
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


def relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("source_file", "")), str(row.get("source_kind", "")), str(row.get("id", "")))


def safe_group_filename(value: str, suffix: str = ".jsonl") -> str:
    slug = normalize_key_piece(value.replace("!", "__").replace("/", "__").replace("\\", "__"))
    digest = stable_id(value)
    if len(slug) > 92:
        slug = slug[:92].rstrip("_.-")
    return f"{slug or 'group'}__{digest}{suffix}"


def raw_preview(value: str, limit: int = 180) -> str:
    clean = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def grouped_counts(rows: Iterable[dict[str, Any]], field: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[str(row.get(field, "unknown"))] += 1
    return dict(sorted(counts.items()))


def grouped_mode_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for mode in row.get("mode_support", []):
            counts[str(mode)] += 1
    return dict(sorted(counts.items()))


def minimal_index_row(row: dict[str, Any], workpack_path: str = "", translation_part: str = "") -> dict[str, Any]:
    segments = segment_entries(row)
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    return {
        "id": row.get("id", ""),
        "source_kind": row.get("source_kind", ""),
        "source_file": row.get("source_file", ""),
        "raw_preview": raw_preview(str(row.get("raw", ""))),
        "translation_key": row.get("translation_key", ""),
        "resource_namespace": row.get("resource_namespace", ""),
        "mode_support": row.get("mode_support", []),
        "confidence": row.get("confidence", ""),
        "protected": row.get("protected", []),
        "review_status": row.get("review_status", ""),
        "identity_coupled": bool(context.get("identity_coupled")),
        "identity_group": context.get("identity_group", ""),
        "identity_role": context.get("identity_role", ""),
        "segment_count": len(segments),
        "workpack": workpack_path,
        "translation_part": translation_part,
    }


def write_source_summary(path: Path, source_file: str, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {source_file}",
        "",
        f"- Units: {len(rows)}",
        f"- Source kinds: {', '.join(sorted({str(row.get('source_kind', 'unknown')) for row in rows}))}",
        f"- Modes: {', '.join(sorted({str(mode) for row in rows for mode in row.get('mode_support', [])}))}",
        "",
        "## Units",
        "",
    ]
    for row in sorted(rows, key=row_sort_key):
        protected = row.get("protected", [])
        protected_note = f" protected={json.dumps(protected, ensure_ascii=False)}" if protected else ""
        lines.append(
            f"- `{row.get('id', '')}` `{row.get('source_kind', '')}` `{row.get('confidence', '')}`{protected_note}: "
            f"{raw_preview(str(row.get('raw', '')), 260)}"
        )
        segments = segment_entries(row)
        if segments:
            for segment in segments:
                lines.append(
                    f"  - segment `{segment.get('index', '')}` `{segment.get('json_path', '')}`: "
                    f"{raw_preview(str(segment.get('raw', '')), 220)}"
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def row_translation_complete(row: dict[str, Any]) -> bool:
    if not translation_item_complete(row):
        return False
    segments = segment_entries(row)
    if segments and any(not translation_item_complete(segment) for segment in segments):
        return False
    return True


def pack_progress(rows: list[dict[str, Any]], expected_count: int | None = None) -> dict[str, int | str]:
    total_units = expected_count if expected_count is not None else len(rows)
    translated_units = sum(1 for row in rows if str(row.get("translation", "")).strip())
    complete_units = sum(1 for row in rows if row_translation_complete(row))
    total_segments = sum(len(segment_entries(row)) for row in rows)
    translated_segments = sum(
        1
        for row in rows
        for segment in segment_entries(row)
        if str(segment.get("translation", "")).strip()
    )
    unreviewed_same_as_source = sum(
        1
        for row in rows
        if translation_review_state(row) == "unreviewed_same_as_source"
    ) + sum(
        1
        for row in rows
        for segment in segment_entries(row)
        if translation_review_state(segment) == "unreviewed_same_as_source"
    )
    if total_units and complete_units >= total_units and translated_segments >= total_segments:
        status = "complete"
    elif translated_units or translated_segments:
        status = "in-progress"
    else:
        status = "pending"
    return {
        "status": status,
        "total_units": total_units,
        "translated_units": translated_units,
        "complete_units": complete_units,
        "total_segments": total_segments,
        "translated_segments": translated_segments,
        "unreviewed_same_as_source": unreviewed_same_as_source,
    }


def load_project_manifest(project_root: Path) -> dict[str, Any]:
    manifest_path = project_root / "index" / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing indexed project manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest must be an object: {manifest_path}")
    return manifest


def format_progress_line(pack: dict[str, Any], stats: dict[str, int | str]) -> str:
    checked = "x" if stats["status"] == "complete" else " "
    file_name = Path(str(pack.get("file", ""))).name
    part_name = str(pack.get("translation_part", ""))
    source_count = len(pack.get("source_files", [])) if isinstance(pack.get("source_files"), list) else 0
    kind_text = ", ".join(str(kind) for kind in pack.get("source_kinds", []) if kind) or "unknown"
    return (
        f"- [{checked}] `{file_name}` {stats['status']} - "
        f"units {stats['complete_units']}/{stats['total_units']} complete "
        f"({stats['translated_units']} translated), "
        f"segments {stats['translated_segments']}/{stats['total_segments']}; "
        f"unreviewed same-as-source {stats['unreviewed_same_as_source']}; "
        f"part `{part_name}`; sources {source_count}; kinds {kind_text}"
    )


def write_progress_todo_file(project_root: Path, out: Path | None = None) -> dict[str, Any]:
    project_root = project_root.resolve()
    manifest = load_project_manifest(project_root)
    workpacks = [pack for pack in manifest.get("workpacks", []) if isinstance(pack, dict)]
    out_path = out.resolve() if out else project_root / "translation_progress.md"

    pack_reports: list[dict[str, Any]] = []
    overall = Counter()
    lines = [
        "# Translation Progress",
        "",
        "Maintain this TODO whenever translating the map. Update it before starting a workpack, after writing a translation part, and after merging/export QA.",
        "",
        f"- Project root: `{project_root}`",
        f"- Updated: {utc_now()}",
        f"- Workpacks: {len(workpacks)}",
        "",
        "## Workpack TODO",
        "",
    ]

    for pack in workpacks:
        part = str(pack.get("translation_part", ""))
        part_path = project_root / part if part else Path()
        if part and part_path.exists():
            rows = read_jsonl_file(part_path.resolve())
        else:
            fallback = str(pack.get("file", ""))
            fallback_path = project_root / fallback if fallback else Path()
            rows = read_jsonl_file(fallback_path.resolve()) if fallback and fallback_path.exists() else []
        expected_count = int(pack.get("unit_count", len(rows)) or len(rows))
        stats = pack_progress(rows, expected_count)
        pack_reports.append({"file": pack.get("file", ""), "translation_part": part, **stats})
        overall["total_units"] += int(stats["total_units"])
        overall["translated_units"] += int(stats["translated_units"])
        overall["complete_units"] += int(stats["complete_units"])
        overall["total_segments"] += int(stats["total_segments"])
        overall["translated_segments"] += int(stats["translated_segments"])
        overall["unreviewed_same_as_source"] += int(stats["unreviewed_same_as_source"])
        lines.append(format_progress_line(pack, stats))

    complete_packs = sum(1 for item in pack_reports if item["status"] == "complete")
    in_progress_packs = sum(1 for item in pack_reports if item["status"] == "in-progress")
    pending_packs = sum(1 for item in pack_reports if item["status"] == "pending")
    lines.extend(
        [
            "",
            "## Totals",
            "",
            f"- Complete workpacks: {complete_packs}/{len(workpacks)}",
            f"- In-progress workpacks: {in_progress_packs}",
            f"- Pending workpacks: {pending_packs}",
            f"- Complete units: {overall['complete_units']}/{overall['total_units']}",
            f"- Translated units: {overall['translated_units']}/{overall['total_units']}",
            f"- Translated segments: {overall['translated_segments']}/{overall['total_segments']}",
            f"- Unreviewed same-as-source slots: {overall['unreviewed_same_as_source']}",
            "",
            "## Maintenance Rules",
            "",
            "- Mark the active workpack in the conversation TODO before editing it.",
            "- Keep this file synchronized with `translations/parts/*.jsonl`; rerun `write-progress-todo` after each batch.",
            "- Do not mark a workpack complete until full-unit translations and all required segment translations are filled; unchanged source text needs an approved review status and reason.",
            "- Run `merge-translations`, `validate-units`, and `translation-status` before final export.",
            "",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "path": str(out_path),
        "workpacks": len(workpacks),
        "complete_workpacks": complete_packs,
        "in_progress_workpacks": in_progress_packs,
        "pending_workpacks": pending_packs,
        "total_units": overall["total_units"],
        "complete_units": overall["complete_units"],
        "translated_units": overall["translated_units"],
        "total_segments": overall["total_segments"],
        "translated_segments": overall["translated_segments"],
        "unreviewed_same_as_source": overall["unreviewed_same_as_source"],
    }


def make_project_files(args: argparse.Namespace) -> int:
    rows = selected_rows(read_jsonl(Path(args.units).resolve()), args)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.prepare_segments:
        for row in rows:
            if "hybrid-key-injection" in row.get("mode_support", []):
                ensure_segments(row)

    rows.sort(key=row_sort_key)
    index_dir = out_dir / "index"
    units_dir = out_dir / "units"
    by_source_dir = units_dir / "by-source"
    by_kind_dir = units_dir / "by-kind"
    context_dir = out_dir / "context" / "source-summaries"
    workpack_dir = out_dir / "workpacks" / "contextual"
    translation_parts_dir = out_dir / "translations" / "parts"

    for directory in [index_dir, by_source_dir, by_kind_dir, context_dir, workpack_dir, translation_parts_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    rows_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row.get("source_file", "unknown"))].append(row)
        rows_by_kind[str(row.get("source_kind", "unknown"))].append(row)

    source_entries: list[dict[str, Any]] = []
    source_file_lookup: dict[str, str] = {}
    context_file_lookup: dict[str, str] = {}
    for source_file, source_rows in sorted(rows_by_source.items()):
        units_path = by_source_dir / safe_group_filename(source_file)
        summary_path = context_dir / safe_group_filename(source_file, ".md")
        write_jsonl(units_path, sorted(source_rows, key=row_sort_key))
        write_source_summary(summary_path, source_file, source_rows)
        source_rel = relative_posix(units_path, out_dir)
        summary_rel = relative_posix(summary_path, out_dir)
        source_file_lookup[source_file] = source_rel
        context_file_lookup[source_file] = summary_rel
        source_entries.append(
            {
                "source_file": source_file,
                "unit_count": len(source_rows),
                "source_kinds": sorted({str(row.get("source_kind", "")) for row in source_rows}),
                "mode_support": sorted({str(mode) for row in source_rows for mode in row.get("mode_support", [])}),
                "units_file": source_rel,
                "context_summary": summary_rel,
                "first_id": source_rows[0].get("id", "") if source_rows else "",
                "last_id": source_rows[-1].get("id", "") if source_rows else "",
            }
        )

    kind_entries: list[dict[str, Any]] = []
    for source_kind, kind_rows in sorted(rows_by_kind.items()):
        kind_path = by_kind_dir / safe_group_filename(source_kind)
        write_jsonl(kind_path, sorted(kind_rows, key=row_sort_key))
        kind_entries.append(
            {
                "source_kind": source_kind,
                "unit_count": len(kind_rows),
                "units_file": relative_posix(kind_path, out_dir),
            }
        )

    workpacks: list[dict[str, Any]] = []
    index_rows: list[dict[str, Any]] = []
    for start in range(0, len(rows), args.max_units):
        chunk = rows[start : start + args.max_units]
        pack_no = len(workpacks) + 1
        pack_path = workpack_dir / f"workpack_{pack_no:03d}.jsonl"
        part_path = translation_parts_dir / f"workpack_{pack_no:03d}.jsonl"
        write_jsonl(pack_path, chunk)
        if args.overwrite_translation_parts or not part_path.exists():
            write_jsonl(part_path, chunk)
        pack_rel = relative_posix(pack_path, out_dir)
        part_rel = relative_posix(part_path, out_dir)
        source_files = sorted({str(row.get("source_file", "")) for row in chunk})
        context_files = sorted({context_file_lookup[source_file] for source_file in source_files if source_file in context_file_lookup})
        workpacks.append(
            {
                "file": pack_rel,
                "translation_part": part_rel,
                "unit_count": len(chunk),
                "source_files": source_files,
                "source_kinds": sorted({str(row.get("source_kind", "")) for row in chunk}),
                "context_summaries": context_files,
                "first_id": chunk[0].get("id", "") if chunk else "",
                "last_id": chunk[-1].get("id", "") if chunk else "",
            }
        )
        for row in chunk:
            index_rows.append(minimal_index_row(row, pack_rel, part_rel))

    raw_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        raw_groups[str(row.get("raw", ""))].append(row)
    repeated_rows = [
        {
            "raw": raw,
            "count": len(group),
            "unit_ids": [str(row.get("id", "")) for row in group],
            "source_kinds": sorted({str(row.get("source_kind", "")) for row in group}),
            "source_files": sorted({str(row.get("source_file", "")) for row in group}),
        }
        for raw, group in raw_groups.items()
        if len(group) > 1
    ]
    repeated_rows.sort(key=lambda item: (-int(item["count"]), str(item["raw"])))

    write_jsonl(index_dir / "unit_index.jsonl", index_rows)
    write_jsonl(index_dir / "source_index.jsonl", source_entries)
    write_jsonl(index_dir / "kind_index.jsonl", kind_entries)
    write_jsonl(index_dir / "raw_repeats.jsonl", repeated_rows)

    instructions = [
        "# Translation Project Instructions",
        "",
        "This folder is intentionally indexed. Do not load every file into model context.",
        "",
        "1. Read `index/manifest.json` and `glossary.md` first.",
        "2. Read and maintain `translation_progress.md` as the persistent workpack TODO list.",
        "3. Pick one unchecked or in-progress `workpacks/contextual/workpack_###.jsonl` entry from the TODO/manifest.",
        "4. Read only the listed `context/source-summaries/*.md` files and any nearby source/kind index rows needed for that pack.",
        "5. Fill `translation` in the matching `translations/parts/workpack_###.jsonl` file.",
        "6. For `segments[]`, translate the full unit first, then fill each segment so the styled Minecraft component still reads naturally.",
        "7. When translation equals raw, set an approved review_status and concrete review_reason; otherwise it remains incomplete.",
        "8. Keep one translation and canonical keys across every context.identity_coupled group.",
        "9. Refresh `translation_progress.md` after each translated batch.",
        "10. Run `merge-translations`, `validate-units`, and `qa-translations` before export.",
        "",
    ]
    (out_dir / "translation_instructions.md").write_text("\n".join(instructions), encoding="utf-8")

    manifest = {
        "schema": "mc-map-translate-indexed-project.v1",
        "created_at": utc_now(),
        "source_units": str(Path(args.units).resolve()),
        "project_root": str(out_dir),
        "total_units": len(rows),
        "max_units_per_workpack": args.max_units,
        "prepared_segments": args.prepare_segments,
        "indexes": {
            "unit_index": "index/unit_index.jsonl",
            "source_index": "index/source_index.jsonl",
            "kind_index": "index/kind_index.jsonl",
            "raw_repeats": "index/raw_repeats.jsonl",
        },
        "directories": {
            "units_by_source": "units/by-source",
            "units_by_kind": "units/by-kind",
            "source_summaries": "context/source-summaries",
            "workpacks": "workpacks/contextual",
            "translation_parts": "translations/parts",
        },
        "counts_by_kind": grouped_counts(rows, "source_kind"),
        "counts_by_mode": grouped_mode_counts(rows),
        "source_file_count": len(rows_by_source),
        "workpack_count": len(workpacks),
        "workpacks": workpacks,
    }
    write_json(index_dir / "manifest.json", manifest)
    todo = write_progress_todo_file(out_dir)

    print(f"project_files: {out_dir}")
    print(f"units: {len(rows)}")
    print(f"sources: {len(rows_by_source)}")
    print(f"workpacks: {len(workpacks)}")
    print(f"translation_parts: {translation_parts_dir}")
    print(f"progress_todo: {todo['path']}")
    return 0


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
    errors: list[str] = []
    for row in rows:
        errors.extend(unit_encoding_errors(row))
    if errors:
        print_blocking_errors(errors, f"table export blocked: {len(errors)} encoding error(s)")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "source_kind",
        "raw",
        "translation",
        "review_status",
        "review_reason",
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
    errors: list[str] = []

    with table_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect="excel-tab")
        for row in reader:
            line = reader.line_num
            errors.extend(replacement_character_errors(row.get("translation", ""), "TSV translation", line))
            errors.extend(replacement_character_errors(row.get("notes", ""), "TSV notes", line))
            errors.extend(replacement_character_errors(row.get("review_reason", ""), "TSV review_reason", line))
            row_id = str(row.get("id", "")).strip()
            if row_id:
                updates[row_id] = row
    if errors:
        print_blocking_errors(errors, f"table import blocked: {len(errors)} encoding error(s)")
        return 1

    changed = 0
    for row in base_rows:
        update = updates.get(str(row.get("id", "")))
        if not update:
            continue
        translation = update.get("translation", "")
        notes = update.get("notes", "")
        review_status = str(update.get("review_status", "")).strip()
        review_reason = str(update.get("review_reason", "")).strip()
        if translation or args.allow_empty_translation:
            if row.get("translation") != translation:
                changed += 1
            row["translation"] = translation
        if notes:
            row["notes"] = notes
        if review_status:
            row["review_status"] = review_status
        if review_reason:
            row["review_reason"] = review_reason

    write_jsonl(out, base_rows)
    print(f"translations: {out}")
    print(f"updated_rows: {changed}")
    return 0


def export_segment_table(args: argparse.Namespace) -> int:
    rows = selected_rows(read_jsonl(Path(args.units).resolve()), args)
    out = Path(args.out).resolve()
    errors: list[str] = []
    for row in rows:
        errors.extend(unit_encoding_errors(row))
    if errors:
        print_blocking_errors(errors, f"segment table export blocked: {len(errors)} encoding error(s)")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "unit_id",
        "segment_index",
        "json_path",
        "raw",
        "translation",
        "review_status",
        "review_reason",
        "translation_key",
        "unit_raw",
        "unit_translation",
        "unit_review_status",
        "unit_review_reason",
        "source_kind",
        "source_file",
        "confidence",
        "notes",
    ]
    count = 0
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, dialect="excel-tab", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if "hybrid-key-injection" not in row.get("mode_support", []):
                continue
            ensure_segments(row)
            segments = segment_entries(row)
            if len(segments) <= 1:
                continue
            for segment in segments:
                writer.writerow(
                    {
                        "unit_id": row.get("id", ""),
                        "segment_index": segment.get("index", ""),
                        "json_path": segment.get("json_path", ""),
                        "raw": segment.get("raw", ""),
                        "translation": segment.get("translation", ""),
                        "review_status": segment.get("review_status", ""),
                        "review_reason": segment.get("review_reason", ""),
                        "translation_key": segment.get("translation_key", ""),
                        "unit_raw": row.get("raw", ""),
                        "unit_translation": row.get("translation", ""),
                        "unit_review_status": row.get("review_status", ""),
                        "unit_review_reason": row.get("review_reason", ""),
                        "source_kind": row.get("source_kind", ""),
                        "source_file": row.get("source_file", ""),
                        "confidence": row.get("confidence", ""),
                        "notes": row.get("notes", ""),
                    }
                )
                count += 1
    print(f"segment_table: {out}")
    print(f"segments: {count}")
    return 0


def import_segment_table(args: argparse.Namespace) -> int:
    base_rows = read_jsonl(Path(args.base).resolve())
    table_path = Path(args.table).resolve()
    out = Path(args.out).resolve()
    by_id = {str(row.get("id", "")): row for row in base_rows}
    updates: dict[tuple[str, int], dict[str, str]] = {}
    unit_translation_updates: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    with table_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect="excel-tab")
        for row in reader:
            line = reader.line_num
            errors.extend(replacement_character_errors(row.get("translation", ""), "TSV segment translation", line))
            errors.extend(replacement_character_errors(row.get("unit_translation", ""), "TSV unit_translation", line))
            errors.extend(replacement_character_errors(row.get("notes", ""), "TSV notes", line))
            errors.extend(replacement_character_errors(row.get("review_reason", ""), "TSV segment review_reason", line))
            errors.extend(replacement_character_errors(row.get("unit_review_reason", ""), "TSV unit review_reason", line))
            unit_id = str(row.get("unit_id", "")).strip()
            if not unit_id:
                continue
            try:
                index = int(str(row.get("segment_index", "")).strip())
            except ValueError:
                continue
            translation = row.get("translation", "")
            if translation or args.allow_empty_translation:
                updates[(unit_id, index)] = {
                    "translation": translation,
                    "review_status": str(row.get("review_status", "")).strip(),
                    "review_reason": str(row.get("review_reason", "")).strip(),
                }
            unit_translation = row.get("unit_translation", "")
            if unit_translation:
                unit_translation_updates[unit_id] = {
                    "translation": unit_translation,
                    "review_status": str(row.get("unit_review_status", "")).strip(),
                    "review_reason": str(row.get("unit_review_reason", "")).strip(),
                }
    if errors:
        print_blocking_errors(errors, f"segment table import blocked: {len(errors)} encoding error(s)")
        return 1

    changed = 0
    for unit_id, unit_update in unit_translation_updates.items():
        row = by_id.get(unit_id)
        if row is not None:
            for field, value in unit_update.items():
                if value and row.get(field) != value:
                    row[field] = value
                    changed += 1

    for unit_id, index in sorted(updates):
        row = by_id.get(unit_id)
        if row is None:
            continue
        ensure_segments(row)
        segments = segment_entries(row)
        for segment in segments:
            if segment.get("index") != index:
                continue
            for field, value in updates[(unit_id, index)].items():
                if (value or (field == "translation" and args.allow_empty_translation)) and segment.get(field) != value:
                    segment[field] = value
                    changed += 1
            break

    write_jsonl(out, base_rows)
    print(f"translations: {out}")
    print(f"updated_segment_fields: {changed}")
    return 0


def merge_segment_updates(base_row: dict[str, Any], update_row: dict[str, Any], allow_empty: bool) -> int:
    update_segments = update_row.get("segments")
    if not isinstance(update_segments, list):
        return 0
    ensure_segments(base_row)
    base_segments = segment_entries(base_row)
    by_index = {segment.get("index"): segment for segment in base_segments}
    by_path = {segment.get("json_path"): segment for segment in base_segments}
    changed = 0

    for update_segment in update_segments:
        if not isinstance(update_segment, dict):
            continue
        target = by_index.get(update_segment.get("index")) or by_path.get(update_segment.get("json_path"))
        if target is None:
            continue
        translation = str(update_segment.get("translation", ""))
        if translation or allow_empty:
            if target.get("translation") != translation:
                target["translation"] = translation
                changed += 1
        key = str(update_segment.get("translation_key", ""))
        if key and target.get("translation_key") != key:
            target["translation_key"] = key
            changed += 1
        for field in ("review_status", "review_reason"):
            value = str(update_segment.get(field, "")).strip()
            if value and target.get(field) != value:
                target[field] = value
                changed += 1
    return changed


def apply_translation_updates(
    base_rows: list[dict[str, Any]],
    update_rows: list[dict[str, Any]],
    *,
    allow_empty: bool = False,
    allow_conflicts: bool = False,
) -> tuple[int, int, list[dict[str, str]], list[str]]:
    by_id = {str(row.get("id", "")): row for row in base_rows if row.get("id")}
    seen_unit_translation: dict[str, str] = {}
    seen_segment_translation: dict[tuple[str, int], str] = {}
    changed = 0
    unknown = 0
    conflicts: list[dict[str, str]] = []
    updated_ids: set[str] = set()

    for update in update_rows:
        row_id = str(update.get("id", "")).strip()
        if not row_id or row_id not in by_id:
            unknown += 1
            continue
        base = by_id[row_id]

        translation = str(update.get("translation", ""))
        if translation or allow_empty:
            previous = seen_unit_translation.get(row_id)
            if previous is not None and previous != translation:
                conflicts.append({"id": row_id, "field": "translation", "first": previous, "second": translation})
                if not allow_conflicts:
                    continue
            seen_unit_translation[row_id] = translation
            if base.get("translation") != translation:
                base["translation"] = translation
                changed += 1
                updated_ids.add(row_id)

        notes = str(update.get("notes", ""))
        if notes and base.get("notes") != notes:
            base["notes"] = notes
            changed += 1
            updated_ids.add(row_id)

        for field in ("review_status", "review_reason"):
            value = str(update.get(field, "")).strip()
            if value and base.get(field) != value:
                base[field] = value
                changed += 1
                updated_ids.add(row_id)

        update_segments = update.get("segments")
        if isinstance(update_segments, list):
            for segment in update_segments:
                if not isinstance(segment, dict):
                    continue
                index = segment.get("index")
                if not isinstance(index, int):
                    continue
                segment_translation = str(segment.get("translation", ""))
                if not segment_translation and not allow_empty:
                    continue
                key = (row_id, index)
                previous = seen_segment_translation.get(key)
                if previous is not None and previous != segment_translation:
                    conflicts.append(
                        {
                            "id": row_id,
                            "field": f"segments[{index}].translation",
                            "first": previous,
                            "second": segment_translation,
                        }
                    )
                    if not allow_conflicts:
                        continue
                seen_segment_translation[key] = segment_translation
            before = json.dumps(base.get("segments", []), ensure_ascii=False, sort_keys=True)
            changed += merge_segment_updates(base, update, allow_empty)
            after = json.dumps(base.get("segments", []), ensure_ascii=False, sort_keys=True)
            if before != after:
                updated_ids.add(row_id)

    return changed, unknown, conflicts, sorted(updated_ids)


def merge_translations(args: argparse.Namespace) -> int:
    base_path = Path(args.base).resolve()
    out = Path(args.out).resolve()
    base_rows = read_jsonl(base_path)

    update_rows: list[dict[str, Any]] = []
    input_files: list[str] = []
    for item in args.inputs:
        input_path = Path(item).resolve()
        files = resolve_jsonl_inputs(input_path)
        input_files.extend(str(path) for path in files)
        for file_path in files:
            update_rows.extend(read_jsonl_file(file_path))

    update_encoding_errors: list[str] = []
    for row in update_rows:
        update_encoding_errors.extend(unit_encoding_errors(row))
    if update_encoding_errors:
        print_blocking_errors(update_encoding_errors, f"merge blocked: {len(update_encoding_errors)} input encoding error(s)")
        return 1

    changed, unknown, conflicts, updated_ids = apply_translation_updates(
        base_rows,
        update_rows,
        allow_empty=args.allow_empty_translation,
        allow_conflicts=args.allow_conflicts,
    )
    if conflicts and not args.allow_conflicts:
        for conflict in conflicts[:20]:
            print(
                f"conflict: {conflict['id']} {conflict['field']} has multiple translations",
                file=sys.stderr,
            )
        print(f"merge blocked: {len(conflicts)} conflict(s)", file=sys.stderr)
        return 1

    encoding_errors: list[str] = []
    for row in base_rows:
        encoding_errors.extend(unit_encoding_errors(row))
    if encoding_errors:
        print_blocking_errors(encoding_errors, f"merge blocked: {len(encoding_errors)} encoding error(s)")
        return 1

    write_jsonl(out, base_rows)
    report = {
        "schema": "mc-map-translate-merge-report.v1",
        "created_at": utc_now(),
        "base": str(base_path),
        "inputs": input_files,
        "output": str(out),
        "base_units": len(base_rows),
        "input_rows": len(update_rows),
        "changed_fields": changed,
        "updated_units": len(updated_ids),
        "unknown_update_rows": unknown,
        "conflicts": conflicts,
    }
    write_json(out.with_suffix(out.suffix + ".merge_report.json"), report)
    print(f"translations: {out}")
    print(f"input_rows: {len(update_rows)}")
    print(f"updated_units: {len(updated_ids)}")
    print(f"unknown_update_rows: {unknown}")
    print(f"conflicts: {len(conflicts)}")
    return 0


def translation_status(args: argparse.Namespace) -> int:
    base_rows = read_jsonl(Path(args.units).resolve())
    rows = base_rows
    if args.translations:
        rows = [dict(row) for row in base_rows]
        updates = read_jsonl(Path(args.translations).resolve())
        apply_translation_updates(
            rows,
            updates,
            allow_empty=args.allow_empty_translation,
            allow_conflicts=True,
        )

    translated = [row for row in rows if str(row.get("translation", "")).strip()]
    complete = [row for row in rows if row_translation_complete(row)]
    review_states = Counter(translation_review_state(row) for row in rows)
    hybrid = [row for row in rows if "hybrid-key-injection" in row.get("mode_support", [])]
    segment_units = [row for row in rows if segment_entries(row)]
    translated_segments = 0
    total_segments = 0
    segment_review_states: Counter[str] = Counter()
    for row in segment_units:
        for segment in segment_entries(row):
            total_segments += 1
            segment_review_states[translation_review_state(segment)] += 1
            if str(segment.get("translation", "")).strip():
                translated_segments += 1

    by_kind: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = str(row.get("source_kind", "unknown"))
        item = by_kind.setdefault(kind, {"total": 0, "translated": 0, "complete": 0, "unreviewed_same_as_source": 0})
        item["total"] += 1
        if str(row.get("translation", "")).strip():
            item["translated"] += 1
        if row_translation_complete(row):
            item["complete"] += 1
        if translation_review_state(row) == "unreviewed_same_as_source":
            item["unreviewed_same_as_source"] += 1

    by_source: list[dict[str, Any]] = []
    rows_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[str(row.get("source_file", ""))].append(row)
    for source_file, source_rows in rows_by_source.items():
        count = len(source_rows)
        done = sum(1 for row in source_rows if row_translation_complete(row))
        if not args.incomplete_only or done < count:
            by_source.append(
                {
                    "source_file": source_file,
                    "total": count,
                    "complete": done,
                    "remaining": count - done,
                    "unreviewed_same_as_source": sum(
                        1 for row in source_rows if translation_review_state(row) == "unreviewed_same_as_source"
                    ),
                }
            )
    by_source.sort(key=lambda item: (-int(item["remaining"]), str(item["source_file"])))

    status = {
        "schema": "mc-map-translate-status.v2",
        "created_at": utc_now(),
        "total_units": len(rows),
        "translated_units": len(translated),
        "complete_units": len(complete),
        "remaining_units": len(rows) - len(complete),
        "review_states": dict(sorted(review_states.items())),
        "hybrid_units": len(hybrid),
        "segment_units": len(segment_units),
        "translated_segments": translated_segments,
        "total_segments": total_segments,
        "segment_review_states": dict(sorted(segment_review_states.items())),
        "by_source_kind": dict(sorted(by_kind.items())),
        "top_sources": by_source[: args.top_sources],
    }

    if args.out:
        write_json(Path(args.out).resolve(), status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def write_translation_qa_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Translation QA",
        "",
        f"- Status: **{report['status']}**",
        f"- Units: {report['total_units']}",
        f"- Complete units: {report['complete_units']}",
        f"- Unreviewed same-as-source slots: {report['unreviewed_same_as_source_count']}",
        f"- Identity groups: {report['identity_qa']['group_count']}",
        f"- Identity conflicts: {report['identity_qa']['conflict_count']}",
        f"- Sign faces: {report['sign_qa']['face_count']}",
        f"- Complete sign faces: {report['sign_qa']['complete_face_count']}",
        "",
    ]
    if report.get("blocking_reasons"):
        lines.extend(["## Blocking Reasons", ""])
        for reason in report["blocking_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
    if report.get("unreviewed_same_as_source"):
        lines.extend(["## Unreviewed Same As Source", ""])
        for item in report["unreviewed_same_as_source"][:100]:
            lines.append(
                f"- `{item.get('id', '')}` `{item.get('source_kind', '')}` `{item.get('source_file', '')}` "
                f"{item.get('field', 'translation')}: `{raw_preview(str(item.get('raw', '')), 180)}`"
            )
        lines.append("")
    if report["identity_qa"].get("conflicts"):
        lines.extend(["## Identity Conflicts", ""])
        for item in report["identity_qa"]["conflicts"][:50]:
            lines.append(
                f"- `{item.get('identity_group', '')}` units={len(item.get('unit_ids', []))} "
                f"key_conflicts={len(item.get('key_conflicts', {}))} "
                f"translation_conflicts={len(item.get('translation_conflicts', {}))}"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def qa_translations(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.translations).resolve())
    validation_errors: list[str] = []
    review_states: Counter[str] = Counter()
    unreviewed: list[dict[str, Any]] = []
    for row in rows:
        validation_errors.extend(validate_unit(row))
        state = translation_review_state(row)
        review_states[state] += 1
        if state == "unreviewed_same_as_source":
            unreviewed.append(
                {
                    "id": row.get("id", ""),
                    "field": "translation",
                    "raw": row.get("raw", ""),
                    "source_kind": row.get("source_kind", ""),
                    "source_file": row.get("source_file", ""),
                }
            )
        for segment in segment_entries(row):
            segment_state = translation_review_state(segment)
            review_states[f"segment:{segment_state}"] += 1
            if segment_state == "unreviewed_same_as_source":
                unreviewed.append(
                    {
                        "id": row.get("id", ""),
                        "field": f"segments[{segment.get('index', '')}].translation",
                        "raw": segment.get("raw", ""),
                        "source_kind": row.get("source_kind", ""),
                        "source_file": row.get("source_file", ""),
                    }
                )

    complete_rows = [row for row in rows if row_translation_complete(row)]
    sign_rows = [row for row in rows if str(row.get("source_kind", "")) == "sign"]
    identity_qa = identity_consistency_report(rows)
    blocking_reasons: list[str] = []
    if validation_errors:
        blocking_reasons.append(f"{len(validation_errors)} structural, encoding, protected-token, or review validation error(s)")
    if len(complete_rows) != len(rows) and not args.allow_incomplete:
        blocking_reasons.append(f"{len(rows) - len(complete_rows)} unit(s) are incomplete or unreviewed")
    if unreviewed:
        blocking_reasons.append(f"{len(unreviewed)} unchanged source slot(s) lack an approved status and reason")
    if identity_qa["conflict_count"]:
        blocking_reasons.append(f"{identity_qa['conflict_count']} identity-coupled group(s) have conflicting keys or translations")

    report = {
        "schema": "mc-map-translation-qa.v1",
        "created_at": utc_now(),
        "translations_file": str(Path(args.translations).resolve()),
        "status": "pass" if not blocking_reasons else "blocked",
        "allow_incomplete": bool(args.allow_incomplete),
        "total_units": len(rows),
        "complete_units": len(complete_rows),
        "remaining_units": len(rows) - len(complete_rows),
        "review_states": dict(sorted(review_states.items())),
        "unreviewed_same_as_source_count": len(unreviewed),
        "unreviewed_same_as_source": unreviewed[:500],
        "validation_error_count": len(validation_errors),
        "validation_errors": validation_errors[:500],
        "identity_qa": identity_qa,
        "sign_qa": {
            "face_count": len(sign_rows),
            "complete_face_count": sum(1 for row in sign_rows if row_translation_complete(row)),
            "segment_count": sum(len(segment_entries(row)) for row in sign_rows),
            "complete_segment_count": sum(
                1 for row in sign_rows for segment in segment_entries(row) if translation_item_complete(segment)
            ),
        },
        "blocking_reasons": blocking_reasons,
    }
    out = Path(args.out).resolve()
    write_json(out, report)
    markdown_path = out.with_suffix(".md")
    write_translation_qa_markdown(markdown_path, report)
    print(f"qa_report: {out}")
    print(f"qa_review: {markdown_path}")
    print(f"status: {report['status']}")
    return 0 if not blocking_reasons else 4


def write_progress_todo(args: argparse.Namespace) -> int:
    project_root = Path(args.project).resolve()
    out = Path(args.out).resolve() if args.out else None
    report = write_progress_todo_file(project_root, out)
    print(f"progress_todo: {report['path']}")
    print(f"workpacks: {report['workpacks']}")
    print(f"complete_workpacks: {report['complete_workpacks']}")
    print(f"complete_units: {report['complete_units']}/{report['total_units']}")
    print(f"translated_segments: {report['translated_segments']}/{report['total_segments']}")
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

    segments = subparsers.add_parser("prepare-segments", help="add segment translation slots for multi-text JSON component units")
    segments.add_argument("units", help="translation_units.jsonl or translations.jsonl")
    segments.add_argument("--out", required=True, help="output JSONL with segments[] scaffolds")
    segments.add_argument("--mode", choices=sorted(VALID_MODES), default="", help="filter by supported export mode")
    segments.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    segments.add_argument("--source-file-regex", default="", help="regex filter for source_file")
    segments.add_argument("--untranslated-only", action="store_true", help="only include units with empty translation")
    segments.add_argument("--overwrite", action="store_true", help="regenerate existing segments[] entries")
    segments.set_defaults(func=prepare_segments)

    summary = subparsers.add_parser("summarize-units", help="summarize unit coverage")
    summary.add_argument("units", help="translation units JSONL")
    summary.set_defaults(func=summarize_units)

    project = subparsers.add_parser("make-project-files", help="create indexed multi-file project layout from units")
    project.add_argument("units", help="translation units JSONL, project dir, or workpack dir")
    project.add_argument("--out-dir", required=True, help="project/work directory to populate")
    project.add_argument("--max-units", type=int, default=120, help="maximum units per contextual workpack")
    project.add_argument("--mode", choices=sorted(VALID_MODES), default="", help="filter by supported export mode")
    project.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    project.add_argument("--source-file-regex", default="", help="regex filter for source_file")
    project.add_argument("--untranslated-only", action="store_true", help="only include units with empty translation")
    project.add_argument(
        "--no-prepare-segments",
        dest="prepare_segments",
        action="store_false",
        help="do not scaffold segments[] for multi-text hybrid units",
    )
    project.add_argument(
        "--overwrite-translation-parts",
        action="store_true",
        help="rewrite existing translations/parts files instead of preserving staged work",
    )
    project.set_defaults(func=make_project_files, prepare_segments=True)

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

    export_segments = subparsers.add_parser("export-segment-table", help="export multi-text segment slots to UTF-8 TSV")
    export_segments.add_argument("units", help="translation units JSONL")
    export_segments.add_argument("--out", required=True, help="output TSV path")
    export_segments.add_argument("--mode", choices=sorted(VALID_MODES), default="", help="filter by supported export mode")
    export_segments.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    export_segments.add_argument("--source-file-regex", default="", help="regex filter for source_file")
    export_segments.add_argument("--untranslated-only", action="store_true", help="only include units with empty translation")
    export_segments.set_defaults(func=export_segment_table)

    import_segments = subparsers.add_parser("import-segment-table", help="merge translated segment TSV back into JSONL by unit id and segment index")
    import_segments.add_argument("table", help="translated segment TSV from export-segment-table")
    import_segments.add_argument("--base", required=True, help="base translation units JSONL")
    import_segments.add_argument("--out", required=True, help="output translations JSONL")
    import_segments.add_argument("--allow-empty-translation", action="store_true", help="allow TSV empty segment translations to overwrite existing segment translations")
    import_segments.set_defaults(func=import_segment_table)

    merge = subparsers.add_parser("merge-translations", help="merge translated JSONL files/directories into one canonical translations JSONL")
    merge.add_argument("inputs", nargs="+", help="translated workpack JSONL files, translation parts dirs, or a project dir")
    merge.add_argument("--base", required=True, help="base translation_units.jsonl or project dir")
    merge.add_argument("--out", required=True, help="output merged translations JSONL")
    merge.add_argument("--allow-empty-translation", action="store_true", help="allow empty translations to overwrite existing translations")
    merge.add_argument("--allow-conflicts", action="store_true", help="allow later conflicting translations to win")
    merge.set_defaults(func=merge_translations)

    status = subparsers.add_parser("translation-status", help="report translation coverage by kind, source, and segment slots")
    status.add_argument("units", help="base units JSONL or project dir")
    status.add_argument("--translations", default="", help="optional translated JSONL, parts dir, or project dir to overlay")
    status.add_argument("--allow-empty-translation", action="store_true", help="allow empty translations to overwrite base while computing status")
    status.add_argument("--incomplete-only", action="store_true", help="show only sources with remaining untranslated units")
    status.add_argument("--top-sources", type=int, default=20, help="number of source files to include")
    status.add_argument("--out", default="", help="optional JSON report output")
    status.set_defaults(func=translation_status)

    qa = subparsers.add_parser(
        "qa-translations",
        help="block delivery on incomplete/unreviewed translations or identity-coupled key conflicts",
    )
    qa.add_argument("translations", help="translations JSONL, parts directory, or project directory")
    qa.add_argument("--out", required=True, help="output QA JSON report; a Markdown review is written beside it")
    qa.add_argument("--allow-incomplete", action="store_true", help="allow empty translations for an interim QA run")
    qa.set_defaults(func=qa_translations)

    todo = subparsers.add_parser("write-progress-todo", help="write or refresh the persistent translation progress TODO file")
    todo.add_argument("project", help="indexed project work directory containing index/manifest.json")
    todo.add_argument("--out", default="", help="optional output path; defaults to <project>/translation_progress.md")
    todo.set_defaults(func=write_progress_todo)

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
