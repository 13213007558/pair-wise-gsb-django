from collections import Counter, defaultdict
from functools import partial, reduce
from itertools import chain
from operator import attrgetter, or_

from django.db import IntegrityError, connections, models, transaction
from django.db.models import query_utils, signals, sql


class ProtectedError(IntegrityError):
    def __init__(self, msg, protected_objects):
        self.protected_objects = protected_objects
        super().__init__(msg, protected_objects)


class RestrictedError(IntegrityError):
    def __init__(self, msg, restricted_objects):
        self.restricted_objects = restricted_objects
        super().__init__(msg, restricted_objects)


def CASCADE(collector, field, sub_objs, using):
    collector.collect(
        sub_objs,
        source=field.remote_field.model,
        source_attr=field.name,
        nullable=field.null,
        fail_on_restricted=False,
    )
    if field.null and not connections[using].features.can_defer_constraint_checks:
        collector.add_field_update(field, None, sub_objs)


def PROTECT(collector, field, sub_objs, using):
    raise ProtectedError(
        "Cannot delete some instances of model '%s' because they are "
        "referenced through a protected foreign key: '%s.%s'"
        % (
            field.remote_field.model.__name__,
            sub_objs[0].__class__.__name__,
            field.name,
        ),
        sub_objs,
    )


def RESTRICT(collector, field, sub_objs, using):
    collector.add_restricted_objects(field, sub_objs)
    collector.add_dependency(field.remote_field.model, field.model)


def SET(value):
    if callable(value):

        def set_on_delete(collector, field, sub_objs, using):
            collector.add_field_update(field, value(), sub_objs)

    else:

        def set_on_delete(collector, field, sub_objs, using):
            collector.add_field_update(field, value, sub_objs)

        set_on_delete.lazy_sub_objs = True

    set_on_delete.deconstruct = lambda: ("django.db.models.SET", (value,), {})
    return set_on_delete


def SET_NULL(collector, field, sub_objs, using):
    collector.add_field_update(field, None, sub_objs)


SET_NULL.lazy_sub_objs = True


def SET_DEFAULT(collector, field, sub_objs, using):
    collector.add_field_update(field, field.get_default(), sub_objs)


def DO_NOTHING(collector, field, sub_objs, using):
    pass


def get_candidate_relations_to_delete(opts):
    # The candidate relations are the ones that come from N-1 and 1-1 relations.
    # N-N  (i.e., many-to-many) relations aren't candidates for deletion.
    return (
        f
        for f in opts.get_fields(include_hidden=True)
        if f.auto_created and not f.concrete and (f.one_to_one or f.one_to_many)
    )


