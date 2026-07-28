"""
Discovery shim for the YouTube Scout plugin's tests.

The real tests live with the plugin (``plugins/youtube_scout/tests.py``) so
they travel with it, and ``plugins/`` is a real package so the plugin imports
as ``plugins.youtube_scout`` with no path splicing. This shim just re-exports
its TestCase classes so ``manage.py test core`` picks them up.

Private plugins under ``plugins/`` are gitignored: the shim therefore no-ops
when the folder isn't present, so a clone without the private plugins still
has a green suite.
"""

from pathlib import Path

from django.conf import settings

_plugin = Path(settings.BASE_DIR) / "plugins" / "youtube_scout"

if (_plugin / "tests.py").exists():
    from plugins.youtube_scout.tests import *  # noqa: E402,F401,F403
