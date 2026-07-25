#!/usr/bin/env python3
"""Java Edition map localization tools for mc-map-translate.

The scanner is conservative by design. It extracts language JSON and grouped
Minecraft JSON text components from resource packs, datapacks, functions,
NBT files, and supported region chunks. Unsupported binary formats are reported
instead of being guessed from raw bytes.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from mcmap_contract import ensure_segments, make_project_files, normalize_key_piece, read_jsonl, require_locale, stable_id, utc_now, write_json


LANG_PATH_RE = re.compile(r"(?:^|.*[!/])assets/([^/]+)/lang/([a-z]{2,3}_[a-z0-9]{2,8})\.json$")
REGION_PATH_RE = re.compile(r"(?:^|.*/)(region|entities|poi)/r\.(-?\d+)\.(-?\d+)\.mca$")
PROTECTED_TOKEN_RE = re.compile(
    r"(@[pares](?:\[[^\]]+\])?)"
    r"|(%(?:\d+\$)?[sdif])"
    r"|(\$\{[^}]+\})"
    r"|(\u00a7[0-9A-FK-ORa-fk-or])"
    r"|(minecraft:[a-z0-9_./:-]+)"
)
INTERNAL_ID_RE = re.compile(r"^[a-z0-9_.:/+-]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_TEXT_BYTES = 4_000_000
MAX_NBT_BYTES = 64_000_000

TEXT_COMPONENT_KEYS = {
    "text",
    "translate",
    "with",
    "extra",
    "score",
    "selector",
    "keybind",
    "nbt",
}
PLAIN_TEXT_PATH_HINTS = {
    "customname",
    "displayname",
    "levelname",
    "title",
    "subtitle",
    "description",
    "author",
    "filtered_title",
}
COMMAND_START_RE = re.compile(r"^\s*(tellraw|title|bossbar|scoreboard|team|summon|data|item|loot)\b", re.I)
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Entry:
    path: str
    data: bytes | None = None
    size: int = 0


@dataclass(frozen=True)
class NbtString:
    path: str
    value: str
    chunk: dict[str, int] | None = None


class NbtReadError(ValueError):
    pass


class NbtStringReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.strings: list[tuple[str, str]] = []

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, length: int) -> bytes:
        if length < 0 or self.pos + length > len(self.data):
            raise NbtReadError("unexpected end of NBT payload")
        value = self.data[self.pos : self.pos + length]
        self.pos += length
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def i16(self) -> int:
        return int.from_bytes(self.take(2), "big", signed=True)

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big", signed=False)

    def i32(self) -> int:
        return int.from_bytes(self.take(4), "big", signed=True)

    def nbt_string(self) -> str:
        length = self.u16()
        return self.take(length).decode("utf-8", errors="replace")

    def scan(self) -> list[tuple[str, str]]:
        tag_type = self.u8()
        if tag_type == 0:
            return []
        root_name = self.nbt_string()
        root_path = root_name or "root"
        self.scan_payload(tag_type, root_path)
        return self.strings

    def scan_named_payload(self, parent_path: str) -> bool:
        tag_type = self.u8()
        if tag_type == 0:
            return False
        name = self.nbt_string()
        path = f"{parent_path}.{name}" if parent_path else name
        self.scan_payload(tag_type, path)
        return True

    def scan_payload(self, tag_type: int, path: str) -> None:
        if tag_type == 1:
            self.take(1)
        elif tag_type == 2:
            self.take(2)
        elif tag_type == 3:
            self.take(4)
        elif tag_type == 4:
            self.take(8)
        elif tag_type == 5:
            self.take(4)
        elif tag_type == 6:
            self.take(8)
        elif tag_type == 7:
            self.take(self.i32())
        elif tag_type == 8:
            self.strings.append((path, self.nbt_string()))
        elif tag_type == 9:
            child_type = self.u8()
            length = self.i32()
            if length < 0:
                raise NbtReadError("negative list length")
            for index in range(length):
                self.scan_payload(child_type, f"{path}[{index}]")
        elif tag_type == 10:
            while self.scan_named_payload(path):
                pass
        elif tag_type == 11:
            self.take(self.i32() * 4)
        elif tag_type == 12:
            self.take(self.i32() * 8)
        else:
            raise NbtReadError(f"unknown NBT tag type {tag_type}")


@dataclass
class NbtTag:
    tag_type: int
    value: Any


class NbtTree:
    def __init__(self, root_type: int, root_name: str, root: NbtTag):
        self.root_type = root_type
        self.root_name = root_name
        self.root = root

    @property
    def root_path(self) -> str:
        return self.root_name or "root"


class NbtTreeReader:
    """Read an NBT tree while preserving enough structure to write it back."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, length: int) -> bytes:
        if length < 0 or self.pos + length > len(self.data):
            raise NbtReadError("unexpected end of NBT payload")
        value = self.data[self.pos : self.pos + length]
        self.pos += length
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return int.from_bytes(self.take(4), "big", signed=True)

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big", signed=False)

    def nbt_string(self) -> str:
        length = self.u16()
        return self.take(length).decode("utf-8", errors="replace")

    def read(self) -> NbtTree:
        root_type = self.u8()
        if root_type == 0:
            return NbtTree(root_type, "", NbtTag(0, None))
        root_name = self.nbt_string()
        root_path = root_name or "root"
        root = self.read_payload(root_type, root_path)
        return NbtTree(root_type, root_name, root)

    def read_payload(self, tag_type: int, path: str) -> NbtTag:
        if tag_type == 1:
            return NbtTag(tag_type, self.take(1))
        if tag_type == 2:
            return NbtTag(tag_type, self.take(2))
        if tag_type == 3:
            return NbtTag(tag_type, self.take(4))
        if tag_type == 4:
            return NbtTag(tag_type, self.take(8))
        if tag_type == 5:
            return NbtTag(tag_type, self.take(4))
        if tag_type == 6:
            return NbtTag(tag_type, self.take(8))
        if tag_type == 7:
            length = self.i32()
            return NbtTag(tag_type, self.take(length))
        if tag_type == 8:
            return NbtTag(tag_type, self.nbt_string())
        if tag_type == 9:
            child_type = self.u8()
            length = self.i32()
            if length < 0:
                raise NbtReadError("negative list length")
            items = [self.read_payload(child_type, f"{path}[{index}]") for index in range(length)]
            return NbtTag(tag_type, (child_type, items))
        if tag_type == 10:
            items: list[tuple[str, NbtTag]] = []
            while True:
                child_type = self.u8()
                if child_type == 0:
                    break
                name = self.nbt_string()
                child_path = f"{path}.{name}" if path else name
                items.append((name, self.read_payload(child_type, child_path)))
            return NbtTag(tag_type, items)
        if tag_type == 11:
            length = self.i32()
            return NbtTag(tag_type, self.take(length * 4))
        if tag_type == 12:
            length = self.i32()
            return NbtTag(tag_type, self.take(length * 8))
        raise NbtReadError(f"unknown NBT tag type {tag_type}")


