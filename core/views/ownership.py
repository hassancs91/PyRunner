"""
Shared helpers for plugin-owned resource guards in the console (Plugin Platform v2).

A resource with ``owner_plugin`` set is managed by a plugin: the SDK created it and
the plugin depends on it. The generic Scripts/Secrets/DataStores pages must not let
a user silently delete it out from under the plugin. Deletion is blocked with a
message routing them to the plugin — except a superuser may **force-delete** with an
explicit ``force=1`` POST (the documented escape hatch; cascades drop dangling
SecretGrants / DataStoreEntries automatically).

Value edits (e.g. rotating a secret) stay allowed; only structural delete is guarded.
"""


def is_owned(obj) -> bool:
    """True if ``obj`` is owned by a plugin (has a non-empty owner_plugin)."""
    return bool(getattr(obj, "owner_plugin", None))


def owned_delete_blocked(request, obj) -> bool:
    """Whether this delete request must be refused because ``obj`` is plugin-owned.

    Not blocked when the object is unowned, or when a superuser passes ``force=1``
    (the explicit escape hatch).
    """
    if not is_owned(obj):
        return False
    forced = request.user.is_superuser and request.POST.get("force") == "1"
    return not forced


def owned_block_message(obj, kind: str) -> str:
    """The user-facing message explaining why a delete was refused, routing to the plugin."""
    owner = getattr(obj, "owner_plugin", None)
    msg = (
        f"This {kind} is managed by the “{owner}” plugin. Manage it from the "
        f"plugin’s page, or uninstall the plugin to remove it."
    )
    return msg
