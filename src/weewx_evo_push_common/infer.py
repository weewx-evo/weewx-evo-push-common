#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""What to do with a field nobody has mapped yet.

Ecowitt ships new sensors faster than drivers get updated, and the usual outcome is
that the readings arrive and are thrown away. That is what happens today: an
HP2561 sends `tf_ch1`, `lightning_num` and six `soil_ec_*` fields, and a driver that
does not know them logs "unrecognized parameter" and drops them.

Two things can be said about an unknown field without guessing:

1.  It continues a series the catalog already describes. The catalog maps `tf_ch1`
    through `tf_ch8` to `soilTemp1` through `soilTemp8`, so `tf_ch9` belongs on
    `soilTemp9`. The continuation is not a guess: the hardware numbers its own
    channels, and the catalog supplies both ends of the series.

    Where that series points is another matter, and it is the catalog's decision
    rather than this module's. A WN34 reports on `tf_chN` whether it is a probe in a
    bed, a silicone lead in a pool, or a sensor on a wall. `soilTemp` is where they
    are put because `extraTemp` is already taken by the WH31, and the reading is in
    the right unit and the right channel either way. See `PLACEMENT_UNKNOWN` in the
    catalog, and `field_map_extensions` for saying where a channel really is.

2.  Its name says what it measures. Ecowitt is consistent about this: a name ending
    in `f` is Fahrenheit, `humidity` and `moisture` are percentages, `mph` is a wind
    speed. That is an inference, not a derivation, and it can be wrong.

