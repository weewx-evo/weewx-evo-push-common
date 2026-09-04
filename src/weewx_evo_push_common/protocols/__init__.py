#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""The ways hardware pushes readings at us, and how to tell them apart.

A weather station that uploads to a custom server speaks one of a handful of
protocols. They differ in four things, and only four:

    what the fields are called      the catalog
    what the values mean            units, and which reading a name stands for
    what the device wants to hear   an upload it cannot acknowledge is retried
    how the device names itself     PASSKEY, ID, a serial number, a MAC

Everything else is shared, so everything else lives outside this package.

Two words, kept apart on purpose:

    A **protocol** is an exchange. A path, an answer, a way of naming the station.
    Ecowitt and Weather Underground are different protocols.

    A **dialect** is a catalog. The same exchange with different field names, or the
    same names in different units. Fine Offset firmwares speak Weather Underground in
    Fahrenheit and inches, or in Celsius and millimetres, on the same endpoint with
    the same credentials. That is one protocol with two dialects.

The split matters because detection works on protocols and mapping works on dialects.
A driver picks the protocol once per upload and the dialect once per upload, and they
are not the same question.

Nothing here imports weewx. The unit systems are the numbers weewx uses, checked by a
test, so that a protocol can be exercised from a captured payload on a machine with
no WeeWX on it.
"""

import logging

log = logging.getLogger(__name__)

# The unit systems, by the numbers weewx.units gives them. Repeated rather than
# imported so this package stays testable without WeeWX; tests/test_units.py asserts
# they still agree.
US = 1              # F, inHg, inch, mph
METRIC = 16         # C, mbar, cm, km/h
METRICWX = 17       # C, mbar, mm, m/s


class Dialect:
    """One catalog: what the names mean, and what units they arrive in.

    Everything the mapper needs and nothing it does not. A protocol hands one of
    these over per upload, and the driver keeps a mapper per dialect, because
    inference state that was learned from Fahrenheit field names has no business
    being applied to Celsius ones.

    Attributes:
        name (str): What to call it in a log line, e.g. 'wunderground/metric'.
        fields (dict): Raw field name -> WeeWX field.
        groups (dict): WeeWX field -> unit group, for fields outside the WeeWX schema.
        channels (dict): Raw prefix -> (sensor, how many channels), so that a channel
            beyond what the hardware has is reported rather than derived.
        contested (dict): Raw field -> where somebody else puts it. Not written until
            the user says which placement they want.
        contested_with (str): Who that somebody else is, for the log line that asks.
        placement_unknown (dict): Raw prefix -> why the target name claims more than
            the hardware does.
        scale (dict): Raw field -> multiplier, for the few readings that arrive in a
            unit other than the one WeeWX keeps that column in.
        units (int): US, METRIC or METRICWX.
        metadata (frozenset): Fields that name the device rather than measure.
        absent (tuple): Values that mean "no reading", beyond the empty ones.
        prefix (str): What to call a field nothing could place, e.g. 'ecowitt_'.
        shared_channels (tuple): Pairs of families that draw channel numbers from one
            pool, so that the same number arriving from both means an assumption is
            wrong and one reading is about to overwrite the other.
    """

    __slots__ = ('name', 'fields', 'groups', 'channels', 'contested',
                 'contested_with', 'placement_unknown', 'scale', 'units',
                 'metadata', 'absent', 'prefix', 'shared_channels')

    def __init__(self, name, fields, groups=None, channels=None, contested=None,
                 contested_with='', placement_unknown=None, scale=None, units=US,
                 metadata=frozenset(), absent=(), prefix='', shared_channels=()):
        self.name = name
        self.fields = fields
        self.groups = groups or {}
        self.channels = channels or {}
        self.contested = contested or {}
        self.contested_with = contested_with
        self.placement_unknown = placement_unknown or {}
        self.scale = scale or {}
        self.units = units
        self.metadata = metadata
        self.absent = absent
        self.prefix = prefix or (name.split('/')[0] + '_')
        self.shared_channels = shared_channels

    def __repr__(self):
        return "Dialect(%s, %d fields)" % (self.name, len(self.fields))


class Protocol:
    """One way that hardware pushes readings at us.

    Subclasses are mostly data. The methods exist because a protocol is the only
    thing that can say whether an arriving upload belongs to it, which catalog to
    read it with, and how its payload is shaped.
    """

    # What to call it in the configuration file and the log.
    name = ''
    # What to call it in a sentence.
    label = ''
    # Hardware for which this is the protocol, for the log line at startup.
    hardware = ''

    # How somebody points that hardware at this driver.
    #
    # Two parts, kept apart on purpose. `settings` is what goes into the fields of an
    # app, as (label, value) pairs, and the page lays it out as a table to copy from.
    # `notes` is everything that is a sentence rather than a field.
    #
    # Splitting them here rather than guessing in the page is the difference between
    # a table of five things to type and a table with 'In the WSView Plus app' in it
    # as though it were one.
    #
    # '%(address)s', '%(port)s' and '%(path)s' are filled in with where the driver
    # actually is, because the answer to 'what do I type' is not 'your address'.
    settings = ()
    notes = ()

    # Paths this protocol answers on. Empty means any path, which is the case for
    # every device that lets you type the path into an app. A device with the path
    # burned into its firmware lists it here, and that is then also what identifies
    # the protocol before anything has been parsed.
    paths = ()

    # What the device wants to read back. Many treat an upload as failed until they
    # have seen the right answer: they retry, and eventually give up.
    answer = ''
    content_type = 'text/plain'

    # Whether this arrives over UDP rather than HTTP.
    datagram = False
    # The port such a device broadcasts on, when it is fixed by the hardware.
    default_port = None

    # Which raw fields name the station rather than measure anything. The first one
    # present is what the driver checks against its list of known consoles.
    identity = ()
    # A field the device sends that is a shared secret rather than an identifier.
    # Only Weather Underground has one, and it is the only protocol here where the
    # hardware can authenticate itself at all.
    secret = None

    # What kind of secret this hardware can be given when a station is set up:
    #
    #   'path'      the upload path is yours to choose, so it can be a secret and an
    #               identity at once. The driver knows which station an upload is
    #               from before it has parsed anything.
    #   'password'  the protocol carries one of its own.
    #   None        it cannot carry anything. Either its path is burned into the
    #               firmware, or it broadcasts. These are the ones that have to be
    #               adopted: heard first, confirmed afterwards.
    secret_kind = None

    # The catalog, as class attributes, for a protocol with only one dialect.
    fields = {}
    groups = {}
    channels = {}
    contested = {}
    contested_with = ''
    placement_unknown = {}
    scale = {}
    metadata = frozenset()
    absent = ()
    units = US
    shared_channels = ()

    # Which running counter WeeWX has to difference to get 'rain', the amount in this
    # packet. Almost every protocol here sends counters and none of them sends the
    # amount, so without StdDelta pointed at the right one a station records no rain
    # at all. None means the protocol already sends 'rain' and must not be
    # differenced again.
    rain_counter = 'dayRain'

    @classmethod
    def claims(cls, request, raw):
        """How sure this protocol is that the upload is its own.

        Returns 0 for "not mine" and a larger number for a more specific match, so
        that a protocol which recognises itself precisely outranks one that merely
        cannot rule itself out. Given both the request, because a path can settle it
        before parsing, and the parsed pairs, because usually only a name can.
        """
        return 0

    @classmethod
    def dialect(cls, raw):
        """The catalog to read this upload with.

        The default is the one catalog the protocol has. Weather Underground
        overrides this, because the same endpoint carries two.
        """
        return Dialect(cls.name, cls.fields, cls.groups, cls.channels,
                       cls.contested, cls.contested_with, cls.placement_unknown,
                       cls.scale, cls.units, cls.metadata, cls.absent,
                       shared_channels=cls.shared_channels)

    @classmethod
    def readings(cls, request, raw):
        """Return the raw name/value pairs in this upload.

        The default is for the protocols that send them directly, which is most of
        them. A protocol whose payload is shaped otherwise, e.g. WeatherFlow's JSON
        arrays or Acurite's per-sensor frames, overrides this and unpacks its own.
        """
        return raw

    @classmethod
    def station_of(cls, raw):
        """What names the station in this upload, or '' if nothing does."""
        for field in cls.identity:
            value = raw.get(field)
            if value:
                return str(value).strip()
        return ''

    @classmethod
    def settled_contested(cls, raw):
        """Contested fields this particular upload settles by itself.

        A firmware that names itself can remove the doubt about what one of its
        fields means. Returns {raw: field} for the ones it settles.
        """
        return {}


#: Every protocol that has announced itself: name -> (precedence, class).
#:
#: A dict rather than a list because a package can be installed twice over its
#: own lifetime -- an upgrade re-imports it -- and a second copy of Ecowitt in
#: the detection order would be a tie with itself.
#:
#: The precedence is held here rather than on the class because the parser
#: modules are byte-identical to weewx-ultimate-push, where there is one list
#: and no such number. Setting an attribute on the imported class would work
#: and would be the thing that makes the next diff unreadable.
_KNOWN: dict[str, tuple[int, type]] = {}

#: What a protocol that says nothing gets: after everything that named a
#: number, before nothing.
ORDINARY = 100


def register(protocol, precedence: int = ORDINARY):
    """Announce a protocol. Called by the package that carries it, on import.

    This was a hand-written list of six while all six lived in one directory.
    They are separate packages now, so the list cannot be written here: the
    point of a package per protocol is that this file does not know which ones
    exist.
    """
    _KNOWN[protocol.name] = (int(precedence), protocol)
    return protocol


def registry():
    """Every announced protocol, in the order detection considers them.

    **The order is the precedence, not the order packages were installed.**
    A tie means two protocols were equally sure about an upload, and it is
    broken by whichever comes first -- so with the order coming from the
    filesystem, the same upload could be read as Ecowitt on one machine and as
    Weather Underground on the next, with nothing on either saying why.

    Low first, and the number is how surely a protocol recognises itself: one
    that looks for a PASSKEY or a serial number claims low, one that
    recognises a shape anything could post claims high. Equal numbers fall
    back to the name, so the answer is the same on every machine even when two
    packages claim the same rank.
    """
    return [one for _, _, one in
            sorted((rank, name, one) for name, (rank, one) in _KNOWN.items())]


def posting():
    """The protocols that arrive over HTTP.

    These are what 'protocols = auto' listens for. A protocol that broadcasts needs a
    second socket on a port of its own, and opening one for hardware nobody has is
    not something to do by default, so it is named or it is off.
    """
    return [protocol for protocol in registry() if not protocol.datagram]


def by_name(name):
    """The protocol called this, or None."""
    for protocol in registry():
        if protocol.name == name:
            return protocol
    return None


def names():
    """Every protocol name, for the error message that lists the choices."""
    return [protocol.name for protocol in registry()]


def detect(request, raw, among):
    """Which of these protocols an upload belongs to, or None.

    Highest claim wins. A tie goes to whichever comes first in `among`, so that the
    configuration file decides when the payload cannot.
    """
    best = None
    best_score = 0
    for protocol in among:
        score = protocol.claims(request, raw)
        if score > best_score:
            best, best_score = protocol, score
    return best
