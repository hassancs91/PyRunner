"""
Discovery shim for the YouTube Comments AI plugin's tests.

The real tests live with the plugin (``examples/yt_comments/tests.py``) so they
travel with it in the catalogue. The plugin folder isn't on the import path
during a normal test run, so here we splice ``examples/`` onto the ``plugins``
package ``__path__`` (exactly as Dev Mode does in ``pyrunner/settings.py``) —
making the plugin import as ``plugins.yt_comments`` — then re-export its
TestCase classes so ``manage.py test core`` picks them up.
"""

from pathlib import Path

from django.conf import settings

import plugins as _plugins_pkg

_examples = str(Path(settings.BASE_DIR) / "examples")
if _examples not in _plugins_pkg.__path__:
    _plugins_pkg.__path__.append(_examples)

from plugins.yt_comments.tests import *  # noqa: E402,F401,F403
