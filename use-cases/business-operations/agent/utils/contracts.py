# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# GENERATED from use-cases/business-operations/types/. Do not edit:
# change the schema, then run `deploy.sh usecase run business-operations types`.

"""Constants the runtime data contracts declare."""

CASE_STATUSES = (
    "detected",
    "processing",
    "awaiting_human_input",
    "sap_updated",
    "complete",
    "manual_review_required",
    "error",
)
PLATFORM_CASE_STATUSES = (
    "detected",
    "processing",
)
CASE_ID_PATTERN = "^[a-z][a-z0-9]{1,7}-[a-z2-7]{26}$"
PACK_ID_PATTERN = "^[a-z][a-z0-9_]*$"
SHORT_ID_PATTERN = "^[a-z][a-z0-9]{1,7}$"
LIVE_RUN_STALE_SECONDS = 60
