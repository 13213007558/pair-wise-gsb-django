"""
Local fixture references for JSON fixtures.

When explicitly enabled (loaddata --fixture-refs), objects in JSON fixtures
may declare a "_fixture_id" and relation fields may reference it with
{"$ref": "<id>"}. The ids are scoped to a single loaddata call: they are
never written to the database and are forgotten once the command finishes.
"""

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.serializers import base
from django.core.serializers.base import (
    FIXTURE_ID_KEY,
    FIXTURE_REF_KEY,
    DeserializationError,
)
from django.core.serializers.python import Deserializer as PythonDeserializer
from django.db import models


class FixtureRefError(DeserializationError):
    """A local fixture reference couldn't be resolved."""


def is_ref(value):
    """Return True if the value is a local reference, e.g. {"$ref": "x"}."""
    return isinstance(value, dict) and set(value) == {FIXTURE_REF_KEY}


class FixtureEntry:
    """A single object from a fixture file, with its loading provenance."""

    def __init__(self, source, index, data):
        self.source = source
        self.index = index
        self.data = data
        self.model = None
        self.ref_id = data.get(FIXTURE_ID_KEY) if isinstance(data, dict) else None
        self.deserialized = None
        self.saved = False
        # Non-nullable FK references: hard edges for the insertion order.
        self.hard_fk_refs = []
        # Nullable FK references: deferred until every object is inserted.
        self.soft_fk_refs = []
        # M2M field name -> raw list containing local references.
        self.m2m_refs = {}
        self.stashed_m2m_data = None

    def describe(self):
        if isinstance(self.data, dict):
            model_label = self.data.get("model", "?")
        else:
            model_label = "?"
        parts = ["%s object #%d" % (model_label, self.index)]
        if self.ref_id is not None:
            parts.append("%s %r" % (FIXTURE_ID_KEY, self.ref_id))
        elif isinstance(self.data, dict) and "pk" in self.data:
            parts.append("pk=%r" % (self.data["pk"],))
        return "%s in fixture '%s'" % (", ".join(parts), self.source)


