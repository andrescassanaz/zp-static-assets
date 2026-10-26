"""Pruebas de integración con API simulada. No leen claves ni usan la red."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync

A, B, C, D = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def video(identifier=A, seconds=600, audio="es-AR", **extra):
    item = {"id": identifier, "snippet": {"title": "Una pieza", "channelTitle": "Ramiro Diz", "channelId": "UC" + "a" * 22,
            "publishedAt": "2026-10-20T00:00:00Z", "defaultAudioLanguage": audio, "liveBroadcastContent": "none"},
            "contentDetails": {"duration": f"PT{seconds}S"},
            "status": {"privacyStatus": "public", "uploadStatus": "processed", "embeddable": True}}
    item.update(extra)
    return item


def page(ids, token=None):
    result = {"items": [{"contentDetails": {"videoId": identifier}} for identifier in ids]}
    if token:
        result["nextPageToken"] = token
    return result


class FakeAPI:
    def __init__(self, videos=None, pages=None, fail=None):
        self.items = videos or {}
        self.pages = pages or {}
        self.fail = fail
        self.used = 0
        self.calls = []

    def get(self, resource, **params):
        self.used += 1
        self.calls.append((resource, params))
        if resource == "channels":
            return {"items": [{"id": "UC" + "a" * 22, "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}}}]}
        token = params.get("pageToken")
        if self.fail == token or isinstance(self.fail, Exception):
            if self.fail is not None:
                raise self.fail if isinstance(self.fail, Exception) else sync.SyncError("YouTube HTTP 503")
        return deepcopy(self.pages.get((params["playlistId"], token), {"items": []}))

    def videos(self, ids):
        self.used += 1
        self.calls.append(("videos", {"ids": list(ids)}))
        if isinstance(self.fail, Exception):
            raise self.fail
        return {identifier: deepcopy(self.items.get(identifier)) for identifier in ids}


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        sync.save(self.root / "channels.json", {"channels": []})
        self.config = {"version": 1, "quotaBudget": 100,
                       "channels": [{"id": "test", "name": "Prueba", "sources": [A]}]}
        self.write_config()

    def write_config(self):
        sync.save(self.root / "sources.json", self.config)

    def run_sync(self, api=None, mode="daily", now=NOW):
        engine = sync.Sync(self.root, api or FakeAPI(), now)
        return engine.run(mode), engine

    def published(self):
        return sync.load(self.root / "channels/test.json")

    def create_existing(self, ids=(A, B), durations=(600, 900)):
        sync.save(self.root / "channels.json", {"extra": "manual", "channels": [{"id": "test", "number": 7,
                  "name": "Nombre manual", "playbackMode": "cycle", "file": "channels/test.json", "logo": "manual"}]})
        sync.save(self.root / "channels/test.json", {"cycleStartsAt": sync.stamp(NOW - timedelta(seconds=3700)), "custom": "manual",
                  "videos": [{"youtubeVideoId": identifier, "durationSeconds": seconds, "note": "manual"}
                             for identifier, seconds in zip(ids, durations)]})

    def test_create_channel_and_compatible_json_without_catalog(self):
        result, _ = self.run_sync(FakeAPI({A: video()}))
        self.assertEqual(0, result)
        self.assertEqual({"cycleStartsAt": sync.stamp(NOW), "videos": [{"youtubeVideoId": A, "durationSeconds": 600, "title": "Una pieza", "youtubeChannelName": "Ramiro Diz"}]}, self.published())
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual({"id": "test", "name": "Prueba", "number": 1, "file": "channels/test.json", "playbackMode": "cycle"}, entry)
        self.assertFalse((self.root / "catalog").exists())
        state = sync.load(self.root / "sync-state.json")
        self.assertNotIn("knownIds", str(state))
        self.assertNotIn("publishedIds", str(state))
        self.assertNotIn("pageToken", str(state))

    def test_channel_not_published_without_valid_videos(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        self.assertFalse((self.root / "channels/test.json").exists())
        self.assertEqual([], sync.load(self.root / "channels.json")["channels"])

    def test_noop_keeps_public_bytes_and_mtime(self):
        self.run_sync(FakeAPI({A: video()}))
        path = self.root / "channels/test.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        api = FakeAPI()
        self.run_sync(api, now=NOW + timedelta(days=1))
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual([], api.calls)

    def test_existing_metadata_and_order_preserved_append_only(self):
        self.create_existing()
        before = self.published()
        self.config["channels"][0]["sources"] = [C, A]
        self.write_config()
        self.run_sync(FakeAPI({C: video(C)}))
        after = self.published()
        self.assertEqual(before["videos"], after["videos"][:2])
        self.assertEqual("manual", after["custom"])
        self.assertEqual([A, B, C], [row["youtubeVideoId"] for row in after["videos"]])
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual("Nombre manual", entry["name"])
        self.assertEqual(7, entry["number"])
        self.assertEqual("manual", entry["logo"])

    def test_full_upload_history_even_when_first_page_already_exists(self):
        self.create_existing()
        self.config["channels"][0]["sources"] = ["@autor"]
        self.write_config()
        pages = {("UUuploads", None): page([A], "p2"), ("UUuploads", "p2"): page([B], "p3"), ("UUuploads", "p3"): page([C])}
        api = FakeAPI({C: video(C)}, pages)
        self.run_sync(api)
        self.assertEqual([None, "p2", "p3"], [params.get("pageToken") for resource, params in api.calls if resource == "playlistItems"])
        self.assertEqual([[C], [A, B]], [params["ids"] for resource, params in api.calls if resource == "videos"])
        self.assertEqual([A, B, C], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_full_playlist_scanned_again_and_tail_insertion_found(self):
        source = "PLabcdefghijklmnop"
        self.config["channels"][0]["sources"] = [source]
        self.write_config()
        self.run_sync(FakeAPI({A: video(), B: video(B)}, {(source, None): page([A], "p2"), (source, "p2"): page([B])}))
        pages = {(source, None): page([A], "p2"), (source, "p2"): page([B], "p3"), (source, "p3"): page([C], "p4"), (source, "p4"): page([D])}
        api = FakeAPI({C: video(C), D: video(D)}, pages)
        self.run_sync(api, now=NOW + timedelta(days=1))
        self.assertEqual([None, "p2", "p3", "p4"], [params.get("pageToken") for resource, params in api.calls if resource == "playlistItems"])
        self.assertEqual([A, B, C, D], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertEqual([[C], [D]], [params["ids"] for resource, params in api.calls if resource == "videos"])

    def test_duplicate_across_sources_and_pages_queried_once(self):
        source = "PLabcdefghijklmnop"
        self.config["channels"][0]["sources"] = [A, source]
        self.write_config()
        api = FakeAPI({A: video(), B: video(B)}, {(source, None): page([A, A, B], "p2"), (source, "p2"): page([A, B])})
        self.run_sync(api)
        self.assertEqual([[A], [B]], [params["ids"] for resource, params in api.calls if resource == "videos"])
        self.assertEqual([A, B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_existing_repeated_rows_are_preserved(self):
        self.create_existing((A, A), (600, 600))
        before = self.published()
        self.run_sync(FakeAPI())
        self.assertEqual(before, self.published())

    def test_exclusion_blocks_readdition_and_removes_existing_id(self):
        self.create_existing()
        self.config["channels"][0]["excludedVideoIds"] = [A]
        self.write_config()
        api = FakeAPI({A: video()})
        self.run_sync(api)
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertEqual([("videos", {"ids": [B]})], api.calls)

    def test_deleted_id_without_exclusion_is_compared_as_new(self):
        self.create_existing((B,), (900,))
        self.run_sync(FakeAPI({A: video()}))
        self.assertEqual([B, A], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_invalid_exclusion_fails_before_any_calls(self):
        self.config["channels"][0]["excludedVideoIds"] = ["invalid"]
        self.write_config()
        with self.assertRaises(sync.SyncError):
            self.run_sync(FakeAPI())

    def test_missing_video_removed_during_maintenance(self):
        self.create_existing()
        self.run_sync(FakeAPI({B: video(B)}), mode="maintenance")
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_all_unavailable_holds_last_nonempty_programming(self):
        self.create_existing()
        before = self.published()
        self.run_sync(FakeAPI(), mode="maintenance")
        self.assertEqual(before, self.published())
        state = sync.load(self.root / "sync-state.json")
        self.assertEqual("held_empty", state["channels"]["test"]["publication"])

    def test_existing_duration_and_language_not_rewritten(self):
        self.create_existing()
        self.run_sync(FakeAPI({A: video(seconds=750), B: video(B, audio=None)}), mode="maintenance")
        self.assertEqual(600, self.published()["videos"][0]["durationSeconds"])
        self.assertEqual([A, B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_pending_metadata_is_retried_from_sources_next_run(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        self.assertFalse((self.root / "channels/test.json").exists())
        self.run_sync(FakeAPI({A: video()}), now=NOW + timedelta(days=1))
        self.assertEqual(A, self.published()["videos"][0]["youtubeVideoId"])

    def test_source_failure_keeps_verified_pages_and_other_sources_work(self):
        self.config["channels"][0]["sources"] = ["@autor", B]
        self.write_config()
        api = FakeAPI({A: video(), B: video(B)}, {("UUuploads", None): page([A], "p2")}, fail="p2")
        result, _ = self.run_sync(api)
        self.assertEqual(2, result)
        self.assertEqual([A, B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_quota_error_preserves_existing_programming(self):
        self.create_existing()
        before = self.published()
        result, _ = self.run_sync(FakeAPI(fail=sync.QuotaError("Presupuesto agotado")), mode="maintenance")
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())

    def test_metadata_failure_does_not_append_unverified_id(self):
        self.create_existing((B,), (900,))
        before = self.published()
        result, _ = self.run_sync(FakeAPI(fail=sync.SyncError("Error de red")))
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())

    def test_inactive_channel_untouched(self):
        self.config["channels"][0]["active"] = False
        self.write_config()
        api = FakeAPI()
        self.run_sync(api)
        self.assertEqual([], api.calls)
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_duplicate_explicit_number_rejected(self):
        self.config["channels"][0]["number"] = 1
        self.config["channels"].append({"id": "other", "name": "Otro", "number": 1, "sources": []})
        self.write_config()
        with self.assertRaises(sync.SyncError):
            sync.Sync(self.root, None)

    def test_live_creation_compatible(self):
        self.config["channels"][0]["playbackMode"] = "live"
        self.write_config()
        item = video(liveStreamingDetails={"actualStartTime": "2026-10-23T00:00:00Z"})
        self.run_sync(FakeAPI({A: item}))
        self.assertEqual({"videos": [{"youtubeVideoId": A, "title": "Una pieza", "youtubeChannelName": "Ramiro Diz"}]}, self.published())


class Refresh(unittest.TestCase):
    write_config = Integration.write_config
    run_sync = Integration.run_sync
    published = Integration.published
    create_existing = Integration.create_existing

    def setUp(self):
        Integration.setUp(self)
        self.channel = self.config["channels"][0]
        self.channel.update(updateMode="replace", rules={"maxAgeDays": 60})
        self.write_config()

    def test_replace_rebuilds_from_sources_and_revalidates_existing_ids(self):
        self.create_existing()
        self.channel["sources"] = [A, C, D]
        self.write_config()
        api = FakeAPI({A: video(), C: video(C), D: video(D, audio="en")})
        result, _ = self.run_sync(api)
        self.assertEqual(0, result)
        self.assertEqual([A, C], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertEqual("manual", self.published()["custom"])
        self.assertEqual([[A], [C], [D]], [params["ids"] for resource, params in api.calls if resource == "videos"])
        stats = sync.load(self.root / "sync-state.json")["channels"]["test"]
        self.assertEqual(1, stats["addedVideos"])
        self.assertEqual(1, stats["removedVideos"])

    def test_window_includes_cutoff_but_rejects_older_future_and_unknown_dates(self):
        self.channel["sources"] = ["PLabcdefghijklmnop"]
        self.channel["rules"]["maxAgeDays"] = 2
        self.write_config()
        now = sync.date("2026-10-24T17:08:00Z")
        cutoff = "2026-10-22T17:08:00Z"
        for published, accepted in [(cutoff, True), ("2026-10-22T17:07:59Z", False),
                                    ("2026-10-24T17:08:00Z", True), ("2026-10-24T17:08:01Z", False),
                                    (None, False), ("bad", False), ("2026-10-20", False),
                                    ("2026-10-22T14:08:00-03:00", True)]:
            with self.subTest(published=published):
                item = video()
                item["snippet"]["publishedAt"] = published
                api = FakeAPI({A: item}, {("PLabcdefghijklmnop", None): page([A])})
                result, _ = self.run_sync(api, now=now)
                self.assertEqual(0, result)
                self.assertEqual(accepted, (self.root / "channels/test.json").exists())

    def test_append_window_only_filters_incoming_videos(self):
        self.create_existing((B,), (900,))
        self.channel["updateMode"] = "append"
        self.write_config()
        item = video()
        item["snippet"]["publishedAt"] = "2026-08-01T00:00:00Z"
        self.run_sync(FakeAPI({A: item}))
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_age_window_inherited_from_defaults_and_overridden_with_null(self):
        self.channel.pop("rules")
        self.config["defaults"] = {"maxAgeDays": 60}
        self.write_config()
        item = video()
        item["snippet"]["publishedAt"] = "2026-01-01T00:00:00Z"
        self.run_sync(FakeAPI({A: item}))
        self.assertFalse((self.root / "channels/test.json").exists())
        self.channel["rules"] = {"maxAgeDays": None}
        self.write_config()
        self.run_sync(FakeAPI({A: item}))
        self.assertEqual(A, self.published()["videos"][0]["youtubeVideoId"])

    def test_window_moves_on_next_run_and_expires_previously_accepted_video(self):
        self.run_sync(FakeAPI({A: video()}))
        self.assertTrue((self.root / "channels/test.json").exists())
        self.run_sync(FakeAPI({A: video()}), now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertFalse((self.root / "channels/test.json").exists())
        self.assertEqual([], sync.load(self.root / "channels.json")["channels"])
        self.run_sync(FakeAPI({A: video()}))
        self.assertTrue((self.root / "channels/test.json").exists())

    def test_old_playlist_dates_skip_metadata_without_stopping_unordered_pages(self):
        source = "PLabcdefghijklmnop"
        self.channel["sources"] = [source]
        self.write_config()
        old_page = page([A], "p2")
        old_page["items"][0]["contentDetails"].update(videoPublishedAt="2026-01-01T00:00:00Z")
        api = FakeAPI({B: video(B)}, {(source, None): old_page, (source, "p2"): page([B])})
        self.run_sync(api)
        self.assertEqual([[B]], [params["ids"] for resource, params in api.calls if resource == "videos"])
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_replace_failure_on_later_page_preserves_complete_old_file(self):
        self.create_existing()
        before = (self.root / "channels/test.json").read_bytes()
        self.channel["sources"] = ["@autor", D]
        self.write_config()
        api = FakeAPI({C: video(C), D: video(D)}, {("UUuploads", None): page([C], "p2")}, fail="p2")
        result, _ = self.run_sync(api)
        self.assertEqual(2, result)
        self.assertEqual(before, (self.root / "channels/test.json").read_bytes())
        self.assertEqual("held_incomplete", sync.load(self.root / "sync-state.json")["channels"]["test"]["publication"])

    def test_replace_quota_failure_and_failed_first_run_do_not_publish_partial(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.create_existing()
                result, _ = self.run_sync(FakeAPI(fail=sync.QuotaError("Sin cuota")))
                self.assertEqual(2, result)
                self.assertEqual(existing, (self.root / "channels/test.json").exists())

    def test_replace_without_window_replaces_all_history_and_noop_keeps_bytes(self):
        self.create_existing()
        self.channel["rules"]["maxAgeDays"] = None
        self.write_config()
        item = video()
        item["snippet"].pop("publishedAt")
        self.run_sync(FakeAPI({A: item}))
        path = self.root / "channels/test.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.run_sync(FakeAPI({A: item}), now=NOW + timedelta(days=1))
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_replace_maintenance_removes_expired_videos(self):
        self.create_existing()
        old_item = video()
        old_item["snippet"]["publishedAt"] = "2026-01-01T00:00:00Z"
        self.run_sync(FakeAPI({A: old_item, B: video(B)}), mode="maintenance")
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_replace_deduplicates_sources_and_honors_exclusions(self):
        source = "PLabcdefghijklmnop"
        self.channel.update(sources=[A, source], excludedVideoIds=[B])
        self.write_config()
        api = FakeAPI({A: video(), B: video(B), C: video(C)}, {(source, None): page([A, B, C, C])})
        self.run_sync(api)
        self.assertEqual([A, C], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertEqual([[A], [C]], [params["ids"] for resource, params in api.calls if resource == "videos"])

    def test_replace_metadata_error_after_verified_source_preserves_old(self):
        self.create_existing()
        before = self.published()
        self.channel["sources"] = [C, D]
        self.write_config()
        class MetadataFailure(FakeAPI):
            def videos(self, ids):
                if D in ids:
                    raise sync.SyncError("Metadatos incompletos")
                return super().videos(ids)
        result, _ = self.run_sync(MetadataFailure({C: video(C)}))
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())

    def test_invalid_options_rejected_before_api_calls(self):
        for options in [{"updateMode": "erase"}, {"updateMode": None}, {"updateMode": []},
                        {"rules": {"maxAgeDays": 0}}, {"rules": {"maxAgeDays": -1}},
                        {"rules": {"maxAgeDays": 1.5}}, {"rules": {"maxAgeDays": True}},
                        {"rules": {"maxAgeDays": "2"}}, {"sources": []}]:
            with self.subTest(options=options):
                before = deepcopy(self.channel)
                self.channel.update(options)
                self.write_config()
                with self.assertRaises(sync.SyncError):
                    self.run_sync(FakeAPI())
                self.channel.clear()
                self.channel.update(before)


class Validation(unittest.TestCase):
    def test_day_cutoff_preserves_utc_time_across_months_and_years(self):
        for instant, days, expected in [
            ("2026-10-24T17:08:00Z", 2, "2026-10-22T17:08:00Z"),
            ("2026-10-24T12:34:56Z", 60, "2026-08-25T12:34:56Z"),
            ("2026-03-01T12:34:56Z", 1, "2026-02-28T12:34:56Z"),
            ("2024-03-01T12:34:56Z", 1, "2024-02-29T12:34:56Z"),
            ("2026-01-01T12:34:56Z", 2, "2025-12-30T12:34:56Z")]:
            with self.subTest(instant=instant):
                self.assertEqual(expected, sync.stamp(sync.age_cutoff(sync.date(instant), days)))
        self.assertIsNone(sync.age_cutoff(NOW, None))

    def test_day_cutoff_clamps_to_minimum_datetime_on_overflow(self):
        for instant, days in [("0001-01-01T12:00:00Z", 1),
                              ("2026-10-24T12:00:00Z", 999999999),
                              ("2026-10-24T12:00:00Z", 10**100)]:
            with self.subTest(instant=instant, days=days):
                self.assertEqual(datetime.min.replace(tzinfo=timezone.utc),
                                 sync.age_cutoff(sync.date(instant), days))

    def test_mixed_source_types(self):
        cases = {"UC" + "x" * 22: "channel", "@nombre": "handle", "PLabcdefghijk": "playlist", A: "video",
                 "playlist:short": "playlist", "video:" + B: "video"}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, sync.source_type(value)[0])
        for value in ["abc", "https://youtube.com/123", " @autor", "", "../file"]:
            with self.assertRaises(sync.SyncError):
                sync.source_type(value)

    def test_duration_and_exclusive_boundaries(self):
        rules = {**sync.DEFAULT_RULES, "maxDurationSeconds": 1000}
        for seconds, expected in [(420, "rejected"), (421, "approved"), (999, "approved"), (1000, "rejected")]:
            self.assertEqual(expected, sync.assess(video(seconds=seconds), rules, "cycle")[0])
        self.assertEqual(93784, sync.duration("P1DT2H3M4S"))
        self.assertIsNone(sync.duration("PT0S"))
        self.assertIsNone(sync.duration("PT3.5S"))
        self.assertIsNone(sync.duration("bad"))

    def test_audio_requires_declared_audio_not_title_or_default_language(self):
        for audio, expected in [("es", "approved"), ("es-AR", "approved"), ("ES-es", "approved"), (None, "pending"), ("en", "rejected")]:
            item = video(audio=audio)
            item["snippet"]["defaultLanguage"] = "es"
            self.assertEqual(expected, sync.assess(item, sync.DEFAULT_RULES, "cycle")[0])
        self.assertEqual("approved", sync.assess(video(audio="en"), {**sync.DEFAULT_RULES, "music": True}, "cycle")[0])

    def test_technical_unavailability(self):
        variants = [None, video(status={"privacyStatus": "private", "uploadStatus": "processed", "embeddable": True}),
                    video(status={"privacyStatus": "public", "uploadStatus": "processed", "embeddable": False}),
                    video(contentDetails={"duration": "PT600S", "regionRestriction": {"blocked": ["AR"]}}),
                    video(contentDetails={"duration": "PT600S", "regionRestriction": {"allowed": []}}),
                    video(contentDetails={"duration": "PT600S", "contentRating": {"ytRating": "ytAgeRestricted"}})]
        for item in variants:
            self.assertEqual("unavailable", sync.assess(item, sync.DEFAULT_RULES, "cycle")[0])

    def test_phase_preserved_after_append_for_many_cycles(self):
        old = {"cycleStartsAt": "2026-01-01T00:00:00Z", "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}, {"youtubeVideoId": B, "durationSeconds": 900}]}
        rows = old["videos"] + [{"youtubeVideoId": C, "durationSeconds": 700}]
        for elapsed in [0, 599, 600, 1499, 1500, 172801, 25000000]:
            instant = sync.date(old["cycleStartsAt"]) + timedelta(seconds=elapsed)
            anchor = sync.anchor_update(old, rows, instant)
            self.assertEqual(elapsed % 1500, int((instant - sync.date(anchor)).total_seconds()) % 2200)

    def test_phase_preserved_when_removing_before_current_program(self):
        old = {"cycleStartsAt": sync.stamp(NOW - timedelta(seconds=700)), "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}, {"youtubeVideoId": B, "durationSeconds": 900}]}
        anchor = sync.anchor_update(old, old["videos"][1:], NOW)
        self.assertEqual(100, int((NOW - sync.date(anchor)).total_seconds()) % 900)

    def test_shortened_current_program_aligns_next_at_old_natural_end(self):
        old = {"cycleStartsAt": sync.stamp(NOW - timedelta(seconds=550)), "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}, {"youtubeVideoId": B, "durationSeconds": 900}]}
        rows = [{"youtubeVideoId": A, "durationSeconds": 500}, old["videos"][1]]
        anchor = sync.anchor_update(old, rows, NOW)
        self.assertEqual(NOW + timedelta(seconds=50), sync.date(anchor) + timedelta(seconds=500))

    def test_utc_dates_require_complete_timestamp_and_preserve_fraction(self):
        for value in ["2026-10-23Z", "2026-10-23T00:00:00+00:00", "invalidZ", None]:
            with self.assertRaises(sync.SyncError):
                sync.date(value)
        old = {"cycleStartsAt": "2026-10-23T00:00:00.500000Z", "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}]}
        rows = old["videos"] + [{"youtubeVideoId": B, "durationSeconds": 600}]
        anchor = sync.anchor_update(old, rows, NOW)
        self.assertEqual(500000, sync.date(anchor).microsecond)

    def test_repeated_legacy_id_preserves_its_current_occurrence(self):
        old = {"cycleStartsAt": sync.stamp(NOW - timedelta(seconds=2900)), "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}, {"youtubeVideoId": B, "durationSeconds": 900},
            {"youtubeVideoId": A, "durationSeconds": 600}]}
        rows = old["videos"] + [{"youtubeVideoId": C, "durationSeconds": 600}]
        instant = NOW + timedelta(seconds=900)
        anchor = sync.anchor_update(old, rows, instant)
        self.assertEqual(1700, int((instant - sync.date(anchor)).total_seconds()) % 2700)

    def test_removing_current_program_aligns_next_at_natural_end(self):
        old = {"cycleStartsAt": sync.stamp(NOW - timedelta(seconds=100)), "videos": [
            {"youtubeVideoId": A, "durationSeconds": 600}, {"youtubeVideoId": B, "durationSeconds": 900}]}
        anchor = sync.anchor_update(old, old["videos"][1:], NOW)
        self.assertEqual(NOW + timedelta(seconds=500), sync.date(anchor))

    def test_lock_prevents_parallel_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sync.lock(root):
                with self.assertRaises(sync.SyncError):
                    with sync.lock(root):
                        pass
            self.assertFalse((root / ".sync.lock").exists())


class Transport(unittest.TestCase):
    def test_batching_cache_and_budget(self):
        urls = []
        def opener(url, **kwargs):
            urls.append(url)
            return BytesIO(b'{"items": []}')
        api = sync.YouTube("SENTINEL_SECRET", 2, opener=opener)
        ids = [f"{value:011d}" for value in range(51)]
        api.videos(ids)
        api.videos(ids)
        self.assertEqual(2, len(urls))
        with self.assertRaises(sync.QuotaError) as context:
            api.get("playlistItems", playlistId="test")
        self.assertNotIn("SENTINEL_SECRET", str(context.exception))

    def test_retries_count_against_budget_without_leaking_secret(self):
        def opener(url, **kwargs):
            raise URLError(url)
        api = sync.YouTube("SENTINEL_SECRET", 2, opener=opener, sleep=lambda _: None)
        with self.assertRaises(sync.QuotaError) as context:
            api.get("channels", id="test")
        self.assertEqual(2, api.used)
        self.assertNotIn("SENTINEL_SECRET", str(context.exception))

    def test_quota_response_is_sanitized(self):
        def opener(url, **kwargs):
            raise HTTPError(url, 403, "SENTINEL_SECRET", {}, BytesIO(b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'))
        api = sync.YouTube("SENTINEL_SECRET", 100, opener=opener)
        with self.assertRaises(sync.QuotaError) as context:
            api.get("channels", id="test")
        self.assertNotIn("SENTINEL_SECRET", str(context.exception))

    def test_malformed_response_does_not_mean_deleted_video(self):
        api = sync.YouTube("SENTINEL_SECRET", 10, opener=lambda *a, **k: BytesIO(b'{}'))
        with self.assertRaises(sync.SyncError):
            api.videos([A])
        self.assertNotIn(A, api.video_cache)

    def test_malformed_items_do_not_remove_existing_videos(self):
        for payload in [{"items": [{}]}, {"items": ["bad"]}, {"items": [{"id": A, "status": []}]}]:
            api = sync.YouTube("SENTINEL_SECRET", 10, opener=lambda *a, **k: BytesIO(json.dumps(payload).encode()))
            with self.assertRaises(sync.SyncError):
                api.videos([A])
            self.assertNotIn(A, api.video_cache)

    def test_incomplete_status_is_pending_not_unavailable(self):
        self.assertEqual("pending", sync.assess(video(status={}), sync.DEFAULT_RULES, "cycle")[0])


if __name__ == "__main__":
    unittest.main()
