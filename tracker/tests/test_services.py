from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import Workbook

from tracker.models import (
    Assignment, AuditEvent, CallAttempt, ImportBatch, ImportRow, PhoneNumber,
    RepBatch, RepListMembership, TextingListType,
)
from tracker.services import (
    close_batch, close_upload, import_workbook, normalize_phone, preview_split,
    publish_batches, record_outcome, start_call, eligible_reps, classify_upload,
)


def spreadsheet(values, name="phones.xlsx", header=True, second_sheet=None):
    workbook = Workbook()
    sheet = workbook.active
    if header:
        sheet.append(["Phone number"])
    for value in values:
        sheet.append(value if isinstance(value, list) else [value])
    if second_sheet is not None:
        extra = workbook.create_sheet("Extra")
        for value in second_sheet:
            extra.append([value])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def valid_numbers(count):
    return [f"212555{1000 + index:04d}" for index in range(count)]


class ServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model()
        cls.manager = users.objects.create_user("manager", password="ManagerExample123!", is_staff=True)
        cls.rep = users.objects.create_user("rep-a", password="RepExample123!")
        cls.other_rep = users.objects.create_user("rep-b", password="OtherExample123!")
        cls.third_rep = users.objects.create_user("rep-c", password="ThirdExample123!")
        RepListMembership.objects.bulk_create([
            RepListMembership(rep=rep, list_type=TextingListType.GFS)
            for rep in [cls.rep, cls.other_rep, cls.third_rep]
        ])

    def import_numbers(self, count=3, *, list_type=TextingListType.GFS):
        return import_workbook(
            spreadsheet(valid_numbers(count)), self.manager, "Weekly leads", list_type=list_type,
        )

    def assigned(self, count=3, day=None, rep=None):
        upload = self.import_numbers(count)
        batch = publish_batches(upload, [day or timezone.localdate()], [rep or self.rep], self.manager)[0]
        return upload, batch, list(batch.assignments.select_related("phone"))

    def test_normalizes_format_variants_and_excel_numbers(self):
        for raw in ["(212) 555-1234", "+1 212-555-1234", "12125551234", 2125551234, 2125551234.0]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "+12125551234")
        self.assertEqual(normalize_phone("416 555 1234"), "+14165551234")

    def test_rejects_incomplete_international_extensions_and_fractional_numbers(self):
        for raw in ["5551234", "1234567890", "+44 20 7946 0958", "2125551234 x5", 2125551234.5, True, "+2125551234"]:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                normalize_phone(raw)

    def test_import_tracks_within_file_duplicates_and_invalid_rows(self):
        upload = import_workbook(spreadsheet([
            "2125551234", "(212) 555-1234", None, "4165551234", "bad data",
        ]), self.manager, list_type=TextingListType.GFS)
        self.assertEqual(upload.total_count, 4)
        self.assertEqual(upload.ready_count, 2)
        self.assertEqual(upload.duplicate_count, 1)
        self.assertEqual(upload.invalid_count, 1)
        self.assertEqual(PhoneNumber.objects.count(), 2)
        self.assertEqual(upload.rows.first().row_number, 2)
        self.assertEqual(upload.rows.get(classification="duplicate").phone, upload.rows.first().phone)

    def test_repeat_import_never_resets_phone_state(self):
        first = self.import_numbers(2)
        second = self.import_numbers(2)
        self.assertEqual(first.ready_count, 2)
        self.assertEqual(second.ready_count, 0)
        self.assertEqual(second.previous_count, 2)
        self.assertEqual(PhoneNumber.objects.count(), 2)
        with self.assertRaises(ValidationError):
            publish_batches(second, [timezone.localdate()], [self.rep], self.manager)

    def test_repeat_import_describes_assignment_without_loading_legacy_sales_outcomes(self):
        _, batch, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        record_outcome(assignments[0], self.rep, "not_interested", "Private legacy sales note")
        with CaptureQueriesContext(connection) as queries:
            repeated = self.import_numbers()
        self.assertFalse(any("tracker_callattempt" in item["sql"].lower() for item in queries.captured_queries))
        self.assertEqual(repeated.ready_count, 0)
        for row in repeated.rows.all():
            self.assertIn("Phone number uploaded previously on", row.message)
            self.assertIn(f"Assigned to {self.rep.username} for {batch.scheduled_date.isoformat()}", row.message)
            self.assertNotIn("interested", row.message.lower())
            self.assertNotIn("called", row.message.lower())
            self.assertNotIn("Private legacy sales note", row.message)
        self.assertEqual(CallAttempt.objects.get().note, "Private legacy sales note")

    def test_rejects_formulas_multiple_columns_multiple_sheets_atomically(self):
        for file in [
            spreadsheet(["2125551234", "=1+1"]),
            spreadsheet([["2125551234", "extra"]]),
            spreadsheet(["2125551234"], second_sheet=["4165551234"]),
        ]:
            with self.subTest(filename=file.name), self.assertRaises(ValidationError):
                import_workbook(file, self.manager, list_type=TextingListType.GFS)
        self.assertEqual(ImportBatch.objects.count(), 0)
        self.assertEqual(PhoneNumber.objects.count(), 0)

    def test_rejects_bad_files_and_oversized_uploads(self):
        for file in [
            SimpleUploadedFile("phones.csv", b"2125551234"),
            SimpleUploadedFile("phones.xlsx", b"not an excel file"),
            spreadsheet([]),
        ]:
            with self.subTest(filename=file.name), self.assertRaises(ValidationError):
                import_workbook(file, self.manager, list_type=TextingListType.GFS)
        with override_settings(IMPORT_MAX_BYTES=50), self.assertRaises(ValidationError):
            self.import_numbers()
        with override_settings(IMPORT_MAX_ROWS=2), self.assertRaises(ValidationError):
            self.import_numbers(3)

    def test_representative_cannot_import_publish_or_clear(self):
        with self.assertRaises(PermissionDenied):
            import_workbook(spreadsheet(valid_numbers(2)), self.rep, list_type=TextingListType.GFS)
        upload = self.import_numbers()
        with self.assertRaises(PermissionDenied):
            publish_batches(upload, [timezone.localdate()], [self.rep], self.rep)
        with self.assertRaises(PermissionDenied):
            close_upload(upload, self.rep)
        batch = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)[0]
        with self.assertRaises(PermissionDenied):
            close_batch(batch, self.rep)

    def test_day_first_split_is_balanced_and_rotates_extras(self):
        upload = self.import_numbers(23)
        dates = [timezone.localdate() + timedelta(days=index) for index in range(4)]
        reps = [self.rep, self.other_rep, self.third_rep]
        preview = preview_split(upload, dates, reps)
        self.assertEqual(sum(item["count"] for item in preview), 23)
        self.assertEqual([sum(item["count"] for item in preview if item["date"] == day) for day in dates], [6, 6, 6, 5])
        rep_totals = [sum(item["count"] for item in preview if item["rep"] == rep) for rep in reps]
        self.assertLessEqual(max(rep_totals) - min(rep_totals), 1)
        batches = publish_batches(upload, dates, reps, self.manager)
        self.assertEqual(len(batches), 12)
        self.assertEqual(Assignment.objects.count(), 23)
        self.assertEqual(Assignment.objects.values("phone_id").distinct().count(), 23)
        for item in preview:
            batch = next(batch for batch in batches if batch.scheduled_date == item["date"] and batch.rep_id == item["rep"].pk)
            self.assertEqual(batch.assigned_count, item["count"])

    def test_new_imports_require_a_valid_texting_list_type(self):
        for invalid in ["", None, "other", "GFS", 0, []]:
            with self.subTest(list_type=invalid), self.assertRaises(ValidationError):
                self.import_numbers(list_type=invalid)
        with self.assertRaises(TypeError):
            import_workbook(spreadsheet(valid_numbers(1)), self.manager)
        self.assertEqual(ImportBatch.objects.count(), 0)
        self.assertEqual(PhoneNumber.objects.count(), 0)
        upload = self.import_numbers()
        self.assertEqual(upload.list_type, TextingListType.GFS)
        self.assertEqual(AuditEvent.objects.get(action="upload.imported").details["list_type"], "gfs")

    def test_rep_from_other_list_cannot_preview_or_receive_batch(self):
        ring_rep = get_user_model().objects.create_user("ring-only")
        RepListMembership.objects.create(rep=ring_rep, list_type=TextingListType.RINGCENTRAL)
        upload = self.import_numbers()
        for operation in [
            lambda: preview_split(upload, [timezone.localdate()], [ring_rep]),
            lambda: publish_batches(upload, [timezone.localdate()], [ring_rep], self.manager),
        ]:
            with self.assertRaisesMessage(ValidationError, "belong to GFS Texting"):
                operation()
        self.assertEqual(RepBatch.objects.count(), 0)
        self.assertEqual(Assignment.objects.count(), 0)
        ring_upload = import_workbook(
            spreadsheet(["4165551234"]), self.manager, list_type=TextingListType.RINGCENTRAL,
        )
        with self.assertRaisesMessage(ValidationError, "belong to Ringcentral Texting"):
            publish_batches(ring_upload, [timezone.localdate()], [self.rep], self.manager)

    def test_dual_member_can_receive_both_types_without_duplicate_assignments(self):
        RepListMembership.objects.create(rep=self.rep, list_type=TextingListType.RINGCENTRAL)
        gfs = self.import_numbers(2)
        ring = import_workbook(
            spreadsheet(["4165551234", "6135551234"]), self.manager,
            list_type=TextingListType.RINGCENTRAL,
        )
        for upload in [gfs, ring]:
            self.assertEqual(preview_split(upload, [timezone.localdate()], [self.rep])[0]["count"], 2)
            batches = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
            self.assertEqual(batches[0].rep, self.rep)
        self.assertEqual(RepBatch.objects.filter(rep=self.rep).count(), 2)
        self.assertEqual(Assignment.objects.values("phone_id").distinct().count(), 4)
        self.assertEqual(list(eligible_reps(TextingListType.RINGCENTRAL)), [self.rep])

    def test_phone_deduplication_is_global_across_texting_list_types(self):
        RepListMembership.objects.create(rep=self.rep, list_type=TextingListType.RINGCENTRAL)
        original = self.import_numbers()
        publish_batches(original, [timezone.localdate()], [self.rep], self.manager)
        repeated = self.import_numbers(list_type=TextingListType.RINGCENTRAL)
        self.assertEqual(repeated.list_type, TextingListType.RINGCENTRAL)
        self.assertEqual(repeated.previous_count, 3)
        self.assertEqual(repeated.ready_count, 0)
        with self.assertRaises(ValidationError):
            publish_batches(repeated, [timezone.localdate()], [self.rep], self.manager)
        self.assertEqual(PhoneNumber.objects.count(), 3)
        self.assertEqual(Assignment.objects.count(), 3)

    def test_membership_revoked_after_preview_is_rechecked_before_publication(self):
        upload = self.import_numbers()
        preview_split(upload, [timezone.localdate()], [self.rep])
        RepListMembership.objects.filter(rep=self.rep, list_type=TextingListType.GFS).delete()
        with self.assertRaises(ValidationError):
            preview_split(upload, [timezone.localdate()], [self.rep])
        with self.assertRaises(ValidationError):
            publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
        self.assertEqual(Assignment.objects.count(), 0)
        upload.refresh_from_db()
        self.assertEqual(upload.status, ImportBatch.Status.DRAFT)

    def test_publish_retry_rechecks_revoked_membership_without_changing_existing_batch(self):
        upload = self.import_numbers()
        batch = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)[0]
        RepListMembership.objects.filter(rep=self.rep, list_type=TextingListType.GFS).delete()
        with self.assertRaises(ValidationError):
            publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
        self.assertEqual(RepBatch.objects.get().pk, batch.pk)
        self.assertEqual(Assignment.objects.count(), 3)

    def test_eligible_reps_excludes_inactive_and_manager_accounts(self):
        RepListMembership.objects.create(rep=self.manager, list_type=TextingListType.GFS)
        get_user_model().objects.filter(pk=self.other_rep.pk).update(is_active=False)
        self.assertEqual(set(eligible_reps(TextingListType.GFS)), {self.rep, self.third_rep})
        with self.assertRaises(ValidationError):
            eligible_reps("")

    def test_unclassified_legacy_upload_is_preserved_but_cannot_be_published(self):
        upload = self.import_numbers()
        ImportBatch.objects.filter(pk=upload.pk).update(list_type="")
        with self.assertRaises(ValidationError):
            preview_split(upload, [timezone.localdate()], [self.rep])
        with self.assertRaises(ValidationError):
            publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
        self.assertEqual(upload.rows.count(), 3)
        self.assertEqual(PhoneNumber.objects.count(), 3)

    def test_manager_can_classify_legacy_draft_without_reimporting_numbers(self):
        upload = self.import_numbers()
        original_rows = list(upload.rows.values_list("pk", "phone_id", "classification"))
        ImportBatch.objects.filter(pk=upload.pk).update(list_type="")
        classified = classify_upload(upload, TextingListType.GFS, self.manager)
        self.assertEqual(classified.list_type, TextingListType.GFS)
        self.assertEqual(list(classified.rows.values_list("pk", "phone_id", "classification")), original_rows)
        self.assertEqual(ImportBatch.objects.count(), 1)
        self.assertEqual(PhoneNumber.objects.count(), 3)
        event = AuditEvent.objects.get(action="upload.type_changed")
        self.assertEqual(event.details, {"list_type": "gfs", "previous_list_type": ""})
        classify_upload(upload, TextingListType.GFS, self.manager)
        self.assertEqual(AuditEvent.objects.filter(action="upload.type_changed").count(), 1)
        self.assertEqual(len(publish_batches(classified, [timezone.localdate()], [self.rep], self.manager)), 1)

    def test_classification_requires_manager_valid_type_and_unassigned_draft(self):
        upload = self.import_numbers()
        with self.assertRaises(PermissionDenied):
            classify_upload(upload, TextingListType.RINGCENTRAL, self.rep)
        with self.assertRaises(ValidationError):
            classify_upload(upload, "", self.manager)
        batch = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)[0]
        with self.assertRaises(ValidationError):
            classify_upload(upload, TextingListType.RINGCENTRAL, self.manager)
        # Even an inconsistent legacy draft with a retained batch cannot be reclassified.
        ImportBatch.objects.filter(pk=upload.pk).update(status=ImportBatch.Status.DRAFT)
        with self.assertRaises(ValidationError):
            classify_upload(upload, TextingListType.RINGCENTRAL, self.manager)
        self.assertEqual(RepBatch.objects.get().pk, batch.pk)
        close_upload(upload, self.manager)
        with self.assertRaises(ValidationError):
            classify_upload(upload, TextingListType.RINGCENTRAL, self.manager)
        upload.refresh_from_db()
        self.assertEqual(upload.list_type, TextingListType.GFS)

    def test_database_enforces_membership_uniqueness_and_valid_type_values(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            RepListMembership.objects.create(rep=self.rep, list_type=TextingListType.GFS)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RepListMembership.objects.create(rep=self.rep, list_type="other")
        upload = self.import_numbers()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImportBatch.objects.filter(pk=upload.pk).update(list_type="other")

    def test_remainder_rotation_balances_multiple_small_days(self):
        upload = self.import_numbers(8)
        days = [timezone.localdate() + timedelta(days=index) for index in range(4)]
        reps = [self.rep, self.other_rep, self.third_rep]
        preview = preview_split(upload, days, reps)
        totals = [sum(row["count"] for row in preview if row["rep"] == rep) for rep in reps]
        self.assertEqual(totals, [3, 3, 2])
        self.assertEqual(len(publish_batches(upload, days, reps, self.manager)), 12)

    def test_publish_retry_is_idempotent_and_changed_settings_are_rejected(self):
        upload = self.import_numbers()
        first = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
        second = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)
        self.assertEqual([batch.pk for batch in first], [batch.pk for batch in second])
        self.assertEqual(Assignment.objects.count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="upload.published").count(), 1)
        with self.assertRaises(ValidationError):
            publish_batches(upload, [timezone.localdate()], [self.other_rep], self.manager)

    def test_split_requires_distinct_dates_active_reps_and_no_past_dates(self):
        upload = self.import_numbers()
        today = timezone.localdate()
        inactive = get_user_model().objects.create_user("inactive", is_active=False)
        for dates, reps in [([], [self.rep]), ([today], []), ([today, today], [self.rep]),
                             ([today], [self.rep, self.rep]), ([today], [self.manager]),
                             ([today], [inactive]), ([today - timedelta(days=1)], [self.rep])]:
            with self.subTest(dates=dates, reps=reps), self.assertRaises(ValidationError):
                publish_batches(upload, dates, reps, self.manager)
        self.assertEqual(RepBatch.objects.count(), 0)

    def test_future_assignments_reserve_numbers_but_cannot_start_early(self):
        _, _, assignments = self.assigned(day=timezone.localdate() + timedelta(days=1))
        self.assertEqual(assignments[0].phone.status, PhoneNumber.Status.RESERVED)
        with self.assertRaises(PermissionDenied):
            start_call(assignments[0], self.rep)
        repeated = self.import_numbers()
        self.assertEqual(repeated.previous_count, 3)

    def test_wrong_rep_cannot_start_or_record_calls(self):
        _, _, assignments = self.assigned()
        with self.assertRaises(PermissionDenied):
            start_call(assignments[0], self.other_rep)
        start_call(assignments[0], self.rep)
        with self.assertRaises(PermissionDenied):
            record_outcome(assignments[0], self.other_rep, "no_answer")

    def test_start_is_idempotent_and_only_one_call_can_be_in_progress(self):
        _, _, assignments = self.assigned()
        first = start_call(assignments[0], self.rep)
        second = start_call(assignments[0], self.rep)
        self.assertEqual(first.started_at, second.started_at)
        self.assertEqual(AuditEvent.objects.filter(action="call.started").count(), 1)
        with self.assertRaises(ValidationError):
            start_call(assignments[1], self.rep)
        record_outcome(first, self.rep, "no_answer")
        self.assertEqual(start_call(assignments[1], self.rep).status, Assignment.Status.IN_PROGRESS)

    def test_unstarted_or_invalid_outcomes_are_rejected(self):
        _, _, assignments = self.assigned()
        with self.assertRaises(ValidationError):
            record_outcome(assignments[0], self.rep, "no_answer")
        start_call(assignments[0], self.rep)
        with self.assertRaises(ValidationError):
            record_outcome(assignments[0], self.rep, "made_up")
        with self.assertRaises(ValidationError):
            record_outcome(assignments[0], self.rep, "no_answer", "x" * 1001)
        self.assertEqual(CallAttempt.objects.count(), 0)

    def test_not_interested_and_do_not_call_are_suppressed_globally(self):
        _, _, assignments = self.assigned()
        for assignment, outcome in zip(assignments, ["not_interested", "do_not_call"]):
            start_call(assignment, self.rep)
            result = record_outcome(assignment, self.rep, outcome)
            self.assertEqual(result.phone.status, PhoneNumber.Status.BLOCKED)
            self.assertEqual(result.phone.suppression_reason, outcome)
        repeated = self.import_numbers()
        self.assertEqual(repeated.blocked_count, 2)
        self.assertEqual(repeated.ready_count, 0)
        self.assertEqual(repeated.previous_count, 1)

    def test_outcome_retry_is_idempotent_but_cannot_overwrite_history(self):
        _, _, assignments = self.assigned()
        assignment = start_call(assignments[0], self.rep)
        record_outcome(assignment, self.rep, "no_answer", "Rang out")
        record_outcome(assignment, self.rep, "no_answer", "Rang out")
        self.assertEqual(CallAttempt.objects.count(), 1)
        with self.assertRaises(ValidationError):
            record_outcome(assignment, self.rep, "interested")
        self.assertEqual(CallAttempt.objects.get().outcome, "no_answer")
        repeated = self.import_numbers()
        self.assertEqual(repeated.ready_count, 0)

    def test_clear_upload_cancels_pending_and_legacy_in_progress_assignments(self):
        upload, batch, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        cleared = close_upload(upload, self.manager)
        self.assertEqual(cleared.status, ImportBatch.Status.CLOSED)
        batch.refresh_from_db()
        self.assertEqual(batch.status, RepBatch.Status.CLOSED)
        self.assertEqual(batch.assignments.filter(status=Assignment.Status.CANCELLED).count(), 3)
        self.assertEqual(batch.remaining_count, 0)
        with self.assertRaises(ValidationError):
            start_call(assignments[1], self.rep)
        with self.assertRaises(ValidationError):
            record_outcome(assignments[0], self.rep, "interested")
        self.assertEqual(CallAttempt.objects.count(), 0)
        self.assertEqual(PhoneNumber.objects.filter(status=PhoneNumber.Status.RETIRED).count(), 3)
        self.assertEqual(PhoneNumber.objects.count(), 3)
        self.assertEqual(ImportRow.objects.count(), 3)
        self.assertEqual(self.import_numbers().previous_count, 3)

    def test_clear_one_batch_does_not_close_other_batches(self):
        upload = self.import_numbers(4)
        batches = publish_batches(upload, [timezone.localdate()], [self.rep, self.other_rep], self.manager)
        legacy_assignment = start_call(batches[0].assignments.first(), batches[0].rep)
        close_batch(batches[0], self.manager)
        legacy_assignment.refresh_from_db()
        self.assertEqual(legacy_assignment.status, Assignment.Status.CANCELLED)
        self.assertEqual(batches[0].assignments.filter(status=Assignment.Status.CANCELLED).count(), 2)
        batches[1].refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(batches[1].status, RepBatch.Status.OPEN)
        self.assertEqual(upload.status, ImportBatch.Status.PUBLISHED)
        first = batches[1].assignments.first()
        self.assertEqual(start_call(first, batches[1].rep).status, Assignment.Status.IN_PROGRESS)

    def test_clear_preserves_completed_legacy_history_and_cancels_remaining_assignments(self):
        upload, batch, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        record_outcome(assignments[0], self.rep, "not_interested", "Retained historical note")
        start_call(assignments[1], self.rep)
        close_upload(upload, self.manager)
        self.assertEqual(batch.assignments.filter(status=Assignment.Status.DONE).count(), 1)
        self.assertEqual(batch.assignments.filter(status=Assignment.Status.CANCELLED).count(), 2)
        self.assertEqual(CallAttempt.objects.get().note, "Retained historical note")
        assignments[0].phone.refresh_from_db()
        self.assertEqual(assignments[0].phone.status, PhoneNumber.Status.BLOCKED)
        self.assertEqual(self.import_numbers().ready_count, 0)

    def test_reclearing_old_closed_upload_cancels_legacy_active_assignments(self):
        upload, batch, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        original_closed_at = timezone.now() - timedelta(hours=1)
        # Simulate a batch closed by the earlier version, which kept started calls alive.
        RepBatch.objects.filter(pk=batch.pk).update(
            status=RepBatch.Status.CLOSED, closed_at=original_closed_at, closed_by=self.manager,
        )
        ImportBatch.objects.filter(pk=upload.pk).update(
            status=ImportBatch.Status.CLOSED, closed_at=original_closed_at, closed_by=self.manager,
        )
        with self.assertRaises(ValidationError):
            record_outcome(assignments[0], self.rep, "interested")
        close_upload(upload, self.manager)
        close_upload(upload, self.manager)
        batch.refresh_from_db()
        upload.refresh_from_db()
        self.assertEqual(batch.assignments.filter(status=Assignment.Status.CANCELLED).count(), 3)
        self.assertEqual(batch.remaining_count, 0)
        self.assertEqual(batch.closed_at, original_closed_at)
        self.assertEqual(upload.closed_at, original_closed_at)
        self.assertEqual(PhoneNumber.objects.filter(status=PhoneNumber.Status.RETIRED).count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="batch.closed").count(), 1)

    def test_clear_draft_and_retry_preserves_all_history(self):
        upload = self.import_numbers()
        close_upload(upload, self.manager)
        close_upload(upload, self.manager)
        self.assertEqual(PhoneNumber.objects.filter(status=PhoneNumber.Status.RETIRED).count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="upload.closed").count(), 1)
        self.assertEqual(self.import_numbers().previous_count, 3)
        with self.assertRaises(ValidationError):
            publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)

    def test_clear_upload_closes_future_batches(self):
        upload = self.import_numbers(4)
        batches = publish_batches(upload, [timezone.localdate(), timezone.localdate() + timedelta(days=1)], [self.rep], self.manager)
        close_upload(upload, self.manager)
        self.assertEqual(upload.batches.filter(status=RepBatch.Status.CLOSED).count(), 2)
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.CANCELLED).count(), 4)
        self.assertEqual(len(batches), 2)

    def test_started_call_finishes_after_midnight_but_pending_cannot_start(self):
        _, _, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        tomorrow = timezone.localdate() + timedelta(days=1)
        with patch("tracker.services.timezone.localdate", return_value=tomorrow):
            self.assertEqual(record_outcome(assignments[0], self.rep, "callback_requested").status, Assignment.Status.DONE)
            with self.assertRaises(PermissionDenied):
                start_call(assignments[1], self.rep)

    def test_database_constraint_prevents_duplicate_live_assignments(self):
        _, batch, assignments = self.assigned()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.create(batch=batch, phone=assignments[0].phone, sequence=99)

    def test_audit_keeps_metadata_and_prevents_instance_edits(self):
        _, _, assignments = self.assigned()
        start_call(assignments[0], self.rep)
        record_outcome(assignments[0], self.rep, "not_interested", "Private call notes")
        for event in AuditEvent.objects.all():
            self.assertNotIn(assignments[0].phone.canonical, str(event.details))
            self.assertNotIn("Private call notes", str(event.details))
        event = AuditEvent.objects.first()
        with self.assertRaises(ValueError):
            event.save()
        with self.assertRaises(ValueError):
            event.delete()