class Collector:
    def __init__(self, using, origin=None):
        self.using = using
        # A Model or QuerySet object.
        self.origin = origin
        # Initially, {model: {instances}}, later values become lists.
        self.data = defaultdict(set)
        # {(field, value): [instances, …]}
        self.field_updates = defaultdict(list)
        # {model: {field: {instances}}}
        self.restricted_objects = defaultdict(partial(defaultdict, set))
        # fast_deletes is a list of queryset-likes that can be deleted without
        # fetching the objects into memory.
        self.fast_deletes = []

        # Tracks deletion-order dependency for databases without transactions
        # or ability to defer constraint checks. Only concrete model classes
        # should be included, as the dependencies exist only between actual
        # database tables; proxy models are represented here by their concrete
        # parent.
        self.dependencies = defaultdict(set)  # {model: {models}}

    def add(self, objs, source=None, nullable=False, reverse_dependency=False):
        """
        Add 'objs' to the collection of objects to be deleted.  If the call is
        the result of a cascade, 'source' should be the model that caused it,
        and 'nullable' should be set to True if the relation can be null.

        Return a list of all objects that were not already collected.
        """
        if not objs:
            return []
        new_objs = []
        model = objs[0].__class__
        instances = self.data[model]
        for obj in objs:
            if obj not in instances:
                new_objs.append(obj)
        instances.update(new_objs)
        # Nullable relationships can be ignored -- they are nulled out before
        # deleting, and therefore do not affect the order in which objects have
        # to be deleted.
        if source is not None and not nullable:
            self.add_dependency(source, model, reverse_dependency=reverse_dependency)
        return new_objs

    def add_dependency(self, model, dependency, reverse_dependency=False):
        if reverse_dependency:
            model, dependency = dependency, model
        self.dependencies[model._meta.concrete_model].add(
            dependency._meta.concrete_model
        )
        self.data.setdefault(dependency, self.data.default_factory())

    def add_field_update(self, field, value, objs):
        """
        Schedule a field update. 'objs' must be a homogeneous iterable
        collection of model instances (e.g. a QuerySet).
        """
        self.field_updates[field, value].append(objs)

    def add_restricted_objects(self, field, objs):
        if objs:
            model = objs[0].__class__
            self.restricted_objects[model][field].update(objs)

    def clear_restricted_objects_from_set(self, model, objs):
        if model in self.restricted_objects:
            self.restricted_objects[model] = {
                field: items - objs
                for field, items in self.restricted_objects[model].items()
            }

    def clear_restricted_objects_from_queryset(self, model, qs):
        if model in self.restricted_objects:
            objs = set(
                qs.filter(
                    pk__in=[
                        obj.pk
                        for objs in self.restricted_objects[model].values()
                        for obj in objs
                    ]
                )
            )
            self.clear_restricted_objects_from_set(model, objs)

    def _has_signal_listeners(self, model):
        return signals.pre_delete.has_listeners(
            model
        ) or signals.post_delete.has_listeners(model)

    def can_fast_delete(self, objs, from_field=None):
        """
        Determine if the objects in the given queryset-like or single object
        can be fast-deleted. This can be done if there are no cascades, no
        parents and no signal listeners for the object class.

        The 'from_field' tells where we are coming from - we need this to
        determine if the objects are in fact to be deleted. Allow also
        skipping parent -> child -> parent chain preventing fast delete of
        the child.
        """
        if from_field and from_field.remote_field.on_delete is not CASCADE:
            return False
        if hasattr(objs, "_meta"):
            model = objs._meta.model
        elif hasattr(objs, "model") and hasattr(objs, "_raw_delete"):
            model = objs.model
        else:
            return False
        if self._has_signal_listeners(model):
            return False
        # The use of from_field comes from the need to avoid cascade back to
        # parent when parent delete is cascading to child.
        opts = model._meta
        return (
            all(
                link == from_field
                for link in opts.concrete_model._meta.parents.values()
            )
            and
            # Foreign keys pointing to this model.
            all(
                related.field.remote_field.on_delete is DO_NOTHING
                for related in get_candidate_relations_to_delete(opts)
            )
            and (
                # Something like generic foreign key.
                not any(
                    hasattr(field, "bulk_related_objects")
                    for field in opts.private_fields
                )
            )
        )

    def get_del_batches(self, objs, fields):
        """
        Return the objs in suitably sized batches for the used connection.
        """
        conn_batch_size = max(
            connections[self.using].ops.bulk_batch_size(fields, objs), 1
        )
        if len(objs) > conn_batch_size:
            return [
                objs[i : i + conn_batch_size]
                for i in range(0, len(objs), conn_batch_size)
            ]
        else:
            return [objs]

    def collect(
        self,
        objs,
        source=None,
        nullable=False,
        collect_related=True,
        source_attr=None,
        reverse_dependency=False,
        keep_parents=False,
        fail_on_restricted=True,
    ):
        """
        Add 'objs' to the collection of objects to be deleted as well as all
        parent instances.  'objs' must be a homogeneous iterable collection of
        model instances (e.g. a QuerySet).  If 'collect_related' is True,
        related objects will be handled by their respective on_delete handler.

        If the call is the result of a cascade, 'source' should be the model
        that caused it and 'nullable' should be set to True, if the relation
        can be null.

        If 'reverse_dependency' is True, 'source' will be deleted before the
        current model, rather than after. (Needed for cascading to parent
        models, the one case in which the cascade follows the forwards
        direction of an FK rather than the reverse direction.)

        If 'keep_parents' is True, data of parent model's will be not deleted.

        If 'fail_on_restricted' is False, error won't be raised even if it's
        prohibited to delete such objects due to RESTRICT, that defers
        restricted object checking in recursive calls where the top-level call
        may need to collect more objects to determine whether restricted ones
        can be deleted.
        """
        if self.can_fast_delete(objs):
            self.fast_deletes.append(objs)
            return
        new_objs = self.add(
            objs, source, nullable, reverse_dependency=reverse_dependency
        )
        if not new_objs:
            return

        model = new_objs[0].__class__

        if not keep_parents:
            # Recursively collect concrete model's parent models, but not their
            # related objects. These will be found by meta.get_fields()
            concrete_model = model._meta.concrete_model
            for ptr in concrete_model._meta.parents.values():
                if ptr:
                    parent_objs = [getattr(obj, ptr.name) for obj in new_objs]
                    self.collect(
                        parent_objs,
                        source=model,
                        source_attr=ptr.remote_field.related_name,
                        collect_related=False,
                        reverse_dependency=True,
                        fail_on_restricted=False,
                    )
        if not collect_related:
            return

        model_fast_deletes = defaultdict(list)
        protected_objects = defaultdict(list)
        for related in get_candidate_relations_to_delete(model._meta):
            # Preserve parent reverse relationships if keep_parents=True.
            if keep_parents and related.model in model._meta.all_parents:
                continue
            field = related.field
            on_delete = field.remote_field.on_delete
            if on_delete == DO_NOTHING:
                continue
            related_model = related.related_model
            if self.can_fast_delete(related_model, from_field=field):
                model_fast_deletes[related_model].append(field)
                continue
            batches = self.get_del_batches(new_objs, [field])
            for batch in batches:
                sub_objs = self.related_objects(related_model, [field], batch)
                # Non-referenced fields can be deferred if no signal receivers
                # are connected for the related model as they'll never be
                # exposed to the user. Skip field deferring when some
                # relationships are select_related as interactions between both
                # features are hard to get right. This should only happen in
                # the rare cases where .related_objects is overridden anyway.
                if not (
                    sub_objs.query.select_related
                    or self._has_signal_listeners(related_model)
                ):
                    referenced_fields = set(
                        chain.from_iterable(
                            (rf.attname for rf in rel.field.foreign_related_fields)
                            for rel in get_candidate_relations_to_delete(
                                related_model._meta
                            )
                        )
                    )
                    sub_objs = sub_objs.only(*tuple(referenced_fields))
                if getattr(on_delete, "lazy_sub_objs", False) or sub_objs:
                    try:
                        self.execute_on_delete(on_delete, field, sub_objs)
                    except ProtectedError as error:
                        key = "'%s.%s'" % (field.model.__name__, field.name)
                        protected_objects[key] += error.protected_objects
        if protected_objects:
            raise ProtectedError(
                "Cannot delete some instances of model %r because they are "
                "referenced through protected foreign keys: %s."
                % (
                    model.__name__,
                    ", ".join(protected_objects),
                ),
                set(chain.from_iterable(protected_objects.values())),
            )
        for related_model, related_fields in model_fast_deletes.items():
            batches = self.get_del_batches(new_objs, related_fields)
            for batch in batches:
                sub_objs = self.related_objects(related_model, related_fields, batch)
                self.fast_deletes.append(sub_objs)
        for field in model._meta.private_fields:
            if hasattr(field, "bulk_related_objects"):
                # It's something like generic foreign key.
                sub_objs = field.bulk_related_objects(new_objs, self.using)
                self.collect(
                    sub_objs, source=model, nullable=True, fail_on_restricted=False
                )

        if fail_on_restricted:
            # Raise an error if collected restricted objects (RESTRICT) aren't
            # candidates for deletion also collected via CASCADE.
            for related_model, instances in self.data.items():
                self.clear_restricted_objects_from_set(related_model, instances)
            for qs in self.fast_deletes:
                self.clear_restricted_objects_from_queryset(qs.model, qs)
            if self.restricted_objects.values():
                restricted_objects = defaultdict(list)
                for related_model, fields in self.restricted_objects.items():
                    for field, objs in fields.items():
                        if objs:
                            key = "'%s.%s'" % (related_model.__name__, field.name)
                            restricted_objects[key] += objs
                if restricted_objects:
                    raise RestrictedError(
                        "Cannot delete some instances of model %r because "
                        "they are referenced through restricted foreign keys: "
                        "%s."
                        % (
                            model.__name__,
                            ", ".join(restricted_objects),
                        ),
                        set(chain.from_iterable(restricted_objects.values())),
                    )

    def related_objects(self, related_model, related_fields, objs):
        """
        Get a QuerySet of the related model to objs via related fields.
        """
        predicate = query_utils.Q.create(
            [(f"{related_field.name}__in", objs) for related_field in related_fields],
            connector=query_utils.Q.OR,
        )
        return related_model._base_manager.using(self.using).filter(predicate)

    def execute_on_delete(self, on_delete, field, sub_objs):
        """
        Run the on_delete handler for a related field. Extracted so that
        subclasses (e.g. DeletionPreview) can intercept the dispatch.
        """
        on_delete(self, field, sub_objs, self.using)

    def instances_with_model(self):
        for model, instances in self.data.items():
            for obj in instances:
                yield model, obj

    def sort(self):
        sorted_models = []
        concrete_models = set()
        models = list(self.data)
        while len(sorted_models) < len(models):
            found = False
            for model in models:
                if model in sorted_models:
                    continue
                dependencies = self.dependencies.get(model._meta.concrete_model)
                if not (dependencies and dependencies.difference(concrete_models)):
                    sorted_models.append(model)
                    concrete_models.add(model._meta.concrete_model)
                    found = True
            if not found:
                return
        self.data = {model: self.data[model] for model in sorted_models}

    def delete(self):
        # sort instance collections
        for model, instances in self.data.items():
            self.data[model] = sorted(instances, key=attrgetter("pk"))

        # if possible, bring the models in an order suitable for databases that
        # don't support transactions or cannot defer constraint checks until the
        # end of a transaction.
        self.sort()
        # number of objects deleted for each model label
        deleted_counter = Counter()

        # Optimize for the case with a single obj and no dependencies
        if len(self.data) == 1 and len(instances) == 1:
            instance = list(instances)[0]
            if self.can_fast_delete(instance):
                with transaction.mark_for_rollback_on_error(self.using):
                    count = sql.DeleteQuery(model).delete_batch(
                        [instance.pk], self.using
                    )
                setattr(instance, model._meta.pk.attname, None)
                return count, {model._meta.label: count}

        with transaction.atomic(using=self.using, savepoint=False):
            # send pre_delete signals
            for model, obj in self.instances_with_model():
                if not model._meta.auto_created:
                    signals.pre_delete.send(
                        sender=model,
                        instance=obj,
                        using=self.using,
                        origin=self.origin,
                    )

            # fast deletes
            for qs in self.fast_deletes:
                count = qs._raw_delete(using=self.using)
                if count:
                    deleted_counter[qs.model._meta.label] += count

            # update fields
            for (field, value), instances_list in self.field_updates.items():
                updates = []
                objs = []
                for instances in instances_list:
                    if (
                        isinstance(instances, models.QuerySet)
                        and instances._result_cache is None
                    ):
                        updates.append(instances)
                    else:
                        objs.extend(instances)
                if updates:
                    combined_updates = reduce(or_, updates)
                    combined_updates.update(**{field.name: value})
                if objs:
                    model = objs[0].__class__
                    query = sql.UpdateQuery(model)
                    query.update_batch(
                        list({obj.pk for obj in objs}), {field.name: value}, self.using
                    )

            # reverse instance collections
            for instances in self.data.values():
                instances.reverse()

            # delete instances
            for model, instances in self.data.items():
                query = sql.DeleteQuery(model)
                pk_list = [obj.pk for obj in instances]
                count = query.delete_batch(pk_list, self.using)
                if count:
                    deleted_counter[model._meta.label] += count

                if not model._meta.auto_created:
                    for obj in instances:
                        signals.post_delete.send(
                            sender=model,
                            instance=obj,
                            using=self.using,
                            origin=self.origin,
                        )

        for model, instances in self.data.items():
            for instance in instances:
                setattr(instance, model._meta.pk.attname, None)
        return sum(deleted_counter.values()), dict(deleted_counter)


