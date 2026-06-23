from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class QdrantBackupConfig(PluginAppConfig):
    name = "plugins.qdrant_backup"
    label = "qdrant_backup"
    plugin = PyRunnerPlugin(
        slug="qdrant_backup",
        name="Qdrant Backup",
        version="1.0.0",
        nav_items=[
            NavItem(
                label="Qdrant Backup",
                url_name="qdrant_backup:index",
                # database / storage icon
                icon_svg='<path stroke-linecap="round" stroke-linejoin="round" d="M4 7c0-1.66 3.58-3 8-3s8 1.34 8 3-3.58 3-8 3-8-1.34-8-3z"/><path stroke-linecap="round" stroke-linejoin="round" d="M4 7v5c0 1.66 3.58 3 8 3s8-1.34 8-3V7"/><path stroke-linecap="round" stroke-linejoin="round" d="M4 12v5c0 1.66 3.58 3 8 3s8-1.34 8-3v-5"/>',
                superuser_only=True,
            )
        ],
    )
