"""Readable audit rows without exposing raw metadata or full phone numbers.

Presentation only: callers must authorize access to the audit page. Target
references are resolved in at most four queries for an already-loaded page.
"""

from datetime import date
from uuid import UUID

from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import Assignment, CallAttempt, ImportBatch, RepBatch


_ACTION_LABELS = {
    "upload.imported": "Spreadsheet uploaded",
    "upload.published": "Batches created",
    "upload.previous_numbers_decided": "Previously uploaded numbers",
    "upload.planned": "Upload filled a planned list",
    "template.suggested": "Template drafted from past weeks",
    "template.created": "Template created",
    "template.updated": "Template saved",
    "template.list_saved": "Template list saved",
    "template.list_removed": "Template list removed",
    "template.deleted": "Template deleted",
    "plan.skipped": "Planned day skipped",
    "texting_list.created": "Texting list created",
    "texting_list.updated": "Texting list saved",
    "texting_list.members_changed": "Texting list reps changed",
    "texting_list.deleted": "Texting list deleted",
    "plan.unskipped": "Skipped day planned again",
    "upload.closed": "Upload cleared",
    "upload.viewed": "Upload reviewed",
    "batch.closed": "Batch cleared",
    "batch.viewed": "Batch opened",
    "call.started": "Call started",
    "call.outcome_recorded": "Call outcome recorded",
    "account.signed_in": "Signed in",
    "account.signed_out": "Signed out",
    "account.sign_in_failed": "Sign-in failed",
    "account.password_changed": "Password changed",
    "account.rep_created": "Rep account created",
    "account.rep_enabled": "Rep access enabled",
    "account.rep_disabled": "Rep access disabled",
    "account.rep_password_reset": "Rep password reset",
    "account.rep_memberships_changed": "Texting lists updated",
}

_SIMPLE_DESCRIPTIONS = {
    "upload.planned": "Linked this upload to a planned list so its days and reps were filled in.",
    "template.created": "Created a weekly template.",
    "template.list_saved": "Saved a planned list's name, days, and reps.",
    "template.list_removed": "Removed a planned list; existing lists were unchanged.",
    "template.deleted": "Deleted a weekly template; existing lists were unchanged.",
    "upload.viewed": "Opened the spreadsheet review and allocation details.",
    "call.started": "Marked this assigned number as a call in progress.",
    "account.signed_in": "Signed in with their personal account.",
    "account.signed_out": "Ended their signed-in session.",
    "account.sign_in_failed": "A sign-in attempt was unsuccessful.",
    "account.password_changed": "Updated their account password.",
    "account.rep_created": "Created a representative account with individual sign-in access.",
    "account.rep_enabled": "Restored account access; assignments and history were retained.",
    "account.rep_disabled": "Disabled account access; assignments and history were retained.",
    "account.rep_password_reset": "Generated a new password for this representative.",
    "account.rep_memberships_changed": "Updated the representative's texting-list eligibility; existing assignments were retained.",
}


def _target_key(event):
    kind = event.target_type if isinstance(event.target_type, str) else ""
    kind = kind.lower()
    if kind == "upload":
        kind = "importbatch"
    try:
        if kind in {"importbatch", "repbatch", "assignment"}:
            return kind, UUID(str(event.target_id))
        if kind == "user":
            value = str(event.target_id)
            if value.isascii() and value.isdecimal():
                identifier = int(value)
                if 0 < identifier <= 9223372036854775807:
                    return kind, identifier
    except (ValueError, TypeError, AttributeError):
        pass
    return kind, None


def _number(details, key):
    value = details.get(key)
    return value if type(value) is int and value >= 0 else None


def _quantity(value, singular, plural=None):
    return f"{value:,} {singular if value == 1 else plural or singular + 's'}"


def _date_label(value):
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _rep_label(rep):
    return rep.get_full_name().strip() or rep.get_username()


def _batch_label(batch):
    return f"{_rep_label(batch.rep)} · {_date_label(batch.scheduled_date)}"


