#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""Hardware that answers whoever asks, and has nowhere to be pointed.

A PurpleAir sensor runs a small web server. So does a Davis AirLink, and so
does most of what is sold as "with a local API". None of it can be told where
to upload, so none of it can push, and a listener that only ever waits never
sees it.

What is here is the asking, and nothing else. Once an answer is in hand it
goes through `Driver.start`'s `deliver`, which is the same door an upload
comes through: the same parse, the same raw names, the same dialect, the same
live table. That is the point of doing it this way rather than writing a
second, parallel path -- a protocol becomes pollable by saying
`fetched = True`, and everything it already had keeps working.

## One thread per source

A sensor that has been unplugged then holds up only itself, and the thread is
cheap next to the sixty seconds it spends asleep. The same arrangement the
export runners use, for the same reason.

## Why not the hour's grid

`schedule.py` in the core puts anything with its own cadence on :00, :05, :10,
so that two installations agree and a chart's timestamps line up with the
records beside them. That is about *reports*. A sensor reading is a
measurement, it is stored with the time it was taken, and nothing downstream
compares one station's polling instants with another's.

Worse, the grid would make every source on an installation ask at the same
second. Four sensors, one burst, four timeouts arriving together. So: a plain
interval from when the thread started, which spreads them by however long the
listener took to come up.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: How long to wait for a sensor on the local network. Long enough for one
#: that is busy, short enough that a thread asking every 60 seconds does not
#: still be waiting when the next turn comes round.
TIMEOUT = 10.0

#: The floor under an interval. Anything faster is asking a sensor that
#: samples every few seconds to answer more often than it measures.
FASTEST = 10


@dataclass
class Source:
    """One thing to ask, and what it needs to be asked with."""

    #: What somebody typed in: a host, a host and port, or a whole URL.
    address: str
    #: Seconds between one answer and the next question.
    interval: int = 60
    #: Whatever the protocol asked for on top -- an API key, an account id.
    settings: dict[str, Any] = field(default_factory=dict)
    timeout: float = TIMEOUT
    #: Where a credential goes, for a service that takes one in a header.
    #: Kept off the URL on purpose: the URL is printed. A log line says which
    #: address could not be reached and the page of raw uploads says where a
    #: reading came from, and neither should carry somebody's API key.
    headers: dict[str, str] = field(default_factory=dict)
    #: The same, for the services that will not take one in a header.
    #: Ambient Weather wants its two keys in the query string and offers no
    #: other way, so they are added at the moment of asking and never stored
    #: on the address.
    query: dict[str, str] = field(default_factory=dict)

    def url(self, path: str = "") -> str:
        """The address as something `urlopen` will take.

        A person types `192.168.1.50`, and once in a while
        `http://192.168.1.50:8080`. Both have to work: making somebody write
        the scheme is a support question that arrives once per user.
        """
        base = self.address.strip().rstrip("/")
        if not base:
            return ""
        if "://" not in base:
            base = f"http://{base}"
        return base + (path or "")


def ask(source: Source, url: str, body: bytes | None = None
        ) -> tuple[bytes, dict[str, str]]:
    """One request. Returns (body, headers).

    Raises whatever urllib raises. The caller decides what a failure means,
    because for a sensor on a home network it usually means somebody unplugged
    it and it will be back.
    """
    if source.query:
        # Appended here rather than kept on the address, so that what a log
        # line and the raw-upload page print is the address and not the key.
        joiner = "&" if "?" in url else "?"
        url = url + joiner + urllib.parse.urlencode(source.query)
    request = urllib.request.Request(  # noqa: S310 - the address is the operator's
        url, data=body, headers=dict(source.headers))
    with urllib.request.urlopen(request, timeout=source.timeout) as answer:  # noqa: S310
        return answer.read(), dict(answer.headers)


