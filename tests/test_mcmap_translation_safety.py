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
        rows = []
        for source_file, nbt_path in (
            ("region/r.0.0.mca", "root.block_entities[0].Offers.Recipes[0].buy.components.minecraft:custom_name"),
            ("region/r.0.0.mca", "root.block_entities[2].Items[0].components.minecraft:custom_name"),
        ):
            rows.append(
                java_tools.make_unit(
                    edition="java",
                    source_kind="item_name",
                    source_file=source_file,
                    address={"nbt_path": nbt_path, "json_path": "$"},
                    raw="Kitatcho Coin",
                    mode_support=["hybrid-key-injection"],
                    confidence="high",
                    resource_namespace="mcmap",
                    translation_key="temporary.key",
                    context={"text_nodes": [{"json_path": "$.text", "text": "Kitatcho Coin"}]},
                )
            )

        summary = java_tools.canonicalize_identity_keys(rows, "mcmap", "fixture")

        self.assertEqual(1, summary["group_count"])
        self.assertEqual(2, summary["max_group_size"])
        self.assertEqual(1, len({row["translation_key"] for row in rows}))
        self.assertTrue(all(row["context"]["identity_coupled"] for row in rows))
        self.assertEqual("trade_input", rows[0]["context"]["identity_role"])

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