def _json_safe(value):
    """
    Return value unchanged if it's natively JSON serializable, otherwise
    return its string representation.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _set_closure_value(on_delete):
    """
    If on_delete is a SET() closure, return (True, wrapped_value). Otherwise
    return (False, None). The deconstruct() lambda attached by SET() only
    builds a tuple, so calling it has no side effects.
    """
    deconstruct = getattr(on_delete, "deconstruct", None)
    if deconstruct is None:
        return False, None
    try:
        path, args, kwargs = deconstruct()
    except Exception:
        return False, None
    if path != "django.db.models.SET" or len(args) != 1:
        return False, None
    return True, args[0]


class DeletionPreview(Collector):
    """
    Collector subclass that computes what a deletion would do without
    performing it. Only SELECT queries are issued; no delete signals are
    sent, no delete() methods are called, and no writes are executed.

    on_delete handlers whose effect can't be determined without running
    user code (custom handlers, SET() with a callable, SET_DEFAULT with a
    callable field default) aren't executed; they're reported as
    unanalyzable instead.

    No locks are taken, so the actual deletion must be checked again when
    it's performed.
    """

    def __init__(self, using, origin=None):
        super().__init__(using, origin=origin)
        # {(model, pk): [{"via": ..., "on_delete": ...}, ...]}
        self.reasons = defaultdict(list)
        # [{"model": ..., "pk": ..., "via": ..., "on_delete": ...}]
        self.blocked = []
        # [{"model": ..., "field": ..., "handler": ..., "pks": ..., "reason": ...}]
        self.unanalyzable = []
        self._pending_reason = None

    def can_fast_delete(self, objs, from_field=None):
        # Every object must be examined for the preview, so nothing may be
        # fast-deleted (which would skip collection entirely).
        return False

    def collect(
        self,
        objs,
        source=None,
        nullable=False,
        collect_related=True,
        source_attr=None,
        reverse_dependency=False,
        keep_parents=False,
        fail_on_restricted=True,
    ):
        if source is not None and reverse_dependency:
            reason = {
                "via": "%s (parent link)" % source._meta.label,
                "on_delete": "CASCADE",
            }
        elif self._pending_reason is not None:
            reason = self._pending_reason
        elif source is None:
            reason = {"via": None, "on_delete": "ROOT"}
        else:
            via = source._meta.label
            if source_attr:
                via = "%s.%s" % (via, source_attr)
            reason = {"via": via, "on_delete": "CASCADE"}
        for obj in objs:
            obj_reasons = self.reasons[obj.__class__, obj.pk]
            if reason not in obj_reasons:
                obj_reasons.append(reason)
        return super().collect(
            objs,
            source=source,
            nullable=nullable,
            collect_related=collect_related,
            source_attr=source_attr,
            reverse_dependency=reverse_dependency,
            keep_parents=keep_parents,
            fail_on_restricted=fail_on_restricted,
        )

    def execute_on_delete(self, on_delete, field, sub_objs):
        via = "%s.%s" % (field.model._meta.label, field.name)
        if on_delete is SET_DEFAULT and callable(field.default):
            self._record_unanalyzable(
                on_delete, field, sub_objs, "SET_DEFAULT with a callable default"
            )
            return
        if on_delete in (CASCADE, PROTECT, RESTRICT, SET_NULL, SET_DEFAULT):
            previous_reason = self._pending_reason
            self._pending_reason = {"via": via, "on_delete": on_delete.__name__}
            try:
                on_delete(self, field, sub_objs, self.using)
            except ProtectedError as error:
                for obj in error.protected_objects:
                    self.blocked.append(
                        {
                            "model": obj._meta.label,
                            "pk": obj.pk,
                            "via": via,
                            "on_delete": "PROTECT",
                        }
                    )
            finally:
                self._pending_reason = previous_reason
            return
        is_set, set_value = _set_closure_value(on_delete)
        if is_set:
            if callable(set_value):
                self._record_unanalyzable(
                    on_delete, field, sub_objs, "SET() with a callable value"
                )
            else:
                # SET with a constant only schedules a field update.
                on_delete(self, field, sub_objs, self.using)
            return
        self._record_unanalyzable(
            on_delete, field, sub_objs, "custom on_delete handler"
        )

    def _record_unanalyzable(self, on_delete, field, sub_objs, description):
        handler = "%s.%s" % (
            getattr(on_delete, "__module__", ""),
            getattr(on_delete, "__qualname__", on_delete),
        )
        self.unanalyzable.append(
            {
                "model": field.model._meta.label,
                "field": field.name,
                "handler": handler,
                "pks": sorted(_json_safe(obj.pk) for obj in sub_objs),
                "reason": "%s cannot be analyzed without executing it." % description,
            }
        )

    def report(self):
        """
        Return a JSON-serializable dictionary describing the planned
        deletion. Ordering is stable: entries are sorted by model, primary
        key, and relation.
        """
        # Restricted objects that are themselves part of the deletion set
        # don't block it.
        for model, instances in self.data.items():
            self.clear_restricted_objects_from_set(model, instances)
        for model, fields in self.restricted_objects.items():
            for field, objs in fields.items():
                for obj in objs:
                    self.blocked.append(
                        {
                            "model": model._meta.label,
                            "pk": obj.pk,
                            "via": "%s.%s" % (model._meta.label, field.name),
                            "on_delete": "RESTRICT",
                        }
                    )

        deletions = []
        for model, instances in self.data.items():
            for obj in instances:
                reasons = sorted(
                    self.reasons.get((model, obj.pk), []),
                    key=lambda reason: (reason["via"] or "", reason["on_delete"]),
                )
                deletions.append(
                    {"model": model._meta.label, "pk": obj.pk, "reasons": reasons}
                )
        deletions.sort(key=lambda entry: (entry["model"], entry["pk"]))
        for entry in deletions:
            entry["pk"] = _json_safe(entry["pk"])

        field_updates = []
        for (field, value), instances_list in self.field_updates.items():
            pks = set()
            for instances in instances_list:
                pks.update(obj.pk for obj in instances)
            if not pks:
                continue
            field_updates.append(
                {
                    "model": field.model._meta.label,
                    "field": field.name,
                    "value": _json_safe(value),
                    "pks": sorted(_json_safe(pk) for pk in pks),
                }
            )
        field_updates.sort(
            key=lambda entry: (entry["model"], entry["field"], entry["pks"])
        )

        blocked = sorted(
            self.blocked,
            key=lambda entry: (entry["model"], entry["pk"], entry["via"]),
        )
        for entry in blocked:
            entry["pk"] = _json_safe(entry["pk"])

        unanalyzable = sorted(
            self.unanalyzable, key=lambda entry: (entry["model"], entry["field"])
        )

        planned = Counter(entry["model"] for entry in deletions)
        blocked_count = Counter(entry["model"] for entry in blocked)
        return {
            "database": self.using,
            "complete": not unanalyzable,
            "deletions": deletions,
            "field_updates": field_updates,
            "blocked": blocked,
            "unanalyzable": unanalyzable,
            "summary": {
                "planned": dict(sorted(planned.items())),
                "planned_total": sum(planned.values()),
                "blocked": dict(sorted(blocked_count.items())),
                "blocked_total": sum(blocked_count.values()),
            },
        }
