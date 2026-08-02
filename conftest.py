"""Test-session isolation from ambient configuration.

Every test in this repository is meant to declare the environment it needs.
Several of them did not, and instead inherited whatever the developer's ``.env``
happened to say — so the suite passed on a clean checkout and failed on a working
one. Measured on 2026-08-02 with a ``.env`` copied from the shipped
``.env.example``:

    S15_OTEL_EXPORTER_ENDPOINT=...   tests/test_budget_runtime.py::test_the_trace_route_serves_the_span_tree
                                     s15code/core/a2a/tests/test_hardening.py::test_official_subscription_...
    S15_GATEWAY_PROVIDER=gemini      s15code/core/a2a/tests/test_hardening.py::test_official_subscription_...

Both variables are in ``.env.example``, which means following the project's own
setup instructions was enough to see three failures that had nothing to do with
the change under test. That is worse than a flaky test: it teaches a reader to
distrust the suite.

So the whole ``S15_*`` and ``GLC_*`` namespace is cleared for the duration of the
session. Tests that want one of them still set it with ``monkeypatch.setenv``,
which is unaffected — this only removes the ones nobody asked for. It lives at
the repository root so it applies to the tests under ``s15code/`` as well as
those under ``tests/``.

Clearing the variables is not sufficient on its own: ``s15code.main`` calls
``load_dotenv(ROOT / ".env")`` at import, so the file is read again the moment a
test imports the app and every variable comes straight back. The loader is
therefore neutralised for the session as well. Nothing in the suite needs it —
tests configure themselves — and in production the call still runs exactly as
before.
"""

from __future__ import annotations

import os

import dotenv
import pytest

#: Prefixes owned by this project's configuration. A test that depends on one of
#: these must set it itself.
ISOLATED_PREFIXES = ("S15_", "GLC_")


@pytest.fixture(autouse=True, scope="session")
def _isolate_project_environment():
    """Run the suite as if no ``.env`` had been loaded."""
    removed = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(ISOLATED_PREFIXES)
    }
    for name in removed:
        del os.environ[name]

    real_load_dotenv = dotenv.load_dotenv

    def _no_dotenv(*_args, **_kwargs) -> bool:
        """Refuse to read the developer's .env back over the cleared namespace."""
        return False

    dotenv.load_dotenv = _no_dotenv
    try:
        yield removed
    finally:
        dotenv.load_dotenv = real_load_dotenv
        os.environ.update(removed)
