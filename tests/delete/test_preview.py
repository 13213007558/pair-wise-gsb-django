import json

from django.db import connection
from django.db.models import ProtectedError, RestrictedError
from django.db.models.signals import post_delete, pre_delete
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    B1,
    B2,
    B3,
    MRNull,
    B,
    CustomOnDelete,
    CycleA,
    CycleB,
    DeleteBottom,
    DeleteTop,
    GenericB1,
    M,
    Origin,
    R,
    RChild,
    Referrer,
    S,
    SecondReferrer,
    SetCallable,
    SetConstant,
    SetDefaultCallable,
    SetDefaultConstant,
    T,
    custom_on_delete_calls,
)


class DeletionPreviewTests(TestCase):
    def assert_read_only(self, qs):
        """
        Run delete_preview() while capturing all SQL and delete signals,
        and assert that nothing but SELECTs happened and that the result is
        JSON serializable.
        """
        fired_signals = []

        def receiver(sender, **kwargs):
            fired_signals.append(sender)

        pre_delete.connect(receiver, weak=False)
        post_delete.connect(receiver, weak=False)
        try:
            with CaptureQueriesContext(connection) as captured:
                preview = qs.delete_preview()
        finally:
            pre_delete.disconnect(receiver)
            post_delete.disconnect(receiver)
        self.assertEqual(fired_signals, [])
        for query in captured.captured_queries:
            self.assertTrue(
                query["sql"].lstrip().upper().startswith("SELECT"),
                msg="delete_preview() issued non-SELECT SQL: %s" % query["sql"],
            )
        # The result must be directly JSON serializable.
        json.dumps(preview)
        return preview

    def assert_matches_delete(self, qs):
        """
        Compare the preview with a real deletion performed afterwards on
        the same (transaction-isolated) data.
        """
        preview = self.assert_read_only(qs)
        _, deleted_per_model = qs.delete()
        self.assertEqual(preview["summary"]["planned"], deleted_per_model)
        self.assertEqual(
            preview["summary"]["planned_total"], sum(deleted_per_model.values())
        )
        return preview

    def deletions_by_model(self, preview):
        return {(entry["model"], entry["pk"]): entry for entry in preview["deletions"]}

    def test_cascade(self):
        r = R.objects.create()
        s = S.objects.create(r=r)
        t = T.objects.create(s=s)
        preview = self.assert_matches_delete(R.objects.filter(pk=r.pk))
        self.assertEqual(preview["database"], "default")
        self.assertTrue(preview["complete"])
        entries = self.deletions_by_model(preview)
        self.assertEqual(
            entries["delete.R", r.pk]["reasons"], [{"via": None, "on_delete": "ROOT"}]
        )
        self.assertEqual(
            entries["delete.S", s.pk]["reasons"],
            [{"via": "delete.S.r", "on_delete": "CASCADE"}],
        )
        self.assertEqual(
            entries["delete.T", t.pk]["reasons"],
            [{"via": "delete.T.s", "on_delete": "CASCADE"}],
        )
        self.assertEqual(preview["blocked"], [])
        self.assertEqual(preview["summary"]["blocked_total"], 0)

    def test_object_reached_via_two_paths_counted_once(self):
        origin = Origin.objects.create()
        referrer = Referrer.objects.create(unique_field=1, origin=origin)
        second = SecondReferrer.objects.create(
            referrer=referrer, other_referrer=referrer
        )
        preview = self.assert_matches_delete(Referrer.objects.filter(pk=referrer.pk))
        entries = self.deletions_by_model(preview)
        # A single deletion entry, but both relation reasons are kept.
        self.assertEqual(
            entries["delete.SecondReferrer", second.pk]["reasons"],
            [
                {"via": "delete.SecondReferrer.other_referrer", "on_delete": "CASCADE"},
                {"via": "delete.SecondReferrer.referrer", "on_delete": "CASCADE"},
            ],
        )
        self.assertEqual(
            preview["summary"]["planned"],
            {"delete.Referrer": 1, "delete.SecondReferrer": 1},
        )

    def test_set_null(self):
        m = M.objects.create()
        r = R.objects.create()
        mr_null = MRNull.objects.create(m=m, r=r)
        preview = self.assert_matches_delete(R.objects.filter(pk=r.pk))
        self.assertEqual(
            preview["field_updates"],
            [
                {
                    "model": "delete.MRNull",
                    "field": "r",
                    "value": None,
                    "pks": [mr_null.pk],
                }
            ],
        )
        mr_null.refresh_from_db()
        self.assertIsNone(mr_null.r_id)

    def test_set_constant(self):
        r = R.objects.create()
        set_constant = SetConstant.objects.create(r=r)
        preview = self.assert_matches_delete(R.objects.filter(pk=r.pk))
        self.assertEqual(
            preview["field_updates"],
            [
                {
                    "model": "delete.SetConstant",
                    "field": "r",
                    "value": None,
                    "pks": [set_constant.pk],
                }
            ],
        )

    def test_set_default_constant(self):
        r = R.objects.create()
        set_default = SetDefaultConstant.objects.create(r=r)
        preview = self.assert_matches_delete(R.objects.filter(pk=r.pk))
        self.assertEqual(
            preview["field_updates"],
            [
                {
                    "model": "delete.SetDefaultConstant",
                    "field": "r",
                    "value": None,
                    "pks": [set_default.pk],
                }
            ],
        )

    def test_protect(self):
        r = R.objects.create()
        b = B.objects.create(protect=r)
        preview = self.assert_read_only(R.objects.filter(pk=r.pk))
        self.assertEqual(
            preview["blocked"],
            [
                {
                    "model": "delete.B",
                    "pk": b.pk,
                    "via": "delete.B.protect",
                    "on_delete": "PROTECT",
                }
            ],
        )
        self.assertEqual(preview["summary"]["planned"], {"delete.R": 1})
        self.assertEqual(preview["summary"]["blocked"], {"delete.B": 1})
        self.assertEqual(preview["summary"]["blocked_total"], 1)
        with self.assertRaises(ProtectedError) as cm:
            R.objects.filter(pk=r.pk).delete()
        self.assertEqual({obj.pk for obj in cm.exception.protected_objects}, {b.pk})

    def test_restrict(self):
        r = R.objects.create()
        b3 = B3.objects.create(restrict=r)
        preview = self.assert_read_only(R.objects.filter(pk=r.pk))
        self.assertEqual(
            preview["blocked"],
            [
                {
                    "model": "delete.B3",
                    "pk": b3.pk,
                    "via": "delete.B3.restrict",
                    "on_delete": "RESTRICT",
                }
            ],
        )
        self.assertEqual(preview["summary"]["blocked"], {"delete.B3": 1})
        with self.assertRaises(RestrictedError) as cm:
            R.objects.filter(pk=r.pk).delete()
        self.assertEqual({obj.pk for obj in cm.exception.restricted_objects}, {b3.pk})

    def test_restrict_cleared_when_object_is_deleted_too(self):
        delete_top = DeleteTop.objects.create()
        b1 = B1.objects.create(delete_top=delete_top)
        b2 = B2.objects.create(delete_top=delete_top)
        DeleteBottom.objects.create(b1=b1, b2=b2)
        # DeleteBottom restricts B1, but DeleteBottom is itself deleted
        # through the B2 cascade, so nothing is blocked.
        preview = self.assert_matches_delete(DeleteTop.objects.filter(pk=delete_top.pk))
        self.assertEqual(preview["blocked"], [])
        self.assertEqual(
            preview["summary"]["planned"],
            {
                "delete.B1": 1,
                "delete.B2": 1,
                "delete.DeleteBottom": 1,
                "delete.DeleteTop": 1,
            },
        )

    def test_m2m_through_model(self):
        m = M.objects.create()
        r = R.objects.create()
        m.m2m.add(r)
        preview = self.assert_matches_delete(M.objects.filter(pk=m.pk))
        entries = self.deletions_by_model(preview)
        through_pk = next(pk for model, pk in entries if model == "delete.M_m2m")
        self.assertEqual(
            entries["delete.M_m2m", through_pk]["reasons"],
            [{"via": "delete.M_m2m.m", "on_delete": "CASCADE"}],
        )
        # The related R object isn't deleted.
        self.assertTrue(R.objects.filter(pk=r.pk).exists())

    def test_inheritance_cascade_down(self):
        r_child = RChild.objects.create()
        preview = self.assert_matches_delete(R.objects.filter(pk=r_child.pk))
        entries = self.deletions_by_model(preview)
        self.assertEqual(
            entries["delete.RChild", r_child.pk]["reasons"],
            [{"via": "delete.RChild.r_ptr", "on_delete": "CASCADE"}],
        )
        self.assertEqual(
            preview["summary"]["planned"], {"delete.R": 1, "delete.RChild": 1}
        )

    def test_inheritance_cascade_up(self):
        r_child = RChild.objects.create()
        preview = self.assert_matches_delete(RChild.objects.filter(pk=r_child.pk))
        entries = self.deletions_by_model(preview)
        self.assertEqual(
            entries["delete.R", r_child.pk]["reasons"],
            [{"via": "delete.RChild (parent link)", "on_delete": "CASCADE"}],
        )
        self.assertEqual(
            preview["summary"]["planned"], {"delete.R": 1, "delete.RChild": 1}
        )

    def test_cycle(self):
        a = CycleA.objects.create()
        b = CycleB.objects.create(a=a)
        a.b = b
        a.save()
        preview = self.assert_matches_delete(CycleA.objects.filter(pk=a.pk))
        entries = self.deletions_by_model(preview)
        self.assertEqual(
            entries["delete.CycleA", a.pk]["reasons"],
            [
                {"via": None, "on_delete": "ROOT"},
                {"via": "delete.CycleA.b", "on_delete": "CASCADE"},
            ],
        )
        self.assertEqual(
            entries["delete.CycleB", b.pk]["reasons"],
            [{"via": "delete.CycleB.a", "on_delete": "CASCADE"}],
        )

    def test_generic_relation(self):
        delete_top = DeleteTop.objects.create()
        GenericB1.objects.create(generic_delete_top=delete_top)
        preview = self.assert_matches_delete(DeleteTop.objects.filter(pk=delete_top.pk))
        self.assertEqual(
            preview["summary"]["planned"],
            {"delete.DeleteTop": 1, "delete.GenericB1": 1},
        )

    def test_custom_on_delete_is_unanalyzable(self):
        r = R.objects.create()
        custom = CustomOnDelete.objects.create(r=r)
        custom_on_delete_calls.clear()
        preview = self.assert_read_only(R.objects.filter(pk=r.pk))
        self.assertFalse(preview["complete"])
        self.assertEqual(
            preview["unanalyzable"],
            [
                {
                    "model": "delete.CustomOnDelete",
                    "field": "r",
                    "handler": "delete.models.custom_on_delete",
                    "pks": [custom.pk],
                    "reason": (
                        "custom on_delete handler cannot be analyzed "
                        "without executing it."
                    ),
                }
            ],
        )
        # The handler wasn't called to guess its effect.
        self.assertEqual(custom_on_delete_calls, [])
        self.assertEqual(preview["summary"]["planned"], {"delete.R": 1})
        # A real deletion does call it.
        r.delete()
        self.assertEqual(custom_on_delete_calls, [(CustomOnDelete, [custom.pk])])
        # The no-op handler left a dangling reference; remove it so the
        # deferred constraint checks at teardown pass.
        custom.delete()

    def test_set_callable_is_unanalyzable(self):
        r = R.objects.create()
        set_callable = SetCallable.objects.create(r=r)
        r_count = R.objects.count()
        preview = self.assert_read_only(R.objects.filter(pk=r.pk))
        self.assertFalse(preview["complete"])
        self.assertEqual(
            preview["unanalyzable"],
            [
                {
                    "model": "delete.SetCallable",
                    "field": "r",
                    "handler": "django.db.models.deletion.SET.<locals>.set_on_delete",
                    "pks": [set_callable.pk],
                    "reason": (
                        "SET() with a callable value cannot be analyzed "
                        "without executing it."
                    ),
                }
            ],
        )
        # The callable wasn't invoked: it would have created a default R.
        self.assertEqual(R.objects.count(), r_count)

    def test_set_default_callable_is_unanalyzable(self):
        r = R.objects.create()
        set_default = SetDefaultCallable.objects.create(r=r)
        r_count = R.objects.count()
        preview = self.assert_read_only(R.objects.filter(pk=r.pk))
        self.assertFalse(preview["complete"])
        self.assertEqual(
            preview["unanalyzable"],
            [
                {
                    "model": "delete.SetDefaultCallable",
                    "field": "r",
                    "handler": "django.db.models.deletion.SET_DEFAULT",
                    "pks": [set_default.pk],
                    "reason": (
                        "SET_DEFAULT with a callable default cannot be "
                        "analyzed without executing it."
                    ),
                }
            ],
        )
        self.assertEqual(R.objects.count(), r_count)

    def test_stable_ordering(self):
        for _ in range(3):
            r = R.objects.create()
            S.objects.create(r=r)
            S.objects.create(r=r)
        qs = R.objects.all()
        first = self.assert_read_only(qs)
        second = self.assert_read_only(qs)
        self.assertEqual(first, second)
        self.assertEqual(
            [(entry["model"], entry["pk"]) for entry in first["deletions"]],
            sorted((entry["model"], entry["pk"]) for entry in first["deletions"]),
        )
        for entry in first["deletions"]:
            self.assertEqual(
                entry["reasons"],
                sorted(
                    entry["reasons"],
                    key=lambda reason: (reason["via"] or "", reason["on_delete"]),
                ),
            )

    def test_sliced_queryset(self):
        msg = "Cannot use 'limit' or 'offset' with delete_preview()."
        with self.assertRaisesMessage(TypeError, msg):
            R.objects.all()[:1].delete_preview()

    def test_values_queryset(self):
        msg = "Cannot call delete_preview() after .values() or .values_list()"
        with self.assertRaisesMessage(TypeError, msg):
            R.objects.values("id").delete_preview()

    def test_empty_queryset(self):
        preview = self.assert_read_only(R.objects.none())
        self.assertTrue(preview["complete"])
        self.assertEqual(preview["deletions"], [])
        self.assertEqual(preview["field_updates"], [])
        self.assertEqual(preview["blocked"], [])
        self.assertEqual(
            preview["summary"],
            {"planned": {}, "planned_total": 0, "blocked": {}, "blocked_total": 0},
        )

    def test_manager_call(self):
        r = R.objects.create()
        S.objects.create(r=r)
        preview = self.assert_read_only(R.objects.all())
        manager_preview = R.objects.delete_preview()
        self.assertEqual(preview, manager_preview)
        self.assertEqual(
            preview["summary"]["planned"], {"delete.R": 1, "delete.S": 1}
        )
