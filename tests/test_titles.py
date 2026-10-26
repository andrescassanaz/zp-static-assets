from copy import deepcopy
import unittest
import test_sync as fixtures
from test_sync import FakeAPI, video, A, B, NOW
import sync
from video_titles import normalize_title


class Titles(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    run_sync = fixtures.Integration.run_sync
    published = fixtures.Integration.published
    create_existing = fixtures.Integration.create_existing

    def test_music_presentation_keeps_real_playback_and_never_calls_youtube(self):
        self.create_existing()
        self.config["channels"][0]["rules"] = {"music": True}
        self.write_config()
        before = self.published()
        api = FakeAPI(fail=AssertionError("No debe consultar YouTube"))
        self.assertEqual(0, self.run_sync(api, mode="presentation")[0])
        entry = sync.load(self.root / "channels.json")["channels"][0]
        self.assertEqual("continuous", entry["guideMode"])
        self.assertEqual("cycle", entry["playbackMode"])
        self.assertEqual(before, self.published())
        self.assertEqual([], api.calls)

    def test_new_music_channel_publishes_continuous_metadata_on_daily_sync(self):
        self.config["channels"][0]["rules"] = {"music": True}
        self.write_config()
        self.run_sync(FakeAPI({A: video()}))
        self.assertEqual("continuous", sync.load(self.root / "channels.json")["channels"][0]["guideMode"])
        self.assertIn("durationSeconds", self.published()["videos"][0])

    def test_cleanup_is_idempotent_and_preserves_accents_names_and_acronyms(self):
        examples = {
            "🥬Compre una *Nueva* Amarok V6 y esta es mi Opinion 🥬": "Compre una Nueva Amarok V6 y esta es mi Opinion",
            "VUELO DE JAPÓN A ESTADOS UNIDOS 🇯🇵": "Vuelo de Japón a Estados Unidos",
            "NBA | Michael Jackson &amp; AC/DC en 4K": "NBA · Michael Jackson & AC/DC en 4K",
            "  ¿CÓMO USAR C++ Y LA API?  ": "¿Cómo Usar C++ y la API?",
        }
        for raw, expected in examples.items():
            self.assertEqual(expected, normalize_title(raw))
            self.assertEqual(expected, normalize_title(expected))

    def test_local_cleanup_changes_only_titles_and_preserves_manual_metadata(self):
        self.create_existing((A, B, A), (600, 900, 600))
        before = self.published()
        for row in before["videos"]:
            row["title"] = "🔥 LA NBA EN VIVO 🔥"
        sync.save(self.root / "channels/test.json", before)
        self.run_sync(mode="presentation")
        after = self.published()
        self.assertTrue(all(row.pop("title") == "La NBA en Vivo" for row in after["videos"]))
        for row in before["videos"]:
            row.pop("title")
        self.assertEqual(before, after)

    def test_backfill_preserves_order_duplicates_durations_origin_and_manual_fields(self):
        self.create_existing((A, B, A), (600, 900, 600))
        before = self.published()
        api = FakeAPI({A: video(A), B: video(B)})
        self.assertEqual(0, self.run_sync(api, mode="titles")[0])
        expected = deepcopy(before)
        for row in expected["videos"]:
            row["title"] = "Una pieza"
            row["youtubeChannelName"] = "Ramiro Diz"
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
