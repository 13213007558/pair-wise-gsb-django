import contextlib
import json

from django.db import connection
from django.db.models.deletion import ProtectedError, RestrictedError
from django.db.models.signals import post_delete, pre_delete
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    CALLABLE_SET_CALLS,
    CUSTOM_ON_DELETE_CALLS,
    Author,
    Award,
    Book,
    CallableSetTask,
    ConstantSetTask,
    CustomOnDeleteTask,
    Node,
    Place,
    Profile,
    Restaurant,
    Series,
    Shelf,
    Status,
    Tag,
)

@contextlib.contextmanager
def assert_read_only(test_case):
    """Fail if any write SQL or delete signal happens inside the block."""
    fired_signals = []

    def receiver(sender, **kwargs):
        fired_signals.append(sender)

    pre_delete.connect(receiver, weak=False)
    post_delete.connect(receiver, weak=False)
    try:
        with CaptureQueriesContext(connection) as queries:
            yield
    finally:
        pre_delete.disconnect(receiver)
        post_delete.disconnect(receiver)
    test_case.assertEqual(fired_signals, [])
    for query in queries:
        sql = query["sql"].strip().upper()
        test_case.assertTrue(
            sql.startswith("SELECT"),
            msg="Non-SELECT query during preview: %s" % query["sql"],
        )


class DeletePreviewTestMixin:
    def preview(self, queryset):
        with assert_read_only(self):
            result = queryset.delete_preview()
        # The whole result must be JSON-serializable.
        return json.loads(json.dumps(result))


