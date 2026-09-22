import unittest

from django.core.exceptions import ImproperlyConfigured
from django.db.models import CTE, F, Subquery, Value
from django.db.models.functions import Concat
from django.test import SimpleTestCase, TestCase

from .models import Node

try:
    from django.db.backends.postgresql.base import (
        DatabaseWrapper as PostgreSQLDatabaseWrapper,
    )
except (ImportError, ImproperlyConfigured):
    PostgreSQLDatabaseWrapper = None


def descendants_cte(base, name="tree", union="all"):
    """
    Build a recursive CTE selecting the rows of the base queryset and all
    their descendants, with depth and /-separated path annotations.
    """

    def recursive(cte):
        return cte.join(Node, parent=cte.col.id).annotate(
            depth=cte.col.depth + 1,
            path=Concat(cte.col.path, Value("/"), F("name")),
        )

    return CTE.recursive(base=base, recursive=recursive, name=name, union=union)


def tree_cte(name="tree", union="all"):
    return descendants_cte(
        Node.objects.filter(parent__isnull=True).annotate(
            depth=Value(0), path=F("name")
        ),
        name=name,
        union=union,
    )


class RecursiveCTETests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A tree with duplicate node names on every level:
        #         root
        #       /      \
        #   branch     branch
        #   /    \        \
        # leaf  leaf     leaf
        cls.root = Node.objects.create(name="root")
        cls.b1 = Node.objects.create(name="branch", parent=cls.root)
        cls.b2 = Node.objects.create(name="branch", parent=cls.root)
        cls.l1 = Node.objects.create(name="leaf", parent=cls.b1)
        cls.l2 = Node.objects.create(name="leaf", parent=cls.b1)
        cls.l3 = Node.objects.create(name="leaf", parent=cls.b2)

    def test_descendants_and_depth(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte)
        # The whole traversal is performed by the database in one query.
        with self.assertNumQueries(1):
            result = sorted((node.name, node.depth) for node in qs)
        self.assertEqual(
            result,
            [
                ("branch", 1),
                ("branch", 1),
                ("leaf", 2),
                ("leaf", 2),
                ("leaf", 2),
                ("root", 0),
            ],
        )

    def test_annotate(self):
        cte = tree_cte()
        qs = (
            cte.queryset()
            .with_cte(cte)
            .annotate(double_depth=cte.col.depth * 2)
            .order_by("id")
        )
        with self.assertNumQueries(1):
            result = [(node.depth, node.double_depth) for node in qs]
        self.assertEqual(
            result,
            [(0, 0), (1, 2), (1, 2), (2, 4), (2, 4), (2, 4)],
        )

    def test_values_and_values_list(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte).filter(depth=1).order_by("id")
        self.assertEqual(
            list(qs.values_list("name", "depth")),
            [("branch", 1), ("branch", 1)],
        )
        self.assertEqual(
            list(qs.values("name", "path")),
            [
                {"name": "branch", "path": "root/branch"},
                {"name": "branch", "path": "root/branch"},
            ],
        )

    def test_depth_filter(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte).filter(depth=1)
        self.assertEqual([node.name for node in qs], ["branch", "branch"])
        qs = cte.queryset().with_cte(cte).filter(depth__gte=1)
        self.assertEqual(qs.count(), 5)

    def test_path_filtering(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte)
        # Same-named nodes are distinguishable through their paths.
        self.assertEqual(
            sorted(node.path for node in qs.filter(name="branch")),
            ["root/branch", "root/branch"],
        )
        self.assertEqual(qs.filter(path="root").count(), 1)
        self.assertEqual(qs.filter(path__endswith="/leaf").count(), 3)
        self.assertEqual(qs.filter(name="leaf", path="root/branch/leaf").count(), 3)

    def test_order_by_and_pagination(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte).order_by("path", "id")
        paths = [node.path for node in qs]
        self.assertEqual(
            paths,
            [
                "root",
                "root/branch",
                "root/branch",
                "root/branch/leaf",
                "root/branch/leaf",
                "root/branch/leaf",
            ],
        )
        self.assertEqual([node.path for node in qs[1:3]], paths[1:3])
        self.assertEqual([node.path for node in qs[3:]], paths[3:])
        self.assertEqual(qs[0].path, "root")

    def test_in_subquery(self):
        cte = tree_cte()
        subquery = cte.queryset().with_cte(cte).filter(depth=2).values("id")
        qs = Node.objects.filter(id__in=subquery)
        self.assertEqual({node.name for node in qs}, {"leaf"})
        self.assertEqual(qs.count(), 3)

    def test_scalar_subquery(self):
        cte = tree_cte()
        deepest = cte.queryset().with_cte(cte).order_by("-depth").values("depth")[:1]
        qs = Node.objects.filter(name="root").annotate(max_depth=Subquery(deepest))
        self.assertEqual(qs.get().max_depth, 2)

    def test_subquery_aliases_do_not_leak(self):
        # The outer query and the CTE body both select from the node table;
        # aliases of the subquery must not leak into the outer query.
        cte = tree_cte()
        qs = (
            Node.objects.filter(parent__isnull=False, children__isnull=False)
            .filter(pk__in=cte.queryset().with_cte(cte).values("id"))
            .distinct()
        )
        self.assertEqual([node.name for node in qs], ["branch", "branch"])
        sql = str(qs.query)
        self.assertIn("WITH RECURSIVE", sql)
        self.assertIn("IN (WITH RECURSIVE", sql)

    def test_shared_base_queryset_is_independent(self):
        base = Node.objects.filter(parent__isnull=True).annotate(
            depth=Value(0), path=F("name")
        )
        cte1 = descendants_cte(base, name="shared_one")
        cte2 = descendants_cte(base, name="shared_two")
        qs1 = cte1.queryset().with_cte(cte1).filter(name="leaf")
        qs2 = cte2.queryset().with_cte(cte2).filter(name="branch")
        self.assertEqual(qs1.count(), 3)
        self.assertEqual(qs2.count(), 2)
        # Evaluating one query doesn't affect the other.
        self.assertEqual([node.name for node in qs2], ["branch", "branch"])
        self.assertEqual([node.name for node in qs1], ["leaf", "leaf", "leaf"])
        # The shared base queryset is left untouched.
        self.assertEqual(base.query._with_ctes, ())
        self.assertEqual(base.count(), 1)

    def test_shared_cte_queryset_is_independent(self):
        cte = tree_cte()
        qs_leaves = cte.queryset().with_cte(cte).filter(name="leaf")
        qs_branches = cte.queryset().with_cte(cte).filter(name="branch")
        self.assertEqual(qs_leaves.count(), 3)
        self.assertEqual(qs_branches.count(), 2)
        self.assertEqual(qs_leaves.count(), 3)

    def test_multiple_ctes_dependency_order(self):
        cte1 = tree_cte(name="cte_one")
        # cte2 depends on cte1: its base term selects from cte1.
        cte2 = descendants_cte(
            cte1.queryset().filter(depth=0),
            name="cte_two",
        )
        # The CTEs are attached in the wrong order; the dependency order must
        # be restored when the SQL is generated.
        qs = cte2.queryset().with_cte(cte2, cte1)
        sql = str(qs.query)
        self.assertLess(sql.index('"cte_one"'), sql.index('"cte_two"'))
        self.assertEqual(qs.count(), 6)
        # Dependencies don't need to be attached explicitly.
        qs = cte2.queryset().with_cte(cte2)
        sql = str(qs.query)
        self.assertLess(sql.index('"cte_one"'), sql.index('"cte_two"'))
        self.assertEqual(qs.count(), 6)

    def test_multiple_ctes_subquery_dependency(self):
        cte1 = tree_cte(name="cte_sub_one")
        # cte2 depends on cte1 through a subquery in its base term.
        cte2 = descendants_cte(
            Node.objects.filter(
                pk__in=cte1.queryset().filter(depth=0).values("id")
            ).annotate(depth=Value(0), path=F("name")),
            name="cte_sub_two",
        )
        qs = cte2.queryset().with_cte(cte2)
        sql = str(qs.query)
        self.assertLess(sql.index('"cte_sub_one"'), sql.index('"cte_sub_two"'))
        self.assertEqual(qs.count(), 6)

    def test_cte_name_conflict(self):
        cte1 = tree_cte(name="conflicting")
        cte2 = tree_cte(name="conflicting")
        qs = cte1.queryset().with_cte(cte1, cte2)
        with self.assertRaisesMessage(ValueError, "multiple different CTEs"):
            list(qs)
        # Attaching the same CTE twice is fine.
        qs = cte1.queryset().with_cte(cte1, cte1)
        self.assertEqual(qs.count(), 6)

    def test_column_count_mismatch(self):
        with self.assertRaisesMessage(ValueError, "same number of columns"):
            CTE.recursive(
                base=Node.objects.all(),
                recursive=lambda cte: cte.join(Node, parent=cte.col.id).values("id"),
            )

    def test_column_type_mismatch(self):
        with self.assertRaisesMessage(ValueError, "incompatible types"):
            CTE.recursive(
                base=Node.objects.all().annotate(tag=Value(0)),
                recursive=lambda cte: cte.join(Node, parent=cte.col.id).annotate(
                    tag=F("name")
                ),
            )

    def test_invalid_union(self):
        with self.assertRaisesMessage(ValueError, "union must be"):
            tree_cte(union="bogus")

    def test_with_cte_invalid_argument(self):
        with self.assertRaisesMessage(TypeError, "with_cte"):
            Node.objects.with_cte(object())


class RecursiveCTECycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A cycle: ca -> cb -> cc -> ca (following the parent links).
        cls.a = Node.objects.create(name="ca")
        cls.b = Node.objects.create(name="cb", parent=cls.a)
        cls.c = Node.objects.create(name="cc", parent=cls.b)
        cls.a.parent = cls.c
        cls.a.save()

    def test_union_all_with_termination_condition(self):
        # UNION ALL keeps duplicates; the recursion only stops because the
        # caller expressed a termination condition (depth < 4).
        cte = CTE.recursive(
            base=Node.objects.filter(pk=self.a.pk).annotate(depth=Value(0)),
            recursive=lambda cte: cte.join(Node, parent=cte.col.id)
            .annotate(depth=cte.col.depth + 1)
            .filter(depth__lt=4),
            union="all",
            name="cycle",
        )
        qs = cte.queryset().with_cte(cte).order_by("depth")
        self.assertEqual(
            [(node.name, node.depth) for node in qs],
            [("ca", 0), ("cb", 1), ("cc", 2), ("ca", 3)],
        )

    def test_union_all_values_with_termination_condition(self):
        cte = CTE.recursive(
            base=Node.objects.filter(pk=self.a.pk)
            .annotate(depth=Value(0))
            .values("id", "depth"),
            recursive=lambda cte: cte.join(Node, parent=cte.col.id)
            .annotate(depth=cte.col.depth + 1)
            .filter(depth__lt=4)
            .values("id", "depth"),
            union="all",
            name="cycle_values",
        )
        qs = cte.queryset().with_cte(cte).order_by("depth")
        self.assertEqual(
            [row["id"] for row in qs],
            [self.a.pk, self.b.pk, self.c.pk, self.a.pk],
        )

    def test_union_distinct_terminates_on_cycle(self):
        # UNION (distinct) deduplicates the rows, so the recursion terminates
        # on its own once every node has been produced; no termination
        # condition is needed.
        cte = CTE.recursive(
            base=Node.objects.filter(pk=self.a.pk).values("id"),
            recursive=lambda cte: cte.join(Node, parent=cte.col.id).values("id"),
            union="distinct",
            name="cycle_distinct",
        )
        qs = cte.queryset().with_cte(cte)
        self.assertEqual(
            sorted(row["id"] for row in qs),
            sorted([self.a.pk, self.b.pk, self.c.pk]),
        )


