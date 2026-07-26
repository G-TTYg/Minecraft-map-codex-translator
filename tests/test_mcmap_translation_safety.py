from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "mc-map-translate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mcmap_contract as contract  # noqa: E402
import mcmap_java_tools as java_tools  # noqa: E402


def int_tag(value: int) -> java_tools.NbtTag:
    return java_tools.NbtTag(3, int(value).to_bytes(4, "big", signed=True))


def string_tag(value: str) -> java_tools.NbtTag:
    return java_tools.NbtTag(8, value)


def compound(**items: java_tools.NbtTag) -> java_tools.NbtTag:
    return java_tools.NbtTag(10, list(items.items()))


def compound_pairs(*items: tuple[str, java_tools.NbtTag]) -> java_tools.NbtTag:
    return java_tools.NbtTag(10, list(items))


def item_stack(name: str, marker: str = "kitatcho", lore: str = "") -> java_tools.NbtTag:
    components: list[tuple[str, java_tools.NbtTag]] = [
        ("minecraft:custom_name", string_tag(json.dumps({"text": name}, separators=(",", ":")))),
        ("minecraft:custom_data", compound(currency=string_tag(marker))),
    ]
    if lore:
        components.append(
            (
                "minecraft:lore",
                java_tools.NbtTag(9, (8, [string_tag(json.dumps({"text": lore}, separators=(",", ":")))])),
            )
        )
    return compound_pairs(
        ("id", string_tag("minecraft:slime_ball")),
        ("count", int_tag(1)),
        ("components", compound_pairs(*components)),
    )


def item_identity_payload(*, include_source: bool = True, second_marker: str = "") -> bytes:
    recipe = compound_pairs(
        ("buy", item_stack("Kitatcho Coin")),
        ("sell", compound_pairs(("id", string_tag("minecraft:diamond")), ("count", int_tag(1)))),
    )
    villager = compound_pairs(
        ("id", string_tag("minecraft:villager")),
        ("Offers", compound_pairs(("Recipes", java_tools.NbtTag(9, (10, [recipe]))))),
    )
    block_entities = [villager]
    if include_source:
        block_entities.append(
            compound_pairs(
                ("id", string_tag("minecraft:chest")),
                ("Items", java_tools.NbtTag(9, (10, [item_stack("Kitatcho Coin")]))),
            )
        )
    if second_marker:
        block_entities.append(
            compound_pairs(
                ("id", string_tag("minecraft:chest")),
                ("Items", java_tools.NbtTag(9, (10, [item_stack("Kitatcho Coin", second_marker)]))),
            )
        )
    tree = java_tools.NbtTree(
        10,
        "",
        compound_pairs(("block_entities", java_tools.NbtTag(9, (10, block_entities)))),
    )
    return java_tools.write_nbt_tree(tree)


def sign_tag(x: int, y: int, z: int, lines: list[str]) -> java_tools.NbtTag:
    messages = [string_tag(json.dumps({"text": line}, separators=(",", ":"))) for line in lines]
    return compound(
        id=string_tag("minecraft:oak_sign"),
        x=int_tag(x),
        y=int_tag(y),
        z=int_tag(z),
        front_text=compound(messages=java_tools.NbtTag(9, (8, messages))),
    )


def sign_fixture_payload() -> bytes:
    signs = [
        sign_tag(10, 64, -2, ["Maximum", "occupancy not", "to exceed 2", ""]),
        sign_tag(11, 64, -2, ["Exit", "", "", ""]),
    ]
    tree = java_tools.NbtTree(
        10,
        "",
        compound(block_entities=java_tools.NbtTag(9, (10, signs))),
    )
    return java_tools.write_nbt_tree(tree)


def named_entity_selector_fixture_payload() -> bytes:
    custom_name = json.dumps({"text": "Guide"}, separators=(",", ":"))
    blocks = [
        compound_pairs(
            ("id", string_tag("minecraft:armor_stand")),
            ("CustomName", string_tag(custom_name)),
        ),
        compound_pairs(
            ("id", string_tag("minecraft:command_block")),
            ("Command", string_tag('execute as @e[name="Guide"] run say Welcome')),
        ),
    ]
    return java_tools.write_nbt_tree(
        java_tools.NbtTree(
            10,
            "",
            compound_pairs(("block_entities", java_tools.NbtTag(9, (10, blocks)))),
        )
    )


