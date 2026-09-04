"""Every protocol that pushes readings at us, as drivers.

Six protocols, one place. They were three separate plugins here -- ecowitt,
wunderground, and nothing at all for the other four -- and each new one meant
another copy of the same parsing, the same inference, the same argument about
what a field name means.

Upstream this is `weewx-ultimate-push`, and the split it makes is the reason
the whole thing fits under one roof:

    A **protocol** is an exchange. A path, an answer, a way of naming the
    station. Ecowitt and Weather Underground are different protocols.

    A **dialect** is a catalog. The same exchange with different field names,
    or the same names in different units. Fine Offset firmwares speak Weather
    Underground in Fahrenheit and inches, or in Celsius and millimetres, on
    the same endpoint with the same credentials. One protocol, two dialects.

`protocols/` and `catalogs/` are taken from there unchanged, because they
import nothing -- not WeeWX, not us -- and a fix should be able to travel in
either direction. This file is the whole of the adaptation: it turns a
`Protocol` class into something the listener can call.

## What the listener already does for them

Their `driver.py` is 1610 lines, and most of it is work this program has
done elsewhere for a while:

    the socket, threads, shutdown      ingest/listener.py
    which sender may write             listener access policy
    what reaches each archive column   Place membership, role, placement.toml
    unit registration                  units.contribute
    telling somebody what is wrong     the settings page

So none of that comes over. What is left is a per-protocol parse and a
per-dialect mapper, which is this file.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from weewx_evo.db.live import Packet
from weewx_evo.ingest import drivers
from weewx_evo.ingest.drivers import Response

from . import mapping, polling, transport
from . import protocols as protocol_defs

log = logging.getLogger(__name__)

#: WeeWX's unit-system constants, which are also ours: the envelope carries
#: `usUnits` because the archive column does.
US = 1
METRIC = 16
METRICWX = 17

#: Values that name the station rather than describe the weather. Taken out
#: before the readings are stored -- see `_without_secrets`.
SECRETS = frozenset(transport.SECRETS)

#: How many read-side mappers to keep before starting again. Each one walks
#: its whole catalog to build an inferrer, so they are worth keeping; but the
#: key includes the placements, and an entry per combination ever seen is a
#: leak in a process that runs for months.
MAX_READERS = 32


class _Request:
    """What a protocol asks about the request it is deciding on.

    Three attributes, because that is all any of the six look at. Made here
    rather than passing our `meta` dictionary through, so that the protocols
    stay code that can be lifted back out unchanged.
    """

    __slots__ = ("body", "method", "path")

    def __init__(self, path: str = "/", body: bytes = b"",
                 method: str = "POST") -> None:
        self.path = path
        self.body = body
        self.method = method

    @property
    def text(self) -> str:
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8", "replace")
        return str(self.body or "")


class PushDriver:
    """One protocol, as a driver.

    A mapper per dialect, not per protocol. Inference learned from `tempf`
    and `soiltemp2f` has no business being applied to `outtemp` and
    `absbaro`, and Weather Underground carries both on one endpoint.
    """

    #: Set on each subclass by `driver_class`. Here so that `__init__` can
    #: take the protocol from the class rather than as an argument, which is
    #: what lets the subclass keep this constructor -- see the note there.
    protocol_class: Any = None

    def __init__(self, protocol: Any = None,
                 field_map_extensions: dict[str, str] | None = None,
                 infer_unknown: str = mapping.SERIES,
                 max_behind: int | None = None, max_ahead: int | None = None,
                 stations: dict[str, dict] | None = None,
                 addresses: Any = None, interval: Any = None,
                 **ignored: Any) -> None:
        # `field_map_extensions` keeps the name the ecowitt driver used, and
        # the running instance has two decisions written under it -- which
        # column `tf_ch1` and `soil_ec_temp1` go to. Renaming the option would
        # mean two sensors quietly landing in one column, which is the one
        # failure the whole contested-field mechanism exists to prevent.
        protocol = protocol or type(self).protocol_class
        self.protocol = protocol
        self.name = protocol.name
        #: One mapper per (dialect, mode, placements), built on first use.
        #: Used only by the listener's proposal collector. Archive processes
        #: execute the declarative description returned by `dialect_spec`.
        self._readers: dict[tuple, mapping.Mapper] = {}
        self.field_map = dict(field_map_extensions or {})
        self.infer_unknown = infer_unknown
        # Read here rather than relying on a schema to have done it. These
        # arrive from three places now -- the core settings, a station, and
        # `drivers.<name>.max_behind` left in a file from when this was a
        # per-protocol option -- and only the first two are parsed on the way.
        # Handed the string "4h", `int()` raises during startup, the
        # exception is swallowed, and the driver runs on its default: nothing
        # in any log reads as a wrong setting and the figure silently does
        # nothing.
        self.max_behind = _duration(max_behind, transport.MAX_BEHIND)
        self.max_ahead = _duration(max_ahead, transport.MAX_AHEAD)
        #: What each console says about itself, keyed by the identity it
        #: uploads with. The core hands these over from stations.toml.
        #:
        #: Keyed by identity, not by name, and that is the whole of a bug
        #: this had: the lookup below is made with what the protocol read off
        #: the upload -- a PASSKEY -- while the dictionary arrived keyed by
        #: the name somebody typed. It therefore never matched, and every
        #: placement the settings page wrote went nowhere. The installation
        #: kept working because the same decisions were also sitting in the
        #: driver's own `field_map_extensions`, where they had been put by
        #: hand before the page existed.
        self.stations = _by_identity(stations)
        #: The sources this driver goes and asks, for a protocol that is
        #: `fetched`. Empty for every protocol that is pushed at, which is
        #: most of them, and then `start()` does nothing at all.
        #:
        #: `ignored` carries whatever else the protocol asked for in its own
        #: `options()` -- an API key, an account id -- and it travels with
        #: each source: this class must not learn what any of them mean.
        self.sources = polling.sources_from(
            {"addresses": addresses, "interval": interval, **ignored},
            protocol)
        self._poller: polling.Poller | None = None
        #: What could not be placed, for the settings page to show.
        self.unplaced: dict[str, Any] = {}
        #: What the device wants to read back. An attribute, because that is
        #: what `drivers.response_of` reads -- a method here was taken as the
        #: answer itself and indexed, which is a 500 on every upload.
        #:
        #: Many of these treat an upload as failed until they have seen the
        #: right answer: they retry, and eventually give up. Ecowitt wants
        #: JSON, Weather Underground wants the word `success`.
        self.response: Response = ((protocol.answer or "success").encode(),
                                   protocol.content_type)

    # -- what the listener calls -----------------------------------------

    def claims(self, body: bytes, meta: dict) -> float:
        """How sure this protocol is that the upload is its own.

        Their scale is 0 to 5 and ours is 0 to 1, so it is divided. The
        ordering is what matters and that survives.
        """
        request = _Request(path=str(meta.get("path") or "/"), body=body)
        try:
            raw = self._raw(request)
        except Exception:
            return 0.0
        try:
            return self.protocol.claims(request, raw) / 5.0
        except Exception:
            log.debug("%s could not decide about an upload", self.name,
                      exc_info=True)
            return 0.0

    def packets(self, body: bytes, meta: dict) -> list[Packet]:
        """The upload as readings, under the names the console used.

        Nothing is named, placed or dropped here beyond the secrets. The
        catalog is persisted separately as JSON by `dialect_spec`; which
        fields an archive wants is still decided later, so a decision made
        next week reaches the readings that arrived today.

        The clock is the exception and stays here: a stamp is too old because
        a *clock* is wrong, and a clock belongs to a box at the moment it
        uploaded. There is no answering that a week later.
        """
        request = _Request(path=str(meta.get("path") or "/"), body=body)
        raw = self._raw(request)
        if not raw:
            return []

        readings = self.protocol.readings(request, raw)
        if not readings:
            return []
        dialect = self.protocol.dialect(raw)
        identity = self._station_of(raw)
        mine = self.stations.get(identity.casefold()) or {}
        stamp = transport.device_time(
            readings, now=meta.get("received"),
            max_behind=_clock(mine, "max_behind", self.max_behind),
            max_ahead=_clock(mine, "max_ahead", self.max_ahead))
        if stamp is None:
            stamp = meta.get("received") or time.time()
        data = _without_secrets(readings)
        spec = self._dialect_spec(dialect, data)
        return [Packet(dateTime=int(stamp), usUnits=int(dialect.units),
                       data=data,
                       identity=identity,
                       # The dialect's own name, not prefixed with the
                       # protocol: `driver` already says which protocol, and
                       # Weather Underground's metric dialect is itself
                       # called `wunderground/metric` -- prefixed, that is a
                       # name with two slashes in it that no placement file
                       # would ever be written against.
                       dialect=dialect.name,
                       mapping=spec.as_dict(),
                       kind="loop", received=meta.get("received"),
                       volatile=frozenset(dialect.metadata or transport.METADATA))]

    def dialect_spec(self, readings: dict, dialect: str) -> drivers.DialectSpec:
        """The catalog as inert data for the core archiver.

        The listener calls this immediately after `packets`. The raw readings
        remain raw; only the rules for interpreting them are serialized. A
        split archiver therefore needs neither this module nor any installed
        third-party driver.
        """
        found = self.protocol.dialect(readings)
        if dialect and found.name != dialect:
            raise ValueError(f"stored dialect {dialect!r} became {found.name!r}")
        return self._dialect_spec(found, readings)

    def _dialect_spec(self, found: Any, readings: dict) -> drivers.DialectSpec:
        """Serialize one already-selected dialect."""

        fields = dict(found.fields)
        fields.update(self.field_map)
        contested = set(found.contested)
        contested.difference_update(self.field_map)

        # Some firmware identifies the meaning of an otherwise contested
        # field in the upload itself. Persist that answer; the metadata it was
        # inferred from is still in `readings`, but the archive must not need
        # protocol code to ask the question again.
        for raw, target in (self.protocol.settled_contested(readings) or {}).items():
            if raw in self.field_map:
                continue
            fields[raw] = target
            contested.discard(raw)

        return drivers.DialectSpec(
            fields=fields,
            contested=frozenset(contested),
            scale=dict(found.scale),
            metadata=frozenset(found.metadata),
            absent=transport.ABSENT + tuple(found.absent),
            groups=dict(found.groups),
            usUnits=int(found.units),
        )

    def place(self, readings: dict, dialect: str, decisions: dict[str, str],
              infer: str = mapping.OFF) -> drivers.Placed | None:
        """Stored readings as an observation record, for one console.

        The dialect is worked out from the readings again rather than looked
        up by the stored name. The name does not fix the units: Weather
        Underground's metric dialect answers to one name and carries either
        `METRIC` or `METRICWX`, with different scale factors, depending on a
        class attribute. Asking the protocol is the same question the upload
        was answered with, so the two cannot drift.
        """
        found = self.protocol.dialect(readings)
        if dialect and found.name != dialect:
            # The catalog moved under the data. Said rather than corrected:
            # which of the two is right is not answerable from here, and a
            # record built from the wrong one is plausible and wrong.
            log.warning(
                "these readings were stored as %r and read back as %r. A rebuild of "
                "that span will use %r; if that is not what the console speaks, the "
                "record it produces is not the record it produced the first time.",
                dialect, found.name, found.name)
        # Without `dateutc` the mapper does not ask about the clock, and it
        # has no business asking: the question was answered when the upload
        # arrived and the answer is on the packet. Asked again it compares the
        # console's stamp against the `now` this passes, which is not a time
        # -- so a perfectly good console produced a warning about being fifty
        # years out, on every packet, for ever. Measured on the instance.
        #
        # It cannot change a placement. `dateutc` is in `dialect.metadata`, so
        # `numbers()` routes it to the text half either way and no field of
        # the record can come from it.
        if "dateutc" in readings:
            readings = {name: value for name, value in readings.items()
                        if name != "dateutc"}
        record, guesses = self._reader(found, decisions, infer).to_packet(readings, now=0)
        record.pop("dateTime", None)
        # The guesses go back only to whoever asked for them. `Archiver`
        # never reads them, and that is the one thing keeping this a pure
        # function: the list is shorter the second time, because the mapper
        # remembers having already said so.
        return drivers.Placed(record=record, usUnits=int(found.units),
                              proposals=tuple(guesses))

    def unit_groups(self) -> dict[str, str]:
        """What this protocol's fields measure, for anything that formats one.

        The catalog's, and only the catalog's. What a *guessed* column
        measures used to be collected from the mappers here -- and it never
        reached anybody, because `install_driver_groups` asks this once at
        startup, before a packet has arrived. Those groups are written down
        beside the placement that produced them now (`placement.toml`,
        `[groups]`), which is the only shape in which a second process can
        see them.
        """
        return dict(self.protocol.groups)

    def redact(self, raw: str) -> str:
        """The upload with whatever names the station replaced.

        The raw body is kept beside the packet for an hour so that a question
        about a new sensor can be answered from it, and that copy is meant to
        be pasteable into an issue. A PASSKEY is what somebody would need to
        forge this station's readings and a Weather Underground PASSWORD is a
        password; everything else in these uploads is weather, which is the
        point of sending one to somebody who can help.

        Without this the listener keeps the body as it arrived. That is how
        it was before the six protocols moved in together, and losing it was
        the only thing this move broke that a test caught.
        """
        return transport.redact(raw)

    def status(self) -> dict[str, Any]:
        said = {
            "protocol": self.name,
            "hardware": self.protocol.hardware,
            "dialects": sorted({key[0] for key in self._readers}),
            "unplaced": sorted(self.unplaced),
        }
        if self._poller is not None:
            # How the last attempt at each source went. Not a history: a
            # table of every attempt of every sensor is one nobody empties,
            # and the question is whether it is answering now.
            said["polling"] = dict(self._poller.last)
        return said

    # -- going and asking ------------------------------------------------

    def start(self, deliver) -> None:
        """Ask this protocol's sources, if it is one that has to be asked.

        Nothing at all for a protocol that is pushed at, which is most of
        them: no thread, no timer, no lookup. That case has to stay exactly
        as cheap as it was.
        """
        if not getattr(self.protocol, "fetched", False) or not self.sources:
            return
        self._poller = polling.Poller(self.protocol, self.sources)
        self._poller.start(deliver)
        log.info("%s: asking %s every %ds", self.name,
                 ", ".join(one.address for one in self.sources),
                 self.sources[0].interval)

    def close(self) -> None:
        if self._poller is not None:
            self._poller.stop()
            self._poller = None

    # -- the parts of it -------------------------------------------------

    def _raw(self, request: _Request) -> dict[str, str]:
        """The name/value pairs, however this protocol shapes them."""
        return transport.parse(request.text)

    def _station_of(self, raw: dict) -> str:
        try:
            return self.protocol.station_of(raw) or ""
        except Exception:
            return ""

    def _reader(self, dialect: Any, decisions: dict[str, str],
                infer: str = mapping.OFF) -> mapping.Mapper:
        """A mapper that looks things up, cached by everything it was built from.

        **With `infer_unknown='off'` this is a pure function**, which is the
        property `Archiver.rebuild` needs. Checked against `mapping.py` rather
        than assumed: in that mode `_unmapped` never takes a guess, so `seen`
        and `groups` are never written; `ignored` and `warned` only stop the
        same answer being worked out and said twice. The one thing that does
        change with use is the guesses it hands back, which is why the
        archiver never reads them.

        The driver-wide `field_map_extensions` still comes first and the
        operator's placements win: the running installation has two decisions
        under that key from before the settings page existed.
        """
        key = (dialect.name, infer, tuple(sorted(decisions.items())))
        mapper = self._readers.get(key)
        if mapper is None:
            if len(self._readers) >= MAX_READERS:
                # Cleared whole rather than evicted one at a time. Which
                # entry is oldest says nothing about which is next, and a
                # half-cleared cache is one more state to reason about.
                self._readers.clear()
            extensions = dict(self.field_map)
            extensions.update(decisions)
            mapper = mapping.Mapper(dialect, extensions=extensions,
                                    infer_unknown=infer)
            self._readers[key] = mapper
        return mapper


def _without_secrets(readings: dict) -> dict:
    """The readings with the values that name the station taken out.

    This did not have to exist before. `data` held mapped weather fields and
    never a PASSKEY, and the one copy of the body beside it was redacted and
    thrown away after an hour. A journal keeps what the console sent for the
    whole retention period, so without this a PASSKEY, a MAC, a serial or --
    on Weather Underground -- the upload password would sit in it for a week.

    The identity is in its own column, which is the only place anything needs
    it, and it is what `stations.toml` matches against.
    """
    return {name: value for name, value in readings.items() if name not in SECRETS}


def _duration(value: Any, fallback: float) -> float:
    """Seconds from a number or from what somebody typed ("4h", "5m")."""
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        from weewx_evo.options import parse_duration

        return float(parse_duration(str(value)))
    except Exception:
        log.warning("could not read %r as a length of time; using %s seconds",
                    value, fallback)
        return float(fallback)


def _by_identity(stations: dict[str, dict] | None) -> dict[str, dict]:
    """The stations keyed by what a console actually sends.

    They arrive keyed by the display name somebody typed. What reaches the
    lookup is a PASSKEY or station ID read off the upload, so the key has to
    be the identity, folded because consoles upper-case what is typed into
    them.
    """
    found: dict[str, dict] = {}
    for name, entry in (stations or {}).items():
        if not isinstance(entry, dict):
            continue
        identity = str(entry.get("passkey") or entry.get("identity") or "")
        if identity:
            found[identity.casefold()] = entry
        # Under its name as well: a protocol that names its station some
        # other way still finds itself, and it costs one dictionary entry.
        found.setdefault(str(name).casefold(), entry)
    return found


def _clock(station: dict, name: str, fallback: float) -> float:
    found = station.get(name)
    return fallback if found is None else float(found)


def _options(protocol: Any) -> list:
    """Nothing, unless this protocol has to be asked.

    ## A protocol that is pushed at has nothing to configure

    It had three: what to do with a field the catalog does not name, and how
    far a console's clock may be out in either direction. Six protocols
    arrived at once, so that was six settings pages carrying the same three
    fields, and every one of them described something that is not a property
    of a protocol:

        infer_unknown   a policy of the installation. Somebody careful about
                        a guessed field name is careful about all six of
                        them, so it is asked once, in the core.
        max_behind      a property of a console. A stamp is too old because
        max_ahead       a clock is wrong, and a clock is in a box: an old
                        display that drifts and a GW2000 keeping NTP are one
                        protocol and two answers. Set per protocol, the
                        tolerant figure the old one needs would be handed to
                        the new one as well. They are on the station now,
                        defaulting to the core's.

    What is genuinely per-protocol -- the endpoint, the answer the hardware
    waits for, the catalog, which dialects exist -- is in `protocols/` and
    `catalogs/`, and none of it is anybody's to change.

    So the six drivers have no page, and the sidebar has no six entries. A
    driver from outside the repository is unaffected: it declares whatever it
    likes and gets its own page, which is the point of the mechanism.

    ## One that has to be asked has exactly two things

    Where to ask, and how often. There is nowhere else these could come from:
    a sensor with no field to type a server address into cannot announce
    itself, so somebody has to say where it is.

    `addresses` is a list because one household has two PurpleAir sensors as
    readily as one. A driver instance per sensor would be the alternative,
    and two instances of one protocol share a name -- which is half of what
    `stations.by_identity` matches on, so the two sensors would be
    indistinguishable.

    Anything beyond these two is the protocol's own: an API key, an account
    id, a station id. It declares them in `Protocol.options()` and they are
    appended here, so this file never learns what any of them mean.
    """
    if not getattr(protocol, "fetched", False):
        return []

    from weewx_evo.options import Group, Option

    own = list(getattr(protocol, "options", list)() or [])
    return [Group("Where to ask", "This hardware answers whoever asks it, "
                                  "and can be pointed at nothing.", (
        Option("addresses", "Addresses", kind="list", default=(),
               placeholder="192.168.1.50",
               help="One per line. A host, a host and port, or a whole URL "
                    "if it is not plain HTTP. Give the sensor a fixed "
                    "address in your router: one whose address moves stops "
                    "being read, and nothing about that looks like a "
                    "network problem."),
        Option("interval", "Ask every", kind="duration", default=60,
               minimum=polling.FASTEST, maximum=3600,
               help="Faster than the sensor measures buys nothing but "
                    "duplicate readings. Most of them sample every ten "
                    "seconds to a minute."),
        *own,
    ))]


#: Notes that describe how the *upstream WeeWX extension* is configured, not
#: how the hardware is. Matched on a fragment of the sentence rather than the
#: whole of it, and what is not matched is passed through: a note that stops
#: matching because upstream reworded it is a note the page then prints, and
#: `no_note_sends_anybody_to_a_weewx_conf` in tools/console_setup_test.py
#: fails on the words that give it away. Dropping silently would leave the
#: page telling somebody to edit a file this installation does not have.
SAID_HERE_INSTEAD = {
    # Two fragments, one answer. Upstream splits the instruction across a
    # sentence and the config block under it, so catching only the block
    # left "In weewx.conf, then restart:" standing on its own above our
    # replacement -- pointing at a file, then somewhere else. Both map to
    # the same text and the duplicate is dropped below.
    "weewx.conf": (
        "This one arrives over UDP rather than HTTP, so the listener has to "
        "be told to open a second socket: set UDP port to %(udp)s under "
        "Listener and restart. A datagram carries no path, so that port is "
        "the whole of the access control -- the upload token cannot reach it."
    ),
    "[UltimatePush]": (
        "This one arrives over UDP rather than HTTP, so the listener has to "
        "be told to open a second socket: set UDP port to %(udp)s under "
        "Listener and restart. A datagram carries no path, so that port is "
        "the whole of the access control -- the upload token cannot reach it."
    ),
    "in the driver section": (
        "PASSWORD is the upload token. These consoles have no field for a "
        "path, so that is where it goes, and an upload carrying the wrong "
        "one is refused."
    ),
    # Not about configuration, but it names the wrong program: the advice is
    # right and the reader is not running WeeWX.
    "running WeeWX as root": (
        "It posts to port 80, so redirect that rather than running this as "
        "root:"
    ),
}


def _notes(protocol: Any) -> tuple[str, ...]:
    """The protocol's notes, with the ones about weewx.conf said our way.

    `%(udp)s` is filled in here rather than left to the page: which port a
    hub broadcasts on is a property of the hardware and the protocol knows
    it, so the page is left with the three placeholders it can actually
    answer -- where this driver is reachable.
    """
    out = []
    for original in protocol.notes:
        said = original
        for fragment, ours in SAID_HERE_INSTEAD.items():
            if fragment in original:
                # Replaced rather than `%`-formatted: our own text carries
                # `%(address)s` too, and formatting it here would raise on
                # the placeholder that is the page's to fill.
                said = ours.replace(
                    "%(udp)s", str(protocol.default_port or "the hub's port"))
                break
        if said not in out:
            out.append(said)
    return tuple(out)


def _setup(protocol: Any) -> drivers.Setup:
    """How somebody points this protocol's hardware at us.

    Every part of the answer is already on the protocol class, written by the
    people who own the hardware -- which is why this reads rather than
    repeats. The stations page carried a hand-written list of three instead,
    and it had drifted: it turned an Ambient console away with "has no server
    field" while `protocols/ambient.py` lists five, Server IP and Port among
    them.

    The one judgement here is `identity`, and it is structural rather than a
    list of names. A protocol names the field its console identifies itself
    with; if that same field is also one of the ones somebody types in, the
    identity is ours to hand out and the console will carry what it is given.
    Weather Underground is the only one of the six where it is -- `ID` is in
    both -- and an identity handed out cannot be handed out twice, which is
    the better arrangement wherever the hardware allows it.
    """
    typed = {label.strip().lower() for label, _value in protocol.settings}
    carries = [one for one in protocol.identity if one.strip().lower() not in typed]

    # Which row holds the identity and which holds the token is answered
    # here, where the field names are, rather than by the page matching
    # labels against a list. Upstream's own value for both is "anything you
    # like", which is true of a WeeWX installation that checks neither and
    # wrong here: the token is what stands between the open internet and the
    # measurement series.
    named = {one.strip().lower() for one in protocol.identity}
    secret = (protocol.secret or "").strip().lower()
    fields = []
    for label, given in protocol.settings:
        low = label.strip().lower()
        if low in named:
            value = "%(identity)s"
        elif secret and low == secret:
            value = "%(token)s"
        else:
            value = given
        fields.append((label, value))

    return drivers.Setup(
        label=protocol.label,
        hardware=protocol.hardware,
        fields=tuple(fields),
        notes=_notes(protocol),
        identity=carries[0] if carries else "",
        secret=protocol.secret_kind or "",
    )


def driver_class(protocol: Any) -> type:
    """A class per protocol, so each can be asked what it configures.

    `cli.all_schemas` asks `type(driver)` for its options, which is right --
    the settings of a driver are a property of the driver and not of the one
    instance that happens to be running. But six protocols sharing one class
    would then be six identical forms under six names, all of them the last
    protocol's.

    So each gets a subclass with its own `options()` and its own protocol
    bound. Six lines of machinery to keep one line of the core unchanged.
    """
    def options() -> list:
        return _options(protocol)

    def setup() -> drivers.Setup:
        return _setup(protocol)

    # No `__init__` of its own, and that is not tidiness. The core decides
    # what to hand a driver by reading its constructor -- `_accepts` in
    # cli.py, the same way `state` is offered only to a driver that asks for
    # one. A subclass whose `__init__` was `lambda self, **kw` has a
    # signature with no parameters in it at all, so it was offered nothing:
    # not `stations`, not the clock, not the placements the settings page
    # writes. Uploads kept working on the driver-wide field map alone, which
    # is why it went unnoticed. The protocol comes off the class instead.
    return type(
        f"{protocol.name.title()}Driver", (PushDriver,),
        {
            "__doc__": f"{protocol.label}: {protocol.hardware}",
            "protocol_class": protocol,
            "options": staticmethod(options),
            "setup": staticmethod(setup),
        },
    )


def driver_for(protocol: Any,
               precedence: int = protocol_defs.ORDINARY) -> type:
    """Announce a protocol and hand back the class its package exports.

    The one line a protocol package needs. It was a `load(registry)` that
    walked a list of six while all six lived in one directory; with a package
    each, the core loads them through their entry points and this is what
    each one puts behind its own.

    Registering the protocol as well is not tidiness: `detect()` chooses
    between whatever is installed when an upload does not name one, so a
    package that exported a class without announcing it would answer on its
    own endpoint and be invisible to detection.

    `precedence` is passed here rather than set on the protocol class,
    because the parsers are byte-identical to weewx-ultimate-push and this
    number does not exist over there. Where a protocol comes in the order is
    a fact about the set that is installed, so it belongs to the packaging.
    """
    protocol_defs.register(protocol, precedence)
    return driver_class(protocol)
