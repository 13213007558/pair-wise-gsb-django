"""
Public API for common table expressions (CTEs), including recursive CTEs.

A CTE lets callers hand the database a starting query, a recursive term, and
a set of output columns, and get back a queryset that can be further
filtered, annotated, ordered, sliced, or embedded in a Subquery. All of the
work, including the recursion itself, is performed by the database.
"""

import copy
import itertools

from django.core.exceptions import FieldError
from django.db.models.expressions import Col
from django.db.models.fields import AutoField, Field, IntegerField
from django.db.models.query import QuerySet
from django.db.models.sql.constants import INNER, LOUTER

__all__ = ["CTE"]

_model_counter = itertools.count(1)


def _resolve_select_columns(query):
    """
    Return a list of (name, field) pairs describing the columns a query
    outputs, in SELECT order.
    """
    model = query.model
    columns = []
    selected = query.selected
    if selected is None:
        for field in model._meta.concrete_fields:
            columns.append((field.attname, field))
        for name, expression in query.annotation_select.items():
            columns.append((name, expression.output_field))
    else:
        for name in selected:
            if name in query.annotations:
                field = query.annotations[name].output_field
            elif name in query.extra_select:
                # The type of an extra() select cannot be determined.
                field = Field()
            else:
                field = model._meta.get_field(name)
            columns.append((name, field))
    return columns


def _make_output_field(name, field):
    """
    Return a standalone copy of "field" whose database column is "name",
    suitable for referencing a CTE output column.
    """
    if field.is_relation and field.concrete:
        # A foreign key is represented by its target column.
        field = field.target_field
    if isinstance(field, AutoField):
        # Avoid auto-generated fields; a model cannot have more than one.
        field = IntegerField()
    field = copy.copy(field)
    field.column = name
    # Discard any column reference cached against the original model's
    # table; it would otherwise leak a stale alias into compiled SQL.
    field.__dict__.pop("cached_col", None)
    return field


# Groups of field types that the database treats as interchangeable in the
# branches of a compound (UNION) query.
_COMPATIBLE_TYPES = ({"CharField", "TextField"},)


def _fields_compatible(lhs, rhs):
    lhs_type = lhs.get_internal_type()
    rhs_type = rhs.get_internal_type()
    if lhs_type == rhs_type:
        return True
    # A bare Field means the type could not be determined (e.g. extra()).
    if lhs_type == "Field" or rhs_type == "Field":
        return True
    return any(lhs_type in group and rhs_type in group for group in _COMPATIBLE_TYPES)


class CTEJoin:
    """
    A Join-compatible object (see sql.datastructures.Join) that joins a
    query against a CTE table with an explicit ON clause.
    """

    def __init__(self, table_name, parent_alias, on):
        self.table_name = table_name
        self.parent_alias = parent_alias
        self.table_alias = None
        self.join_type = INNER
        self.nullable = False
        self.filtered_relation = None
        # List of (lhs, rhs) expression pairs for the ON clause.
        self.on = tuple(on)

    def as_sql(self, compiler, connection):
        qn = compiler.quote_name_unless_alias
        conditions = []
        params = []
        for lhs, rhs in self.on:
            lhs_sql, lhs_params = compiler.compile(lhs)
            rhs_sql, rhs_params = compiler.compile(rhs)
            conditions.append("%s = %s" % (lhs_sql, rhs_sql))
            params.extend(lhs_params)
            params.extend(rhs_params)
        alias_str = (
            "" if self.table_alias == self.table_name else " %s" % self.table_alias
        )
        sql = "%s %s%s ON (%s)" % (
            self.join_type,
            qn(self.table_name),
            alias_str,
            " AND ".join(conditions),
        )
        return sql, params

    def relabeled_clone(self, change_map):
        clone = self.__class__(
            self.table_name,
            change_map.get(self.parent_alias, self.parent_alias),
            [
                (lhs.relabeled_clone(change_map), rhs.relabeled_clone(change_map))
                for lhs, rhs in self.on
            ],
        )
        clone.join_type = self.join_type
        clone.table_alias = change_map.get(self.table_alias, self.table_alias)
        return clone

    @property
    def identity(self):
        return (self.__class__, self.table_name, self.parent_alias, self.on)

    def __eq__(self, other):
        if not isinstance(other, CTEJoin):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self):
        return hash(self.identity)

    def demote(self):
        new = self.relabeled_clone({})
        new.join_type = INNER
        return new

    def promote(self):
        new = self.relabeled_clone({})
        new.join_type = LOUTER
        return new


class CTEColumns:
    """Provide expressions referencing the output columns of a CTE."""

    def __init__(self, cte):
        self.cte = cte

    def __getattr__(self, name):
        try:
            field = self.cte._output_column_map[name]
        except KeyError:
            raise AttributeError(
                "CTE '%s' has no output column named '%s'." % (self.cte.name, name)
            ) from None
        return Col(self.cte.name, field)

    def __getitem__(self, name):
        return getattr(self, name)


