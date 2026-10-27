# SPDX-FileCopyrightText: 2026 Brookhaven Science Associates, LLC.
# SPDX-License-Identifier: Apache-2.0

"""The ``script`` builder: run a topic-provided build script.

The script (``script = "build.sh"`` in ``[env.<e>]``, relative to
``envs/<e>/``; default ``build.sh``) runs from ``envs/<e>/`` in the build
environment, which carries XERO_SOURCE, XERO_BUILD, XERO_INSTALL, XERO_VIEW,
XERO_ENV, XERO_TOPIC and CMAKE_PREFIX_PATH.  Arguments are the requested
package names, if any.
"""

from __future__ import annotations

from .base import Builder
from ..util import die, sha256_file


class Script(Builder):
    name = "script"

    @property
    def script(self):
        return self.ctx.topic.env_root(self.ctx.env) / self.ctx.config.get("script", "build.sh")

    def fingerprint(self) -> dict:
        return self.ctx.config_fingerprint(self.name, {"script": sha256_file(self.script)})

    def configured(self) -> bool:
        return True

    def configure(self) -> None:
        self.ctx.write_init_cache()

    def build(self, packages: list[str] | None) -> None:
        if not self.script.is_file():
            die(f"no build script {self.script}")
        self.ctx.run(["bash", self.script, *(packages or [])], cwd=self.ctx.topic.env_root(self.ctx.env))
