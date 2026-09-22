"""
Public API for expressing recursive common table expressions (CTEs) with the
ORM, e.g. to evaluate hierarchical or graph traversals in the database
instead of loading rows into Python and filtering them there.
"""

from itertools import count

from django.core.exceptions import FieldError
from django.db.models.expressions import Expression
from django.db.models.sql.constants import INNER
from django.db.models.sql.datastructures import BaseTable, Join
from django.db.models.sql.query import Query
from django.db.models.sql.where import WhereNode

__all__ = ["CTE"]


class CTERef(Expression):
    """An expression resolving to an output column of a CTE."""

    def __init__(self, cte, column_name, output_field=None):
        super().__init__(output_field=output_field)
        self.cte = cte
        self.column_name = column_name

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.cte.name, self.column_name)

    def _resolve_alias(self, query):
        for alias, join in query.alias_map.items():
            if getattr(join, "table_name", None) == self.cte.name:
                return alias
        if isinstance(query, CTEQuery) and query.cte is self.cte:
            # The CTE is the base table of the query. Its alias (the CTE
            # name) is only assigned when the FROM clause is built, which may
            # not have happened yet.
            return self.cte.name
        raise FieldError(
            "Cannot resolve reference to column %r of CTE %r: the query "
            "doesn't select from or join that CTE." % (self.column_name, self.cte.name)
        )

    def as_sql(self, compiler, connection):
        qn = compiler.quote_name_unless_alias
        qn2 = connection.ops.quote_name
        alias = self._resolve_alias(compiler.query)
        return "%s.%s" % (qn(alias), qn2(self.column_name)), []


class CTEJoin(Join):
    """Join a query against a CTE, e.g. for the recursive term of a CTE."""

    def __init__(
        self,
        cte,
        parent_alias,
        table_alias,
        join_conditions,
        join_type=INNER,
        nullable=False,
    ):
        # Join.__init__() requires a join field, but the join conditions of a
        # CTE join are plain column pairs, so set the attributes directly.
        self.cte = cte
        self.table_name = cte.name
        self.parent_alias = parent_alias
        self.table_alias = table_alias
        self.join_type = join_type
        self.join_field = None
        self.join_fields = None
        self.join_cols = ()
        self.nullable = nullable
        self.filtered_relation = None
        # [(cte_column_name, model_column_name), ...]
        self.join_conditions = tuple(join_conditions)

    def as_sql(self, compiler, connection):
        qn = compiler.quote_name_unless_alias
        qn2 = connection.ops.quote_name
        on_clause_sql = " AND ".join(
            "%s.%s = %s.%s"
            % (
                qn(self.table_alias),
                qn2(cte_column),
                qn(self.parent_alias),
                qn2(model_column),
            )
            for cte_column, model_column in self.join_conditions
        )
        alias_str = (
            "" if self.table_alias == self.table_name else " %s" % self.table_alias
        )
        sql = "%s %s%s ON (%s)" % (
            self.join_type,
            qn(self.table_name),
            alias_str,
            on_clause_sql,
        )
        return sql, []

    def relabeled_clone(self, change_map):
        return self.__class__(
            self.cte,
            change_map.get(self.parent_alias, self.parent_alias),
            change_map.get(self.table_alias, self.table_alias),
            self.join_conditions,
            self.join_type,
            self.nullable,
        )

    @property
    def identity(self):
        return (
            self.__class__,
            self.table_name,
            self.parent_alias,
            self.join_conditions,
        )


class CTEQuery(Query):
    """A query selecting from a CTE rather than from a model table."""

    def __init__(self, model, cte, **kwargs):
        self.cte = cte
        super().__init__(model, **kwargs)
        concrete_field_names = {
            name
            for field in model._meta.concrete_fields
            for name in (field.name, field.attname)
        }
        # Output columns of the CTE that don't map to a model field (e.g.
        # annotations of the base term) are made available as annotations so
        # they can be filtered, ordered, and selected. When the CTE is built
        # from a values() term, all columns are exposed as annotations since
        # their names may differ from the model column names.
        shadow_model_fields = cte.is_values_query
        for column_name, output_field in cte.output_columns:
            if shadow_model_fields or column_name not in concrete_field_names:
                self.add_annotation(CTERef(cte, column_name, output_field), column_name)

    def get_initial_alias(self):
        if self.alias_map:
            alias = self.base_table
            self.ref_alias(alias)
        else:
            alias = self.join(BaseTable(self.cte.name, None))
        return alias


