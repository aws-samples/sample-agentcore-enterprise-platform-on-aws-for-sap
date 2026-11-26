# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A restored RNG state must not replay the ids OpenTelemetry mints."""

import importlib
import random

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from utils.snapshot_safe import use_system_random


def test_restored_state_does_not_replay_span_ids() -> None:
    try:
        use_system_random()
        snapshot = random.getstate()
        first = RandomIdGenerator().generate_trace_id()
        random.setstate(snapshot)  # what a V2 restore does to every instance
        assert RandomIdGenerator().generate_trace_id() != first
    finally:
        importlib.reload(random)
