from django.template import Library, Node

register = Library()

render_count = 0


class CounterNode(Node):
    def render(self, context):
        global render_count
        render_count += 1
        return str(render_count)


@register.tag
def counter(parser, token):
    return CounterNode()


def reset_counter():
    global render_count
    render_count = 0


def get_counter():
    return render_count