class CTEColumnAccessor:
    """Build expressions referencing the output columns of a CTE (cte.col)."""

    def __init__(self, cte):
        self._cte = cte

    def __getattr__(self, name):
        try:
            output_field = self._cte._output_fields[name]
        except KeyError:
            raise AttributeError(
                "CTE %r has no output column %r." % (self._cte.name, name)
            ) from None
        return CTERef(self._cte, name, output_field)


class CTE:
    """
    A recursive common table expression.

    The base term is any queryset. The recursive term is a callable that
    receives the CTE and returns a queryset; it references the CTE's own
    output through CTE.join() and cte.col. The caller controls deduplication
    with union="all" (UNION ALL) or union="distinct" (UNION) and expresses
    any termination condition (e.g. a depth limit) in the recursive term
    itself; nothing is deduplicated or limited implicitly.
    """

    _name_counter = count(1)

    def __init__(self, base, recursive, name=None, union="all"):
        if union not in ("all", "distinct"):
            raise ValueError("union must be 'all' or 'distinct', got %r." % (union,))
        if not callable(recursive):
            raise TypeError(
                "The recursive term of a CTE must be a callable accepting the "
                "CTE and returning a queryset."
            )
        self.union_all = union == "all"
        self.base = base
        self.model = base.model
        if name is None:
            name = "cte_%s_%d" % (self.model._meta.db_table, next(self._name_counter))
        self.name = name
        self._output_columns = _get_select_columns(base)
        self._output_fields = dict(self._output_columns)
        self.col = CTEColumnAccessor(self)
        recursive_qs = recursive(self)
        if not isinstance(getattr(recursive_qs, "query", None), Query):
            raise TypeError(
                "The recursive term of a CTE must return a queryset, got %r."
                % (recursive_qs,)
            )
        self._check_columns(_get_select_columns(recursive_qs))
        self.recursive = recursive_qs
        self.combined = base.union(recursive_qs, all=self.union_all)

    @classmethod
    def recursive(cls, base, recursive, name=None, union="all"):
        """Create a recursive CTE from a base queryset and a recursive term."""
        return cls(base, recursive, name=name, union=union)

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.name)

    @property
    def is_values_query(self):
        return bool(self.base.query.values_select)

    @property
    def output_columns(self):
        """((column_name, output_field), ...) of the CTE."""
        return tuple(self._output_columns)

    @property
    def column_names(self):
        return tuple(name for name, _ in self._output_columns)

    def _check_columns(self, recursive_columns):
        base_columns = self._output_columns
        if len(base_columns) != len(recursive_columns):
            raise ValueError(
                "The base and recursive terms of CTE %r must select the same "
                "number of columns (%d != %d)."
                % (self.name, len(base_columns), len(recursive_columns))
            )
        for pos, columns in enumerate(zip(base_columns, recursive_columns), start=1):
            (base_name, base_field), (_, recursive_field) = columns
            if base_field is None or recursive_field is None:
                continue
            if not (
                isinstance(base_field, type(recursive_field))
                or isinstance(recursive_field, type(base_field))
            ):
                raise ValueError(
                    "The base and recursive terms of CTE %r select "
                    "incompatible types for column %d (%r): %s and %s."
                    % (
                        self.name,
                        pos,
                        base_name,
                        base_field.__class__.__name__,
                        recursive_field.__class__.__name__,
                    )
                )

    def queryset(self):
        """Return a queryset selecting from this CTE."""
        from django.db.models.query import QuerySet

        queryset = QuerySet(
            model=self.model, query=CTEQuery(self.model, self), using=self.base._db
        )
        if self.is_values_query:
            queryset = queryset.values(*self.column_names)
        return queryset

    def join(self, model_or_queryset, **on):
        """
        Return a queryset on the given model (or a clone of the given
        queryset) joined against this CTE. Each keyword argument maps a field
        of the model to a CTE output column, e.g.
        cte.join(Node, parent=cte.col.id).
        """
        if isinstance(model_or_queryset, type):
            queryset = model_or_queryset._default_manager.all()
        else:
            queryset = model_or_queryset.all()
        if not on:
            raise ValueError("CTE.join() requires at least one join condition.")
        query = queryset.query
        if query.alias_map:
            parent_alias = query.base_table
        else:
            parent_alias = query.get_initial_alias()
        conditions = []
        for lookup, cte_ref in on.items():
            if not isinstance(cte_ref, CTERef) or cte_ref.cte is not self:
                raise ValueError(
                    "CTE.join() conditions must reference output columns of "
                    "the CTE via cte.col, got %r." % (cte_ref,)
                )
            field = query.model._meta.get_field(lookup)
            conditions.append((cte_ref.column_name, field.column))
        query.join(CTEJoin(self, parent_alias, None, conditions))
        return queryset

    def get_dependencies(self):
        """Return the CTEs referenced by the base and recursive terms."""
        dependencies = {}
        for cte in _iter_cte_references(self.combined.query):
            if cte is not self:
                dependencies.setdefault(cte.name, cte)
        return tuple(dependencies.values())

    def _as_sql(self, connection):
        compiler = self.combined.query.get_compiler(connection=connection)
        body_sql, body_params = compiler.as_sql()
        qn = connection.ops.quote_name
        columns = ", ".join(qn(name) for name in self.column_names)
        return "%s (%s) AS (%s)" % (qn(self.name), columns, body_sql), body_params


