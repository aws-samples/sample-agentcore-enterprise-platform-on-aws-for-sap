# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep the module-level ``random`` functions unique across Runtime V2 restores.

V2 snapshots the process once and restores that snapshot into every new instance, so
the ``random`` module's Mersenne Twister state is identical in all of them. Every trace
and span id OpenTelemetry mints comes from ``random.getrandbits``, and Strands' direct
tool-use ids from ``random.randint``, so without this every instance would emit the same
id sequence. AgentCore has no post-restore hook to reseed from, so the drawing functions
are rebound to ``SystemRandom``, which reads ``os.urandom`` on every call. Both callers
look the function up on the module each time, which is why rebinding reaches them.
"""

import random

_DRAWS = (
    "random",
    "getrandbits",
    "randint",
    "randrange",
    "choice",
    "choices",
    "shuffle",
    "sample",
    "uniform",
)


def use_system_random() -> None:
    system = random.SystemRandom()
    for name in _DRAWS:
        setattr(random, name, getattr(system, name))
