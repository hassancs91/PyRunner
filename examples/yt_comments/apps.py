from core.plugins import NavItem, PluginAppConfig, PyRunnerPlugin


class YtCommentsConfig(PluginAppConfig):
    name = "plugins.yt_comments"
    label = "yt_comments"
    plugin = PyRunnerPlugin(
        slug="yt_comments",
        name="YouTube Comments AI",
        version="0.5.0",
        nav_items=[
            NavItem(
                label="YT Comments",
                url_name="yt_comments:index",
                # chat-bubble / comment icon
                icon_svg='<path stroke-linecap="round" stroke-linejoin="round" d="M8 10h8M8 13h5m-8.2 5.6L4 21l.9-3.6A8.4 8.4 0 1 1 8 19.4z"/>',
                superuser_only=True,
            )
        ],
    )