def _get_select_columns(queryset):
    """Return [(column_name, output_field), ...] selected by the queryset."""
    compiler = queryset.query.get_compiler(using=queryset.db)
    columns = []
    for expression, _, alias in compiler.get_select()[0]:
        if alias is not None:
            name = alias
        else:
            name = getattr(getattr(expression, "target", None), "column", None)
            if name is None:
                raise ValueError(
                    "Cannot determine a CTE column name for %r; use "
                    "annotate() or values() to name the column." % (expression,)
                )
        columns.append((name, expression.output_field))
    return columns


def _iter_cte_references(query, _seen_queries=None):
    """Yield the CTE objects referenced by the query (joins and subqueries)."""
    if _seen_queries is None:
        _seen_queries = set()
    if id(query) in _seen_queries:
        return
    _seen_queries.add(id(query))
    cte = getattr(query, "cte", None)
    if cte is not None:
        yield cte
    for join in query.alias_map.values():
        cte = getattr(join, "cte", None)
        if cte is not None:
            yield cte
    yield from _iter_cte_refs_in_node(query.where, _seen_queries)
    for annotation in query.annotations.values():
        yield from _iter_cte_refs_in_expression(annotation, _seen_queries)
    for combined_query in query.combined_queries or ():
        yield from _iter_cte_references(combined_query, _seen_queries)


def _iter_cte_refs_in_node(node, _seen_queries):
    for child in node.children:
        if isinstance(child, WhereNode):
            yield from _iter_cte_refs_in_node(child, _seen_queries)
        else:
            for attribute in ("lhs", "rhs"):
                yield from _iter_cte_refs_in_object(
                    getattr(child, attribute, None), _seen_queries
                )


def _iter_cte_refs_in_object(obj, _seen_queries):
    if isinstance(obj, Query):
        yield from _iter_cte_references(obj, _seen_queries)
    elif isinstance(obj, Expression):
        yield from _iter_cte_refs_in_expression(obj, _seen_queries)


def _iter_cte_refs_in_expression(expression, _seen_queries):
    query = getattr(expression, "query", None)
    if isinstance(query, Query):
        yield from _iter_cte_references(query, _seen_queries)
    for source in expression.get_source_expressions():
        if source is not None and source is not query:
            yield from _iter_cte_refs_in_object(source, _seen_queries)


def resolve_cte_order(ctes):
    """
    Order CTEs so that each CTE appears after the CTEs it depends on, and
    detect conflicting names and circular dependencies.
    """
    ordered = []
    by_name = {}
    visiting = set()

    def visit(cte):
        existing = by_name.get(cte.name)
        if existing is not None:
            if existing is not cte:
                raise ValueError(
                    "CTE name %r is used by multiple different CTEs." % cte.name
                )
            return
        if cte.name in visiting:
            raise ValueError(
                "Cannot order CTEs: %r is part of a circular dependency." % cte.name
            )
        visiting.add(cte.name)
        for dependency in cte.get_dependencies():
            visit(dependency)
        visiting.discard(cte.name)
        by_name[cte.name] = cte
        ordered.append(cte)

    for cte in ctes:
        visit(cte)
    return ordered