@skipUnless(connection.vendor == "postgresql", "PostgreSQL is required for concurrent-write verification.")
class PostgreSQLConcurrencyTests(TransactionTestCase):
    """Run against PostgreSQL before deployment; SQLite cannot prove row locking."""

    def setUp(self):
        self.manager = get_user_model().objects.create_user("manager", is_staff=True)
        self.rep = get_user_model().objects.create_user("rep")
        RepListMembership.objects.create(rep=self.rep, list_type=TextingListType.GFS)

    def run_concurrently(self, operation):
        gate = Barrier(2)

        def worker(index):
            close_old_connections()
            try:
                gate.wait(timeout=10)
                return operation(index)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, index) for index in range(2)]
            return [future.result(timeout=30) for future in futures]

    def test_simultaneous_imports_register_phone_once(self):
        def operation(_):
            manager = get_user_model().objects.get(pk=self.manager.pk)
            upload = import_workbook(spreadsheet(valid_numbers(1)), manager, list_type=TextingListType.GFS)
            return upload.ready_count, upload.previous_count

        results = self.run_concurrently(operation)
        self.assertEqual(sum(result[0] for result in results), 1)
        self.assertEqual(sum(result[1] for result in results), 1)
        self.assertEqual(PhoneNumber.objects.count(), 1)

    def test_simultaneous_publish_creates_assignments_once(self):
        upload = import_workbook(spreadsheet(valid_numbers(3)), self.manager, list_type=TextingListType.GFS)

        def operation(_):
            manager = get_user_model().objects.get(pk=self.manager.pk)
            rep = get_user_model().objects.get(pk=self.rep.pk)
            return [batch.pk for batch in publish_batches(upload, [timezone.localdate()], [rep], manager)]

        results = self.run_concurrently(operation)
        self.assertEqual(results[0], results[1])
        self.assertEqual(Assignment.objects.count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="upload.published").count(), 1)

    def test_simultaneous_start_is_idempotent(self):
        upload = import_workbook(spreadsheet(valid_numbers(1)), self.manager, list_type=TextingListType.GFS)
        batch = publish_batches(upload, [timezone.localdate()], [self.rep], self.manager)[0]
        assignment = batch.assignments.first()

        def operation(_):
            rep = get_user_model().objects.get(pk=self.rep.pk)
            return start_call(assignment, rep).started_at

        results = self.run_concurrently(operation)
        self.assertEqual(results[0], results[1])
        self.assertEqual(AuditEvent.objects.filter(action="call.started").count(), 1)
