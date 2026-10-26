"""Orden de generación y formato de los assets públicos, sin red ni cuota."""
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import test_sync as fixtures
from test_sync import A, B, C, D, NOW, FakeAPI, page, video
import sync


class VideoOrder(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    run_sync = fixtures.Integration.run_sync
    published = fixtures.Integration.published
    create_existing = fixtures.Integration.create_existing

    def test_new_channel_randomizes_across_sources_and_pages_by_default(self):
        self.config["channels"][0].pop("videoOrder")
        self.config["channels"][0]["sources"] = ["PLabcdefghijklmnop", D]
        self.write_config()
        api = FakeAPI({identifier: video(identifier) for identifier in (A, B, C, D)},
                      {("PLabcdefghijklmnop", None): page([A, B], "p2"),
                       ("PLabcdefghijklmnop", "p2"): page([B, C])})
        with patch.object(sync.random, "shuffle", side_effect=lambda rows: rows.reverse()) as shuffle:
            self.assertEqual(0, self.run_sync(api)[0])
        shuffle.assert_called_once()
        self.assertEqual([D, C, B, A], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_append_randomizes_only_new_batch_and_preserves_noop_and_current_phase(self):
        self.create_existing()
        old = self.published()
        self.config["channels"][0].update(videoOrder="random", sources=[C, D])
        self.write_config()
        with patch.object(sync.random, "shuffle", side_effect=lambda rows: rows.reverse()) as shuffle:
            self.run_sync(FakeAPI({C: video(C), D: video(D)}))
        shuffle.assert_called_once()
        after = self.published()
        self.assertEqual([A, B, D, C], [row["youtubeVideoId"] for row in after["videos"]])
        self.assertEqual(old["videos"], after["videos"][:2])
        self.assertEqual(sync.anchor_update(old, after["videos"], NOW), after["cycleStartsAt"])
        path = self.root / "channels/test.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with patch.object(sync.random, "shuffle") as shuffle:
            self.run_sync(now=NOW + timedelta(days=1))
        shuffle.assert_not_called()
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_ordered_preserves_source_and_page_order_in_append_and_replace(self):
        for update_mode in ("append", "replace"):
            with self.subTest(update_mode=update_mode):
                self.create_existing()
                self.config["channels"][0].update(videoOrder="ordered", updateMode=update_mode,
                                                  sources=[D, "PLabcdefghijklmnop"])
                self.write_config()
                api = FakeAPI({identifier: video(identifier) for identifier in (A, B, C, D)},
                              {("PLabcdefghijklmnop", None): page([C], "p2"),
                               ("PLabcdefghijklmnop", "p2"): page([A])})
                with patch.object(sync.random, "shuffle") as shuffle:
                    self.run_sync(api)
                shuffle.assert_not_called()
                expected = [A, B, D, C] if update_mode == "append" else [D, C, A]
                self.assertEqual(expected, [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_replace_randomizes_whole_rebuilt_list_and_preserves_phase(self):
        self.create_existing()
        old = self.published()
        self.config["channels"][0].update(videoOrder="random", updateMode="replace", sources=[A, B, C])
        self.write_config()
        with patch.object(sync.random, "shuffle", side_effect=lambda rows: rows.reverse()):
            self.run_sync(FakeAPI({identifier: video(identifier) for identifier in (A, B, C)}))
        after = self.published()
        self.assertEqual([C, B, A], [row["youtubeVideoId"] for row in after["videos"]])
        self.assertEqual(sync.anchor_update(old, after["videos"], NOW), after["cycleStartsAt"])

    def test_metadata_and_maintenance_modes_keep_random_channel_order(self):
        for mode in ("maintenance", "titles", "names", "presentation"):
            with self.subTest(mode=mode):
                self.create_existing()
                self.config["channels"][0]["videoOrder"] = "random"
                self.write_config()
                with patch.object(sync.random, "shuffle") as shuffle:
                    self.run_sync(FakeAPI({A: video(), B: video(B)}), mode=mode)
                shuffle.assert_not_called()
                self.assertEqual([A, B], [row["youtubeVideoId"] for row in self.published()["videos"]])
                self.assert_minified(self.root / "channels/test.json")

    def test_incomplete_replace_does_not_shuffle_or_write(self):
        self.create_existing()
        path = self.root / "channels/test.json"
        before = path.read_bytes()
        self.config["channels"][0].update(videoOrder="random", updateMode="replace",
                                          sources=["PLabcdefghijklmnop"])
        self.write_config()
        api = FakeAPI({C: video(C), D: video(D)},
                      {("PLabcdefghijklmnop", None): page([C, D], "p2")}, fail="p2")
        with patch.object(sync.random, "shuffle") as shuffle:
            self.assertEqual(2, self.run_sync(api)[0])
        shuffle.assert_not_called()
        self.assertEqual(before, path.read_bytes())

    def test_invalid_video_order_rejected_before_api_calls(self):
        for value in (None, True, 1, [], {}, "", "shuffle", "ORDERED"):
            with self.subTest(value=value):
                self.config["channels"][0]["videoOrder"] = value
                self.write_config()
                api = FakeAPI()
                with self.assertRaisesRegex(sync.SyncError, "videoOrder"):
                    self.run_sync(api)
                self.assertEqual([], api.calls)

    def assert_minified(self, path):
        value = sync.load(path)
        self.assertEqual(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                         path.read_bytes())

    def test_public_files_minified_on_creation_and_equivalent_legacy_files_migrated_once(self):
        self.run_sync(FakeAPI({A: video()}))
        for name in ("channels.json", "channels/test.json"):
            path = self.root / name
            self.assert_minified(path)
            value = sync.load(path)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.run_sync()
        for name in ("channels.json", "channels/test.json"):
            path = self.root / name
            self.assert_minified(path)
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            self.assertFalse(sync.save(path, sync.load(path), compact=True))
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertIn("\n  ", (self.root / "sources.json").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "sync-state.json").exists())
