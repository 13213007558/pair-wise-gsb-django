from django import template

register = template.Library()

render_count = 0


def reset_count():
    global render_count
    render_count = 0


@register.simple_tag
def count_render():
    global render_count
    render_count += 1
    return ""
