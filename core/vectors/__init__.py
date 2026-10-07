"""
Vector seam — embeddings + vector collections as a platform capability.

Layout (see docs/PLAN_vector_db.md):

- ``filters``      the portable payload-filter grammar every backend implements
- ``local_store``  the built-in backend: one SQLite file per collection + exact
                   numpy search over a per-process cache
- ``embeddings``   the OpenAI-compatible ``/v1/embeddings`` client with preset quirks
- ``service``      ``VectorService``: resolve a collection, pick the backend,
                   enforce caps, count usage — the single implementation behind
                   every door (console today; loopback + SDK in Stage 2)

Nothing here is imported at Django start-up except through ``core.services`` and
the console views, so the package stays import-light for scripts and tests.
"""
