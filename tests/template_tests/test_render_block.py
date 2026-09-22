from django.template import Context, TemplateSyntaxError, engines
from django.template.engine import Engine
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from .templatetags import block_render
from .utils import TEMPLATE_DIR


@override_settings(
    TEMPLATES=[
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [TEMPLATE_DIR],
            "OPTIONS": {
                "context_processors": [
                    "template_tests.test_response.test_processor"
                ],
            },
        }
    ]
)
class RenderBlockTests(SimpleTestCase):
    def setUp(self):
        block_render.reset_counter()
        self.templates = {
            "base.html": (
                "{% load block_render %}HEADER{% counter %}\n"
                "{% block content %}base {{ value }} {% load i18n %}"
                "{% translate \"Page\" %}{% endblock %}\n"
                "FOOTER{% block other %}base other{% endblock %}"
            ),
            "child.html": (
                "{% extends \"base.html\" %}\n"
                "{% block content %}{% include \"include.html\" %}"
                "{{ block.super }}{% endblock %}"
            ),
            "grandchild.html": (
                "{% extends \"child.html\" %}\n"
                "{% block content %}{{ block.super }}grandchild{% endblock %}\n"
                "{% block other %}{{ block.super }}grandchild other{% endblock %}"
            ),
            "include.html": "included {{ value }} ",
        }
        self.engine = Engine(
            libraries={
                "block_render": "template_tests.templatetags.block_render",
                "i18n": "django.templatetags.i18n",
            },
            loaders=[
                (
                    "django.template.loaders.cached.Loader",
                    [("django.template.loaders.locmem.Loader", self.templates)],
                )
            ],
        )

    def test_render_inherited_block_without_page_chrome(self):
        template = self.engine.get_template("grandchild.html")

        output = template.render_block(Context({"value": "<b>"}), "content")

        self.assertEqual(
            output,
            "included &lt;b&gt; base &lt;b&gt; Pagegrandchild",
        )
        self.assertNotIn("HEADER", output)
        self.assertNotIn("FOOTER", output)
        self.assertEqual(block_render.get_counter(), 0)

    def test_full_page_still_renders_chrome(self):
        template = self.engine.get_template("grandchild.html")

        output = template.render(Context({"value": "<b>"}))

        self.assertIn("HEADER1", output)
        self.assertIn("FOOTER", output)
        self.assertEqual(block_render.get_counter(), 1)

    def test_extends_from_context_variable(self):
        parent = self.engine.get_template("base.html")
        template = self.engine.from_string(
            '{% extends parent_name %}'
            '{% block content %}variable {{ block.super }}{% endblock %}'
        )

        output = template.render_block(
            Context({"parent_name": "base.html", "value": "<b>"}),
            "content",
        )

        self.assertEqual(output, "variable base &lt;b&gt; Page")

        template = self.engine.from_string(
            "{% extends parent %}"
            "{% block content %}object {{ block.super }}{% endblock %}"
        )

        output = template.render_block(
            Context({"parent": parent, "value": "<b>"}),
            "content",
        )

        self.assertEqual(output, "object base &lt;b&gt; Page")

    def test_missing_block_reports_template_origin(self):
        template = self.engine.get_template("grandchild.html")

        with self.assertRaisesMessage(
            TemplateSyntaxError,
            "Block 'missing' not found in template 'grandchild.html'",
        ):
            template.render_block(Context(), "missing")

    def test_missing_block_in_string_template_reports_origin(self):
        template = self.engine.from_string("{% block content %}content{% endblock %}")

        with self.assertRaisesMessage(
            TemplateSyntaxError,
            "Block 'missing' not found in template '<unknown source>'",
        ):
            template.render(Context(), "missing")

    def test_low_level_render_and_engine_render_to_string_accept_block_name(self):
        template = self.engine.get_template("grandchild.html")

        self.assertEqual(
            template.render(Context(), "other"),
            "base othergrandchild other",
        )
        self.assertEqual(
            self.engine.render_to_string("grandchild.html", block_name="other"),
            "base othergrandchild other",
        )

    def test_public_loader_and_backend_render_block(self):
        template = engines["django"].from_string(
            "HEADER{% block content %}content{% endblock %}FOOTER"
        )

        self.assertEqual(
            render_to_string("inclusion_extends2.html", block_name="content"),
            "two",
        )
        self.assertEqual(
            template.render_block(block_name="content"),
            "content",
        )

    def test_cached_template_block_renders_are_isolated(self):
        first = self.engine.get_template("grandchild.html")
        second = self.engine.get_template("grandchild.html")
        self.assertIs(first, second)
        context = Context()

        content = "included  base  Pagegrandchild"
        other = "base othergrandchild other"
        for expected in (content, other, other, content, content, other):
            with self.subTest(expected=expected):
                block_name = "content" if expected == content else "other"
                self.assertEqual(
                    first.render_block(context, block_name),
                    expected,
                )

        full_page = first.render(context)
        self.assertIn("HEADER1", full_page)
        self.assertEqual(block_render.get_counter(), 1)

    def test_template_response_renders_selected_block_lazily(self):
        from django.template.response import TemplateResponse

        django_engine = engines["django"]
        parent = django_engine.from_string(
            "HEADER{% block content %}base {{ processors }}{% endblock %}FOOTER"
        )
        child = django_engine.from_string(
            "{% extends parent %}{% block content %}"
            "{{ block.super }} child{% endblock %}"
        )
        request = RequestFactory().get("/")
        response = TemplateResponse(
            request,
            child,
            {"parent": parent},
            block_name="content",
        )

        self.assertFalse(response.is_rendered)
        self.assertEqual(
            response.rendered_content,
            "base yes child",
        )
        self.assertFalse(response.is_rendered)

        callbacks = []
        response.add_post_render_callback(lambda response: callbacks.append(1))
        response.render()
        response.render()

        self.assertTrue(response.is_rendered)
        self.assertEqual(response.content, b"base yes child")
        self.assertNotIn(b"HEADER", response.content)
        self.assertEqual(callbacks, [1])

    def test_simple_template_response_supports_selected_block(self):
        from django.template.response import SimpleTemplateResponse

        template = engines["django"].from_string(
            "HEADER{% block content %}content{% endblock %}FOOTER"
        )
        response = SimpleTemplateResponse(template, block_name="content")

        response.render()

        self.assertEqual(response.content, b"content")
