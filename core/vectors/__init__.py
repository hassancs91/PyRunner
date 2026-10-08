"""
Vector seam — embeddings + vector collections as a platform capability.

Layout (see docs/PLAN_vector_db.md):

- ``errors``        ``VectorError`` and the backends' ``StoreError`` family
                    (dependency-free, so the plugin SDK can re-export them)
- ``points``        point validation shared by every backend
- ``filters``       the portable payload-filter grammar: the reference evaluator,
                    the SQLite compiler and the Qdrant compiler
- ``local_store``   the built-in backend: one SQLite file per collection + exact
                    numpy search over a per-process cache
- ``qdrant_store``  the Qdrant backend: a thin adapter on its REST API
- ``embeddings``    the OpenAI-compatible ``/v1/embeddings`` client with preset quirks
- ``service``       ``VectorService``: resolve a collection, pick the backend,
                    enforce caps, count usage — the single implementation behind
                    every door (console, loopback helper, plugin SDK, in-process)

Nothing heavy is imported at Django start-up except through ``core.services`` and
the console views, so the package stays import-light for scripts and tests; the
plugin SDK imports only ``errors``, which has no dependencies.
"""
