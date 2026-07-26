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
import struct
import sys
import tempfile
import zipfile
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from mcmap_contract import ensure_segments, identity_consistency_report, make_project_files, normalize_key_piece, print_blocking_errors, read_jsonl, require_locale, row_translation_complete, stable_id, translation_item_complete, unit_encoding_errors, utc_now, write_json


LANG_PATH_RE = re.compile(r"(?:^|.*[!/])assets/([^/]+)/lang/([a-z]{2,3}_[a-z0-9]{2,8})\.json$")
REGION_PATH_RE = re.compile(r"(?:^|.*/)(region|entities|poi)/r\.(-?\d+)\.(-?\d+)\.mca$")
DATAPACK_FUNCTION_RE = re.compile(r"(?:^|.*/)datapacks/[^/]+/data/([^/]+)/functions?/(.+)\.mcfunction$", re.I)
PROTECTED_TOKEN_RE = re.compile(
    r"(@[pares](?:\[[^\]]+\])?)"
    r"|(%(?:\d+\$)?[sdif])"
    r"|(\$\{[^}]+\})"
    r"|(\$\([^)]+\))"
    r"|(\\(?:[nrtbf\"'/\\]|u[0-9A-Fa-f]{4}))"
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
    "custom_name",
    "minecraft:custom_name",
    "displayname",
    "levelname",
    "title",
    "subtitle",
    "description",
    "author",
    "filtered_title",
}
COMMAND_START_RE = re.compile(
    r"^\s*/?\s*(tellraw|title|bossbar|scoreboard|team|summon|data|item|loot|give|clear|setblock|execute|say|tell|msg|w|function)\b",
    re.I,
)
SIGN_NEW_TEXT_RE = re.compile(r"^(?P<base>.*\.(?:front_text|back_text)\.messages)\[(?P<index>[0-3])\]$", re.I)
SIGN_OLD_TEXT_RE = re.compile(r"^(?P<base>.*)\.Text(?P<index>[1-4])$", re.I)
ENGLISH_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z']{2,}\b")
RESOURCE_PNG_RE = re.compile(r"(?:^|[!/])assets/[^/]+/.+\.png$", re.I)
RESOURCE_FONT_JSON_RE = re.compile(r"(?:^|[!/])assets/[^/]+/font/.+\.json$", re.I)
RESOURCE_MODEL_JSON_RE = re.compile(r"(?:^|[!/])assets/[^/]+/models/.+\.json$", re.I)
VISUAL_TEXT_PATH_HINT_RE = re.compile(
    r"(?:^|[/_.-])(text|font|title|subtitle|logo|sign|poster|banner|menu|gui|ui|tutorial|instruction|rule|dialog|quest|book|letter|label|notice)(?:$|[/_.-])",
    re.I,
)
SNBT_TEXT_KEYS = {
    "customname",
    "custom_name",
    "minecraft:custom_name",
    "minecraft:lore",
    "minecraft:written_book_content",
    "minecraft:written_book_content.pages",
    "display.name",
    "lore",
    "pages",
    "filtered_pages",
    "filteredpages",
    "written_book_content.pages",
    "written_book_content",
    "messages",
    "front_text.messages",
    "back_text.messages",
    "text",
    "title",
    "subtitle",
    "description",
    "dialogue",
    "dialogues",
    "message",
    "menu",
    "menus",
    "quest",
    "quests",
    "task",
    "tasks",
    "hint",
    "hints",
    "label",
    "labels",
    "body",
    "content",
    "line",
    "lines",
    "speaker",
}
JSON_TEXT_PATH_HINTS = {
    "title",
    "subtitle",
    "description",
    "name",
    "display_name",
    "custom_name",
    "text",
    "message",
    "messages",
    "label",
    "labels",
    "lore",
    "pages",
    "page",
    "author",
    "body",
    "content",
    "dialogue",
    "dialogues",
    "quest",
    "quests",
    "task",
    "tasks",
    "hint",
    "hints",
}
CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
IDENTITY_COUPLED_SOURCE_KINDS = {"item_name", "item_lore"}
IDENTITY_PRODUCER_COMMANDS = {"give", "loot", "item", "summon", "setblock"}
IDENTITY_CONSUMER_COMMANDS = {"clear", "execute_if_items"}


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
    block_pos: dict[str, int] | None = None
    item_identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class StringLiteralSpan:
    start: int
    end: int
    quote: str
    decoded: str


@dataclass
class SnbtNode:
    kind: str
    value: Any
    start: int
    end: int
    scalar_type: str = ""


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
        start = self.pos
        data = self.take(length)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NbtReadError(f"invalid UTF-8 in NBT string at byte {start + exc.start}") from exc

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
        start = self.pos
        data = self.take(length)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NbtReadError(f"invalid UTF-8 in NBT string at byte {start + exc.start}") from exc

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
    if " " not in stripped and (":" in stripped or "/" in stripped or "." in stripped) and INTERNAL_ID_RE.match(stripped.lower()):
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
    for token in ("\r\n", "\n", "\r"):
        if token in value and token not in seen:
            seen.append(token)
    return seen


def path_parts(path: str) -> list[str]:
    return [part.lower().split("[", 1)[0] for part in path.split(".")]


def nbt_path_leaf(path: str) -> str:
    return path_parts(path)[-1] if path_parts(path) else ""


def nbt_path_is_last_output(path: str) -> bool:
    return nbt_path_leaf(path) == "lastoutput"


def normalize_command_line(line: str) -> str:
    return effective_command_text(line)[0]


def strip_command_prefix_at(line: str, offset: int = 0) -> tuple[str, int]:
    cursor = offset
    while cursor < len(line) and line[cursor].isspace():
        cursor += 1
    if cursor < len(line) and line[cursor] == "$":
        cursor += 1
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
    if cursor < len(line) and line[cursor] == "/":
        cursor += 1
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
    return line[cursor:], cursor