The difference matters, so the two are kept apart. A series is applied by default. A
rule is reported and left alone unless asked for, because a field that lands in the
database under the wrong unit is harder to notice, and harder to fix, than one that
never arrived.
"""

import logging
import re

log = logging.getLogger(__name__)

# A name splits into a stem, an index and a tail: 'tf_ch1' -> ('tf_ch', 1, ''),
# 'temp2f' -> ('temp', 2, 'f'), 'wh31_ch1_batt' -> ('wh31_ch', 1, '_batt').
INDEXED = re.compile(r'^(?P<stem>.*?)(?P<index>\d+)(?P<tail>[A-Za-z_]*)$')

# What a name says about what it measures, when no series covers it. Ordered: the
# first match wins, so the specific patterns come before the general ones.
RULES = [
    (r'rssi$', 'group_db', 'dB'),
    (r'_sig$', 'group_count', 'count'),
    (r'batt', 'group_count', 'count'),
    (r'_time$', 'group_time', 'unix_epoch'),
    (r'^barom.*in$', 'group_pressure', 'inHg'),
    (r'rain.*in$|rain.*piezo$', 'group_rain', 'inch'),
    (r'mph$', 'group_speed', 'mile_per_hour'),
    (r'^winddir', 'group_direction', 'degree_compass'),
    (r'^(temp|tf_|soiltemp|thermo)|temp.*f$', 'group_temperature', 'degree_F'),
    (r'humidity|moisture|_hum(\d|$)', 'group_percent', 'percent'),
    (r'^pm(1|4|10|25)', 'group_concentration', 'microgram_per_meter_cubed'),
    (r'co2|^co(\d|$)', 'group_fraction', 'ppm'),
    (r'solarradiation|radiation', 'group_radiation', 'watt_per_meter_squared'),
    (r'^uv', 'group_uv', 'uv_index'),
    (r'^vpd$', 'group_pressure', 'kPa'),
    (r'^(depth|air|thi)_ch', 'group_distance', 'mm'),
]

RULES = [(re.compile(pattern), group, unit) for pattern, group, unit in RULES]


class Guess:
    """One proposal for a field the catalog does not cover.

    Attributes:
        raw (str): The name the hardware used.
        field (str): The WeeWX field it should go to.
        group (str): The unit group, e.g. 'group_temperature'.
        unit (str): The unit the hardware sends it in, or None if the group settles it.
        certain (bool): True if this continues a series in the catalog, i.e. it was
            derived rather than guessed. False if it came from a rule.
        why (str): One line saying how this was arrived at, for the log and for
            whoever has to decide whether to keep it.
    """

    __slots__ = ('raw', 'field', 'group', 'unit', 'certain', 'why')

    def __init__(self, raw, field, group, unit, certain, why):
        self.raw = raw
        self.field = field
        self.group = group
        self.unit = unit
        self.certain = certain
        self.why = why

    def __repr__(self):
        return "Guess(%s -> %s, %s, %s)" % (self.raw, self.field, self.group,
                                            'series' if self.certain else 'rule')

    def __eq__(self, other):
        return isinstance(other, Guess) and repr(self) == repr(other)


def _split(name):
    match = INDEXED.match(name)
    if not match:
        return None
    return match.group('stem'), int(match.group('index')), match.group('tail')


class Inferrer:
    """Works out where an unmapped field belongs, from the catalog and from its name.

    Args:
        fields (dict): Raw field name -> WeeWX field, i.e. the catalog.
        groups (dict): WeeWX field -> unit group, for fields outside the WeeWX schema.
        channels (dict): Raw prefix -> (model, channel count), i.e. how far a family
            is known to go. A channel beyond that is still reported, but as a guess:
            either the table is out of date, or something is wrong.
        prefix (str): What to put in front of a field that only a naming rule could
            place, so that two protocols sending the same unrecognised name do not
            land in one column.
    """

    def __init__(self, fields, groups=None, channels=None, prefix='push_'):
        self.fields = fields
        self.groups = groups or {}
        self.channels = channels or {}
        self.prefix = prefix
        self.series = self._learn_series(fields)

    @staticmethod
    def _learn_series(fields):
        """Find the numbered families in the catalog.

        A family is a raw stem and tail whose members all map to targets sharing one
        stem and tail, with the index offset holding across every member. Anything
        less consistent than that is not a series, and is left out rather than
        smoothed over.
        """
        seen = {}
        for raw, target in fields.items():
            raw_parts = _split(raw)
            target_parts = _split(target)
            if not raw_parts or not target_parts:
                continue
            raw_stem, raw_index, raw_tail = raw_parts
            target_stem, target_index, target_tail = target_parts
            key = (raw_stem, raw_tail)
            entry = (target_stem, target_tail, target_index - raw_index)
            seen.setdefault(key, []).append((entry, target))

        series = {}
        for key, entries in seen.items():
            shapes = {entry for entry, _ in entries}
            if len(shapes) != 1 or len(entries) < 2:
                # Either the family disagrees with itself, or a single member is all
                # there is, which is not enough to call it a series.
                continue
            series[key] = (shapes.pop(), [target for _, target in entries])
        return series

    def guess(self, raw):
        """Return a Guess for an unmapped field, or None if nothing can be said."""
        return self._from_series(raw) or self._from_rules(raw)

    def _from_series(self, raw):
        parts = _split(raw)
        if not parts:
            return None
        stem, index, tail = parts
        found = self.series.get((stem, tail))
        if not found:
            return None
        (target_stem, target_tail, offset), members = found
        field = "%s%d%s" % (target_stem, index + offset, target_tail)
        if field in self.fields.values():
            # Somebody already sends this one under another name. Do not collide.
            return None
        group = self.groups.get(members[0])
        limit = self.channels.get(stem)
        if limit and index > limit[1]:
            # Ecowitt says this family stops before here. The reading is real, so it is
            # not dropped, but it is not derived either: say so and let somebody look.
            return Guess(raw, field, group, None, False,
                         "channel %d, past the %d a %s is said to support"
                         % (index, limit[1], limit[0]))
        return Guess(raw, field, group, None, True,
                     "continues %s, e.g. %s" % (stem + tail, members[0]))

    def _from_rules(self, raw):
        for pattern, group, unit in RULES:
            if pattern.search(raw):
                return Guess(raw, self.prefix + raw, group, unit, False,
                             "name matches %s" % pattern.pattern)
        return None


def report(guesses):
    """Render guesses as the lines a person needs in order to act on them."""
    lines = []
    for guess in sorted(guesses, key=lambda g: (not g.certain, g.raw)):
        lines.append("%-24s -> %-22s %-26s %s"
                     % (guess.raw, guess.field, guess.group or '',
                        ('derived: ' if guess.certain else 'guessed: ') + guess.why))
    return lines
