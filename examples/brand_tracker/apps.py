from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class BrandTrackerConfig(PluginAppConfig):
    name = "plugins.brand_tracker"
    label = "brand_tracker"
    plugin = PyRunnerPlugin(
        slug="brand_tracker",
        name="Brand Tracker",
        version="1.1.0",
        nav_items=[
            NavItem(
                label="Brand Tracker",
                url_name="brand_tracker:index",
                # magnifying-glass / search icon
                icon_svg='<circle cx="11" cy="11" r="6"/><path stroke-linecap="round" stroke-linejoin="round" d="M20 20l-3.8-3.8"/>',
                superuser_only=True,
            )
        ],
    )
