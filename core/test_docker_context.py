"""
Guard the Docker build context.

`COPY . .` in the Dockerfile copies everything the build context contains,
including gitignored files — `.gitignore` does not protect the image, only
`.dockerignore` does. The v1.17 e2e pass found the private-overlay git dir
(`.git-private`, with the history of the private plan docs and plugins) and
23 host `__pycache__` dirs inside the image because those entries were
missing or not recursive. This test pins the local-only paths that must never
ship, so adding a new local-only dir without an ignore rule fails CI.
"""

from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dockerignore_rules() -> set[str]:
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


class DockerignoreTests(SimpleTestCase):
    # Every path that exists locally on a dev machine but must not enter the
    # image. Keep in sync with docs/PRIVATE_OVERLAY.md and the release notes.
    REQUIRED_RULES = {
        ".git",
        ".git-private",
        ".privateignore",
        "private.ps1",
        "private.sh",
        ".claude/",
        ".env",
        "data/",
        "docs/",
        "dist/",
        "landing/",
        "client_secret.json",
        "token.json",
        "plugins/gmail_agent/",
        "plugins/youtube_scout/",
        # Docker's ignore grammar only matches bare patterns at the context
        # root; caches need the recursive form.
        "**/__pycache__",
        "**/*.pyc",
    }

    def test_local_only_paths_are_excluded_from_the_build_context(self):
        missing = self.REQUIRED_RULES - _dockerignore_rules()
        self.assertFalse(
            missing,
            f".dockerignore is missing rules for local-only paths: {sorted(missing)}",
        )

    def test_python_cache_rules_are_recursive(self):
        rules = _dockerignore_rules()
        for bare in ("__pycache__", "*.pyc", "*.pyo", "*.pyd"):
            self.assertNotIn(
                bare, rules,
                f"'{bare}' only matches at the context root; use '**/{bare}'",
            )
