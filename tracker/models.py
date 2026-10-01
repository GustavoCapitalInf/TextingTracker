"""Persistent calling history and the assignments generated from each import."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone


class TextingListType(models.TextChoices):
    GFS = "gfs", "GFS Texting"
    RINGCENTRAL = "ringcentral", "Ringcentral Texting"


class RepListMembership(models.Model):
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="list_memberships",
    )
    list_type = models.CharField(max_length=16, choices=TextingListType)

    class Meta:
        ordering = ["rep_id", "list_type"]
        constraints = [
            models.UniqueConstraint(fields=["rep", "list_type"], name="unique_rep_list_membership"),
            models.CheckConstraint(
                condition=Q(list_type__in=TextingListType.values), name="valid_rep_list_membership_type",
            ),
        ]


class ImportBatch(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=120)
    list_type = models.CharField(max_length=16, choices=TextingListType, blank=True, default="")
    filename = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="imports"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.DRAFT)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="closed_imports", null=True, blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(list_type__in=["", *TextingListType.values]), name="valid_import_list_type",
            ),
        ]

    def __str__(self):
        return self.label

    def get_absolute_url(self):
        return reverse("upload_detail", kwargs={"upload_id": self.pk})

    @property
    def total_count(self):
        return self.rows.count()

    def _count(self, classification):
        return self.rows.filter(classification=classification).count()

    @property
    def ready_count(self):
        return self._count(ImportRow.Classification.READY)

    @property
    def previous_count(self):
        return self._count(ImportRow.Classification.PREVIOUS)

    @property
    def duplicate_count(self):
        return self._count(ImportRow.Classification.DUPLICATE)

    @property
    def invalid_count(self):
        return self._count(ImportRow.Classification.INVALID)

    @property
    def blocked_count(self):
        return self._count(ImportRow.Classification.BLOCKED)


class PhoneNumber(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        RESERVED = "reserved", "Reserved"
        CALLED = "called", "Called"
        BLOCKED = "blocked", "Blocked"
        RETIRED = "retired", "Retired"

    canonical = models.CharField(max_length=16, unique=True)
    status = models.CharField(max_length=12, choices=Status, default=Status.NEW)
    suppression_reason = models.CharField(max_length=40, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.canonical

    @property
    def e164(self):
        return self.canonical


class ImportRow(models.Model):
    class Classification(models.TextChoices):
        READY = "ready", "Ready to call"
        PREVIOUS = "previous", "Phone number uploaded previously"
        DUPLICATE = "duplicate", "Duplicate in this file"
        INVALID = "invalid", "Needs correction"
        BLOCKED = "blocked", "Blocked from calling"

    upload = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw_value = models.CharField(max_length=120)
    phone = models.ForeignKey(
        PhoneNumber, on_delete=models.PROTECT, related_name="import_rows", null=True, blank=True
    )
    classification = models.CharField(max_length=12, choices=Classification)
    message = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["row_number"]
        constraints = [
            models.UniqueConstraint(fields=["upload", "row_number"], name="unique_upload_row"),
        ]


class RepBatch(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    upload = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="batches")
    rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="rep_batches"
    )
    scheduled_date = models.DateField(db_index=True)
    status = models.CharField(max_length=10, choices=Status, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="closed_rep_batches", null=True, blank=True,
    )

    class Meta:
        ordering = ["scheduled_date", "rep__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["upload", "scheduled_date", "rep"], name="unique_daily_rep_batch"
            ),
        ]

    def __str__(self):
        return f"{self.upload.label} / {self.scheduled_date} / {self.rep.get_username()}"

    def get_absolute_url(self):
        return reverse("batch_detail", kwargs={"batch_id": self.pk})

    @property
    def assigned_count(self):
        return self.assignments.count()

    @property
    def completed_count(self):
        return self.assignments.filter(status=Assignment.Status.DONE).count()

    @property
    def remaining_count(self):
        return self.assignments.filter(
            status__in=[Assignment.Status.PENDING, Assignment.Status.IN_PROGRESS]
        ).count()


class Assignment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ready"
        IN_PROGRESS = "in_progress", "Call in progress"
        DONE = "done", "Completed"
        CANCELLED = "cancelled", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(RepBatch, on_delete=models.PROTECT, related_name="assignments")
    phone = models.ForeignKey(PhoneNumber, on_delete=models.PROTECT, related_name="assignments")
    sequence = models.PositiveIntegerField()
    status = models.CharField(max_length=14, choices=Status, default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["phone"], condition=Q(status__in=["pending", "in_progress"]),
                name="one_active_assignment_per_phone",
            ),
            models.UniqueConstraint(fields=["batch", "sequence"], name="unique_batch_sequence"),
        ]


class CallAttempt(models.Model):
    class Outcome(models.TextChoices):
        NOT_INTERESTED = "not_interested", "Not interested"
        DO_NOT_CALL = "do_not_call", "Do not call"
        NO_ANSWER = "no_answer", "No answer"
        INTERESTED = "interested", "Interested"
        CALLBACK_REQUESTED = "callback_requested", "Callback requested"

    assignment = models.ForeignKey(
        Assignment, on_delete=models.PROTECT, related_name="attempts"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    outcome = models.CharField(max_length=24, choices=Outcome)
    note = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["assignment"], name="one_outcome_per_assignment"),
        ]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=80)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Audit events cannot be edited.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit events cannot be deleted.")


class SecurityThrottle(models.Model):
    """Persistent login throttling shared across application worker processes."""

    key = models.CharField(max_length=64, unique=True)
    failures = models.PositiveIntegerField(default=0)
    window_started = models.DateTimeField(default=timezone.now)
    blocked_until = models.DateTimeField(null=True, blank=True)
