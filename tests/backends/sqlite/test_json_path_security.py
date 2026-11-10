"""
Security tests for JSONField key handling on the SQLite backend.

JSONField keys may originate from user-submitted filter conditions. These
tests prove that special characters in a key (quotes, backslashes, dots,
SQL-looking fragments) cannot alter the structure of the generated query or
break out of the JSON path expression.

Path construction vs. parameter binding
----------------------------------------
* The JSON path itself is built by
  ``DatabaseOperations.compile_json_path()`` (in
  ``django/db/backends/base/operations.py``; SQLite only customizes numeric
  array indexing via ``format_json_path_numeric_index()`` in
  ``django/db/backends/sqlite3/operations.py``). Each non-integer key segment
  is serialized with ``json.dumps()`` and prefixed with a dot, so quotes and
  backslashes follow JSON escaping rules rather than SQL string rules.
* On SQLite the compiled path is never interpolated into the SQL text. Every
  entry point (``KeyTransform.as_sqlite()`` through ``_process_as_sqlite()``,
  ``HasKeyLookup.as_sqlite()`` with the ``JSON_TYPE(...)`` template, and
  ``KeyTransformIsNull.as_sqlite()``) appends the path to the parameter list
  and puts a ``%s`` placeholder next to ``JSON_TYPE``/``JSON_EXTRACT``. The
  database driver therefore binds the path as a single value; even a path
  containing quotes or SQL keywords is only data.

These tests inspect both the compiled SQL string and its bound parameters, and
then execute every query against the real (in-memory) test database. Checking
the number of escaped quotes alone would not prove that the row is still
reachable, so correct rows are always asserted.
"""

import json
import unittest

