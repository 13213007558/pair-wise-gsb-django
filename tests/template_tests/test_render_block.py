from django.template import Context
from django.template.base import BlockNotFound
from django.test import SimpleTestCase

from .templatetags import render_block_tags
from .utils import setup


class RenderBlockTests(SimpleTestCase):
    libraries = {
        "i18n": "django.templatetags.i18n",
        "render_block_tags": "template_tests.templatetags.render_block_tags",
    }

    @setup(
        {
            "block-base": "header[{% block content %}base{% endblock %}]footer",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child{% endblock %}",
            "block-grandchild": '{% extends "block-child" %}'
            "{% block content %}grandchild({{ block.super }}){% endblock %}",
        }
    )
    def test_inheritance_chain(self):
        template = self.engine.get_template("block-grandchild")
        # The whole inheritance chain is resolved, but the content outside of
        # the block (header/footer of the base template) isn't rendered.
        self.assertEqual(
            template.render_block("content", Context({})), "grandchild(child)"
        )

    @setup(
        {
            "block-base": "header[{% block content %}base{% endblock %}]footer",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child({{ block.super }}){% endblock %}",
            "block-grandchild": '{% extends "block-child" %}'
            "{% block content %}grandchild({{ block.super }}){% endblock %}",
        }
    )
    def test_block_super_chain(self):
        template = self.engine.get_template("block-grandchild")
        self.assertEqual(
            template.render_block("content", Context({})),
            "grandchild(child(base))",
        )

    @setup(
        {"block-standalone": "before{% block content %}standalone{% endblock %}after"}
    )
    def test_standalone_template(self):
        template = self.engine.get_template("block-standalone")
        self.assertEqual(template.render_block("content", Context({})), "standalone")

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block extra %}extra{% endblock %}",
        }
    )
    def test_block_defined_only_in_child(self):
        template = self.engine.get_template("block-child")
        self.assertEqual(template.render_block("extra", Context({})), "extra")

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block extra %}extra{% endblock %}",
        }
    )
    def test_block_inherited_from_parent(self):
        template = self.engine.get_template("block-child")
        self.assertEqual(template.render_block("content", Context({})), "base")

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}",
            "block-dynamic": "{% extends parent %}"
            "{% block content %}dynamic({{ block.super }}){% endblock %}",
        }
    )
    def test_extends_from_context_variable(self):
        template = self.engine.get_template("block-dynamic")
        self.assertEqual(
            template.render_block("content", Context({"parent": "block-base"})),
            "dynamic(base)",
        )

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child{% endblock %}",
        }
    )
    def test_block_not_found(self):
        template = self.engine.get_template("block-child")
        msg = "Block with name 'missing' does not exist in template 'block-child'."
        with self.assertRaisesMessage(BlockNotFound, msg):
            template.render_block("missing", Context({}))

    @setup(
        {
            "block-base": "{% load render_block_tags %}{% count_render %}"
            "{% block content %}base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child{% endblock %}",
        }
    )
    def test_content_outside_block_not_rendered(self):
        template = self.engine.get_template("block-child")
        render_block_tags.reset_count()
        template.render_block("content", Context({}))
        # The counting tag in the base template's header wasn't executed.
        self.assertEqual(render_block_tags.render_count, 0)
        template.render(Context({}))
        self.assertEqual(render_block_tags.render_count, 1)

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}"
            "{% block sidebar %}sidebar-base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child({{ block.super }}){% endblock %}"
            "{% block sidebar %}sidebar-child({{ block.super }}){% endblock %}",
        }
    )
    def test_interleaved_blocks_cached_template(self):
        # Rendering different blocks of the same cached template in any order
        # doesn't consume the block.super stacks of the other blocks.
        template = self.engine.get_template("block-child")
        expected = {
            "content": "child(base)",
            "sidebar": "sidebar-child(sidebar-base)",
        }
        for _ in range(2):
            for block_name in ("content", "sidebar"):
                self.assertEqual(
                    template.render_block(block_name, Context({})),
                    expected[block_name],
                )
        # A full render is unaffected by the previous block renders.
        self.assertEqual(
            template.render(Context({})),
            "child(base)sidebar-child(sidebar-base)",
        )

    @setup(
        {
            "block-base": "{% block content %}base{% endblock %}",
            "block-child": '{% extends "block-base" %}'
            "{% block content %}child{% endblock %}",
        }
    )
    def test_render_context_not_polluted(self):
        context = Context({})
        template = self.engine.get_template("block-child")
        template.render_block("content", context)
        # The block context used for the block render is discarded.
        self.assertEqual(len(context.render_context.dicts), 1)

    @setup(
        {
            "block-included": "included {{ value }}",
            "block-main": "{% block content %}main {% include 'block-included' %}"
            "{% endblock %}",
        }
    )
    def test_include_inside_block(self):
        template = self.engine.get_template("block-main")
        self.assertEqual(
            template.render_block("content", Context({"value": "value"})),
            "main included value",
        )

    @setup({"block-autoescape": "{% block content %}{{ value }}{% endblock %}"})
    def test_autoescape(self):
        template = self.engine.get_template("block-autoescape")
        self.assertEqual(
            template.render_block("content", Context({"value": "<b>"})),
            "&lt;b&gt;",
        )

    @setup(
        {
            "block-i18n": "{% load i18n %}{% block content %}"
            "{% trans 'Hello' %}{% endblock %}"
        }
    )
    def test_trans_inside_block(self):
        template = self.engine.get_template("block-i18n")
        self.assertEqual(template.render_block("content", Context({})), "Hello")
