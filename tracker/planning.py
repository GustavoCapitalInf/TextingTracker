"""Weekly templates: learn recurring lists from history and plan upcoming weeks.

A template is a week of planned lists (name, texting group, weekdays, reps).
Active templates add "planned" entries to the admin calendar until a file is
uploaded and split for that list and week. Reps never see planned entries.
"""

import re
from collections import defaultdict
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import ImportBatch, PlanSkip, RepBatch, ScheduleTemplate, TemplateList
from . import texting_lists
from .services import _phone_transaction, _require_manager, eligible_reps, write_audit


LOOKBACK_WEEKS = 4


def week_start(day):
    return day - timedelta(days=day.weekday())


def _label_key(label):
    """Compare list names by their letters and digits only, so typing variations match."""
    return re.sub(r"[\W_]+", "", label.casefold())


def _plan_key(slot):
    return _label_key(slot.label), slot.list_type


def suggest_template(actor):
    """Create an inactive template from the lists used in the last few weeks.

    Lists are grouped by name and texting group. Each suggestion copies the most
    recent week's weekdays and reps and notes how many recent weeks used it.
    """
    _require_manager(actor)
    today = timezone.localdate()
    first_week = week_start(today) - timedelta(weeks=LOOKBACK_WEEKS - 1)
    batches = RepBatch.objects.filter(
        upload__status=ImportBatch.Status.PUBLISHED, upload__list_type__in=texting_lists.active_keys(),
        scheduled_date__range=(first_week, week_start(today) + timedelta(days=6)),
    ).values("upload_id", "upload__label", "upload__list_type", "scheduled_date", "rep_id")
    uploads = {}
    for batch in batches:
        upload = uploads.setdefault(batch["upload_id"], {
            "label": batch["upload__label"], "list_type": batch["upload__list_type"], "dates": set(), "reps": set(),
        })
        upload["dates"].add(batch["scheduled_date"])
        upload["reps"].add(batch["rep_id"])
    if not uploads:
        raise ValidationError("There are no lists from the last 4 weeks to learn from yet.")

    patterns = defaultdict(list)
    for upload in uploads.values():
        patterns[(_label_key(upload["label"]), upload["list_type"])].append(upload)
    suggestions = []
    for occurrences in patterns.values():
        latest = max(occurrences, key=lambda item: max(item["dates"]))
        latest_week = week_start(max(latest["dates"]))
        weeks_used = {week_start(day) for item in occurrences for day in item["dates"]}
        suggestions.append({
            "label": latest["label"], "list_type": latest["list_type"],
            "weekdays": sorted({day.weekday() for day in latest["dates"] if week_start(day) == latest_week}),
            "reps": latest["reps"], "weeks": len(weeks_used),
        })
    suggestions.sort(key=lambda item: (-item["weeks"], item["weekdays"][0], item["label"].casefold()))

    with _phone_transaction():
        template = ScheduleTemplate.objects.create(
            name=f"Suggested from {first_week:%b} {first_week.day}–{today:%b} {today.day}", created_by=actor,
        )
        for item in suggestions:
            planned = TemplateList.objects.create(
                template=template, label=item["label"], list_type=item["list_type"], weekdays=item["weekdays"],
                note=f"Used in {item['weeks']} of the last {LOOKBACK_WEEKS} weeks",
            )
            # Only reps who can still receive this group's lists are suggested.
            planned.reps.set(eligible_reps(item["list_type"]).filter(pk__in=item["reps"]))
        write_audit(actor, "template.suggested", template, {"list_count": len(suggestions)})
    return template


