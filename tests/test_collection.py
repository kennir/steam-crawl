import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from steam_crawl.__main__ import main
from steam_crawl.http import Client, NoRedirect, SourceError
from steam_crawl.sources import Collector, app_id, import_steamdb
from steam_crawl.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "steam.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_history_and_latest_export_preserve_sources_and_locales(self):
        self.store.save(570, "store", "us:english", {"name": "Old"}, 1)
        self.store.save(570, "store", "us:english", {"name": "New"}, 2)
        self.store.save(570, "store", "cn:schinese", {"name": "中文"}, 3)
        self.store.save(570, "steamspy", "global", {"is_estimate": True}, 4)
        rows = list(self.store.export())
        self.assertEqual(len(rows), 1)
        sources = rows[0]["sources"]
        self.assertEqual(sources["store"]["us:english"]["data"]["name"], "New")
        self.assertEqual(sources["store"]["cn:schinese"]["data"]["name"], "中文")
        self.assertTrue(sources["steamspy"]["global"]["data"]["is_estimate"])
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0], 4)

    def test_cache_partial_failure_and_estimate_semantics(self):
        client = Mock()
        def fetch(source, **params):
            if source == "store":
                return {"570": {"success": True, "data": {
                    "steam_appid": 570, "type": "game", "name": "Dota 2",
                }}}
            if source == "players":
                raise SourceError("players: unavailable")
            return {"appid": 570, "owners": "100 .. 200", "name": "Dota 2"}
        client.get.side_effect = fetch
        self.store.save(570, "players", "global", {"player_count": 42}, 1)
        collector = Collector(client, self.store)
        self.assertEqual(collector.collect(570, steamspy=True), ["players: unavailable"])
        estimate = self.store.latest(570, "steamspy", "global")["data"]
        self.assertEqual(estimate["owners_estimate_range"], "100 .. 200")
        self.assertTrue(estimate["is_estimate"])
        self.assertNotIn("sales", estimate)
        self.assertEqual(self.store.latest(570, "players", "global")["data"],
                         {"player_count": 42})
        client.get.reset_mock()
        collector.collect(570, steamspy=True)
        client.get.assert_called_once_with("players", appid=570)
        client.get.reset_mock()
        Collector(client, self.store, country="cn").collect(570)
        self.assertEqual([call.args[0] for call in client.get.call_args_list],
                         ["store", "players"])

    def test_malformed_source_does_not_overwrite_last_success(self):
        self.store.save(570, "store", "us:english", {"name": "Old"}, 1)
        client = Mock()
        client.get.side_effect = [
            {"570": {"success": True, "data": {"steam_appid": 571, "type": "game"}}},
            {"response": {"result": 1, "player_count": 0}},
        ]
        errors = Collector(client, self.store).collect(570)
        self.assertEqual(errors, ["store: invalid app data"])
        self.assertEqual(self.store.latest(570, "store", "us:english")["data"],
                         {"name": "Old"})
        self.assertEqual(self.store.latest(570, "players", "global")["data"],
                         {"player_count": 0})

    def test_fresh_player_cache_and_steamspy_opt_in(self):
        self.store.save(570, "players", "global", {"player_count": 5})
        client = Mock()
        client.get.return_value = {"570": {"success": False}}
        self.assertEqual(Collector(client, self.store).collect(570),
                         ["store: app unavailable"])
        client.get.assert_called_once_with("store", appids=570, cc="us", l="english")

    def test_official_catalog_pagination(self):
        client = Mock()
        client.get.side_effect = [
            {"response": {"apps": [{"appid": 10, "name": "Game"}],
                          "have_more_results": True, "last_appid": 10}},
            {"response": {"apps": [], "have_more_results": False, "last_appid": 10}},
        ]
        pages = list(Collector(client, self.store).catalog("test-placeholder", 2))
        self.assertEqual(len(pages), 2)
        self.assertEqual(client.get.call_args_list[1].kwargs["last_appid"], 10)
        self.assertEqual(client.get.call_args.kwargs["include_dlc"], "false")
        self.assertNotIn("key", json.dumps(pages))

    def test_catalog_requires_key_and_rejects_nonadvancing_cursor(self):
        client = Mock()
        collector = Collector(client, self.store)
        with self.assertRaises(ValueError):
            list(collector.catalog(""))
        client.get.assert_not_called()
        client.get.return_value = {"response": {
            "apps": [], "have_more_results": True, "last_appid": 0,
        }}
        with self.assertRaises(SourceError):
            list(collector.catalog("test-placeholder"))

    def test_import_validation_before_any_writes(self):
        path = Path(self.directory.name) / "import.json"
        record = {"appid": 570, "source_url": "https://steamdb.info/app/570/",
                  "collected_at": "2026-10-01T00:00:00Z", "data": {"peak": 100}}
        path.write_text(json.dumps([record, {"appid": 1}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            import_steamdb(path, self.store)
        self.assertEqual(list(self.store.export()), [])
        path.write_text(json.dumps([record]), encoding="utf-8")
        self.assertEqual(import_steamdb(path, self.store), 1)
        self.assertEqual(self.store.latest(570, "steamdb", "global")["data"]["data"],
                         {"peak": 100})
        for invalid in [
            dict(record, source_url="https://example.com/570/"),
            dict(record, collected_at=""),
            dict(record, collected_at="not-a-date"),
            dict(record, collected_at="2026-10-01T00:00:00"),
            dict(record, data={"peak": float("nan")}),
        ]:
            path.write_text(json.dumps([invalid]), encoding="utf-8")
            with self.assertRaises(ValueError):
                import_steamdb(path, self.store)

    def test_cli_cached_crawl_export_and_import_permission(self):
        self.store.save(570, "store", "us:english", {"name": "Dota 2"})
        self.store.save(570, "players", "global", {"player_count": 1})
        output = io.StringIO()
        with patch("steam_crawl.http.Client.get") as get, contextlib.redirect_stdout(output):
            self.assertEqual(main(["--db", str(self.path), "crawl", "570", "570"]), 0)
            get.assert_not_called()
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--db", str(self.path), "export"]), 0)
        self.assertEqual(json.loads(output.getvalue())["appid"], 570)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--db", str(self.path), "import-steamdb", "/unused"])

    def test_cli_continues_after_source_failure(self):
        def fetch(source, **params):
            if source == "store":
                raise SourceError("store: unavailable")
            return {"response": {"result": 1, "player_count": 1}}
        with patch("steam_crawl.http.Client.get", side_effect=fetch), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.path), "crawl", "570", "730"]), 1)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIsNotNone(self.store.latest(730, "players", "global"))