class ScannerSafetyTests(unittest.TestCase):
    def test_sign_faces_always_group_and_keep_coordinates(self) -> None:
        counters: Counter[str] = Counter()
        items = java_tools.scan_nbt_strings(sign_fixture_payload(), chunk={"local_index": 0})
        units = java_tools.scan_nbt_items(
            items,
            source_file="data/signs.dat",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=counters,
        )
        signs = [row for row in units if row["source_kind"] == "sign"]

        self.assertEqual(2, len(signs))
        self.assertEqual(2, counters["aggregated_sign_groups"])
        self.assertEqual(2, counters["sign_faces_seen"])
        by_x = {row["address"]["block_pos"]["x"]: row for row in signs}
        self.assertEqual("Maximum\noccupancy not\nto exceed 2", by_x[10]["raw"])
        self.assertEqual(["Maximum", "occupancy not", "to exceed 2", ""], by_x[10]["context"]["line_texts"])
        self.assertEqual(4, len(by_x[10]["address"]["sign_lines"]))
        self.assertEqual("Exit", by_x[11]["raw"])
        self.assertEqual(1, len(by_x[11]["segments"]))

    def test_identity_coupled_item_text_uses_one_canonical_key(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload()),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )

        summary = java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")

        self.assertEqual(1, summary["group_count"])
        self.assertEqual(2, summary["max_group_size"])
        self.assertEqual(0, summary["unresolved_unit_count"])
        self.assertEqual(1, len({row["translation_key"] for row in rows}))
        self.assertTrue(all(row["context"]["identity_coupled"] for row in rows))
        self.assertEqual("trade_input", rows[0]["context"]["identity_role"])
        self.assertEqual("container", rows[1]["context"]["identity_role"])

    def test_named_entity_selector_protects_matching_custom_name(self) -> None:
        references: list[dict[str, object]] = []
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(named_entity_selector_fixture_payload()),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
            selector_references=references,
        )
        summary = java_tools.couple_selector_entity_names(rows, references)
        entity = next(row for row in rows if row["source_kind"] == "entity_name")

        self.assertEqual(1, summary["name_reference_count"])
        self.assertEqual(1, summary["matched_reference_count"])
        self.assertTrue(entity["context"]["selector_identity_coupled"])
        self.assertEqual("entity_custom_name", entity["context"]["selector_identity_role"])
        self.assertNotIn("hybrid-key-injection", entity["mode_support"])
        self.assertNotIn("embedded-direct", entity["mode_support"])

    def test_selector_parser_handles_negation_nbt_macros_and_ignores_stable_arguments(self) -> None:
        lines = [
            '@e[name=!"Guide"]',
            "@e[nbt={CustomName:'{\"text\":\"Guide\"}'}]",
            "@e[name=$(npc_name)]",
            "@e[tag=guide,type=minecraft:armor_stand,scores={quest=1},predicate=map:ready]",
        ]
        references = [
            reference
            for index, line in enumerate(lines)
            for reference in java_tools.selector_identity_references(
                line,
                source_file="data/map/functions/test.mcfunction",
                base_address={"function_line": index + 1},
            )
        ]

        self.assertEqual(3, len(references))
        self.assertTrue(references[0]["negated"])
        self.assertEqual("Guide", references[0]["name"])
        self.assertEqual("nbt_custom_name", references[1]["match_kind"])
        self.assertEqual("Guide", references[1]["name"])
        self.assertTrue(references[2]["dynamic"])

    def test_nbt_custom_name_selector_literal_is_not_patchable(self) -> None:
        references: list[dict[str, object]] = []
        rows = java_tools.scan_command_line(
            "execute as @e[nbt={CustomName:'{\"text\":\"Guide\"}'}] run say Welcome",
            source_file="data/map/functions/test.mcfunction",
            base_address={"function_line": 1},
            namespace="mcmap",
            map_slug="fixture",
            fallback_kind="function",
            confidence="high",
            selector_references=references,
        )
        protected = [
            row
            for row in rows
            if row.get("context", {}).get("selector_identity_role") == "selector_predicate_literal"
        ]

        self.assertEqual(1, len(protected))
        self.assertEqual([], protected[0]["mode_support"])
        self.assertTrue(any(row["raw"] == "Welcome" for row in rows))

    def test_datapack_json_component_selector_is_indexed(self) -> None:
        payload = json.dumps(
            {"text": "Following: ", "extra": [{"selector": "@e[name=Guide]"}]},
            separators=(",", ":"),
        ).encode("utf-8")
        entry = java_tools.Entry(
            "datapacks/map/data/map/dialogue/guide.json",
            payload,
            len(payload),
        )
        references: list[dict[str, object]] = []

        rows = java_tools.scan_json_file(
            entry,
            "mcmap",
            "fixture",
            selector_references=references,
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(1, len(references))
        self.assertEqual("Guide", references[0]["name"])
        self.assertEqual("$.extra[0].selector", references[0]["address"]["component_selector_path"])

    def test_full_scan_writes_selector_identity_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "world"
            data_dir = world / "data"
            data_dir.mkdir(parents=True)
            (world / "level.dat").write_bytes(
                java_tools.write_nbt_tree(java_tools.NbtTree(10, "", compound()))
            )
            (data_dir / "entities.dat").write_bytes(named_entity_selector_fixture_payload())
            out = root / "work"
            args = argparse.Namespace(
                source=str(world),
                out=str(out),
                target="zh_cn",
                source_locale="en_us",
                map_slug="fixture",
                namespace="mcmap",
                mode="resource-pack",
                no_binary=False,
                include_last_output=False,
                max_binary_errors=50,
                project_layout=False,
                max_workpack_units=120,
                no_prepare_segments=False,
            )

            self.assertEqual(0, java_tools.scan_source(args))
            artifact = json.loads((out / "selector_identity.json").read_text(encoding="utf-8"))
            rows = contract.read_jsonl(out / "translation_units.jsonl")
            entity = next(row for row in rows if row["source_kind"] == "entity_name")

            self.assertEqual(1, artifact["summary"]["reference_count"])
            self.assertEqual(1, artifact["summary"]["protected_unit_count"])
            self.assertEqual([], entity["mode_support"])

    def test_same_name_with_different_custom_data_is_not_merged(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload(second_marker="other-currency")),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")

        fingerprints = {row["context"]["identity_item_fingerprint"] for row in rows}
        self.assertEqual(2, len(fingerprints))
        other = [row for row in rows if "block_entities[2]" in row["address"]["nbt_path"]][0]
        self.assertNotEqual(rows[0]["translation_key"], other["translation_key"])

    def test_same_name_with_different_lore_is_not_merged(self) -> None:
        chests = []
        for lore in ("Opens the red vault", "Opens the blue vault"):
            chests.append(
                compound_pairs(
                    ("id", string_tag("minecraft:chest")),
                    ("Items", java_tools.NbtTag(9, (10, [item_stack("Vault Key", lore=lore)]))),
                )
            )
        payload = java_tools.write_nbt_tree(
            java_tools.NbtTree(
                10,
                "",
                compound_pairs(("block_entities", java_tools.NbtTag(9, (10, chests)))),
            )
        )
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(payload),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        names = [row for row in rows if row["source_kind"] == "item_name"]
        lore_rows = [row for row in rows if row["source_kind"] == "item_lore"]
        self.assertEqual(2, len({row["context"]["identity_item_fingerprint"] for row in names}))
        self.assertEqual(2, len({row["translation_key"] for row in names}))
        self.assertEqual({"lore[0]"}, {row["context"]["identity_slot"] for row in lore_rows})

    def test_command_producer_clear_and_predicate_match_nbt_item(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload()),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        item_spec = (
            "minecraft:slime_ball["
            "minecraft:custom_name='{\"text\":\"Kitatcho Coin\"}',"
            "minecraft:custom_data={currency:\"kitatcho\"}]"
        )
        for line_no, command in enumerate(
            (
                f"give @s {item_spec}",
                f"clear @s {item_spec}",
                f"execute if items entity @s inventory.* {item_spec} run say matched",
                (
                    "execute if entity @e[nbt={Inventory:[{id:\"minecraft:slime_ball\",count:1,"
                    "components:{\"minecraft:custom_name\":'{\"text\":\"Kitatcho Coin\"}',"
                    "\"minecraft:custom_data\":{currency:\"kitatcho\"}}}]}] run say matched"
                ),
            ),
            1,
        ):
            rows.extend(
                java_tools.scan_command_line(
                    command,
                    source_file="datapacks/fixture/data/test/function/identity.mcfunction",
                    base_address={"line": line_no},
                    namespace="mcmap",
                    map_slug="fixture",
                    fallback_kind="function",
                    confidence="high",
                )
            )

        summary = java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        item_rows = [row for row in rows if row["source_kind"] == "item_name"]
        self.assertEqual(1, summary["group_count"])
        self.assertEqual(1, len({row["context"]["identity_item_fingerprint"] for row in item_rows}))
        self.assertEqual(1, len({row["translation_key"] for row in item_rows}))
        self.assertEqual(
            {"trade_input", "container", "producer", "consumer", "predicate"},
            {row["context"]["identity_role"] for row in item_rows},
        )

    def test_score_name_inside_text_display_is_not_item_text(self) -> None:
        command = (
            'summon text_display ~ ~ ~ {text:[{"text":"Day: "},'
            '{"score":{"name":"day","objective":"time"}}]}'
        )
        rows = java_tools.scan_command_line(
            command,
            source_file="test.mcfunction",
            base_address={"line": 1},
            namespace="mcmap",
            map_slug="fixture",
            fallback_kind="function",
            confidence="high",
        )
        self.assertFalse(any(row["source_kind"] in {"item_name", "item_lore"} for row in rows))
        self.assertFalse(any(row["raw"] == "day" for row in rows))

    def test_unparsed_partial_item_predicate_is_identity_blocking(self) -> None:
        command = (
            "execute if items entity @s inventory.* "
            "minecraft:slime_ball[minecraft:custom_name~'{\"text\":\"Quest Coin\"}'] run say yes"
        )
        rows = java_tools.scan_command_line(
            command,
            source_file="test.mcfunction",
            base_address={"line": 1},
            namespace="mcmap",
            map_slug="fixture",
            fallback_kind="function",
            confidence="high",
        )
        summary = java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        item_rows = [row for row in rows if row["source_kind"] == "item_name"]
        self.assertEqual(1, len(item_rows))
        self.assertEqual("unresolved", item_rows[0]["context"]["identity_resolution"])
        self.assertEqual("predicate", item_rows[0]["context"]["identity_role"])
        self.assertEqual(1, summary["unresolved_unit_count"])

    def test_unparsed_item_text_remains_unresolved_and_occurrence_keyed(self) -> None:
        rows = []
        for index in range(2):
            rows.append(
                java_tools.make_unit(
                    edition="java",
                    source_kind="item_name",
                    source_file="unknown.json",
                    address={"json_path": f"$.items[{index}].name"},
                    raw="Coin",
                    mode_support=["hybrid-key-injection"],
                    confidence="low",
                    resource_namespace="mcmap",
                    translation_key=f"temporary.key.{index}",
                )
            )
        summary = java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        self.assertEqual(2, summary["unresolved_unit_count"])
        self.assertEqual(2, len({row["translation_key"] for row in rows}))

    def test_reviewed_manual_identity_decisions_canonicalize_without_code_edits(self) -> None:
        rows = []
        for index, role in enumerate(("producer", "consumer")):
            row = java_tools.make_unit(
                edition="java",
                source_kind="item_name",
                source_file="unknown.json",
                address={"json_path": f"$.items[{index}].name"},
                raw="Quest Key",
                mode_support=["hybrid-key-injection"],
                confidence="low",
                resource_namespace="mcmap",
                translation_key=f"temporary.key.{index}",
            )
            row["context"]["identity_role"] = role
            rows.append(row)
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "translations.jsonl"
            decisions = root / "identity_decisions.json"
            out = root / "resolved.jsonl"
            java_tools.write_jsonl(source, rows)
            decisions.write_text(
                json.dumps(
                    {
                        "namespace": "mcmap",
                        "map_slug": "fixture",
                        "groups": [
                            {
                                "name": "quest_key",
                                "item_id": "minecraft:tripwire_hook",
                                "unit_ids": [row["id"] for row in rows],
                                "review_reason": "Both anchors use the same custom-data quest key.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                translations=str(source),
                decisions=str(decisions),
                out=str(out),
                namespace="",
                map_slug="",
                report="",
            )
            self.assertEqual(0, java_tools.resolve_item_identities(args))
            resolved = contract.read_jsonl(out)
            self.assertEqual(1, len({row["translation_key"] for row in resolved}))
            self.assertTrue(all(row["context"]["identity_resolution"] == "manual" for row in resolved))

    def test_sign_face_hybrid_apply_reports_by_face(self) -> None:
        payload = sign_fixture_payload()
        units = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(payload),
            source_file="data/signs.dat",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        signs = [row for row in units if row["source_kind"] == "sign"]
        translated_lines = {
            "Maximum": "最多",
            "occupancy not": "容纳人数",
            "to exceed 2": "不得超过 2",
            "Exit": "出口",
        }
        for row in signs:
            row["translation"] = "\n".join(translated_lines.get(line, line) for line in row["context"]["line_texts"]).strip("\n")
            row["review_status"] = "translated"
            for segment in row["segments"]:
                segment["translation"] = translated_lines[segment["raw"]]
                segment["review_status"] = "translated"

        state = java_tools.ApplyState(dry_run=False, multi_text_mode="split-nodes")
        patched, changed = java_tools.patch_nbt_blob(payload, signs, state)

        self.assertTrue(changed)
        self.assertEqual(2, state.changed_units)
        self.assertEqual(2, state.type_report(signs)["sign_face"]["changed"])
        values = {item.path: item.value for item in java_tools.scan_nbt_strings(patched)}
        for row in signs:
            for segment in row["segments"]:
                component = json.loads(values[segment["nbt_path"]])
                self.assertEqual(segment["translation_key"], component["translate"])

    def test_apply_hybrid_keys_smoke_copies_world_and_preserves_source(self) -> None:
        payload = sign_fixture_payload()
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(payload),
            source_file="data/signs.dat",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        for row in rows:
            translated_segments = []
            for segment in row["segments"]:
                segment["translation"] = f"Localized {segment['raw']}"
                segment["review_status"] = "translated"
                translated_segments.append(segment["translation"])
            row["translation"] = "\n".join(translated_segments)
            row["review_status"] = "translated"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "world"
            (world / "data").mkdir(parents=True)
            (world / "level.dat").write_bytes(b"fixture-level")
            source_nbt = world / "data" / "signs.dat"
            source_nbt.write_bytes(payload)
            translations = root / "translations.jsonl"
            java_tools.write_jsonl(translations, rows)
            out = root / "world-keyed"
            args = argparse.Namespace(
                source=str(world),
                out=str(out),
                translations=str(translations),
                resource_pack="",
                allow_separate_resource_pack=False,
                multi_text_mode="split-nodes",
                min_confidence="medium",
                source_kind="",
                unit_id="",
                translated_only=True,
                dry_run=False,
                report="",
                allow_no_changes=False,
                force=False,
                replace_existing_resource_pack=False,
            )
            self.assertEqual(0, java_tools.apply_hybrid_keys(args))
            self.assertEqual(payload, source_nbt.read_bytes())
            self.assertNotEqual(payload, (out / "data" / "signs.dat").read_bytes())
            report = json.loads((out / "mcmap_hybrid_apply_report.json").read_text(encoding="utf-8"))
            self.assertEqual(2, report["changed_units"])

    def test_residual_audit_groups_sign_faces(self) -> None:
        findings: list[dict[str, object]] = []
        errors = java_tools.audit_binary_entry(
            java_tools.Entry("data/signs.dat", sign_fixture_payload(), len(sign_fixture_payload())),
            findings=findings,
            max_findings=50,
            include_last_output=False,
        )
        self.assertEqual([], errors)
        self.assertEqual(2, len(findings))
        self.assertTrue(all(item["source_kind"] == "sign" for item in findings))
        self.assertTrue(any("Maximum\\noccupancy not\\nto exceed 2" == item["raw_preview"] for item in findings))


class TranslationContractTests(unittest.TestCase):
    def test_same_as_source_requires_status_and_reason(self) -> None:
        row = {
            "id": "same-source",
            "edition": "java",
            "source_kind": "sign",
            "source_file": "data/signs.dat",
            "address": {"nbt_path": "root.Text1"},
            "raw": "TNT",
            "translation": "TNT",
            "translation_key": "mcmap.fixture.sign.tnt",
            "resource_namespace": "mcmap",
            "mode_support": ["hybrid-key-injection"],
            "protected": [],
            "context": {},
            "confidence": "high",
        }
        errors = contract.validate_unit(row)
        self.assertTrue(any("equals the source" in error for error in errors))
        self.assertFalse(contract.row_translation_complete(row))

        row["review_status"] = "code"
        row["review_reason"] = "Canonical in-game explosive abbreviation."
        self.assertEqual([], contract.validate_unit(row))
        self.assertTrue(contract.row_translation_complete(row))

    def test_identity_qa_blocks_different_keys(self) -> None:
        rows = []
        for index, key in enumerate(("mcmap.fixture.coin.a", "mcmap.fixture.coin.b")):
            rows.append(
                {
                    "id": f"coin-{index}",
                    "source_kind": "item_name",
                    "raw": "Coin",
                    "translation": "Coin",
                    "translation_key": key,
                    "mode_support": ["hybrid-key-injection"],
                    "context": {"identity_coupled": True, "identity_group": "coin", "identity_role": "producer"},
                }
            )
        report = contract.identity_consistency_report(rows)
        self.assertEqual(1, report["conflict_count"])

    def test_identity_qa_blocks_selector_coupled_custom_name_translation(self) -> None:
        references: list[dict[str, object]] = []
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(named_entity_selector_fixture_payload()),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
            selector_references=references,
        )
        java_tools.couple_selector_entity_names(rows, references)
        entity = next(row for row in rows if row["source_kind"] == "entity_name")
        entity["translation"] = "向导"
        entity["review_status"] = "translated"

        report = contract.identity_consistency_report(rows)

        self.assertEqual(1, report["selector_identity_conflict_count"])
        self.assertGreater(report["blocking_count"], 0)

    def test_identity_qa_accepts_reviewed_preserved_selector_custom_name(self) -> None:
        references: list[dict[str, object]] = []
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(named_entity_selector_fixture_payload()),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
            selector_references=references,
        )
        java_tools.couple_selector_entity_names(rows, references)
        entity = next(row for row in rows if row["source_kind"] == "entity_name")
        entity["translation"] = entity["raw"]
        entity["review_status"] = "intentional_name"
        entity["review_reason"] = "Preserved because @e[name=Guide] uses this CustomName as entity identity."

        report = contract.identity_consistency_report(rows)

        self.assertEqual(0, report["selector_identity_conflict_count"])

    def test_hybrid_apply_refuses_selector_coupled_custom_name_translation(self) -> None:
        references: list[dict[str, object]] = []
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(named_entity_selector_fixture_payload()),
            source_file="data/entities.dat",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
            selector_references=references,
        )
        java_tools.couple_selector_entity_names(rows, references)
        entity = next(row for row in rows if row["source_kind"] == "entity_name")
        entity["translation"] = "向导"
        entity["review_status"] = "translated"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "world"
            world.mkdir()
            (world / "level.dat").write_bytes(b"fixture")
            translations = root / "translations.jsonl"
            java_tools.write_jsonl(translations, rows)
            out = root / "blocked-output"
            args = argparse.Namespace(
                source=str(world),
                out=str(out),
                translations=str(translations),
                resource_pack="",
                allow_separate_resource_pack=False,
                multi_text_mode="split-nodes",
                min_confidence="medium",
                source_kind="",
                unit_id="",
                translated_only=True,
                dry_run=False,
                report="",
                allow_no_changes=True,
                force=False,
                replace_existing_resource_pack=False,
            )

            self.assertEqual(1, java_tools.apply_hybrid_keys(args))
            self.assertFalse(out.exists())

    def test_identity_qa_requires_source_for_trade_input(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload(include_source=False)),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        report = contract.identity_consistency_report(rows)
        self.assertEqual(1, report["relationship_gap_count"])
        self.assertGreater(report["blocking_count"], 0)

        rows[0]["context"]["identity_external_source"] = True
        rows[0]["context"]["identity_external_source_reason"] = "Granted by an unscannable runtime plugin."
        report = contract.identity_consistency_report(rows)
        self.assertEqual(0, report["relationship_gap_count"])

    def test_identity_qa_reports_same_text_on_distinct_items_without_merging(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload(second_marker="other-currency")),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        report = contract.identity_consistency_report(rows)
        self.assertEqual(1, report["same_text_distinct_item_count"])
        self.assertEqual(0, report["conflict_count"])

    def test_interim_translation_qa_does_not_bypass_identity_gaps(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload(include_source=False)),
            source_file="region/r.0.0.mca",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / "translations.jsonl"
            out = root / "qa" / "translation_qa.json"
            java_tools.write_jsonl(units, rows)
            args = argparse.Namespace(translations=str(units), out=str(out), allow_incomplete=True)
            self.assertEqual(4, contract.qa_translations(args))
            identity_report = json.loads((out.parent / "identity_qa.json").read_text(encoding="utf-8"))
            self.assertEqual(1, identity_report["relationship_gap_count"])

    def test_hybrid_apply_refuses_identity_relationship_gap(self) -> None:
        rows = java_tools.scan_nbt_items(
            java_tools.scan_nbt_strings(item_identity_payload(include_source=False)),
            source_file="data/items.dat",
            namespace="mcmap",
            map_slug="fixture",
            include_last_output=False,
            counters=Counter(),
        )
        java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = root / "world"
            world.mkdir()
            (world / "level.dat").write_bytes(b"fixture")
            translations = root / "translations.jsonl"
            java_tools.write_jsonl(translations, rows)
            out = root / "blocked-output"
            args = argparse.Namespace(
                source=str(world),
                out=str(out),
                translations=str(translations),
                resource_pack="",
                allow_separate_resource_pack=False,
                multi_text_mode="split-nodes",
                min_confidence="medium",
                source_kind="",
                unit_id="",
                translated_only=True,
                dry_run=False,
                report="",
                allow_no_changes=True,
                force=False,
                replace_existing_resource_pack=False,
            )
            self.assertEqual(1, java_tools.apply_hybrid_keys(args))
            self.assertFalse(out.exists())

    def test_audit_excludes_source_language_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lang = root / "assets" / "fixture" / "lang"
            lang.mkdir(parents=True)
            (lang / "en_us.json").write_text(json.dumps({"menu.start": "Start Game"}), encoding="utf-8")
            (lang / "zh_cn.json").write_text(json.dumps({"menu.tnt": "TNT Shop"}), encoding="utf-8")
            out = root / "audit.json"
            args = argparse.Namespace(
                source=str(root),
                out=str(out),
                max_findings=50,
                include_last_output=False,
                target_locale="zh_cn",
                source_locale="en_us",
                include_source_language=False,
            )

            self.assertEqual(0, java_tools.audit_english(args))
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(1, report["skipped_language_file_count"])
            self.assertEqual(1, report["finding_count"])
            self.assertEqual("assets/fixture/lang/zh_cn.json", report["findings"][0]["source_file"])

    def test_delivery_rejects_interim_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scan_report.json").write_text(json.dumps({"map_resource_pack_count": 0}), encoding="utf-8")
            primary = root / "pack.zip"
            primary.write_bytes(b"fixture")
            qa = root / "translation_qa.json"
            qa.write_text(
                json.dumps({"status": "pass", "allow_incomplete": True, "remaining_units": 1}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                project=str(root),
                mode="resource-pack-only",
                primary_output=str(primary),
                resource_pack_output=str(primary),
                translation_qa=str(qa),
                residual_audit="",
                apply_report=[],
                notes="",
                out=str(root / "exports" / "DELIVERY.md"),
            )
            with self.assertRaisesRegex(ValueError, "requires complete translation QA"):
                java_tools.write_delivery(args)

            qa.write_text(
                json.dumps({"status": "pass", "allow_incomplete": False, "remaining_units": 0}),
                encoding="utf-8",
            )
            self.assertEqual(0, java_tools.write_delivery(args))
            self.assertTrue((root / "exports" / "DELIVERY.md").exists())


if __name__ == "__main__":
    unittest.main()
