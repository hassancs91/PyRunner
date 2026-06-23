from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class PluginTemplateConfig(PluginAppConfig):
    name = "plugins.plugin_template"
    label = "plugin_template"
    plugin = PyRunnerPlugin(
        slug="plugin_template",
        name="Plugin Template",
        version="0.1.0",
        nav_items=[
            NavItem(
                label="Plugin Template",
                url_name="plugin_template:index",
                icon_svg='<path stroke-linecap="round" stroke-linejoin="round" d="M4 7h16M4 12h16M4 17h10"/>',
                superuser_only=True,
            )
        ],
    )
