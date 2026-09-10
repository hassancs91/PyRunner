"""
Discovery shim for the Brand Tracker plugin's tests.

The real tests live with the plugin (``examples/brand_tracker/tests.py``) so they
travel with it in the catalogue and serve as a reference for "how to test a
PyRun plugin". The plugin folder isn't on the import path during a normal test
run, so here we splice ``examples/`` onto the ``plugins`` package ``__path__``
(exactly as Dev Mode does in ``pyrunner/settings.py``) — making the plugin import
as ``plugins.brand_tracker`` — then re-export its TestCase classes so
``manage.py test core`` picks them up.
"""

from pathlib import Path

from django.conf import settings

import plugins as _plugins_pkg

_examples = str(Path(settings.BASE_DIR) / "examples")
if _examples not in _plugins_pkg.__path__:
    _plugins_pkg.__path__.append(_examples)

from plugins.brand_tracker.tests import *  # noqa: E402,F401,F403
