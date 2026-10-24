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
    item = {"id": identifier, "snippet": {"title": "Una pieza", "channelId": "UC" + "a" * 22,
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
        self.config = {"version": 1, "quotaBudget": 100, "maxPagesPerSource": 2,
                       "channels": [{"id": "test", "name": "Prueba", "sources": [A]}]}
        self.write_config()

    def write_config(self):
        sync.save(self.root / "sources.json", self.config)

    def run_sync(self, api=None, mode="daily", now=NOW):
        engine = sync.Sync(self.root, api, now)
        result = engine.run(mode)
        return result, engine

    def published(self):
        return sync.load(self.root / "channels/test.json")

    def records(self):
        return sync.load(self.root / "catalog/test.json")["videos"]

    def create_existing(self, ids=(A, B), durations=(600, 900), origin=None):
        origin = origin or NOW - timedelta(seconds=3700)
        sync.save(self.root / "channels.json", {"extra": "manual", "channels": [{"id": "test", "number": 7,
                  "name": "Nombre manual", "playbackMode": "cycle", "file": "channels/test.json", "logo": "manual"}]})
        sync.save(self.root / "channels/test.json", {"cycleStartsAt": sync.stamp(origin), "custom": "manual",
                  "videos": [{"youtubeVideoId": identifier, "durationSeconds": seconds, "note": "manual"}
                             for identifier, seconds in zip(ids, durations)]})
        self.run_sync(mode="bootstrap")

    def test_create_channel_and_compatible_json(self):
        result, _ = self.run_sync(FakeAPI({A: video()}))
        self.assertEqual(0, result)
        self.assertEqual({"cycleStartsAt": sync.stamp(NOW), "videos": [{"youtubeVideoId": A, "durationSeconds": 600}]}, self.published())
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual({"id": "test", "name": "Prueba", "number": 1, "file": "channels/test.json", "playbackMode": "cycle"}, entry)
        self.assertEqual("approved", self.records()[A]["status"])

    def test_new_channel_not_published_without_approved_videos(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        self.assertFalse((self.root / "channels/test.json").exists())
        self.assertEqual([], sync.load(self.root / "channels.json")["channels"])
        self.assertEqual("pending", self.records()[A]["status"])

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

    def test_manual_removal_never_reimported(self):
        self.create_existing()
        playlist = self.published()
        playlist["videos"] = playlist["videos"][1:]
        sync.save(self.root / "channels/test.json", playlist)
        self.run_sync(FakeAPI({A: video()}))
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertTrue(self.records()[A]["manualRemoval"])
        self.assertEqual("rejected", self.records()[A]["status"])

    def test_explicit_rejection_and_notes_survive_maintenance(self):
        self.create_existing()
        catalog = sync.load(self.root / "catalog/test.json")
        catalog["videos"][A].update(manualDecision="rejected", note="No corresponde")
        sync.save(self.root / "catalog/test.json", catalog)
        self.run_sync(FakeAPI({A: video(), B: video(B)}), mode="maintenance")
        self.assertEqual("No corresponde", self.records()[A]["note"])
        self.assertEqual([B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_missing_becomes_unavailable_and_recovers(self):
        self.create_existing()
        self.run_sync(FakeAPI({B: video(B)}), mode="maintenance")
        self.assertEqual("unavailable", self.records()[A]["status"])
        # Una baja automática NO se convierte en baja manual en la ejecución siguiente.
        self.run_sync(FakeAPI({A: video()}), now=NOW + timedelta(days=1))
        self.assertEqual([B, A], [row["youtubeVideoId"] for row in self.published()["videos"]])
        self.assertNotIn("manualRemoval", self.records()[A])

    def test_all_unavailable_holds_last_nonempty_programming(self):
        self.create_existing()
        before = self.published()
        self.run_sync(FakeAPI(), mode="maintenance")
        self.assertEqual(before, self.published())
        self.assertEqual("unavailable", self.records()[A]["status"])
        state = sync.load(self.root / "sync-state.json")
        self.assertEqual("held_empty", state["channels"]["test"]["publication"])

    def test_pending_reevaluated_without_rescanning_source(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        self.run_sync(FakeAPI({A: video()}), now=NOW + timedelta(days=1))
        self.assertEqual("approved", self.records()[A]["status"])
        self.assertTrue((self.root / "channels/test.json").exists())

    def test_manual_approval_cannot_bypass_missing_language(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        catalog = sync.load(self.root / "catalog/test.json")
        catalog["videos"][A].update(status="approved", manualDecision="approved")
        sync.save(self.root / "catalog/test.json", catalog)
        self.run_sync(FakeAPI(), now=NOW + timedelta(days=1))
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_manual_hold_remains_pending(self):
        self.run_sync(FakeAPI({A: video(audio=None)}))
        catalog = sync.load(self.root / "catalog/test.json")
        catalog["videos"][A]["manualDecision"] = "pending"
        sync.save(self.root / "catalog/test.json", catalog)
        self.run_sync(FakeAPI({A: video()}), now=NOW + timedelta(days=1))
        self.assertEqual("pending", self.records()[A]["status"])
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_uploads_stops_on_known_page_and_resolves_handle_once(self):
        self.config["channels"][0]["sources"] = ["@autor"]
        self.write_config()
        self.run_sync(FakeAPI({A: video()}, {("UUuploads", None): page([A])}))
        api = FakeAPI({B: video(B)}, {("UUuploads", None): page([B], "old"), ("UUuploads", "old"): page([A], "history")})
        self.run_sync(api, now=NOW + timedelta(days=1))
        self.assertEqual(["playlistItems", "videos", "playlistItems"], [resource for resource, _ in api.calls])
        self.assertEqual([A, B], [row["youtubeVideoId"] for row in self.published()["videos"]])

    def test_backfill_resumes_without_fetching_old_metadata(self):
        self.config["channels"][0]["sources"] = ["@autor"]
        self.write_config()
        pages = {("UUuploads", None): page([A], "p2"), ("UUuploads", "p2"): page([B], "p3"), ("UUuploads", "p3"): page([C])}
        self.run_sync(FakeAPI({A: video(), B: video(B)}, pages))
        api = FakeAPI({C: video(C)}, pages)
        self.run_sync(api, now=NOW + timedelta(days=1))
        self.assertEqual([None, "p3"], [params.get("pageToken") for resource, params in api.calls if resource == "playlistItems"])
        self.assertEqual([[C]], [params["ids"] for resource, params in api.calls if resource == "videos"])

    def test_large_new_upload_burst_resumes_even_when_initialized(self):
        self.config["channels"][0]["sources"] = ["@autor"]
        self.write_config()
        self.run_sync(FakeAPI({A: video()}, {("UUuploads", None): page([A])}))
        pages = {("UUuploads", None): page([B], "p2"), ("UUuploads", "p2"): page([C], "p3"), ("UUuploads", "p3"): page([D])}
        self.run_sync(FakeAPI({B: video(B), C: video(C)}, pages), now=NOW + timedelta(days=1))
        self.run_sync(FakeAPI({D: video(D)}, pages), now=NOW + timedelta(days=2))
        self.assertIn(D, self.records())

    def test_playlist_tail_insertion_is_found_despite_known_head(self):
        source = "PLabcdefghijklmnop"
        self.config["channels"][0]["sources"] = [source]
        self.write_config()
        self.run_sync(FakeAPI({A: video(), B: video(B)}, {(source, None): page([A], "p2"), (source, "p2"): page([B])}))
        pages = {(source, None): page([A], "p2"), (source, "p2"): page([B], "p3"), (source, "p3"): page([C])}
        self.run_sync(FakeAPI({}, pages), now=NOW + timedelta(days=1))
        self.run_sync(FakeAPI({C: video(C)}, pages), now=NOW + timedelta(days=2))
        self.assertIn(C, self.records())

    def test_failed_page_does_not_advance_cursor(self):
        self.config["channels"][0]["sources"] = ["@autor"]
        self.write_config()
        pages = {("UUuploads", None): page([A], "p2")}
        result, _ = self.run_sync(FakeAPI({A: video()}, pages, fail="p2"))
        self.assertEqual(2, result)
        state = sync.load(self.root / "sync-state.json")["channels"]["test"]["sources"]["@autor"]
        self.assertEqual("p2", state["backfillToken"])
        self.assertEqual([A], state["knownIds"])
        self.assertEqual(A, self.published()["videos"][0]["youtubeVideoId"])

    def test_quota_error_preserves_existing_programming(self):
        self.create_existing()
        before = self.published()
        result, _ = self.run_sync(FakeAPI(fail=sync.QuotaError("Presupuesto agotado")), mode="maintenance")
        self.assertEqual(2, result)
        self.assertEqual(before, self.published())
        self.assertEqual("approved", self.records()[A]["status"])

    def test_failed_metadata_request_does_not_register_video_as_known(self):
        result, _ = self.run_sync(FakeAPI(fail=sync.SyncError("Error de red")))
        self.assertEqual(2, result)
        self.assertEqual({}, self.records())
        state = sync.load(self.root / "sync-state.json")["channels"]["test"]["sources"][A]
        self.assertNotIn("knownIds", state)

    def test_duplicate_across_sources_queried_once(self):
        source = "PLabcdefghijklmnop"
        self.config["channels"][0]["sources"] = [A, source]
        self.write_config()
        api = FakeAPI({A: video()}, {(source, None): page([A])})
        self.run_sync(api)
        self.assertEqual(1, sum(resource == "videos" for resource, _ in api.calls))
        self.assertEqual([A, source], self.records()[A]["sources"])
        self.assertEqual(1, len(self.published()["videos"]))

    def test_inactive_channel_untouched(self):
        self.config["channels"][0]["active"] = False
        self.write_config()
        api = FakeAPI()
        self.run_sync(api)
        self.assertEqual([], api.calls)
        self.assertFalse((self.root / "catalog/test.json").exists())

    def test_duplicate_explicit_number_rejected(self):
        self.config["channels"][0]["number"] = 1
        self.config["channels"].append({"id": "other", "name": "Otro", "number": 1, "sources": []})
        self.write_config()
        with self.assertRaises(sync.SyncError):
            sync.Sync(self.root, None)

    def test_invalid_catalog_fails_before_calls_or_mutation(self):
        self.create_existing()
        path = self.root / "catalog/test.json"
        catalog = sync.load(path)
        catalog["videos"][A]["status"] = "aprobado"
        sync.save(path, catalog)
        api = FakeAPI()
        with self.assertRaises(sync.SyncError):
            self.run_sync(api)
        self.assertEqual([], api.calls)

    def test_legacy_duration_not_overwritten_by_metadata(self):
        self.create_existing()
        self.run_sync(FakeAPI({A: video(seconds=750), B: video(B, audio=None)}), mode="maintenance")
        self.assertEqual(600, self.published()["videos"][0]["durationSeconds"])
        self.assertEqual("approved", self.records()[B]["status"])

    def test_live_creation_compatible(self):
        self.config["channels"][0]["playbackMode"] = "live"
        self.write_config()
        item = video(liveStreamingDetails={"actualStartTime": "2026-10-23T00:00:00Z"})
        self.run_sync(FakeAPI({A: item}))
        self.assertEqual({"videos": [{"youtubeVideoId": A}]}, self.published())


class Validation(unittest.TestCase):
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
