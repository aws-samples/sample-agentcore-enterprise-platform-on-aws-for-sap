# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cedar conditions for this use case's tools on the shared Gateway.

The conditions are the shared ones (`sap_kit/cedar_policies.py`), keyed on this
use case's scopes, `uc-business-operations/<scope>`. Who holds a scope is told apart
per identity mode (`identity.py`): brokered, the token's `scope` tag; direct, the
calling app's `azp`, from `identity.cedar_principals`.

`for_mode(text, principals=...)` and `scoped_permit(actions, scope, gateway_arn, principals=...)`
take `principals`, that map (scope, or None for the obo read permit, to app ids), or
None in brokered mode.
"""

from __future__ import annotations

import functools

from sap_kit import cedar_policies

SCOPE_PREFIX = "uc-business-operations"


for_mode = functools.partial(cedar_policies.for_mode, scope_prefix=SCOPE_PREFIX)
scoped_permit = functools.partial(
    cedar_policies.scoped_permit, scope_prefix=SCOPE_PREFIX
)