from django.core.exceptions import FieldError
from django.db import NotSupportedError, OperationalError, connection
from django.db.models.fields.json import KT, KeyTransform
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from ..models import SQLiteJSONPathModel


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite JSON path tests.")
class SQLiteJSONPathSecurityTests(TestCase):

    # Legal JSON object key names that would be dangerous if the key were ever
    # concatenated into SQL or interpreted as JSON path syntax.
    QUOTE_KEY = 'quote"key'
    BACKSLASH_KEY = "back\\slash"
    DOT_KEY = "dot.key"
    APOSTROPHE_KEY = "apo'strophe"
    SQL_LOOKING_KEY = "x') OR 1=1 OR ('d"
    PATH_LOOKING_KEY = '$.nested" OR "a"="a'
    TAB_KEY = "tab\tname"
    # A key whose name collides with built-in lookup/transform names.
    LOOKUP_COLLISION_KEY = "has_key"
    NESTED_QUOTE_KEY = 'q"q'
    # A key that looks numeric but is an object key, not an array index.
    NUMERIC_KEY = "123"

    @classmethod
    def setUpTestData(cls):
        cls.special_keys = [
            cls.QUOTE_KEY,
            cls.BACKSLASH_KEY,
            cls.DOT_KEY,
            cls.APOSTROPHE_KEY,
            cls.SQL_LOOKING_KEY,
            cls.PATH_LOOKING_KEY,
            cls.TAB_KEY,
            cls.LOOKUP_COLLISION_KEY,
            cls.NUMERIC_KEY,
        ]
        payload = {key: f"value-of-{key}" for key in cls.special_keys}
        payload["nested"] = {
            cls.NESTED_QUOTE_KEY: {
                cls.BACKSLASH_KEY: 3,
                "null_leaf": None,
            },
            "array": [{"id": 1}, {"id": 2}],
        }
        # JSON null must be distinguishable from a missing key.
        payload["json_null"] = None
        payload["present"] = 1

        cls.model = SQLiteJSONPathModel
        cls.target = cls.model.objects.create(data=payload)
        cls.other = cls.model.objects.create(data={"unrelated": True})

    def _compile(self, queryset):
        """Return (sql, params) as the ORM would send to the driver."""
        return queryset.query.sql_with_params()

    def _assert_path_is_bound_parameter(self, queryset, expected_paths):
        """
        Compile and execute ``queryset`` while asserting that every JSON path
        appears only as a bound parameter, never in the SQL text itself. The
        executed query captured by CaptureQueriesContext is the driver-facing
        form: SQLite's last_executed_query() re-quotes the bound params back
        in, so the path text may legitimately appear there, always wrapped in
        single quotes produced by the driver rather than by the ORM.
        """
        sql, params = self._compile(queryset)
        for path in expected_paths:
            self.assertIn(path, params)
            # The raw, unparameterized SQL sent to the driver contains
            # placeholders only; no JSON path text is interpolated into it.
            self.assertNotIn(path, sql)
        with CaptureQueriesContext(connection) as captured:
            list(queryset)
        self.assertEqual(len(captured), 1)
        return sql, params, captured[0]["sql"]

    def test_compile_json_path_escapes_special_key_segments(self):
        # compile_json_path() is the single place where key segments become
        # JSON path syntax. json.dumps() quotes each segment, which is what
        # neutralizes embedded quotes and backslashes.
        compile_path = connection.ops.compile_json_path
        cases = [
            ([self.QUOTE_KEY], '$.' + json.dumps(self.QUOTE_KEY)),
            ([self.BACKSLASH_KEY], '$.' + json.dumps(self.BACKSLASH_KEY)),
            ([self.DOT_KEY], '$.' + json.dumps(self.DOT_KEY)),
            ([self.SQL_LOOKING_KEY], '$.' + json.dumps(self.SQL_LOOKING_KEY)),
            (
                ["nested", self.NESTED_QUOTE_KEY, self.BACKSLASH_KEY],
                '$.%s.%s.%s'
                % (
                    json.dumps("nested"),
                    json.dumps(self.NESTED_QUOTE_KEY),
                    json.dumps(self.BACKSLASH_KEY),
                ),
            ),
        ]
        for transforms, expected in cases:
            with self.subTest(transforms=transforms):
                path = compile_path(transforms)
                self.assertEqual(path, expected)
                # Every segment is json.dumps() output, so an embedded quote
                # is always backslash-escaped and an embedded backslash is
                # doubled. An unescaped quote would terminate the path string.
                self.assertEqual(path.count('\\"'), expected.count('\\"'))
                self.assertEqual(path.count("\\\\"), expected.count("\\\\"))
        # include_root=False still escapes the segment (used by has_key).
        self.assertEqual(
            compile_path([self.QUOTE_KEY], include_root=False),
            "." + json.dumps(self.QUOTE_KEY),
        )

    def test_nested_key_transform_binds_path_and_fetches_row(self):
        queryset = self.model.objects.filter(
            **{
                "data__nested__%s__%s"
                % (self.NESTED_QUOTE_KEY, self.BACKSLASH_KEY): 3,
            }
        )
        # _process_as_sqlite() repeats the path three times (JSON_TYPE twice,
        # JSON_EXTRACT once), all as parameters.
        expected_path = connection.ops.compile_json_path(
            ["nested", self.NESTED_QUOTE_KEY, self.BACKSLASH_KEY]
        )
        sql, params, executed_sql = self._assert_path_is_bound_parameter(
            queryset, [expected_path]
        )
        self.assertEqual(params.count(expected_path), 3)
        self.assertEqual(sql.count("%s"), 4)
        # The SQL fragment stays structural; no key text leaks into it.
        self.assertNotIn(self.BACKSLASH_KEY, sql)
        self.assertIn("JSON_EXTRACT", executed_sql)
        self.assertSequenceEqual(queryset, [self.target])

    def test_nested_key_text_transform(self):
        # KeyTextTransform (KT) reaches the same _process_as_sqlite path.
        queryset = self.model.objects.annotate(
            leaf=KT(
                "data__nested__%s__%s"
                % (self.NESTED_QUOTE_KEY, self.BACKSLASH_KEY)
            )
        ).filter(leaf=3)
        expected_path = connection.ops.compile_json_path(
            ["nested", self.NESTED_QUOTE_KEY, self.BACKSLASH_KEY]
        )
        self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertSequenceEqual(queryset, [self.target])

    def test_has_key_with_each_special_key(self):
        for key in self.special_keys:
            with self.subTest(key=key):
                queryset = self.model.objects.filter(data__has_key=key)
                expected_path = "$" + "." + json.dumps(key)
                sql, params, _ = self._assert_path_is_bound_parameter(
                    queryset, [expected_path]
                )
                # HasKeyLookup.as_sqlite(): JSON_TYPE(col, %s) IS NOT NULL.
                self.assertIn("JSON_TYPE", sql)
                self.assertIn("IS NOT NULL", sql)
                self.assertSequenceEqual(queryset, [self.target])
        # The same key absent from the second row must not match it.
        self.assertFalse(
            self.model.objects.filter(data__has_key=self.QUOTE_KEY)
            .filter(pk=self.other.pk)
            .exists()
        )

    def test_has_key_nested_under_special_key(self):
        queryset = self.model.objects.filter(
            **{"data__nested__has_key": self.NESTED_QUOTE_KEY}
        )
        expected_path = connection.ops.compile_json_path(
            ["nested", self.NESTED_QUOTE_KEY]
        )
        self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertSequenceEqual(queryset, [self.target])

    def test_has_keys_combines_paths_with_and(self):
        keys = [
            self.QUOTE_KEY,
            self.BACKSLASH_KEY,
            self.DOT_KEY,
            self.SQL_LOOKING_KEY,
        ]
        queryset = self.model.objects.filter(data__has_keys=keys)
        expected_paths = ["$" + "." + json.dumps(key) for key in keys]
        sql, params, executed_sql = self._assert_path_is_bound_parameter(
            queryset, expected_paths
        )
        self.assertEqual(sql.count("JSON_TYPE"), len(keys))
        self.assertEqual(sql.count(" AND "), len(keys) - 1)
        self.assertNotIn(" OR ", sql)
        self.assertSequenceEqual(queryset, [self.target])
        # One missing key makes the whole AND fail.
        self.assertFalse(
            self.model.objects.filter(
                data__has_keys=[self.QUOTE_KEY, "missing"]
            ).exists()
        )

    def test_has_any_keys_combines_paths_with_or(self):
        keys = ["totally missing", self.APOSTROPHE_KEY, self.PATH_LOOKING_KEY]
        queryset = self.model.objects.filter(data__has_any_keys=keys)
        expected_paths = ["$" + "." + json.dumps(key) for key in keys]
        sql, params, _ = self._assert_path_is_bound_parameter(queryset, expected_paths)
        self.assertEqual(sql.count(" OR "), len(keys) - 1)
        self.assertNotIn(" AND ", sql)
        self.assertSequenceEqual(queryset, [self.target])

    def test_has_key_with_keytransform_rhs(self):
        # The KeyTransform branch of HasKeyLookup._as_sql_parts() builds the
        # path from preprocess_lhs(); exercise it with a special final key.
        queryset = self.model.objects.filter(
            data__has_key=KeyTransform(self.QUOTE_KEY, "data")
        )
        expected_path = "$" + "." + json.dumps(self.QUOTE_KEY)
        self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertSequenceEqual(queryset, [self.target])

    def test_key_named_like_a_lookup_is_treated_as_data(self):
        # "has_key" as a key name followed by an explicit real lookup must be
        # parsed as a KeyTransform, not as the has_key lookup.
        queryset = self.model.objects.filter(
            **{"data__%s__exact" % self.LOOKUP_COLLISION_KEY:
               "value-of-%s" % self.LOOKUP_COLLISION_KEY}
        )
        expected_path = connection.ops.compile_json_path([self.LOOKUP_COLLISION_KEY])
        sql, params, _ = self._assert_path_is_bound_parameter(queryset, [expected_path])
        # This is a value comparison (CASE/JSON_EXTRACT ... = ...), not a
        # JSON_TYPE(...) IS NOT NULL has_key predicate.
        self.assertIn("JSON_EXTRACT", sql)
        self.assertNotIn("IS NOT NULL", sql)
        self.assertSequenceEqual(queryset, [self.target])

        # The same name on the right-hand side of has_key is just a key.
        has_key_qs = self.model.objects.filter(
            data__has_key=self.LOOKUP_COLLISION_KEY
        )
        self.assertSequenceEqual(has_key_qs, [self.target])

    def test_numeric_object_key_is_not_treated_as_array_index(self):
        # HasKeyLookup compiles the final key with json.dumps() even when it is
        # numeric, so "123" stays an object key instead of becoming [123].
        queryset = self.model.objects.filter(data__has_key=self.NUMERIC_KEY)
        expected_path = '$."123"'
        sql, params, _ = self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertNotIn("[123]", sql)
        self.assertSequenceEqual(queryset, [self.target])

    def test_array_index_path_is_combined_with_special_keys(self):
        queryset = self.model.objects.filter(
            **{"data__nested__array__1__id": 2}
        )
        expected_path = '$.%s.%s[1].%s' % (
            json.dumps("nested"),
            json.dumps("array"),
            json.dumps("id"),
        )
        self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertSequenceEqual(queryset, [self.target])

    def test_json_null_differs_from_missing_key(self):
        present_null = self.model.objects.filter(
            data__json_null__isnull=False
        )
        missing = self.model.objects.filter(data__json_null__isnull=True)
        # KeyTransformIsNull.as_sqlite(): JSON_TYPE(...) IS [NOT] NULL, where
        # JSON_TYPE returns 'null' for a JSON null leaf but NULL for a missing
        # path. Both paths are still bound parameters.
        path = '$.' + json.dumps("json_null")
        self._assert_path_is_bound_parameter(present_null, [path])
        self.assertSequenceEqual(present_null, [self.target])
        self.assertSequenceEqual(missing, [self.other])
        # An actually missing key is NULL on both rows.
        self.assertCountEqual(
            self.model.objects.filter(data__no_such_key__isnull=True),
            [self.target, self.other],
        )
        self.assertFalse(
            self.model.objects.filter(data__no_such_key__isnull=False).exists()
        )
        # Exact match against JSON null (adapted as the 'null' datatype value)
        # finds only the row carrying the null leaf, never the row missing it.
        self.assertSequenceEqual(
            self.model.objects.filter(data__json_null=None), [self.target]
        )
        self.assertFalse(
            self.model.objects.filter(data__no_such_key=None).exists()
        )

    def test_malformed_path_bound_as_data_errors_at_execution(self):
        # A path that is syntactically invalid JSON-path syntax reaches SQLite
        # as a bound parameter and raises an operational error instead of being
        # parsed as SQL or silently matching something. This demonstrates the
        # database, not string quoting, interprets the path.
        with connection.cursor() as cursor:
            with self.assertRaises(OperationalError):
                cursor.execute(
                    "SELECT JSON_EXTRACT(?, ?)", ('{"a": 1}', '$."a" --')
                ).fetchall()
            # The same value correctly serialized resolves the key.
            cursor.execute("SELECT JSON_EXTRACT(?, ?)", ('{"a": 1}', "$.a"))
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_unsupported_contains_lookup_fails_explicitly(self):
        # SQLite sets supports_json_field_contains = False; DataContains must
        # raise NotSupportedError instead of silently returning an empty or
        # all-matching queryset.
        with self.assertRaises(NotSupportedError):
            list(self.model.objects.filter(data__contains={self.QUOTE_KEY: 1}))
        with self.assertRaises(NotSupportedError):
            list(
                self.model.objects.filter(
                    data__contained_by={self.QUOTE_KEY: 1}
                )
            )

    def test_invalid_lookup_fails_at_compilation(self):
        # A lookup name that cannot be resolved as either a registered lookup
        # or a key transform must fail loudly rather than being swallowed.
        with self.assertRaises(FieldError):
            self.model.objects.filter(data__a__gt__gt=1).exists()

    def test_unknown_trailing_name_is_a_bound_key_not_a_lookup(self):
        # JSONField.get_transform() turns any unregistered name into a
        # KeyTransform, so an "unknown" trailing name is a key. It must still
        # go through compile_json_path() and be bound as a parameter, and the
        # row carrying that exact key must be reachable.
        key = "not_a_real_lookup"
        self.model.objects.create(data={"nested": {key: 1}})
        queryset = self.model.objects.filter(**{"data__nested__%s" % key: 1})
        expected_path = connection.ops.compile_json_path(["nested", key])
        self._assert_path_is_bound_parameter(queryset, [expected_path])
        self.assertTrue(queryset.exists())

    def test_sql_looking_key_does_not_change_query_structure(self):
        # End-to-end guard: the most injection-shaped key still produces a
        # single-parameter JSON_TYPE predicate and only matches its own row.
        for key in (self.SQL_LOOKING_KEY, self.PATH_LOOKING_KEY):
            with self.subTest(key=key):
                queryset = self.model.objects.filter(data__has_key=key)
                sql, params = self._compile(queryset)
                table = self.model._meta.db_table
                self.assertEqual(
                    sql,
                    f'SELECT "{table}"."id", "{table}"."data" '
                    f'FROM "{table}" WHERE '
                    f'JSON_TYPE("{table}"."data", %s) IS NOT NULL',
                )
                self.assertEqual(params, ("$" + "." + json.dumps(key),))
                self.assertSequenceEqual(list(queryset), [self.target])
                # No row without that exact key can be reached.
                self.assertFalse(
                    self.model.objects.exclude(pk=self.target.pk)
                    .filter(data__has_key=key)
                    .exists()
                )

    def test_key_transform_exact_on_special_key_binds_value(self):
        key = self.QUOTE_KEY
        queryset = self.model.objects.filter(
            **{"data__%s" % key: "value-of-%s" % key}
        )
        expected_path = connection.ops.compile_json_path([key])
        sql, params, _ = self._assert_path_is_bound_parameter(queryset, [expected_path])
        # The comparison value is adapted through JSON_EXTRACT(%s, '$'), also
        # parameterized; nothing user-controlled is embedded in the SQL text.
        self.assertNotIn("value-of", sql)
        self.assertSequenceEqual(queryset, [self.target])
        # A tampered value must not match.
        self.assertFalse(
            self.model.objects.filter(**{"data__%s" % key: "wrong"}).exists()
        )
