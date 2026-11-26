# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations against the SAP layer it requires: the caller both checks key on,
its production fixture's pinned server, and the layer's parameters no role may write.

The layer's own tests are in layers/sap/tests/test_sap_layer.py; these import its helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CONTRACT = REPO / "use-cases/business-operations/fixtures/contract"
sys.path.insert(0, str(REPO / "use-cases" / "business-operations"))
sys.path.insert(0, str(REPO / "layers" / "sap"))  # sap_check, as the layer's tests do
sys.path.append(str(REPO / "layers" / "sap" / "tests"))

import business_operations_check as bo
import sap_check
from test_sap_layer import _bearer, assert_no_role_writes_the_layers_parameters


def test_the_caller_is_the_app_business_operations_check_keys_its_claim_on():
    # Extensions are bundled apart, so each decodes the token itself: keep them equal.
    for claims in (
        {"client_id": "c1"},
        {"azp": "a1"},
        {"azp": "a1", "scp": "x", "oid": "1"},
    ):
        headers = {"Authorization": _bearer(claims)}
        assert sap_check._caller(headers) == bo._app_id(headers)


def test_the_production_fixture_pins_the_image():
    raw = yaml.safe_load((CONTRACT / "business-operations-production.yaml").read_text())
    assert raw["layers"]["sap"]["image_tag"] not in (None, "latest")


@pytest.mark.parametrize(
    "fixture",
    ["business-operations-production.yaml", "business-operations-direct.yaml"],
)
def test_no_role_the_stacks_define_may_write_the_layers_parameters(tmp_path, fixture):
    config = yaml.safe_load((CONTRACT / fixture).read_text())
    assert_no_role_writes_the_layers_parameters(tmp_path, config)