def word_spans_outside_strings(text: str, word: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    target = word.lower()
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if lowered.startswith(target, index):
            before = text[index - 1] if index > 0 else ""
            after_index = index + len(target)
            after = text[after_index] if after_index < len(text) else ""
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                end = after_index
                while end < len(text) and text[end].isspace():
                    end += 1
                spans.append((index, end))
                index = end
                continue
        index += 1
    return spans


def effective_command_text(line: str) -> tuple[str, int]:
    text, offset = strip_command_prefix_at(line)
    lowered = text.lower()
    while lowered.startswith("execute "):
        matches = word_spans_outside_strings(text, "run")
        if not matches:
            break
        _match_start, match_end = matches[-1]
        offset += match_end
        text = text[match_end:]
        text, extra = strip_command_prefix_at(text)
        offset += extra
        lowered = text.lower()
    return text.strip(), offset + (len(text) - len(text.lstrip()))


def command_word(line: str) -> str:
    stripped = normalize_command_line(line)
    if not stripped:
        return ""
    return stripped.split(None, 1)[0].lower()


def execute_run_tail(line: str) -> str:
    command, _offset = effective_command_text(line)
    return command if command.lower() != strip_command_prefix_at(line)[0].strip().lower() else ""


def is_macro_function_line(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("$")


def split_command_arguments(command: str) -> tuple[str, str]:
    stripped = command.strip()
    if not stripped:
        return "", ""
    parts = stripped.split(None, 1)
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def function_call_target(line: str) -> str:
    command, _offset = effective_command_text(line)
    word, rest = split_command_arguments(command)
    if word != "function":
        return ""
    return rest.split(None, 1)[0] if rest.strip() else ""


def function_id_from_path(path: str) -> str:
    normalized = to_posix(path)
    match = DATAPACK_FUNCTION_RE.match(normalized)
    if not match:
        return ""
    namespace = match.group(1)
    function_path = match.group(2)
    return f"{namespace}:{function_path}"


def decode_snbt_single_quoted(value: str) -> str:
    result: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            if char in {"'", "\\", '"'}:
                result.append(char)
            else:
                result.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def iter_quoted_string_literals(text: str) -> Iterable[StringLiteralSpan]:
    index = 0
    while index < len(text):
        quote = text[index]
        if quote not in {"'", '"'}:
            index += 1
            continue
        escaped = False
        cursor = index + 1
        while cursor < len(text):
            char = text[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                raw = text[index + 1 : cursor]
                try:
                    decoded = json.loads(text[index : cursor + 1]) if quote == '"' else decode_snbt_single_quoted(raw)
                except json.JSONDecodeError:
                    decoded = raw
                yield StringLiteralSpan(start=index, end=cursor + 1, quote=quote, decoded=str(decoded))
                index = cursor + 1
                break
            cursor += 1
        else:
            index += 1


def encode_snbt_string_literal(value: str, quote: str) -> str:
    if quote == '"':
        return json.dumps(value, ensure_ascii=False)
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"


class SnbtParser:
    """Small span-preserving SNBT parser for item identity analysis.

    It intentionally covers compounds, lists, typed arrays, quoted strings,
    and scalar values used by Java commands. Translation apply still relies on
    the scanner's exact string anchors rather than rewriting this syntax tree.
    """

    def __init__(self, text: str, start: int = 0):
        self.text = text
        self.pos = start

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def parse(self) -> SnbtNode:
        self.skip_ws()
        return self.parse_value()

    def parse_value(self) -> SnbtNode:
        self.skip_ws()
        if self.pos >= len(self.text):
            raise ValueError("missing SNBT value")
        char = self.text[self.pos]
        if char == "{":
            return self.parse_compound()
        if char == "[":
            return self.parse_list()
        if char in {"'", '"'}:
            return self.parse_quoted()
        return self.parse_bare()

    def parse_quoted(self) -> SnbtNode:
        start = self.pos
        quote = self.text[self.pos]
        self.pos += 1
        escaped = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                self.pos += 1
                token = self.text[start:self.pos]
                if quote == '"':
                    try:
                        value = json.loads(token)
                    except json.JSONDecodeError:
                        value = token[1:-1]
                else:
                    value = decode_snbt_single_quoted(token[1:-1])
                return SnbtNode("scalar", str(value), start, self.pos, "string")
            self.pos += 1
        raise ValueError("unterminated SNBT string")

    def parse_bare(self) -> SnbtNode:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in ",]}= \t\r\n":
            self.pos += 1
        if self.pos == start:
            raise ValueError("empty SNBT scalar")
        token = self.text[start:self.pos]
        lowered = token.lower()
        numeric = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)([bsldf]?)", lowered)
        if numeric:
            suffix = numeric.group(2)
            type_name = {"b": "byte", "s": "short", "l": "long", "f": "float", "d": "double"}.get(
                suffix, "double" if any(char in numeric.group(1) for char in ".e") else "int"
            )
            number: int | float
            number = float(numeric.group(1)) if type_name in {"float", "double"} else int(numeric.group(1))
            return SnbtNode("scalar", number, start, self.pos, type_name)
        if lowered in {"true", "false"}:
            return SnbtNode("scalar", 1 if lowered == "true" else 0, start, self.pos, "byte")
        return SnbtNode("scalar", token, start, self.pos, "string")

    def parse_key(self, separator: str) -> str:
        self.skip_ws()
        if self.pos >= len(self.text):
            raise ValueError("missing SNBT key")
        if self.text[self.pos] in {"'", '"'}:
            node = self.parse_quoted()
            return str(node.value)
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] != separator:
            if self.text[self.pos] in "{},[] \t\r\n":
                break
            self.pos += 1
        key = self.text[start:self.pos].strip()
        if not key:
            raise ValueError("empty SNBT key")
        return key

    def parse_compound(self) -> SnbtNode:
        start = self.pos
        self.pos += 1
        items: list[tuple[str, SnbtNode]] = []
        self.skip_ws()
        while self.pos < len(self.text) and self.text[self.pos] != "}":
            key = self.parse_key(":")
            self.skip_ws()
            if self.pos >= len(self.text) or self.text[self.pos] != ":":
                raise ValueError("missing ':' after SNBT key")
            self.pos += 1
            items.append((key, self.parse_value()))
            self.skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                self.skip_ws()
                continue
            break
        if self.pos >= len(self.text) or self.text[self.pos] != "}":
            raise ValueError("unterminated SNBT compound")
        self.pos += 1
        return SnbtNode("compound", items, start, self.pos)

    def parse_list(self) -> SnbtNode:
        start = self.pos
        self.pos += 1
        self.skip_ws()
        array_type = ""
        if self.pos + 1 < len(self.text) and self.text[self.pos].upper() in {"B", "I", "L"} and self.text[self.pos + 1] == ";":
            array_type = {"B": "byte_array", "I": "int_array", "L": "long_array"}[self.text[self.pos].upper()]
            self.pos += 2
            self.skip_ws()
        values: list[SnbtNode] = []
        while self.pos < len(self.text) and self.text[self.pos] != "]":
            values.append(self.parse_value())
            self.skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                self.skip_ws()
                continue
            break
        if self.pos >= len(self.text) or self.text[self.pos] != "]":
            raise ValueError("unterminated SNBT list")
        self.pos += 1
        return SnbtNode("list", values, start, self.pos, array_type)

    def parse_assignment_list(self) -> SnbtNode:
        start = self.pos
        if self.pos >= len(self.text) or self.text[self.pos] != "[":
            raise ValueError("missing item component list")
        self.pos += 1
        items: list[tuple[str, SnbtNode]] = []
        self.skip_ws()
        while self.pos < len(self.text) and self.text[self.pos] != "]":
            key = self.parse_key("=")
            self.skip_ws()
            if self.pos >= len(self.text) or self.text[self.pos] != "=":
                raise ValueError("missing '=' after item component key")
            self.pos += 1
            items.append((key, self.parse_value()))
            self.skip_ws()
            if self.pos < len(self.text) and self.text[self.pos] == ",":
                self.pos += 1
                self.skip_ws()
                continue
            break
        if self.pos >= len(self.text) or self.text[self.pos] != "]":
            raise ValueError("unterminated item component list")
        self.pos += 1
        return SnbtNode("compound", items, start, self.pos)


def snbt_compound_names(node: SnbtNode) -> set[str]:
    if node.kind != "compound":
        return set()
    return {str(name).lower() for name, _child in node.value}


def collect_snbt_slot_spans(
    node: SnbtNode,
    path: tuple[str | int, ...] = (),
    out: dict[str, list[tuple[int, int]]] | None = None,
) -> dict[str, list[tuple[int, int]]]:
    result = out if out is not None else defaultdict(list)
    if node.kind == "compound":
        for name, child in node.value:
            collect_snbt_slot_spans(child, (*path, str(name)), result)
    elif node.kind == "list":
        for index, child in enumerate(node.value):
            collect_snbt_slot_spans(child, (*path, index), result)
    elif node.scalar_type == "string":
        slot = identity_text_slot(path)
        if slot:
            result[slot].append((node.start, node.end))
    return dict(result)


def snbt_item_descriptors(
    node: SnbtNode,
    *,
    path: str,
    default_role: str,
    confidence: str,
) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []

    def visit(current: SnbtNode, current_path: str) -> None:
        if current.kind == "compound":
            names = snbt_compound_names(current)
            path_role = identity_role_from_path(current_path)
            looks_like_item = path_role != "item_component" or (
                "id" in names and bool(names.intersection({"count", "components", "tag", "slot"}))
            )
            if looks_like_item:
                role = (
                    default_role
                    if default_role == "predicate"
                    else path_role
                    if path_role != "item_component"
                    else default_role
                )
                metadata = make_item_identity_metadata(
                    snbt_node_identity_value(current),
                    item_root=current_path,
                    role=role,
                    confidence=confidence,
                    span=(current.start, current.end),
                    slot_spans=collect_snbt_slot_spans(current),
                )
                if metadata is not None:
                    descriptors.append(metadata)
            for name, child in current.value:
                visit(child, f"{current_path}.{name}")
        elif current.kind == "list":
            for index, child in enumerate(current.value):
                visit(child, f"{current_path}[{index}]")

    visit(node, path)
    return descriptors


def top_level_token_spans(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    limit = len(text) if end is None else min(end, len(text))
    spans: list[tuple[int, int]] = []
    token_start: int | None = None
    stack: list[str] = []
    quote = ""
    escaped = False
    pairs = {"{": "}", "[": "]", "(": ")"}
    index = start
    while index < limit:
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
            if token_start is None:
                token_start = index
        elif char in pairs:
            stack.append(pairs[char])
            if token_start is None:
                token_start = index
        elif stack and char == stack[-1]:
            stack.pop()
        elif char.isspace() and not stack:
            if token_start is not None:
                spans.append((token_start, index))
                token_start = None
        elif token_start is None:
            token_start = index
        index += 1
    if token_start is not None:
        spans.append((token_start, limit))
    return spans


def parse_command_item_stack(
    text: str,
    start: int,
    end: int,
    *,
    role: str,
    root_name: str,
) -> dict[str, Any] | None:
    token = text[start:end]
    match = re.match(r"#?(?:[a-z0-9_.-]+:)?[a-z0-9_./-]+", token, re.I)
    if not match:
        return None
    item_id = match.group(0)
    cursor = start + match.end()
    id_node = SnbtNode("scalar", item_id, start, start + match.end(), "string")
    children: list[tuple[str, SnbtNode]] = [("id", id_node)]
    try:
        if cursor < end and text[cursor] == "[":
            parser = SnbtParser(text, cursor)
            components = parser.parse_assignment_list()
            if parser.pos > end:
                return None
            children.append(("components", components))
        elif cursor < end and text[cursor] == "{":
            parser = SnbtParser(text, cursor)
            tag = parser.parse()
            if tag.kind != "compound" or parser.pos > end:
                return None
            children.append(("tag", tag))
    except ValueError:
        return None
    root = SnbtNode("compound", children, start, end)
    return make_item_identity_metadata(
        snbt_node_identity_value(root),
        item_root=root_name,
        role=role,
        confidence="high",
        span=(start, end),
        slot_spans=collect_snbt_slot_spans(root),
    )


def iter_snbt_compounds(text: str) -> Iterable[SnbtNode]:
    index = 0
    quote = ""
    escaped = False
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "{":
            try:
                parser = SnbtParser(text, index)
                node = parser.parse()
            except ValueError:
                index += 1
                continue
            if node.kind == "compound":
                yield node
                index = max(index + 1, node.end)
                continue
        index += 1


def command_item_identity_descriptors(line: str) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    final_command, command_offset = effective_command_text(line)
    command_end = command_offset + len(final_command)
    token_spans = top_level_token_spans(line, command_offset, command_end)
    tokens = [line[start:end].lower() for start, end in token_spans]
    word = tokens[0] if tokens else ""

    candidates: list[tuple[int, int, str, str]] = []
    if word in {"give", "clear"} and len(token_spans) >= 3:
        role = "producer" if word == "give" else "consumer"
        candidates.append((*token_spans[2], role, f"command.{word}.item"))
    elif word == "item" and "with" in tokens:
        with_index = tokens.index("with")
        if with_index + 1 < len(token_spans):
            candidates.append((*token_spans[with_index + 1], "producer", "command.item.with"))

    original_spans = top_level_token_spans(line)
    original_tokens = [line[start:end].lower().lstrip("/$") for start, end in original_spans]
    for index in range(len(original_tokens) - 2):
        if original_tokens[index : index + 3] not in (["execute", "if", "items"], ["execute", "unless", "items"]):
            continue
        run_index = next((offset for offset in range(index + 3, len(original_tokens)) if original_tokens[offset] == "run"), len(original_tokens))
        if run_index > index + 3:
            predicate_index = run_index - 1
            candidates.append(
                (*original_spans[predicate_index], "predicate", "command.execute.items.predicate")
            )

    for start, end, role, root_name in candidates:
        metadata = parse_command_item_stack(line, start, end, role=role, root_name=root_name)
        if metadata is not None:
            descriptors.append(metadata)

    original_lowered = strip_command_prefix_at(line)[0].lower()
    predicate_command = bool(
        re.search(r"^execute\s+(?:if|unless)\s+(?:entity|data|block)\b", original_lowered)
        and ("nbt=" in original_lowered or "components" in original_lowered or "item" in original_lowered)
    )
    command_role = {
        "give": "producer",
        "loot": "producer",
        "item": "producer",
        "data": "producer",
        "clear": "consumer",
    }.get(
        word,
        "predicate"
        if predicate_command
        else "producer"
        if word in {"summon", "setblock"}
        else "item_component",
    )
    for node in iter_snbt_compounds(line):
        descriptors.extend(
            snbt_item_descriptors(
                node,
                path=f"command.{word or 'unknown'}.snbt",
                default_role=command_role,
                confidence="medium",
            )
        )

    unique: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}
    for descriptor in descriptors:
        span = tuple(int(value) for value in descriptor.get("identity_item_span", []))
        key = (str(descriptor.get("identity_item_fingerprint", "")), span)
        unique[key] = descriptor
    return list(unique.values())


def span_inside_any(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(outer_start <= start and end <= outer_end for outer_start, outer_end in spans)


def span_contains_any(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start <= inner_start and inner_end <= end for inner_start, inner_end in spans)


def snbt_key_hint_before(text: str, start: int) -> str:
    prefix = text[:start]
    window = prefix[-240:]
    matches = list(re.finditer(r"([A-Za-z0-9_:.+-]+)\s*:", window))
    if matches:
        return matches[-1].group(1).lower()
    return ""


def item_text_key_hint_before(text: str, start: int) -> str:
    window = text[max(0, start - 320) : start]
    matches = list(
        re.finditer(
            r"(?i)(minecraft:(?:custom_name|item_name|lore)|custom_name|item_name|lore)\s*(?:=|:|~)\s*['\"]?\s*$",
            window,
        )
    )
    if matches:
        return matches[-1].group(1).lower()
    if re.search(r"(?is)display\s*:\s*\{[^{}]{0,240}\bname\s*:\s*$", window):
        return "display.name"
    return ""


def snbt_key_is_player_text(key: str) -> bool:
    lowered = key.lower()
    if lowered in SNBT_TEXT_KEYS:
        return True
    return any(lowered.endswith(f".{hint}") for hint in SNBT_TEXT_KEYS if "." in hint)


def source_kind_from_snbt_key(key: str, fallback: str) -> str:
    lowered = key.lower()
    if "custom_name" in lowered:
        return "item_name"
    if "customname" in lowered:
        return "entity_name"
    if "lore" in lowered:
        return "item_lore"
    if "display.name" in lowered:
        return "item_name"
    if "pages" in lowered:
        return "book"
    if "messages" in lowered:
        return "sign"
    if lowered.endswith("text"):
        return "text_display"
    return fallback


def source_kind_from_plain_command(word: str, fallback: str) -> str:
    if word == "say":
        return "say"
    if word in {"tell", "msg", "w"}:
        return "tellraw"
    return fallback


def command_plain_message_units(
    line: str,
    *,
    source_file: str,
    base_address: dict[str, Any],
    namespace: str,
    map_slug: str,
    fallback_kind: str,
    confidence: str,
    occupied_spans: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    command, command_offset = effective_command_text(line)
    word, rest = split_command_arguments(command)
    if word not in {"say", "tell", "msg", "w"}:
        return []

    if word == "say":
        message_start_in_command = len(word) + (len(command[len(word) :]) - len(command[len(word) :].lstrip()))
    else:
        target_parts = rest.split(None, 1)
        if len(target_parts) < 2:
            return []
        target = target_parts[0]
        after_word = command[len(word) :]
        spaces_after_word = len(after_word) - len(after_word.lstrip())
        message_start_in_command = len(word) + spaces_after_word + len(target)
        after_target = command[message_start_in_command:]
        message_start_in_command += len(after_target) - len(after_target.lstrip())

    start = command_offset + message_start_in_command
    end = len(line.rstrip("\r\n"))
    if start >= end or span_inside_any(start, end, occupied_spans):
        return []
    raw = line[start:end]
    if not is_player_text(raw):
        return []
    source_kind = source_kind_from_plain_command(word, fallback_kind)
    address = {**base_address, "command_plain_span": [start, end], "command_word": word}
    temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
    return [
        make_unit(
            edition="java",
            source_kind=source_kind,
            source_file=source_file,
            address=address,
            raw=raw,
            mode_support=["embedded-direct"],
            confidence="low" if confidence != "low" else confidence,
            resource_namespace=namespace,
            translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
            notes="Plain command message; direct copied-world or copied-function patching is required.",
        )
    ]


def storage_value_string_units(
    line: str,
    *,
    source_file: str,
    base_address: dict[str, Any],
    namespace: str,
    map_slug: str,
    fallback_kind: str,
    confidence: str,
    occupied_spans: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    command, _command_offset = effective_command_text(line)
    lowered = command.lower()
    if not lowered.startswith("data modify storage "):
        return []
    units: list[dict[str, Any]] = []
    occupied = list(occupied_spans)
    for literal in iter_quoted_string_literals(line):
        if span_inside_any(literal.start, literal.end, occupied):
            continue
        prefix = line[: literal.start].lower()
        if not re.search(r"\b(?:set|append|prepend|insert\s+\d+)\s+value\s*$", prefix):
            continue
        raw = literal.decoded
        if not is_player_text(raw):
            continue
        address = {
            **base_address,
            "command_string_span": [literal.start, literal.end],
            "command_string_quote": literal.quote,
            "command_storage_value": True,
        }
        temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
        units.append(
            make_unit(
                edition="java",
                source_kind="storage_text",
                source_file=source_file,
                address=address,
                raw=raw,
                mode_support=["embedded-direct"],
                confidence="low" if confidence != "low" else confidence,
                resource_namespace=namespace,
                translation_key=generated_key(namespace, map_slug, "storage_text", temp_id),
                notes="Plain data modify storage value; verify it is player-facing before direct copied-world patching.",
            )
        )
        occupied.append((literal.start, literal.end))
    return units


def command_json_plain_string_units(
    obj: Any,
    *,
    span_start: int,
    span_end: int,
    source_file: str,
    base_address: dict[str, Any],
    namespace: str,
    map_slug: str,
    fallback_kind: str,
    confidence: str,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for json_string_path, value in iter_json_strings(obj):
        if json_string_is_inside_component(obj, json_string_path):
            continue
        if parse_component_string(value) is not None:
            continue
        if not json_path_is_plain_text_candidate(json_string_path) or not is_player_text(value):
            continue
        source_kind = source_kind_from_json_path(json_string_path, fallback_kind)
        if source_kind == fallback_kind and fallback_kind in {"command_block", "function"}:
            source_kind = "storage_text"
        address = {
            **base_address,
            "command_span": [span_start, span_end],
            "command_json_path": json_string_path,
        }
        temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), value)
        units.append(
            make_unit(
                edition="java",
                source_kind=source_kind,
                source_file=source_file,
                address=address,
                raw=value,
                mode_support=["embedded-direct"],
                confidence="low" if confidence != "low" else confidence,
                resource_namespace=namespace,
                translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
                notes="Plain string inside a command JSON value; direct copied-world or copied-function patching is required.",
            )
        )
    return units


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
        "review_status": "",
        "review_reason": "",
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


def identity_text_shape(row: dict[str, Any]) -> list[str]:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    nodes = context.get("text_nodes") if isinstance(context, dict) else []
    if isinstance(nodes, list):
        texts = [str(node.get("text", "")) for node in nodes if isinstance(node, dict)]
        if texts:
            return texts
    return [str(row.get("raw", ""))]


def identity_role_for_row(row: dict[str, Any]) -> str:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    existing = str(context.get("identity_role", "")).strip()
    if existing:
        return existing
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    path = str(address.get("nbt_path", "")).lower()
    if ".offers.recipes[" in path:
        if re.search(r"\.(?:buy|buyb)(?:\.|$)", path):
            return "trade_input"
        if re.search(r"\.sell(?:\.|$)", path):
            return "trade_output"
    command = str(context.get("identity_command", "")).lower()
    if command in IDENTITY_PRODUCER_COMMANDS:
        return "producer"
    if command == "execute_if_items":
        return "predicate"
    if command in IDENTITY_CONSUMER_COMMANDS:
        return "consumer"
    return "item_component"


def identity_slot_for_row(row: dict[str, Any], source_path: str = "") -> str:
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    existing = str(context.get("identity_slot", "")).strip()
    if existing:
        return existing
    lowered_path = source_path.lower()
    if str(row.get("source_kind", "")) == "item_lore":
        match = re.search(r"lore\[(\d+)\]", lowered_path)
        return f"lore[{match.group(1)}]" if match else "lore"
    return "name" if str(row.get("source_kind", "")) == "item_name" else ""


def attach_item_identity_metadata(
    rows: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    *,
    source_path: str = "",
) -> None:
    if not metadata:
        return
    for row in rows:
        if str(row.get("source_kind", "")) not in IDENTITY_COUPLED_SOURCE_KINDS:
            continue
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        for key, value in metadata.items():
            if key not in {"identity_slot_spans"}:
                context[key] = value
        context["identity_slot"] = identity_slot_for_row(row, source_path)
        row["context"] = context


def row_command_identity_span(row: dict[str, Any]) -> tuple[int, int] | None:
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    for field in ("command_string_span", "command_span", "command_plain_span"):
        value = address.get(field)
        if isinstance(value, list) and len(value) == 2 and all(isinstance(item, int) for item in value):
            return int(value[0]), int(value[1])
    return None


def attach_command_item_identities(rows: list[dict[str, Any]], line: str) -> None:
    descriptors = command_item_identity_descriptors(line)
    for row in rows:
        source_kind = str(row.get("source_kind", ""))
        span = row_command_identity_span(row)
        desired_prefixes = (
            {"lore"}
            if source_kind == "item_lore"
            else {"name"}
            if source_kind == "item_name"
            else {"name", "lore"}
        )
        matches: list[tuple[int, dict[str, Any], str]] = []
        for descriptor in descriptors:
            item_span = descriptor.get("identity_item_span", [])
            if not isinstance(item_span, list) or len(item_span) != 2:
                continue
            item_start, item_end = int(item_span[0]), int(item_span[1])
            if span is not None and not (item_start <= span[0] and span[1] <= item_end):
                continue
            slot_spans = descriptor.get("identity_slot_spans", {})
            if not isinstance(slot_spans, dict):
                continue
            for slot, values in slot_spans.items():
                if not any(str(slot).startswith(prefix) for prefix in desired_prefixes):
                    continue
                if span is not None and not any(
                    isinstance(value, list)
                    and len(value) == 2
                    and int(value[0]) < span[1]
                    and span[0] < int(value[1])
                    for value in values
                ):
                    continue
                matches.append((item_end - item_start, descriptor, str(slot)))
        if not matches:
            continue
        _size, metadata, slot = min(matches, key=lambda item: item[0])
        copied = dict(metadata)
        copied.pop("identity_slot_spans", None)
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        context.update(copied)
        context["identity_slot"] = slot
        row["context"] = context
        row["source_kind"] = "item_lore" if slot.startswith("lore") else "item_name"


def canonicalize_identity_keys(rows: list[dict[str, Any]], namespace: str, map_slug: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        source_kind = str(row.get("source_kind", ""))
        if source_kind not in IDENTITY_COUPLED_SOURCE_KINDS:
            continue
        shape = identity_text_shape(row)
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        fingerprint = str(context.get("identity_item_fingerprint", "")).strip()
        slot = identity_slot_for_row(row, str(row.get("address", {}).get("nbt_path", "")))
        resolution = str(context.get("identity_resolution", "")).strip()
        if fingerprint and slot and resolution in {"structural", "manual"}:
            group_id = stable_id("identity-slot-v2", fingerprint, slot)
        else:
            group_id = stable_id("identity-unresolved-v2", str(row.get("id", "")))
            resolution = "unresolved"
            unresolved.append(row)
        context["identity_coupled"] = True
        context["identity_group"] = group_id
        context["identity_role"] = identity_role_for_row(row)
        context["identity_text_shape"] = shape
        context["identity_slot"] = slot
        context["identity_resolution"] = resolution
        row["context"] = context
        groups[group_id].append(row)

    for group_id, group_rows in groups.items():
        source_kind = normalize_key_piece(str(group_rows[0].get("source_kind", "item_text")))
        canonical_key = ".".join(
            [normalize_key_piece(namespace), normalize_key_piece(map_slug), "identity", source_kind, group_id]
        )
        for row in group_rows:
            context = row.get("context") if isinstance(row.get("context"), dict) else {}
            if (
                context.get("identity_resolution") in {"structural", "manual"}
                and "hybrid-key-injection" in row.get("mode_support", [])
            ):
                row["translation_key"] = canonical_key
                for segment in row_segments(row):
                    index = int(segment.get("index", 0))
                    segment["translation_key"] = f"{canonical_key}.part_{index}"

    return {
        "unit_count": sum(len(group) for group in groups.values()),
        "group_count": len(groups),
        "repeated_group_count": sum(1 for group in groups.values() if len(group) > 1),
        "max_group_size": max((len(group) for group in groups.values()), default=0),
        "structural_group_count": sum(
            1
            for group in groups.values()
            if group
            and isinstance(group[0].get("context"), dict)
            and group[0]["context"].get("identity_resolution") in {"structural", "manual"}
        ),
        "unresolved_unit_count": len(unresolved),
    }


def write_identity_review_template(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    namespace: str,
    map_slug: str,
) -> None:
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        if not context.get("identity_coupled") or context.get("identity_resolution") != "unresolved":
            continue
        unresolved.append(
            {
                "unit_id": str(row.get("id", "")),
                "source_kind": str(row.get("source_kind", "")),
                "raw": str(row.get("raw", "")),
                "source_file": str(row.get("source_file", "")),
                "address": row.get("address", {}),
                "inferred_role": str(context.get("identity_role", "item_component")),
            }
        )
    template = {
        "schema": "mc-map-identity-decisions.v1",
        "namespace": namespace,
        "map_slug": map_slug,
        "unresolved_units": unresolved,
        "groups": [],
        "external_sources": [],
        "instructions": (
            "Review exact anchors and non-text item evidence. Fill groups only for one logical item; "
            "do not group by visible wording alone. External sources require a concrete runtime-source reason."
        ),
    }
    write_json(path, template)


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


def set_json_path(obj: Any, path: str, value: Any) -> None:
    parent_path, leaf = parent_json_path(path)
    parent = get_json_path(obj, parent_path)
    if isinstance(leaf, int):
        if not isinstance(parent, list) or leaf < 0 or leaf >= len(parent):
            raise KeyError(path)
        parent[leaf] = value
        return
    if not isinstance(parent, dict) or leaf not in parent:
        raise KeyError(path)
    parent[leaf] = value


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
    stripped = normalize_command_line(line)
    execute_tail = execute_run_tail(stripped)
    if execute_tail:
        return infer_command_source_kind(execute_tail, fallback)
    lowered = stripped.lower()
    if lowered.startswith("tellraw "):
        return "tellraw"
    if lowered.startswith("tell "):
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
    if lowered.startswith("summon text_display "):
        return "text_display"
    if "customname" in lowered:
        return "entity_name"
    return fallback


def iter_json_literal_component_spans(line: str, direct_spans: list[tuple[int, int]]) -> Iterable[tuple[int, int, Any]]:
    for literal in iter_quoted_string_literals(line):
        if span_inside_any(literal.start, literal.end, direct_spans) or span_contains_any(literal.start, literal.end, direct_spans):
            continue
        stripped = literal.decoded.strip()
        if not (stripped.startswith("{") or stripped.startswith("[")):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not is_component_root(obj):
            continue
        yield literal.start, literal.end, obj


def plain_snbt_string_units(
    line: str,
    *,
    source_file: str,
    base_address: dict[str, Any],
    namespace: str,
    map_slug: str,
    fallback_kind: str,
    confidence: str,
    direct_spans: list[tuple[int, int]],
    literal_json_spans: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    occupied = list(direct_spans) + list(literal_json_spans)
    for literal in iter_quoted_string_literals(line):
        if span_inside_any(literal.start, literal.end, occupied):
            continue
        key_hint = item_text_key_hint_before(line, literal.start) or snbt_key_hint_before(line, literal.start)
        if not key_hint or not snbt_key_is_player_text(key_hint):
            continue
        raw = literal.decoded
        stripped = raw.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            continue
        if not is_player_text(raw):
            continue
        source_kind = source_kind_from_snbt_key(key_hint, fallback_kind)
        address = {
            **base_address,
            "command_string_span": [literal.start, literal.end],
            "command_string_quote": literal.quote,
            "snbt_key_hint": key_hint,
        }
        temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
        units.append(
            make_unit(
                edition="java",
                source_kind=source_kind,
                source_file=source_file,
                address=address,
                raw=raw,
                mode_support=["embedded-direct"],
                confidence=confidence,
                resource_namespace=namespace,
                translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
                notes="Plain SNBT/command string from a player-facing key hint; direct copied-world patching is required.",
            )
        )
    return units


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
    direct_component_spans: list[tuple[int, int]] = []
    for start, end, obj in iter_json_spans(line):
        component_units = extract_text_components(
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
        if component_units:
            direct_component_spans.append((start, end))
            units.extend(component_units)
        else:
            plain_json_units = command_json_plain_string_units(
                obj,
                span_start=start,
                span_end=end,
                source_file=source_file,
                base_address=base_address,
                namespace=namespace,
                map_slug=map_slug,
                fallback_kind=source_kind,
                confidence=confidence,
            )
            if plain_json_units:
                direct_component_spans.append((start, end))
                units.extend(plain_json_units)
    literal_json_spans: list[tuple[int, int]] = []
    for start, end, obj in iter_json_literal_component_spans(line, direct_component_spans):
        literal_json_spans.append((start, end))
        units.extend(
            extract_text_components(
                obj,
                source_file=source_file,
                source_kind=source_kind,
                base_address={**base_address, "command_string_span": [start, end]},
                json_path="$",
                namespace=namespace,
                map_slug=map_slug,
                confidence=confidence,
                notes="Quoted command/SNBT string containing a JSON text component.",
            )
        )
    occupied_spans = list(direct_component_spans) + list(literal_json_spans)
    units.extend(
        plain_snbt_string_units(
            line,
            source_file=source_file,
            base_address=base_address,
            namespace=namespace,
            map_slug=map_slug,
            fallback_kind=source_kind,
            confidence="low" if confidence == "medium" else confidence,
            direct_spans=direct_component_spans,
            literal_json_spans=literal_json_spans,
        )
    )
    occupied_spans.extend(
        tuple(row.get("address", {}).get("command_string_span", []))
        for row in units
        if isinstance(row.get("address"), dict)
        and isinstance(row["address"].get("command_string_span"), list)
        and len(row["address"]["command_string_span"]) == 2
    )
    units.extend(
        storage_value_string_units(
            line,
            source_file=source_file,
            base_address=base_address,
            namespace=namespace,
            map_slug=map_slug,
            fallback_kind=source_kind,
            confidence=confidence,
            occupied_spans=[span for span in occupied_spans if len(span) == 2],
        )
    )
    occupied_spans.extend(
        tuple(row.get("address", {}).get("command_string_span", []))
        for row in units
        if isinstance(row.get("address"), dict)
        and isinstance(row["address"].get("command_string_span"), list)
        and len(row["address"]["command_string_span"]) == 2
    )
    units.extend(
        command_plain_message_units(
            line,
            source_file=source_file,
            base_address=base_address,
            namespace=namespace,
            map_slug=map_slug,
            fallback_kind=source_kind,
            confidence=confidence,
            occupied_spans=[span for span in occupied_spans if len(span) == 2],
        )
    )
    lowered_line = strip_command_prefix_at(line)[0].lower()
    identity_command = (
        "execute_if_items"
        if re.search(r"\bexecute\s+(?:if|unless)\s+items\b", lowered_line)
        else command_word(line)
    )
    if identity_command:
        for row in units:
            if str(row.get("source_kind", "")) not in IDENTITY_COUPLED_SOURCE_KINDS:
                continue
            context = row.get("context") if isinstance(row.get("context"), dict) else {}
            context["identity_command"] = identity_command
            row["context"] = context
    attach_command_item_identities(units, line)
    for row in units:
        if str(row.get("source_kind", "")) in IDENTITY_COUPLED_SOURCE_KINDS:
            continue
        span = row_command_identity_span(row)
        if span is None:
            continue
        item_key_hint = item_text_key_hint_before(line, span[0])
        if not item_key_hint:
            continue
        row["source_kind"] = source_kind_from_snbt_key(item_key_hint, str(row.get("source_kind", "function")))
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        if identity_command:
            context["identity_command"] = identity_command
        context["identity_parse_warning"] = "item text key was found, but the containing item structure was not parsed"
        row["context"] = context
    return units


def scan_mcfunction(
    entry: Entry,
    namespace: str,
    map_slug: str,
    *,
    counters: Counter[str] | None = None,
    suspicious_hints: list[dict[str, Any]] | None = None,
    function_calls: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if entry.data is None:
        return []
    text = decode_text(entry.data, entry.path)
    if text is None:
        return []

    units: list[dict[str, Any]] = []
    function_id = function_id_from_path(entry.path)
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        is_macro = is_macro_function_line(line)
        if is_macro and counters is not None:
            counters["macro_function_lines"] += 1
        call_target = function_call_target(line)
        if call_target and function_calls is not None:
            function_calls.append(
                {
                    "source_file": entry.path,
                    "function_id": function_id,
                    "line": line_no,
                    "target": call_target,
                    "macro": is_macro,
                }
            )
        base_address: dict[str, Any] = {"function_line": line_no}
        if is_macro:
            base_address["function_macro"] = True
        if function_id:
            base_address["function_id"] = function_id
        line_units = scan_command_line(
            line,
            source_file=entry.path,
            base_address=base_address,
            namespace=namespace,
            map_slug=map_slug,
            fallback_kind="function",
            confidence="high",
        )
        if line_units:
            for row in line_units:
                context = row.get("context") if isinstance(row.get("context"), dict) else {}
                if function_id:
                    context["function_id"] = function_id
                context["function_line"] = line_no
                if is_macro:
                    context["function_macro"] = True
                if call_target:
                    context["function_call_target"] = call_target
                row["context"] = context
            units.extend(line_units)
        elif suspicious_hints is not None and (is_macro or command_word(line) in {"say", "tell", "msg", "w"}):
            if has_english_words(stripped) and len(suspicious_hints) < 200:
                suspicious_hints.append(
                    {
                        "kind": "mcfunction_line",
                        "source_file": entry.path,
                        "line": line_no,
                        "function_id": function_id,
                        "macro": is_macro,
                        "raw_preview": audit_preview(stripped),
                    }
                )
    return units


def json_path_name_parts(path: str) -> list[str]:
    parts: list[str] = []
    try:
        for part in parse_json_path(path):
            if isinstance(part, str):
                lowered = part.lower()
                parts.append(lowered)
                if ":" in lowered:
                    parts.append(lowered.rsplit(":", 1)[-1])
    except ValueError:
        for part in re.findall(r"\.([A-Za-z0-9_:-]+)", path):
            lowered = part.lower()
            parts.append(lowered)
            if ":" in lowered:
                parts.append(lowered.rsplit(":", 1)[-1])
    return parts


def json_path_is_plain_text_candidate(path: str) -> bool:
    parts = json_path_name_parts(path)
    if not parts:
        return False
    if "score" in parts and parts[-1] in {"name", "objective"}:
        return False
    return any(part in JSON_TEXT_PATH_HINTS for part in parts)


def source_kind_from_json_path(path: str, fallback: str = "datapack_json") -> str:
    parts = json_path_name_parts(path)
    lowered = ".".join(parts)
    if "score" in parts:
        return "scoreboard"
    if "lore" in lowered:
        return "item_lore"
    if "pages" in lowered or ".page" in lowered:
        return "book"
    if "custom_name" in lowered or lowered.endswith("name") or ".name" in lowered:
        return "item_name"
    if "title" in lowered:
        return "title"
    if "subtitle" in lowered:
        return "title"
    if "message" in lowered or "dialogue" in lowered:
        return "datapack_json"
    if lowered.endswith("text") or ".text" in lowered:
        return "datapack_json"
    return fallback


def json_string_is_inside_component(obj: Any, path: str) -> bool:
    try:
        parent_path, _leaf = parent_json_path(path)
        parent = get_json_path(obj, parent_path)
    except (KeyError, ValueError):
        return False
    return isinstance(parent, dict) and any(key in parent for key in TEXT_COMPONENT_KEYS)


def embedded_json_string_component_units(
    obj: Any,
    *,
    source_file: str,
    namespace: str,
    map_slug: str,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for json_string_path, value in iter_json_strings(obj):
        if json_string_is_inside_component(obj, json_string_path):
            continue
        component = parse_component_string(value)
        if component is None:
            continue
        units.extend(
            extract_text_components(
                component,
                source_file=source_file,
                source_kind=source_kind_from_json_path(json_string_path),
                base_address={"json_string_path": json_string_path},
                json_path="$",
                namespace=namespace,
                map_slug=map_slug,
                confidence="medium",
                notes="Datapack JSON string containing a JSON text component.",
            )
        )
    return units


def plain_json_string_units(
    obj: Any,
    *,
    source_file: str,
    namespace: str,
    map_slug: str,
    suspicious_hints: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for json_string_path, value in iter_json_strings(obj):
        if json_string_is_inside_component(obj, json_string_path):
            continue
        if parse_component_string(value) is not None:
            continue
        if not is_player_text(value):
            continue
        if json_path_is_plain_text_candidate(json_string_path):
            source_kind = source_kind_from_json_path(json_string_path, "datapack_json_plain")
            address = {"json_string_path": json_string_path}
            temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), value)
            units.append(
                make_unit(
                    edition="java",
                    source_kind=source_kind,
                    source_file=source_file,
                    address=address,
                    raw=value,
                    mode_support=["embedded-direct"],
                    confidence="low",
                    resource_namespace=namespace,
                    translation_key=generated_key(namespace, map_slug, source_kind, temp_id),
                    notes="Plain datapack JSON string from a player-facing path hint; direct copied-world patching is required.",
                )
            )
        elif suspicious_hints is not None and len(suspicious_hints) < 200 and has_english_words(value):
            suspicious_hints.append(
                {
                    "kind": "datapack_json_string",
                    "source_file": source_file,
                    "json_path": json_string_path,
                    "raw_preview": audit_preview(value),
                }
            )
    return units


def scan_json_file(
    entry: Entry,
    namespace: str,
    map_slug: str,
    *,
    suspicious_hints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
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
    units = extract_text_components(
        obj,
        source_file=entry.path,
        source_kind="datapack_json",
        base_address={},
        json_path="$",
        namespace=namespace,
        map_slug=map_slug,
    )
    units.extend(embedded_json_string_component_units(obj, source_file=entry.path, namespace=namespace, map_slug=map_slug))
    units.extend(
        plain_json_string_units(
            obj,
            source_file=entry.path,
            namespace=namespace,
            map_slug=map_slug,
            suspicious_hints=suspicious_hints,
        )
    )
    return units


def is_binary_world_data(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(".mca") or lowered.endswith(".dat")


def decompress_dat_payload(data: bytes) -> bytes:
    if len(data) > MAX_NBT_BYTES:
        raise NbtReadError(f"NBT payload too large: {len(data)} bytes")
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data


def nbt_integer_value(tag: NbtTag) -> int | None:
    widths = {1: 1, 2: 2, 3: 4, 4: 8}
    width = widths.get(tag.tag_type)
    if width is None:
        return None
    data = bytes(tag.value)
    if len(data) != width:
        return None
    return int.from_bytes(data, "big", signed=True)


NBT_SCALAR_TYPES = {
    1: "byte",
    2: "short",
    3: "int",
    4: "long",
    5: "float",
    6: "double",
    7: "byte_array",
    8: "string",
    11: "int_array",
    12: "long_array",
}


def nbt_tag_identity_value(tag: NbtTag) -> Any:
    if tag.tag_type == 10:
        return {"$compound": [[name, nbt_tag_identity_value(child)] for name, child in tag.value]}
    if tag.tag_type == 9:
        _child_type, items = tag.value
        return {"$list": [nbt_tag_identity_value(child) for child in items]}
    if tag.tag_type in {1, 2, 3, 4}:
        return {"$type": NBT_SCALAR_TYPES[tag.tag_type], "value": nbt_integer_value(tag)}
    if tag.tag_type == 5:
        return {"$type": "float", "value": struct.unpack(">f", bytes(tag.value))[0]}
    if tag.tag_type == 6:
        return {"$type": "double", "value": struct.unpack(">d", bytes(tag.value))[0]}
    if tag.tag_type == 8:
        return {"$type": "string", "value": str(tag.value)}
    if tag.tag_type in {7, 11, 12}:
        width = {7: 1, 11: 4, 12: 8}[tag.tag_type]
        data = bytes(tag.value)
        values = [int.from_bytes(data[index : index + width], "big", signed=True) for index in range(0, len(data), width)]
        return {"$type": NBT_SCALAR_TYPES[tag.tag_type], "value": values}
    return {"$type": f"tag_{tag.tag_type}", "value": None}


def snbt_node_identity_value(node: SnbtNode) -> Any:
    if node.kind == "compound":
        return {"$compound": [[name, snbt_node_identity_value(child)] for name, child in node.value]}
    if node.kind == "list":
        if node.scalar_type:
            return {
                "$type": node.scalar_type,
                "value": [child.value for child in node.value],
            }
        value = {"$list": [snbt_node_identity_value(child) for child in node.value]}
        return value
    return {"$type": node.scalar_type or "string", "value": node.value}


def identity_compound_items(value: Any) -> list[tuple[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("$compound"), list):
        return []
    return [(str(item[0]), item[1]) for item in value["$compound"] if isinstance(item, list) and len(item) == 2]


def identity_scalar_string(value: Any) -> str:
    if isinstance(value, dict) and value.get("$type") == "string":
        return str(value.get("value", ""))
    return ""


def identity_text_slot(path: tuple[str | int, ...]) -> str:
    names = [str(part).lower() for part in path if isinstance(part, str)]
    joined = ".".join(names)
    if any(name.rsplit(":", 1)[-1] in {"custom_name", "item_name"} for name in names):
        return "name"
    if "display.name" in joined:
        return "name"
    lore_at = next((index for index, name in enumerate(names) if name.rsplit(":", 1)[-1] == "lore"), -1)
    if lore_at >= 0:
        path_index = next((part for part in path[lore_at + 1 :] if isinstance(part, int)), None)
        return f"lore[{path_index}]" if path_index is not None else "lore"
    return ""


def canonicalize_identity_value(
    value: Any,
    path: tuple[str | int, ...] = (),
    *,
    redact_text: bool = False,
    top_level: bool = True,
) -> Any:
    compound_items = identity_compound_items(value)
    if compound_items:
        normalized: list[list[Any]] = []
        for name, child in compound_items:
            lowered = name.lower()
            if top_level and lowered in {"count", "slot"}:
                continue
            normalized.append(
                [
                    name,
                    canonicalize_identity_value(child, (*path, name), redact_text=redact_text, top_level=False),
                ]
            )
        normalized.sort(key=lambda item: item[0])
        return {"$compound": normalized}
    if isinstance(value, dict) and isinstance(value.get("$list"), list):
        result = {
            "$list": [
                canonicalize_identity_value(child, (*path, index), redact_text=redact_text, top_level=False)
                for index, child in enumerate(value["$list"])
            ]
        }
        if value.get("$array_type"):
            result["$array_type"] = value["$array_type"]
        return result
    slot = identity_text_slot(path)
    if redact_text and slot and isinstance(value, dict) and value.get("$type") == "string":
        return {"$type": "identity_text", "slot": slot}
    return value


def collect_identity_slot_values(value: Any, path: tuple[str | int, ...] = ()) -> dict[str, list[str]]:
    slots: dict[str, list[str]] = defaultdict(list)

    def visit(current: Any, current_path: tuple[str | int, ...]) -> None:
        compound_items = identity_compound_items(current)
        if compound_items:
            for name, child in compound_items:
                visit(child, (*current_path, name))
            return
        if isinstance(current, dict) and isinstance(current.get("$list"), list):
            for index, child in enumerate(current["$list"]):
                visit(child, (*current_path, index))
            return
        slot = identity_text_slot(current_path)
        if slot and isinstance(current, dict) and current.get("$type") == "string":
            slots[slot].append(str(current.get("value", "")))

    visit(value, path)
    return dict(slots)


def identity_role_from_path(path: str) -> str:
    lowered = path.lower()
    if re.search(r"\.offers\.recipes\[\d+\]\.(?:buy|buyb)(?:\.|$)", lowered):
        return "trade_input"
    if re.search(r"\.offers\.recipes\[\d+\]\.sell(?:\.|$)", lowered):
        return "trade_output"
    if re.search(r"\.(?:items|inventory|enderitems)\[\d+\](?:\.|$)", lowered):
        return "container"
    return "item_component"


def make_item_identity_metadata(
    value: Any,
    *,
    item_root: str,
    role: str,
    confidence: str,
    span: tuple[int, int] | None = None,
    slot_spans: dict[str, list[tuple[int, int]]] | None = None,
) -> dict[str, Any] | None:
    item_id = ""
    for name, child in identity_compound_items(value):
        if name.lower() == "id":
            item_id = identity_scalar_string(child)
            break
    if not item_id:
        return None
    canonical = canonicalize_identity_value(value)
    non_text = canonicalize_identity_value(value, redact_text=True)
    slot_values = collect_identity_slot_values(value)
    if not slot_values:
        return None
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    non_text_json = json.dumps(non_text, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    metadata: dict[str, Any] = {
        "identity_item_id": item_id,
        "identity_item_fingerprint": stable_id("item-structure-v2", canonical_json),
        "identity_non_text_fingerprint": stable_id("item-non-text-v2", non_text_json),
        "identity_item_root": item_root,
        "identity_role": role,
        "identity_resolution": "structural",
        "identity_confidence": confidence,
        "identity_slots": sorted(slot_values),
    }
    if span is not None:
        metadata["identity_item_span"] = [span[0], span[1]]
    if slot_spans:
        metadata["identity_slot_spans"] = {
            slot: [[start, end] for start, end in spans] for slot, spans in sorted(slot_spans.items())
        }
    return metadata


def nbt_item_identity_metadata(tag: NbtTag, path: str) -> dict[str, Any] | None:
    if tag.tag_type != 10:
        return None
    names = {name.lower() for name, _child in tag.value}
    role = identity_role_from_path(path)
    looks_like_item = role != "item_component" or (
        "id" in names and bool(names.intersection({"count", "components", "tag", "slot"}))
    )
    if not looks_like_item:
        return None
    return make_item_identity_metadata(
        nbt_tag_identity_value(tag),
        item_root=path,
        role=role,
        confidence="high",
    )


def compound_block_pos(tag: NbtTag) -> dict[str, int] | None:
    if tag.tag_type != 10:
        return None
    values = {name.lower(): child for name, child in tag.value}
    coords = {axis: nbt_integer_value(values[axis]) for axis in ("x", "y", "z") if axis in values}
    if len(coords) != 3 or any(value is None for value in coords.values()):
        return None
    return {axis: int(coords[axis]) for axis in ("x", "y", "z")}


def collect_nbt_strings(
    tag: NbtTag,
    path: str,
    out: list[NbtString],
    *,
    chunk: dict[str, int] | None,
    block_pos: dict[str, int] | None = None,
    item_identity: dict[str, Any] | None = None,
) -> None:
    if tag.tag_type == 8:
        out.append(
            NbtString(
                path=path,
                value=str(tag.value),
                chunk=chunk,
                block_pos=block_pos,
                item_identity=item_identity,
            )
        )
        return
    if tag.tag_type == 9:
        _child_type, items = tag.value
        for index, child in enumerate(items):
            collect_nbt_strings(
                child,
                f"{path}[{index}]",
                out,
                chunk=chunk,
                block_pos=block_pos,
                item_identity=item_identity,
            )
        return
    if tag.tag_type == 10:
        current_pos = compound_block_pos(tag) or block_pos
        current_item_identity = nbt_item_identity_metadata(tag, path) or item_identity
        for name, child in tag.value:
            child_path = f"{path}.{name}" if path else name
            collect_nbt_strings(
                child,
                child_path,
                out,
                chunk=chunk,
                block_pos=current_pos,
                item_identity=current_item_identity,
            )


def scan_nbt_strings(data: bytes, chunk: dict[str, int] | None = None) -> list[NbtString]:
    tree = NbtTreeReader(data).read()
    strings: list[NbtString] = []
    collect_nbt_strings(tree.root, tree.root_path, strings, chunk=chunk)
    return strings


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
    leaf = nbt_path_leaf(path)
    if "custom_name" in lowered and (
        ".components" in lowered
        or ".items[" in lowered
        or ".buy" in lowered
        or ".sell" in lowered
        or ".tag.display" in lowered
    ):
        return "item_name"
    if "customname" in lowered or "custom_name" in lowered:
        return "entity_name"
    if "lore" in lowered:
        return "item_lore"
    if ".pages" in lowered or ".filteredpages" in lowered or "written_book_content.pages" in lowered:
        return "book"
    if "front_text" in lowered or "back_text" in lowered or SIGN_OLD_TEXT_RE.match(path):
        return "sign"
    if "bossbar" in lowered:
        return "bossbar"
    if "scoreboard" in lowered or "displayname" in lowered:
        return "scoreboard"
    if "display.name" in lowered or lowered.endswith(".name"):
        return "item_name"
    if leaf == "text" and (".entities[" in lowered or ".block_entities[" in lowered or ".blockentities[" in lowered):
        return "text_display"
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
    lowered = path.lower()
    parts = path_parts(path)
    leaf = parts[-1] if parts else ""
    if any(part in PLAIN_TEXT_PATH_HINTS for part in parts):
        return True
    if "front_text.messages" in lowered or "back_text.messages" in lowered:
        return True
    if ".pages" in lowered or ".filteredpages" in lowered or "written_book_content.pages" in lowered:
        return True
    if "display.lore" in lowered or "display.name" in lowered:
        return True
    if leaf == "text" and (".entities[" in lowered or ".block_entities[" in lowered or ".blockentities[" in lowered):
        return True
    return False


def sign_line_info(path: str) -> tuple[str, int] | None:
    match = SIGN_NEW_TEXT_RE.match(path)
    if match:
        return match.group("base"), int(match.group("index"))
    match = SIGN_OLD_TEXT_RE.match(path)
    if match:
        return f"{match.group('base')}.Text", int(match.group("index")) - 1
    return None


def parse_component_string(value: str) -> Any | None:
    stripped = value.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return obj if is_component_root(obj) else None


def collect_all_component_text_nodes(obj: Any, json_path: str = "$") -> list[dict[str, str]]:
    nodes: list[dict[str, str]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str) and value["text"]:
                nodes.append({"json_path": f"{path}.text", "text": value["text"]})
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(obj, json_path)
    return nodes


def line_synthetic_json_path(line_index: int, component_json_path: str) -> str:
    suffix = component_json_path[1:] if component_json_path.startswith("$") else f".{component_json_path}"
    return f"$.lines[{line_index}]{suffix}"


def build_sign_group_unit(
    items: list[tuple[int, NbtString]],
    *,
    source_file: str,
    base_path: str,
    namespace: str,
    map_slug: str,
) -> tuple[dict[str, Any] | None, set[str]]:
    line_texts = ["", "", "", ""]
    text_nodes: list[dict[str, str]] = []
    segments: list[dict[str, Any]] = []
    sign_lines: list[dict[str, Any]] = []
    grouped_paths: set[str] = set()
    chunk = items[0][1].chunk if items else None
    block_pos = items[0][1].block_pos if items else None

    for line_index, item in sorted(items, key=lambda pair: pair[0]):
        obj = parse_component_string(item.value)
        if obj is None:
            continue
        component_nodes = collect_all_component_text_nodes(obj, "$")
        grouped_paths.add(item.path)
        sign_lines.append({"line_index": line_index, "nbt_path": item.path, "json_path": "$"})
        if not component_nodes:
            continue
        line_raw = "".join(str(node.get("text", "")) for node in component_nodes if isinstance(node, dict))
        line_texts[line_index] = line_raw
        for node in component_nodes:
            if not isinstance(node, dict):
                continue
            component_path = str(node.get("json_path", ""))
            raw = str(node.get("text", ""))
            synthetic_path = line_synthetic_json_path(line_index, component_path)
            text_nodes.append({"json_path": synthetic_path, "text": raw})
            segments.append(
                {
                    "index": len(segments),
                    "line_index": line_index,
                    "nbt_path": item.path,
                    "json_path": synthetic_path,
                    "component_json_path": component_path,
                    "raw": raw,
                    "translation": "",
                    "review_status": "",
                    "review_reason": "",
                    "translation_key": "",
                }
            )

    raw = "\n".join(line_texts).strip("\n")
    player_segments = [segment for segment in segments if is_player_text(str(segment.get("raw", "")))]
    if not player_segments or not is_player_text(raw):
        return None, set()

    address: dict[str, Any] = {"sign_group": base_path, "sign_lines": sign_lines}
    if chunk:
        address["chunk"] = chunk
    if block_pos:
        address["block_pos"] = block_pos
    context = {
        "text_nodes": text_nodes,
        "sign_lines": sign_lines,
        "line_texts": line_texts,
        "sign_group": base_path,
    }
    temp_id = stable_id(source_file, json.dumps(address, sort_keys=True), raw)
    translation_key = generated_key(namespace, map_slug, "sign", temp_id)
    unit = make_unit(
        edition="java",
        source_kind="sign",
        source_file=source_file,
        address=address,
        raw=raw,
        mode_support=["hybrid-key-injection"],
        confidence="medium",
        resource_namespace=namespace,
        translation_key=translation_key,
        context=context,
        notes="Aggregated sign text. Translate raw as the complete sign face, then fill segments per sign line/text node for safe key injection.",
    )
    for segment in segments:
        segment["translation_key"] = f"{translation_key}.part_{segment['index']}"
    unit["segments"] = segments
    return unit, grouped_paths


def scan_nbt_value(
    item: NbtString,
    *,
    source_file: str,
    namespace: str,
    map_slug: str,
    include_last_output: bool = False,
    counters: Counter[str] | None = None,
) -> list[dict[str, Any]]:
    value = item.value
    if nbt_path_is_last_output(item.path) and not include_last_output:
        if counters is not None:
            counters["excluded_last_output"] += 1
        return []
    if nbt_path_is_internal(item.path):
        return []
    source_kind = source_kind_from_nbt_path(item.path, value)
    base_address: dict[str, Any] = {"nbt_path": item.path}
    if item.chunk:
        base_address["chunk"] = item.chunk
    if item.block_pos:
        base_address["block_pos"] = item.block_pos

    units: list[dict[str, Any]] = []
    if ".command" in item.path.lower() or COMMAND_START_RE.match(value):
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
        attach_item_identity_metadata(units, item.item_identity, source_path=item.path)
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
            attach_item_identity_metadata(units, item.item_identity, source_path=item.path)
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
    attach_item_identity_metadata(units, item.item_identity, source_path=item.path)
    return units


def scan_nbt_items(
    items: list[NbtString],
    *,
    source_file: str,
    namespace: str,
    map_slug: str,
    include_last_output: bool,
    counters: Counter[str],
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    sign_groups: dict[tuple[str, str], list[tuple[int, NbtString]]] = defaultdict(list)
    for item in items:
        info = sign_line_info(item.path)
        if info is None:
            continue
        base_path, line_index = info
        chunk_key = json.dumps(item.chunk or {}, sort_keys=True)
        sign_groups[(chunk_key, base_path)].append((line_index, item))

    counters["sign_faces_seen"] += len(sign_groups)
    grouped_sign_paths: set[str] = set()
    for (_chunk_key, base_path), group_items in sign_groups.items():
        unit, paths = build_sign_group_unit(
            group_items,
            source_file=source_file,
            base_path=base_path,
            namespace=namespace,
            map_slug=map_slug,
        )
        if unit is not None:
            units.append(unit)
            grouped_sign_paths.update(paths)
            counters["aggregated_sign_groups"] += 1
        else:
            counters["sign_faces_without_player_text"] += 1

    for item in items:
        if item.path in grouped_sign_paths:
            continue
        units.extend(
            scan_nbt_value(
                item,
                source_file=source_file,
                namespace=namespace,
                map_slug=map_slug,
                include_last_output=include_last_output,
                counters=counters,
            )
        )
    return units


def scan_binary_entry(
    entry: Entry,
    namespace: str,
    map_slug: str,
    *,
    include_last_output: bool,
    counters: Counter[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if entry.data is None:
        return [], [f"{entry.path}: no data"]
    units: list[dict[str, Any]] = []
    errors: list[str] = []
    lowered = entry.path.lower()

    if lowered.endswith(".dat"):
        try:
            nbt_data = decompress_dat_payload(entry.data)
            units.extend(
                scan_nbt_items(
                    scan_nbt_strings(nbt_data),
                    source_file=entry.path,
                    namespace=namespace,
                    map_slug=map_slug,
                    include_last_output=include_last_output,
                    counters=counters,
                )
            )
        except Exception as exc:
            errors.append(f"{entry.path}: {exc}")
        return units, errors

    if lowered.endswith(".mca"):
        blobs, region_errors = iter_region_nbt(entry)
        errors.extend(region_errors)
        for chunk, nbt_data in blobs:
            try:
                units.extend(
                    scan_nbt_items(
                        scan_nbt_strings(nbt_data, chunk=chunk),
                        source_file=entry.path,
                        namespace=namespace,
                        map_slug=map_slug,
                        include_last_output=include_last_output,
                        counters=counters,
                    )
                )
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


def visual_text_asset_kind(path: str) -> str:
    lowered = path.lower()
    if RESOURCE_PNG_RE.search(lowered) and VISUAL_TEXT_PATH_HINT_RE.search(lowered):
        return "png_text_candidate"
    if RESOURCE_FONT_JSON_RE.search(lowered):
        return "font_provider_json"
    if RESOURCE_MODEL_JSON_RE.search(lowered) and VISUAL_TEXT_PATH_HINT_RE.search(lowered):
        return "model_text_candidate"
    return ""


def full_localization_recommendation(report: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    counts_by_mode = report.get("counts_by_mode", {})
    has_hybrid = bool(counts_by_mode.get("hybrid-key-injection", 0))
    has_direct = bool(report.get("direct_only_unit_count", 0) or report.get("plain_command_string_unit_count", 0))
    if counts_by_mode.get("hybrid-key-injection", 0):
        reasons.append("hardcoded JSON text components require hybrid key injection in a copied map for resource-pack-backed localization")
    if has_direct:
        reasons.append("plain command/SNBT/datapack JSON/NBT strings require explicit embedded-direct copied-world patching")
    visual = report.get("visual_text_asset_hints", {})
    if visual.get("total", 0):
        reasons.append("resource-pack image/font/model assets may contain visual text that language JSON cannot translate")
    if report.get("suspicious_text_hint_count", 0):
        reasons.append("some datapack macro/storage/JSON strings look player-facing but need confirmation before safe apply")
    if report.get("pending_binary_parser_coverage"):
        reasons.append("some binary files were pending or failed parser coverage")
    if report.get("map_resource_pack_count", 0):
        reasons.append("the map already includes resources.zip; embedded exports must merge generated language files into the existing pack instead of replacing it")
    return {
        "ask_user_after_scan": True,
        "suggest_full_translation_mode": bool(reasons),
        "reasons": reasons,
        "export_modes": [
            {
                "name": "resource-pack-only",
                "world_data_changed": False,
                "confirmation_required": False,
                "output": "standalone resource-pack zip",
                "covers": "existing language/resource entries and text already using translate keys",
                "limits": "does not cover hardcoded command/sign/book/entity/plain NBT text or visual text in images/fonts",
            },
            {
                "name": "embedded-pack-copy",
                "world_data_changed": "copy-only",
                "confirmation_required": False,
                "output": "copied map/world with resources.zip beside level.dat, merged with existing resources.zip when present",
                "covers": "same text as resource-pack-only, with easier distribution to players",
                "limits": "does not cover hardcoded text unless it already uses translate keys",
            },
            {
                "name": "hybrid-keyed-copy",
                "world_data_changed": "copied map patched",
                "confirmation_required": False,
                "output": "copied map/world zip plus matching resource pack or embedded resources.zip, merged with existing resources.zip when present",
                "covers": "resource-pack units plus supported hardcoded JSON text components converted to translate keys",
                "limits": "does not cover direct-only plain strings or visual image/font text",
                "recommended_when": "hardcoded JSON text exists" if has_hybrid else "not required by current scan unless future QA finds hardcoded JSON text",
            },
            {
                "name": "direct-text-copy",
                "world_data_changed": "copied map patched",
                "confirmation_required": True,
                "output": "copied map/world zip with direct literal replacements",
                "covers": "supported plain command/SNBT/datapack JSON/NBT strings that cannot be key-injected; may start from a hybrid-keyed copy when both source kinds exist",
                "limits": "higher risk; exact anchors only; must be validated and reported separately",
                "recommended_when": "direct-only text remains" if has_direct else "not required by current scan unless residual audit finds direct-only text",
            },
        ],
        "full_translation_definition": {
            "not_a_fifth_mode": True,
            "meaning": "choose the least invasive one of the four export modes that covers the scanned player-facing text",
            "mode_selection_rules": [
                "resource-pack-only when all player-facing text is already reachable through language/resource keys",
                "embedded-pack-copy when resource-pack-only coverage is enough but the save should carry resources.zip",
                "hybrid-keyed-copy as the safest complete mode when hardcoded JSON text components exist",
                "direct-text-copy as the maximum-coverage mode when direct-only plain command/SNBT/datapack JSON/NBT strings remain and the user explicitly accepts the risk",
            ],
            "artifact_note": "A selected mode can emit several artifacts such as a map zip, resource-pack zip, resources.zip, apply reports, residual-English audit, and visual asset findings; these artifacts are not additional modes.",
            "existing_resource_pack_rule": "when the source map already includes resources.zip, embedded-pack-copy and hybrid-keyed-copy must merge generated translation files into that pack by default; replacing it would drop original textures, sounds, fonts, models, or custom assets",
            "resource_pack_only_is_full_only_when": "all player-facing text is already reachable through language/resource keys and no hardcoded/direct-only/visual text remains",
        },
        "common_output_artifacts": [
            "standalone resource-pack zip",
            "copied map with resources.zip, merged with the original map resource pack when one exists",
            "hybrid-keyed copied map zip when hardcoded text is translated",
            "QA/apply reports",
        ],
        "user_prompt": (
            "Choose one export mode: resource-pack-only, embedded-pack-copy, hybrid-keyed-copy "
            "(safest complete mode when hardcoded JSON text exists), or direct-text-copy "
            "(maximum coverage, explicit confirmation required)."
        ),
    }


def write_scan_review(path: Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Scan Review",
        "",
        f"- Source: `{report['source']}`",
        f"- Units: {report['unit_count']}",
        f"- Scanned files: {report['scanned_files']}",
        f"- Binary units: {report['binary_unit_count']}",
        f"- Pending/failed binary files: {len(report['pending_binary_parser_coverage'])}",
        f"- Encoding warnings: {report.get('encoding_warning_count', 0)}",
        f"- Excluded LastOutput strings: {report.get('discovery_counters', {}).get('excluded_last_output', 0)}",
        f"- Sign faces discovered: {report.get('discovery_counters', {}).get('sign_faces_seen', 0)}",
        f"- Aggregated sign groups: {report.get('discovery_counters', {}).get('aggregated_sign_groups', 0)}",
        f"- Sign faces without player text: {report.get('discovery_counters', {}).get('sign_faces_without_player_text', 0)}",
        f"- Identity-coupled units/groups: {report.get('identity_coupled', {}).get('unit_count', 0)}/{report.get('identity_coupled', {}).get('group_count', 0)}",
        f"- Structurally resolved/unresolved identity groups or units: {report.get('identity_coupled', {}).get('structural_group_count', 0)}/{report.get('identity_coupled', {}).get('unresolved_unit_count', 0)}",
        f"- Macro function lines: {report.get('discovery_counters', {}).get('macro_function_lines', 0)}",
        f"- Function calls: {report.get('function_call_count', 0)}",
        f"- Suspicious text hints: {report.get('suspicious_text_hint_count', 0)}",
        f"- Map resources.zip files: {report.get('map_resource_pack_count', 0)}",
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
    if report.get("encoding_warnings"):
        lines.extend(["", "## Encoding Warnings", ""])
        for warning in report["encoding_warnings"][:20]:
            lines.append(f"- {warning}")
    if report.get("function_call_graph"):
        lines.extend(["", "## Datapack Function Calls", ""])
        for call in report["function_call_graph"][:40]:
            lines.append(
                f"- `{call.get('function_id', '') or call.get('source_file', '')}` line {call.get('line', '')} -> `{call.get('target', '')}`"
            )
    if report.get("suspicious_text_hints"):
        lines.extend(["", "## Suspicious Text Hints", ""])
        lines.append("These strings look player-facing but were not promoted to normal apply units; inspect them during full localization QA.")
        for hint in report["suspicious_text_hints"][:40]:
            location = hint.get("json_path") or hint.get("line") or ""
            lines.append(
                f"- `{hint.get('kind', '')}` `{hint.get('source_file', '')}` {location}: `{hint.get('raw_preview', '')}`"
            )
    if report.get("map_resource_packs"):
        lines.extend(["", "## Existing Map Resource Packs", ""])
        lines.append(
            "The source map already contains resources.zip. Embedded exports must merge generated language files into the copied pack instead of replacing it, or original textures/sounds/fonts/models may be lost."
        )
        for pack in report["map_resource_packs"][:20]:
            lines.append(f"- `{pack.get('path', '')}` ({pack.get('size', 0)} bytes)")
    visual = report.get("visual_text_asset_hints", {})
    if visual.get("total", 0) or visual.get("png_texture_inventory_count", 0) or visual.get("model_json_inventory_count", 0):
        lines.extend(["", "## Resource-Pack Visual Text Hints", ""])
        lines.append(
            "PNG/model warnings are path-filtered candidates, not every asset. Confirm candidates with OCR or visual inspection; font providers remain structural leads."
        )
        lines.append(f"- PNG texture inventory (not all text candidates): {visual.get('png_texture_inventory_count', 0)}")
        lines.append(f"- Model JSON inventory (not all text candidates): {visual.get('model_json_inventory_count', 0)}")
        for key, count in sorted(visual.get("counts", {}).items()):
            lines.append(f"- `{key}`: {count}")
        for sample in visual.get("samples", [])[:30]:
            lines.append(f"- `{sample.get('kind', '')}` `{sample.get('path', '')}`")
    recommendation = report.get("full_localization_recommendation", {})
    if recommendation:
        lines.extend(["", "## Export Mode Choices", ""])
        lines.append("Explain these four export modes to the user before major translation/export work:")
        for mode in recommendation.get("export_modes", []):
            lines.append(
                f"- `{mode.get('name', '')}`: output={mode.get('output', '')}; covers={mode.get('covers', '')}; limits={mode.get('limits', '')}."
            )
        full_definition = recommendation.get("full_translation_definition", {})
        if full_definition:
            lines.extend(["", "## Full Translation Definition", ""])
            lines.append(
                "`Full translation` is not a fifth export mode. It means choosing the least invasive one of the four modes that covers the scanned player-facing text."
            )
            rules = full_definition.get("mode_selection_rules", [])
            if rules:
                lines.append("Mode selection rules:")
                for item in rules:
                    lines.append(f"- {item}")
            artifact_note = full_definition.get("artifact_note")
            if artifact_note:
                lines.append(f"Artifact note: {artifact_note}")
            existing_pack_rule = full_definition.get("existing_resource_pack_rule")
            if existing_pack_rule:
                lines.append(f"Existing resource-pack rule: {existing_pack_rule}.")
            condition = full_definition.get("resource_pack_only_is_full_only_when")
            if condition:
                lines.append(f"Resource-pack-only is complete only when {condition}.")
        lines.extend(["", "## Full Translation Mode Prompt", ""])
        if recommendation.get("suggest_full_translation_mode"):
            lines.append(
                "Ask the user which export mode to use. Recommend hybrid-keyed-copy as the safest complete mode when hardcoded JSON text exists."
            )
            lines.append(
                "Recommend direct-text-copy only for maximum coverage when direct-only plain command/SNBT/datapack JSON/NBT strings remain, and require explicit confirmation before producing it."
            )
            for reason in recommendation.get("reasons", []):
                lines.append(f"- {reason}")
        else:
            lines.append("Resource-pack-only export appears sufficient for currently scanned text, subject to residual-English and in-game QA.")
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
    discovery_counters: Counter[str] = Counter()
    visual_counts: Counter[str] = Counter()
    visual_samples: list[dict[str, str]] = []
    png_texture_inventory_count = 0
    model_json_inventory_count = 0
    map_resource_packs: list[dict[str, Any]] = []
    suspicious_text_hints: list[dict[str, Any]] = []
    function_calls: list[dict[str, Any]] = []

    for entry in iter_entries(source):
        scanned_files += 1
        lowered = entry.path.lower()
        if "!" not in entry.path and PurePosixPath(entry.path).name.lower() == "resources.zip":
            map_resource_packs.append({"path": entry.path, "size": entry.size})
        if RESOURCE_PNG_RE.search(entry.path.lower()):
            png_texture_inventory_count += 1
        if RESOURCE_MODEL_JSON_RE.search(entry.path.lower()):
            model_json_inventory_count += 1
        visual_kind = visual_text_asset_kind(entry.path)
        if visual_kind:
            visual_counts[visual_kind] += 1
            if len(visual_samples) < 100:
                visual_samples.append({"kind": visual_kind, "path": entry.path})
        try:
            units.extend(scan_lang_file(entry, args.source_locale))
            if lowered.endswith(".mcfunction"):
                units.extend(
                    scan_mcfunction(
                        entry,
                        namespace,
                        map_slug,
                        counters=discovery_counters,
                        suspicious_hints=suspicious_text_hints,
                        function_calls=function_calls,
                    )
                )
            elif lowered.endswith(".json"):
                units.extend(scan_json_file(entry, namespace, map_slug, suspicious_hints=suspicious_text_hints))
            elif is_binary_world_data(entry.path):
                if args.no_binary:
                    pending_binary.append(entry.path)
                else:
                    before = len(units)
                    binary_found, binary_errors = scan_binary_entry(
                        entry,
                        namespace,
                        map_slug,
                        include_last_output=args.include_last_output,
                        counters=discovery_counters,
                    )
                    units.extend(binary_found)
                    binary_units += len(units) - before
                    if binary_errors:
                        pending_binary.append(entry.path)
                        warnings.extend(binary_errors[: args.max_binary_errors])
        except Exception as exc:
            warnings.append(f"{entry.path}: {exc}")

    identity_summary = canonicalize_identity_keys(units, namespace, map_slug)
    unit_path = out / "translation_units.jsonl"
    write_jsonl(unit_path, units)
    identity_review_path = out / "identity_review.json"
    write_identity_review_template(
        identity_review_path,
        units,
        namespace=namespace,
        map_slug=map_slug,
    )
    encoding_warnings: list[str] = [
        warning
        for warning in warnings
        if "invalid UTF-8" in warning or "cannot decode" in warning or "UnicodeDecodeError" in warning
    ]
    for row in units:
        encoding_warnings.extend(unit_encoding_errors(row))

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

    visual_hints = {
        "total": sum(visual_counts.values()),
        "counts": dict(sorted(visual_counts.items())),
        "samples": visual_samples,
        "png_texture_inventory_count": png_texture_inventory_count,
        "model_json_inventory_count": model_json_inventory_count,
        "note": "PNG/model candidates are path-filtered for likely text-bearing assets; inventory counts are not themselves text warnings. Confirm candidates with OCR or visual inspection.",
    }
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
        "encoding_warning_count": len(encoding_warnings),
        "encoding_warnings": encoding_warnings[:200],
        "include_last_output": bool(args.include_last_output),
        "discovery_counters": dict(sorted(discovery_counters.items())),
        "visual_text_asset_hints": visual_hints,
        "suspicious_text_hints": suspicious_text_hints[:200],
        "suspicious_text_hint_count": len(suspicious_text_hints),
        "function_call_graph": function_calls[:1000],
        "function_call_count": len(function_calls),
        "map_resource_packs": map_resource_packs,
        "map_resource_pack_count": len(map_resource_packs),
        "identity_coupled": identity_summary,
        "identity_review_file": str(identity_review_path),
        "direct_only_unit_count": sum(
            1
            for row in units
            if "embedded-direct" in row.get("mode_support", []) and "hybrid-key-injection" not in row.get("mode_support", [])
        ),
        "plain_command_string_unit_count": sum(
            1
            for row in units
            if isinstance(row.get("address"), dict)
            and isinstance(row["address"].get("command_string_span"), list)
            and not isinstance(row["address"].get("json_path"), str)
        ),
        "counts_by_kind": count_by(units, "source_kind"),
        "counts_by_mode": count_modes(units),
        "top_source_files": top_counts(units, "source_file"),
        "top_raw": top_counts(units, "raw"),
    }
    report["full_localization_recommendation"] = full_localization_recommendation(report)
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


def has_english_words(value: str) -> bool:
    if len(value.strip()) < 3:
        return False
    stripped = value.strip()
    if is_probably_internal(stripped):
        return False
    scrubbed = stripped
    for token in protected_tokens(stripped):
        scrubbed = scrubbed.replace(token, " ")
    return bool(ENGLISH_WORD_RE.search(scrubbed))


def audit_preview(value: str, limit: int = 240) -> str:
    clean = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return clean[: limit - 3] + "..." if len(clean) > limit else clean


def audit_add(
    findings: list[dict[str, Any]],
    *,
    max_findings: int,
    source_file: str,
    location: dict[str, Any],
    source_kind: str,
    raw: str,
    confidence: str = "medium",
) -> None:
    if len(findings) >= max_findings:
        return
    if not has_english_words(raw):
        return
    findings.append(
        {
            "source_file": source_file,
            "location": location,
            "source_kind": source_kind,
            "raw_preview": audit_preview(raw),
            "confidence": confidence,
        }
    )


def audit_add_rows(
    rows: list[dict[str, Any]],
    *,
    findings: list[dict[str, Any]],
    max_findings: int,
    default_confidence: str = "low",
) -> None:
    for row in rows:
        if row.get("source_kind") == "text_component_translate":
            continue
        audit_add(
            findings,
            max_findings=max_findings,
            source_file=str(row.get("source_file", "")),
            location=row.get("address", {}) if isinstance(row.get("address"), dict) else {},
            source_kind=str(row.get("source_kind", "unknown")),
            raw=str(row.get("raw", "")),
            confidence=str(row.get("confidence", default_confidence)) or default_confidence,
        )


def audit_add_suspicious_hints(
    hints: list[dict[str, Any]],
    *,
    findings: list[dict[str, Any]],
    max_findings: int,
) -> None:
    for hint in hints:
        location: dict[str, Any] = {}
        if isinstance(hint.get("line"), int):
            location["line"] = hint["line"]
        if isinstance(hint.get("json_path"), str):
            location["json_path"] = hint["json_path"]
        if isinstance(hint.get("function_id"), str):
            location["function_id"] = hint["function_id"]
        audit_add(
            findings,
            max_findings=max_findings,
            source_file=str(hint.get("source_file", "")),
            location=location,
            source_kind=str(hint.get("kind", "suspicious_text")),
            raw=str(hint.get("raw_preview", "")),
            confidence="low",
        )


def nbt_path_is_audit_candidate(path: str) -> bool:
    lowered = path.lower()
    hints = [
        ".command",
        "front_text.messages",
        "back_text.messages",
        ".text1",
        ".text2",
        ".text3",
        ".text4",
        "customname",
        "custom_name",
        "display.name",
        "display.lore",
        ".lore",
        ".pages",
        "written_book_content.pages",
        "title",
        "subtitle",
        "bossbar",
    ]
    if any(hint in lowered for hint in hints):
        return True
    leaf = nbt_path_leaf(path)
    return leaf == "text" and (".entities[" in lowered or ".block_entities[" in lowered or ".blockentities[" in lowered)


def json_path_is_audit_candidate(path: str) -> bool:
    lowered = path.lower()
    return any(
        lowered.endswith(suffix)
        for suffix in (
            ".text",
            ".title",
            ".subtitle",
            ".description",
            ".name",
            ".lore",
            ".message",
            ".messages",
        )
    ) or ".pages[" in lowered


def iter_json_strings(obj: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from iter_json_strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_json_strings(value, f"{path}[{index}]")


def audit_nbt_string(
    item: NbtString,
    *,
    source_file: str,
    findings: list[dict[str, Any]],
    max_findings: int,
) -> None:
    source_kind = source_kind_from_nbt_path(item.path, item.value)
    base_location: dict[str, Any] = {"nbt_path": item.path}
    if item.chunk:
        base_location["chunk"] = item.chunk

    if ".command" in item.path.lower() or COMMAND_START_RE.match(item.value):
        rows = scan_command_line(
            item.value,
            source_file=source_file,
            base_address=base_location,
            namespace="audit",
            map_slug="audit",
            fallback_kind=source_kind,
            confidence="low",
        )
        for row in rows:
            if row.get("source_kind") == "text_component_translate":
                continue
            audit_add(
                findings,
                max_findings=max_findings,
                source_file=source_file,
                location=row.get("address", base_location),
                source_kind=str(row.get("source_kind", source_kind)),
                raw=str(row.get("raw", "")),
                confidence="low",
            )
        return

    obj = parse_component_string(item.value)
    if obj is not None:
        rows = extract_text_components(
            obj,
            source_file=source_file,
            source_kind=source_kind,
            base_address=base_location,
            json_path="$",
            namespace="audit",
            map_slug="audit",
            confidence="low",
        )
        for row in rows:
            if row.get("source_kind") == "text_component_translate":
                continue
            audit_add(
                findings,
                max_findings=max_findings,
                source_file=source_file,
                location=row.get("address", base_location),
                source_kind=str(row.get("source_kind", source_kind)),
                raw=str(row.get("raw", "")),
                confidence="low",
            )
        return

    if nbt_path_is_audit_candidate(item.path):
        audit_add(
            findings,
            max_findings=max_findings,
            source_file=source_file,
            location=base_location,
            source_kind=source_kind,
            raw=item.value,
        )


def audit_binary_entry(
    entry: Entry,
    *,
    findings: list[dict[str, Any]],
    max_findings: int,
    include_last_output: bool,
) -> list[str]:
    errors: list[str] = []
    if entry.data is None:
        return [f"{entry.path}: no data"]
    lowered = entry.path.lower()
    try:
        if lowered.endswith(".dat"):
            items = scan_nbt_strings(decompress_dat_payload(entry.data))
            rows = scan_nbt_items(
                items,
                source_file=entry.path,
                namespace="audit",
                map_slug="audit",
                include_last_output=include_last_output,
                counters=Counter(),
            )
            audit_add_rows(rows, findings=findings, max_findings=max_findings)
        elif lowered.endswith(".mca"):
            blobs, region_errors = iter_region_nbt(entry)
            errors.extend(region_errors)
            for chunk, nbt_data in blobs:
                try:
                    rows = scan_nbt_items(
                        scan_nbt_strings(nbt_data, chunk=chunk),
                        source_file=entry.path,
                        namespace="audit",
                        map_slug="audit",
                        include_last_output=include_last_output,
                        counters=Counter(),
                    )
                    audit_add_rows(rows, findings=findings, max_findings=max_findings)
                except Exception as exc:
                    errors.append(f"{entry.path}: chunk {chunk.get('local_index')}: {exc}")
    except Exception as exc:
        errors.append(f"{entry.path}: {exc}")
    return errors


def write_audit_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Residual English Audit",
        "",
        f"- Source: `{report['source']}`",
        f"- Scanned files: {report['scanned_files']}",
        f"- Findings: {report['finding_count']}",
        f"- Candidate findings before limit: {report.get('candidate_finding_count', report['finding_count'])}",
        f"- Skipped language files: {report.get('skipped_language_file_count', 0)}",
        f"- Warnings: {len(report.get('warnings', []))}",
        "",
    ]
    visual = report.get("visual_text_asset_hints", {})
    if visual.get("total", 0) or visual.get("png_texture_inventory_count", 0) or visual.get("model_json_inventory_count", 0):
        lines.extend(["## Visual Asset Hints", ""])
        lines.append(f"- PNG texture inventory (not all text candidates): {visual.get('png_texture_inventory_count', 0)}")
        lines.append(f"- Model JSON inventory (not all text candidates): {visual.get('model_json_inventory_count', 0)}")
        for key, count in sorted(visual.get("counts", {}).items()):
            lines.append(f"- `{key}`: {count}")
        for sample in visual.get("samples", [])[:30]:
            lines.append(f"- `{sample.get('kind', '')}` `{sample.get('path', '')}`")
        lines.append("")
    lines.extend(["## Findings", ""])
    for item in report.get("findings", [])[:200]:
        location = json.dumps(item.get("location", {}), ensure_ascii=False, sort_keys=True)
        lines.append(
            f"- `{item.get('source_kind', '')}` `{item.get('source_file', '')}` {location}: `{item.get('raw_preview', '')}`"
        )
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in report["warnings"][:50]:
            lines.append(f"- {warning}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def audit_finding_priority(item: dict[str, Any]) -> tuple[int, str, str]:
    source_kind = str(item.get("source_kind", ""))
    priorities = {
        "sign": 0,
        "tellraw": 1,
        "title": 1,
        "actionbar": 1,
        "bossbar": 1,
        "command_block": 1,
        "text_display": 1,
        "book": 2,
        "item_name": 2,
        "item_lore": 2,
        "entity_name": 2,
        "storage_text": 3,
        "function": 3,
        "datapack_json": 3,
        "lang": 8,
    }
    return priorities.get(source_kind, 5), str(item.get("source_file", "")), str(item.get("raw_preview", ""))


def audit_english(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    out = Path(args.out).resolve()
    if args.target_locale:
        require_locale(args.target_locale, "--target-locale")
    if args.source_locale:
        require_locale(args.source_locale, "--source-locale")
    out.parent.mkdir(parents=True, exist_ok=True)
    findings: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned_files = 0
    visual_counts: Counter[str] = Counter()
    visual_samples: list[dict[str, str]] = []
    png_texture_inventory_count = 0
    model_json_inventory_count = 0
    skipped_language_files: list[str] = []
    collection_limit = max(args.max_findings * 20, args.max_findings, 1000)

    for entry in iter_entries(source):
        scanned_files += 1
        lowered = entry.path.lower()
        if RESOURCE_PNG_RE.search(lowered):
            png_texture_inventory_count += 1
        if RESOURCE_MODEL_JSON_RE.search(lowered):
            model_json_inventory_count += 1
        visual_kind = visual_text_asset_kind(entry.path)
        if visual_kind:
            visual_counts[visual_kind] += 1
            if len(visual_samples) < 100:
                visual_samples.append({"kind": visual_kind, "path": entry.path})
        try:
            if lowered.endswith(".mcfunction") and entry.data is not None:
                suspicious_hints: list[dict[str, Any]] = []
                rows = scan_mcfunction(
                    entry,
                    "audit",
                    "audit",
                    counters=Counter(),
                    suspicious_hints=suspicious_hints,
                    function_calls=[],
                )
                audit_add_rows(rows, findings=findings, max_findings=collection_limit)
                audit_add_suspicious_hints(suspicious_hints, findings=findings, max_findings=collection_limit)
            elif lowered.endswith(".json") and entry.data is not None:
                text = decode_text(entry.data, entry.path)
                if text is None:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                is_lang = bool(LANG_PATH_RE.match(entry.path))
                if is_lang:
                    match = LANG_PATH_RE.match(entry.path)
                    locale = match.group(2) if match else ""
                    include_lang = bool(args.target_locale and locale == args.target_locale)
                    if locale == args.source_locale and not args.include_source_language:
                        include_lang = False
                    if not include_lang:
                        skipped_language_files.append(entry.path)
                        continue
                    for json_path, value in iter_json_strings(obj):
                        audit_add(
                            findings,
                            max_findings=collection_limit,
                            source_file=entry.path,
                            location={"json_path": json_path},
                            source_kind="lang",
                            raw=value,
                            confidence="low",
                        )
                else:
                    suspicious_hints = []
                    rows = scan_json_file(entry, "audit", "audit", suspicious_hints=suspicious_hints)
                    audit_add_rows(rows, findings=findings, max_findings=collection_limit, default_confidence="medium")
                    audit_add_suspicious_hints(suspicious_hints, findings=findings, max_findings=collection_limit)
            elif is_binary_world_data(entry.path):
                warnings.extend(
                    audit_binary_entry(
                        entry,
                        findings=findings,
                        max_findings=collection_limit,
                        include_last_output=args.include_last_output,
                    )
                )
        except Exception as exc:
            warnings.append(f"{entry.path}: {exc}")

    candidate_finding_count = len(findings)
    findings.sort(key=audit_finding_priority)
    findings = findings[: args.max_findings]
    report = {
        "schema": "mc-map-residual-english-audit.v1",
        "created_at": utc_now(),
        "source": str(source),
        "scanned_files": scanned_files,
        "finding_count": len(findings),
        "candidate_finding_count": candidate_finding_count,
        "max_findings": args.max_findings,
        "include_last_output": bool(args.include_last_output),
        "target_locale": args.target_locale,
        "source_locale": args.source_locale,
        "include_source_language": bool(args.include_source_language),
        "skipped_language_file_count": len(skipped_language_files),
        "skipped_language_files": skipped_language_files[:200],
        "findings": findings,
        "warnings": warnings,
        "visual_text_asset_hints": {
            "total": sum(visual_counts.values()),
            "counts": dict(sorted(visual_counts.items())),
            "samples": visual_samples,
            "png_texture_inventory_count": png_texture_inventory_count,
            "model_json_inventory_count": model_json_inventory_count,
            "note": "PNG/model candidates are path-filtered; verify them with OCR or visual inspection instead of treating every asset as text-bearing.",
        },
    }
    write_json(out, report)
    markdown_path = out.with_suffix(".md")
    write_audit_markdown(markdown_path, report)
    print(f"audit_report: {out}")
    print(f"audit_review: {markdown_path}")
    print(f"findings: {len(findings)}")
    return 0


class ApplyState:
    def __init__(self, *, dry_run: bool, multi_text_mode: str):
        self.dry_run = dry_run
        self.multi_text_mode = multi_text_mode
        self.changed_units = 0
        self.already_applied = 0
        self.no_op_units = 0
        self.skipped: Counter[str] = Counter()
        self.skipped_samples: list[dict[str, str]] = []
        self.status_by_id: dict[str, str] = {}
        self.changed_files: set[str] = set()
        self.outcomes_by_type: dict[str, Counter[str]] = defaultdict(Counter)

    def row_id(self, row: dict[str, Any]) -> str:
        return str(row.get("id") or id(row))

    def row_type(self, row: dict[str, Any]) -> str:
        return apply_unit_type(row)

    def mark_changed(self, row: dict[str, Any], source_file: str) -> None:
        row_id = self.row_id(row)
        if self.status_by_id.get(row_id) == "changed":
            return
        self.status_by_id[row_id] = "changed"
        self.changed_units += 1
        self.changed_files.add(source_file)
        self.outcomes_by_type[self.row_type(row)]["changed"] += 1

    def mark_already(self, row: dict[str, Any]) -> None:
        row_id = self.row_id(row)
        if row_id in self.status_by_id:
            return
        self.status_by_id[row_id] = "already_applied"
        self.already_applied += 1
        self.outcomes_by_type[self.row_type(row)]["already"] += 1

    def mark_noop(self, row: dict[str, Any]) -> None:
        row_id = self.row_id(row)
        if row_id in self.status_by_id:
            return
        self.status_by_id[row_id] = "no_op"
        self.no_op_units += 1
        self.outcomes_by_type[self.row_type(row)]["no_op"] += 1

    def mark_skip(self, row: dict[str, Any], reason: str, detail: str = "") -> None:
        row_id = self.row_id(row)
        if row_id in self.status_by_id:
            return
        self.status_by_id[row_id] = f"skipped:{reason}"
        self.skipped[reason] += 1
        self.outcomes_by_type[self.row_type(row)]["skipped"] += 1
        if len(self.skipped_samples) < 100:
            self.skipped_samples.append(
                {
                    "id": str(row.get("id", "")),
                    "source_file": str(row.get("source_file", "")),
                    "reason": reason,
                    "detail": detail[:300],
                }
            )

    def type_report(self, selected_rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        selected = Counter(apply_unit_type(row) for row in selected_rows)
        result: dict[str, dict[str, int]] = {}
        for unit_type in sorted(set(selected) | set(self.outcomes_by_type)):
            outcomes = self.outcomes_by_type.get(unit_type, Counter())
            result[unit_type] = {
                "selected": selected[unit_type],
                "changed": outcomes["changed"],
                "no_op": outcomes["no_op"],
                "already": outcomes["already"],
                "skipped": outcomes["skipped"],
            }
        return result


def apply_unit_type(row: dict[str, Any]) -> str:
    if is_sign_group_row(row):
        return "sign_face"
    return str(row.get("source_kind", "unknown")) or "unknown"


def confidence_allows(row: dict[str, Any], min_confidence: str) -> bool:
    value = str(row.get("confidence", "low"))
    return CONFIDENCE_RANK.get(value, 0) >= CONFIDENCE_RANK[min_confidence]


def row_has_translation(row: dict[str, Any]) -> bool:
    return row_translation_complete(row)


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
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        normal_component = isinstance(address.get("json_path"), str) and isinstance(text_nodes, list) and bool(text_nodes)
        sign_group = is_sign_group_row(row)
        if not normal_component and not sign_group:
            skipped["not_json_text_component"] += 1
            continue
        selected.append(row)

    return selected, skipped


def is_sign_group_row(row: dict[str, Any]) -> bool:
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    if str(row.get("source_kind", "")) != "sign":
        return False
    if not isinstance(address.get("sign_lines"), list):
        return False
    return any(isinstance(segment, dict) and isinstance(segment.get("nbt_path"), str) for segment in row_segments(row))


def is_int_span(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], int)
        and isinstance(value[1], int)
        and value[0] < value[1]
    )


def direct_anchor_kind(row: dict[str, Any]) -> str:
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    source_file = str(row.get("source_file", "")).lower()
    if isinstance(address.get("json_path"), str):
        return ""

    if source_file.endswith((".dat", ".mca")) and isinstance(address.get("nbt_path"), str) and address.get("nbt_path"):
        return "nbt_plain_string"

    if source_file.endswith(".mcfunction") and isinstance(address.get("function_line"), int):
        if is_int_span(address.get("command_plain_span")):
            return "mcfunction_plain_span"
        if is_int_span(address.get("command_string_span")):
            return "mcfunction_string_span"
        if is_int_span(address.get("command_span")) and isinstance(address.get("command_json_path"), str):
            return "mcfunction_json_span_string"
        return ""

    if source_file.endswith(".json") and isinstance(address.get("json_string_path"), str):
        return "json_plain_string"

    return ""


def select_direct_text_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], Counter[str]]:
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

        if not direct_anchor_kind(row):
            skipped["unsupported_direct_anchor"] += 1
            continue

        translation = str(row.get("translation", ""))
        if not translation.strip() and not args.allow_empty_translation:
            skipped["missing_translation"] += 1
            continue
        if translation == str(row.get("raw", "")):
            if not translation_item_complete(row):
                skipped["unreviewed_same_as_source"] += 1
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


def patch_json_string_span(value: str, start: int, end: int, row: dict[str, Any], state: ApplyState) -> tuple[str, bool]:
    if start < 0 or end > len(value) or start >= end:
        state.mark_skip(row, "invalid_command_string_span")
        return value, False
    literal = value[start:end]
    if len(literal) < 2 or literal[0] not in {"'", '"'} or literal[-1] != literal[0]:
        state.mark_skip(row, "command_string_span_not_quoted")
        return value, False
    quote = literal[0]
    try:
        decoded = json.loads(literal) if quote == '"' else decode_snbt_single_quoted(literal[1:-1])
        obj = json.loads(str(decoded).strip())
    except json.JSONDecodeError as exc:
        state.mark_skip(row, "command_string_json_parse_failed", str(exc))
        return value, False
    changed, reason = inject_component_for_unit(obj, row, state.multi_text_mode)
    if not changed:
        if reason == "already_applied":
            state.mark_already(row)
        else:
            state.mark_skip(row, reason)
        return value, False
    state.mark_changed(row, str(row.get("source_file", "")))
    encoded = encode_snbt_string_literal(dump_json_component(obj), quote)
    return value[:start] + encoded + value[end:], True


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


def collect_nbt_string_tags(tag: NbtTag, path: str, out: dict[str, NbtTag]) -> None:
    if tag.tag_type == 8:
        out[path] = tag
        return
    if tag.tag_type == 9:
        _child_type, items = tag.value
        for index, child in enumerate(items):
            collect_nbt_string_tags(child, f"{path}[{index}]", out)
        return
    if tag.tag_type == 10:
        for name, child in tag.value:
            child_path = f"{path}.{name}" if path else name
            collect_nbt_string_tags(child, child_path, out)


def sign_segments_for_nbt_path(row: dict[str, Any], nbt_path: str) -> list[dict[str, Any]]:
    return [
        segment
        for segment in row_segments(row)
        if isinstance(segment, dict) and str(segment.get("nbt_path", "")) == nbt_path
    ]


def patch_sign_json_value(value: str, row: dict[str, Any], nbt_path: str) -> tuple[str, bool, str]:
    segments = sign_segments_for_nbt_path(row, nbt_path)
    if not segments:
        return value, False, "no_segments_for_sign_line"
    prefix_len = len(value) - len(value.lstrip())
    suffix_start = len(value.rstrip())
    prefix = value[:prefix_len]
    suffix = value[suffix_start:]
    payload = value[prefix_len:suffix_start]
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        return value, False, f"sign_line_json_parse_failed:{exc}"

    operations: list[tuple[dict[str, Any], str]] = []
    already = 0
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()
    for segment in segments:
        component_path = str(segment.get("component_json_path") or segment.get("json_path") or "")
        expected = str(segment.get("raw", ""))
        key = str(segment.get("translation_key", "")).strip()
        if not component_path or not key:
            return value, False, "missing_sign_segment_path_or_key"
        if component_path in seen_paths:
            return value, False, "duplicate_sign_segment_path"
        if key in seen_keys:
            return value, False, "duplicate_sign_segment_key"
        seen_paths.add(component_path)
        seen_keys.add(key)
        try:
            parent_path, leaf = parent_json_path(component_path)
            parent = get_json_path(obj, parent_path)
        except (KeyError, ValueError) as exc:
            return value, False, f"sign_segment_json_path_missing:{exc}"
        if leaf != "text" or not isinstance(parent, dict):
            return value, False, "sign_segment_not_object_text_field"
        if parent.get("translate") == key and "text" not in parent:
            already += 1
            continue
        if "translate" in parent and parent.get("translate") != key:
            return value, False, "existing_sign_segment_translate_conflict"
        if parent.get("text") != expected:
            return value, False, "sign_segment_source_text_mismatch"
        operations.append((parent, key))

    if already == len(segments):
        return value, False, "already_applied"
    for parent, key in operations:
        replace_text_with_translate(parent, key)
    return prefix + dump_json_component(obj) + suffix, True, "changed"


def patch_sign_group_rows(tree: NbtTree, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    if not rows:
        return False
    string_tags: dict[str, NbtTag] = {}
    collect_nbt_string_tags(tree.root, tree.root_path, string_tags)
    changed_any = False
    for row in rows:
        paths = sorted(
            {
                str(segment.get("nbt_path", ""))
                for segment in row_segments(row)
                if isinstance(segment, dict) and str(segment.get("nbt_path", ""))
            }
        )
        if not paths:
            state.mark_skip(row, "missing_sign_segment_nbt_paths")
            continue
        missing = [path for path in paths if path not in string_tags]
        if missing:
            state.mark_skip(row, "sign_line_nbt_path_missing", ", ".join(missing[:5]))
            continue

        patched_values: dict[str, str] = {}
        changed_row = False
        already_count = 0
        failure = ""
        for nbt_path in paths:
            current = str(string_tags[nbt_path].value)
            patched, changed, reason = patch_sign_json_value(current, row, nbt_path)
            if reason == "already_applied":
                already_count += 1
            elif reason != "changed":
                failure = reason
                break
            if changed:
                patched_values[nbt_path] = patched
                changed_row = True
        if failure:
            state.mark_skip(row, failure)
            continue
        if changed_row:
            for nbt_path, patched in patched_values.items():
                string_tags[nbt_path].value = patched
            state.mark_changed(row, str(row.get("source_file", "")))
            changed_any = True
        elif already_count == len(paths):
            state.mark_already(row)
        else:
            state.mark_skip(row, "sign_group_no_changes")
    return changed_any


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
        span_rows: list[tuple[int, int, str, dict[str, Any]]] = []
        for row in line_rows:
            span = row.get("address", {}).get("command_span")
            if (
                isinstance(span, list)
                and len(span) == 2
                and isinstance(span[0], int)
                and isinstance(span[1], int)
            ):
                span_rows.append((span[0], span[1], "json", row))
                continue
            string_span = row.get("address", {}).get("command_string_span")
            if (
                isinstance(string_span, list)
                and len(string_span) == 2
                and isinstance(string_span[0], int)
                and isinstance(string_span[1], int)
            ):
                span_rows.append((string_span[0], string_span[1], "json_string", row))
            else:
                state.mark_skip(row, "missing_command_span")
        for start, end, span_kind, row in sorted(span_rows, key=lambda item: item[0], reverse=True):
            if span_kind == "json_string":
                body, changed = patch_json_string_span(body, start, end, row, state)
            else:
                body, changed = patch_json_span(body, start, end, row, state)
            if changed:
                changed_file = True
        lines[line_no - 1] = body + eol

    if changed_file and not state.dry_run:
        path.write_text("".join(lines), encoding="utf-8")
    return changed_file


def patch_json_string_component_value(obj: Any, row: dict[str, Any], state: ApplyState) -> tuple[bool, str]:
    address = row.get("address") if isinstance(row.get("address"), dict) else {}
    json_string_path = address.get("json_string_path")
    if not isinstance(json_string_path, str) or not json_string_path:
        return False, "missing_json_string_path"
    try:
        current = get_json_path(obj, json_string_path)
    except (KeyError, ValueError) as exc:
        return False, f"json_string_path_missing:{exc}"
    if not isinstance(current, str):
        return False, "json_string_path_not_string"
    try:
        component = json.loads(current.strip())
    except json.JSONDecodeError as exc:
        return False, f"json_string_component_parse_failed:{exc}"
    changed, reason = inject_component_for_unit(component, row, state.multi_text_mode)
    if not changed:
        return False, reason
    try:
        set_json_path(obj, json_string_path, dump_json_component(component))
    except (KeyError, ValueError) as exc:
        return False, f"json_string_path_set_failed:{exc}"
    return True, "changed"


def patch_json_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        for row in rows:
            state.mark_skip(row, "json_file_parse_failed", str(exc))
        return False

    changed_file = False
    for row in rows:
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        if isinstance(address.get("json_string_path"), str):
            changed, reason = patch_json_string_component_value(obj, row, state)
        else:
            changed, reason = inject_component_for_unit(obj, row, state.multi_text_mode)
        changed_file = note_json_apply_result(row, source_file, changed, reason, state) or changed_file

    if changed_file and not state.dry_run:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed_file


def patch_direct_mcfunction_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
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
        span_rows: list[tuple[int, int, str, dict[str, Any]]] = []
        for row in line_rows:
            address = row.get("address", {}) if isinstance(row.get("address"), dict) else {}
            command_span = address.get("command_span")
            if is_int_span(command_span) and isinstance(address.get("command_json_path"), str):
                span_rows.append((command_span[0], command_span[1], "json_plain", row))
                continue
            plain_span = address.get("command_plain_span")
            if is_int_span(plain_span):
                span_rows.append((plain_span[0], plain_span[1], "plain", row))
                continue
            string_span = address.get("command_string_span")
            if is_int_span(string_span):
                span_rows.append((string_span[0], string_span[1], "string", row))
            else:
                state.mark_skip(row, "missing_direct_command_span")

        for start, end, span_kind, group_rows in sorted(grouped_span_rows(span_rows), key=lambda item: item[0], reverse=True):
            if span_kind == "json_plain":
                body, changed = patch_direct_command_json_span_rows(body, start, end, group_rows, state)
            elif len(group_rows) > 1:
                mark_direct_rows_skipped(group_rows, state, "direct_span_conflict")
                changed = False
            elif span_kind == "plain":
                body, changed = patch_direct_plain_span(body, start, end, group_rows[0], state)
            else:
                body, changed = patch_direct_command_string_span(body, start, end, group_rows[0], state)
            if changed:
                changed_file = True
        lines[line_no - 1] = body + eol

    if changed_file and not state.dry_run:
        path.write_text("".join(lines), encoding="utf-8")
    return changed_file


def patch_direct_json_file(path: Path, source_file: str, rows: list[dict[str, Any]], state: ApplyState) -> bool:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        for row in rows:
            state.mark_skip(row, "json_file_parse_failed", str(exc))
        return False

    changed_file = False
    seen_paths: set[str] = set()
    for row in rows:
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        json_string_path = address.get("json_string_path")
        if not isinstance(json_string_path, str) or not json_string_path:
            state.mark_skip(row, "missing_json_string_path")
            continue
        if json_string_path in seen_paths:
            state.mark_skip(row, "duplicate_json_string_path")
            continue
        seen_paths.add(json_string_path)
        try:
            current = get_json_path(obj, json_string_path)
        except (KeyError, ValueError) as exc:
            state.mark_skip(row, "json_string_path_missing", str(exc))
            continue
        if not isinstance(current, str):
            state.mark_skip(row, "json_string_path_not_string")
            continue
        raw = str(row.get("raw", ""))
        translation = str(row.get("translation", ""))
        if current == translation:
            mark_direct_current_translation(row, state)
            continue
        if current != raw:
            state.mark_skip(row, "source_text_mismatch", f"expected {raw[:120]!r}, found {current[:120]!r}")
            continue
        try:
            set_json_path(obj, json_string_path, translation)
        except (KeyError, ValueError) as exc:
            state.mark_skip(row, "json_string_path_set_failed", str(exc))
            continue
        state.mark_changed(row, source_file)
        changed_file = True

    if changed_file and not state.dry_run:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed_file


def patch_nbt_string_value(value: str, rows: list[dict[str, Any]], state: ApplyState) -> tuple[str, bool]:
    changed_any = False
    command_rows: list[tuple[int, int, str, dict[str, Any]]] = []
    full_json_rows: list[dict[str, Any]] = []
    for row in rows:
        span = row.get("address", {}).get("command_span")
        if (
            isinstance(span, list)
            and len(span) == 2
            and isinstance(span[0], int)
            and isinstance(span[1], int)
        ):
            command_rows.append((span[0], span[1], "json", row))
            continue
        string_span = row.get("address", {}).get("command_string_span")
        if (
            isinstance(string_span, list)
            and len(string_span) == 2
            and isinstance(string_span[0], int)
            and isinstance(string_span[1], int)
        ):
            command_rows.append((string_span[0], string_span[1], "json_string", row))
        else:
            full_json_rows.append(row)

    for start, end, span_kind, row in sorted(command_rows, key=lambda item: item[0], reverse=True):
        if span_kind == "json_string":
            value, changed = patch_json_string_span(value, start, end, row, state)
        else:
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
    sign_rows = [row for row in rows if is_sign_group_row(row)]
    normal_rows = [row for row in rows if not is_sign_group_row(row)]
    sign_changed = patch_sign_group_rows(tree, sign_rows, state)

    rows_by_nbt_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in normal_rows:
        nbt_path = row.get("address", {}).get("nbt_path")
        if isinstance(nbt_path, str) and nbt_path:
            rows_by_nbt_path[nbt_path].append(row)
        else:
            state.mark_skip(row, "missing_nbt_path")

    changed = patch_nbt_tag_strings(tree.root, tree.root_path, rows_by_nbt_path, state)
    for row in normal_rows:
        if state.row_id(row) not in state.status_by_id:
            state.mark_skip(row, "nbt_path_missing")
    if not changed and not sign_changed:
        return data, False
    return write_nbt_tree(tree), True


def mark_direct_rows_skipped(rows: list[dict[str, Any]], state: ApplyState, reason: str, detail: str = "") -> None:
    for row in rows:
        state.mark_skip(row, reason, detail)


def mark_direct_rows_already(rows: list[dict[str, Any]], state: ApplyState) -> None:
    for row in rows:
        mark_direct_current_translation(row, state)


def mark_direct_current_translation(row: dict[str, Any], state: ApplyState) -> None:
    if str(row.get("raw", "")) == str(row.get("translation", "")):
        state.mark_noop(row)
    else:
        state.mark_already(row)


def mark_direct_rows_changed(rows: list[dict[str, Any]], state: ApplyState) -> None:
    for row in rows:
        state.mark_changed(row, str(row.get("source_file", "")))


def patch_direct_plain_span(
    value: str,
    start: int,
    end: int,
    row: dict[str, Any],
    state: ApplyState,
    *,
    max_container_utf8_bytes: int | None = None,
) -> tuple[str, bool]:
    if start < 0 or end > len(value) or start >= end:
        state.mark_skip(row, "invalid_command_plain_span")
        return value, False
    raw = str(row.get("raw", ""))
    translation = str(row.get("translation", ""))
    current = value[start:end]
    if current == translation:
        mark_direct_current_translation(row, state)
        return value, False
    if current != raw:
        state.mark_skip(row, "source_text_mismatch", f"expected {raw[:120]!r}, found {current[:120]!r}")
        return value, False
    candidate = value[:start] + translation + value[end:]
    if max_container_utf8_bytes is not None and len(candidate.encode("utf-8")) > max_container_utf8_bytes:
        state.mark_skip(row, "translation_too_long_for_nbt_string")
        return value, False
    state.mark_changed(row, str(row.get("source_file", "")))
    return candidate, True


def patch_direct_command_json_span_rows(
    value: str,
    start: int,
    end: int,
    rows: list[dict[str, Any]],
    state: ApplyState,
    *,
    max_container_utf8_bytes: int | None = None,
) -> tuple[str, bool]:
    if start < 0 or end > len(value) or start >= end:
        mark_direct_rows_skipped(rows, state, "invalid_command_span")
        return value, False
    try:
        obj = json.loads(value[start:end])
    except json.JSONDecodeError as exc:
        mark_direct_rows_skipped(rows, state, "command_span_json_parse_failed", str(exc))
        return value, False

    operations: list[tuple[str, str, dict[str, Any]]] = []
    already: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for row in rows:
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        json_path = address.get("command_json_path")
        if not isinstance(json_path, str) or not json_path:
            state.mark_skip(row, "missing_command_json_path")
            continue
        if json_path in seen_paths:
            state.mark_skip(row, "duplicate_command_json_path")
            continue
        seen_paths.add(json_path)
        try:
            current = get_json_path(obj, json_path)
        except (KeyError, ValueError) as exc:
            state.mark_skip(row, "command_json_path_missing", str(exc))
            continue
        if not isinstance(current, str):
            state.mark_skip(row, "command_json_path_not_string")
            continue
        raw = str(row.get("raw", ""))
        translation = str(row.get("translation", ""))
        if current == translation:
            already.append(row)
        elif current != raw:
            state.mark_skip(row, "source_text_mismatch", f"expected {raw[:120]!r}, found {current[:120]!r}")
        else:
            operations.append((json_path, translation, row))

    if not operations:
        for row in already:
            mark_direct_current_translation(row, state)
        return value, False

    for json_path, translation, _row in operations:
        try:
            set_json_path(obj, json_path, translation)
        except (KeyError, ValueError) as exc:
            for _json_path, _translation, op_row in operations:
                state.mark_skip(op_row, "command_json_path_set_failed", str(exc))
            return value, False
    replacement = dump_json_component(obj)
    candidate = value[:start] + replacement + value[end:]
    if max_container_utf8_bytes is not None and len(candidate.encode("utf-8")) > max_container_utf8_bytes:
        for _json_path, _translation, row in operations:
            state.mark_skip(row, "translation_too_long_for_nbt_string")
        return value, False
    for row in already:
        mark_direct_current_translation(row, state)
    for _json_path, _translation, row in operations:
        state.mark_changed(row, str(row.get("source_file", "")))
    return candidate, True


def grouped_span_rows(span_rows: list[tuple[int, int, str, dict[str, Any]]]) -> list[tuple[int, int, str, list[dict[str, Any]]]]:
    groups: dict[tuple[int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for start, end, kind, row in span_rows:
        groups[(start, end, kind)].append(row)
    return [(start, end, kind, rows) for (start, end, kind), rows in groups.items()]


def patch_direct_nbt_string_value(value: str, rows: list[dict[str, Any]], state: ApplyState) -> tuple[str, bool]:
    span_rows: list[tuple[int, int, str, dict[str, Any]]] = []
    full_rows: list[dict[str, Any]] = []
    for row in rows:
        command_span = row.get("address", {}).get("command_span")
        if is_int_span(command_span) and isinstance(row.get("address", {}).get("command_json_path"), str):
            span_rows.append((command_span[0], command_span[1], "json_plain", row))
            continue
        plain_span = row.get("address", {}).get("command_plain_span")
        if is_int_span(plain_span):
            span_rows.append((plain_span[0], plain_span[1], "plain", row))
            continue
        string_span = row.get("address", {}).get("command_string_span")
        if is_int_span(string_span):
            span_rows.append((string_span[0], string_span[1], "string", row))
        else:
            full_rows.append(row)

    changed_any = False
    for start, end, span_kind, group_rows in sorted(grouped_span_rows(span_rows), key=lambda item: item[0], reverse=True):
        if span_kind == "json_plain":
            value, changed = patch_direct_command_json_span_rows(
                value,
                start,
                end,
                group_rows,
                state,
                max_container_utf8_bytes=65535,
            )
        elif len(group_rows) > 1:
            mark_direct_rows_skipped(group_rows, state, "direct_span_conflict")
            changed = False
        elif span_kind == "plain":
            value, changed = patch_direct_plain_span(
                value,
                start,
                end,
                group_rows[0],
                state,
                max_container_utf8_bytes=65535,
            )
        else:
            value, changed = patch_direct_command_string_span(
                value,
                start,
                end,
                group_rows[0],
                state,
                max_container_utf8_bytes=65535,
            )
        changed_any = changed or changed_any

    if span_rows:
        if full_rows:
            mark_direct_rows_skipped(full_rows, state, "mixed_command_span_and_full_nbt_string")
        return value, changed_any

    rows = full_rows
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


def patch_direct_command_string_span(
    value: str,
    start: int,
    end: int,
    row: dict[str, Any],
    state: ApplyState,
    *,
    max_container_utf8_bytes: int | None = None,
) -> tuple[str, bool]:
    if start < 0 or end > len(value) or start >= end:
        state.mark_skip(row, "invalid_command_string_span")
        return value, False
    literal = value[start:end]
    if len(literal) < 2 or literal[0] not in {"'", '"'} or literal[-1] != literal[0]:
        state.mark_skip(row, "command_string_span_not_quoted")
        return value, False
    quote = literal[0]
    try:
        decoded = json.loads(literal) if quote == '"' else decode_snbt_single_quoted(literal[1:-1])
    except json.JSONDecodeError as exc:
        state.mark_skip(row, "command_string_decode_failed", str(exc))
        return value, False
    raw = str(row.get("raw", ""))
    translation = str(row.get("translation", ""))
    if decoded == translation:
        mark_direct_current_translation(row, state)
        return value, False
    if decoded != raw:
        state.mark_skip(row, "source_text_mismatch", f"expected {raw[:120]!r}, found {str(decoded)[:120]!r}")
        return value, False
    encoded = encode_snbt_string_literal(translation, quote)
    candidate = value[:start] + encoded + value[end:]
    if max_container_utf8_bytes is not None and len(candidate.encode("utf-8")) > max_container_utf8_bytes:
        state.mark_skip(row, "translation_too_long_for_nbt_string")
        return value, False
    state.mark_changed(row, str(row.get("source_file", "")))
    return candidate, True


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


def resource_pack_file_entries(src: Path) -> list[tuple[Path, str]]:
    if not (src / "pack.mcmeta").exists():
        raise ValueError(f"resource pack root is missing pack.mcmeta: {src}")
    return [(path, path.relative_to(src).as_posix()) for path in sorted(p for p in src.rglob("*") if p.is_file())]


def zip_resource_pack_dir(
    src: Path,
    out: Path,
    *,
    base_zip: Path | None = None,
    replace_existing_resource_pack: bool = False,
) -> dict[str, Any]:
    overlay_entries = resource_pack_file_entries(src)
    overlay_names = {rel for _, rel in overlay_entries}
    out.parent.mkdir(parents=True, exist_ok=True)

    base_data: bytes | None = None
    if base_zip and base_zip.exists() and not replace_existing_resource_pack:
        base_data = base_zip.read_bytes()

    report = {
        "mode": "replace" if replace_existing_resource_pack else "merge" if base_data is not None else "create",
        "base_resource_pack": str(base_zip) if base_data is not None and base_zip else "",
        "output": str(out),
        "base_entry_count": 0,
        "overlay_entry_count": len(overlay_entries),
        "overwritten_entry_count": 0,
        "duplicate_base_entry_count": 0,
        "preserved_base_pack_mcmeta": False,
    }

    tmp = out.with_name(f"{out.name}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            written: set[str] = set()
            if base_data is not None:
                try:
                    with zipfile.ZipFile(io.BytesIO(base_data)) as base_archive:
                        for info in sorted(base_archive.infolist(), key=lambda item: item.filename):
                            if info.is_dir():
                                continue
                            rel = to_posix(info.filename)
                            if rel in written:
                                report["duplicate_base_entry_count"] += 1
                                continue
                            if rel == "pack.mcmeta":
                                archive.writestr(rel, base_archive.read(info))
                                written.add(rel)
                                report["base_entry_count"] += 1
                                report["preserved_base_pack_mcmeta"] = True
                                continue
                            if rel in overlay_names:
                                report["overwritten_entry_count"] += 1
                                continue
                            archive.writestr(rel, base_archive.read(info))
                            written.add(rel)
                            report["base_entry_count"] += 1
                except zipfile.BadZipFile as exc:
                    raise ValueError(
                        f"existing resource pack is not a valid zip and will not be overwritten implicitly: {base_zip}; "
                        "repair it or pass --replace-existing-resource-pack"
                    ) from exc
            for path, rel in overlay_entries:
                if rel in written:
                    continue
                archive.write(path, rel)
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()

    return report


def find_world_root_for_resources(root: Path) -> Path:
    """Return the Java world root inside a copied package tree."""
    if (root / "level.dat").is_file():
        return root

    level_files = sorted(p for p in root.rglob("level.dat") if p.is_file())
    if len(level_files) == 1:
        return level_files[0].parent
    if not level_files:
        raise ValueError(f"cannot embed resources.zip because no level.dat was found under copied world: {root}")
    sample = ", ".join(path.relative_to(root).as_posix() for path in level_files[:5])
    raise ValueError(f"cannot choose resources.zip location because multiple level.dat files were found: {sample}")


def default_apply_report_path(out: Path, is_zip_output: bool) -> Path:
    if is_zip_output:
        return out.with_suffix(out.suffix + ".mcmap_hybrid_apply_report.json")
    return out / "mcmap_hybrid_apply_report.json"


def default_direct_apply_report_path(out: Path, is_zip_output: bool) -> Path:
    if is_zip_output:
        return out.with_suffix(out.suffix + ".mcmap_direct_text_apply_report.json")
    return out / "mcmap_direct_text_apply_report.json"


def world_file_path(root: Path, source_file: str) -> Path | None:
    rel = safe_rel_path(source_file)
    if rel is None:
        return None
    return root.joinpath(*rel.parts)


def source_resource_pack_paths(source: Path) -> list[str]:
    return [
        name
        for name in entry_names(source)
        if PurePosixPath(name).name.lower() == "resources.zip"
    ]


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
        if lowered.endswith(".mcfunction"):
            patch_direct_mcfunction_file(path, source_file, file_rows, state)
        elif lowered.endswith(".json"):
            patch_direct_json_file(path, source_file, file_rows, state)
        elif lowered.endswith(".dat"):
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
    existing_map_resource_packs = source_resource_pack_paths(source)

    if existing_map_resource_packs and not args.resource_pack and not args.allow_separate_resource_pack:
        sample = ", ".join(existing_map_resource_packs[:5])
        raise ValueError(
            "source map already contains resources.zip; hybrid-keyed output must embed and merge the generated "
            f"resource pack with --resource-pack (found: {sample}). Pass --allow-separate-resource-pack only when "
            "the translated resource pack will be delivered and loaded separately."
        )

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

    all_rows = read_jsonl(translations)
    identity_qa = identity_consistency_report(all_rows)
    if identity_qa["blocking_count"]:
        print_blocking_errors(
            [
                f"{identity_qa['conflict_count']} identity conflict(s)",
                f"{identity_qa['unresolved_count']} unresolved identity unit(s)",
                f"{identity_qa['relationship_gap_count']} identity relationship gap(s)",
            ],
            "hybrid apply blocked by identity QA",
        )
        return 1
    rows, selection_skipped = select_hybrid_rows(all_rows, args)
    encoding_errors: list[str] = []
    for row in rows:
        encoding_errors.extend(unit_encoding_errors(row))
    if encoding_errors:
        print_blocking_errors(encoding_errors, f"hybrid apply blocked: {len(encoding_errors)} encoding error(s)")
        return 1
    state = ApplyState(dry_run=args.dry_run, multi_text_mode=args.multi_text_mode)

    if args.report:
        report_path = Path(args.report).resolve()
    elif args.dry_run and not is_zip_output:
        report_path = out.with_name(out.name + ".mcmap_hybrid_apply_report.json")
    else:
        report_path = default_apply_report_path(out, is_zip_output)

    resource_pack_embed_path = ""
    resource_pack_merge_report: dict[str, Any] = {}

    def run_on_copy(workdir: Path) -> None:
        nonlocal resource_pack_embed_path, resource_pack_merge_report
        copy_source_to_workdir(source, workdir)
        patch_world_copy(workdir, rows, state)
        if args.resource_pack:
            pack = Path(args.resource_pack).resolve()
            if not state.dry_run:
                resource_root = find_world_root_for_resources(workdir)
                target = resource_root / "resources.zip"
                resource_pack_merge_report = zip_dir(
                    pack,
                    target,
                    base_zip=target if target.exists() else None,
                    replace_existing_resource_pack=args.replace_existing_resource_pack,
                )
                resource_pack_embed_path = target.relative_to(workdir).as_posix()

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
        "no_op_units": state.no_op_units,
        "already_applied_units": state.already_applied,
        "by_type": state.type_report(rows),
        "changed_files": sorted(state.changed_files),
        "changed_file_count": len(state.changed_files),
        "skipped": dict(sorted(state.skipped.items())),
        "skipped_samples": state.skipped_samples,
        "resource_pack_embedded": bool(args.resource_pack and not args.dry_run),
        "resource_pack_embed_path": resource_pack_embed_path,
        "resource_pack_merge": resource_pack_merge_report,
        "source_map_resource_packs": existing_map_resource_packs,
        "separate_resource_pack_explicit": bool(args.allow_separate_resource_pack),
        "identity_qa": identity_qa,
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

    all_rows = read_jsonl(translations)
    identity_qa = identity_consistency_report(all_rows)
    if identity_qa["blocking_count"]:
        print_blocking_errors(
            [
                f"{identity_qa['conflict_count']} identity conflict(s)",
                f"{identity_qa['unresolved_count']} unresolved identity unit(s)",
                f"{identity_qa['relationship_gap_count']} identity relationship gap(s)",
            ],
            "direct text apply blocked by identity QA",
        )
        return 1
    rows, selection_skipped = select_direct_text_rows(all_rows, args)
    encoding_errors: list[str] = []
    for row in rows:
        encoding_errors.extend(unit_encoding_errors(row))
    if encoding_errors:
        print_blocking_errors(encoding_errors, f"direct text apply blocked: {len(encoding_errors)} encoding error(s)")
        return 1
    state = ApplyState(dry_run=args.dry_run, multi_text_mode="skip")

    if args.report:
        report_path = Path(args.report).resolve()
    elif args.dry_run and not is_zip_output:
        report_path = out.with_name(out.name + ".mcmap_direct_text_apply_report.json")
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
        "schema": "mc-map-translate-direct-text-apply-report.v1",
        "created_at": utc_now(),
        "source": str(source),
        "output": str(out),
        "translations_file": str(translations),
        "dry_run": args.dry_run,
        "selected_units": len(rows),
        "selection_skipped": dict(sorted(selection_skipped.items())),
        "changed_units": state.changed_units,
        "no_op_units": state.no_op_units,
        "already_applied_units": state.already_applied,
        "by_type": state.type_report(rows),
        "changed_files": sorted(state.changed_files),
        "changed_file_count": len(state.changed_files),
        "skipped": dict(sorted(state.skipped.items())),
        "skipped_samples": state.skipped_samples,
        "risk": "embedded-direct plain text replacement in copied .mcfunction, datapack JSON, .dat, or .mca anchors; source text must match exactly and original source is never edited",
        "identity_qa": identity_qa,
    }
    write_json(report_path, report)

    print(f"copied_world: {out}")
    print(f"apply_report: {report_path}")
    print(f"selected_units: {len(rows)}")
    print(f"changed_units: {state.changed_units}")
    print(f"changed_files: {len(state.changed_files)}")
    print(f"skipped_units: {sum(state.skipped.values())}")
    return 0 if state.changed_units or args.allow_no_changes else 3


def zip_dir(
    src: Path,
    out: Path,
    *,
    base_zip: Path | None = None,
    replace_existing_resource_pack: bool = False,
) -> dict[str, Any]:
    return zip_resource_pack_dir(
        src,
        out,
        base_zip=base_zip,
        replace_existing_resource_pack=replace_existing_resource_pack,
    )


def zip_resource_pack(args: argparse.Namespace) -> int:
    src = Path(args.resource_pack).resolve()
    out = Path(args.out).resolve()
    base = Path(args.base_resource_pack).resolve() if args.base_resource_pack else None
    merge_report = zip_dir(src, out, base_zip=base)
    print(f"zip: {out}")
    print(f"resource_pack_zip_mode: {merge_report['mode']}")
    if merge_report.get("base_resource_pack"):
        print(f"base_resource_pack: {merge_report['base_resource_pack']}")
        print(f"overwritten_entries: {merge_report['overwritten_entry_count']}")
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
    resource_root = find_world_root_for_resources(out)
    target = resource_root / "resources.zip"
    merge_report = zip_dir(
        pack,
        target,
        base_zip=target if target.exists() else None,
        replace_existing_resource_pack=args.replace_existing_resource_pack,
    )
    embed_report = {
        "schema": "mc-map-resource-pack-embed-report.v1",
        "created_at": utc_now(),
        "source": str(world),
        "output": str(out),
        "resource_pack": str(pack),
        "resource_pack_embed_path": target.relative_to(out).as_posix(),
        "source_map_resource_packs": source_resource_pack_paths(world),
        "resource_pack_merge": merge_report,
    }
    report_path = out / "mcmap_resource_pack_embed_report.json"
    write_json(report_path, embed_report)
    print(f"copied_world: {out}")
    print(f"embedded_resource_pack: {target}")
    print(f"embed_report: {report_path}")
    print(f"resource_pack_zip_mode: {merge_report['mode']}")
    if merge_report.get("base_resource_pack"):
        print(f"base_resource_pack: {merge_report['base_resource_pack']}")
        print(f"overwritten_entries: {merge_report['overwritten_entry_count']}")
    return 0


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_delivery(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    primary = Path(args.primary_output).resolve()
    translation_qa_path = Path(args.translation_qa).resolve()
    resource_pack = Path(args.resource_pack_output).resolve() if args.resource_pack_output else None
    residual_audit = Path(args.residual_audit).resolve() if args.residual_audit else None
    apply_reports = [Path(item).resolve() for item in args.apply_report]
    required_paths = [project, primary, translation_qa_path, *apply_reports]
    if resource_pack:
        required_paths.append(resource_pack)
    if residual_audit:
        required_paths.append(residual_audit)
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise FileNotFoundError(f"delivery inputs do not exist: {', '.join(missing)}")

    translation_qa = read_json_object(translation_qa_path)
    if translation_qa.get("status") != "pass":
        raise ValueError(f"translation QA is not passing: {translation_qa_path}")
    if translation_qa.get("allow_incomplete") or int(translation_qa.get("remaining_units", 0) or 0):
        raise ValueError(f"delivery requires complete translation QA without --allow-incomplete: {translation_qa_path}")
    scan_report_path = project / "scan_report.json"
    scan_report = read_json_object(scan_report_path) if scan_report_path.exists() else {}
    report_objects = [read_json_object(path) for path in apply_reports]
    existing_pack_count = int(scan_report.get("map_resource_pack_count", 0) or 0)
    merge_proven = any(
        isinstance(report.get("resource_pack_merge"), dict)
        and report["resource_pack_merge"].get("mode") == "merge"
        for report in report_objects
    )
    if existing_pack_count and args.mode in {"embedded-pack-copy", "hybrid-keyed-copy", "direct-text-copy"} and not merge_proven:
        raise ValueError(
            "source map had resources.zip, but no supplied apply/embed report proves that the generated pack was merged"
        )
    if args.mode == "resource-pack-only" and resource_pack is None:
        raise ValueError("resource-pack-only delivery requires --resource-pack-output")
    if args.mode in {"hybrid-keyed-copy", "direct-text-copy"} and not apply_reports:
        raise ValueError(f"{args.mode} delivery requires at least one --apply-report")

    residual_report = read_json_object(residual_audit) if residual_audit else {}
    out = Path(args.out).resolve() if args.out else project / "exports" / "DELIVERY.md"
    lines = [
        "# Delivery",
        "",
        f"- Export mode: `{args.mode}`",
        f"- Primary output: `{primary}`",
        f"- Translation QA: `{translation_qa_path}` (`pass`)",
        f"- Source map resource packs: {existing_pack_count}",
        f"- Existing resource-pack merge proven: {'yes' if merge_proven else 'not required'}",
    ]
    if resource_pack:
        lines.append(f"- Resource-pack output: `{resource_pack}`")
    if residual_audit:
        lines.append(
            f"- Residual-English audit: `{residual_audit}` ({residual_report.get('finding_count', 0)} reported findings)"
        )
    for path in apply_reports:
        lines.append(f"- Apply/embed report: `{path}`")
    if args.notes:
        lines.extend(["", "## Notes", "", args.notes.strip()])
    lines.extend(
        [
            "",
            "Use the primary output above as the canonical delivery artifact. Other zips in the exports folder are intermediate or alternate-mode artifacts unless listed here.",
            "",
        ]
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    report = {
        "schema": "mc-map-delivery.v1",
        "created_at": utc_now(),
        "mode": args.mode,
        "primary_output": str(primary),
        "resource_pack_output": str(resource_pack) if resource_pack else "",
        "translation_qa": str(translation_qa_path),
        "residual_audit": str(residual_audit) if residual_audit else "",
        "apply_reports": [str(path) for path in apply_reports],
        "source_map_resource_pack_count": existing_pack_count,
        "resource_pack_merge_proven": merge_proven,
    }
    write_json(out.with_suffix(".json"), report)
    print(f"delivery: {out}")
    print(f"primary_output: {primary}")
    return 0


def resolve_item_identities(args: argparse.Namespace) -> int:
    translations = Path(args.translations).resolve()
    decisions_path = Path(args.decisions).resolve()
    out = Path(args.out).resolve()
    rows = read_jsonl(translations)
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    if not isinstance(decisions, dict):
        raise ValueError("identity decisions must be a JSON object")
    row_by_id = {str(row.get("id", "")): row for row in rows}
    touched: set[str] = set()
    errors: list[str] = []

    groups = decisions.get("groups", [])
    if not isinstance(groups, list):
        raise ValueError("identity decisions groups must be a list")
    for index, decision in enumerate(groups):
        if not isinstance(decision, dict):
            errors.append(f"groups[{index}] must be an object")
            continue
        name = str(decision.get("name", "")).strip()
        reason = str(decision.get("review_reason", "")).strip()
        unit_ids = decision.get("unit_ids", [])
        if not name or not reason or not isinstance(unit_ids, list) or not unit_ids:
            errors.append(f"groups[{index}] requires name, review_reason, and non-empty unit_ids")
            continue
        fingerprint = stable_id(
            "manual-item-identity-v2",
            normalize_key_piece(name),
            str(decision.get("item_id", "")),
        )
        role_overrides = decision.get("roles", {})
        if not isinstance(role_overrides, dict):
            errors.append(f"groups[{index}].roles must be an object keyed by unit id")
            role_overrides = {}
        for raw_unit_id in unit_ids:
            unit_id = str(raw_unit_id)
            row = row_by_id.get(unit_id)
            if row is None:
                errors.append(f"groups[{index}] references unknown unit id: {unit_id}")
                continue
            if unit_id in touched:
                errors.append(f"unit id appears in more than one manual identity group: {unit_id}")
                continue
            if str(row.get("source_kind", "")) not in IDENTITY_COUPLED_SOURCE_KINDS:
                errors.append(f"groups[{index}] unit is not item_name/item_lore: {unit_id}")
                continue
            context = row.get("context") if isinstance(row.get("context"), dict) else {}
            context["identity_item_fingerprint"] = fingerprint
            context["identity_non_text_fingerprint"] = fingerprint
            context["identity_resolution"] = "manual"
            context["identity_review_reason"] = reason
            context["identity_manual_name"] = name
            if decision.get("item_id"):
                context["identity_item_id"] = str(decision["item_id"])
            if unit_id in role_overrides:
                context["identity_role"] = str(role_overrides[unit_id])
            context["identity_slot"] = identity_slot_for_row(
                row, str(row.get("address", {}).get("nbt_path", ""))
            )
            row["context"] = context
            touched.add(unit_id)

    external_sources = decisions.get("external_sources", [])
    if not isinstance(external_sources, list):
        raise ValueError("identity decisions external_sources must be a list")
    approved_external = 0
    for index, decision in enumerate(external_sources):
        if not isinstance(decision, dict):
            errors.append(f"external_sources[{index}] must be an object")
            continue
        reason = str(decision.get("reason", "")).strip()
        unit_ids = decision.get("unit_ids", [])
        if not reason or not isinstance(unit_ids, list) or not unit_ids:
            errors.append(f"external_sources[{index}] requires reason and non-empty unit_ids")
            continue
        for raw_unit_id in unit_ids:
            unit_id = str(raw_unit_id)
            row = row_by_id.get(unit_id)
            if row is None:
                errors.append(f"external_sources[{index}] references unknown unit id: {unit_id}")
                continue
            context = row.get("context") if isinstance(row.get("context"), dict) else {}
            context["identity_external_source"] = True
            context["identity_external_source_reason"] = reason
            row["context"] = context
            approved_external += 1

    if errors:
        raise ValueError("identity decisions rejected:\n- " + "\n- ".join(errors))

    namespace = normalize_key_piece(str(args.namespace or decisions.get("namespace") or "mcmap"))
    map_slug = normalize_key_piece(str(args.map_slug or decisions.get("map_slug") or "map"))
    summary = canonicalize_identity_keys(rows, namespace, map_slug)
    write_jsonl(out, rows)
    report = {
        "schema": "mc-map-identity-resolution-report.v1",
        "created_at": utc_now(),
        "source": str(translations),
        "decisions": str(decisions_path),
        "output": str(out),
        "manual_unit_count": len(touched),
        "external_source_approval_count": approved_external,
        "identity_summary": summary,
    }
    report_path = Path(args.report).resolve() if args.report else out.with_suffix(out.suffix + ".identity_resolution_report.json")
    write_json(report_path, report)
    print(f"resolved_translations: {out}")
    print(f"identity_resolution_report: {report_path}")
    print(f"manual_units: {len(touched)}")
    print(f"unresolved_units: {summary['unresolved_unit_count']}")
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
    scan.add_argument("--include-last-output", action="store_true", help="include command block LastOutput logs; excluded by default because they are usually vanilla execution noise")
    scan.add_argument("--max-binary-errors", type=int, default=50, help="maximum binary parser warnings to keep in scan_report.json")
    scan.add_argument("--project-layout", action="store_true", help="also create indexed multi-file project layout for staged AI translation")
    scan.add_argument("--max-workpack-units", type=int, default=120, help="maximum units per contextual workpack when --project-layout is used")
    scan.add_argument("--no-prepare-segments", action="store_true", help="do not scaffold segments[] when --project-layout is used")
    scan.set_defaults(func=scan_source)

    resolve_identity = subparsers.add_parser(
        "resolve-item-identities",
        help="apply reviewed manual item-identity groups and external-source decisions",
    )
    resolve_identity.add_argument("translations", help="translation JSONL or project containing identity-coupled rows")
    resolve_identity.add_argument("--decisions", required=True, help="reviewed identity decisions JSON")
    resolve_identity.add_argument("--out", required=True, help="output JSONL with canonical manual identity keys")
    resolve_identity.add_argument("--namespace", default="", help="generated translation-key namespace")
    resolve_identity.add_argument("--map-slug", default="", help="stable map slug for generated keys")
    resolve_identity.add_argument("--report", default="", help="custom identity resolution report path")
    resolve_identity.set_defaults(func=resolve_item_identities)

    apply = subparsers.add_parser("apply-hybrid-keys", help="patch a copied Java world so hardcoded JSON text components use translation keys")
    apply.add_argument("source", help="original Java world directory or map zip")
    apply.add_argument("--translations", required=True, help="translation_units.jsonl or translations.jsonl containing hybrid units")
    apply.add_argument("--out", required=True, help="copied world output directory or .zip")
    apply.add_argument("--resource-pack", default="", help="optional resource-pack directory to embed as resources.zip in the copied world")
    apply.add_argument(
        "--allow-separate-resource-pack",
        action="store_true",
        help="allow a source map with resources.zip to ship generated hybrid language keys as a separate manually loaded pack",
    )
    apply.add_argument(
        "--multi-text-mode",
        choices=["split-nodes", "skip"],
        default="split-nodes",
        help="how to handle grouped components with multiple hardcoded text nodes",
    )
    apply.add_argument("--min-confidence", choices=["low", "medium", "high"], default="medium", help="minimum scanner confidence to apply")
    apply.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    apply.add_argument("--unit-id", default="", help="comma-separated unit id filter")
    apply.add_argument(
        "--translated-only",
        dest="translated_only",
        action="store_true",
        help="only inject keys for fully translated/reviewed units (default)",
    )
    apply.add_argument(
        "--include-untranslated",
        dest="translated_only",
        action="store_false",
        help="unsafe diagnostic mode: allow key injection for units without complete translations",
    )
    apply.add_argument("--dry-run", action="store_true", help="copy to a temporary directory and report what would change")
    apply.add_argument("--report", default="", help="custom apply report JSON path")
    apply.add_argument("--allow-no-changes", action="store_true", help="return success even when no units changed")
    apply.add_argument("--force", action="store_true", help="replace an existing output copy")
    apply.add_argument(
        "--replace-existing-resource-pack",
        action="store_true",
        help="when embedding --resource-pack, replace an existing copied resources.zip instead of merging the generated pack into it",
    )
    apply.set_defaults(func=apply_hybrid_keys, translated_only=True)

    direct = subparsers.add_parser(
        "apply-direct-nbt-strings",
        help="patch copied Java world direct text anchors with translated text (legacy name)",
    )
    direct.add_argument("source", help="original Java world directory or map zip")
    direct.add_argument("--translations", required=True, help="translation_units.jsonl or translations.jsonl containing embedded-direct units")
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

    direct_text = subparsers.add_parser("apply-direct-text", help="patch copied Java world embedded-direct text anchors")
    direct_text.add_argument("source", help="original Java world directory or map zip")
    direct_text.add_argument("--translations", required=True, help="translation_units.jsonl or translations.jsonl containing embedded-direct units")
    direct_text.add_argument("--out", required=True, help="copied world output directory or .zip")
    direct_text.add_argument("--min-confidence", choices=["low", "medium", "high"], default="medium", help="minimum scanner confidence to apply")
    direct_text.add_argument("--source-kind", default="", help="comma-separated source_kind filter")
    direct_text.add_argument("--unit-id", default="", help="comma-separated unit id filter")
    direct_text.add_argument("--allow-empty-translation", action="store_true", help="allow empty translations to replace source strings")
    direct_text.add_argument("--dry-run", action="store_true", help="copy to a temporary directory and report what would change")
    direct_text.add_argument("--report", default="", help="custom apply report JSON path")
    direct_text.add_argument("--allow-no-changes", action="store_true", help="return success even when no units changed")
    direct_text.add_argument("--force", action="store_true", help="replace an existing output copy")
    direct_text.set_defaults(func=apply_direct_nbt_strings)

    audit = subparsers.add_parser("audit-english", help="audit exported Java map/resource files for residual English-looking player-facing text")
    audit.add_argument("source", help="Java world directory or map zip to audit")
    audit.add_argument("--out", required=True, help="output residual English audit JSON report")
    audit.add_argument("--max-findings", type=int, default=500, help="maximum findings to record")
    audit.add_argument("--target-locale", default="", help="audit only this target locale language JSON, for example zh_cn")
    audit.add_argument("--source-locale", default="en_us", help="source locale language JSON to exclude by default")
    audit.add_argument(
        "--include-source-language",
        action="store_true",
        help="include source-locale language JSON in residual-English findings",
    )
    audit.add_argument("--include-last-output", action="store_true", help="include command block LastOutput logs in the audit")
    audit.set_defaults(func=audit_english)

    zip_pack = subparsers.add_parser("zip-resource-pack", help="zip a resource-pack directory")
    zip_pack.add_argument("resource_pack", help="resource pack directory with pack.mcmeta at root")
    zip_pack.add_argument("--out", required=True, help="output zip path")
    zip_pack.add_argument("--base-resource-pack", default="", help="optional existing resource-pack zip to merge before overlaying generated files")
    zip_pack.set_defaults(func=zip_resource_pack)

    embed = subparsers.add_parser("embed-resource-pack", help="copy a Java world and add or merge resources.zip")
    embed.add_argument("world", help="original Java world directory")
    embed.add_argument("--resource-pack", required=True, help="resource pack directory")
    embed.add_argument("--out", required=True, help="copied world output directory")
    embed.add_argument("--force", action="store_true", help="replace existing output directory")
    embed.add_argument(
        "--replace-existing-resource-pack",
        action="store_true",
        help="replace an existing copied resources.zip instead of merging the generated pack into it",
    )
    embed.set_defaults(func=embed_resource_pack)

    delivery = subparsers.add_parser("write-delivery", help="write one canonical DELIVERY.md after QA and apply checks")
    delivery.add_argument("project", help="localization project/work directory containing scan_report.json")
    delivery.add_argument(
        "--mode",
        required=True,
        choices=["resource-pack-only", "embedded-pack-copy", "hybrid-keyed-copy", "direct-text-copy"],
        help="exact user-facing export mode",
    )
    delivery.add_argument("--primary-output", required=True, help="canonical map or resource-pack artifact to deliver")
    delivery.add_argument("--resource-pack-output", default="", help="optional standalone or merged resource-pack artifact")
    delivery.add_argument("--translation-qa", required=True, help="passing qa-translations JSON report")
    delivery.add_argument("--residual-audit", default="", help="optional residual-English audit JSON report")
    delivery.add_argument("--apply-report", action="append", default=[], help="apply/embed report; repeat for chained hybrid/direct exports")
    delivery.add_argument("--notes", default="", help="short delivery notes")
    delivery.add_argument("--out", default="", help="output DELIVERY.md; defaults to <project>/exports/DELIVERY.md")
    delivery.set_defaults(func=write_delivery)

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
