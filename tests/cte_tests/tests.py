from django.core.exceptions import FieldError
from django.db import connection
from django.db.backends.base.features import BaseDatabaseFeatures
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.models import CTE, F, IntegerField, Subquery, TextField, Value
from django.db.models.functions import Cast, Concat, Length
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from .models import Edge, GraphNode, Node


def descendants_cte(start, name="tree", union=CTE.UNION_ALL):
    """A recursive CTE selecting the start nodes and all their descendants."""
    base = start.annotate(
        depth=Value(0, output_field=IntegerField()),
        path=Cast("name", output_field=TextField()),
    ).values("id", "name", "parent_id", "depth", "path")

    def recursive(cte):
        return (
            cte.join(Node, parent_id=cte.col.id)
            .annotate(
                depth=cte.col.depth + 1,
                path=Concat(
                    cte.col.path, Value("/"), "name", output_field=TextField()
                ),
            )
            .values("id", "name", "parent_id", "depth", "path")
        )

    return CTE.recursive(base, recursive, name=name, union=union)


class RecursiveCTETreeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A tree with several nodes sharing the same name:
        # root
        # +-- a            (a1)
        # |   +-- b        (b1)
        # |   |   +-- a    (a2)
        # |   +-- a        (a3)
        # +-- b            (b2)
        #     +-- a        (a4)
        cls.root = Node.objects.create(name="root")
        cls.a1 = Node.objects.create(name="a", parent=cls.root)
        cls.b1 = Node.objects.create(name="b", parent=cls.a1)
        cls.a2 = Node.objects.create(name="a", parent=cls.b1)
        cls.a3 = Node.objects.create(name="a", parent=cls.a1)
        cls.b2 = Node.objects.create(name="b", parent=cls.root)
        cls.a4 = Node.objects.create(name="a", parent=cls.b2)

    def test_descendants_are_fetched_by_the_database(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        qs = cte.queryset().filter(depth__gte=1)
        with CaptureQueriesContext(connection) as captured:
            rows = list(qs)
        # The whole traversal is a single query; nothing is filtered in
        # Python.
        self.assertEqual(len(captured), 1)
        self.assertIn("WITH RECURSIVE", captured[0]["sql"])
        self.assertCountEqual(
            [row.name for row in rows], ["a", "a", "a", "a", "b", "b"]
        )

    def test_depth_and_path(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        rows = list(cte.queryset())
        by_path = {row.path: row for row in rows}
        self.assertEqual(len(rows), 7)
        expected_depths = {
            "root": 0,
            "root/a": 1,
            "root/a/b": 2,
            "root/a/b/a": 3,
            "root/a/a": 2,
            "root/b": 1,
            "root/b/a": 2,
        }
        for path, depth in expected_depths.items():
            self.assertEqual(by_path[path].depth, depth, path)
        # Nodes sharing a name are told apart by their path.
        self.assertEqual(
            {row.name for row in rows},
            {"root", "a", "b"},
        )
        self.assertNotEqual(by_path["root/a"].id, by_path["root/b/a"].id)
        self.assertEqual(by_path["root/a/b/a"].parent_id, by_path["root/a/b"].id)

    def test_path_filter(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        rows = list(
            cte.queryset().filter(name="a", path__startswith="root/a/").order_by("path")
        )
        self.assertEqual([row.path for row in rows], ["root/a/a", "root/a/b/a"])

    def test_pagination(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        qs = cte.queryset().order_by("path")
        self.assertEqual(
            [row.path for row in qs[:3]], ["root", "root/a", "root/a/a"]
        )
        self.assertEqual(
            [row.path for row in qs[3:5]], ["root/a/b", "root/a/b/a"]
        )

    def test_annotate_filter_order_by_on_cte_queryset(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        rows = list(
            cte.queryset()
            .annotate(name_length=Length("name"))
            .filter(name_length=1, depth=2)
            .order_by("path")
        )
        self.assertEqual(
            [row.path for row in rows], ["root/a/a", "root/a/b", "root/b/a"]
        )

    def test_cte_queryset_in_subquery(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        inner = cte.queryset().filter(depth=2).values("id")
        qs = Node.objects.filter(pk__in=inner)
        self.assertCountEqual([node.name for node in qs], ["a", "a", "b"])

    def test_cte_queryset_in_subquery_expression(self):
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        qs = Node.objects.filter(pk=self.root.pk).annotate(
            deepest=Subquery(cte.queryset().order_by("-depth").values("depth")[:1])
        )
        self.assertEqual(qs.get().deepest, 3)

    def test_subquery_aliases_do_not_leak(self):
        # The outer query, a plain subquery, and the CTE body all read the
        # same table; their aliases must not interfere.
        inner = Node.objects.filter(name="a")
        cte = descendants_cte(Node.objects.filter(pk=self.root.pk))
        qs = Node.objects.filter(pk__in=inner.values("pk")).filter(
            pk__in=cte.queryset().values("id")
        )
        self.assertEqual(len(list(qs)), 4)
        self.assertEqual({node.name for node in qs}, {"a"})

    def test_reused_queryset_builds_independent_queries(self):
        base = Node.objects.filter(name="root")
        cte1 = descendants_cte(base, name="tree_one")
        cte2 = descendants_cte(base, name="tree_two")
        qs1 = cte1.queryset().filter(depth=0)
        qs2 = cte2.queryset().filter(depth=1)
        # Attaching a CTE to one queryset must not leak into the other.
        self.assertEqual([cte.name for cte in qs1.query._ctes], ["tree_one"])
        self.assertEqual([cte.name for cte in qs2.query._ctes], ["tree_two"])
        sql1, params1 = qs1.query.sql_with_params()
        sql2, params2 = qs2.query.sql_with_params()
        self.assertIn('"tree_one"', sql1)
        self.assertNotIn('"tree_two"', sql1)
        self.assertIn('"tree_two"', sql2)
        self.assertEqual(params1.count("root"), 1)
        self.assertEqual(params2.count("root"), 1)
        self.assertEqual(len(list(qs1)), 1)
        self.assertEqual(len(list(qs2)), 2)
        # The reused queryset itself is unchanged.
        self.assertEqual(list(base), [self.root])

    def test_multiple_ctes_are_emitted_in_dependency_order(self):
        first = CTE(Node.objects.filter(name="b"), name="first_cte")
        second = CTE(
            first.join(Node, parent_id=first.col.id).values("id", "name", "parent_id"),
            name="second_cte",
        )
        qs = second.queryset()
        sql, _ = qs.query.sql_with_params()
        self.assertLess(sql.index('"first_cte"'), sql.index('"second_cte"'))
        self.assertNotIn("RECURSIVE", sql)
        rows = list(qs.order_by("id"))
        # Children of the "b" nodes.
        self.assertEqual([row.id for row in rows], [self.a2.id, self.a4.id])

    def test_conflicting_cte_names_raise_before_execution(self):
        cte_a = CTE(Node.objects.filter(name="a"), name="dup")
        cte_b = CTE(Node.objects.filter(name="b"), name="dup")
        with self.assertRaisesMessage(ValueError, "already attached"):
            Node.objects.all().with_cte(cte_a).with_cte(cte_b)
        # Attaching the same CTE twice is a no-op.
        qs = Node.objects.all().with_cte(cte_a).with_cte(cte_a)
        self.assertEqual(len(qs.query._ctes), 1)

    def test_column_count_mismatch_raises_before_execution(self):
        base = Node.objects.filter(pk=self.root.pk).values("id", "name")

        def recursive(cte):
            return cte.join(Node, parent_id=cte.col.id).values("id")

        with self.assertRaisesMessage(ValueError, "different number of columns"):
            CTE.recursive(base, recursive)

    def test_column_type_mismatch_raises_before_execution(self):
        base = (
            Node.objects.filter(pk=self.root.pk)
            .annotate(depth=Value(0, output_field=IntegerField()))
            .values("id", "depth")
        )

        def recursive(cte):
            return (
                cte.join(Node, parent_id=cte.col.id)
                .annotate(depth=Cast("name", output_field=TextField()))
                .values("id", "depth")
            )

        with self.assertRaisesMessage(FieldError, "incompatible types"):
            CTE.recursive(base, recursive)

    def test_invalid_union_option(self):
        base = Node.objects.filter(pk=self.root.pk).values("id")

        def recursive(cte):
            return cte.join(Node, parent_id=cte.col.id).values("id")

        with self.assertRaisesMessage(ValueError, "union must be"):
            CTE.recursive(base, recursive, union="bogus")


class RecursiveCTEGraphTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A cycle: a -> b -> c -> a.
        cls.a = GraphNode.objects.create(name="a")
        cls.b = GraphNode.objects.create(name="b")
        cls.c = GraphNode.objects.create(name="c")
        Edge.objects.create(from_node=cls.a, to_node=cls.b)
        Edge.objects.create(from_node=cls.b, to_node=cls.c)
        Edge.objects.create(from_node=cls.c, to_node=cls.a)

    def make_reachability_cte(self, union, max_depth):
        base = (
            GraphNode.objects.filter(pk=self.a.pk)
            .annotate(node_id=F("id"), depth=Value(0, output_field=IntegerField()))
            .values("node_id", "depth")
        )

        def recursive(cte):
            qs = cte.join(Edge, from_node_id=cte.col.node_id).annotate(
                node_id=F("to_node_id"),
                depth=cte.col.depth + 1,
            )
            if max_depth is not None:
                # The termination condition is expressed by the caller.
                qs = qs.filter(depth__lt=max_depth)
            return qs.values("node_id", "depth")

        return CTE.recursive(base, recursive, name="reach", union=union)

    def make_distinct_reachability_cte(self):
        base = GraphNode.objects.filter(pk=self.a.pk).annotate(
            node_id=F("id")
        ).values("node_id")

        def recursive(cte):
            return cte.join(Edge, from_node_id=cte.col.node_id).annotate(
                node_id=F("to_node_id")
            ).values("node_id")

        return CTE.recursive(base, recursive, name="reach", union=CTE.UNION)

    def test_union_all_revisits_nodes_on_cycles(self):
        # UNION ALL does not deduplicate, so the caller must bound the
        # recursion; nodes on the cycle appear multiple times.
        cte = self.make_reachability_cte(CTE.UNION_ALL, max_depth=5)
        sql, _ = cte.queryset().query.sql_with_params()
        self.assertIn("UNION ALL", sql)
        rows = list(cte.queryset().order_by("depth"))
        self.assertEqual([row.depth for row in rows], [0, 1, 2, 3, 4])
        self.assertEqual(
            [row.node_id for row in rows],
            [self.a.pk, self.b.pk, self.c.pk, self.a.pk, self.b.pk],
        )

    def test_union_terminates_on_cycles(self):
        # UNION removes duplicate rows, so the recursion terminates on its
        # own once every node has been reached.
        cte = self.make_distinct_reachability_cte()
        sql, _ = cte.queryset().query.sql_with_params()
        self.assertNotIn("UNION ALL", sql)
        rows = list(cte.queryset())
        self.assertCountEqual(
            [row.node_id for row in rows], [self.a.pk, self.b.pk, self.c.pk]
        )


class PostgreSQLLikeOperations(BaseDatabaseOperations):
    # The PostgreSQL backend uses the generic compiler for SELECT queries;
    # point there so compiling doesn't require psycopg or a server.
    compiler_module = "django.db.models.sql.compiler"

    def quote_name(self, name):
        return '"%s"' % name.replace('"', '""')


class PostgreSQLLikeFeatures(BaseDatabaseFeatures):
    supports_slicing_ordering_in_compound = True


class PostgreSQLLikeConnection:
    """A minimal stand-in for a PostgreSQL connection for compile tests."""

    vendor = "postgresql"
    alias = "default"
    data_types = {"TextField": "text"}
    operators = {
        "exact": "= %s",
        "iexact": "= UPPER(%s)",
        "contains": "LIKE %s",
        "icontains": "LIKE UPPER(%s)",
        "gt": "> %s",
        "gte": ">= %s",
        "lt": "< %s",
        "lte": "<= %s",
        "startswith": "LIKE %s",
        "endswith": "LIKE %s",
        "istartswith": "LIKE UPPER(%s)",
        "iendswith": "LIKE UPPER(%s)",
    }

    def __init__(self):
        self.ops = PostgreSQLLikeOperations(self)
        self.features = PostgreSQLLikeFeatures(self)


def compile_for_postgresql(qs):
    compiler = qs.query.get_compiler(connection=PostgreSQLLikeConnection())
    return compiler.as_sql()


class PostgreSQLCompileTests(SimpleTestCase):
    def test_recursive_cte_sql(self):
        cte = descendants_cte(Node.objects.filter(name="root"))
        qs = cte.queryset().filter(depth__lte=2).order_by("path")[:5]
        sql, params = compile_for_postgresql(qs)
        self.assertTrue(
            sql.startswith(
                'WITH RECURSIVE "tree" ("id", "name", "parent_id", "depth", '
                '"path") AS ('
            ),
            sql,
        )
        self.assertIn("UNION ALL", sql)
        self.assertIn('FROM "tree"', sql)
        self.assertIn("ORDER BY", sql)
        self.assertIn("LIMIT", sql)
        # Parameters of the CTE body come before the outer query's.
        self.assertEqual(tuple(params[:2]), (0, "root"))
        self.assertEqual(params[-1], 2)

    def test_union_distinct_sql(self):
        cte = descendants_cte(Node.objects.filter(name="root"), union=CTE.UNION)
        sql, _ = compile_for_postgresql(cte.queryset())
        self.assertIn("WITH RECURSIVE", sql)
        self.assertIn(" UNION ", sql)
        self.assertNotIn("UNION ALL", sql)

    def test_non_recursive_cte_sql(self):
        cte = CTE(Node.objects.filter(name="b"), name="bs")
        sql, _ = compile_for_postgresql(cte.queryset())
        self.assertTrue(
            sql.startswith('WITH "bs" ("id", "name", "parent_id") AS ('), sql
        )
        self.assertNotIn("RECURSIVE", sql)

    def test_cte_in_subquery_sql(self):
        cte = descendants_cte(Node.objects.filter(name="root"))
        qs = Node.objects.filter(pk__in=cte.queryset().values("id"))
        sql, _ = compile_for_postgresql(qs)
        self.assertIn("IN (WITH RECURSIVE", sql)
