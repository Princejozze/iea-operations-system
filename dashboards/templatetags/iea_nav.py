"""{% main_nav %}: the navigation bar on every screen (dashboards.navigation)."""

from django import template

from dashboards.navigation import nav_for

register = template.Library()


@register.inclusion_tag("dashboards/_nav.html", takes_context=True)
def main_nav(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    data = {"user": user, "csrf_token": context.get("csrf_token"), "show_menu": False}
    if user is not None and user.is_authenticated and not user.must_change_password:
        data.update(nav_for(user, request), show_menu=True)
    return data
