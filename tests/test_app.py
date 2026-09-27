import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, convert_timeline, parse_fps, seed_demo


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


class PlatformSpecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "spec.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        # 1000ms..3000ms at 24fps = frames 24..72, far inside the 120s demo media.
        self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹出现在冰面", "expected_revision": 0})

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self):
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")

    def test_spec_25fps_keeps_frames_and_shows_both_timecodes(self):
        spec = self.db.create_spec(self.version, "alice", {"platform_name": "PAL 电视台", "source_fps": "24", "target_fps": "25"}, "owner")
        self.assertEqual(spec["status"], "ready")
        self.assertFalse(spec["blocked"])
        cue = spec["cues"][0]
        self.assertEqual((cue["orig_start_frame"], cue["orig_end_frame"]), (24, 72))
        self.assertEqual((cue["new_start_frame"], cue["new_end_frame"]), (24, 72))
        # 24fps wall time -> same frame number at 25fps runs faster: 24*40=960ms.
        self.assertEqual((cue["new_start_ms"], cue["new_end_ms"]), (960.0, 2880.0))
        self.assertEqual(cue["orig_timecode"], "00:00:01:00 --> 00:00:03:00")
        self.assertEqual(cue["new_timecode"], "00:00:00:24 --> 00:00:02:22")
        self.assertEqual(cue["text"], "海豹出现在冰面")
        self._approve()
        delivered = self.db.deliver_spec(spec["id"], "alice")
        self.assertEqual(delivered["status"], "delivered")

    def test_spec_23976_ntsc_runs_slow(self):
        spec = self.db.create_spec(self.version, "alice", {"platform_name": "流媒体", "source_fps": 24, "target_fps": "23.976"}, "owner")
        cue = spec["cues"][0]
        # 24000/1001: frame 24 wall time = 24*1001/24 = 1001ms.
        self.assertEqual(cue["new_start_ms"], 1001.0)
        self.assertEqual(cue["new_end_ms"], 3003.0)
        self.assertEqual(cue["new_timecode"], "00:00:01:00 --> 00:00:03:00")

    def test_empty_duration_blocks_delivery(self):
        # 20ms at 24fps (41.7ms/frame) rounds start and end onto frame 24.
        self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 5000, "end_ms": 5020, "text": "短句", "expected_revision": 1})
        spec = self.db.create_spec(self.version, "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")
        self.assertEqual(spec["status"], "blocked")
        self.assertTrue(spec["blocked"])
        kinds = {(i["cue_index"], i["kind"]) for i in spec["issues"]}
        self.assertIn((2, "empty_duration"), kinds)
        self._approve()
        with self.assertRaisesRegex(DomainError, "已阻止交付"):
            self.db.deliver_spec(spec["id"], "alice")
        self.assertEqual(self.db.get_spec(spec["id"])["status"], "blocked")

    def test_out_of_bounds_is_flagged(self):
        project = self.db.create_project("alice", {"name": "短片", "source_language": "en", "media_name": "s.mp4", "media_sha256": "c" * 64, "duration_ms": 80}, "owner")
        version = self.db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
        self.db.save_cue(version["id"], "alice", {"cue_index": 1, "start_ms": 0, "end_ms": 80, "text": "到片尾"}, "owner")
        spec = self.db.create_spec(version["id"], "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")
        self.assertEqual(spec["status"], "blocked")
        self.assertEqual(spec["issues"][0]["kind"], "out_of_bounds")

    def test_saved_spec_keeps_its_timeline_after_source_changes(self):
        spec = self.db.create_spec(self.version, "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")
        self.assertEqual(spec["source_revision"], 1)
        cue_id = self.db.list_cues(self.version)[0]["id"]
        # 规格生成后负责人继续改原内容（版本仍是草稿）。
        self.db.save_cue(self.version, "bob", {"cue_id": cue_id, "cue_index": 1, "start_ms": 2000, "end_ms": 4000, "text": "改写后的字幕", "expected_revision": 1})
        frozen = self.db.get_spec(spec["id"])
        self.assertEqual(frozen["source_revision"], 1)
        self.assertEqual(frozen["cues"][0]["text"], "海豹出现在冰面")
        self.assertEqual((frozen["cues"][0]["new_start_ms"], frozen["cues"][0]["new_end_ms"]), (960.0, 2880.0))

    def test_multiple_specs_are_queried_separately(self):
        pal = self.db.create_spec(self.version, "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")
        ntsc = self.db.create_spec(self.version, "alice", {"platform_name": "NTSC", "source_fps": "24", "target_fps": "23.976"}, "owner")
        specs = self.db.list_specs(self.version)
        self.assertEqual({s["platform_name"] for s in specs}, {"PAL", "NTSC"})
        self.assertEqual(self.db.get_spec(pal["id"])["target_fps"], "25")
        self.assertEqual(self.db.get_spec(ntsc["id"])["target_fps"], "23.976")
        with self.assertRaisesRegex(DomainError, "已存在"):
            self.db.create_spec(self.version, "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")

    def test_permission_and_unapproved_version_guard(self):
        with self.assertRaisesRegex(DomainError, "只有项目负责人"):
            self.db.create_spec(self.version, "bob", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "translator")
        spec = self.db.create_spec(self.version, "alice", {"platform_name": "PAL", "source_fps": "24", "target_fps": "25"}, "owner")
        with self.assertRaisesRegex(DomainError, "尚未通过复核"):
            self.db.deliver_spec(spec["id"], "alice")
        with self.assertRaisesRegex(DomainError, "帧率"):
            self.db.create_spec(self.version, "alice", {"platform_name": "X", "source_fps": "24", "target_fps": "24"}, "owner")

    def test_convert_timeline_flags_overlap(self):
        fps24, fps25 = parse_fps("24"), parse_fps("25")
        cues = [
            {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "甲"},
            {"cue_index": 2, "start_ms": 2000, "end_ms": 4000, "text": "乙"},
        ]
        _, issues = convert_timeline(cues, fps24, fps25, 120000)
        pairs = {(i["cue_index"], i["kind"]) for i in issues}
        self.assertEqual(pairs, {(1, "overlap"), (2, "overlap")})
        self.assertEqual({i["related_index"] for i in issues}, {2, 1})


if __name__ == "__main__":
    unittest.main()
