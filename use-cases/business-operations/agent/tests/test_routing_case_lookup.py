# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The read that resolves an opaque `case_id` for routing.

`[ROUTING] Resolved process_type=… from <case>` is the first line an operator reads when
a case reaches the wrong skill, and a minted id is a digest — so this lookup
fetches the natural key with the process type and the log line prints both.

`key` is a DynamoDB reserved word. Getting that wrong throws a ValidationException that
the lookup's own `except` swallows, so routing would silently fall back to general mode
for every case — visible as worse answers, not as an error.
"""

from unittest import mock

from utils.case_key import to_case_key
from utils.payload import _lookup_process_type

_MINTED = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"
_KEY = {"SupplierInvoice": "5100001976", "FiscalYear": "2026"}


def _run(monkeypatch, item):
    monkeypatch.setenv("CASES_TABLE", "cases")
    table = mock.MagicMock()
    table.get_item.return_value = {"Item": item} if item is not None else {}
    with mock.patch("boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        return _lookup_process_type(_MINTED), table.get_item.call_args.kwargs


def test_the_lookup_returns_the_natural_key_with_the_process_type(monkeypatch):
    result, _ = _run(monkeypatch, {"process_type": "invoice_exception", "key": _KEY})
    assert result == ("invoice_exception", _KEY)


def test_key_reaches_dynamodb_only_as_an_expression_name(monkeypatch):
    _, kwargs = _run(monkeypatch, {"process_type": "invoice_exception", "key": _KEY})
    assert kwargs["ExpressionAttributeNames"] == {"#k": "key"}
    # The reserved word itself must not appear in the projection text.
    assert "key" not in kwargs["ProjectionExpression"]
    assert "#k" in kwargs["ProjectionExpression"]
    assert kwargs["Key"] == to_case_key(_MINTED)


def test_a_case_with_no_key_still_routes(monkeypatch):
    # Routing must not depend on the mitigation for opacity.
    assert _run(monkeypatch, {"process_type": "invoice_exception"})[0] == (
        "invoice_exception",
        None,
    )


def test_a_missing_case_and_a_failed_read_both_yield_a_pair(monkeypatch):
    # The caller unpacks two values unconditionally, so every path must return two.
    assert _run(monkeypatch, None)[0] == (None, None)

    monkeypatch.setenv("CASES_TABLE", "cases")
    with mock.patch("boto3.resource", side_effect=RuntimeError("boom")):
        assert _lookup_process_type(_MINTED) == (None, None)

    monkeypatch.delenv("CASES_TABLE")
    assert _lookup_process_type(_MINTED) == (None, None)
