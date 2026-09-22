from django.db import connection
from django.db.models import Count, Max, Q, Sum
from django.test import TestCase

from .models import Comment, Order, Post, Product, Tenant, User, Warehouse


class CompositePKAggregateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create()
        cls.tenant_2 = Tenant.objects.create()
        cls.user_1 = User.objects.create(
            tenant=cls.tenant_1,
            id=1,
            email="user0001@example.com",
        )
        cls.user_2 = User.objects.create(
            tenant=cls.tenant_1,
            id=2,
            email="user0002@example.com",
        )
        cls.user_3 = User.objects.create(
            tenant=cls.tenant_2,
            id=3,
            email="user0003@example.com",
        )
        cls.comment_1 = Comment.objects.create(id=1, user=cls.user_2, text="foo")
        cls.comment_2 = Comment.objects.create(id=2, user=cls.user_1, text="bar")
        cls.comment_3 = Comment.objects.create(id=3, user=cls.user_1, text="foobar")
        cls.comment_4 = Comment.objects.create(id=4, user=cls.user_3, text="foobarbaz")
        cls.comment_5 = Comment.objects.create(id=5, user=cls.user_3, text="barbaz")
        cls.comment_6 = Comment.objects.create(id=6, user=cls.user_3, text="baz")

    def test_users_annotated_with_comments_id_count(self):
        user_1, user_2, user_3 = User.objects.annotate(Count("comments__id")).order_by(
            "pk"
        )

        self.assertEqual(user_1, self.user_1)
        self.assertEqual(user_1.comments__id__count, 2)
        self.assertEqual(user_2, self.user_2)
        self.assertEqual(user_2.comments__id__count, 1)
        self.assertEqual(user_3, self.user_3)
        self.assertEqual(user_3.comments__id__count, 3)

    def test_users_annotated_with_aliased_comments_id_count(self):
        user_1, user_2, user_3 = User.objects.annotate(
            comments_count=Count("comments__id")
        ).order_by("pk")

        self.assertEqual(user_1, self.user_1)
        self.assertEqual(user_1.comments_count, 2)
        self.assertEqual(user_2, self.user_2)
        self.assertEqual(user_2.comments_count, 1)
        self.assertEqual(user_3, self.user_3)
        self.assertEqual(user_3.comments_count, 3)

    def test_users_annotated_with_comments_count(self):
        user_1, user_2, user_3 = User.objects.annotate(Count("comments")).order_by("pk")

        self.assertEqual(user_1, self.user_1)
        self.assertEqual(user_1.comments__count, 2)
        self.assertEqual(user_2, self.user_2)
        self.assertEqual(user_2.comments__count, 1)
        self.assertEqual(user_3, self.user_3)
        self.assertEqual(user_3.comments__count, 3)

    def test_users_annotated_with_comments_count_filter(self):
        user_1, user_2, user_3 = User.objects.annotate(
            comments__count=Count(
                "comments", filter=Q(pk__in=[self.user_1.pk, self.user_2.pk])
            )
        ).order_by("pk")

        self.assertEqual(user_1, self.user_1)
        self.assertEqual(user_1.comments__count, 2)
        self.assertEqual(user_2, self.user_2)
        self.assertEqual(user_2.comments__count, 1)
        self.assertEqual(user_3, self.user_3)
        self.assertEqual(user_3.comments__count, 0)

    def test_count_distinct(self):
        user_1, user_2, user_3 = User.objects.annotate(
            comments__count=Count("comments", distinct=True)
        ).order_by("pk")

        self.assertEqual(user_1, self.user_1)
        self.assertEqual(user_1.comments__count, 2)
        self.assertEqual(user_2, self.user_2)
        self.assertEqual(user_2.comments__count, 1)
        self.assertEqual(user_3, self.user_3)
        self.assertEqual(user_3.comments__count, 3)

    def test_count_distinct_no_comments(self):
        user_4 = User.objects.create(
            tenant=self.tenant_2,
            id=4,
            email="user0004@example.com",
        )
        users = User.objects.annotate(
            comments__count=Count("comments", distinct=True)
        ).order_by("pk")

        self.assertEqual(users.last(), user_4)
        self.assertEqual(users.last().comments__count, 0)

    def test_user_values_annotated_with_comments_id_count(self):
        self.assertSequenceEqual(
            User.objects.values("pk").annotate(Count("comments__id")).order_by("pk"),
            (
                {"pk": self.user_1.pk, "comments__id__count": 2},
                {"pk": self.user_2.pk, "comments__id__count": 1},
                {"pk": self.user_3.pk, "comments__id__count": 3},
            ),
        )

    def test_user_values_annotated_with_filtered_comments_id_count(self):
        self.assertSequenceEqual(
            User.objects.values("pk")
            .annotate(
                comments_count=Count(
                    "comments__id",
                    filter=Q(comments__text__icontains="foo"),
                )
            )
            .order_by("pk"),
            (
                {"pk": self.user_1.pk, "comments_count": 1},
                {"pk": self.user_2.pk, "comments_count": 1},
                {"pk": self.user_3.pk, "comments_count": 1},
            ),
        )

    def test_filter_and_count_users_by_comments_fields(self):
        users = User.objects.filter(comments__id__gt=2).order_by("pk")
        self.assertEqual(users.count(), 4)
        self.assertSequenceEqual(
            users, (self.user_1, self.user_3, self.user_3, self.user_3)
        )

        users = User.objects.filter(comments__text__icontains="foo").order_by("pk")
        self.assertEqual(users.count(), 3)
        self.assertSequenceEqual(users, (self.user_1, self.user_2, self.user_3))

        users = User.objects.filter(comments__text__icontains="baz").order_by("pk")
        self.assertEqual(users.count(), 3)
        self.assertSequenceEqual(users, (self.user_3, self.user_3, self.user_3))

    def test_order_by_comments_id_count(self):
        self.assertSequenceEqual(
            User.objects.annotate(comments_count=Count("comments__id")).order_by(
                "-comments_count"
            ),
            (self.user_3, self.user_1, self.user_2),
        )

    def test_max_pk(self):
        msg = "Max expression does not support composite primary keys."
        with self.assertRaisesMessage(ValueError, msg):
            Comment.objects.aggregate(Max("pk"))

    def test_first_from_unordered_queryset_aggregation_pk_selected(self):
        self.assertEqual(
            Comment.objects.values("pk").annotate(max=Max("id")).first(),
            {"pk": (self.comment_1.tenant_id, 1), "max": 1},
        )

    def test_first_from_unordered_queryset_aggregation_pk_selected_separately(self):
        self.assertEqual(
            Comment.objects.values("tenant", "id").annotate(max=Max("id")).first(),
            {"tenant": self.comment_1.tenant_id, "id": 1, "max": 1},
        )

    def test_first_from_unordered_queryset_aggregation_pk_incomplete(self):
        msg = (
            "Cannot use QuerySet.first() on an unordered queryset performing "
            "aggregation. Add an ordering with order_by()."
        )
        with self.assertRaisesMessage(TypeError, msg):
            Comment.objects.values("tenant").annotate(max=Max("id")).first()


class CompositePKCountDistinctTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.warehouse_1 = Warehouse.objects.create(id=1, name="w1")
        cls.warehouse_12 = Warehouse.objects.create(id=12, name="w12")
        # A warehouse without any products.
        cls.warehouse_3 = Warehouse.objects.create(id=3, name="w3")
        # The same sku in different warehouses identifies different products.
        cls.product_1 = Product.objects.create(
            warehouse=cls.warehouse_1, sku="sku-1", price=10
        )
        cls.product_2 = Product.objects.create(
            warehouse=cls.warehouse_12, sku="sku-1", price=20
        )
        # The (1, "2-5") and (12, "5") tuples would collide if the columns
        # were concatenated with a "-" separator ("1-2-5").
        cls.product_3 = Product.objects.create(
            warehouse=cls.warehouse_1, sku="2-5", price=30
        )
        cls.product_4 = Product.objects.create(
            warehouse=cls.warehouse_12, sku="5", price=40
        )
        cls.product_5 = Product.objects.create(
            warehouse=cls.warehouse_1, sku="a|b,c", price=50
        )
        for total in (1, 2, 3):
            Order.objects.create(warehouse=cls.warehouse_1, total=total)
        Order.objects.create(warehouse=cls.warehouse_12, total=10)

    def test_aggregate_count_distinct_pk(self):
        self.assertEqual(
            Product.objects.aggregate(n=Count("pk", distinct=True)),
            {"n": 5},
        )
        # Non-distinct counts keep counting the first column only.
        self.assertEqual(Product.objects.aggregate(n=Count("pk")), {"n": 5})

    def test_aggregate_count_distinct_pk_with_filter(self):
        self.assertEqual(
            Product.objects.aggregate(
                n=Count("pk", distinct=True, filter=Q(sku__contains="sku"))
            ),
            {"n": 2},
        )

    def test_aggregate_count_distinct_pk_empty(self):
        self.assertEqual(
            Product.objects.none().aggregate(n=Count("pk", distinct=True)),
            {"n": 0},
        )
        self.assertEqual(
            Product.objects.filter(sku="missing").aggregate(
                n=Count("pk", distinct=True)
            ),
            {"n": 0},
        )

    def test_aggregate_count_distinct_pk_uuid(self):
        tenant = Tenant.objects.create()
        Post.objects.create(tenant=tenant)
        Post.objects.create(tenant=tenant)
        self.assertEqual(
            Post.objects.aggregate(n=Count("pk", distinct=True)),
            {"n": 2},
        )

    def test_annotate_count_distinct_pk(self):
        warehouses = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True)
        ).order_by("pk")
        self.assertSequenceEqual(
            [(w.pk, w.product_count) for w in warehouses],
            [(1, 3), (3, 0), (12, 2)],
        )

    def test_annotate_count_distinct_pk_join_fanout(self):
        # Joining multiple multi-valued relations expands each product into
        # several rows; distinct counting must still count each product once.
        warehouses = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True),
            order_count=Count("orders__id", distinct=True),
        ).order_by("pk")
        self.assertSequenceEqual(
            [(w.pk, w.product_count, w.order_count) for w in warehouses],
            [(1, 3, 3), (3, 0, 0), (12, 2, 1)],
        )

    def test_annotate_count_distinct_pk_preserves_other_aggregates(self):
        # The distinct count must not alter the rows seen by other aggregates.
        def totals(queryset):
            return {w.pk: w.total for w in queryset.order_by("pk")}

        base = Warehouse.objects.annotate(
            product_count=Count("products__pk"),
            total=Sum("orders__total"),
        )
        distinct = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True),
            total=Sum("orders__total"),
        )
        self.assertEqual(totals(distinct), totals(base))

    def test_annotate_count_distinct_pk_filter(self):
        warehouses = Warehouse.objects.annotate(
            product_count=Count(
                "products__pk",
                distinct=True,
                filter=Q(products__sku__contains="sku"),
            )
        ).order_by("pk")
        self.assertSequenceEqual(
            [(w.pk, w.product_count) for w in warehouses],
            [(1, 1), (3, 0), (12, 1)],
        )

    def test_annotate_count_distinct_pk_having(self):
        warehouses = (
            Warehouse.objects.annotate(
                product_count=Count("products__pk", distinct=True)
            )
            .filter(product_count__gte=2)
            .order_by("pk")
        )
        self.assertSequenceEqual([w.pk for w in warehouses], [1, 12])

    def test_values_count_distinct_pk(self):
        self.assertSequenceEqual(
            Warehouse.objects.values("pk")
            .annotate(product_count=Count("products__pk", distinct=True))
            .order_by("pk"),
            [
                {"pk": 1, "product_count": 3},
                {"pk": 3, "product_count": 0},
                {"pk": 12, "product_count": 2},
            ],
        )

    def test_count_distinct_pk_separator_in_key(self):
        # (1, "2-5") and (12, "5") must count as two distinct products even
        # though concatenating the columns with a "-" separator would
        # produce "1-2-5" for both.
        self.assertEqual(
            Product.objects.filter(sku__in=["2-5", "5"]).aggregate(
                n=Count("pk", distinct=True)
            ),
            {"n": 2},
        )

    def test_count_distinct_pk_queryset_reuse(self):
        queryset = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True)
        ).order_by("pk")
        self.assertSequenceEqual([w.product_count for w in queryset], [3, 0, 2])
        # Reusing the queryset evaluates the same query again.
        self.assertSequenceEqual([w.product_count for w in queryset], [3, 0, 2])
        self.assertSequenceEqual(
            [w.product_count for w in queryset.filter(product_count__gte=1)],
            [3, 2],
        )

    def test_count_distinct_pk_sql(self):
        queryset = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True)
        )
        sql = str(queryset.query)
        if connection.vendor == "postgresql":
            self.assertIn("COUNT(DISTINCT (", sql)
        elif connection.vendor == "mysql":
            self.assertIn("COUNT(DISTINCT ", sql)
            self.assertNotIn("COUNT(DISTINCT (", sql)
        else:
            # e.g. SQLite emulates counting distinct tuples with JSON_ARRAY().
            self.assertIn("JSON_ARRAY", sql)
