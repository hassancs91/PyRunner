"""
Discovery shim for the SDK Showcase plugin's tests.

The real tests live with the plugin (``examples/sdk_showcase/tests.py``) so they
travel with it in the catalogue. We splice ``examples/`` onto the ``plugins``
package ``__path__`` (as Dev Mode does in ``pyrunner/settings.py``) so the plugin
imports as ``plugins.sdk_showcase``, then re-export its TestCase classes so
``manage.py test core`` picks them up.
"""

from pathlib import Path

from django.conf import settings

import plugins as _plugins_pkg

_examples = str(Path(settings.BASE_DIR) / "examples")
if _examples not in _plugins_pkg.__path__:
    _plugins_pkg.__path__.append(_examples)

from plugins.sdk_showcase.tests import *  # noqa: E402,F401,F403