def _week_coverage(week, keys, exclude_upload=None):
    """For each (name, group) key: its open uploads that week and the days it already has a list.

    An upload made from a planned list counts toward that list; any other
    published list counts when its name and group match.
    """
    found = defaultdict(lambda: {"uploads": {}, "covered": set()})
    linked = ImportBatch.objects.filter(planned_week=week, template_list__isnull=False).exclude(
        status=ImportBatch.Status.CLOSED).exclude(pk=exclude_upload).select_related("template_list", "uploaded_by")
    for upload in linked:
        key = _plan_key(upload.template_list)
        if key in keys:
            found[key]["uploads"][upload.pk] = upload
    batches = RepBatch.objects.filter(
        status=RepBatch.Status.OPEN, upload__status=ImportBatch.Status.PUBLISHED,
        scheduled_date__range=(week, week + timedelta(days=6)),
    ).exclude(upload=exclude_upload).select_related("upload__template_list", "upload__uploaded_by")
    for batch in batches:
        upload = batch.upload
        if upload.template_list_id and upload.planned_week == week:
            key = _plan_key(upload.template_list)
        else:
            key = (_label_key(upload.label), upload.list_type)
        if key in keys:
            found[key]["uploads"][upload.pk] = upload
            found[key]["covered"].add(batch.scheduled_date)
    return {key: {"uploads": sorted(info["uploads"].values(), key=lambda item: item.created_at), "covered": info["covered"]}
            for key, info in found.items()}


def _skipped(week):
    return set(PlanSkip.objects.filter(day__range=(week, week + timedelta(days=6))).values_list("label_key", "list_type", "day"))


def planned_for_week(week, today):
    """Planned entries from active templates for each remaining day of `week`, by date.

    Each day is checked on its own: it stays planned until that day has a list
    with the same name and group, or the manager skips it. The same list in
    several active templates is planned once per day.
    """
    active = texting_lists.active_keys()
    slots = [slot for slot in TemplateList.objects.filter(template__is_active=True).select_related("template").prefetch_related("reps")
             if slot.list_type in active]  # hidden lists can't take new uploads, so they plan nothing
    if not slots:
        return {}
    coverage = _week_coverage(week, {_plan_key(slot) for slot in slots})
    skipped = _skipped(week)
    plan = defaultdict(list)
    planned_keys = defaultdict(set)
    for slot in slots:
        key = _plan_key(slot)
        info = coverage.get(key, {"uploads": [], "covered": set()})
        draft = next((upload for upload in info["uploads"] if upload.status == ImportBatch.Status.DRAFT), None)
        for day in slot.days_in_week(week):
            if day < today or day in info["covered"] or (*key, day) in skipped or key in planned_keys[day]:
                continue
            planned_keys[day].add(key)
            plan[day].append({"slot": slot, "week": week, "day": day, "draft": draft,
                              "state": "uploaded" if draft else "needs_file",
                              "list_type_label": texting_lists.short_label(slot.list_type)})
    return plan


def uploads_for_plan(slot, week):
    """Files already uploaded this week for the same planned list, oldest first."""
    return _week_coverage(week, {_plan_key(slot)}).get(_plan_key(slot), {"uploads": []})["uploads"]


def skipped_for_week(week):
    return PlanSkip.objects.filter(day__range=(week, week + timedelta(days=6)))


def skip_planned_day(actor, slot, day):
    """Hide one planned day for this list (in every active template). Undo removes the skip."""
    _require_manager(actor)
    if day.weekday() not in slot.weekdays or day < timezone.localdate():
        raise ValidationError("That day is not an upcoming day for this planned list.")
    label_key, list_type = _plan_key(slot)
    skip, created = PlanSkip.objects.get_or_create(
        label_key=label_key, list_type=list_type, day=day, defaults={"label": slot.label, "created_by": actor},
    )
    if created:
        write_audit(actor, "plan.skipped", skip, {"day": day.isoformat(), "list_type": list_type})
    return skip


def unskip_planned_day(actor, skip):
    _require_manager(actor)
    write_audit(actor, "plan.unskipped", skip, {"day": skip.day.isoformat(), "list_type": skip.list_type})
    skip.delete()


def planned_slot(slot_id, week_text, today):
    """The active template list and week a planned upload link refers to, or (None, None)."""
    try:
        slot = TemplateList.objects.select_related("template").get(pk=int(slot_id), template__is_active=True)
        week = week_start(date.fromisoformat(week_text))
    except (TypeError, ValueError, TemplateList.DoesNotExist):
        return None, None
    if slot.list_type not in texting_lists.active_keys():
        return None, None
    if week.year > 9998 or week + timedelta(days=6) < today:
        return None, None
    return slot, week


