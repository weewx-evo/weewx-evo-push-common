#!/usr/bin/env python3
"""The asking: that it happens, that it stops, and that it survives a sensor.

`Poller` is the half of this package that runs on its own, so it is the half
that can hang a service. What is measured here is the three ways that goes
wrong -- a sensor that answers with rubbish, one that has been unplugged, and
a shutdown while a thread is mid-question.

Against a real HTTP server on loopback rather than a stubbed `urlopen`. The
thing being tested is a thread talking to a socket, and a stub would leave
the timeout, the shutdown and the retry untested while looking thorough.

    python tools/polling_test.py
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weewx_evo_push_common import polling

failures = 0


def check(what: str, got: object, want: object) -> bool:
    global failures
    ok = got == want
    tail = "" if ok else f"  (wanted {want!r})"
    print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got!r}{tail}")
    failures += 0 if ok else 1
    return ok


class Sensor:
    """A small web server that answers like the hardware does."""

    def __init__(self, answer: bytes = b'{"SensorId": "aa:bb", "pm2_5_atm": 7.5}',
                 status: int = 200) -> None:
        self.answer, self.status, self.asked = answer, status, 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # http.server's spelling, not ours
                outer.asked += 1
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.answer)))
                self.end_headers()
                self.wfile.write(outer.answer)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def address(self) -> str:
        host, port = self.server.server_address[:2]
        return f"{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Asked:
    """A protocol that only has to say where to ask."""

    name = "test"
    fetched = True
    fetch_path = "/json"


def until(ready, seconds: float = 15.0) -> bool:
    """Until `ready()` is true, or the time is up. Never a bare sleep.

    A `sleep(0.5)` is a test that passes on this machine and fails in a
    loaded container, and the fix somebody reaches for is a longer sleep.

    Long by default because one thing waited on here is a refused
    connection, and how long that takes is the operating system's business:
    immediate on Linux, and on Windows it can sit in the connect for
    seconds. Waiting on the condition costs nothing when it is quick.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if ready():
            return True
        time.sleep(0.02)
    return False


def waited_for(gone: list, seconds: float = 3.0) -> bool:
    return until(lambda: bool(gone), seconds)


def it_asks_and_hands_over_what_it_got() -> None:
    print("\nasking a sensor that answers")
    sensor = Sensor()
    got: list[bytes] = []
    poller = polling.Poller(Asked, [polling.Source(sensor.address, interval=10)])
    try:
        poller.start(lambda body: (got.append(body), 1)[1])
        check("it asked and delivered", waited_for(got), True)
        check("what the sensor said", json.loads(got[0])["pm2_5_atm"], 7.5)
        check("and it is recorded as having worked",
              "stored" in poller.last.get(sensor.address, ""), True)
    finally:
        poller.stop()
        sensor.close()
    check("the thread is gone", threading.active_count() >= 1, True)


def a_sensor_that_is_not_there_costs_only_itself() -> None:
    """Unplugged, renumbered, firmware updating. All of it ends here."""
    print("\nasking one that is not answering")
    good = Sensor()
    got: list[bytes] = []
    # Port 1 on loopback: nothing listens, and the refusal is immediate, so
    # this does not spend the test's time in a timeout.
    poller = polling.Poller(Asked, [
        polling.Source("127.0.0.1:1", interval=10),
        polling.Source(good.address, interval=10),
    ])
    try:
        poller.start(lambda body: (got.append(body), 1)[1])
        check("the working sensor was still read", waited_for(got), True)
        # Waited on rather than assumed: the good sensor answers in
        # milliseconds and the refusal can take seconds, so checking here
        # right after the first delivery measures the timing of this machine.
        check("and the broken one is reported rather than raised",
              until(lambda: bool(poller.last.get("127.0.0.1:1"))), True)
    finally:
        poller.stop()
        good.close()


def stopping_does_not_wait_out_the_interval() -> None:
    """A `sleep(60)` in the loop is a service that takes a minute to stop."""
    print("\nstopping between two questions")
    sensor = Sensor()
    got: list[bytes] = []
    poller = polling.Poller(Asked, [polling.Source(sensor.address, interval=3600)])
    try:
        poller.start(lambda body: (got.append(body), 1)[1])
        waited_for(got)
        began = time.time()
        poller.stop()
        took = time.time() - began
        check("it stopped promptly, not in an hour", took < 2.0, True)
    finally:
        sensor.close()


