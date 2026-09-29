"""HTTP routers extracted from ``server.py``.

Each module owns one coherent slice of the API and late-binds back into
``server`` for the helpers and models that stay there — see
``routes.phases`` for the rule and the reason.
"""
