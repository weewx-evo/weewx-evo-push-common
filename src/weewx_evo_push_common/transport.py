#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""What every protocol shares: getting name/value pairs out of an upload.

Six protocols arrive here and four of them are the same shape underneath:

    Ecowitt          POST, an urlencoded form body
    Ambient          POST or GET, likewise
    Wunderground     GET, the same pairs in the query string
    Acurite          GET, likewise, one frame per sensor

So a single function handles all four, and the two that are shaped otherwise,
WeatherFlow's JSON and LaCrosse's frames, unpack themselves and hand their pairs back
in the same form.

Everything in this module is pure: text in, a dictionary out. No sockets, no
configuration, and the clock only where a timestamp has to be judged against it. That
is what makes the field work testable from a captured payload, on a machine with no
WeeWX on it.
"""

import hmac
import json
import logging
import re
import time
import urllib.parse

log = logging.getLogger(__name__)

# Fields that name the device rather than measure anything, in any protocol. A
# protocol adds its own on top; this is the floor, so that a field common to several
# of them does not have to be repeated in each.
METADATA = frozenset([
    'PASSKEY', 'stationtype', 'model', 'freq', 'dateutc', 'ID', 'PASSWORD',
    'action', 'realtime', 'rtfreq', 'softwaretype', 'runtime', 'heap', 'interval',
    'mac', 'macAddress', 'serial_number', 'hub_sn', 'firmware_revision',
])

# Values that mean "the sensor had nothing to report". Every protocol has at least
# one; Fine Offset firmwares add -9999, which is a protocol's own business.
ABSENT = ('', '--', '--.-', '-', 'None', 'null')

# How a device stamps its own time.
DEVICE_TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


def parse(text):
    """Split a payload into raw name/value pairs.

    One function for two shapes, because the driver has to know which protocol sent
    an upload before it can ask that protocol anything, and it cannot know that
    before it has looked inside.

    A urlencoded body and a query string are the same thing, so the four posting
    protocols share a line of code. A datagram is JSON, and a JSON object is already
    name/value pairs; the arrays inside it are unpacked later, by the protocol that
    knows what position means what.
    """
    if not text:
        return {}
    text = text.strip()
    if text.startswith('{'):
        try:
            decoded = json.loads(text)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    if text.startswith('?'):
        text = text[1:]
    return dict(urllib.parse.parse_qsl(text, keep_blank_values=False))


# How far behind ours a console's clock may be before its timestamp is ignored. A
# console with an internet connection sets its clock by NTP, so a stamp a few minutes
# old is a late upload rather than a wrong clock: a relay, a queue, a network that was
# down for a while. WeeWX puts such a packet in the interval its timestamp falls in,
# and weewx.loopstore works the record out again if that interval has been written
# already. An hour is well past any delay worth keeping and well short of the years a
# console with no clock at all reports.
MAX_BEHIND = 3600
# And how far ahead. There is no such thing as a reading from the future, so this only
# has to cover the drift between two clocks that are both roughly right.
MAX_AHEAD = 60


def device_time(raw, now=None, max_behind=MAX_BEHIND, max_ahead=MAX_AHEAD):
    """Return the timestamp the device sent, or None if it is not usable.

    Consoles are frequently wrong about the time, sometimes by years, and a record
    stamped in 2015 is worse than no record at all. But one that is merely late is
    worth keeping, and the window is asymmetric for that reason: a reading can be
    delayed, it cannot arrive early.
    """
    stamp = raw.get('dateutc')
    if not stamp or stamp == 'now':
        return None
    try:
        parsed = time.strptime(stamp, DEVICE_TIME_FORMAT)
    except ValueError:
        log.debug("Cannot read device time '%s'", stamp)
        return None
    # The device sends UTC. calendar.timegm would be the obvious call, but this keeps
    # the module free of one more import.
    seconds = _timegm(parsed)
    if now is None:
        now = time.time()
    behind = now - seconds
    if behind > max_behind or -behind > max_ahead:
        log.warning("Device time %s is %s %s than ours, past what %s allows. Using "
                    "ours.", stamp, _how_far(abs(behind)),
                    "behind" if behind > 0 else "ahead",
                    "max_behind" if behind > 0 else "max_ahead")
        return None
    return seconds


def _how_far(seconds):
    """A span of time in whatever unit reads best."""
    if seconds < 120:
        return "%.0f seconds" % seconds
    if seconds < 7200:
        return "%.0f minutes" % (seconds / 60.0)
    if seconds < 172800:
        return "%.1f hours" % (seconds / 3600.0)
    return "%.0f days" % (seconds / 86400.0)


def _timegm(parsed):
    """Seconds since the epoch for a struct_time that is already UTC."""
    import calendar
    return calendar.timegm(parsed)


def numbers(raw, metadata=METADATA, absent=()):
    """Split raw values into the numeric ones and the rest.

    Returns (readings, text), where readings holds everything that could be read as a
    number, and text holds identifiers, model names and anything else that could not.

    A value the hardware sends as an empty field, or as one of its several ways of
    saying "no reading", becomes None rather than being dropped, because a gap is a
    fact about the sensor. `absent` is what this protocol says on top of the ones
    every protocol uses: Fine Offset firmwares send -9999, and without that a missing
    outdoor temperature is recorded as nine thousand degrees below freezing.
    """
    empty = ABSENT + tuple(absent)
    readings = {}
    text = {}
    for name, value in raw.items():
        if name in metadata:
            text[name] = value
            continue
        if isinstance(value, str) and value.strip() in empty:
            readings[name] = None
            continue
        if value is None:
            readings[name] = None
            continue
        try:
            readings[name] = float(value)
        except (TypeError, ValueError):
            text[name] = value
    return readings, text


# Values that name a station rather than describe the weather. A payload is going to
# be pasted into an issue tracker sooner or later, and these are what somebody else
# could use to impersonate the station or find it.
SECRETS = ('PASSKEY', 'ID', 'PASSWORD', 'key', 'stationkey', 'mac', 'macAddress',
           'id',
           'serial_number', 'hub_sn')


def redact(text):
    """Replace the values that name a station, leaving the readings alone.

    Everything not listed in SECRETS is weather, and weather is the point of sending
    a payload to somebody who can help with it.
    """
    for name in SECRETS:
        text = re.sub(r'(^|[?&])%s=[^&]*' % re.escape(name),
                      r'\g<1>%s=X' % name, text)
        # WeatherFlow sends JSON, where the same values are quoted rather than
        # urlencoded. One pass over both shapes, so a datagram is as safe to paste.
        text = re.sub(r'("%s"\s*:\s*)"[^"]*"' % re.escape(name),
                      r'\g<1>"X"', text)
    return text


def same_secret(presented, expected):
    """Whether two secrets match, without saying how far they matched.

    Constant time, so that somebody who can reach the port cannot find a password one
    character at a time by measuring how long the comparison took.
    """
    return hmac.compare_digest(str(presented).encode('utf-8'),
                               str(expected).encode('utf-8'))