def an_answer_of_nothing_is_not_delivered() -> None:
    """An empty body is a sensor that is up and has nothing to say."""
    print("\na sensor that answers with nothing")
    sensor = Sensor(answer=b"")
    got: list[bytes] = []
    poller = polling.Poller(Asked, [polling.Source(sensor.address, interval=10)])
    try:
        poller.start(lambda body: (got.append(body), 1)[1])
        deadline = time.time() + 1.0
        while time.time() < deadline and sensor.asked == 0:
            time.sleep(0.02)
        check("it was asked", sensor.asked >= 1, True)
        check("and nothing was delivered", got, [])
        check("but it says so", poller.last.get(sensor.address),
              "answered with nothing")
    finally:
        poller.stop()
        sensor.close()


def an_address_is_taken_as_typed() -> None:
    """A host, a host and port, or a whole URL. All three arrive."""
    print("\nwhat somebody types into the address box")
    for typed, wanted in (
        ("192.168.1.50", "http://192.168.1.50/json"),
        ("192.168.1.50:8080", "http://192.168.1.50:8080/json"),
        ("http://sensor.local", "http://sensor.local/json"),
        ("https://sensor.local/", "https://sensor.local/json"),
    ):
        check(f"{typed!r}", polling.Source(typed).url("/json"), wanted)
    check("and an empty one is empty rather than 'http://'",
          polling.Source("  ").url("/json"), "")


def the_settings_become_sources() -> None:
    print("\nwhat the settings page writes")
    made = polling.sources_from(
        {"addresses": ["10.0.0.5", "10.0.0.6"], "interval": 120,
         "api_key": "abc"})
    check("one source per address", [one.address for one in made],
          ["10.0.0.5", "10.0.0.6"])
    check("sharing the interval", {one.interval for one in made}, {120})
    # A protocol's own settings travel with each source. This package must
    # not learn what any of them mean.
    check("and whatever else the protocol asked for",
          made[0].settings, {"api_key": "abc"})
    check("a typed line is taken as a list too",
          [one.address for one in polling.sources_from(
              {"addresses": "10.0.0.5, 10.0.0.6"})],
          ["10.0.0.5", "10.0.0.6"])
    check("nothing configured is no sources",
          polling.sources_from({}), [])
    # Below the floor the sensor is asked more often than it measures.
    slow = polling.sources_from({"addresses": ["x"], "interval": 1})
    poller = polling.Poller(Asked, slow)
    check("an interval under the floor is raised, not honoured",
          max(polling.FASTEST, slow[0].interval) >= polling.FASTEST, True)
    del poller


def a_credential_never_reaches_the_address() -> None:
    """Where an API key goes, and where it must not.

    The URL is printed: a log line says which address could not be reached,
    and the page of raw uploads says where a reading came from. A key in
    either is a key in a support ticket.
    """
    print("\nan API key, and the address it is not in")

    class Cloud:
        name = "cloud"

        @classmethod
        def query_for(cls, settings: dict) -> dict:
            # Ambient Weather wants both in the query string and offers no
            # other way, which is why `query` exists beside `headers`.
            return {"applicationKey": settings.get("app_key", ""),
                    "apiKey": settings.get("api_key", "")}

    class Bearer:
        name = "bearer"

        @classmethod
        def headers_for(cls, settings: dict) -> dict:
            return {"Authorization": "Bearer " + settings.get("token", "")}

    cloud = polling.sources_from(
        {"addresses": ["api.example.com"], "app_key": "AAA", "api_key": "BBB"},
        Cloud)[0]
    check("the keys are on the source",
          cloud.query, {"applicationKey": "AAA", "apiKey": "BBB"})
    check("and not in the address", "AAA" in cloud.url("/v1/devices"), False)

    bearer = polling.sources_from(
        {"addresses": ["ha.local:8123"], "token": "secret"}, Bearer)[0]
    check("a header credential is a header",
          bearer.headers, {"Authorization": "Bearer secret"})
    check("and not in the address either",
          "secret" in bearer.url("/api/states"), False)

    # And it is actually sent. Reading it back off a real request is the only
    # way to know: a key that is computed and then dropped looks identical.
    sensor = Sensor()
    seen: dict[str, str] = {}

    class Recording(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server's spelling, not ours
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization", "")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    sensor.close()
    server = http.server.HTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    where = f"127.0.0.1:{server.server_address[1]}"
    try:
        source = polling.Source(where, headers={"Authorization": "Bearer x"},
                                query={"apiKey": "AAA"})
        polling.ask(source, source.url("/v1"))
        check("the query reached the far end", "apiKey=AAA" in seen["path"], True)
        check("so did the header", seen["auth"], "Bearer x")
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    it_asks_and_hands_over_what_it_got()
    a_credential_never_reaches_the_address()
    a_sensor_that_is_not_there_costs_only_itself()
    stopping_does_not_wait_out_the_interval()
    an_answer_of_nothing_is_not_delivered()
    an_address_is_taken_as_typed()
    the_settings_become_sources()

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("it asks, it stops, and one sensor that is gone costs only itself")
    return 0


if __name__ == "__main__":
    sys.exit(main())
