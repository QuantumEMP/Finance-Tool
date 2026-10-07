import warnings

# alpaca-py imports the deprecated websockets.legacy at import time (third-party code).
# Import it once here with that single warning silenced so `pytest -W error` stays usable.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="websockets.legacy is deprecated",
                            category=DeprecationWarning)
    import alpaca.trading  # noqa: F401
