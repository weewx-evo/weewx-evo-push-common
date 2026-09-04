# weewx-evo-push-common

What every pushing weather station has in common, for
[weewx-evo](https://github.com/hilman2/weewx-evo). **This is not a driver.**
Installing it on its own adds nothing: it registers no protocol and answers on
no endpoint.

You do not install this by hand. A protocol package depends on it, so
installing the one for your hardware brings it along:

```bash
weewx-evo driver install weewx-evo-ecowitt
```

## Why it exists

Twelve protocols, and the parts that are the same in all of them come to 1614
lines: the `Protocol` base a parser answers, the mapper that turns raw names
into archive columns, the body reading, and the seam that makes a protocol
into something the listener can call.

Copied into every protocol package, a field placement corrected in one would
be wrong in eleven. Kept here, each package is its parser and its field table
and nothing else -- 88 lines for the Ecowitt parser, against 1614 it would
otherwise carry.

## What a protocol package does with it

```python
from weewx_evo_push_common import driver_for
from .protocol import Ecowitt

EcowittDriver = driver_for(Ecowitt)
```

and one line in its `pyproject.toml`:

```toml
[project.entry-points."weewx_evo.drivers"]
ecowitt = "weewx_evo_ecowitt:EcowittDriver"
```

`driver_for` does two things. It announces the protocol, so that an upload
which does not name one can be recognised; and it returns a class of its own,
so the settings page can ask that protocol what it configures. One shared
class would be one form under twelve names, all of them the last protocol's.

### Detection order

A protocol declares `precedence`, low first. It decides ties, and a tie means
two protocols were equally sure about an upload -- one that looks for a
PASSKEY or a serial number claims low, one that recognises a shape anything
could post claims high.

It has to be a number rather than a position in a list, because the protocols
are separate packages: with the order coming from whatever pip installed
first, the same upload could be read as Ecowitt on one machine and as Weather
Underground on the next, with nothing on either saying why.

## Where the parsing comes from

`protocols/` and `catalogs/` in the packages that use this are taken from
[weewx-ultimate-push](https://github.com/hilman2/weewx-ultimate-push)
unchanged, and are kept that way. They import nothing -- not WeeWX, not
weewx-evo, not this -- so a correction to a field placement is the same diff
in both projects and can travel either way.

The interface is not shared with it. Over there a protocol lives inside a
WeeWX driver's `genLoopPackets`; here it answers a listener that already owns
the socket, the token, the access policy and where each reading is written.
One seam serving both would end up being the older engine's seam.

## Licence

GPL-3.0-or-later, the same as weewx-evo and weewx-ultimate-push.
