"""Pure, DB-free business logic ported from the production TIP app.

Nothing in this package imports a database driver, makes a network call,
or touches the filesystem. Every function here takes plain dicts/numbers
and returns plain dicts/numbers, so it can be exercised identically with
synthetic fixtures or live-adapter output.
"""
