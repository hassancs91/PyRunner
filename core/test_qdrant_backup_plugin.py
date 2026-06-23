"""
Discovery shim for the Qdrant Backup plugin's tests.

The real tests live with the plugin (``examples/qdrant_backup/tests.py``) so they
travel with it in the catalogue and serve as the reference for "how to test a
PyRunner plugin". The plugin folder isn't on the import path during a normal test
run, so here we splice ``examples/`` onto the ``plugins`` package ``__path__``
(exactly as Dev Mode does in ``pyrunner/settings.py``) — making the plugin import
as ``plugins.qdrant_backup`` — then re-export its TestCase classes so
``manage.py test core`` picks them up.
"""

from pathlib import Path

from django.conf import settings

import plugins as _plugins_pkg

_examples = str(Path(settings.BASE_DIR) / "examples")
if _examples not in _plugins_pkg.__path__:
    _plugins_pkg.__path__.append(_examples)

from plugins.qdrant_backup.tests import *  # noqa: E402,F401,F403
