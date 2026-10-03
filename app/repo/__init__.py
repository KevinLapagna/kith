"""Data access. Every function takes an open connection and returns plain dicts.

Two conventions hold across this package:

  * **`sqlite3.Row` is converted to `dict` before it leaves.** Templates read
    `person.nickname` naturally and `{% if person.nickname %}` works, whereas a Row
    only supports `person["nickname"]`.
  * **Blobs are never selected by accident.** `photo` holds two BLOBs per row, so any
    query that lists photos names its columns explicitly and leaves them out. This is
    the difference between a 40 KB page and a 40 MB one.
"""
