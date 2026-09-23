from django.db import NotSupportedError
from django.db.models.expressions import Func
from django.db.models.fields import IntegerField

__all__ = ["Grouping"]


class Grouping(Func):
    """
    Return 1 when the given expression isn't part of the grouping set the
    current super-aggregate row was produced for, and 0 otherwise. This
    allows distinguishing NULL values present in the grouped data from NULL
    values generated for columns that aren't part of a grouping set.
    """

    function = "GROUPING"
    arity = 1
    # Grouping markers are computed from the grouping sets a row was
    # produced for; filters against them belong to the HAVING clause.
    contains_aggregate = True

    def __init__(self, expression, **extra):
        extra.setdefault("output_field", IntegerField())
        super().__init__(expression, **extra)

    def get_group_by_cols(self):
        # Grouping markers are computed from the grouping sets a row was
        # produced for; they must not be added to the GROUP BY clause.
        return []

    def as_sqlite(self, compiler, connection, **extra_context):
        if connection.features.supports_grouping_sets:
            return self.as_sql(compiler, connection, **extra_context)
        raise NotSupportedError(
            "Grouping() is only supported on this database as a top-level "
            "annotation of a grouping_sets() query."
        )
