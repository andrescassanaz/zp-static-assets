from io import BytesIO
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import test_sync as fixtures
from test_sync import FakeAPI, video, A, B
import sync


class ChannelNames(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    run_sync = fixtures.Integration.run_sync
    published = fixtures.Integration.published
    create_existing = fixtures.Integration.create_existing

    def test_daily_backfills_named_videos_without_changing_playback(self):
        self.create_existing((A, B, A), (600, 900, 600))
        before = self.published()
        for row in before["videos"]:
            row["title"] = "Título existente"
        sync.save(self.root / "channels/test.json", before)
        api = FakeAPI({A: video(A), B: video(B)})
        self.assertEqual(0, self.run_sync(api)[0])
        after = self.published()
        self.assertTrue(all(row.pop("youtubeChannelName") == "Ramiro Diz" for row in after["videos"]))
        self.assertEqual(before, after)
        self.assertEqual([("videos", {"ids": [A, B]})], api.calls)

    def test_playlist_uploader_and_case_are_preserved_in_append_replace_and_maintenance(self):
        for mode, replace in [("daily", False), ("daily", True), ("maintenance", False)]:
            with self.subTest(mode=mode, replace=replace):
                self.create_existing((A,), (600,))
                self.config["channels"][0]["sources"] = ["PLotherOwnerPlaylist"]
                self.config["channels"][0]["updateMode"] = "replace" if replace else "append"
                self.write_config()
                item = video()
                item["snippet"]["channelTitle"] = "  Ramiro Diz · AC/DC 日本 🎬  "
                api = FakeAPI({A: item}, {("PLotherOwnerPlaylist", None): fixtures.page([A])})
                self.assertEqual(0, self.run_sync(api, mode=mode)[0])
                self.assertEqual("Ramiro Diz · AC/DC 日本 🎬", self.published()["videos"][0]["youtubeChannelName"])

    def test_missing_or_invalid_api_name_and_failure_keep_last_good_name(self):
        self.create_existing((A,), (600,))
        original = self.published()
        original["videos"][0].update(title="Una pieza", youtubeChannelName="Nombre anterior")
        sync.save(self.root / "channels/test.json", original)
        for name in (None, "", "   ", 42):
            item = video()
            item["snippet"]["channelTitle"] = name
            self.run_sync(FakeAPI({A: item}), mode="titles")
            self.assertEqual(original, self.published())
        self.assertEqual(2, self.run_sync(FakeAPI(fail=sync.QuotaError("Sin cuota")), mode="titles")[0])
        self.assertEqual(original, self.published())

    def test_optional_name_validation_supports_legacy_and_live(self):
        for mode in ("live", "cycle"):
            body = {"videos": [{"youtubeVideoId": A}]}
            if mode == "cycle":
                body.update(cycleStartsAt="2026-10-25T00:00:00Z")
                body["videos"][0]["durationSeconds"] = 600
            sync.validate_playlist(body, mode)
            body["videos"][0]["youtubeChannelName"] = "Ramiro Diz"
            sync.validate_playlist(body, mode)
            for bad in (42, "", " "):
                body["videos"][0]["youtubeChannelName"] = bad
                with self.assertRaises(sync.SyncError):
                    sync.validate_playlist(body, mode)

    def test_transport_requests_channel_title_in_same_metadata_batch(self):
        urls = []
        def opener(url, **_):
            urls.append(url)
            return BytesIO(json.dumps({"items": [video()]}).encode())
        api = sync.YouTube("synthetic-test-key", 1, opener=opener)
        self.assertEqual("Ramiro Diz", api.videos([A])[A]["snippet"]["channelTitle"])
        self.assertIn("channelTitle", parse_qs(urlsplit(urls[0]).query)["fields"][0])
        api.videos([A])
        self.assertEqual(1, len(urls))

    def test_names_mode_changes_only_missing_names_and_resumes_without_duplicate_requests(self):
        self.create_existing((A, B, A), (600, 900, 600))
        before = self.published()
        for row in before["videos"]:
            row["title"] = "🔥 MI TÍTULO MANUAL 🔥"
            if row["youtubeVideoId"] == A:
                row["youtubeChannelName"] = "Nombre verificado"
        sync.save(self.root / "channels/test.json", before)
        api = FakeAPI({B: video(B)})
        self.assertEqual(0, self.run_sync(api, mode="names")[0])
        after = self.published()
        self.assertEqual("Ramiro Diz", after["videos"][1].pop("youtubeChannelName"))
        self.assertEqual(before, after)
        self.assertEqual([("videos", {"ids": [B]})], api.calls)
        again = FakeAPI()
        self.run_sync(again, mode="names")
        self.assertEqual([], again.calls)

    def test_names_mode_preserves_missing_metadata_without_removing_or_inventing_a_video(self):
        self.create_existing()
        before = self.published()
        result, _ = self.run_sync(FakeAPI(), mode="names")
        self.assertEqual(0, result)
        self.assertEqual(before, self.published())
        report = self.engine.report["channelNames"]
        self.assertEqual(2, report["missingVideos"])
        self.assertEqual(0, report["namedVideos"])
        result, _ = self.run_sync(FakeAPI(fail=sync.QuotaError("Sin cuota")), mode="names")
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())

    def test_names_mode_saves_a_verified_batch_before_quota_failure(self):
        ids = tuple(f"{index:011d}" for index in range(51))
        self.create_existing(ids, (600,) * len(ids))
        before = self.published()
        class FailSecondBatch(FakeAPI):
            def videos(self, ids):
                if self.used:
                    raise sync.QuotaError("Sin cuota")
                return super().videos(ids)
        result, _ = self.run_sync(FailSecondBatch({identifier: video(identifier) for identifier in ids}), mode="names")
        self.assertEqual(2, result)
        after = self.published()
        for row in after["videos"][:50]:
            self.assertEqual("Ramiro Diz", row.pop("youtubeChannelName"))
        self.assertEqual(before, after)

    def test_quota_failure_after_a_batch_keeps_verified_names_and_original_schedule(self):
        ids = tuple(f"{index:011d}" for index in range(51))
        self.create_existing(ids, (600,) * len(ids))
        before = self.published()
        class FailSecondBatch(FakeAPI):
            def videos(self, ids):
                if self.used:
                    raise sync.QuotaError("Sin cuota")
                return super().videos(ids)
        result, _ = self.run_sync(FailSecondBatch({identifier: video(identifier) for identifier in ids}), mode="titles")
        self.assertEqual(2, result)
        after = self.published()
        self.assertEqual(before["cycleStartsAt"], after["cycleStartsAt"])
        self.assertEqual(list(ids), [row["youtubeVideoId"] for row in after["videos"]])
        for row in after["videos"][:50]:
            self.assertEqual("Ramiro Diz", row.pop("youtubeChannelName"))
            row.pop("title")
        self.assertEqual(before, after)
