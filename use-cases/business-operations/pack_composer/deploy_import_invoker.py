# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The pack import at deploy, handed to the composer with its throttles retried.

The composer runs one write at a time, so an author's save that overlaps a deploy would
throttle the import, and the custom-resource provider's own retries last about a second.
This waits out a save, then fails the deploy if the composer is still busy, or says why
the import failed.
"""

from __future__ import annotations

import os

import boto3
import composer_client

#: About a minute in all, where a save holds the composer for a few seconds.
BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


def _proof() -> str:
    """The secret that tells the composer this import is the deploy's."""
    return boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["DEPLOY_IMPORT_PROOF_ARN"]
    )["SecretString"]


def handler(event, _context):
    got = composer_client.invoke({**event, "DeployImportProof": _proof()}, BACKOFF)
    if got is None:
        raise RuntimeError("The pack composer stayed busy; deploy again")
    answer, failed = got
    if failed:
        raise RuntimeError(
            f"The pack import at deploy failed: {answer.get('errorMessage')}"
        )
    return answer
