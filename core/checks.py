"""
Django system checks for the vector seam (registered from ``CoreConfig.ready``).

Surfaced by ``manage.py check`` (which CONTRIBUTING requires to be clean), by
``runserver`` and at test start — so a deployment whose Python lacks SQLite's
JSON1 functions, or whose data volume is read-only, learns it at boot rather
than on the first collection write.
"""

import os
import sqlite3

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


def json1_available() -> bool:
    """Python's bundled SQLite ships JSON1; assert rather than assume."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            return conn.execute("SELECT json_extract('{\"a\": 1}', '$.a')").fetchone() == (1,)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


@register(Tags.compatibility)
def check_vector_store_prerequisites(app_configs, **kwargs):
    findings = []
    if not json1_available():
        findings.append(
            Error(
                "The sqlite3 module has no JSON1 functions; the Local vector store "
                "filters payloads with json_extract().",
                hint="Use a Python build linked against SQLite 3.38+ (JSON1 built in).",
                id="core.E001",
            )
        )
    vectors_dir = getattr(settings, "VECTORS_DIR", None)
    if vectors_dir is not None and not os.access(vectors_dir, os.W_OK):
        findings.append(
            Warning(
                f"{vectors_dir} is not writable; Local vector collections cannot be created.",
                hint="Make the data volume writable by the PyRun process.",
                id="core.W001",
            )
        )
    return findings
