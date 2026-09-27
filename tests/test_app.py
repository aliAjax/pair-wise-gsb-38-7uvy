import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class SubtitleQCFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["version_revision"], 1)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


class DeliverySpecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.cue1 = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "第一句", "expected_revision": 0})
        self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3000, "end_ms": 5000, "text": "第二句", "expected_revision": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def test_frame_conversion_and_ready_delivery(self):
        spec = self.db.create_spec(self.version, "alice", {"platform": "极光TV", "target_fps": 23.976}, "owner")
        self.assertEqual(spec["status"], "ready")
        self.assertEqual(spec["source_fps"], 25.0)
        self.assertEqual(spec["target_fps"], 23.976)
        self.assertEqual(spec["source_revision"], 2)
        rows = {r["cue_index"]: r for r in spec["rows"]}
        # 25fps 下第 25/75/125 帧，折到 23.976fps 后时间轴整体拉长
        self.assertEqual((rows[1]["orig_start_ms"], rows[1]["orig_end_ms"]), (1000, 3000))
        self.assertEqual((rows[1]["new_start_ms"], rows[1]["new_end_ms"]), (1043, 3128))
        self.assertEqual((rows[2]["new_start_ms"], rows[2]["new_end_ms"]), (3128, 5214))
        delivered = self.db.deliver_spec(spec["id"], "alice", "owner")
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(len(delivered["snapshot_hash"]), 64)
        with self.assertRaisesRegex(DomainError, "重复交付"):
            self.db.deliver_spec(spec["id"], "alice", "owner")

    def test_spec_snapshot_survives_later_edits_and_specs_query_separately(self):
        first = self.db.create_spec(self.version, "alice", {"platform": "极光TV", "target_fps": 23.976}, "owner")
        self.db.save_cue(self.version, "bob", {"cue_id": self.cue1["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "第一句改", "expected_revision": 2})
        second = self.db.create_spec(self.version, "alice", {"platform": "极光TV", "target_fps": 23.976}, "owner")
        other = self.db.create_spec(self.version, "alice", {"platform": "星河OTT", "target_fps": 25}, "owner")
        # 已保存规格仍按当时时间轴保留
        kept = self.db.get_spec(first["id"])
        self.assertEqual(kept["source_revision"], 2)
        self.assertEqual({r["cue_index"]: (r["orig_end_ms"], r["text"]) for r in kept["rows"]}[1], (3000, "第一句"))
        # 同平台新规格递增序号并反映新时间轴
        self.assertEqual(second["spec_no"], 2)
        self.assertEqual({r["cue_index"]: r["orig_end_ms"] for r in second["rows"]}[1], 2500)
        # 25→25 帧率相同，时码不变
        self.assertEqual({r["cue_index"]: (r["new_start_ms"], r["new_end_ms"]) for r in other["rows"]}[2], (3000, 5000))
        specs = self.db.list_specs(self.version)
        self.assertEqual(len(specs), 3)
        self.assertEqual({s["id"] for s in specs}, {first["id"], second["id"], other["id"]})

    def test_zero_duration_after_conversion_blocks_delivery(self):
        self.db.save_cue(self.version, "bob", {"cue_index": 3, "start_ms": 6000, "end_ms": 6010, "text": "短句", "expected_revision": 2})
        spec = self.db.create_spec(self.version, "alice", {"platform": "极光TV", "target_fps": 23.976}, "owner")
        self.assertEqual(spec["status"], "blocked")
        self.assertTrue(any(i["cue_index"] == 3 and i["type"] == "empty_duration" for i in spec["issues"]))
        with self.assertRaisesRegex(DomainError, "第3句"):
            self.db.deliver_spec(spec["id"], "alice", "owner")

    def test_out_of_bounds_after_conversion_blocks_delivery(self):
        spec = self.db.create_spec(self.version, "alice", {"platform": "短视频平台", "target_fps": 25, "media_duration_ms": 4000}, "owner")
        self.assertEqual(spec["status"], "blocked")
        self.assertTrue(any(i["cue_index"] == 2 and i["type"] == "out_of_bounds" for i in spec["issues"]))
        with self.assertRaisesRegex(DomainError, "第2句"):
            self.db.deliver_spec(spec["id"], "alice", "owner")

    def test_overlap_validation_reports_cue_indexes(self):
        rows = [
            {"cue_index": 4, "text": "甲", "orig_start_ms": 0, "orig_end_ms": 2000, "new_start_ms": 0, "new_end_ms": 2100},
            {"cue_index": 5, "text": "乙", "orig_start_ms": 2000, "orig_end_ms": 3000, "new_start_ms": 2000, "new_end_ms": 3000},
        ]
        issues = Database._validate_spec_rows(rows, 120000)
        overlaps = [i for i in issues if i["type"] == "overlap"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["cue_index"], 5)
        self.assertIn("第5句", overlaps[0]["message"])
        self.assertIn("第4句", overlaps[0]["message"])

    def test_spec_permissions_and_validation(self):
        with self.assertRaisesRegex(DomainError, "项目负责人"):
            self.db.create_spec(self.version, "bob", {"platform": "极光TV", "target_fps": 23.976}, "translator")
        with self.assertRaisesRegex(DomainError, "目标帧率"):
            self.db.create_spec(self.version, "alice", {"platform": "极光TV"}, "owner")
        with self.assertRaisesRegex(DomainError, "平台名称"):
            self.db.create_spec(self.version, "alice", {"platform": " ", "target_fps": 25}, "owner")
        spec = self.db.create_spec(self.version, "alice", {"platform": "极光TV", "target_fps": 23.976}, "owner")
        with self.assertRaisesRegex(DomainError, "项目负责人"):
            self.db.deliver_spec(spec["id"], "bob", "translator")


if __name__ == "__main__":
    unittest.main()
