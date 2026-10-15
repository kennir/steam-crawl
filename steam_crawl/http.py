import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SourceError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    URLS = {
        "catalog": "https://api.steampowered.com/IStoreService/GetAppList/v1/",
        "players": "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
        "store": "https://store.steampowered.com/api/appdetails",
        "steamspy": "https://steamspy.com/api.php",
    }
    GROUPS = {"catalog": "web", "players": "web", "store": "store", "steamspy": "steamspy"}
    INTERVALS = {"web": 2.0, "store": 2.0, "steamspy": 1.0}

    def __init__(self, timeout=30.0, retries=2, opener=None,
                 sleep=time.sleep, clock=time.monotonic):
        if not math.isfinite(timeout) or timeout <= 0 or not 0 <= retries <= 5:
            raise ValueError("invalid timeout or retry count")
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.clock = clock
        self.next_request = {}
        self.blocked = set()

    def _pace(self, group):
        delay = self.next_request.get(group, 0) - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_request[group] = self.clock() + self.INTERVALS[group]

    @staticmethod
    def _retry_delay(headers, attempt):
        value = headers.get("Retry-After")
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    delay = (date - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 2 ** attempt
            if not math.isfinite(delay):
                return 61
            return max(0, delay)
        return 2 ** attempt

    def get(self, source, **params):
        group = self.GROUPS[source]
        if group in self.blocked:
            raise SourceError(f"{source}: access paused for this run")
        request = Request(
            self.URLS[source] + "?" + urlencode(params),
            headers={"User-Agent": "steam-crawl/1.0 (public game research)",
                     "Accept": "application/json"},
        )
        for attempt in range(self.retries + 1):
            self._pace(group)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read(10 * 1024 * 1024 + 1)
                    if len(body) > 10 * 1024 * 1024:
                        raise SourceError(f"{source}: response too large")
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        try:
                            expected = int(length)
                        except (TypeError, ValueError):
                            raise SourceError(f"{source}: invalid response length") from None
                        if len(body) < expected:
                            raise IncompleteRead(body, expected - len(body))
                try:
                    data = json.loads(body)
                    json.dumps(data, allow_nan=False)
                except (ValueError, UnicodeError):
                    raise SourceError(f"{source}: invalid JSON response") from None
                if not isinstance(data, dict):
                    raise SourceError(f"{source}: expected JSON object")
                return data
            except HTTPError as error:
                status = error.code
                delay = self._retry_delay(error.headers, attempt)
                error.close()
                if status in (401, 403) or status == 429 and (
                    attempt == self.retries or delay > 60
                ):
                    self.blocked.add(group)
                    raise SourceError(f"{source}: HTTP {status}; access paused") from None
                if status not in (429, 500, 502, 503, 504):
                    raise SourceError(f"{source}: HTTP {status}") from None
                self.next_request[group] = max(
                    self.next_request[group], self.clock() + delay
                )
                if delay > 60:
                    self.blocked.add(group)
                    raise SourceError(f"{source}: server requested extended cooldown") from None
                if attempt == self.retries:
                    raise SourceError(f"{source}: HTTP {status}") from None
                self.sleep(delay)
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt == self.retries:
                    raise SourceError(f"{source}: network request failed") from None
                self.sleep(2 ** attempt)
