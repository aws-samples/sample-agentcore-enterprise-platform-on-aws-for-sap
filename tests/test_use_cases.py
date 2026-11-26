# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The use-case extension point: manifests, opt-in, prerequisites, namespace.

Pure-Python (the CI pytest job has no aws_cdk). The synth half — that an
enabled use case's stack actually builds — is covered by check-contract.sh
via tests/fixtures/contract/usecase.yaml.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

import infra_utils.platform_config as pc
from infra_utils.platform_config import (
    PlatformConfig,
    UseCaseManifest,
    design_plan,
    discover_use_cases,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "platform"


@pytest.fixture
def fixture_roots(monkeypatch):
    """tests/fixtures/platform/'s layers and use cases, as in test_layers.py."""
    layers, use_cases = FIXTURES / "layers", FIXTURES / "use-cases"
    monkeypatch.setattr(
        pc, "discover_layers", lambda: pc._discover(layers, pc.LayerManifest)
    )
    monkeypatch.setattr(
        pc, "discover_use_cases", lambda: pc._discover(use_cases, pc.UseCaseManifest)
    )


def test_reference_use_case_is_discoverable():
    found = discover_use_cases()
    assert "hello-platform" in found
    m = found["hello-platform"]
    assert m.owner and m.summary  # contribution requirements, not decoration
    assert m.requires == ["gateway"]
    assert m.stacks == ["uc-hello-platform"]


def test_unknown_use_case_is_a_validation_error():
    with pytest.raises(ValueError, match="hello-platform"):
        # The error must LIST what is available, not just refuse.
        PlatformConfig.model_validate({"use_cases": {"no-such-thing": {}}})


def test_nothing_deploys_without_opt_in():
    config = PlatformConfig.model_validate({})
    assert not any("uc-" in s for s in config.expected_stacks())


def test_enabled_use_case_joins_the_footprint():
    config = PlatformConfig.model_validate({"use_cases": {"hello-platform": {}}})
    assert config.expected_stacks()[-1] == "agentcore-workshop-dev-uc-hello-platform"


def test_requires_is_checked_per_federation_role():
    # A federated WORKLOAD account has no local gateway: a gateway-requiring
    # use case must be refused there, with the reason in the message...
    config = PlatformConfig.model_validate(
        {
            "deployment": {
                "strategy": "federated",
                "platform_account": "111111111111",
                "workload_accounts": ["222222222222"],
            },
            "use_cases": {"hello-platform": {}},
        }
    )
    with pytest.raises(ValueError, match="gateway"):
        config.expected_stacks("222222222222")
    # ...and allowed on the platform side, which runs the gateway.
    assert any("uc-hello-platform" in s for s in config.expected_stacks("111111111111"))


def test_uc_namespace_is_enforced():
    with pytest.raises(ValueError, match="uc-"):
        UseCaseManifest.model_validate(
            {
                "name": "sneaky",
                "owner": "x",
                "summary": "tries to claim a core stack name",
                "stacks": ["gateway"],
            }
        )


def test_manifest_rejects_unknown_keys():
    with pytest.raises(ValueError):
        UseCaseManifest.model_validate(
            {
                "name": "typo-carrier",
                "owner": "x",
                "summary": "s",
                "stacks": ["uc-typo-carrier"],
                "stackz": ["oops"],
            }
        )


def test_a_use_case_whose_folder_is_gone_is_one_error_naming_every_place(
    fixture_roots,
):
    # Deleting use-cases/<name>/ leaves its keys in platform.yaml. Each key used to
    # fail on its own terms, or not at all (a layer's access row was accepted).
    raw = {
        "use_cases": {"gone-one": {}},
        "layers": {"demo": {"grants": {"gone-one": {}, "needs-demo": {}}}},
        "identity": {
            "group_claim": "groups",
            "group_map": {"gone-one": {"g": ["gone-one-users"]}},
            "use_case_clients": {
                "gone-two": {"r": {"client_id": "a", "client_secret_name": "b"}}
            },
        },
        "gateway": {
            "interceptor_extensions": [
                {"path": "use-cases/gone-one/check", "target_prefixes": ["gone-one-"]}
            ]
        },
    }
    with pytest.raises(ValidationError) as e:
        PlatformConfig.model_validate(raw)
    assert len(e.value.errors()) == 1
    message = e.value.errors()[0]["msg"]
    assert "not installed: gone-one, gone-two" in message
    for place in (
        "use_cases.gone-one",
        "layers.demo.grants.gone-one",
        "identity.group_map.gone-one",
        "identity.use_case_clients.gone-two",
        "gateway.interceptor_extensions[0].path (use-cases/gone-one/check)",
    ):
        assert f"  - {place}\n" in message
    assert "needs-demo" not in message.split("Installed:")[0]
    assert "Remove these keys" in message and "use-cases/<name>/" in message


def test_an_installed_use_case_may_stay_named_while_disabled(fixture_roots):
    # Turning a use case off keeps its folder; its layer rows may stay for later.
    PlatformConfig.model_validate({"layers": {"demo": {"grants": {"needs-demo": {}}}}})


@pytest.fixture
def two_use_cases(monkeypatch):
    """hello-platform and a copy of it, so two use cases share the Gateway."""
    real = discover_use_cases()["hello-platform"]
    copy = real.model_copy(update={"name": "hello-copy", "stacks": ["uc-hello-copy"]})
    monkeypatch.setattr(
        pc, "discover_use_cases", lambda: {"hello-platform": real, "hello-copy": copy}
    )
    return {"hello-platform": {}, "hello-copy": {}}


@pytest.mark.parametrize(
    ("cedar", "state"),
    [
        ({}, "Cedar is off"),
        ({"enabled": True}, "Cedar is in LOG_ONLY mode, which refuses nothing"),
    ],
)
def test_two_use_cases_without_cedar_enforce_warn(two_use_cases, cedar, state):
    config = PlatformConfig.model_validate(
        {"use_cases": two_use_cases, "security": {"cedar": cedar}}
    )
    assert config.cedar_warning.startswith(
        f"{state}, and 2 use cases share the Gateway (hello-copy, hello-platform)"
    )
    assert "security.cedar.mode: ENFORCE" in config.cedar_warning
    assert config.cedar_warning in config.warnings
    assert f"  - {config.cedar_warning}" in design_plan(config)


def test_one_use_case_or_cedar_enforce_does_not_warn(two_use_cases):
    enforce = {"enabled": True, "mode": "ENFORCE"}
    for raw in (
        {"use_cases": {"hello-platform": {}}},
        {"use_cases": two_use_cases, "security": {"cedar": enforce}},
    ):
        config = PlatformConfig.model_validate(raw)
        assert config.cedar_warning == ""
        assert config.warnings == []