def _description(event):
    details = event.details if isinstance(event.details, dict) else {}
    action = event.action
    if action in _SIMPLE_DESCRIPTIONS:
        return _SIMPLE_DESCRIPTIONS[action]
    if action == "upload.imported":
        total = _number(details, "total_rows")
        parts = [f"{_quantity(total, 'row')} checked" if total is not None else "Spreadsheet checked"]
        counts = details.get("counts")
        if isinstance(counts, dict):
            previous = (_number(counts, "previous") or 0) + (_number(counts, "blocked") or 0)
            for count, label in [
                (_number(counts, "ready"), "new"), (previous, "previously uploaded"),
                (_number(counts, "duplicate"), "duplicates in the file"),
                (_number(counts, "invalid"), "invalid"),
            ]:
                if count:
                    parts.append(f"{count:,} {label}")
        return "; ".join(parts) + "."
    if action == "upload.published":
        assigned = _number(details, "assigned_count")
        batch_count = _number(details, "batch_count")
        rep_ids = details.get("rep_ids")
        summary = f"Assigned {_quantity(assigned, 'number')}" if assigned is not None else "Created assignments"
        if batch_count is not None:
            summary += f" across {_quantity(batch_count, 'batch', 'batches')}"
        if isinstance(rep_ids, list):
            valid_reps = {identifier for identifier in rep_ids if type(identifier) is int and identifier > 0}
            if valid_reps:
                summary += f" for {_quantity(len(valid_reps), 'rep')}"
        days = []
        raw_dates = details.get("dates")
        if isinstance(raw_dates, list):
            for raw in raw_dates[:31]:
                if isinstance(raw, str):
                    try:
                        days.append(date.fromisoformat(raw))
                    except ValueError:
                        pass
        if days:
            summary += ". List dates: " + "; ".join(_date_label(day) for day in sorted(set(days)))
        reused = _number(details, "reused_count")
        if reused:
            summary += f". Includes {_quantity(reused, 'previously uploaded number')}"
        return summary + "."
    if action == "upload.previous_numbers_decided":
        count = _number(details, "previous_count")
        numbers = _quantity(count, "previously uploaded number") if count is not None else "Previously uploaded numbers"
        if details.get("include") is True:
            return f"Chose to upload {numbers} again in this list." if count is not None else "Chose to upload previously uploaded numbers again."
        if details.get("include") is False:
            return f"Left {numbers} out of this list." if count is not None else "Left previously uploaded numbers out of this list."
        return "Recorded a decision about previously uploaded numbers."
    if action == "texting_list.created":
        count = _number(details, "rep_count")
        return f"Created a texting list with {_quantity(count, 'rep')}." if count is not None else "Created a texting list."
    if action == "texting_list.updated":
        if details.get("is_active") is False:
            return "Saved the texting list; it is hidden from new uploads and templates."
        return "Renamed the texting list." if details.get("renamed") is True else "Saved the texting list."
    if action == "texting_list.members_changed":
        added, removed = _number(details, "added"), _number(details, "removed")
        if added is None or removed is None:
            return "Changed which reps are on the texting list."
        return f"Added {_quantity(added, 'rep')} and removed {_quantity(removed, 'rep')}; removed reps lost access to this list."
    if action == "texting_list.deleted":
        return "Deleted an unused texting list."
    if action in ("plan.skipped", "plan.unskipped"):
        raw = details.get("day")
        try:
            day = _date_label(date.fromisoformat(raw)) if isinstance(raw, str) else None
        except ValueError:
            day = None
        verb = "Skipped a planned list" if action == "plan.skipped" else "Brought back a skipped planned list"
        return f"{verb} for {day}." if day else f"{verb}."
    if action == "template.suggested":
        count = _number(details, "list_count")
        return f"Drafted {_quantity(count, 'list')} from the last 4 weeks." if count is not None else "Drafted a template from recent weeks."
    if action == "template.updated":
        return "Turned the template on for calendar planning." if details.get("is_active") is True else "Saved the template; it is not planning the calendar."
    if action == "upload.closed":
        count = _number(details, "batch_count")
        summary = f"Closed {_quantity(count, 'batch', 'batches')} from this upload" if count is not None else "Closed this upload"
        return summary + "; all history was retained."
    if action == "batch.closed":
        count = _number(details, "cancelled_count")
        summary = f"Removed {_quantity(count, 'assignment')} from this rep's list" if count is not None else "Closed this rep's list"
        return summary + "; all history was retained."
    if action == "batch.viewed":
        count = _number(details, "numbers_displayed")
        page = _number(details, "page")
        summary = f"Displayed {_quantity(count, 'number')}" if count is not None else "Opened the assigned list"
        if page:
            summary += f" on page {page:,}"
        return summary + "."
    if action == "call.outcome_recorded":
        outcome = details.get("outcome")
        outcome_label = dict(CallAttempt.Outcome.choices).get(outcome) if isinstance(outcome, str) else None
        summary = f"Recorded outcome: {outcome_label}." if outcome_label else "Saved the completed call's outcome."
        if isinstance(outcome, str) and outcome in {CallAttempt.Outcome.NOT_INTERESTED, CallAttempt.Outcome.DO_NOT_CALL}:
            summary += " Number blocked across all batches."
        if details.get("has_note") is True:
            summary += " A call note was saved."
        return summary
    return "Recorded an account or assignment activity."


def present_events(events):
    """Attach action_label, description, target_label, and target_url in place."""
    events = list(events)
    keys = [_target_key(event) for event in events]
    ids = {
        kind: {identifier for current_kind, identifier in keys if current_kind == kind and identifier is not None}
        for kind in ("importbatch", "repbatch", "assignment", "user")
    }
    targets = {
        "importbatch": ImportBatch.objects.in_bulk(ids["importbatch"]),
        "repbatch": RepBatch.objects.select_related("rep").in_bulk(ids["repbatch"]),
        "assignment": Assignment.objects.select_related("phone", "batch__rep").in_bulk(ids["assignment"]),
        "user": get_user_model().objects.in_bulk(ids["user"]),
    }
    for event, (kind, identifier) in zip(events, keys):
        event.action_label = _ACTION_LABELS.get(event.action, "Activity recorded")
        event.description = _description(event)
        event.target_url = ""
        event.target_label = {
            "importbatch": "Upload unavailable", "repbatch": "Batch unavailable",
            "assignment": "Assignment unavailable", "user": "Account unavailable",
        }.get(kind, "Sign-in" if event.action == "account.sign_in_failed" else "Workspace")
        target = targets.get(kind, {}).get(identifier)
        if target is None:
            continue
        if kind == "importbatch":
            event.target_label = target.label
            event.target_url = target.get_absolute_url()
        elif kind == "repbatch":
            event.target_label = _batch_label(target)
            event.target_url = target.get_absolute_url()
        elif kind == "assignment":
            event.target_label = f"•••• {target.phone.canonical[-4:]} · {_batch_label(target.batch)}"
            event.target_url = target.batch.get_absolute_url()
        else:
            event.target_label = _rep_label(target)
            if not target.is_staff and not target.is_superuser:
                event.target_url = reverse("team")
    return events
