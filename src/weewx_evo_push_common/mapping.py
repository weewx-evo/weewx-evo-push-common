#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""From named readings to a WeeWX packet.

This is where a catalog, the user's own mapping and the inference meet. It stays free
of WeeWX imports so that it can be tested with nothing but a captured payload: the
unit groups it wants registered come back as data, and the driver does the registering.

A mapper belongs to one dialect, not to one protocol. Weather Underground has two
dialects on one endpoint, and inference learned from `tempf` and `soiltemp2f` has no
business being applied to `outtemp` and `absbaro`. The driver keeps one mapper per
dialect it has actually seen.
"""

import logging
import re

from . import infer, transport

log = logging.getLogger(__name__)

OFF = 'off'          # drop it, the way every other driver does
SERIES = 'series'    # take it when it continues a series and its placement is not
                     # in question, report the rest
ALL = 'all'          # take whatever can be named, including from rules
MODES = (OFF, SERIES, ALL)


# What a field map says when a reading is to be written nowhere at all. Needed
# because an empty placement and no placement are different answers: no placement
# means the catalog decides, and this means somebody decided against the catalog.
NOWHERE = '-'


class Mapper:
    """Turns the raw readings of one dialect into a WeeWX packet.

    Args:
        dialect (Dialect): The catalog to read with. See protocols/__init__.py.
        extensions (dict): Raw field -> WeeWX field, overriding the catalog. This is
            the user's own mapping, from the configuration file.
        infer_unknown (str): 'off', 'series' or 'all'. See above. Default 'series',
            i.e. accept what can be derived and merely report what was guessed.
        max_behind (int): How many seconds behind ours a console's clock may be
            before its timestamp is ignored and the arrival time used instead.
        max_ahead (int): The same, for a clock that is fast.
    """

    def __init__(self, dialect, extensions=None, infer_unknown=SERIES,
                 max_behind=transport.MAX_BEHIND, max_ahead=transport.MAX_AHEAD):
        if infer_unknown not in MODES:
            raise ValueError("infer_unknown must be one of %s, not '%s'"
                             % (', '.join(MODES), infer_unknown))
        self.dialect = dialect
        self.mode = infer_unknown
        # How far the console's own clock may be out before its timestamp is dropped
        # in favour of the arrival time.
        self.max_behind = max_behind
        self.max_ahead = max_ahead

        self.fields = dict(dialect.fields)
        self.extensions = dict(extensions or {})
        self.fields.update(self.extensions)
        # Fields another driver, or another firmware, puts somewhere else. Until the
        # user says which placement they want, these are not written: either answer
        # can be the one that continues an existing series, and the wrong one cannot
        # be undone.
        self.undecided = dict(dialect.contested)
        for raw in self.extensions:
            # Naming a field yourself is the decision. That settles it.
            self.undecided.pop(raw, None)
        self.groups = dict(dialect.groups)
        self.scale = dict(dialect.scale)
        self.inferrer = infer.Inferrer(self.fields, self.groups, dialect.channels,
                                       prefix=dialect.prefix)
        # Every unmapped field is looked at once. After that it is either part of the
        # mapping or a known refusal, and either way it does not need saying again.
        self.seen = {}
        self.ignored = set()
        self.warned = set()

    def settle(self, settled):
        """Move a field, because the upload itself said where it belongs.

        A firmware that names itself can say what one of its fields means, and then
        the catalog's default is simply wrong for this station. Two firmwares send
        station pressure in a field every other firmware uses for sea-level pressure,
        and both put their name in the payload.

        The user still outranks the firmware: a field named in field_map_extensions
        is not moved, because that was somebody's decision rather than a default.
        """
        for raw, field in (settled or {}).items():
            if raw in self.extensions or self.fields.get(raw) == field:
                continue
            was = self.fields.get(raw)
            self.undecided.pop(raw, None)
            self.fields[raw] = field
            self.seen.pop(raw, None)
            log.info("This firmware means '%s' by '%s', not '%s'. Moving it.",
                     field, raw, was or 'nothing')

    def to_packet(self, raw, now=None):
        """Return (packet, guesses) for one upload.

        `raw` is the name/value pairs, which is what the driver has by the time it
        gets here: it parsed them once already, to work out which protocol sent them.
        A captured payload as text is accepted too, and parsed, because a diagnostic
        run and a test both start from a file rather than from a request.

        The packet is ready for WeeWX apart from its unit system, which the caller
        sets from the dialect, because that is one decision about the whole packet
        rather than one per reading. Guesses are the fields that were not in the
        mapping, whether or not they made it into the packet.
        """
        if isinstance(raw, str):
            raw = transport.parse(raw)
        readings, _ = transport.numbers(raw, self.dialect.metadata,
                                        self.dialect.absent)

        self._check_shared_channels(readings)

        packet = {}
        fresh = []
        for name, value in readings.items():
            if name in self.undecided:
                self._say_undecided(name)
                continue
            field = self.fields.get(name)
            if field is None:
                field = self._unmapped(name, fresh)
                if field is None:
                    continue
            if field == NOWHERE:
                # Placed nowhere on purpose. Somebody looked at two stations both
                # filling one column and said which of them should have it; this is
                # the other one. Dropping it here is the whole of that decision.
                continue
            factor = self.scale.get(name)
            if factor is not None and value is not None:
                value = value * factor
            packet[field] = value

        stamp = transport.device_time(raw, now=now, max_behind=self.max_behind,
                                      max_ahead=self.max_ahead)
        packet['dateTime'] = int(stamp if stamp is not None
                                 else (now if now is not None else _now()))
        return packet, fresh

    def _say_undecided(self, name):
        """Say once that a field is waiting for a decision, and what settles it."""
        if name in self.warned:
            return
        self.warned.add(name)
        log.warning(
            "'%s' is not being written, because drivers disagree about where it goes. "
            "The wrong choice mixes two sensors into one column, and afterwards they "
            "cannot be separated. Add one of these under [[field_map_extensions]]: "
            "'%s = %s' for this driver's placement, or '%s = %s' if your history came "
            "from %s.",
            name, name, self.fields.get(name, '?'),
            name, self.undecided[name], self.dialect.contested_with)

    def _check_shared_channels(self, readings):
        """Warn if two sensors turn out to be writing the same field after all.

        A WH51 and a WH52 are documented with sixteen channels each, but the console
        compatibility table gives them one pool of sixteen between them, so the same
        channel number should never arrive from both. If it does, the assumption is
        wrong and one of the readings is about to overwrite the other.
        """
        for first, second in self.dialect.shared_channels:
            for name in readings:
                if not name.startswith(first):
                    continue
                twin = second + name[len(first):]
                if twin in readings and (name, twin) not in self.warned:
                    self.warned.add((name, twin))
                    log.warning("Both '%s' and '%s' arrived, and they map to the same "
                                "field. One will overwrite the other. Give one of them "
                                "a field of its own in field_map_extensions.",
                                name, twin)

    def _unmapped(self, name, fresh):
        """Decide what happens to a field that is not in the mapping."""
        if name in self.ignored:
            return None
        if name in self.seen:
            return self.seen[name].field

        guess = self.inferrer.guess(name)
        if guess is None:
            log.info("No idea what '%s' is. Left out.", name)
            self.ignored.add(name)
            return None

        fresh.append(guess)
        note = self.placement_note(name)
        take = self.mode == ALL or (self.mode == SERIES and guess.certain and not note)
        if not take:
            if note and guess.certain:
                # The channel is derived, but where its family lands is a convention,
                # and the field it would take may already hold a different sensor's
                # history. Two series in one column cannot be separated afterwards.
                log.info("New channel '%s' would go to '%s'. Which sensor that is, and "
                         "whether that field is free, only you know. Add "
                         "'%s = %s' under [[field_map_extensions]] to accept it.%s",
                         name, guess.field, name, guess.field, note)
            else:
                log.info("New field '%s' looks like %s (%s), but it was only guessed. "
                         "Left out. Add it to field_map_extensions to keep it.",
                         name, guess.group or 'unknown', guess.why)
            self.ignored.add(name)
            return None

        log.info("New field '%s' -> '%s' (%s), %s.%s", name, guess.field,
                 guess.group or 'no group', guess.why, self.placement_note(name) or '')
        self.seen[name] = guess
        if guess.group:
            self.groups[guess.field] = guess.group
        return guess.field

    def placement_note(self, raw):
        """Say so when the field name claims more than the hardware does.

        A WN34 reports on tf_chN whether it is a probe in a bed or a lead in a pool,
        and the catalog has to call it something. Whoever installed it is the only one
        who knows, so the moment a new channel turns up is the moment to say that.
        """
        for prefix, note in self.dialect.placement_unknown.items():
            if re.match(re.escape(prefix) + r'\d', raw):
                return " Placement is a convention, not a reading: " + note
        return None

    def wanted_groups(self):
        """Unit groups the packet needs, for the caller to register with WeeWX."""
        return dict(self.groups)


def _now():
    import time
    return time.time()
