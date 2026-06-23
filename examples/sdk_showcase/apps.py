from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class SdkShowcaseConfig(PluginAppConfig):
    name = "plugins.sdk_showcase"
    label = "sdk_showcase"
    plugin = PyRunnerPlugin(
        slug="sdk_showcase",
        name="SDK Showcase",
        version="1.0.0",
        nav_items=[
            NavItem(
                label="SDK Showcase",
                url_name="sdk_showcase:index",
                # puzzle-piece icon
                icon_svg='<path stroke-linecap="round" stroke-linejoin="round" d="M11 4a2 2 0 114 0v1a1 1 0 001 1h2a1 1 0 011 1v2a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v2a1 1 0 01-1 1h-2a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-2a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h2a1 1 0 001-1V4z"/>',
                superuser_only=True,
            )
        ],
    )