class FixtureRefLoader:
    """
    Two-phase loader for JSON fixtures using local references.

    All objects from every fixture file of a single loaddata call are
    collected and validated first, and an insertion order that satisfies
    non-nullable foreign keys is computed; only then is anything written to
    the database.
    """

    def __init__(self, *, using, ignorenonexistent=False):
        self.using = using
        self.ignorenonexistent = ignorenonexistent
        self.entries = []
        self.registry = {}
        self.save_order = []

    def add_fixture_data(self, source, object_list):
        """Register the raw objects of one fixture file."""
        if not isinstance(object_list, list):
            raise FixtureRefError(
                "Fixture '%s' must contain a JSON array of objects." % source
            )
        for index, data in enumerate(object_list, start=1):
            entry = FixtureEntry(source, index, data)
            self.entries.append(entry)
            if entry.ref_id is not None:
                if not isinstance(entry.ref_id, str):
                    raise FixtureRefError(
                        "%s: %s must be a string."
                        % (entry.describe(), FIXTURE_ID_KEY)
                    )
                if entry.ref_id in self.registry:
                    other = self.registry[entry.ref_id]
                    raise FixtureRefError(
                        "Duplicate %s %r: declared by %s and again by %s."
                        % (
                            FIXTURE_ID_KEY,
                            entry.ref_id,
                            other.describe(),
                            entry.describe(),
                        )
                    )
                self.registry[entry.ref_id] = entry
        return len(object_list)

    def prepare(self):
        """Validate references and build (unsaved) model instances."""
        for entry in self.entries:
            self._prepare_entry(entry)
        self.save_order = self._compute_save_order()

    def save(self, save_obj):
        """
        Save all prepared objects. "save_obj" is called with each
        DeserializedObject in an order that satisfies non-nullable
        references and must return True if the object was saved.
        """
        saved_entries = []
        for entry in self.save_order:
            deserialized = entry.deserialized
            instance = deserialized.object
            for field, ref_id in entry.hard_fk_refs:
                target = self.registry[ref_id]
                setattr(instance, field.attname, self._target_pk(entry, field, target))
            # Many-to-many data is stashed and applied only once every
            # object has been inserted.
            entry.stashed_m2m_data = deserialized.m2m_data
            deserialized.m2m_data = None
            if save_obj(deserialized):
                entry.saved = True
                saved_entries.append(entry)
        for entry in saved_entries:
            self._save_deferred(entry)
        return len(saved_entries)

    def _prepare_entry(self, entry):
        data = entry.data
        if not isinstance(data, dict) or "model" not in data or "fields" not in data:
            raise FixtureRefError(
                "Invalid object #%d in fixture '%s': expected an object with "
                "'model' and 'fields' keys." % (entry.index, entry.source)
            )
        model_label = data["model"]
        entry.model = self._get_model(entry, model_label)
        model = entry.model
        field_names = {f.name for f in model._meta.get_fields()}
        cleaned_fields = {}
        for field_name, field_value in data.get("fields", {}).items():
            if self.ignorenonexistent and field_name not in field_names:
                continue
            try:
                field = model._meta.get_field(field_name)
            except FieldDoesNotExist:
                # Let the standard deserializer raise a proper error.
                cleaned_fields[field_name] = field_value
                continue
            remote_field = field.remote_field
            if isinstance(remote_field, models.ManyToOneRel) and is_ref(field_value):
                ref_id = field_value[FIXTURE_REF_KEY]
                self._resolve_ref(entry, field, ref_id)
                if field.null:
                    entry.soft_fk_refs.append((field, ref_id))
                else:
                    entry.hard_fk_refs.append((field, ref_id))
            elif (
                isinstance(remote_field, models.ManyToManyRel)
                and isinstance(field_value, (list, tuple))
                and any(is_ref(item) for item in field_value)
            ):
                for item in field_value:
                    if is_ref(item):
                        self._resolve_ref(entry, field, item[FIXTURE_REF_KEY])
                entry.m2m_refs[field_name] = list(field_value)
            else:
                cleaned_fields[field_name] = field_value
        cleaned = {"model": model_label, "fields": cleaned_fields}
        if "pk" in data:
            cleaned["pk"] = data["pk"]
        try:
            deserializer = PythonDeserializer(
                [cleaned],
                using=self.using,
                ignorenonexistent=self.ignorenonexistent,
                handle_forward_references=True,
            )
            entry.deserialized = next(iter(deserializer))
        except DeserializationError as e:
            raise FixtureRefError("%s: %s" % (entry.describe(), e))

    def _get_model(self, entry, model_label):
        try:
            return apps.get_model(model_label)
        except (LookupError, TypeError):
            raise FixtureRefError(
                "%s: invalid model identifier: %s" % (entry.describe(), model_label)
            )

    def _resolve_ref(self, entry, field, ref_id):
        """Validate a local reference and return the entry it points to."""
        try:
            target = self.registry[ref_id]
        except KeyError:
            raise FixtureRefError(
                "%s, field '%s': unresolved reference {%r: %r}; no object "
                "with that %s was declared in this loaddata call."
                % (
                    entry.describe(),
                    field.name,
                    FIXTURE_REF_KEY,
                    ref_id,
                    FIXTURE_ID_KEY,
                )
            )
        target_model = self._get_model(target, target.data.get("model"))
        expected_model = field.remote_field.model
        if not (
            target_model._meta.concrete_model is expected_model._meta.concrete_model
            or issubclass(target_model, expected_model)
        ):
            raise FixtureRefError(
                "%s, field '%s': reference {%r: %r} points to %s, but the "
                "field expects %s."
                % (
                    entry.describe(),
                    field.name,
                    FIXTURE_REF_KEY,
                    ref_id,
                    target_model._meta.label,
                    expected_model._meta.label,
                )
            )
        return target

    def _compute_save_order(self):
        """
        Topologically sort entries so that targets of non-nullable FK
        references are inserted first. Raise before anything is written if
        the references form a cycle that cannot be inserted.
        """
        order = []
        state = {}  # id(entry) -> "visiting" | "visited"
        for root in self.entries:
            if state.get(id(root)) == "visited":
                continue
            state[id(root)] = "visiting"
            path = [root]
            stack = [(root, iter(self._hard_targets(root)))]
            while stack:
                entry, targets = stack[-1]
                descended = False
                for target in targets:
                    target_state = state.get(id(target))
                    if target_state == "visiting":
                        cycle = path[path.index(target):] + [target]
                        raise FixtureRefError(self._cycle_message(cycle))
                    if target_state is None:
                        state[id(target)] = "visiting"
                        path.append(target)
                        stack.append((target, iter(self._hard_targets(target))))
                        descended = True
                        break
                if not descended:
                    stack.pop()
                    path.pop()
                    state[id(entry)] = "visited"
                    order.append(entry)
        return order

    def _hard_targets(self, entry):
        return [self.registry[ref_id] for _, ref_id in entry.hard_fk_refs]

    def _cycle_message(self, cycle):
        lines = []
        for entry, next_entry in zip(cycle, cycle[1:]):
            field_name = next(
                field.name
                for field, ref_id in entry.hard_fk_refs
                if self.registry[ref_id] is next_entry
            )
            lines.append(
                "  %s, field '%s' -> %s %r"
                % (entry.describe(), field_name, FIXTURE_ID_KEY, next_entry.ref_id)
            )
        return (
            "Cannot load fixtures: circular reference through non-nullable "
            "foreign keys:\n%s\n"
            "These objects cannot be inserted in an order that satisfies "
            "their foreign key constraints, and database constraints are not "
            "disabled to work around this. Make at least one of these fields "
            "nullable (null=True) so the reference can be deferred, or break "
            "the cycle."
            % "\n".join(lines)
        )

    def _target_pk(self, entry, field, target):
        pk = target.deserialized.object.pk
        if pk is None and not target.saved:
            raise FixtureRefError(
                "%s, field '%s': reference {%r: %r} points to %s, which was "
                "not saved (it may be excluded) and has no primary key."
                % (
                    entry.describe(),
                    field.name,
                    FIXTURE_REF_KEY,
                    target.ref_id,
                    target.describe(),
                )
            )
        return pk

    def _save_deferred(self, entry):
        """Fill in references that had to wait for every object to exist."""
        deserialized = entry.deserialized
        instance = deserialized.object
        for field, ref_id in entry.soft_fk_refs:
            target = self.registry[ref_id]
            setattr(instance, field.attname, self._target_pk(entry, field, target))
        if deserialized.deferred_fields:
            # Also persists the soft FK references set above.
            deserialized.save_deferred_fields(using=self.using)
        elif entry.soft_fk_refs:
            models.Model.save_base(instance, using=self.using, raw=True)
        m2m_data = dict(entry.stashed_m2m_data or {})
        for field_name, raw_values in entry.m2m_refs.items():
            field = entry.model._meta.get_field(field_name)
            resolved = [
                self._target_pk(entry, field, self.registry[item[FIXTURE_REF_KEY]])
                if is_ref(item)
                else item
                for item in raw_values
            ]
            m2m_data[field_name] = base.deserialize_m2m_values(
                field, resolved, self.using, handle_forward_references=False
            )
        for accessor_name, object_list in m2m_data.items():
            getattr(instance, accessor_name).set(object_list)
