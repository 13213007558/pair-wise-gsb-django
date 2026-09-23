from django.db import NotSupportedError
from django.db.models.expressions import Func
from django.db.models.fields import IntegerField


class Grouping(Func):
    """
    Return a bitmap telling which of the given grouping expressions are not
    part of the grouping set the current (super-)aggregate row was produced
    for. With a single argument the result is 1 when the expression is
    grouped away (its column is NULL because of grouping) and 0 when it
    isn't, which allows telling NULLs produced by GROUPING SETS apart from
    NULLs stored in the data. With multiple arguments the result is a bit
    vector where the leftmost argument is the most significant bit.
    """

    function = "GROUPING"
    output_field = IntegerField()
    # Filters referencing Grouping() must be evaluated against grouped rows
    # and thus belong to the HAVING clause.
    contains_aggregate = True

    def get_group_by_cols(self):
        return []

    def as_sql(self, compiler, connection):
        if hasattr(compiler, "_emulated_grouping_set"):
            grouping_set = compiler._emulated_grouping_set
            if grouping_set is None:
                # Placeholder compiled while building the SELECT clause of
                # the UNION ALL emulation. Every branch recompiles Grouping()
                # expressions against its own grouping set.
                return "NULL", ()
            value = 0
            for expression in self.get_source_expressions():
                value = (value << 1) | int(expression not in grouping_set)
            return str(value), ()
        if not connection.features.supports_grouping_sets:
            raise NotSupportedError(
                "GROUPING() is not supported on this database backend."
            )
        return super().as_sql(compiler, connection)
