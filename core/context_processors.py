"""
Context processors for PyRunner templates.
"""

from pyrunner.version import __version__


def pyrunner_version(request):
    """Add PyRunner version to template context."""
    return {
        "pyrunner_version": __version__,
    }
