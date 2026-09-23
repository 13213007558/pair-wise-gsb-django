from unittest import mock

from django.core.exceptions import FieldError
from django.db import NotSupportedError, connection
from django.db.models import Count, F, Q, Sum, Value, Window
from django.db.models.functions import Coalesce, Grouping, RowNumber
from django.test import TestCase

from .models import Sale, Store


class GroupingSetsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        alpha = Store.objects.create(name="alpha")
        beta = Store.objects.create(name="beta")
        Sale.objects.bulk_create(
            [
                Sale(store=alpha, region="east", category="book", amount=10),
                Sale(store=alpha, region="east", category="book", amount=20),
                Sale(store=beta, region="east", category="toy", amount=5),
                Sale(store=beta, region="west", category="book", amount=7),
                # Real NULL values in the grouped data must not be confused
                # with NULLs generated for super-aggregate rows.
                Sale(store=None, region=None, category="book", amount=3),
                Sale(store=alpha, region="east", category=None, amount=4),
            ]
        )

    def test_grouping_sets(self):
        # Hand-computed from the data above: detail rows, region subtotals,
        # and the grand total, with grouping markers for each row.
        expected = [
            # region, category, total, n, g_region, g_category
            ("east", "book", 30, 2, 0, 0),
            ("east", "toy", 5, 1, 0, 0),
            ("east", None, 4, 1, 0, 0),
            ("west", "book", 7, 1, 0, 0),
            (None, "book", 3, 1, 0, 0),
            ("east", None, 39, 4, 0, 1),
            ("west", None, 7, 1, 0, 1),
            (None, None, 3, 1, 0, 1),
            (None, None, 49, 6, 1, 1),
        ]
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(
                total=Sum("amount"),
                n=Count("id"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
            .order_by(
                "g_region",
                "g_category",
                F("region").asc(nulls_last=True),
                F("category").asc(nulls_last=True),
            )
        )
        self.assertEqual(list(qs), expected)

    def test_real_nulls_are_distinguishable_from_grouping_nulls(self):
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region", "category"), ())
            .annotate(
                total=Sum("amount"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
        )
        rows = {tuple(row[:2]): tuple(row[2:]) for row in qs}
        # The detail row of the real NULL region.
        self.assertEqual(rows[(None, "book")], (3, 0, 0))
        # The grand total row also has NULL region and category but is
        # flagged as a super-aggregate row.
        self.assertEqual(rows[(None, None)], (49, 1, 1))

    def test_empty_grouping_set(self):
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(())
            .annotate(total=Sum("amount"), n=Count("id"))
        )
        self.assertEqual(list(qs), [(None, None, 49, 6)])

    def test_empty_table_input(self):
        # Without matching rows only the grand total row is produced.
        qs = (
            Sale.objects.filter(region="atlantis")
            .values_list("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(
                total=Sum("amount"),
                n=Count("id"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
        )
        self.assertEqual(list(qs), [(None, None, None, 0, 1, 1)])

    def test_duplicate_grouping_sets(self):
        # Duplicated grouping sets are preserved and produce duplicated rows.
        expected = [
            ("east", None, 39, 0, 1),
            ("west", None, 7, 0, 1),
            (None, None, 3, 0, 1),
            ("east", None, 39, 0, 1),
            ("west", None, 7, 0, 1),
            (None, None, 3, 0, 1),
            (None, None, 49, 1, 1),
        ]
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region",), ("region",), ())
            .annotate(
                total=Sum("amount"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
            .order_by(
                "g_region",
                "g_category",
                F("region").asc(nulls_last=True),
            )
        )
        self.assertCountEqual(list(qs), expected)

    def test_filtered_aggregates(self):
        expected = [
            # region, book_total, book_n, g_region
            ("east", 30, 2, 0),
            ("west", 7, 1, 0),
            (None, 3, 1, 0),
            (None, 40, 4, 1),
        ]
        qs = (
            Sale.objects.values_list("region")
            .grouping_sets(("region",), ())
            .annotate(
                book_total=Sum("amount", filter=Q(category="book")),
                book_n=Count("id", filter=Q(category="book")),
                g_region=Grouping("region"),
            )
            .order_by("g_region", F("region").asc(nulls_last=True))
        )
        self.assertEqual(list(qs), expected)

    def test_having(self):
        expected = [
            # region, category, total, g_region, g_category
            ("east", "book", 30, 0, 0),
            ("west", "book", 7, 0, 0),
            ("east", None, 39, 0, 1),
            ("west", None, 7, 0, 1),
            (None, None, 49, 1, 1),
        ]
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(
                total=Sum("amount"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
            .filter(total__gt=5)
            .order_by(
                "g_region",
                "g_category",
                F("region").asc(nulls_last=True),
                F("category").asc(nulls_last=True),
            )
        )
        self.assertEqual(list(qs), expected)

    def test_order_by_and_slicing_apply_to_the_combined_result(self):
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(
                total=Sum("amount"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
            .order_by(
                "g_region",
                "g_category",
                F("region").asc(nulls_last=True),
                F("category").asc(nulls_last=True),
            )
        )
        self.assertEqual(
            list(qs[:3]),
            [
                ("east", "book", 30, 0, 0),
                ("east", "toy", 5, 0, 0),
                ("east", None, 4, 0, 0),
            ],
        )
        self.assertEqual(list(qs[8:]), [(None, None, 49, 1, 1)])

    def test_grouping_expression_with_parameters(self):
        expected = [
            ("east", 39, 0),
            ("unknown", 3, 0),
            ("west", 7, 0),
            (None, 49, 1),
        ]
        qs = (
            Sale.objects.annotate(bucket=Coalesce("region", Value("unknown")))
            .values_list("bucket")
            .grouping_sets(("bucket",), ())
            .annotate(total=Sum("amount"), g_bucket=Grouping("bucket"))
            .order_by("g_bucket", "bucket")
        )
        self.assertEqual(list(qs), expected)

    def test_grouping_set_expression_entries(self):
        # Grouping sets entries can be expressions, including expressions
        # with bound parameters.
        expected = [
            ("east", 39, 0),
            ("unknown", 3, 0),
            ("west", 7, 0),
            (None, 49, 1),
        ]
        qs = (
            Sale.objects.annotate(bucket=Coalesce("region", Value("unknown")))
            .values_list("bucket")
            .grouping_sets((Coalesce("region", Value("unknown")),), ())
            .annotate(total=Sum("amount"), g_bucket=Grouping("bucket"))
            .order_by("g_bucket", "bucket")
        )
        self.assertEqual(list(qs), expected)

    def test_joined_grouping_expression(self):
        expected = [
            ("alpha", 34, 0),
            ("beta", 12, 0),
            (None, 3, 0),
            (None, 49, 1),
        ]
        qs = (
            Sale.objects.values_list("store__name")
            .grouping_sets(("store__name",), ())
            .annotate(total=Sum("amount"), g_store=Grouping("store__name"))
            .order_by("g_store", F("store__name").asc(nulls_last=True))
        )
        self.assertEqual(list(qs), expected)

    def test_grouping_sets_subquery(self):
        subquery = (
            Sale.objects.values("region")
            .grouping_sets(("region",), ())
            .annotate(g_region=Grouping("region"))
            .filter(g_region=0)
            .values_list("region")
        )
        regions = {row[0] for row in subquery}
        self.assertEqual(regions, {"east", "west", None})

    def test_filter_on_grouping_marker(self):
        base = Sale.objects.values_list("region").grouping_sets(
            ("region",), ()
        ).annotate(total=Sum("amount"), g_region=Grouping("region"))
        self.assertEqual(list(base.filter(g_region=1)), [(None, 49, 1)])
        self.assertEqual(
            list(base.filter(g_region=0).order_by(F("region").asc(nulls_last=True))),
            [("east", 39, 0), ("west", 7, 0), (None, 3, 0)],
        )

    def test_grouping_sets_sql(self):
        qs = (
            Sale.objects.values_list("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(total=Sum("amount"), g_region=Grouping("region"))
        )
        with mock.patch.object(
            connection.features, "supports_grouping_sets", True
        ):
            sql, _ = qs.query.get_compiler("default").as_sql()
        self.assertIn(
            'GROUP BY GROUPING SETS (("grouping_sets_sale"."region", '
            '"grouping_sets_sale"."category"), ("grouping_sets_sale"."region"), '
            "())",
            sql,
        )
        self.assertIn('GROUPING("grouping_sets_sale"."region")', sql)

    def test_grouping_sets_require_values(self):
        msg = "grouping_sets() must be used in combination with values()."
        with self.assertRaisesMessage(FieldError, msg):
            list(Sale.objects.grouping_sets(()))
        with self.assertRaisesMessage(FieldError, msg):
            list(Sale.objects.annotate(total=Sum("amount")).grouping_sets(()))

    def test_grouping_set_must_be_an_iterable(self):
        msg = "Each grouping set must be an iterable of field names"
        with self.assertRaisesMessage(TypeError, msg):
            Sale.objects.values("region").grouping_sets("region")

    def test_unknown_grouping_set_expression(self):
        qs = Sale.objects.values("region").grouping_sets(("category",))
        msg = "Grouping set expression 'category' isn't part of the query's"
        with self.assertRaisesMessage(FieldError, msg):
            list(qs)

    def test_window_functions_are_rejected(self):
        qs = (
            Sale.objects.values("region")
            .annotate(
                total=Sum("amount"),
                row_number=Window(RowNumber(), order_by="region"),
            )
            .grouping_sets(("region",), ())
        )
        msg = "grouping_sets() cannot be combined with window functions."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)

    def test_combinator_queries_are_rejected(self):
        qs = Sale.objects.values("region").annotate(total=Sum("amount"))
        qs = qs.union(Sale.objects.values("region").annotate(total=Sum("amount")))
        msg = "grouping_sets() cannot be combined with union queries."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs.grouping_sets(("region",), ()))

    def test_extra_select_is_rejected(self):
        qs = (
            Sale.objects.values("region")
            .extra(select={"one": "1"})
            .grouping_sets(("region",), ())
        )
        msg = "grouping_sets() cannot be combined with extra(select=...)."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)

    def test_grouping_without_grouping_sets(self):
        if connection.features.supports_grouping_sets:
            self.skipTest("This database supports GROUPING natively.")
        qs = Sale.objects.values("region").annotate(g=Grouping("region"))
        msg = "Grouping() is only supported on this database as a top-level"
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)
