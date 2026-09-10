from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class ExamplePluginConfig(PluginAppConfig):
    name = "plugins.example_plugin"
    label = "example_plugin"
    plugin = PyRunnerPlugin(
        slug="example_plugin",
        name="Example Plugin",
        version="1.0.0",
        nav_items=[
            NavItem(
                label="Example Plugin",
                url_name="example_plugin:index",
                # Optional inline SVG <path>; omit to use PyRun's default icon.
                icon_svg='<path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/>',
            )
        ],
    )