@unittest.skipUnless(PostgreSQLDatabaseWrapper, "psycopg isn't installed")
class PostgreSQLCompileTests(SimpleTestCase):
    databases = {"default"}

    @staticmethod
    def pg_connection():
        return PostgreSQLDatabaseWrapper(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "dummy",
                "USER": "",
                "PASSWORD": "",
                "HOST": "",
                "PORT": "",
                "CONN_MAX_AGE": 0,
                "CONN_HEALTH_CHECKS": False,
                "OPTIONS": {},
                "AUTOCOMMIT": True,
                "ATOMIC_REQUESTS": False,
                "TIME_ZONE": None,
            },
            "postgresql",
        )

    def compile(self, qs):
        return qs.query.get_compiler(connection=self.pg_connection()).as_sql()

    def test_recursive_cte_compilation(self):
        cte = tree_cte()
        qs = cte.queryset().with_cte(cte).filter(depth__lte=2).order_by("name")[:5]
        sql, params = self.compile(qs)
        self.assertTrue(sql.startswith("WITH RECURSIVE"))
        self.assertIn(
            '"tree" ("id", "name", "parent_id", "depth", "path") AS', sql
        )
        self.assertIn("UNION ALL", sql)
        self.assertIn("INNER JOIN", sql)
        self.assertIn("LIMIT", sql)
        # The CTE body params come before the outer query params.
        self.assertEqual(params[-1], 2)

    def test_union_distinct_compilation(self):
        cte = CTE.recursive(
            base=Node.objects.values("id"),
            recursive=lambda cte: cte.join(Node, parent=cte.col.id).values("id"),
            union="distinct",
            name="pg_distinct",
        )
        sql, _ = self.compile(cte.queryset().with_cte(cte))
        self.assertIn("UNION", sql)
        self.assertNotIn("UNION ALL", sql)

    def test_dependency_order_compilation(self):
        cte1 = tree_cte(name="pg_one")
        cte2 = descendants_cte(cte1.queryset().filter(depth=0), name="pg_two")
        sql, _ = self.compile(cte2.queryset().with_cte(cte2, cte1))
        self.assertLess(sql.index('"pg_one"'), sql.index('"pg_two"'))