class HttpTests(unittest.TestCase):
    @staticmethod
    def response(body=b'{"ok": true}'):
        response = Mock()
        response.read.return_value = body
        response.headers = {}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def client(self, outcomes, retries=2):
        opener = Mock()
        opener.open.side_effect = outcomes
        now = [0.0]
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay
        client = Client(opener=opener, retries=retries, sleep=sleep, clock=lambda: now[0])
        return client, opener, sleeps

    def test_rate_limit_and_retry_after(self):
        error = HTTPError("https://example.invalid", 429, "", {"Retry-After": "5"}, None)
        client, opener, sleeps = self.client([error, self.response(), self.response()])
        self.assertEqual(client.get("steamspy", appid=570), {"ok": True})
        client.get("steamspy", appid=730)
        self.assertEqual(sleeps, [5, 1])
        self.assertEqual(opener.open.call_count, 3)

    def test_forbidden_shared_web_circuit_and_key_redaction(self):
        error = HTTPError("https://example.invalid/?key=private-value", 403,
                          "private-value", {}, None)
        client, opener, _ = self.client([error, self.response()])
        with self.assertRaises(SourceError) as caught:
            client.get("catalog", key="private-value")
        self.assertNotIn("private-value", str(caught.exception))
        with self.assertRaises(SourceError):
            client.get("players", appid=570)
        self.assertEqual(client.get("store", appids=570), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)

    def test_extended_retry_after_never_retried_early(self):
        error = HTTPError("https://example.invalid", 429, "", {"Retry-After": "3600"}, None)
        client, opener, sleeps = self.client([error])
        with self.assertRaises(SourceError):
            client.get("store", appids=570)
        with self.assertRaises(SourceError):
            client.get("store", appids=730)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_final_503_with_extended_cooldown_pauses_service(self):
        error = HTTPError("https://example.invalid", 503, "",
                          {"Retry-After": "3600"}, None)
        client, opener, sleeps = self.client([error], retries=0)
        with self.assertRaises(SourceError):
            client.get("store", appids=570)
        with self.assertRaises(SourceError):
            client.get("store", appids=730)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_final_503_retains_short_cooldown_for_next_app(self):
        error = HTTPError("https://example.invalid", 503, "", {"Retry-After": "5"}, None)
        client, opener, sleeps = self.client([error, self.response()], retries=0)
        with self.assertRaises(SourceError):
            client.get("store", appids=570)
        self.assertEqual(client.get("store", appids=730), {"ok": True})
        self.assertEqual(sleeps, [5])
        self.assertEqual(opener.open.call_count, 2)

    def test_network_retries_are_bounded_and_json_is_validated(self):
        client, opener, _ = self.client([URLError("secret")] * 3)
        with self.assertRaises(SourceError) as caught:
            client.get("players", appid=570)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(opener.open.call_count, 3)
        for body in [b"<html>challenge</html>", b"[]", b"\xff",
                     b'{"value": NaN}', b'{"value": 1e999}']:
            client, opener, _ = self.client([self.response(body)])
            with self.assertRaises(SourceError):
                client.get("store", appids=570)
            self.assertEqual(opener.open.call_count, 1)

    def test_redirects_are_not_followed_or_retried(self):
        self.assertIsNone(NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://example.invalid"
        ))
        error = HTTPError("https://example.invalid", 302, "",
                          {"Location": "https://example.invalid"}, None)
        client, opener, _ = self.client([error])
        with self.assertRaises(SourceError):
            client.get("catalog", key="test-placeholder")
        self.assertEqual(opener.open.call_count, 1)

    def test_truncated_response_retries(self):
        from http.client import IncompleteRead
        client, opener, _ = self.client([IncompleteRead(b""), self.response()])
        self.assertEqual(client.get("players", appid=570), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)

    def test_short_content_length_body_retries(self):
        response = self.response(b'{"ok":')
        response.headers = {"Content-Length": "12"}
        client, opener, _ = self.client([response, self.response()])
        self.assertEqual(client.get("players", appid=570), {"ok": True})
        self.assertEqual(opener.open.call_count, 2)

    def test_retry_after_http_date(self):
        from email.utils import formatdate
        delay = Client._retry_delay({"Retry-After": formatdate(time.time() + 30,
                                                              usegmt=True)}, 0)
        self.assertGreater(delay, 28)
        self.assertLessEqual(delay, 30)
        self.assertEqual(Client._retry_delay({"Retry-After": "NaN"}, 0), 61)

    def test_invalid_configuration_and_appids(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Client(timeout=timeout)
        for value in (0, -1, True, None, 1.2, "bad", 4294967296):
            with self.assertRaises(ValueError):
                app_id(value)
        self.assertEqual(app_id("570"), 570)


if __name__ == "__main__":
    unittest.main()
