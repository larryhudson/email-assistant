"""Contain cognee's import-time logging side effects.

Importing `email_agent.memory.cognee` pulls the upstream `cognee` package,
which installs its own handlers on the root logger (a stderr stream
formatter that renders Rich-style tracebacks, plus a file handler under
~/.cognee/logs). Those handlers persist for the rest of the pytest
session, which makes any later `_log.exception(...)` in unrelated tests
go through expensive traceback formatting and pytest capture — turning a
0.07s test into 21s when paired with this module.

The fixture below snapshots the root logger state before each test in
this folder and rolls back any handlers cognee installed afterwards.
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _contain_cognee_logging_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
