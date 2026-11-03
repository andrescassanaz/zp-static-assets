"""Aislamiento de canales, reintentos y resultados de CI; sin red ni secretos."""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
import unittest
from unittest.mock import patch

import test_sync as fixtures
from test_sync import A, B, C, FakeAPI, video
import sync
from sync_reporting import actions_summary, annotation, report_outcome


class MissingChannelAPI(FakeAPI):
    def get(self, resource, **params):
        if resource == "channels" and params.get("forHandle") == "@missing":
            self.used += 1
            return {"items": []}
        return super().get(resource, **params)


class Resilience(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    write_config = fixtures.Integration.write_config
    create_existing = fixtures.Integration.create_existing

    def cli(self, api, *args):
        self.write_config()
        output, errors = StringIO(), StringIO()
        summary = self.root / "summary.md"
        with patch.object(sync, "YouTube", return_value=api), \
                patch.dict(os.environ, {"YOUTUBE_API_KEY": "SENTINEL_SECRET", "GITHUB_STEP_SUMMARY": str(summary)}), \
                redirect_stdout(output), redirect_stderr(errors):
            code = sync.main(["--root", str(self.root), "--github-actions", *args])
        self.assertNotIn("SENTINEL_SECRET", output.getvalue() + errors.getvalue())
        return code, output.getvalue(), summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_failed_replace_keeps_old_bytes_and_next_channel_is_published(self):
        self.create_existing()
        old = (self.root / "channels/test.json").read_bytes()
        self.config["channels"][0].update(sources=["@missing"], updateMode="replace")
        self.config["channels"].append({"id": "healthy", "name": "Sano", "sources": [C]})
        code, output, summary = self.cli(MissingChannelAPI({C: video(C)}))
        self.assertEqual(0, code)
        self.assertEqual(old, (self.root / "channels/test.json").read_bytes())
        self.assertEqual(C, sync.load(self.root / "channels/healthy.json")["videos"][0]["youtubeVideoId"])
        self.assertIn("1 de 2 canales sin errores", summary)
        self.assertIn("@missing", summary)
        self.assertIn("Lista anterior conservada", summary)
        self.assertIn("::warning::", output)
        self.assertNotIn("::error::", output)
        report = json.loads(output.splitlines()[0])
        self.assertEqual("@missing", report["channels"]["test"]["sourceErrors"][0]["source"])

    def test_append_publishes_other_sources_even_if_no_channel_is_fully_complete(self):
        self.config["channels"][0]["sources"] = ["@missing", A]
        code, _, summary = self.cli(MissingChannelAPI({A: video()}))
        self.assertEqual(0, code)
        self.assertIn("Sincronización parcial", summary)
        self.assertTrue((self.root / "channels/test.json").exists())

    def test_all_queries_failing_are_not_hidden_by_a_channel_without_sources(self):
        self.config["channels"][0]["sources"] = ["@missing"]
        self.config["channels"].append({"id": "legacy", "name": "Histórico", "sources": []})
        code, output, summary = self.cli(MissingChannelAPI())
        self.assertEqual(1, code)
        self.assertIn("::error::", output)
        self.assertIn("sin avance verificable", summary)

    def test_quota_failure_at_start_remains_a_failure(self):
        self.config["channels"].append({"id": "later", "name": "Luego", "sources": [B]})
        code, _, summary = self.cli(FakeAPI(fail=sync.QuotaError("Cuota agotada")))
        self.assertEqual(1, code)
        self.assertIn("Pendiente", summary)

    def test_quota_failure_after_success_keeps_the_completed_channel(self):
        class LaterQuota(FakeAPI):
            def videos(self, ids):
                if B in ids:
                    raise sync.QuotaError("Cuota agotada")
                return super().videos(ids)
        self.config["channels"].append({"id": "later", "name": "Luego", "sources": [B]})
        code, _, summary = self.cli(LaterQuota({A: video()}))
        self.assertEqual(0, code)
        self.assertIn("1 de 2 canales sin errores", summary)
        self.assertTrue((self.root / "channels/test.json").exists())

    def test_successful_maintenance_does_not_erase_update_errors(self):
        self.config["channels"][0].update(sources=["@missing"], updateMode="replace", updateFrequency="weekly")
        self.config["channels"].append({"id": "healthy", "name": "Sano", "sources": [C], "updateFrequency": "weekly"})
        code, output, summary = self.cli(MissingChannelAPI({C: video(C)}), "--frequency", "weekly", "--maintenance")
        self.assertEqual(0, code)
        self.assertIn("@missing", summary)
        self.assertIn("### maintenance", summary)
        self.assertIn("1 de 2 canales sin errores", summary)
        reports = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual(["partial", "success"], [report["result"] for report in reports])

    def test_failed_refresh_is_not_emptied_by_following_maintenance(self):
        self.create_existing()
        old = (self.root / "channels/test.json").read_bytes()
        self.config["channels"][0].update(sources=["@missing"], updateMode="replace", rules={"maxAgeDays": 3})
        self.config["channels"].append({"id": "healthy", "name": "Sano", "sources": [C]})
        # El mantenimiento eliminaría A/B si consultara ese canal tras el fallo.
        api = MissingChannelAPI({C: video(C)})
        code, output, _ = self.cli(api, "--maintenance")
        self.assertEqual(0, code)
        self.assertEqual(old, (self.root / "channels/test.json").read_bytes())
        reports = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual({"healthy"}, set(reports[1]["channels"]))
        self.assertFalse(any(A in params.get("ids", []) or B in params.get("ids", []) for _, params in api.calls))

    def test_cli_without_actions_preserves_nonzero_partial_status(self):
        self.config["channels"][0]["sources"] = ["@missing", A]
        self.write_config()
        with patch.object(sync, "YouTube", return_value=MissingChannelAPI({A: video()})), \
                patch.dict(os.environ, {"YOUTUBE_API_KEY": "test"}), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(2, sync.main(["--root", str(self.root)]))

    def test_missing_key_and_unexpected_errors_remain_fatal(self):
        with patch.dict(os.environ, {"YOUTUBE_API_KEY": ""}), redirect_stderr(StringIO()):
            self.assertEqual(1, sync.main(["--root", str(self.root)]))
        with patch.object(sync.Sync, "run", side_effect=RuntimeError("SENTINEL_SECRET")):
            code, _, _ = self.cli(FakeAPI())
            self.assertEqual(1, code)

    def test_success_and_empty_selection_do_not_warn(self):
        code, output, summary = self.cli(FakeAPI({A: video()}))
        self.assertEqual(0, code)
        self.assertNotIn("::warning::", output)
        self.assertIn("Sincronización completa", summary)
        code, output, summary = self.cli(FakeAPI(), "--frequency", "weekly")
        self.assertEqual(0, code)
        self.assertIn("0 de 0 canales", summary)


class Responses(unittest.TestCase):
    def test_incomplete_response_retries_same_query_and_recovers(self):
        urls, sleeps = [], []
        def opener(url, **kwargs):
            urls.append(url)
            return BytesIO(json.dumps({} if len(urls) == 1 else {"items": [video()]}).encode())
        api = sync.YouTube("SENTINEL_SECRET", 10, opener=opener, sleep=sleeps.append)
        logs = StringIO()
        with redirect_stderr(logs):
            self.assertEqual(A, api.videos([A])[A]["id"])
        self.assertEqual(2, api.used)
        self.assertEqual(urls[0], urls[1])
        self.assertEqual(1, len(sleeps))
        self.assertNotIn("SENTINEL_SECRET", logs.getvalue())

    def test_repeated_invalid_response_is_bounded_and_never_cached_as_deleted(self):
        api = sync.YouTube("SENTINEL_SECRET", 10, opener=lambda *a, **k: BytesIO(b'{}'), sleep=lambda _: None)
        with redirect_stderr(StringIO()), self.assertRaisesRegex(sync.SyncError, "cuatro intentos"):
            api.videos([A])
        self.assertEqual(4, api.used)
        self.assertNotIn(A, api.video_cache)

    def test_malformed_response_retries_respect_quota_budget(self):
        api = sync.YouTube("test", 2, opener=lambda *a, **k: BytesIO(b'{}'), sleep=lambda _: None)
        with redirect_stderr(StringIO()), self.assertRaises(sync.QuotaError):
            api.videos([A])
        self.assertEqual(2, api.used)
        self.assertNotIn(A, api.video_cache)

    def test_only_confirmed_empty_channel_response_means_missing_channel(self):
        api = sync.YouTube("test", 10, opener=lambda *a, **k: BytesIO(b'{"pageInfo":{"totalResults":0}}'))
        self.assertEqual([], api.get("channels", forHandle="@missing")["items"])
        self.assertEqual(1, api.used)
        # El mismo payload no demuestra que los videos solicitados fueron borrados.
        api.sleep = lambda _: None
        with redirect_stderr(StringIO()), self.assertRaises(sync.SyncError):
            api.videos([A])
        self.assertNotIn(A, api.video_cache)


class Reporting(unittest.TestCase):
    def test_43_of_44_is_partial_with_specific_source_and_preservation(self):
        channels = {f"channel-{i}": {"result": "success", "sourcesScanned": 1, "publication": "published"} for i in range(43)}
        channels["futbol"] = {"result": "partial", "publication": "held_incomplete", "errors": ["Canal inexistente"],
                              "sourceErrors": [{"sourceIndex": 3, "source": "@missing", "error": "Canal inexistente"}]}
        reports = [{"channels": channels, "mode": "update", "errors": ["futbol: Canal inexistente"]}]
        self.assertEqual("partial", report_outcome(reports))
        summary = actions_summary(reports, "partial")
        self.assertIn("43 de 44 canales sin errores; 1 con incidencias", summary)
        self.assertIn("fuente 3 (@missing)", summary)

    def test_annotation_escapes_newlines_and_percent(self):
        output = StringIO()
        with redirect_stdout(output):
            annotation("warning", "100%\r\n::error::injected")
        self.assertEqual("::warning::100%25%0D%0A::error::injected\n", output.getvalue())
