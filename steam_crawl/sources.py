import json
import time
from datetime import datetime

from .http import SourceError


def app_id(value):
    if isinstance(value, bool):
        raise ValueError("appid must be a positive 32-bit integer")
    if not isinstance(value, (str, int)):
        raise ValueError("appid must be a positive 32-bit integer")
    try:
        number = int(value)
    except ValueError:
        raise ValueError("appid must be a positive 32-bit integer") from None
    if not 0 < number <= 4294967295:
        raise ValueError("appid must be a positive 32-bit integer")
    return number


class Collector:
    TTLS = {"store": 86400, "players": 300, "steamspy": 86400}

    def __init__(self, client, store, country="us", language="english"):
        self.client = client
        self.store = store
        self.country = country
        self.language = language

    def collect(self, appid, steamspy=False):
        errors = []
        sources = ["store", "players"] + (["steamspy"] if steamspy else [])
        for source in sources:
            scope = f"{self.country}:{self.language}" if source == "store" else "global"
            cached = self.store.latest(appid, source, scope)
            if cached is not None and 0 <= time.time() - cached["fetched_at"] < self.TTLS[source]:
                continue
            try:
                data = self._fetch(source, appid)
                self.store.save(appid, source, scope, data)
            except SourceError as error:
                errors.append(str(error))
        return errors

    def _fetch(self, source, appid):
        if source == "store":
            response = self.client.get(
                source, appids=appid, cc=self.country, l=self.language
            )
            entry = response.get(str(appid))
            if not isinstance(entry, dict) or entry.get("success") is not True:
                raise SourceError("store: app unavailable")
            data = entry.get("data")
            if not isinstance(data, dict) or data.get("steam_appid") != appid:
                raise SourceError("store: invalid app data")
            if data.get("type") != "game":
                raise SourceError("store: app is not a game")
            return data
        if source == "players":
            data = self.client.get(source, appid=appid).get("response")
            if not isinstance(data, dict) or data.get("result") != 1:
                raise SourceError("players: app unavailable")
            count = data.get("player_count")
            if type(count) is not int or count < 0:
                raise SourceError("players: invalid player count")
            return {"player_count": count}
        data = self.client.get("steamspy", request="appdetails", appid=appid)
        try:
            matches = app_id(data.get("appid")) == appid
        except ValueError:
            matches = False
        if not matches or not isinstance(data.get("owners"), str):
            raise SourceError("steamspy: invalid app data")
        return {
            "owners_estimate_range": data["owners"],
            "is_estimate": True,
            "methodology_url": "https://steamspy.com/about",
            "raw": data,
        }

    def catalog(self, key, max_pages=1, last_appid=0):
        if not key:
            raise ValueError("STEAM_API_KEY is required for catalog enumeration")
        for _ in range(max_pages):
            response = self.client.get(
                "catalog", key=key, include_games="true", include_dlc="false",
                include_software="false", include_videos="false",
                include_hardware="false", max_results=10000, last_appid=last_appid,
            ).get("response")
            if not isinstance(response, dict) or not isinstance(response.get("apps"), list):
                raise SourceError("catalog: invalid response")
            apps = response["apps"]
            if any(not isinstance(app, dict) or type(app.get("appid")) is not int
                   or not 0 < app["appid"] <= 4294967295
                   or not isinstance(app.get("name"), str) for app in apps):
                raise SourceError("catalog: invalid app list")
            more = response.get("have_more_results", False)
            if not isinstance(more, bool):
                raise SourceError("catalog: invalid pagination flag")
            cursor = response.get("last_appid", last_appid)
            if more and (type(cursor) is not int or cursor <= last_appid):
                raise SourceError("catalog: pagination did not advance")
            yield {"apps": apps, "have_more_results": more, "last_appid": cursor}
            if not more:
                return
            last_appid = cursor


def import_steamdb(path, store):
    with open(path, encoding="utf-8") as stream:
        records = json.load(stream)
    if not isinstance(records, list):
        raise ValueError("SteamDB import must be a JSON array")
    validated = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
            raise ValueError("each SteamDB record requires appid and a data object")
        appid = app_id(record.get("appid"))
        url = f"https://steamdb.info/app/{appid}/"
        if record.get("source_url") != url:
            raise ValueError("SteamDB source_url must match the app page")
        collected_at = record.get("collected_at")
        if not isinstance(collected_at, str) or not collected_at.strip():
            raise ValueError("SteamDB record requires collected_at provenance")
        date = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("SteamDB collected_at requires an ISO 8601 timezone")
        payload = {
            "source_url": url, "collected_at": collected_at,
            "acquisition": "authorized_local_import", "data": record["data"],
        }
        json.dumps(payload, allow_nan=False)
        validated.append((appid, payload))
    for appid, payload in validated:
        store.save(appid, "steamdb", "global", payload)
    return len(validated)
