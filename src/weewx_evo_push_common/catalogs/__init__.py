#
#    Copyright (c) 2026 Manuel Hilgert
#
#    See the file LICENSE for your full rights.
#
"""What each protocol calls things, and where those things belong in WeeWX.

One module per protocol, and nothing in any of them but data. No imports, no
logic, no WeeWX: a catalog can be read by a person, diffed by a reviewer, and
loaded by a tool that has none of the rest of this package.

## Why there is nothing here

The catalogs used to be imported at the bottom of this file, because they
lived in this directory. They travel with their protocol now -- the Ecowitt
catalog is in `weewx-evo-ecowitt` and nowhere else -- so this package carries
the shape they share and none of the data.

Where a catalog came from is a fact about that catalog, so it is written at
the top of the catalog. A list here would go stale the first time somebody
published a protocol we do not maintain.

## Their shape

Plain dicts, and the one thing a catalog must not do is import. A person
reading a field table should not have to install anything, and the
`import_catalog` tools that generate several of them run with none of this
package present.
"""
