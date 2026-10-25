from copy import deepcopy
import unittest
import test_sync as fixtures
from test_sync import FakeAPI, video, A, B, NOW
import sync


class Titles(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    run_sync = fixtures.Integration.run_sync
    published = fixtures.Integration.published
    create_existing = fixtures.Integration.create_existing

    def test_backfill_preserves_order_duplicates_durations_origin_and_manual_fields(self):
        self.create_existing((A, B, A), (600, 900, 600))
        before = self.published()
        api = FakeAPI({A: video(A), B: video(B)})
        self.assertEqual(0, self.run_sync(api, mode="titles")[0])
        expected = deepcopy(before)
        for row in expected["videos"]:
            row["title"] = "Una pieza"
        self.assertEqual(expected, self.published())
        self.assertEqual([("videos", {"ids": [A, B]})], api.calls)
        stamp = (self.root / "channels/test.json").stat().st_mtime_ns
        self.run_sync(FakeAPI({A: video(A), B: video(B)}), mode="titles")
        self.assertEqual(stamp, (self.root / "channels/test.json").stat().st_mtime_ns)

    def test_title_only_daily_update_does_not_reanchor_cycle(self):
        self.create_existing()
        before = self.published()
        self.run_sync(FakeAPI({A: video(A), B: video(B)}))
        self.assertEqual(before["cycleStartsAt"], self.published()["cycleStartsAt"])
        self.assertTrue(all(row.get("title") for row in self.published()["videos"]))

    def test_unavailable_title_and_network_error_preserve_last_good_data(self):
        self.create_existing()
        before = self.published()
        self.run_sync(FakeAPI(), mode="titles")
        self.assertEqual(before, self.published())
        result, _ = self.run_sync(FakeAPI(fail=sync.SyncError("Temporal")), mode="titles")
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())
