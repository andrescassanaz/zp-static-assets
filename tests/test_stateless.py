"""Flujo stateless, límites de API, selección y publicación segura; sin red."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_sync as fixtures
from test_sync import A, B, C, NOW, FakeAPI, page, video
import sync


class Stateless(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    run_sync = fixtures.Integration.run_sync
    published = fixtures.Integration.published
    create_existing = fixtures.Integration.create_existing

    def test_three_days_fetches_at_most_150_candidates_and_three_pages(self):
        self.create_existing()
        before = (self.root / "channels/test.json").read_bytes()
        self.config["channels"][0].update(sources=["@autor"], updateMode="replace", rules={"maxAgeDays": 3})
        self.write_config()
        ids = [f"{n:011d}" for n in range(200)]
        pages = {("UUuploads", token): page(ids[start:start + 50], f"p{start + 50}")
                 for start, token in [(0, None), (50, "p50"), (100, "p100"), (150, "p150")]}
        api = FakeAPI({identifier: video(identifier) for identifier in ids}, pages)
        self.assertEqual(2, self.run_sync(api)[0])
        listing = [params for resource, params in api.calls if resource == "playlistItems"]
        self.assertEqual([50, 50, 50], [params["maxResults"] for params in listing])
        self.assertEqual([None, "p50", "p100"], [params.get("pageToken") for params in listing])
        self.assertEqual(before, (self.root / "channels/test.json").read_bytes())
        self.assertEqual("held_incomplete", self.engine.report["channels"]["test"]["publication"])
        self.assertEqual(150, self.engine.report["channels"]["test"]["candidatesScanned"])

    def test_exact_limit_with_no_more_pages_is_complete(self):
        self.config["channels"][0].update(sources=["@autor"], limits={"videosPerDay": 1},
                                          rules={"maxAgeDays": 3}, updateMode="replace")
        self.write_config()
        items = {i: video(i) for i in (A, B, C)}
        for item in items.values():
            item["snippet"]["publishedAt"] = sync.stamp(NOW)
        api = FakeAPI(items, {("UUuploads", None): page([A, B, C])})
        self.assertEqual(0, self.run_sync(api)[0])
        self.assertEqual(3, next(p["maxResults"] for r, p in api.calls if r == "playlistItems"))
        self.assertEqual(3, len(self.published()["videos"]))

    def test_factor_inherited_and_overridden_and_filtered_ids_still_count(self):
        self.config["limits"] = {"videosPerDay": 2}
        self.config["channels"][0].update(sources=["@autor"], rules={"maxAgeDays": 3})
        self.write_config()
        api = FakeAPI({}, {("UUuploads", None): page([A, B, C], "p2")})
        self.run_sync(api)
        self.assertEqual([6, 3], [p["maxResults"] for r, p in api.calls if r == "playlistItems"])
        self.config["channels"][0]["limits"] = {"videosPerDay": 1}
        self.write_config()
        api = FakeAPI({}, {("UUuploads", None): page([A, B, C], "p2")})
        self.assertEqual(2, self.run_sync(api)[0])
        self.assertEqual([3], [p["maxResults"] for r, p in api.calls if r == "playlistItems"])

    def test_uploads_stop_after_old_page_without_fetching_metadata_or_history(self):
        self.config["channels"][0].update(sources=["@autor"], rules={"maxAgeDays": 3}, updateMode="replace")
        self.write_config()
        old = page([B], "p3")
        old["items"][0]["contentDetails"]["videoPublishedAt"] = "2020-01-01T00:00:00Z"
        recent = video()
        recent["snippet"]["publishedAt"] = sync.stamp(NOW)
        api = FakeAPI({A: recent}, {("UUuploads", None): page([A], "p2"), ("UUuploads", "p2"): old})
        self.assertEqual(0, self.run_sync(api)[0])
        self.assertEqual([None, "p2"], [p.get("pageToken") for r, p in api.calls if r == "playlistItems"])
        self.assertEqual([[A]], [p["ids"] for r, p in api.calls if r == "videos"])
        self.assertEqual([A], [v["youtubeVideoId"] for v in self.published()["videos"]])

    def test_mixed_or_unknown_dates_do_not_stop_upload_scan(self):
        self.config["channels"][0].update(sources=["@autor"], rules={"maxAgeDays": 3})
        self.write_config()
        for dates in [("2020-01-01T00:00:00Z", None), ("2020-01-01T00:00:00Z", sync.stamp(NOW))]:
            first = page([A, B], "p2")
            for entry, date in zip(first["items"], dates):
                entry["contentDetails"]["videoPublishedAt"] = date
            api = FakeAPI({B: video(B), C: video(C)}, {("UUuploads", None): first, ("UUuploads", "p2"): page([C])})
            self.run_sync(api)
            self.assertEqual([None, "p2"], [p.get("pageToken") for r, p in api.calls if r == "playlistItems"])

    def test_playlist_does_not_use_upload_date_stop_or_daily_candidate_limit(self):
        source = "PLeditorialplaylist"
        self.config["channels"][0].update(sources=[source], rules={"maxAgeDays": 1},
                                          limits={"videosPerDay": 1}, updateMode="replace")
        self.write_config()
        old = page([A], "p2")
        old["items"][0]["contentDetails"]["videoPublishedAt"] = "2020-01-01T00:00:00Z"
        recent = video(B)
        recent["snippet"]["publishedAt"] = sync.stamp(NOW)
        api = FakeAPI({B: recent}, {(source, None): old, (source, "p2"): page([B])})
        self.assertEqual(0, self.run_sync(api)[0])
        self.assertEqual([50, 50], [p["maxResults"] for r, p in api.calls if r == "playlistItems"])
        self.assertEqual([B], [v["youtubeVideoId"] for v in self.published()["videos"]])

    def test_page_cap_holds_replace_but_append_keeps_verified_additions(self):
        source = "PLeditorialplaylist"
        for mode in ("replace", "append"):
            with self.subTest(mode=mode):
                self.create_existing((B,), (900,))
                before = self.published()
                self.config["channels"][0].update(sources=[source], updateMode=mode, limits={"maxPagesPerSource": 1})
                self.write_config()
                api = FakeAPI({A: video()}, {(source, None): page([A], "p2")})
                self.assertEqual(2, self.run_sync(api)[0])
                if mode == "replace":
                    self.assertEqual(before, self.published())
                else:
                    self.assertEqual([B, A], [v["youtubeVideoId"] for v in self.published()["videos"]])
                self.assertEqual(1, len([r for r, _ in api.calls if r == "playlistItems"]))

    def test_interrupted_source_restarts_at_first_page_and_ignores_old_state(self):
        (self.root / "sync-state.json").write_text("broken old state", encoding="utf-8")
        self.config["channels"][0].update(sources=["@autor"], limits={"maxPagesPerSource": 1})
        self.write_config()
        for _ in range(2):
            api = FakeAPI({A: video()}, {("UUuploads", None): page([A], "p2")})
            self.assertEqual(2, self.run_sync(api)[0])
            self.assertEqual([None], [p.get("pageToken") for r, p in api.calls if r == "playlistItems"])
        self.assertEqual("broken old state", (self.root / "sync-state.json").read_text(encoding="utf-8"))
        self.assertFalse((self.root / ".sync.lock").exists())

    def test_reports_stay_in_logs_and_only_changed_catalog_files_are_written(self):
        result, engine = self.run_sync(FakeAPI({A: video()}))
        output = StringIO()
        with redirect_stdout(output):
            engine.emit_report("update", result)
        report = json.loads(output.getvalue())
        self.assertEqual(1, report["channels"]["test"]["quotaUnits"])
        self.assertEqual({"sources.json", "channels.json", "channels/test.json"},
                         {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()})

    def test_daily_weekly_and_manual_selection_do_not_infer_from_names(self):
        self.config["channels"].append({"id": "weekly", "name": "Noticias", "sources": [B], "updateFrequency": "weekly"})
        self.config["channels"].append({"id": "paused", "name": "Pausado", "sources": [C], "active": False})
        self.write_config()
        for frequency, expected in [("daily", {"test"}), ("weekly", {"weekly"}), (None, {"test", "weekly"})]:
            engine = sync.Sync(self.root, FakeAPI())
            self.assertEqual(expected, {c["id"] for c in engine.selection(frequency=frequency)})
        engine = sync.Sync(self.root, FakeAPI({B: video(B)}))
        self.assertEqual(0, engine.run("update", selected="weekly"))
        self.assertEqual([[B]], [p["ids"] for r, p in engine.api.calls if r == "videos"])
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_configuration_drives_rename_renumber_and_removal(self):
        self.create_existing()
        self.config["channels"][0].update(name="Nombre nuevo", number=42, sources=[])
        self.write_config()
        self.run_sync()
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual(("Nombre nuevo", 42), (entry["name"], entry["number"]))
        self.assertEqual("manual", entry["logo"])
        self.config["channels"] = []
        self.write_config()
        engine = sync.Sync(self.root, FakeAPI())
        engine.run("update", selected=set())
        self.assertEqual([], engine.api.calls)
        self.assertEqual([], sync.load(self.root / "channels.json")["channels"])
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_live_source_change_replaces_previous_id_without_manual_output_edits(self):
        self.config["channels"][0]["playbackMode"] = "live"
        self.write_config()
        live = {"actualStartTime": sync.stamp(NOW - timedelta(hours=1))}
        self.run_sync(FakeAPI({A: video(A, liveStreamingDetails=live)}))
        self.config["channels"][0]["sources"] = [B]
        self.write_config()
        self.run_sync(FakeAPI({B: video(B, liveStreamingDetails=live)}))
        self.assertEqual([B], [v["youtubeVideoId"] for v in self.published()["videos"]])

    def test_new_channel_can_use_number_freed_by_removed_configuration(self):
        self.create_existing()
        self.config["channels"] = [{"id": "replacement", "name": "Nuevo", "number": 7, "sources": [C]}]
        self.write_config()
        engine = sync.Sync(self.root, FakeAPI({C: video(C)}))
        self.assertEqual(0, engine.run("update"))
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual(("replacement", 7), (entry["id"], entry["number"]))
        self.assertFalse((self.root / "channels/test.json").exists())

    def test_explicit_number_changes_cannot_collide_with_existing_implicit_numbers(self):
        self.create_existing()
        self.config["channels"].append({"id": "other", "name": "Otro", "number": 7, "sources": [C]})
        self.write_config()
        with self.assertRaises(sync.SyncError):
            sync.Sync(self.root, FakeAPI())

    def test_cli_invalid_configuration_and_git_comparison_do_not_query_youtube(self):
        with patch.object(sync.YouTube, "get", side_effect=AssertionError("No API")), \
                redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            self.assertEqual(2, sync.main(["--root", str(self.root), "--changed-since", "HEAD"]))
            self.config["channels"][0]["rules"] = {"maxAgeDays": 0}
            self.write_config()
            self.assertEqual(2, sync.main(["--root", str(self.root)]))

    def test_real_git_diff_updates_only_changed_channel_and_restarts_without_state(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True,
                                  encoding="utf-8").stdout.strip()
        git("init", "--quiet")
        git("config", "user.email", "tests@example.invalid")
        git("config", "user.name", "Tests")
        git("add", "sources.json")
        git("commit", "--quiet", "-m", "Initial configuration")
        revision = git("rev-parse", "HEAD")
        self.assertEqual(self.config, sync.previous_config(self.root, revision))
        self.config["channels"].append({"id": "new", "name": "Nuevo", "sources": [B]})
        self.write_config()
        api = FakeAPI({B: video(B)})
        with patch.object(sync, "YouTube", return_value=api), \
                patch.dict(os.environ, {"YOUTUBE_API_KEY": "test"}), redirect_stdout(StringIO()):
            self.assertEqual(0, sync.main(["--root", str(self.root), "--changed-since", revision]))
        self.assertEqual([[B]], [p["ids"] for r, p in api.calls if r == "videos"])
        self.assertFalse((self.root / "channels/test.json").exists())
        self.assertFalse((self.root / "sync-state.json").exists())

    def test_invalid_limits_and_frequency_fail_before_api_access(self):
        original = deepcopy(self.config["channels"][0])
        for options in [{"limits": None}, {"limits": {"unknown": 2}}, {"limits": {"videosPerDay": 0}},
                        {"limits": {"maxPagesPerSource": True}}, {"limits": {"maxPagesPerSource": 1.5}},
                        {"updateFrequency": "monthly"}, {"updateFrequency": []}]:
            self.config["channels"][0] = {**original, **options}
            self.write_config()
            api = FakeAPI()
            with self.subTest(options=options), self.assertRaises(sync.SyncError):
                sync.Sync(self.root, api)
            self.assertEqual([], api.calls)

    def test_copy_backup_is_offline_and_preserves_playback_and_metadata(self):
        self.run_sync(FakeAPI({A: video()}))
        app = self.root / "android"
        manifest = app / "app/src/main/AndroidManifest.xml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("<manifest/>", encoding="utf-8")
        engine = sync.Sync(self.root, None)
        with redirect_stdout(StringIO()):
            sync.copy_backup(engine, app)
        target = app / "app/src/main/assets/catalog"
        for name in ("channels.json", "channels/test.json"):
            self.assertEqual((self.root / name).read_bytes(), (target / name).read_bytes())
        self.assertFalse((target / "sources.json").exists())

    def test_cli_skips_api_key_and_api_access_when_no_configurations_changed(self):
        before = deepcopy(self.config)
        with patch.object(sync, "previous_config", return_value=before), patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}), \
                patch.object(sync.YouTube, "get", side_effect=AssertionError("No API")), redirect_stdout(StringIO()):
            self.assertEqual(0, sync.main(["--root", str(self.root), "--changed-since", "a" * 40]))

    def test_cli_weekly_update_and_maintenance_share_one_api_budget(self):
        self.config["channels"][0]["updateFrequency"] = "weekly"
        self.write_config()
        api = FakeAPI({A: video()})
        with patch.object(sync, "YouTube", return_value=api) as constructor, \
                patch.dict(os.environ, {"YOUTUBE_API_KEY": "test"}), redirect_stdout(StringIO()):
            self.assertEqual(0, sync.main(["--root", str(self.root), "--frequency", "weekly", "--maintenance"]))
        constructor.assert_called_once_with("test", 100)
        self.assertEqual(2, api.used)

    def test_maintenance_failure_after_a_batch_keeps_verified_removals_only_in_append(self):
        ids = tuple(f"{n:011d}" for n in range(51))
        class FailSecondBatch(FakeAPI):
            def videos(self, requested):
                if self.used:
                    raise sync.SyncError("Fallo temporal")
                return super().videos(requested)
        for mode in ("append", "replace"):
            with self.subTest(mode=mode):
                self.create_existing(ids, (600,) * 51)
                before = self.published()
                self.config["channels"][0]["updateMode"] = mode
                self.write_config()
                self.assertEqual(2, self.run_sync(FailSecondBatch(), mode="maintenance")[0])
                if mode == "replace":
                    self.assertEqual(before, self.published())
                else:
                    self.assertEqual([ids[-1]], [v["youtubeVideoId"] for v in self.published()["videos"]])


class ConfigDiff(unittest.TestCase):
    def test_diff_selects_changed_and_new_channels_and_global_changes(self):
        before = {"version": 1, "channels": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}
        after = deepcopy(before)
        after["channels"][1]["name"] = "Nuevo nombre"
        after["channels"].append({"id": "c", "name": "C"})
        self.assertEqual({"b", "c"}, sync.changed_channels(after, before))
        self.assertEqual(set(), sync.changed_channels(before, before))
        after["defaults"] = {"music": True}
        self.assertEqual({"a", "b", "c"}, sync.changed_channels(after, before))

    def test_invalid_or_missing_git_revision_fails_safely(self):
        for revision in ("HEAD", "--help", "a" * 40 + ":secret"):
            with self.assertRaises(sync.SyncError):
                sync.previous_config(Path.cwd(), revision)
        completed = subprocess.CompletedProcess([], 128, "", "untrusted stderr")
        with patch.object(sync.subprocess, "run", return_value=completed), self.assertRaisesRegex(sync.SyncError, "No se pudo leer"):
            sync.previous_config(Path.cwd(), "a" * 40)
        self.assertEqual({"channels": []}, sync.previous_config(Path.cwd(), "0" * 40))
