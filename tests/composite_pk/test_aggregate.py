from unittest import mock

from django.db.models import Count, Max, Q, Sum
from django.test import TestCase

from .models import Comment, Product, Shipment, Tenant, User, Warehouse


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
            Count("comments", distinct=True)
        ).order_by("pk")

        self.assertEqual(user_1.comments__count, 2)
        self.assertEqual(user_2.comments__count, 1)
        self.assertEqual(user_3.comments__count, 3)

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
        cls.sku = "same|sku,1"
        cls.warehouse_1 = Warehouse.objects.create(name="warehouse 1")
        cls.warehouse_2 = Warehouse.objects.create(name="warehouse 2")
        cls.warehouse_3 = Warehouse.objects.create(name="warehouse 3")
        cls.product_1 = Product.objects.create(
            warehouse=cls.warehouse_1,
            sku=cls.sku,
            quantity=10,
        )
        cls.product_2 = Product.objects.create(
            warehouse=cls.warehouse_2,
            sku=cls.sku,
            quantity=20,
        )
        Shipment.objects.bulk_create(
            Shipment(warehouse=cls.warehouse_1, quantity=quantity)
            for quantity in (1, 2, 3)
        )

    def test_aggregate_count_distinct_pk(self):
        self.assertEqual(
            Product.objects.aggregate(product_count=Count("pk", distinct=True)),
            {"product_count": 2},
        )

    def test_aggregate_count_distinct_related_pk(self):
        self.assertEqual(
            Warehouse.objects.aggregate(
                product_count=Count("products", distinct=True),
                shipment_total=Sum("shipments__quantity"),
            ),
            {"product_count": 2, "shipment_total": 6},
        )

    def test_annotate_count_distinct_related_pk(self):
        warehouses = Warehouse.objects.annotate(
            product_count=Count("products", distinct=True),
            shipment_total=Sum("shipments__quantity"),
        ).order_by("name")

        self.assertSequenceEqual(
            [
                (warehouse.name, warehouse.product_count, warehouse.shipment_total)
                for warehouse in warehouses
            ],
            [
                ("warehouse 1", 1, 6),
                ("warehouse 2", 1, None),
                ("warehouse 3", 0, None),
            ],
        )

    def test_count_distinct_related_pk_filter(self):
        self.assertEqual(
            Warehouse.objects.aggregate(
                product_count=Count(
                    "products",
                    distinct=True,
                    filter=Q(products__sku=self.sku),
                ),
                shipment_total=Sum(
                    "shipments__quantity",
                    filter=Q(shipments__quantity__gt=1),
                ),
            ),
            {"product_count": 2, "shipment_total": 5},
        )

    def test_count_distinct_related_pk_no_results(self):
        self.assertEqual(
            Warehouse.objects.filter(name="missing").aggregate(
                product_count=Count("products", distinct=True)
            ),
            {"product_count": 0},
        )

    def test_count_distinct_related_pk_having(self):
        warehouses = (
            Warehouse.objects.annotate(product_count=Count("products", distinct=True))
            .filter(product_count=1)
            .order_by("name")
        )
        self.assertSequenceEqual(
            warehouses.values_list("name", flat=True),
            ["warehouse 1", "warehouse 2"],
        )

    def test_count_distinct_related_pk_outer_grouping(self):
        self.assertSequenceEqual(
            Warehouse.objects.values("name")
            .annotate(product_count=Count("products", distinct=True))
            .order_by("name"),
            [
                {"name": "warehouse 1", "product_count": 1},
                {"name": "warehouse 2", "product_count": 1},
                {"name": "warehouse 3", "product_count": 0},
            ],
        )

    def test_count_distinct_related_pk_queryset_reuse(self):
        warehouses = Warehouse.objects.annotate(
            product_count=Count("products__pk", distinct=True)
        )
        self.assertEqual(
            warehouses.get(name="warehouse 1").product_count,
            1,
        )
        self.assertEqual(
            warehouses.get(name="warehouse 3").product_count,
            0,
        )

    def test_count_distinct_related_pk_postgresql_sql(self):
        warehouses = Warehouse.objects.annotate(
            product_count=Count("products", distinct=True)
        )
        count = warehouses.query.annotations["product_count"]
        compiler = warehouses.query.get_compiler("default")
        connection = mock.Mock(vendor="postgresql")
        connection.ops.quote_name.side_effect = lambda name: f'"{name}"'
        compiler.connection = connection

        sql, params = compiler.compile(count)

        self.assertEqual(params, [])
        self.assertEqual(
            sql,
            'COUNT(DISTINCT ROW("composite_pk_product"."warehouse_id", '
            '"composite_pk_product"."sku"))',
        )
