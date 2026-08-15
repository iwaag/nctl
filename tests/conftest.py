"""Shared fixtures.

`PROFILE_BINDING_VARIABLES` is empty in production: its only entry belonged to
the retired `node_agent` profile. The service-dependency resolver is the
system's single service-dependency path, so the suites that exercise it —
resolution, drift, relations — declare their own binding here rather than
leaving a fictional production entry in place to test against.
"""

import pytest

from nctl_core.production import service_dependencies

TEST_BINDING_PROFILE = "llm_consumer"
TEST_BINDING_NAME = "llm_provider"
TEST_BINDING_VARIABLE = "nintent_llm_provider_url"


@pytest.fixture(autouse=True)
def declared_test_bindings(monkeypatch):
    monkeypatch.setitem(
        service_dependencies.PROFILE_BINDING_VARIABLES,
        (TEST_BINDING_PROFILE, TEST_BINDING_NAME),
        TEST_BINDING_VARIABLE,
    )