def encode_nbt_string(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 65535:
        raise NbtReadError(f"NBT string is too long after patching: {len(data)} bytes")
    return len(data).to_bytes(2, "big") + data


def write_nbt_payload(tag: NbtTag) -> bytes:
    tag_type = tag.tag_type
    if tag_type in {1, 2, 3, 4, 5, 6}:
        return bytes(tag.value)
    if tag_type == 7:
        data = bytes(tag.value)
        return len(data).to_bytes(4, "big", signed=True) + data
    if tag_type == 8:
        return encode_nbt_string(str(tag.value))
    if tag_type == 9:
        child_type, items = tag.value
        payload = bytearray([child_type])
        payload.extend(len(items).to_bytes(4, "big", signed=True))
        for item in items:
            payload.extend(write_nbt_payload(item))
        return bytes(payload)
    if tag_type == 10:
        payload = bytearray()
        for name, child in tag.value:
            payload.append(child.tag_type)
            payload.extend(encode_nbt_string(name))
            payload.extend(write_nbt_payload(child))
        payload.append(0)
        return bytes(payload)
    if tag_type == 11:
        data = bytes(tag.value)
        if len(data) % 4:
            raise NbtReadError("invalid TAG_Int_Array byte length")
        return (len(data) // 4).to_bytes(4, "big", signed=True) + data
    if tag_type == 12:
        data = bytes(tag.value)
        if len(data) % 8:
            raise NbtReadError("invalid TAG_Long_Array byte length")
        return (len(data) // 8).to_bytes(4, "big", signed=True) + data
    if tag_type == 0:
        return b""
    raise NbtReadError(f"unknown NBT tag type {tag_type}")


def write_nbt_tree(tree: NbtTree) -> bytes:
    if tree.root_type == 0:
        return b"\x00"
    return bytes([tree.root_type]) + encode_nbt_string(tree.root_name) + write_nbt_payload(tree.root)


def to_posix(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def is_zip_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".zip", ".mcworld"}


def entry_names(source: Path) -> list[str]:
    source = source.resolve()
    if source.is_dir():
        return sorted(p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file())
    if is_zip_path(source):
        with zipfile.ZipFile(source) as archive:
            return sorted(name for name in archive.namelist() if not name.endswith("/"))
    raise FileNotFoundError(source)


def iter_entries(source: Path, include_nested_resources: bool = True) -> Iterable[Entry]:
    source = source.resolve()
    if source.is_dir():
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            rel = path.relative_to(source).as_posix()
            if should_read(rel):
                data = path.read_bytes()
                yield Entry(rel, data, len(data))
                if include_nested_resources and rel.endswith("resources.zip"):
                    yield from iter_nested_zip(rel, data)
            else:
                yield Entry(rel, None, path.stat().st_size)
        return

    if is_zip_path(source):
        with zipfile.ZipFile(source) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue
                rel = to_posix(info.filename)
                if should_read(rel):
                    data = archive.read(info)
                    yield Entry(rel, data, info.file_size)
                    if include_nested_resources and rel.endswith("resources.zip"):
                        yield from iter_nested_zip(rel, data)
                else:
                    yield Entry(rel, None, info.file_size)
        return

    raise FileNotFoundError(source)


def iter_nested_zip(prefix: str, data: bytes) -> Iterable[Entry]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue
                rel = f"{prefix}!{to_posix(info.filename)}"
                if should_read(rel):
                    yield Entry(rel, archive.read(info), info.file_size)
                else:
                    yield Entry(rel, None, info.file_size)
    except zipfile.BadZipFile:
        return


def should_read(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.endswith(".json")
        or lowered.endswith(".mcfunction")
        or lowered.endswith(".dat")
        or lowered.endswith(".mca")
        or lowered.endswith("resources.zip")
        or "!assets/" in lowered
    )


def inspect_source(args: argparse.Namespace) -> int:
    source = Path(args.source)
    names = [entry.path for entry in iter_entries(source)]
    lower_names = [name.lower() for name in names]

    java_markers = {
        "level_dat": any(PurePosixPath(name).name == "level.dat" for name in lower_names),
        "region_files": sum(1 for name in lower_names if "/region/" in f"/{name}" and name.endswith(".mca")),
        "entity_region_files": sum(1 for name in lower_names if "/entities/" in f"/{name}" and name.endswith(".mca")),
        "poi_region_files": sum(1 for name in lower_names if "/poi/" in f"/{name}" and name.endswith(".mca")),
        "dat_files": sum(1 for name in lower_names if name.endswith(".dat")),
        "resources_zip": sum(1 for name in lower_names if PurePosixPath(name).name == "resources.zip"),
        "lang_json": sum(1 for name in lower_names if LANG_PATH_RE.match(name)),
        "mcfunction": sum(1 for name in lower_names if name.endswith(".mcfunction")),
        "datapack_json": sum(1 for name in lower_names if "/data/" in f"/{name}" and name.endswith(".json")),
    }
    bedrock_markers = {
        "mcworld_extension": source.suffix.lower() == ".mcworld",
        "leveldb": any("/db/" in f"/{name}" for name in lower_names),
        "texts_lang": sum(1 for name in lower_names if "/texts/" in f"/{name}" and name.endswith(".lang")),
    }
    confidence = "high" if java_markers["level_dat"] else "medium" if any(java_markers.values()) else "low"
    if bedrock_markers["mcworld_extension"] or bedrock_markers["leveldb"]:
        confidence = "not-java"

    result = {
        "schema": "mc-map-java-inspect.v1",
        "source": str(source.resolve()),
        "source_kind": "directory" if source.is_dir() else "zip" if is_zip_path(source) else "unknown",
        "java_confidence": confidence,
        "java_markers": java_markers,
        "bedrock_markers": bedrock_markers,
        "entry_count": len(names),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if confidence != "not-java" else 2


def decode_text(data: bytes, path: str) -> str | None:
    if len(data) > MAX_TEXT_BYTES:
        return None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", data, 0, 1, f"cannot decode {path}")


def is_probably_internal(value: str) -> bool:
    stripped = value.strip()
    if UUID_RE.match(stripped):
        return True
    if stripped.startswith("#"):
        return True
    if stripped.startswith("minecraft:"):
        return True
    if stripped.endswith(":") and re.search(r"[A-Za-z\u0080-\uffff]", stripped):
        return False
    if " " not in stripped and (":" in stripped or "/" in stripped) and INTERNAL_ID_RE.match(stripped.lower()):
        return True
    return False


def is_player_text(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 2:
        return False
    if len(stripped) > 4000:
        return False
    if is_probably_internal(stripped):
        return False
    if not re.search(r"[A-Za-z\u0080-\uffff]", stripped):
        return False
    return True


def protected_tokens(value: str) -> list[str]:
    seen: list[str] = []
    for match in PROTECTED_TOKEN_RE.finditer(value):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    return seen


def make_unit(
    *,
    edition: str,
    source_kind: str,
    source_file: str,
    address: dict[str, Any],
    raw: str,
    mode_support: list[str],
    confidence: str,
    resource_namespace: str,
    translation_key: str = "",
    source_locale: str = "",
    context: dict[str, Any] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    unit_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
    return {
        "id": unit_id,
        "edition": edition,
        "source_kind": source_kind,
        "source_file": source_file,
        "address": address,
        "raw": raw,
        "translation": "",
        "translation_key": translation_key,
        "resource_namespace": resource_namespace,
        "source_locale": source_locale,
        "mode_support": mode_support,
        "protected": protected_tokens(raw),
        "context": context or {},
        "confidence": confidence,
        "notes": notes,
    }


def generated_key(namespace: str, map_slug: str, source_kind: str, unit_id: str) -> str:
    return ".".join(
        [
            normalize_key_piece(namespace),
            normalize_key_piece(map_slug),
            normalize_key_piece(source_kind),
            normalize_key_piece(unit_id),
        ]
    )


def scan_lang_file(entry: Entry, source_locale: str) -> list[dict[str, Any]]:
    match = LANG_PATH_RE.match(entry.path)
    if not match or entry.data is None:
        return []
    namespace, locale = match.group(1), match.group(2)
    if locale != source_locale:
        return []
    text = decode_text(entry.data, entry.path)
    if text is None:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    units: list[dict[str, Any]] = []
    for key, value in sorted(data.items()):
        if isinstance(value, str) and is_player_text(value):
            units.append(
                make_unit(
                    edition="java",
                    source_kind="lang",
                    source_file=entry.path,
                    address={"lang_key": key},
                    raw=value,
                    mode_support=["resource-pack"],
                    confidence="high",
                    translation_key=key,
                    resource_namespace=namespace,
                    source_locale=locale,
                )
            )
    return units


def iter_json_spans(text: str) -> Iterable[tuple[int, int, Any]]:
    index = 0
    while index < len(text):
        candidates = [pos for pos in (text.find("{", index), text.find("[", index)) if pos != -1]
        if not candidates:
            break
        start = min(candidates)
        stack: list[str] = []
        in_string = False
        escaped = False
        yielded = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]":
                if not stack or char != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    raw = text[start : index + 1]
                    try:
                        yield start, index + 1, json.loads(raw)
                        yielded = True
                    except json.JSONDecodeError:
                        pass
                    break
        index = (index + 1) if yielded else (start + 1)


def is_component_root(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(key in obj for key in TEXT_COMPONENT_KEYS)
    if isinstance(obj, list):
        return any(is_component_root(item) for item in obj)
    return False


def iter_component_roots(obj: Any, json_path: str = "$") -> Iterable[tuple[str, Any]]:
    if is_component_root(obj):
        yield json_path, obj
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_component_roots(value, f"{json_path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_component_roots(value, f"{json_path}[{index}]")


def collect_component_context(obj: Any, json_path: str = "$") -> dict[str, Any]:
    text_nodes: list[dict[str, str]] = []
    translate_keys: list[dict[str, str]] = []
    selector_tokens: list[str] = []
    keybinds: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str) and is_player_text(value["text"]):
                text_nodes.append({"json_path": f"{path}.text", "text": value["text"]})
            if isinstance(value.get("translate"), str) and value["translate"].strip():
                translate_keys.append({"json_path": f"{path}.translate", "key": value["translate"].strip()})
            if isinstance(value.get("selector"), str):
                selector_tokens.append(value["selector"])
            if isinstance(value.get("keybind"), str):
                keybinds.append(value["keybind"])
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(obj, json_path)
    return {
        "text_nodes": text_nodes,
        "translate_keys": translate_keys,
        "selector_tokens": sorted(set(selector_tokens)),
        "keybinds": sorted(set(keybinds)),
    }


def parse_json_path(path: str) -> list[str | int]:
    if not path or path[0] != "$":
        raise ValueError(f"unsupported JSON path: {path}")
    parts: list[str | int] = []
    index = 1
    while index < len(path):
        char = path[index]
        if char == ".":
            index += 1
            start = index
            while index < len(path) and path[index] not in ".[":
                index += 1
            if start == index:
                raise ValueError(f"empty JSON path key: {path}")
            parts.append(path[start:index])
        elif char == "[":
            end = path.find("]", index)
            if end == -1:
                raise ValueError(f"unterminated JSON path index: {path}")
            parts.append(int(path[index + 1 : end]))
            index = end + 1
        else:
            raise ValueError(f"unsupported JSON path segment in {path!r} at {index}")
    return parts


def get_json_path(obj: Any, path: str) -> Any:
    current = obj
    for part in parse_json_path(path):
        if isinstance(part, int):
            if not isinstance(current, list) or part < 0 or part >= len(current):
                raise KeyError(path)
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise KeyError(path)
            current = current[part]
    return current


def parent_json_path(path: str) -> tuple[str, str | int]:
    parts = parse_json_path(path)
    if not parts:
        raise ValueError(f"JSON path has no parent: {path}")
    parent_parts = parts[:-1]
    parent = "$"
    for part in parent_parts:
        if isinstance(part, int):
            parent += f"[{part}]"
        else:
            parent += f".{part}"
    return parent, parts[-1]


def json_text_node_for_unit(row: dict[str, Any]) -> tuple[str | None, str]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    text_nodes = context.get("text_nodes") if isinstance(context, dict) else None
    if not isinstance(text_nodes, list) or not text_nodes:
        return None, "missing_text_node_context"
    if len(text_nodes) != 1:
        return None, "multiple_text_nodes"
    node = text_nodes[0]
    if not isinstance(node, dict) or not isinstance(node.get("json_path"), str):
        return None, "invalid_text_node_context"
    node_text = str(node.get("text", ""))
    raw = str(row.get("raw", ""))
    if node_text != raw:
        return None, "text_node_raw_mismatch"
    return node["json_path"], ""


def inject_key_into_json_component(obj: Any, row: dict[str, Any]) -> tuple[bool, str]:
    key = str(row.get("translation_key", "")).strip()
    if not key:
        return False, "missing_translation_key"
    text_path, reason = json_text_node_for_unit(row)
    if not text_path:
        return False, reason
    try:
        parent_path, leaf = parent_json_path(text_path)
        parent = get_json_path(obj, parent_path)
    except (KeyError, ValueError) as exc:
        return False, f"json_path_missing:{exc}"
    if leaf != "text" or not isinstance(parent, dict):
        return False, "text_node_not_object_field"

    if parent.get("translate") == key and "text" not in parent:
        return False, "already_applied"
    if "translate" in parent and parent.get("translate") != key:
        return False, "existing_translate_conflict"
    expected = str(row.get("raw", ""))
    if parent.get("text") != expected:
        return False, "source_text_mismatch"

    updated = {"translate": key}
    for child_key, child_value in parent.items():
        if child_key != "text":
            updated[child_key] = child_value
    parent.clear()
    parent.update(updated)
    return True, "changed"


def row_segments(row: dict[str, Any]) -> list[dict[str, Any]]:
    ensure_segments(row)
    segments = row.get("segments")
    if not isinstance(segments, list):
        return []
    return [segment for segment in segments if isinstance(segment, dict)]


def replace_text_with_translate(parent: dict[str, Any], key: str) -> None:
    updated = {"translate": key}
    for child_key, child_value in parent.items():
        if child_key != "text":
            updated[child_key] = child_value
    parent.clear()
    parent.update(updated)


def inject_segments_into_json_component(obj: Any, row: dict[str, Any]) -> tuple[bool, str]:
    text_nodes = [
        node
        for node in (row.get("context", {}).get("text_nodes", []) if isinstance(row.get("context"), dict) else [])
        if isinstance(node, dict)
    ]
    if len(text_nodes) <= 1:
        return inject_key_into_json_component(obj, row)

    segments = row_segments(row)
    if len(segments) != len(text_nodes):
        return False, "segment_count_mismatch"

    text_by_path = {str(node.get("json_path", "")): str(node.get("text", "")) for node in text_nodes}
    operations: list[tuple[dict[str, Any], str]] = []
    already = 0
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()

    for segment in segments:
        text_path = str(segment.get("json_path", ""))
        expected = str(segment.get("raw", ""))
        key = str(segment.get("translation_key", "")).strip()
        if not text_path or not key:
            return False, "missing_segment_path_or_key"
        if text_path in seen_paths:
            return False, "duplicate_segment_path"
        if key in seen_keys:
            return False, "duplicate_segment_key"
        seen_paths.add(text_path)
        seen_keys.add(key)
        if text_by_path.get(text_path) != expected:
            return False, "segment_raw_mismatch"
        try:
            parent_path, leaf = parent_json_path(text_path)
            parent = get_json_path(obj, parent_path)
        except (KeyError, ValueError) as exc:
            return False, f"segment_json_path_missing:{exc}"
        if leaf != "text" or not isinstance(parent, dict):
            return False, "segment_not_object_text_field"
        if parent.get("translate") == key and "text" not in parent:
            already += 1
            continue
        if "translate" in parent and parent.get("translate") != key:
            return False, "existing_segment_translate_conflict"
        if parent.get("text") != expected:
            return False, "segment_source_text_mismatch"
        operations.append((parent, key))

    if already == len(segments):
        return False, "already_applied"
    for parent, key in operations:
        replace_text_with_translate(parent, key)
    return True, "changed"


def inject_component_for_unit(obj: Any, row: dict[str, Any], multi_text_mode: str) -> tuple[bool, str]:
    text_nodes = [
        node
        for node in (row.get("context", {}).get("text_nodes", []) if isinstance(row.get("context"), dict) else [])
        if isinstance(node, dict)
    ]
    if len(text_nodes) <= 1:
        return inject_key_into_json_component(obj, row)
    if multi_text_mode == "split-nodes":
        return inject_segments_into_json_component(obj, row)
    return False, "multiple_text_nodes"


def dump_json_component(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def text_component_units(
    obj: Any,
    *,
    source_file: str,
    source_kind: str,
    base_address: dict[str, Any],
    json_path: str,
    namespace: str,
    map_slug: str,
    confidence: str,
    notes: str,
) -> list[dict[str, Any]]:
    context = collect_component_context(obj, json_path)
    text_nodes = context["text_nodes"]
    translate_keys = context["translate_keys"]
    units: list[dict[str, Any]] = []

    if text_nodes:
        raw = "".join(node["text"] for node in text_nodes)
        if is_player_text(raw):
            address = {**base_address, "json_path": json_path}
            temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
            unit = make_unit(
                edition="java",
                source_kind=source_kind,
                source_file=source_file,
                address=address,
                raw=raw,
                mode_support=["hybrid-key-injection", "embedded-direct"],
                confidence=confidence,
                resource_namespace=namespace,
                translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
                context=context,
                notes=notes or "Hardcoded text component; resource-pack-only output requires key injection.",
            )
            ensure_segments(unit)
            units.append(unit)
        return units

    for item in translate_keys:
        key = item["key"]
        units.append(
            make_unit(
                edition="java",
                source_kind="text_component_translate",
                source_file=source_file,
                address={**base_address, "json_path": item["json_path"]},
                raw=key,
                mode_support=["resource-pack"],
                confidence="medium",
                resource_namespace=namespace,
                translation_key=key,
                context=context,
                notes="Already uses a translation key. Resolve source text from language files when available.",
            )
        )
    return units


def extract_text_components(
    obj: Any,
    *,
    source_file: str,
    source_kind: str,
    base_address: dict[str, Any],
    json_path: str,
    namespace: str,
    map_slug: str,
    confidence: str = "high",
    notes: str = "",
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for root_path, root_obj in iter_component_roots(obj, json_path):
        units.extend(
            text_component_units(
                root_obj,
                source_file=source_file,
                source_kind=source_kind,
                base_address=base_address,
                json_path=root_path,
                namespace=namespace,
                map_slug=map_slug,
                confidence=confidence,
                notes=notes,
            )
        )
    return units


def infer_command_source_kind(line: str, fallback: str = "function") -> str:
    stripped = line.strip()
    lowered = stripped.lower()
    if lowered.startswith("tellraw "):
        return "tellraw"
    if lowered.startswith("title "):
        if " actionbar " in lowered:
            return "actionbar"
        return "title"
    if lowered.startswith("bossbar "):
        return "bossbar"
    if lowered.startswith("scoreboard "):
        return "scoreboard"
    if lowered.startswith("team "):
        return "team"
    if "customname" in lowered:
        return "entity_name"
    if "lore" in lowered:
        return "item_lore"
    if "display" in lowered and "name" in lowered:
        return "item_name"
    return fallback


def scan_command_line(
    line: str,
    *,
    source_file: str,
    base_address: dict[str, Any],
    namespace: str,
    map_slug: str,
    fallback_kind: str,
    confidence: str,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    source_kind = infer_command_source_kind(line, fallback_kind)
    for start, end, obj in iter_json_spans(line):
        units.extend(
            extract_text_components(
                obj,
                source_file=source_file,
                source_kind=source_kind,
                base_address={**base_address, "command_span": [start, end]},
                json_path="$",
                namespace=namespace,
                map_slug=map_slug,
                confidence=confidence,
                notes="Command JSON text component.",
            )
        )
    return units


def scan_mcfunction(entry: Entry, namespace: str, map_slug: str) -> list[dict[str, Any]]:
    if entry.data is None:
        return []
    text = decode_text(entry.data, entry.path)
    if text is None:
        return []

    units: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        units.extend(
            scan_command_line(
                line,
                source_file=entry.path,
                base_address={"function_line": line_no},
                namespace=namespace,
                map_slug=map_slug,
                fallback_kind="function",
                confidence="high",
            )
        )
    return units


def scan_json_file(entry: Entry, namespace: str, map_slug: str) -> list[dict[str, Any]]:
    if entry.data is None or LANG_PATH_RE.match(entry.path):
        return []
    lowered = entry.path.lower()
    if not lowered.endswith(".json") or "/data/" not in f"/{lowered}":
        return []
    text = decode_text(entry.data, entry.path)
    if text is None:
        return []
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    return extract_text_components(
        obj,
        source_file=entry.path,
        source_kind="datapack_json",
        base_address={},
        json_path="$",
        namespace=namespace,
        map_slug=map_slug,
    )


def is_binary_world_data(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(".mca") or lowered.endswith(".dat")


def decompress_dat_payload(data: bytes) -> bytes:
    if len(data) > MAX_NBT_BYTES:
        raise NbtReadError(f"NBT payload too large: {len(data)} bytes")
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data


def scan_nbt_strings(data: bytes, chunk: dict[str, int] | None = None) -> list[NbtString]:
    reader = NbtStringReader(data)
    strings = reader.scan()
    return [NbtString(path=path, value=value, chunk=chunk) for path, value in strings]


def region_coords(path: str) -> tuple[int, int] | None:
    match = REGION_PATH_RE.match(path)
    if not match:
        return None
    return int(match.group(2)), int(match.group(3))


def iter_region_nbt(entry: Entry) -> tuple[list[tuple[dict[str, int], bytes]], list[str]]:
    if entry.data is None:
        return [], [f"{entry.path}: no data"]
    data = entry.data
    if len(data) < 8192:
        return [], [f"{entry.path}: region file too small"]
    coords = region_coords(entry.path)
    region_x, region_z = coords if coords else (0, 0)
    blobs: list[tuple[dict[str, int], bytes]] = []
    errors: list[str] = []

    for index in range(1024):
        loc = data[index * 4 : index * 4 + 4]
        sector_offset = int.from_bytes(loc[:3], "big")
        sector_count = loc[3]
        if sector_offset == 0 or sector_count == 0:
            continue
        offset = sector_offset * 4096
        if offset + 5 > len(data):
            errors.append(f"{entry.path}: chunk {index} points outside file")
            continue
        length = int.from_bytes(data[offset : offset + 4], "big")
        compression = data[offset + 4]
        payload = data[offset + 5 : offset + 4 + length]
        try:
            if compression == 1:
                nbt_data = gzip.decompress(payload)
            elif compression == 2:
                nbt_data = zlib.decompress(payload)
            elif compression == 3:
                nbt_data = payload
            else:
                errors.append(f"{entry.path}: unsupported chunk compression {compression} at index {index}")
                continue
        except Exception as exc:
            errors.append(f"{entry.path}: failed to decompress chunk {index}: {exc}")
            continue
        local_x = index % 32
        local_z = index // 32
        blobs.append(
            (
                {
                    "region_x": region_x,
                    "region_z": region_z,
                    "chunk_x": region_x * 32 + local_x,
                    "chunk_z": region_z * 32 + local_z,
                    "local_index": index,
                },
                nbt_data,
            )
        )
    return blobs, errors


def source_kind_from_nbt_path(path: str, value: str) -> str:
    lowered = path.lower()
    if ".command" in lowered or COMMAND_START_RE.match(value):
        return "command_block"
    if "customname" in lowered:
        return "entity_name"
    if "lore" in lowered:
        return "item_lore"
    if ".pages" in lowered or ".filteredpages" in lowered:
        return "book"
    if "front_text" in lowered or "back_text" in lowered or lowered.endswith(".text1") or lowered.endswith(".text2"):
        return "sign"
    if "bossbar" in lowered:
        return "bossbar"
    if "scoreboard" in lowered or "displayname" in lowered:
        return "scoreboard"
    if "display.name" in lowered or lowered.endswith(".name"):
        return "item_name"
    return "nbt_text"


def nbt_path_is_internal(path: str) -> bool:
    lowered = path.lower()
    if ".playerscores[" in lowered and lowered.endswith(".name"):
        return True
    if ".objectives[" in lowered and lowered.endswith(".name"):
        return True
    if ".teams[" in lowered and lowered.endswith(".name"):
        return True
    return False


def nbt_path_is_plain_text_candidate(path: str) -> bool:
    if nbt_path_is_internal(path):
        return False
    parts = [part.lower().split("[", 1)[0] for part in path.split(".")]
    return any(part in PLAIN_TEXT_PATH_HINTS for part in parts)


def scan_nbt_value(
    item: NbtString,
    *,
    source_file: str,
    namespace: str,
    map_slug: str,
) -> list[dict[str, Any]]:
    value = item.value
    if nbt_path_is_internal(item.path):
        return []
    source_kind = source_kind_from_nbt_path(item.path, value)
    base_address: dict[str, Any] = {"nbt_path": item.path}
    if item.chunk:
        base_address["chunk"] = item.chunk

    units: list[dict[str, Any]] = []
    if COMMAND_START_RE.match(value):
        units.extend(
            scan_command_line(
                value,
                source_file=source_file,
                base_address=base_address,
                namespace=namespace,
                map_slug=map_slug,
                fallback_kind=source_kind,
                confidence="medium",
            )
        )
        return units

    stripped = value.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if obj is not None:
            units.extend(
                extract_text_components(
                    obj,
                    source_file=source_file,
                    source_kind=source_kind,
                    base_address=base_address,
                    json_path="$",
                    namespace=namespace,
                    map_slug=map_slug,
                    confidence="medium",
                    notes="NBT string containing JSON text component.",
                )
            )
            return units

    if nbt_path_is_plain_text_candidate(item.path) and is_player_text(value):
        temp_id = stable_id(source_file, json.dumps(base_address, sort_keys=True), value)
        units.append(
            make_unit(
                edition="java",
                source_kind=source_kind,
                source_file=source_file,
                address=base_address,
                raw=value,
                mode_support=["embedded-direct"],
                confidence="medium",
                resource_namespace=namespace,
                translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
                notes="Plain NBT string from a player-facing path hint; direct copied-world patching is required.",
            )
        )
    return units


def scan_binary_entry(entry: Entry, namespace: str, map_slug: str) -> tuple[list[dict[str, Any]], list[str]]:
    if entry.data is None:
        return [], [f"{entry.path}: no data"]
    units: list[dict[str, Any]] = []
    errors: list[str] = []
    lowered = entry.path.lower()

    if lowered.endswith(".dat"):
        try:
            nbt_data = decompress_dat_payload(entry.data)
            for item in scan_nbt_strings(nbt_data):
                units.extend(scan_nbt_value(item, source_file=entry.path, namespace=namespace, map_slug=map_slug))
        except Exception as exc:
            errors.append(f"{entry.path}: {exc}")
        return units, errors

    if lowered.endswith(".mca"):
        blobs, region_errors = iter_region_nbt(entry)
        errors.extend(region_errors)
        for chunk, nbt_data in blobs:
            try:
                for item in scan_nbt_strings(nbt_data, chunk=chunk):
                    units.extend(scan_nbt_value(item, source_file=entry.path, namespace=namespace, map_slug=map_slug))
            except Exception as exc:
                errors.append(f"{entry.path}: chunk {chunk.get('local_index')}: {exc}")
        return units, errors

    return [], []


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def count_modes(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for mode in row.get("mode_support", []):
            counts[str(mode)] = counts.get(str(mode), 0) + 1
    return dict(sorted(counts.items()))


def top_counts(rows: list[dict[str, Any]], field: str, limit: int = 20) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field, ""))
        counts[value] = counts.get(value, 0) + 1
    return [{"value": value, "count": count} for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def source_prefix(path: str, depth: int = 5) -> str:
    return "/".join(path.split("/")[:depth])


def write_scan_review(path: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Scan Review",
        "",
        f"- Source: `{report['source']}`",
        f"- Units: {report['unit_count']}",
        f"- Scanned files: {report['scanned_files']}",
        f"- Binary units: {report['binary_unit_count']}",
        f"- Pending/failed binary files: {len(report['pending_binary_parser_coverage'])}",
        "",
        "## Counts By Kind",
        "",
    ]
    for key, count in report["counts_by_kind"].items():
        lines.append(f"- `{key}`: {count}")
    lines.extend(["", "## Top Source Prefixes", ""])
    prefix_counts: dict[str, int] = {}
    for row in rows:
        prefix = source_prefix(row["source_file"])
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    for prefix, count in sorted(prefix_counts.items(), key=lambda item: (-item[1], item[0]))[:20]:
        lines.append(f"- `{prefix}`: {count}")
    lines.extend(["", "## Most Repeated Raw Text", ""])
    raw_counts: dict[str, int] = {}
    for row in rows:
        raw = row["raw"].replace("\n", "\\n")
        raw_counts[raw] = raw_counts.get(raw, 0) + 1
    for raw, count in sorted(raw_counts.items(), key=lambda item: (-item[1], item[0]))[:20]:
        sample = raw[:160] + ("..." if len(raw) > 160 else "")
        lines.append(f"- {count}x `{sample}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan_source(args: argparse.Namespace) -> int:
    require_locale(args.target, "--target")
    require_locale(args.source_locale, "--source-locale")

    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    map_slug = normalize_key_piece(args.map_slug or source.stem or "map")
    namespace = normalize_key_piece(args.namespace)
    units: list[dict[str, Any]] = []
    pending_binary: list[str] = []
    binary_units = 0
    scanned_files = 0
    warnings: list[str] = []

    for entry in iter_entries(source):
        scanned_files += 1
        lowered = entry.path.lower()
        try:
            units.extend(scan_lang_file(entry, args.source_locale))
            if lowered.endswith(".mcfunction"):
                units.extend(scan_mcfunction(entry, namespace, map_slug))
            elif lowered.endswith(".json"):
                units.extend(scan_json_file(entry, namespace, map_slug))
            elif is_binary_world_data(entry.path):
                if args.no_binary:
                    pending_binary.append(entry.path)
                else:
                    before = len(units)
                    binary_found, binary_errors = scan_binary_entry(entry, namespace, map_slug)
                    units.extend(binary_found)
                    binary_units += len(units) - before
                    if binary_errors:
                        pending_binary.append(entry.path)
                        warnings.extend(binary_errors[: args.max_binary_errors])
        except Exception as exc:
            warnings.append(f"{entry.path}: {exc}")

    unit_path = out / "translation_units.jsonl"
    write_jsonl(unit_path, units)

    project = {
        "schema": "mc-map-translate-project.v1",
        "created_at": utc_now(),
        "source": str(source),
        "edition": "java",
        "target_locale": args.target,
        "source_locale": args.source_locale,
        "preferred_mode": args.mode,
        "map_slug": map_slug,
        "namespace": namespace,
        "notes": "Original map should remain read-only. Patch copies only.",
    }
    write_json(out / "project.json", project)

    report = {
        "schema": "mc-map-java-scan-report.v2",
        "created_at": utc_now(),
        "source": str(source),
        "scanned_files": scanned_files,
        "unit_count": len(units),
        "binary_unit_count": binary_units,
        "target_locale": args.target,
        "source_locale": args.source_locale,
        "mode": args.mode,
        "pending_binary_parser_coverage": sorted(set(pending_binary)),
        "warnings": warnings,
        "counts_by_kind": count_by(units, "source_kind"),
        "counts_by_mode": count_modes(units),
        "top_source_files": top_counts(units, "source_file"),
        "top_raw": top_counts(units, "raw"),
    }
    write_json(out / "scan_report.json", report)
    write_scan_review(out / "scan_review.md", report, units)

    glossary = out / "glossary.md"
    if not glossary.exists():
        glossary.write_text(
            "# Glossary\n\n"
            "| Source | Translation | Type | Notes |\n"
            "| --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

    if args.project_layout:
        project_args = argparse.Namespace(
            units=str(unit_path),
            out_dir=str(out),
            max_units=args.max_workpack_units,
            mode="",
            source_kind="",
            source_file_regex="",
            untranslated_only=False,
            prepare_segments=not args.no_prepare_segments,
            overwrite_translation_parts=False,
        )
        make_project_files(project_args)

    print(f"workspace: {out}")
    print(f"units: {unit_path}")
    print(f"unit_count: {len(units)}")
    print(f"binary_unit_count: {binary_units}")
    print(f"pending_binary_parser_coverage: {len(set(pending_binary))}")
    return 0


class ApplyState:
    def __init__(self, *, dry_run: bool, multi_text_mode: str):
        self.dry_run = dry_run
        self.multi_text_mode = multi_text_mode
        self.changed_units = 0
        self.already_applied = 0
        self.skipped: Counter[str] = Counter()
        self.skipped_samples: list[dict[str, str]] = []
        self.status_by_id: dict[str, str] = {}
        self.changed_files: set[str] = set()

    def row_id(self, row: dict[str, Any]) -> str:
        return str(row.get("id") or id(row))

    def mark_changed(self, row: dict[str, Any], source_file: str) -> None:
        row_id = self.row_id(row)
        if self.status_by_id.get(row_id) == "changed":
            return
        self.status_by_id[row_id] = "changed"
        self.changed_units += 1
        self.changed_files.add(source_file)

    def mark_already(self, row: dict[str, Any]) -> None:
        row_id = self.row_id(row)
        if row_id in self.status_by_id:
            return
        self.status_by_id[row_id] = "already_applied"
        self.already_applied += 1

    def mark_skip(self, row: dict[str, Any], reason: str, detail: str = "") -> None:
        row_id = self.row_id(row)
        if row_id in self.status_by_id:
            return
        self.status_by_id[row_id] = f"skipped:{reason}"
        self.skipped[reason] += 1
        if len(self.skipped_samples) < 100:
            self.skipped_samples.append(
                {
                    "id": str(row.get("id", "")),
                    "source_file": str(row.get("source_file", "")),
                    "reason": reason,
                    "detail": detail[:300],
                }
            )


def confidence_allows(row: dict[str, Any], min_confidence: str) -> bool:
    value = str(row.get("confidence", "low"))
    return CONFIDENCE_RANK.get(value, 0) >= CONFIDENCE_RANK[min_confidence]


def row_has_translation(row: dict[str, Any]) -> bool:
    if str(row.get("translation", "")).strip():
        return True
    segments = row_segments(row)
    return bool(segments) and all(str(segment.get("translation", "")).strip() for segment in segments)


def select_hybrid_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], Counter[str]]:
    selected: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    source_kinds = {part.strip() for part in args.source_kind.split(",") if part.strip()}
    unit_ids = {part.strip() for part in args.unit_id.split(",") if part.strip()}
    seen_ids: set[str] = set()

    for row in rows:
        row_id = str(row.get("id", ""))
        if row_id in seen_ids:
            skipped["duplicate_unit_id"] += 1
            continue
        seen_ids.add(row_id)
        if row.get("edition") != "java":
            skipped["non_java"] += 1
            continue
        if "hybrid-key-injection" not in row.get("mode_support", []):
            skipped["not_hybrid_key_injection"] += 1
            continue
        if not str(row.get("translation_key", "")).strip():
            skipped["missing_translation_key"] += 1
            continue
        if args.translated_only and not row_has_translation(row):
            skipped["missing_translation"] += 1
            continue
        if not confidence_allows(row, args.min_confidence):
            skipped["below_min_confidence"] += 1
            continue
        if source_kinds and str(row.get("source_kind", "")) not in source_kinds:
            skipped["filtered_source_kind"] += 1
            continue
        if unit_ids and row_id not in unit_ids:
            skipped["filtered_unit_id"] += 1
            continue
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        text_nodes = context.get("text_nodes") if isinstance(context, dict) else None
        if not isinstance(row.get("address", {}).get("json_path"), str) or not isinstance(text_nodes, list) or not text_nodes:
            skipped["not_json_text_component"] += 1
            continue
        selected.append(row)

    return selected, skipped


def select_direct_nbt_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], Counter[str]]:
    selected: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    source_kinds = {part.strip() for part in args.source_kind.split(",") if part.strip()}
    unit_ids = {part.strip() for part in args.unit_id.split(",") if part.strip()}
    seen_ids: set[str] = set()

    for row in rows:
        row_id = str(row.get("id", ""))
        if row_id in seen_ids:
            skipped["duplicate_unit_id"] += 1
            continue
        seen_ids.add(row_id)
        if row.get("edition") != "java":
            skipped["non_java"] += 1
            continue
        if "embedded-direct" not in row.get("mode_support", []):
            skipped["not_embedded_direct"] += 1
            continue
        if not confidence_allows(row, args.min_confidence):
            skipped["below_min_confidence"] += 1
            continue
        if source_kinds and str(row.get("source_kind", "")) not in source_kinds:
            skipped["filtered_source_kind"] += 1
            continue
        if unit_ids and row_id not in unit_ids:
            skipped["filtered_unit_id"] += 1
            continue

        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        if isinstance(address.get("json_path"), str) or isinstance(address.get("command_span"), list):
            skipped["not_plain_nbt_string"] += 1
            continue
        if not isinstance(address.get("nbt_path"), str) or not address.get("nbt_path"):
            skipped["missing_nbt_path"] += 1
            continue

        source_file = str(row.get("source_file", "")).lower()
        if not (source_file.endswith(".dat") or source_file.endswith(".mca")):
            skipped["unsupported_direct_file_type"] += 1
            continue

        translation = str(row.get("translation", ""))
        if not translation.strip() and not args.allow_empty_translation:
            skipped["missing_translation"] += 1
            continue
        protected = row.get("protected", [])
        if not isinstance(protected, list):
            protected = []
        missing_protected = [
            str(token)
            for token in protected
            if str(token) and str(token) not in translation
        ]
        if missing_protected:
            skipped["protected_token_missing"] += 1
            continue

        selected.append(row)

    return selected, skipped


def safe_rel_path(value: str) -> PurePosixPath | None:
    if "!" in value:
        return None
    rel = PurePosixPath(to_posix(value))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def split_eol(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def note_json_apply_result(
    row: dict[str, Any],
    source_file: str,
    changed: bool,
    reason: str,
    state: ApplyState,
) -> bool:
    if changed:
        state.mark_changed(row, source_file)
        return True
    if reason == "already_applied":
        state.mark_already(row)
    else:
        state.mark_skip(row, reason)
    return False


def patch_json_span(value: str, start: int, end: int, row: dict[str, Any], state: ApplyState) -> tuple[str, bool]:
    if start < 0 or end > len(value) or start >= end:
        state.mark_skip(row, "invalid_command_span")
        return value, False
    try:
        obj = json.loads(value[start:end])
    except json.JSONDecodeError as exc:
        state.mark_skip(row, "command_span_json_parse_failed", str(exc))
        return value, False
    changed, reason = inject_component_for_unit(obj, row, state.multi_text_mode)
    if not changed:
        if reason == "already_applied":
            state.mark_already(row)
        else:
            state.mark_skip(row, reason)
        return value, False
    state.mark_changed(row, str(row.get("source_file", "")))
    return value[:start] + dump_json_component(obj) + value[end:], True


def patch_full_json_text(value: str, row: dict[str, Any], state: ApplyState) -> tuple[str, bool]:
    prefix_len = len(value) - len(value.lstrip())
    suffix_start = len(value.rstrip())
    prefix = value[:prefix_len]
    suffix = value[suffix_start:]
    payload = value[prefix_len:suffix_start]
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        state.mark_skip(row, "json_text_parse_failed", str(exc))
        return value, False
    changed, reason = inject_component_for_unit(obj, row, state.multi_text_mode)
    if not changed:
        if reason == "already_applied":
            state.mark_already(row)
        else:
            state.mark_skip(row, reason)
        return value, False
    state.mark_changed(row, str(row.get("source_file", "")))
    return prefix + dump_json_component(obj) + suffix, True


def patch_mcfunction_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    by_line: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        line_no = row.get("address", {}).get("function_line")
        if isinstance(line_no, int) and line_no > 0:
            by_line[line_no].append(row)
        else:
            state.mark_skip(row, "missing_function_line")

    changed_file = False
    for line_no, line_rows in by_line.items():
        if line_no < 1 or line_no > len(lines):
            for row in line_rows:
                state.mark_skip(row, "function_line_missing")
            continue
        body, eol = split_eol(lines[line_no - 1])
        span_rows: list[tuple[int, int, dict[str, Any]]] = []
        for row in line_rows:
            span = row.get("address", {}).get("command_span")
            if (
                isinstance(span, list)
                and len(span) == 2
                and isinstance(span[0], int)
                and isinstance(span[1], int)
            ):
                span_rows.append((span[0], span[1], row))
            else:
                state.mark_skip(row, "missing_command_span")
        for start, end, row in sorted(span_rows, key=lambda item: item[0], reverse=True):
            body, changed = patch_json_span(body, start, end, row, state)
            if changed:
                changed_file = True
        lines[line_no - 1] = body + eol

    if changed_file and not state.dry_run:
        path.write_text("".join(lines), encoding="utf-8")
    return changed_file


def patch_json_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        for row in rows:
            state.mark_skip(row, "json_file_parse_failed", str(exc))
        return False

    changed_file = False
    for row in rows:
        changed, reason = inject_component_for_unit(obj, row, state.multi_text_mode)
        changed_file = note_json_apply_result(row, source_file, changed, reason, state) or changed_file

    if changed_file and not state.dry_run:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed_file


def patch_nbt_string_value(value: str, rows: list[dict[str, Any]], state: ApplyState) -> tuple[str, bool]:
    changed_any = False
    command_rows: list[tuple[int, int, dict[str, Any]]] = []
    full_json_rows: list[dict[str, Any]] = []
    for row in rows:
        span = row.get("address", {}).get("command_span")
        if (
            isinstance(span, list)
            and len(span) == 2
            and isinstance(span[0], int)
            and isinstance(span[1], int)
        ):
            command_rows.append((span[0], span[1], row))
        else:
            full_json_rows.append(row)

    for start, end, row in sorted(command_rows, key=lambda item: item[0], reverse=True):
        value, changed = patch_json_span(value, start, end, row, state)
        changed_any = changed or changed_any

    if command_rows and full_json_rows:
        for row in full_json_rows:
            state.mark_skip(row, "mixed_command_and_full_json_same_nbt_string")
        return value, changed_any

    for row in full_json_rows:
        value, changed = patch_full_json_text(value, row, state)
        changed_any = changed or changed_any

    return value, changed_any


def patch_nbt_tag_strings(
    tag: NbtTag,
    path: str,
    rows_by_nbt_path: dict[str, list[dict[str, Any]]],
    state: ApplyState,
) -> bool:
    changed_any = False
    if tag.tag_type == 8:
        rows = rows_by_nbt_path.get(path, [])
        if rows:
            new_value, changed = patch_nbt_string_value(str(tag.value), rows, state)
            if changed:
                tag.value = new_value
                changed_any = True
        return changed_any

    if tag.tag_type == 9:
        child_type, items = tag.value
        for index, child in enumerate(items):
            changed_any = patch_nbt_tag_strings(child, f"{path}[{index}]", rows_by_nbt_path, state) or changed_any
        tag.value = (child_type, items)
        return changed_any

    if tag.tag_type == 10:
        for name, child in tag.value:
            child_path = f"{path}.{name}" if path else name
            changed_any = patch_nbt_tag_strings(child, child_path, rows_by_nbt_path, state) or changed_any
    return changed_any


def patch_nbt_blob(data: bytes, rows: list[dict[str, Any]], state: ApplyState) -> tuple[bytes, bool]:
    tree = NbtTreeReader(data).read()
    rows_by_nbt_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        nbt_path = row.get("address", {}).get("nbt_path")
        if isinstance(nbt_path, str) and nbt_path:
            rows_by_nbt_path[nbt_path].append(row)
        else:
            state.mark_skip(row, "missing_nbt_path")

    changed = patch_nbt_tag_strings(tree.root, tree.root_path, rows_by_nbt_path, state)
    for row in rows:
        if state.row_id(row) not in state.status_by_id:
            state.mark_skip(row, "nbt_path_missing")
    if not changed:
        return data, False
    return write_nbt_tree(tree), True


def mark_direct_rows_skipped(rows: list[dict[str, Any]], state: ApplyState, reason: str, detail: str = "") -> None:
    for row in rows:
        state.mark_skip(row, reason, detail)


def mark_direct_rows_already(rows: list[dict[str, Any]], state: ApplyState) -> None:
    for row in rows:
        state.mark_already(row)


def mark_direct_rows_changed(rows: list[dict[str, Any]], state: ApplyState) -> None:
    for row in rows:
        state.mark_changed(row, str(row.get("source_file", "")))


def patch_direct_nbt_string_value(value: str, rows: list[dict[str, Any]], state: ApplyState) -> tuple[str, bool]:
    raw_values = {str(row.get("raw", "")) for row in rows}
    translations = {str(row.get("translation", "")) for row in rows}
    if len(raw_values) != 1 or len(translations) != 1:
        mark_direct_rows_skipped(rows, state, "direct_nbt_path_conflict")
        return value, False

    raw = next(iter(raw_values))
    translation = next(iter(translations))
    if len(translation.encode("utf-8")) > 65535:
        mark_direct_rows_skipped(rows, state, "translation_too_long_for_nbt_string")
        return value, False

    if value == translation:
        mark_direct_rows_already(rows, state)
        return value, False
    if value != raw:
        mark_direct_rows_skipped(rows, state, "source_text_mismatch", f"expected {raw[:120]!r}, found {value[:120]!r}")
        return value, False

    mark_direct_rows_changed(rows, state)
    return translation, True


def patch_direct_nbt_tag_strings(
    tag: NbtTag,
    path: str,
    rows_by_nbt_path: dict[str, list[dict[str, Any]]],
    state: ApplyState,
) -> bool:
    changed_any = False
    if tag.tag_type == 8:
        rows = rows_by_nbt_path.get(path, [])
        if rows:
            new_value, changed = patch_direct_nbt_string_value(str(tag.value), rows, state)
            if changed:
                tag.value = new_value
                changed_any = True
        return changed_any

    if tag.tag_type == 9:
        child_type, items = tag.value
        for index, child in enumerate(items):
            changed_any = patch_direct_nbt_tag_strings(child, f"{path}[{index}]", rows_by_nbt_path, state) or changed_any
        tag.value = (child_type, items)
        return changed_any

    if tag.tag_type == 10:
        for name, child in tag.value:
            child_path = f"{path}.{name}" if path else name
            changed_any = patch_direct_nbt_tag_strings(child, child_path, rows_by_nbt_path, state) or changed_any
    return changed_any


def patch_direct_nbt_blob(data: bytes, rows: list[dict[str, Any]], state: ApplyState) -> tuple[bytes, bool]:
    tree = NbtTreeReader(data).read()
    rows_by_nbt_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        nbt_path = row.get("address", {}).get("nbt_path")
        if isinstance(nbt_path, str) and nbt_path:
            rows_by_nbt_path[nbt_path].append(row)
        else:
            state.mark_skip(row, "missing_nbt_path")

    changed = patch_direct_nbt_tag_strings(tree.root, tree.root_path, rows_by_nbt_path, state)
    for row in rows:
        if state.row_id(row) not in state.status_by_id:
            state.mark_skip(row, "nbt_path_missing")
    if not changed:
        return data, False
    return write_nbt_tree(tree), True


def patch_dat_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    original = path.read_bytes()
    gzip_wrapped = original.startswith(b"\x1f\x8b")
    try:
        payload = decompress_dat_payload(original)
        patched, changed = patch_nbt_blob(payload, rows, state)
    except Exception as exc:
        for row in rows:
            state.mark_skip(row, "dat_patch_failed", str(exc))
        return False
    if changed and not state.dry_run:
        path.write_bytes(gzip.compress(patched) if gzip_wrapped else patched)
    if changed:
        state.changed_files.add(source_file)
    return changed


def patch_dat_file_direct(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    original = path.read_bytes()
    gzip_wrapped = original.startswith(b"\x1f\x8b")
    try:
        payload = decompress_dat_payload(original)
        patched, changed = patch_direct_nbt_blob(payload, rows, state)
    except Exception as exc:
        for row in rows:
            state.mark_skip(row, "dat_direct_patch_failed", str(exc))
        return False
    if changed and not state.dry_run:
        path.write_bytes(gzip.compress(patched) if gzip_wrapped else patched)
    if changed:
        state.changed_files.add(source_file)
    return changed


def read_region_chunk(data: bytes, index: int) -> tuple[int, int, int, bytes] | None:
    loc = data[index * 4 : index * 4 + 4]
    sector_offset = int.from_bytes(loc[:3], "big")
    sector_count = loc[3]
    if sector_offset == 0 or sector_count == 0:
        return None
    offset = sector_offset * 4096
    if offset + 5 > len(data):
        return None
    length = int.from_bytes(data[offset : offset + 4], "big")
    if length <= 0 or offset + 4 + length > len(data):
        return None
    compression = data[offset + 4]
    payload = data[offset + 5 : offset + 4 + length]
    raw_record = data[offset : offset + sector_count * 4096]
    return sector_count, compression, length, payload if len(raw_record) else payload


def decompress_region_payload(compression: int, payload: bytes) -> bytes:
    if compression == 1:
        return gzip.decompress(payload)
    if compression == 2:
        return zlib.decompress(payload)
    if compression == 3:
        return payload
    raise NbtReadError(f"unsupported chunk compression {compression}")


def compress_region_payload(compression: int, payload: bytes) -> bytes:
    if compression == 1:
        return gzip.compress(payload)
    if compression == 2:
        return zlib.compress(payload)
    if compression == 3:
        return payload
    raise NbtReadError(f"unsupported chunk compression {compression}")


def build_region_file(original: bytes, chunk_records: dict[int, bytes]) -> bytes:
    locations = bytearray(4096)
    timestamps = bytearray(original[4096:8192] if len(original) >= 8192 else b"\x00" * 4096)
    body = bytearray()
    next_sector = 2

    for index in range(1024):
        record = chunk_records.get(index)
        if not record:
            continue
        padding = (-len(record)) % 4096
        sector_count = (len(record) + padding) // 4096
        locations[index * 4 : index * 4 + 4] = next_sector.to_bytes(3, "big") + bytes([sector_count])
        body.extend(record)
        if padding:
            body.extend(b"\x00" * padding)
        next_sector += sector_count

    return bytes(locations + timestamps + body)


def patch_region_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    data = path.read_bytes()
    if len(data) < 8192:
        for row in rows:
            state.mark_skip(row, "region_file_too_small")
        return False

    rows_by_chunk: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        chunk = row.get("address", {}).get("chunk")
        local_index = chunk.get("local_index") if isinstance(chunk, dict) else None
        if isinstance(local_index, int) and 0 <= local_index < 1024:
            rows_by_chunk[local_index].append(row)
        else:
            state.mark_skip(row, "missing_region_chunk_anchor")

    chunk_records: dict[int, bytes] = {}
    changed_file = False
    for index in range(1024):
        loc = data[index * 4 : index * 4 + 4]
        sector_offset = int.from_bytes(loc[:3], "big")
        sector_count = loc[3]
        if sector_offset == 0 or sector_count == 0:
            continue
        offset = sector_offset * 4096
        if offset + 5 > len(data):
            for row in rows_by_chunk.get(index, []):
                state.mark_skip(row, "region_chunk_outside_file")
            continue
        raw_record = data[offset : offset + sector_count * 4096]
        length = int.from_bytes(raw_record[:4], "big")
        compression = raw_record[4]
        payload = raw_record[5 : 4 + length]
        chunk_rows = rows_by_chunk.get(index, [])
        if not chunk_rows:
            chunk_records[index] = raw_record[: 4 + length]
            continue
        try:
            nbt_payload = decompress_region_payload(compression, payload)
            patched_nbt, changed = patch_nbt_blob(nbt_payload, chunk_rows, state)
            if changed:
                patched_payload = compress_region_payload(compression, patched_nbt)
                record = (len(patched_payload) + 1).to_bytes(4, "big") + bytes([compression]) + patched_payload
                chunk_records[index] = record
                changed_file = True
            else:
                chunk_records[index] = raw_record[: 4 + length]
        except Exception as exc:
            chunk_records[index] = raw_record[: 4 + length]
            for row in chunk_rows:
                state.mark_skip(row, "region_chunk_patch_failed", str(exc))

    for chunk_index, chunk_rows in rows_by_chunk.items():
        loc = data[chunk_index * 4 : chunk_index * 4 + 4]
        if loc == b"\x00\x00\x00\x00":
            for row in chunk_rows:
                state.mark_skip(row, "region_chunk_missing")

    if changed_file and not state.dry_run:
        path.write_bytes(build_region_file(data, chunk_records))
    if changed_file:
        state.changed_files.add(source_file)
    return changed_file


def patch_region_file_direct(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    data = path.read_bytes()
    if len(data) < 8192:
        for row in rows:
            state.mark_skip(row, "region_file_too_small")
        return False

    rows_by_chunk: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        chunk = row.get("address", {}).get("chunk")
        local_index = chunk.get("local_index") if isinstance(chunk, dict) else None
        if isinstance(local_index, int) and 0 <= local_index < 1024:
            rows_by_chunk[local_index].append(row)
        else:
            state.mark_skip(row, "missing_region_chunk_anchor")

    chunk_records: dict[int, bytes] = {}
    changed_file = False
    for index in range(1024):
        loc = data[index * 4 : index * 4 + 4]
        sector_offset = int.from_bytes(loc[:3], "big")
        sector_count = loc[3]
        if sector_offset == 0 or sector_count == 0:
            continue
        offset = sector_offset * 4096
        if offset + 5 > len(data):
            for row in rows_by_chunk.get(index, []):
                state.mark_skip(row, "region_chunk_outside_file")
            continue
        raw_record = data[offset : offset + sector_count * 4096]
        length = int.from_bytes(raw_record[:4], "big")
        compression = raw_record[4]
        payload = raw_record[5 : 4 + length]
        chunk_rows = rows_by_chunk.get(index, [])
        if not chunk_rows:
            chunk_records[index] = raw_record[: 4 + length]
            continue
        try:
            nbt_payload = decompress_region_payload(compression, payload)
            patched_nbt, changed = patch_direct_nbt_blob(nbt_payload, chunk_rows, state)
            if changed:
                patched_payload = compress_region_payload(compression, patched_nbt)
                record = (len(patched_payload) + 1).to_bytes(4, "big") + bytes([compression]) + patched_payload
                chunk_records[index] = record
                changed_file = True
            else:
                chunk_records[index] = raw_record[: 4 + length]
        except Exception as exc:
            chunk_records[index] = raw_record[: 4 + length]
            for row in chunk_rows:
                state.mark_skip(row, "region_chunk_direct_patch_failed", str(exc))

    for chunk_index, chunk_rows in rows_by_chunk.items():
        loc = data[chunk_index * 4 : chunk_index * 4 + 4]
        if loc == b"\x00\x00\x00\x00":
            for row in chunk_rows:
                state.mark_skip(row, "region_chunk_missing")

    if changed_file and not state.dry_run:
        path.write_bytes(build_region_file(data, chunk_records))
    if changed_file:
        state.changed_files.add(source_file)
    return changed_file


def copy_source_to_workdir(source: Path, workdir: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, workdir)
        return
    if is_zip_path(source):
        workdir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            root = workdir.resolve()
            for member in archive.infolist():
                target = (workdir / member.filename).resolve()
                if not is_relative_to(target, root):
                    raise ValueError(f"zip entry escapes output directory: {member.filename}")
            archive.extractall(workdir)
        return
    raise FileNotFoundError(source)


def zip_any_dir(src: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(src).as_posix())


def default_apply_report_path(out: Path, is_zip_output: bool) -> Path:
    if is_zip_output:
        return out.with_suffix(out.suffix + ".mcmap_hybrid_apply_report.json")
    return out / "mcmap_hybrid_apply_report.json"


def default_direct_apply_report_path(out: Path, is_zip_output: bool) -> Path:
    if is_zip_output:
        return out.with_suffix(out.suffix + ".mcmap_direct_nbt_apply_report.json")
    return out / "mcmap_direct_nbt_apply_report.json"


def world_file_path(root: Path, source_file: str) -> Path | None:
    rel = safe_rel_path(source_file)
    if rel is None:
        return None
    return root.joinpath(*rel.parts)


def patch_world_copy(root: Path, rows: list[dict[str, Any]], state: ApplyState) -> None:
    rows_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source_file = str(row.get("source_file", ""))
        if safe_rel_path(source_file) is None:
            state.mark_skip(row, "unsupported_nested_or_unsafe_source_path")
            continue
        rows_by_file[source_file].append(row)

    for source_file, file_rows in sorted(rows_by_file.items()):
        path = world_file_path(root, source_file)
        if path is None or not path.exists():
            for row in file_rows:
                state.mark_skip(row, "source_file_missing_in_copy")
            continue
        lowered = source_file.lower()
        if lowered.endswith(".mcfunction"):
            patch_mcfunction_file(path, source_file, file_rows, state)
        elif lowered.endswith(".json"):
            patch_json_file(path, source_file, file_rows, state)
        elif lowered.endswith(".dat"):
            patch_dat_file(path, source_file, file_rows, state)
        elif lowered.endswith(".mca"):
            patch_region_file(path, source_file, file_rows, state)
        else:
            for row in file_rows:
                state.mark_skip(row, "unsupported_apply_file_type")


def patch_direct_nbt_world_copy(root: Path, rows: list[dict[str, Any]], state: ApplyState) -> None:
    rows_by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        source_file = str(row.get("source_file", ""))
        if safe_rel_path(source_file) is None:
            state.mark_skip(row, "unsupported_nested_or_unsafe_source_path")
            continue
        rows_by_file[source_file].append(row)

    for source_file, file_rows in sorted(rows_by_file.items()):
        path = world_file_path(root, source_file)
        if path is None or not path.exists():
            for row in file_rows:
                state.mark_skip(row, "source_file_missing_in_copy")
            continue
        lowered = source_file.lower()
        if lowered.endswith(".dat"):
            patch_dat_file_direct(path, source_file, file_rows, state)
        elif lowered.endswith(".mca"):
            patch_region_file_direct(path, source_file, file_rows, state)
        else:
            for row in file_rows:
                state.mark_skip(row, "unsupported_direct_apply_file_type")


def apply_hybrid_keys(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    translations = Path(args.translations).resolve()
    is_zip_output = out.suffix.lower() == ".zip"

    if source.is_dir():
        if out == source or is_relative_to(out, source):
            raise ValueError("--out must not be the original world or inside it")
    if out.exists():
        if not args.force:
            raise FileExistsError(f"output already exists; pass --force to replace: {out}")
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()

    rows, selection_skipped = select_hybrid_rows(read_jsonl(translations), args)
    state = ApplyState(dry_run=args.dry_run, multi_text_mode=args.multi_text_mode)

    if args.report:
        report_path = Path(args.report).resolve()
    elif args.dry_run and not is_zip_output:
        report_path = out.with_name(out.name + ".mcmap_hybrid_apply_report.json")
    else:
        report_path = default_apply_report_path(out, is_zip_output)

    def run_on_copy(workdir: Path) -> None:
        copy_source_to_workdir(source, workdir)
        patch_world_copy(workdir, rows, state)
        if args.resource_pack:
            pack = Path(args.resource_pack).resolve()
            if not state.dry_run:
                zip_dir(pack, workdir / "resources.zip")

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="mcmap-apply-") as tmp:
            run_on_copy(Path(tmp) / "world")
    elif is_zip_output:
        with tempfile.TemporaryDirectory(prefix="mcmap-apply-") as tmp:
            workdir = Path(tmp) / "world"
            run_on_copy(workdir)
            zip_any_dir(workdir, out)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        run_on_copy(out)

    for row in rows:
        if state.row_id(row) not in state.status_by_id:
            state.mark_skip(row, "not_processed")

    report = {
        "schema": "mc-map-translate-hybrid-apply-report.v1",
        "created_at": utc_now(),
        "source": str(source),
        "output": str(out),
        "translations_file": str(translations),
        "dry_run": args.dry_run,
        "multi_text_mode": args.multi_text_mode,
        "selected_units": len(rows),
        "selection_skipped": dict(sorted(selection_skipped.items())),
        "changed_units": state.changed_units,
        "already_applied_units": state.already_applied,
        "changed_files": sorted(state.changed_files),
        "changed_file_count": len(state.changed_files),
        "skipped": dict(sorted(state.skipped.items())),
        "skipped_samples": state.skipped_samples,
        "resource_pack_embedded": bool(args.resource_pack and not args.dry_run),
    }
    write_json(report_path, report)

    print(f"copied_world: {out}")
    print(f"apply_report: {report_path}")
    print(f"selected_units: {len(rows)}")
    print(f"changed_units: {state.changed_units}")
    print(f"changed_files: {len(state.changed_files)}")
    print(f"skipped_units: {sum(state.skipped.values())}")
    return 0 if state.changed_units or args.allow_no_changes else 3


def apply_direct_nbt_strings(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    translations = Path(args.translations).resolve()
    is_zip_output = out.suffix.lower() == ".zip"

    if source.is_dir():
        if out == source or is_relative_to(out, source):
            raise ValueError("--out must not be the original world or inside it")
    if out.exists():
        if not args.force:
            raise FileExistsError(f"output already exists; pass --force to replace: {out}")
        if out.is_dir():
            shutil.rmtree(out)
        else:
            out.unlink()

    rows, selection_skipped = select_direct_nbt_rows(read_jsonl(translations), args)
    state = ApplyState(dry_run=args.dry_run, multi_text_mode="skip")

    if args.report:
        report_path = Path(args.report).resolve()
    elif args.dry_run and not is_zip_output:
        report_path = out.with_name(out.name + ".mcmap_direct_nbt_apply_report.json")
    else:
        report_path = default_direct_apply_report_path(out, is_zip_output)

    def run_on_copy(workdir: Path) -> None:
        copy_source_to_workdir(source, workdir)
        patch_direct_nbt_world_copy(workdir, rows, state)

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="mcmap-direct-") as tmp:
            run_on_copy(Path(tmp) / "world")
    elif is_zip_output:
        with tempfile.TemporaryDirectory(prefix="mcmap-direct-") as tmp:
            workdir = Path(tmp) / "world"
            run_on_copy(workdir)
            zip_any_dir(workdir, out)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        run_on_copy(out)

    for row in rows:
        if state.row_id(row) not in state.status_by_id:
            state.mark_skip(row, "not_processed")

    report = {
        "schema": "mc-map-translate-direct-nbt-apply-report.v1",
        "created_at": utc_now(),
        "source": str(source),
        "output": str(out),
        "translations_file": str(translations),
        "dry_run": args.dry_run,
        "selected_units": len(rows),
        "selection_skipped": dict(sorted(selection_skipped.items())),
        "changed_units": state.changed_units,
        "already_applied_units": state.already_applied,
        "changed_files": sorted(state.changed_files),
        "changed_file_count": len(state.changed_files),
        "skipped": dict(sorted(state.skipped.items())),
        "skipped_samples": state.skipped_samples,
        "risk": "embedded-direct plain NBT string replacement; source text must match exactly and original source is never edited",
    }
    write_json(report_path, report)

    print(f"copied_world: {out}")
    print(f"apply_report: {report_path}")
    print(f"selected_units: {len(rows)}")
    print(f"changed_units: {state.changed_units}")
    print(f"changed_files: {len(state.changed_files)}")
    print(f"skipped_units: {sum(state.skipped.values())}")
    return 0 if state.changed_units or args.allow_no_changes else 3


def zip_dir(src: Path, out: Path) -> None:
    if not (src / "pack.mcmeta").exists():
        raise ValueError(f"resource pack root is missing pack.mcmeta: {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(src).as_posix())


def zip_resource_pack(args: argparse.Namespace) -> int:
    src = Path(args.resource_pack).resolve()
    out = Path(args.out).resolve()
    zip_dir(src, out)
    print(f"zip: {out}")
    return 0


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def embed_resource_pack(args: argparse.Namespace) -> int:
    world = Path(args.world).resolve()
    pack = Path(args.resource_pack).resolve()
    out = Path(args.out).resolve()

    if not world.is_dir():
        raise ValueError(f"world must be a directory: {world}")
    if out == world:
        raise ValueError("--out must be a copied world path, not the original world")
    if is_relative_to(out, world):
        raise ValueError("--out must not be inside the original world directory")
    if out.exists():
        if not args.force:
            raise FileExistsError(f"output already exists; pass --force to replace: {out}")
        shutil.rmtree(out)

    shutil.copytree(world, out)
    zip_dir(pack, out / "resources.zip")
    print(f"copied_world: {out}")
    print(f"embedded_resource_pack: {out / 'resources.zip'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Java Minecraft map localization tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="inspect Java map/package markers")
    inspect.add_argument("source", help="Java world directory or map zip")
    inspect.set_defaults(func=inspect_source)

    scan = subparsers.add_parser("scan", help="scan Java text sources into translation_units.jsonl")
    scan.add_argument("source", help="Java world directory or map zip")
    scan.add_argument("--out", required=True, help="localization work directory")
    scan.add_argument("--target", required=True, help="target Java locale, for example ja_jp or fr_fr")
    scan.add_argument("--source-locale", default="en_us", help="source locale to scan from language JSON")
    scan.add_argument("--map-slug", default="", help="stable map slug for generated translation keys")
    scan.add_argument("--namespace", default="mcmap", help="namespace for generated translation keys/resource files")
    scan.add_argument("--mode", choices=["resource-pack", "hybrid-key-injection", "embedded-direct"], default="resource-pack")
    scan.add_argument("--no-binary", action="store_true", help="skip .dat/.mca NBT scanning and report them as pending")
    scan.add_argument("--max-binary-errors", type=int, default=50, help="maximum binary parser warnings to keep in scan_report.json")
    scan.add_argument("--project-layout", action="store_true", help="also create indexed multi-file project layout for staged AI translation")
    scan.add_argument("--max-workpack-units", type=int, default=120, help="maximum units per contextual workpack when --project-layout is used")
    scan.add_argument("--no-prepare-segments", action="store_true", help="do not scaffold segments[] when --project-layout is used")
    scan.set_defaults(func=scan_source)

    apply = subparsers.add_parser("apply-hybrid-keys", help="patch a copied Java world so hardcoded JSON text components use translation keys")
    apply.add_argument("source", help="original Java world directory or map zip")
    apply.add_argument("--translations", required=True, help="translation_units.jsonl or translations.jsonl containing hybrid units")
    apply.add_argument("--out", required=True, help="copied world output directory or .zip")
    apply.add_argument("--resource-pack", default="", help="optional resource-pack directory to embed as resources.zip in the copied world")
    apply.add_argument(
        "--multi-text-mode",
        choices=["split-nodes", "skip"],
        default="split-nodes",
        help="how to handle grouped components with multiple hardcoded text nodes",
    )
    apply.add_argument("--min-confidence", choices=["low", "medium", "high"], default="medium", help="minimum scanner confidence to apply")
    apply.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    apply.add_argument("--unit-id", default="", help="comma-separated unit id filter")
    apply.add_argument("--translated-only", action="store_true", help="only inject keys for units with a non-empty translation")
    apply.add_argument("--dry-run", action="store_true", help="copy to a temporary directory and report what would change")
    apply.add_argument("--report", default="", help="custom apply report JSON path")
    apply.add_argument("--allow-no-changes", action="store_true", help="return success even when no units changed")
    apply.add_argument("--force", action="store_true", help="replace an existing output copy")
    apply.set_defaults(func=apply_hybrid_keys)

    direct = subparsers.add_parser("apply-direct-nbt-strings", help="patch copied Java world plain NBT strings with translated text")
    direct.add_argument("source", help="original Java world directory or map zip")
    direct.add_argument("--translations", required=True, help="translation_units.jsonl or translations.jsonl containing embedded-direct plain NBT units")
    direct.add_argument("--out", required=True, help="copied world output directory or .zip")
    direct.add_argument("--min-confidence", choices=["low", "medium", "high"], default="medium", help="minimum scanner confidence to apply")
    direct.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    direct.add_argument("--unit-id", default="", help="comma-separated unit id filter")
    direct.add_argument("--allow-empty-translation", action="store_true", help="allow empty translations to replace source strings")
    direct.add_argument("--dry-run", action="store_true", help="copy to a temporary directory and report what would change")
    direct.add_argument("--report", default="", help="custom apply report JSON path")
    direct.add_argument("--allow-no-changes", action="store_true", help="return success even when no units changed")
    direct.add_argument("--force", action="store_true", help="replace an existing output copy")
    direct.set_defaults(func=apply_direct_nbt_strings)

    zip_pack = subparsers.add_parser("zip-resource-pack", help="zip a resource-pack directory")
    zip_pack.add_argument("resource_pack", help="resource pack directory with pack.mcmeta at root")
    zip_pack.add_argument("--out", required=True, help="output zip path")
    zip_pack.set_defaults(func=zip_resource_pack)

    embed = subparsers.add_parser("embed-resource-pack", help="copy a Java world and add resources.zip")
    embed.add_argument("world", help="original Java world directory")
    embed.add_argument("--resource-pack", required=True, help="resource pack directory")
    embed.add_argument("--out", required=True, help="copied world output directory")
    embed.add_argument("--force", action="store_true", help="replace existing output directory")
    embed.set_defaults(func=embed_resource_pack)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
