from datetime import date
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from tracker.audit_presenter import present_events
from tracker.models import Assignment, AuditEvent, ImportBatch, PhoneNumber, RepBatch


class AuditPresenterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.manager = User.objects.create_user("manager", is_staff=True)
        cls.rep = User.objects.create_user("emilio", first_name="Emilio", last_name="Arguello")
        cls.upload = ImportBatch.objects.create(label="Monday organic leads", filename="phones.xlsx", uploaded_by=cls.manager)
        cls.batch = RepBatch.objects.create(upload=cls.upload, rep=cls.rep, scheduled_date=date(2026, 10, 5))
        cls.phone = PhoneNumber.objects.create(canonical="+12125550123")
        cls.assignment = Assignment.objects.create(batch=cls.batch, phone=cls.phone, sequence=1)

    def event(self, action, target=None, details=None, target_type=None, target_id=None):
        return AuditEvent(
            action=action, actor=self.manager, details=details or {},
            target_type=target_type if target_type is not None else (target._meta.model_name if target else "system"),
            target_id=target_id if target_id is not None else str(target.pk if target else "None"),
        )

    def test_targets_resolve_in_four_queries_and_numbers_are_masked(self):
        events = [
            self.event("upload.viewed", self.upload),
            self.event("batch.viewed", self.batch, {"numbers_displayed": 8, "page": 1}),
            self.event("call.started", self.assignment),
            self.event("account.rep_created", self.rep),
        ]
        with self.assertNumQueries(4):
            presented = present_events(events * 10)
            self.assertEqual(len(presented), 40)
            self.assertEqual(events[0].target_label, "Monday organic leads")
            self.assertEqual(events[0].target_url, self.upload.get_absolute_url())
            self.assertEqual(events[1].target_label, "Emilio Arguello · Oct 5, 2026")
            self.assertIn("•••• 0123", events[2].target_label)
            self.assertEqual(events[2].target_url, self.batch.get_absolute_url())
            self.assertNotIn(self.phone.canonical, events[2].target_label)
            self.assertEqual(events[3].target_label, "Emilio Arguello")
            self.assertEqual(events[3].target_url, reverse("team"))

    def test_describes_import_allocation_display_and_suppression_without_raw_details(self):
        imported = self.event("upload.imported", self.upload, {
            "total_rows": 10, "counts": {"ready": 5, "previous": 2, "duplicate": 1, "invalid": 1, "blocked": 1},
        })
        published = self.event("upload.published", self.upload, {
            "assigned_count": 20, "batch_count": 4, "rep_ids": [self.rep.pk, 999],
            "dates": ["2026-10-05", "2026-10-06"],
        })
        viewed = self.event("batch.viewed", self.batch, {"numbers_displayed": 5, "page": 2})
        outcome = self.event("call.outcome_recorded", self.assignment, {
            "outcome": "not_interested", "has_note": True, "note": "Private note",
            "password": "Private password", "phone": self.phone.canonical, "ip": "192.0.2.10",
        })
        present_events([imported, published, viewed, outcome])
        self.assertEqual(imported.action_label, "Spreadsheet uploaded")
        self.assertIn("10 rows checked; 5 new; 3 previously uploaded", imported.description)
        self.assertNotIn("blocked", imported.description)
        self.assertEqual(imported.details["counts"]["blocked"], 1)
        self.assertIn("20 numbers across 4 batches for 2 reps", published.description)
        self.assertIn("List dates: Oct 5, 2026; Oct 6, 2026", published.description)
        self.assertEqual(viewed.description, "Displayed 5 numbers on page 2.")
        for event in (imported, published, viewed):
            self.assertNotIn("call", event.description.lower())
        self.assertIn("Not interested", outcome.description)
        self.assertIn("blocked across all batches", outcome.description)
        for private in ["Private note", "Private password", self.phone.canonical, "192.0.2.10"]:
            self.assertNotIn(private, outcome.description + outcome.target_label)
        self.assertEqual(outcome.details["note"], "Private note")

    def test_malformed_and_missing_references_degrade_without_exposing_raw_values(self):
        events = [
            self.event("upload.viewed", target_type="upload", target_id="not-a-uuid"),
            self.event("batch.viewed", target_type="repbatch", target_id=str(uuid4())),
            self.event("account.signed_in", target_type="user", target_id="9" * 70),
            self.event("account.sign_in_failed", details={"ip": "192.0.2.50"}),
            self.event("unknown.event", target_type="malformed", target_id="sensitive-value"),
        ]
        present_events(events)
        self.assertEqual(events[0].target_label, "Upload unavailable")
        self.assertEqual(events[1].target_label, "Batch unavailable")
        self.assertEqual(events[2].target_label, "Account unavailable")
        self.assertEqual(events[3].target_label, "Sign-in")
        self.assertEqual(events[4].action_label, "Activity recorded")
        self.assertTrue(all(event.target_url == "" for event in events))
        self.assertNotIn("192.0.2.50", events[3].description)
        self.assertNotIn("sensitive-value", events[4].description + events[4].target_label)
        self.assertEqual(events[4].description, "Recorded an account or assignment activity.")

    def test_ignores_unexpected_metadata_types_and_keeps_stored_event_unchanged(self):
        stored = AuditEvent.objects.create(
            actor=self.rep, action="call.outcome_recorded", target_type="assignment",
            target_id=str(self.assignment.pk), details={"outcome": "do_not_call", "has_note": False},
        )
        malformed = [
            self.event("upload.imported", self.upload, {"total_rows": True, "counts": "bad"}),
            self.event("upload.published", self.upload, {"dates": ["bad", {}, "2026-10-05"], "rep_ids": [[], False]}),
            self.event("call.outcome_recorded", self.assignment, {"outcome": ["no_answer"]}),
            self.event("upload.imported", self.upload, {"counts": {"previous": True, "blocked": 2}}),
        ]
        present_events([stored, *malformed])
        stored.refresh_from_db()
        self.assertEqual(stored.details, {"outcome": "do_not_call", "has_note": False})
        self.assertEqual(malformed[0].description, "Spreadsheet checked.")
        self.assertIn("Oct 5, 2026", malformed[1].description)
        self.assertTrue(malformed[1].description.startswith("Created assignments."))
        self.assertEqual(malformed[2].description, "Saved the completed call's outcome.")
        self.assertEqual(malformed[3].description, "Spreadsheet checked; 2 previously uploaded.")
