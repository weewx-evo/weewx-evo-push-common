#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""What every pushing weather station has in common. Not a driver itself.

Installing this on its own adds nothing to weewx-evo: it registers no
protocol and answers on no endpoint. It is here because a protocol package
would otherwise carry 1614 lines of machinery that is identical in all of
them, and a field placement corrected in one copy would be wrong in eleven.

## What a protocol package does with it

    from weewx_evo_push_common import driver_for
    from .protocol import Ecowitt

    EcowittDriver = driver_for(Ecowitt)

and one line in its `pyproject.toml`:

    [project.entry-points."weewx_evo.drivers"]
    ecowitt = "weewx_evo_ecowitt:EcowittDriver"

That is the whole of it. `driver_for` announces the protocol so detection can
consider it, and returns a class of its own so the settings page can ask it
what it configures -- one shared class would be one form under twelve names,
all of them the last protocol's.

## What is in here, and what is not

    protocols/__init__   the `Protocol` base: what a parser must answer
    catalogs/__init__    the shape of a field table, and no field table
    mapping              raw names to WeeWX columns, per dialect
    infer                what to do with a name no catalog knows
    transport            reading a body: form-encoded, JSON, query string
    driver               the seam: a `Protocol` as something the listener calls

The catalogs and the parsers themselves are not here. They travel with the
protocol they describe, because that is the thing a person opens when a
sensor is missing.

## Where the parsing comes from

`protocols/` and `catalogs/` in the packages that use this are taken from
weewx-ultimate-push unchanged, and are kept that way. They import nothing --
not WeeWX, not weewx-evo, not this -- so a correction to a field placement is
the same diff in both projects and can travel either way.

The interface is *not* shared with it, and that is deliberate. Over there a
protocol lives inside a WeeWX driver's `genLoopPackets`; here it answers a
listener that already owns the socket, the token, the access policy and the
placement. Trying to have one seam serve both would end as the older engine's
seam.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PushDriver", "driver_class", "driver_for"]

VERSION = "0.1.0"

#: The three names in `driver`, fetched on first use rather than on import.
#:
#: `driver.py` is the seam, so it imports weewx-evo. Nothing else here does:
#: `polling` asks over HTTP, `transport` reads a body, `mapping` turns names
#: into columns, and the protocols and catalogs import nothing at all. A
#: plain `from .driver import ...` at the top of this file would make the
#: core a requirement for reaching any of them -- so the tools that check a
#: field table or a poller would need an installed weewx-evo to import a
#: module that has never heard of it.
#:
#: The same rule the catalogs are held to, one level up.
_SEAM = frozenset(__all__)


def __getattr__(name: str) -> Any:
    if name in _SEAM:
        from . import driver

        return getattr(driver, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
