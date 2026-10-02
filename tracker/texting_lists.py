"""Texting lists (such as GFS and Ringcentral): lookups and admin management.

Lists are looked up at most once per request (see TextingListMiddleware), so
labels on long pages cost one query. Outside a request every lookup reads the
database, which keeps management commands and services always current.
"""

import contextvars

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from .models import ImportBatch, PlanSkip, RepListMembership, TemplateList, TextingList


_request_memo = contextvars.ContextVar("texting_lists", default=None)


class TextingListMiddleware:
    """Remember the texting lists for the duration of one request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request_memo.set({})
        try:
            return self.get_response(request)
        finally:
            _request_memo.reset(token)


def all_lists():
    memo = _request_memo.get()
    if memo is not None and "lists" in memo:
        return memo["lists"]
    lists = {item.key: item for item in TextingList.objects.all()}
    if memo is not None:
        memo["lists"] = lists
    return lists


def forget():
    memo = _request_memo.get()
    if memo is not None:
        memo.pop("lists", None)


def get(key):
    return all_lists().get(key)


def label(key, default="Not selected"):
    item = get(key) if key else None
    return item.label if item else default


def short_label(key, default="Not selected"):
    item = get(key) if key else None
    return item.short_label if item else default


def active_keys():
    return {key for key, item in all_lists().items() if item.is_active}


def active_choices():
    """(key, label) pairs for pickers; hidden lists are left out."""
    return [(item.key, item.label) for item in sorted(all_lists().values(), key=lambda item: item.name.casefold()) if item.is_active]


def rep_queryset():
    return get_user_model().objects.filter(is_active=True, is_staff=False, is_superuser=False)


def usage(item):
    """How much history points at a list; a list with any history can only be hidden."""
    return {
        "uploads": ImportBatch.objects.filter(list_type=item.key).count(),
        "templates": TemplateList.objects.filter(list_type=item.key).count(),
        "skips": PlanSkip.objects.filter(list_type=item.key).count(),
    }


def _clean_name(name, nickname, exclude=None):
    name = " ".join((name or "").split())
    nickname = " ".join((nickname or "").split())
    if not name:
        raise ValidationError("Give the texting list a name.")
    if len(name) > 80 or len(nickname) > 40:
        raise ValidationError("Keep the name under 80 characters and the nickname under 40.")
    duplicate = TextingList.objects.filter(name__iexact=name)
    if exclude is not None:
        duplicate = duplicate.exclude(pk=exclude.pk)
    if duplicate.exists():
        raise ValidationError("A texting list with this name already exists.")
    return name, nickname


def _new_key(name):
    base = (slugify(name) or "list")[:56].strip("-") or "list"
    key, number = base, 2
    while TextingList.objects.filter(key=key).exists():
        key, number = f"{base}-{number}", number + 1
    return key


def _set_members(item, reps):
    """Make exactly these active reps members of the list; returns (added, removed) counts.

    Disabled reps aren't on the form, so their memberships are left alone: re-enabling a rep
    restores their lists and any upcoming batches."""
    wanted = {rep.pk for rep in reps}
    current = set(RepListMembership.objects.filter(list_type=item.key, rep__in=rep_queryset()).values_list("rep_id", flat=True))
    RepListMembership.objects.filter(list_type=item.key, rep_id__in=current - wanted).delete()
    RepListMembership.objects.bulk_create([RepListMembership(rep_id=rep_id, list_type=item.key) for rep_id in sorted(wanted - current)])
    return len(wanted - current), len(current - wanted)


def _require_reps(reps):
    reps = list(reps)
    allowed = set(rep_queryset().filter(pk__in=[rep.pk for rep in reps]).values_list("pk", flat=True))
    if len(allowed) != len({rep.pk for rep in reps}):
        raise ValidationError("Only active sales reps can be on a texting list.")
    return reps


def create_list(actor, *, name, nickname="", reps=()):
    from .services import _phone_transaction, _require_manager, write_audit
    _require_manager(actor)
    reps = _require_reps(reps)
    with _phone_transaction():
        name, nickname = _clean_name(name, nickname)
        item = TextingList.objects.create(key=_new_key(name), name=name, nickname=nickname, created_by=actor)
        _set_members(item, reps)
        write_audit(actor, "texting_list.created", item, {"key": item.key, "rep_count": len(reps)})
    forget()
    return item


def update_list(actor, item, *, name, nickname, is_active):
    from .services import _phone_transaction, _require_manager, write_audit
    _require_manager(actor)
    with _phone_transaction():
        name, nickname = _clean_name(name, nickname, exclude=item)
        changes = {"renamed": name != item.name or nickname != item.nickname, "is_active": bool(is_active)}
        item.name, item.nickname, item.is_active = name, nickname, bool(is_active)
        item.save(update_fields=["name", "nickname", "is_active"])
        write_audit(actor, "texting_list.updated", item, {"key": item.key, **changes})
    forget()
    return item


def set_members(actor, item, reps):
    """Replace a list's reps. Removed reps lose access to that list's batches immediately."""
    from .services import _phone_transaction, _require_manager, write_audit
    _require_manager(actor)
    reps = _require_reps(reps)
    with _phone_transaction():
        added, removed = _set_members(item, reps)
        if added or removed:
            write_audit(actor, "texting_list.members_changed", item, {"key": item.key, "added": added, "removed": removed})
    return added, removed


def delete_list(actor, item):
    """Delete a list that was never used. Lists with history can only be hidden."""
    from .services import _phone_transaction, _require_manager, write_audit
    _require_manager(actor)
    with _phone_transaction():
        if any(usage(item).values()):
            raise ValidationError("This list has uploads or templates, so it can’t be deleted. Hide it instead.")
        RepListMembership.objects.filter(list_type=item.key).delete()
        write_audit(actor, "texting_list.deleted", item, {"key": item.key})
        item.delete()
    forget()

