"""Read-only web API over the run database (`python -m MainStoinker serve`).

Imports nothing from the legacy runtime and nothing heavy: the web packages
are imported by `app` and `serve` only, so importing this package is cheap and
works without them.
"""

API_VERSION = "1"
