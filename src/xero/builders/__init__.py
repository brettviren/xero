# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""Builder plugins, selected per topic+env by ``[env.<e>] builder``."""

from __future__ import annotations

from ..topic import Topic
from .base import Builder, Context
from .cmake import CMakeEach, SuperBuild
from .script import Script

BUILDERS: dict[str, type[Builder]] = {
    SuperBuild.name: SuperBuild,
    CMakeEach.name: CMakeEach,
    Script.name: Script,
}


def get(topic: Topic, env: str) -> Builder:
    return BUILDERS[topic.builder_name(env)](Context(topic, env))