class Poller:
    """Asks one protocol's sources, on their own threads, until stopped."""

    def __init__(self, protocol: Any, sources: list[Source]) -> None:
        self.protocol = protocol
        self.sources = sources
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        #: What each source last did, for the status page. Not a history: how
        #: the last attempt went is the question, and every attempt of every
        #: sensor would be a table nobody empties.
        self.last: dict[str, str] = {}

    def start(self, deliver) -> None:
        """One thread per source. Returns at once."""
        if self._threads:
            raise RuntimeError("this poller is already running")
        self._stop.clear()
        for source in self.sources:
            thread = threading.Thread(
                target=self._loop, args=(source, deliver),
                name=f"poll-{self.protocol.name}-{source.address}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        """Ask them to finish, and wait a little.

        Not forever: a thread blocked in `urlopen` against a sensor that
        accepted the connection and then went quiet is not joinable inside
        the timeout, and holding up the whole shutdown for it would turn one
        unplugged sensor into a service that will not stop. They are daemon
        threads, so the process may leave without them.
        """
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def _loop(self, source: Source, deliver) -> None:
        gap = max(FASTEST, int(source.interval or 60))
        while not self._stop.is_set():
            try:
                self._once(source, deliver)
            except Exception as exc:
                # Every failure mode of a sensor on a home network ends here:
                # unplugged, renumbered by DHCP, firmware updating, answering
                # HTML because somebody typed the wrong port. None of them is
                # a reason to stop asking, and all of them are fixed by the
                # operator rather than by us.
                self.last[source.address] = f"{type(exc).__name__}: {exc}"
                if _first(_said, (self.protocol.name, source.address)):
                    log.warning(
                        "could not read %s at %s: %s. Still asking every %ds; "
                        "this is said once until it answers.",
                        self.protocol.name, source.address, exc, gap)
            # Interruptible, so a stop does not wait out the interval. A
            # `time.sleep(60)` here is a service that takes a minute to shut
            # down for every sensor it has.
            self._stop.wait(gap)

    def _once(self, source: Source, deliver) -> None:
        """Ask, and hand the answer over."""
        fetch = getattr(self.protocol, "fetch", None)
        assembled = fetch(source, ask) if fetch is not None else None
        if assembled is None:
            # The ordinary case: one request to the address, and the protocol
            # says what to append. A protocol needing several -- an Ecowitt
            # gateway, Home Assistant -- overrides `fetch` and hands back one
            # body, so that nothing downstream has to know.
            path = getattr(self.protocol, "fetch_path", "") or ""
            body, _headers = ask(source, source.url(path))
        else:
            body, _headers = assembled

        if not body:
            self.last[source.address] = "answered with nothing"
            return
        stored = deliver(body)
        self.last[source.address] = (
            f"{stored} stored at {time.strftime('%H:%M:%S')}")
        _said.discard((self.protocol.name, source.address))


#: Which (protocol, address) pairs have been complained about. A sensor that
#: is unplugged for a week is one log line, not ten thousand.
_said: set[tuple[str, str]] = set()


def _first(seen: set, key: tuple) -> bool:
    if key in seen:
        return False
    seen.add(key)
    return True


def sources_from(settings: dict[str, Any],
                 protocol: Any = None) -> list[Source]:
    """The sources a driver was configured with.

    `addresses` is a list because one household has two PurpleAir sensors as
    readily as one, and because the alternative -- a second driver instance
    per sensor -- would give them the same name and make them
    indistinguishable to `stations.by_identity`.

    Anything else in the settings travels with each source: an API key or an
    account id belongs to the protocol, and a protocol that wants one asked
    for it in its own `options()`.

    `protocol` is asked where its credential goes. A service that takes one
    in a header says so with `headers_for`, one that will not with
    `query_for`, and both are kept off the address for the reason given on
    `Source.headers`.
    """
    raw = settings.get("addresses") or []
    if isinstance(raw, str):
        raw = [one.strip() for one in raw.replace(",", " ").split() if one.strip()]
    interval = settings.get("interval") or 60
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        interval = 60
    extra = {name: value for name, value in settings.items()
             if name not in ("addresses", "interval")}

    def asked(what: str) -> dict[str, str]:
        found = getattr(protocol, what, None)
        if found is None:
            return {}
        try:
            return {str(k): str(v) for k, v in (found(extra) or {}).items()}
        except Exception:
            # A protocol that cannot work out its own credential from the
            # settings has been misconfigured, and the address is still worth
            # asking: some of them answer without one and say what is wrong.
            log.exception("%s could not work out its %s",
                          getattr(protocol, "name", "?"), what)
            return {}

    headers, query = asked("headers_for"), asked("query_for")
    return [Source(address=str(one), interval=interval, settings=dict(extra),
                   headers=dict(headers), query=dict(query))
            for one in raw if str(one).strip()]


def json_body(body: bytes) -> Any:
    """What a protocol's `fetch` usually wants out of an answer.

    Here rather than in each protocol so that "the sensor answered with an
    HTML error page" is one message instead of six different ones.
    """
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"expected JSON, got {body[:60]!r}") from exc