class CascadePreviewTests(DeletePreviewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(name="author")
        cls.book = Book.objects.create(author=cls.author)
        cls.award = Award.objects.create(author=cls.author, book=cls.book)

    def test_cascade_plan_matches_real_delete(self):
        result = self.preview(Author.objects.filter(pk=self.author.pk))
        self.assertIs(result["read_only"], True)
        self.assertEqual(result["database"], "default")
        self.assertEqual(
            result["summary"]["planned"],
            {
                "delete_preview.Author": 1,
                "delete_preview.Award": 1,
                "delete_preview.Book": 1,
            },
        )
        self.assertEqual(result["summary"]["planned_total"], 3)
        self.assertIs(result["summary"]["blocked"], False)
        # The data is untouched by the preview.
        self.assertEqual(Author.objects.count(), 1)
        deleted_total, deleted_per_model = Author.objects.filter(
            pk=self.author.pk
        ).delete()
        self.assertEqual(deleted_per_model, result["summary"]["planned"])
        self.assertEqual(deleted_total, result["summary"]["planned_total"])

    def test_object_reached_twice_counted_once_with_two_reasons(self):
        result = self.preview(Author.objects.all())
        awards = [
            entry
            for entry in result["deletes"]
            if entry["model"] == "delete_preview.Award"
        ]
        self.assertEqual(len(awards), 1)
        self.assertEqual(awards[0]["pk"], self.award.pk)
        self.assertEqual(len(awards[0]["reasons"]), 2)
        reason_fields = {reason["field"] for reason in awards[0]["reasons"]}
        self.assertEqual(reason_fields, {"author", "book"})
        reason_models = {reason["model"] for reason in awards[0]["reasons"]}
        self.assertEqual(
            reason_models, {"delete_preview.Author", "delete_preview.Book"}
        )

    def test_deletes_sorted_and_root_reason(self):
        Book.objects.create(author=self.author)
        result = self.preview(Author.objects.all())
        keys = [(entry["model"], str(entry["pk"])) for entry in result["deletes"]]
        self.assertEqual(keys, sorted(keys))
        author_entry = next(
            entry
            for entry in result["deletes"]
            if entry["model"] == "delete_preview.Author"
        )
        self.assertEqual(
            author_entry["reasons"],
            [{"kind": "queryset", "model": "delete_preview.Author"}],
        )


class SetNullPreviewTests(DeletePreviewTestMixin, TestCase):
    def test_set_null_field_update(self):
        author = Author.objects.create(name="author")
        shelf = Shelf.objects.create(owner=author)
        result = self.preview(Author.objects.filter(pk=author.pk))
        self.assertEqual(
            result["field_updates"],
            [
                {
                    "model": "delete_preview.Shelf",
                    "pk": shelf.pk,
                    "field": "owner",
                    "value": None,
                }
            ],
        )
        self.assertEqual(result["summary"]["field_updates_total"], 1)
        self.assertNotIn(
            "delete_preview.Shelf", result["summary"]["planned"]
        )
        Author.objects.filter(pk=author.pk).delete()
        shelf.refresh_from_db()
        self.assertIsNone(shelf.owner)


class SetConstantPreviewTests(DeletePreviewTestMixin, TestCase):
    def test_set_constant_field_update(self):
        Status.objects.create(code="done")
        open_status = Status.objects.create(code="open")
        task = ConstantSetTask.objects.create(status=open_status)
        result = self.preview(Status.objects.filter(pk="open"))
        self.assertEqual(
            result["field_updates"],
            [
                {
                    "model": "delete_preview.ConstantSetTask",
                    "pk": task.pk,
                    "field": "status",
                    "value": "done",
                }
            ],
        )
        Status.objects.filter(pk="open").delete()
        task.refresh_from_db()
        self.assertEqual(task.status_id, "done")


class ProtectPreviewTests(DeletePreviewTestMixin, TestCase):
    def test_protect_blocks_and_matches_real_error(self):
        author = Author.objects.create(name="author")
        profile = Profile.objects.create(author=author)
        result = self.preview(Author.objects.filter(pk=author.pk))
        self.assertIs(result["summary"]["blocked"], True)
        self.assertEqual(result["summary"]["protected_total"], 1)
        # The planned deletions are still reported alongside the blockers.
        self.assertEqual(result["summary"]["planned_total"], 1)
        self.assertEqual(
            result["protected"],
            [
                {
                    "model": "delete_preview.Profile",
                    "pk": profile.pk,
                    "field": "author",
                    "protects": {
                        "model": "delete_preview.Author",
                        "pk": author.pk,
                    },
                }
            ],
        )
        with self.assertRaises(ProtectedError) as ctx:
            Author.objects.filter(pk=author.pk).delete()
        self.assertEqual(
            {obj.pk for obj in ctx.exception.protected_objects}, {profile.pk}
        )
        self.assertEqual(Author.objects.count(), 1)


class RestrictPreviewTests(DeletePreviewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = Author.objects.create(name="author")
        cls.series = Series.objects.create(title="series", author=cls.author)
        cls.book = Book.objects.create(author=cls.author, series=cls.series)

    def test_restrict_blocks_when_referrer_not_deleted(self):
        result = self.preview(Series.objects.filter(pk=self.series.pk))
        self.assertIs(result["summary"]["blocked"], True)
        self.assertEqual(result["summary"]["restricted_total"], 1)
        self.assertEqual(
            result["restricted"],
            [
                {
                    "model": "delete_preview.Book",
                    "pk": self.book.pk,
                    "field": "series",
                    "references": {
                        "model": "delete_preview.Series",
                        "pk": self.series.pk,
                    },
                }
            ],
        )
        with self.assertRaises(RestrictedError) as ctx:
            Series.objects.filter(pk=self.series.pk).delete()
        self.assertEqual(
            {obj.pk for obj in ctx.exception.restricted_objects}, {self.book.pk}
        )

    def test_restrict_cleared_when_referrer_in_cascade(self):
        # Deleting the author cascades to both the series and the book
        # referencing it, so the RESTRICT relation doesn't block.
        result = self.preview(Author.objects.filter(pk=self.author.pk))
        self.assertIs(result["summary"]["blocked"], False)
        self.assertEqual(result["restricted"], [])
        self.assertEqual(
            result["summary"]["planned"],
            {
                "delete_preview.Author": 1,
                "delete_preview.Book": 1,
                "delete_preview.Series": 1,
            },
        )
        deleted_total, deleted_per_model = Author.objects.filter(
            pk=self.author.pk
        ).delete()
        self.assertEqual(deleted_per_model, result["summary"]["planned"])
        self.assertEqual(deleted_total, 3)


class ManyToManyPreviewTests(DeletePreviewTestMixin, TestCase):
    def test_auto_created_through_model_listed(self):
        author = Author.objects.create(name="author")
        book = Book.objects.create(author=author)
        tag = Tag.objects.create(label="tag")
        book.tags.add(tag)
        result = self.preview(Author.objects.filter(pk=author.pk))
        self.assertEqual(
            result["summary"]["planned"],
            {
                "delete_preview.Author": 1,
                "delete_preview.Book": 1,
                "delete_preview.Book_tags": 1,
            },
        )
        through_entry = next(
            entry
            for entry in result["deletes"]
            if entry["model"] == "delete_preview.Book_tags"
        )
        self.assertEqual(through_entry["reasons"][0]["model"], "delete_preview.Book")
        deleted_total, deleted_per_model = Author.objects.filter(
            pk=author.pk
        ).delete()
        self.assertEqual(deleted_per_model, result["summary"]["planned"])
        self.assertEqual(Tag.objects.count(), 1)


class InheritancePreviewTests(DeletePreviewTestMixin, TestCase):
    def test_parent_model_included(self):
        author = Author.objects.create(name="author")
        restaurant = Restaurant.objects.create(name="place", owner=author)
        result = self.preview(Author.objects.filter(pk=author.pk))
        self.assertEqual(
            result["summary"]["planned"],
            {
                "delete_preview.Place": 1,
                "delete_preview.Restaurant": 1,
                "delete_preview.Author": 1,
            },
        )
        place_entry = next(
            entry
            for entry in result["deletes"]
            if entry["model"] == "delete_preview.Place"
        )
        self.assertEqual(place_entry["pk"], restaurant.pk)
        self.assertEqual(
            place_entry["reasons"][0]["kind"], "inheritance"
        )
        deleted_total, deleted_per_model = Author.objects.filter(
            pk=author.pk
        ).delete()
        self.assertEqual(deleted_per_model, result["summary"]["planned"])
        self.assertEqual(Place.objects.count(), 0)


class CyclePreviewTests(DeletePreviewTestMixin, TestCase):
    def test_self_referential_cycle(self):
        root = Node.objects.create()
        child = Node.objects.create(parent=root)
        leaf = Node.objects.create(parent=child)
        result = self.preview(Node.objects.filter(pk=root.pk))
        self.assertEqual(
            result["deletes"],
            [
                {
                    "model": "delete_preview.Node",
                    "pk": root.pk,
                    "reasons": [
                        {"kind": "queryset", "model": "delete_preview.Node"}
                    ],
                },
                {
                    "model": "delete_preview.Node",
                    "pk": child.pk,
                    "reasons": [
                        {
                            "kind": "relation",
                            "model": "delete_preview.Node",
                            "pk": root.pk,
                            "field": "parent",
                            "on_delete": "CASCADE",
                        }
                    ],
                },
                {
                    "model": "delete_preview.Node",
                    "pk": leaf.pk,
                    "reasons": [
                        {
                            "kind": "relation",
                            "model": "delete_preview.Node",
                            "pk": child.pk,
                            "field": "parent",
                            "on_delete": "CASCADE",
                        }
                    ],
                },
            ],
        )
        deleted_total, _ = Node.objects.filter(pk=root.pk).delete()
        self.assertEqual(deleted_total, 3)
        self.assertEqual(Node.objects.count(), 0)


class UnanalyzablePreviewTests(DeletePreviewTestMixin, TestCase):
    def test_custom_on_delete_not_called(self):
        status = Status.objects.create(code="open")
        task = CustomOnDeleteTask.objects.create(status=status)
        CUSTOM_ON_DELETE_CALLS.clear()
        result = self.preview(Status.objects.filter(pk="open"))
        self.assertEqual(CUSTOM_ON_DELETE_CALLS, [])
        self.assertEqual(result["summary"]["unanalyzable_total"], 1)
        self.assertEqual(
            result["unanalyzable"],
            [
                {
                    "model": "delete_preview.CustomOnDeleteTask",
                    "field": "status",
                    "on_delete": "custom_on_delete",
                    "reason": "Custom on_delete callable would execute code.",
                    "affected": [
                        {
                            "model": "delete_preview.CustomOnDeleteTask",
                            "pk": task.pk,
                        }
                    ],
                }
            ],
        )
        # The real deletion does invoke the custom handler.
        Status.objects.filter(pk="open").delete()
        self.assertEqual(len(CUSTOM_ON_DELETE_CALLS), 1)

    def test_set_callable_not_called(self):
        Status.objects.create(code="done")
        status = Status.objects.create(code="open")
        task = CallableSetTask.objects.create(status=status)
        CALLABLE_SET_CALLS.clear()
        result = self.preview(Status.objects.filter(pk="open"))
        self.assertEqual(CALLABLE_SET_CALLS, [])
        self.assertEqual(result["summary"]["unanalyzable_total"], 1)
        entry = result["unanalyzable"][0]
        self.assertEqual(entry["model"], "delete_preview.CallableSetTask")
        self.assertEqual(entry["on_delete"], "SET")
        self.assertEqual(
            entry["reason"], "SET() with a callable value would execute code."
        )
        self.assertEqual(
            entry["affected"],
            [{"model": "delete_preview.CallableSetTask", "pk": task.pk}],
        )
        Status.objects.filter(pk="open").delete()
        self.assertEqual(len(CALLABLE_SET_CALLS), 1)
        task.refresh_from_db()
        self.assertEqual(task.status_id, "done")


class QuerySetGuardTests(DeletePreviewTestMixin, TestCase):
    def test_invalid_querysets_rejected(self):
        Author.objects.create(name="author")
        with self.assertRaises(TypeError):
            Author.objects.all()[:1].delete_preview()
        with self.assertRaises(TypeError):
            Author.objects.values("name").delete_preview()
        with self.assertRaises(TypeError):
            Author.objects.values_list("name").delete_preview()

    def test_empty_queryset(self):
        result = self.preview(Author.objects.none())
        self.assertEqual(result["deletes"], [])
        self.assertEqual(result["summary"]["planned_total"], 0)
        self.assertIs(result["summary"]["blocked"], False)