class CTE:
    """
    A common table expression.

    CTE(queryset, name) wraps any queryset so it can be attached to another
    query with QuerySet.with_cte() and joined with cte.join().

    CTE.recursive(base, recursive, ...) builds a recursive CTE from a
    non-recursive starting queryset and a recursive term. The recursive term
    is a callable that receives the CTE under construction (to reference its
    own output via cte.join() and cte.col) and returns a queryset.
    """

    UNION = "distinct"
    UNION_ALL = "all"

    def __init__(self, queryset, name="cte"):
        self.name = name
        self.recursive = False
        self._model = None
        self._queryset = queryset
        self.query = queryset.query
        self._set_output_columns(_resolve_select_columns(self.query))
        self._validate()

    @classmethod
    def recursive(cls, base, recursive, name="cte", union=UNION_ALL):
        """
        Build a recursive CTE. "base" is the non-recursive starting
        queryset. "recursive" is a callable taking the CTE and returning
        the recursive term queryset. "union" selects UNION (CTE.UNION) or
        UNION ALL (CTE.UNION_ALL) semantics; the caller is responsible for
        the termination condition of the recursion.
        """
        if union not in (cls.UNION, cls.UNION_ALL):
            raise ValueError(
                "union must be CTE.UNION or CTE.UNION_ALL, not %r." % (union,)
            )
        cte = cls.__new__(cls)
        cte.name = name
        cte.recursive = True
        cte._model = None
        cte._queryset = None
        cte.query = None
        cte._set_output_columns(_resolve_select_columns(base.query))
        recursive_qs = recursive(cte)
        combined = base.union(recursive_qs, all=union == cls.UNION_ALL)
        cte._queryset = combined
        cte.query = combined.query
        cte._validate()
        return cte

    def _set_output_columns(self, columns):
        self._output_columns = [
            (name, _make_output_field(name, field)) for name, field in columns
        ]
        self._output_column_map = dict(self._output_columns)

    @property
    def output_columns(self):
        return list(self._output_columns)

    @property
    def col(self):
        """Expressions referencing the output columns of this CTE."""
        return CTEColumns(self)

    def _validate(self):
        """
        Check that all branches of a compound CTE query output the same
        number of columns with compatible types. Raise before the query is
        executed rather than letting the database fail.
        """
        query = self.query
        if not query.combinator:
            return
        branches = [_resolve_select_columns(part) for part in query.combined_queries]
        reference = branches[0]
        for branch in branches[1:]:
            if len(branch) != len(reference):
                raise ValueError(
                    "CTE '%s' combines queries with a different number of "
                    "columns (%d and %d)."
                    % (self.name, len(reference), len(branch))
                )
            for (name, field), (_, other) in zip(reference, branch):
                if not _fields_compatible(field, other):
                    raise FieldError(
                        "CTE '%s' combines queries with incompatible types "
                        "for column '%s' (%s and %s)."
                        % (
                            self.name,
                            name,
                            field.get_internal_type(),
                            other.get_internal_type(),
                        )
                    )

    def join(self, model_or_queryset, **on):
        """
        Return a queryset on "model_or_queryset" joined against this CTE.
        Each keyword argument maps a field of the model to a cte.col
        expression, e.g. cte.join(Node, parent=cte.col.id).
        """
        if not on:
            raise ValueError("join() requires at least one join condition.")
        if isinstance(model_or_queryset, QuerySet):
            qs = model_or_queryset._chain()
        else:
            qs = QuerySet(model_or_queryset)
        query = qs.query
        parent_alias = query.get_initial_alias()
        conditions = []
        for field_name, rhs in on.items():
            if not isinstance(rhs, Col) or rhs.alias != self.name:
                raise TypeError(
                    "join() conditions must reference columns of CTE '%s', "
                    "e.g. cte.join(Model, field=cte.col.column)." % self.name
                )
            field = query.model._meta.get_field(field_name)
            conditions.append((Col(parent_alias, field), rhs))
        query.join(CTEJoin(self.name, parent_alias, conditions))
        if self.query is not None:
            # A fully defined CTE joined into another query must be attached
            # so its definition is emitted. While the CTE is still being
            # built (recursive self-reference) nothing is attached.
            query.add_cte(self)
        return qs

    def queryset(self):
        """Return a queryset reading from the output of this CTE."""
        if self._model is None:
            self._model = self._make_model()
        qs = QuerySet(model=self._model, using=self._queryset.db)
        return qs.with_cte(self)

    def _make_model(self):
        """
        Build an unmanaged model class mapping the output columns of this
        CTE to the CTE table name.
        """
        from django.db.models import Model

        attrs = {"__module__": __name__}
        for position, (name, field) in enumerate(self._output_columns):
            field = copy.deepcopy(field)
            # A primary key is required; the model is never saved.
            field.primary_key = position == 0
            attrs[name] = field
        attrs["Meta"] = type(
            "Meta",
            (),
            {"app_label": "cte", "db_table": self.name, "managed": False},
        )
        # The model name must be unique so that registering models for
        # same-named CTEs doesn't warn about re-registration.
        model_name = "CTE_%s_%d" % (self.name, next(_model_counter))
        return type(model_name, (Model,), attrs)