def split_prefill(upload, today):
    """Initial split values for an upload that fills a planned list, with any notes."""
    slot, week = upload.template_list, upload.planned_week
    if slot is None or week is None or upload.status != ImportBatch.Status.DRAFT:
        return None, []
    key = _plan_key(slot)
    covered = _week_coverage(week, {key}, exclude_upload=upload.pk).get(key, {"covered": set()})["covered"]
    skipped = {day for label_key, list_type, day in _skipped(week) if (label_key, list_type) == key}
    upcoming = [day for day in slot.days_in_week(week) if day >= today]
    dates = [day for day in upcoming if day not in covered and day not in skipped]
    planned_reps = list(slot.reps.all())
    reps = list(eligible_reps(upload.list_type).filter(pk__in=[rep.pk for rep in planned_reps]))
    notes = [f"Days and reps were picked from your “{slot.template.name}” template. You can change anything before creating the lists."]
    if len(reps) < len(planned_reps):
        missing = len(planned_reps) - len(reps)
        group = texting_lists.label(upload.list_type)
        notes.append(f"{missing} planned rep{'s' if missing != 1 else ''} {'aren’t' if missing != 1 else 'isn’t'} an active member of {group} and {'were' if missing != 1 else 'was'} left out.")
    if len(upcoming) < len(slot.weekdays):
        notes.append("Planned days that have already passed were left out.")
    if len(dates) < len(upcoming):
        notes.append("Days that already have this list, or were skipped, were left out.")
    initial = {"dates": "\n".join(day.isoformat() for day in dates)}
    if reps:
        initial.update({"rep_count": len(reps), "reps": [rep.pk for rep in reps]})
    return initial, notes


def link_upload(upload, slot, week, actor):
    _require_manager(actor)
    ImportBatch.objects.filter(pk=upload.pk).update(template_list=slot, planned_week=week)
    upload.template_list, upload.planned_week = slot, week
    write_audit(actor, "upload.planned", upload, {"template_id": slot.template_id, "week": week.isoformat()})


def create_template(actor, name):
    _require_manager(actor)
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the template a name.")
    template = ScheduleTemplate.objects.create(name=name[:120], created_by=actor)
    write_audit(actor, "template.created", template)
    return template


def update_template(actor, template, *, name, is_active):
    _require_manager(actor)
    name = (name or "").strip()
    if not name:
        raise ValidationError("Give the template a name.")
    template.name, template.is_active = name[:120], bool(is_active)
    template.save(update_fields=["name", "is_active", "updated_at"])
    write_audit(actor, "template.updated", template, {"is_active": template.is_active})
    return template


def save_template_list(actor, template, *, label, list_type, weekdays, reps, slot=None):
    """Create or update one planned list; every rep must belong to its texting group."""
    _require_manager(actor)
    label = (label or "").strip()
    if not label:
        raise ValidationError("Give the list a name.")
    if list_type not in texting_lists.active_keys():
        raise ValidationError("Choose a texting list.")
    days = sorted({int(day) for day in weekdays})
    if not days or any(day not in range(7) for day in days):
        raise ValidationError("Choose at least one weekday.")
    rep_ids = {rep.pk for rep in reps}
    allowed = set(eligible_reps(list_type).filter(pk__in=rep_ids).values_list("pk", flat=True))
    if allowed != rep_ids:
        raise ValidationError(f"Every rep must be active and belong to {texting_lists.label(list_type)}.")
    with _phone_transaction():
        if slot is None:
            slot = TemplateList(template=template)
        elif slot.template_id != template.pk:
            raise ValidationError("This list belongs to another template.")
        slot.label, slot.list_type, slot.weekdays = label[:120], list_type, days
        slot.save()
        slot.reps.set(allowed)
        template.save(update_fields=["updated_at"])
        write_audit(actor, "template.list_saved", template, {"list_id": slot.pk, "weekdays": days, "rep_count": len(allowed)})
    return slot


def remove_template_list(actor, slot):
    _require_manager(actor)
    template = slot.template
    slot.delete()
    template.save(update_fields=["updated_at"])
    write_audit(actor, "template.list_removed", template)


def delete_template(actor, template):
    """Remove a template; uploads that filled its lists keep all their history."""
    _require_manager(actor)
    write_audit(actor, "template.deleted", template, {"name_length": len(template.name)})
    template.delete()
