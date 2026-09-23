from django.core.exceptions import FieldError
from django.db import NotSupportedError
from django.db.models import Count, F, Q, Sum, Value, Window
from django.db.models.functions import Concat, Grouping, Upper
from django.db.models.functions.window import RowNumber
from django.test import TestCase

from .models import Sale


class GroupingSetsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Sale.objects.bulk_create(
            [
                # region, category, amount, quantity
                Sale(region="east", category="book", amount=10, quantity=1),
                Sale(region="east", category="book", amount=20, quantity=2),
                Sale(region="east", category="food", amount=5, quantity=1),
                # Genuine NULL category stored in the data.
                Sale(region="east", category=None, amount=100, quantity=3),
                Sale(region="west", category="book", amount=7, quantity=1),
                # Genuine NULL region stored in the data.
                Sale(region=None, category="book", amount=3, quantity=1),
            ]
        )

    def rows(self, qs):
        return [
            (
                row["region"],
                row["category"],
                row["total"],
                row["n"],
                row["g_region"],
                row["g_category"],
            )
            for row in qs
        ]

    def grouping_queryset(self, *sets):
        return (
            Sale.objects.values("region", "category")
            .grouping_sets(*sets)
            .annotate(
                total=Sum("amount"),
                n=Count("pk"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
        )

    def test_results_against_hand_computed_table(self):
        """
        Every row produced on SQLite (through the UNION ALL emulation) is
        compared against a hand-computed table, including the Grouping()
        markers that tell genuine NULLs apart from grouping NULLs.
        """
        qs = self.grouping_queryset(
            ("region", "category"), ("region",), ("region",), ()
        )
        expected = [
            # Details: both grouping expressions are part of the set.
            ("east", "book", 30, 2, 0, 0),
            ("east", "food", 5, 1, 0, 0),
            # Genuine NULL category in the data, markers are still (0, 0).
            ("east", None, 100, 1, 0, 0),
            ("west", "book", 7, 1, 0, 0),
            # Genuine NULL region in the data; its detail row must not be
            # confused with a subtotal or the grand total.
            (None, "book", 3, 1, 0, 0),
            # Region subtotals: category is grouped away (marker 1).
            ("east", None, 135, 4, 0, 1),
            ("west", None, 7, 1, 0, 1),
            # Subtotal of the genuine NULL region: (0, 1), not (1, 1).
            (None, None, 3, 1, 0, 1),
            # The duplicate ("region",) set produces a second copy.
            ("east", None, 135, 4, 0, 1),
            ("west", None, 7, 1, 0, 1),
            (None, None, 3, 1, 0, 1),
            # Grand total: both expressions are grouped away.
            (None, None, 145, 6, 1, 1),
        ]
        with self.assertNumQueries(1):
            self.assertCountEqual(self.rows(qs), expected)

    def test_empty_grouping_set_only(self):
        # A grand-total-only query cannot select the grouped columns, so
        # they are projected away with a trailing values().
        qs = (
            Sale.objects.values("region", "category")
            .grouping_sets(())
            .annotate(
                total=Sum("amount"),
                n=Count("pk"),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
            .values("total", "n", "g_region", "g_category")
        )
        self.assertEqual(
            [
                (row["total"], row["n"], row["g_region"], row["g_category"])
                for row in qs
            ],
            [(145, 6, 1, 1)],
        )

    def test_no_matching_details(self):
        # The filter matches no rows: grouped branches return nothing while
        # the empty grouping set still yields the grand total row.
        qs = self.grouping_queryset(("region", "category"), ("region",), ()).filter(
            amount__gt=1000000
        )
        self.assertEqual(self.rows(qs), [(None, None, None, 0, 1, 1)])

    def test_filtered_aggregates(self):
        qs = (
            Sale.objects.values("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(
                total=Sum("amount", filter=Q(category="book")),
                n=Count("pk", filter=Q(category="book")),
                g_region=Grouping("region"),
                g_category=Grouping("category"),
            )
        )
        expected = [
            ("east", "book", 30, 2, 0, 0),
            # SUM() over no matching rows is NULL, COUNT() is 0.
            ("east", "food", None, 0, 0, 0),
            ("east", None, None, 0, 0, 0),
            ("west", "book", 7, 1, 0, 0),
            (None, "book", 3, 1, 0, 0),
            ("east", None, 30, 2, 0, 1),
            ("west", None, 7, 1, 0, 1),
            (None, None, 3, 1, 0, 1),
            (None, None, 40, 4, 1, 1),
        ]
        self.assertCountEqual(self.rows(qs), expected)

    def test_having_on_aggregate(self):
        qs = self.grouping_queryset(
            ("region", "category"), ("region",), ()
        ).filter(total__gte=30)
        expected = [
            ("east", "book", 30, 2, 0, 0),
            ("east", None, 100, 1, 0, 0),
            ("east", None, 135, 4, 0, 1),
            (None, None, 145, 6, 1, 1),
        ]
        self.assertCountEqual(self.rows(qs), expected)

    def test_having_on_grouping_marker(self):
        # Filtering on a Grouping() marker is evaluated per grouping set.
        qs = self.grouping_queryset(
            ("region", "category"), ("region",), ()
        ).filter(g_category=1)
        expected = [
            ("east", None, 135, 4, 0, 1),
            ("west", None, 7, 1, 0, 1),
            (None, None, 3, 1, 0, 1),
            (None, None, 145, 6, 1, 1),
        ]
        self.assertCountEqual(self.rows(qs), expected)

    def test_order_by_and_slice_apply_to_combined_result(self):
        qs = self.grouping_queryset(
            ("region", "category"), ("region",), ()
        ).order_by(F("total").desc(nulls_last=True))[:3]
        self.assertEqual(
            self.rows(qs),
            [
                (None, None, 145, 6, 1, 1),
                ("east", None, 135, 4, 0, 1),
                # The detail row with a genuine NULL category sorts by its
                # own total, not by any subtotal.
                ("east", None, 100, 1, 0, 0),
            ],
        )

    def test_grouping_expression_with_bound_params(self):
        qs = (
            Sale.objects.annotate(region_tag=Concat("region", Value("!")))
            .values("region_tag")
            .grouping_sets(("region_tag",), ())
            .annotate(total=Sum("amount"), g_tag=Grouping("region_tag"))
        )
        self.assertCountEqual(
            [(row["region_tag"], row["total"], row["g_tag"]) for row in qs],
            [
                ("east!", 135, 0),
                ("west!", 7, 0),
                # Concat() coalesces the genuine NULL region to ''.
                ("!", 3, 0),
                (None, 145, 1),
            ],
        )

    def test_grouping_expression_member(self):
        # Grouping set members can be expressions, not only field names.
        qs = (
            Sale.objects.values(region_upper=Upper("region"))
            .grouping_sets((Upper("region"),), ())
            .annotate(total=Sum("amount"), g=Grouping(Upper("region")))
        )
        rows = {(row["region_upper"], row["total"], row["g"]) for row in qs}
        self.assertEqual(
            rows,
            {("EAST", 135, 0), ("WEST", 7, 0), (None, 3, 0), (None, 145, 1)},
        )

    def test_multiple_argument_grouping_bitmap(self):
        qs = (
            Sale.objects.values("region", "category")
            .grouping_sets(("region", "category"), ("region",), ())
            .annotate(g=Grouping("region", "category"), total=Sum("amount"))
        )
        bitmaps = [row["g"] for row in qs]
        # Details: 0b00, region subtotals: 0b01, grand total: 0b11.
        self.assertCountEqual(bitmaps, [0, 0, 0, 0, 0, 1, 1, 1, 3])

    def test_window_functions_rejected(self):
        qs = (
            Sale.objects.values("region")
            .annotate(
                n=Count("pk"),
                rn=Window(expression=RowNumber(), order_by="region"),
            )
            .grouping_sets(("region",), ())
        )
        msg = "GROUPING SETS cannot be used with window functions."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)

    def test_combined_queries_rejected(self):
        qs = Sale.objects.values("region").grouping_sets(("region",), ())
        msg = "GROUPING SETS cannot be combined with union queries."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs.union(qs))

    def test_grouping_sets_after_combined_queries_rejected(self):
        qs = Sale.objects.values("region")
        combined = qs.union(qs)
        msg = "Calling QuerySet.grouping_sets() after union() is not supported."
        with self.assertRaisesMessage(NotSupportedError, msg):
            combined.grouping_sets(("region",), ())

    def test_distinct_rejected(self):
        qs = Sale.objects.values("region").grouping_sets(("region",), ()).distinct()
        msg = "GROUPING SETS cannot be used with DISTINCT queries."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)

    def test_select_for_update_rejected(self):
        qs = (
            Sale.objects.values("region")
            .grouping_sets(("region",), ())
            .select_for_update()
        )
        msg = "GROUPING SETS cannot be used with select_for_update()."
        with self.assertRaisesMessage(NotSupportedError, msg):
            list(qs)

    def test_uncovered_group_by_expression_rejected(self):
        qs = Sale.objects.values("region", "category").grouping_sets(
            ("region",), ()
        )
        msg = "GROUPING SETS declarations do not cover"
        with self.assertRaisesMessage(FieldError, msg):
            list(qs)

    def test_unknown_grouping_set_member_rejected(self):
        qs = Sale.objects.values("region").grouping_sets(("unknown",), ())
        msg = "Cannot resolve keyword 'unknown' into a grouping set expression."
        with self.assertRaisesMessage(FieldError, msg):
            list(qs)

    def test_grouping_sets_requires_at_least_one_set(self):
        with self.assertRaisesMessage(
            ValueError, "grouping_sets() requires at least one grouping set."
        ):
            Sale.objects.values("region").grouping_sets()
