"""Text templates: ready-made messages for situations. Admins add, edit and delete them; reps read them.

No templates ship with the app; an admin adds each one with a name and its text.
"""
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import TextTemplate

NAME_MAX = 80
BODY_MAX = 1600


def _clean(name, body, *, exclude=None):
    name = re.sub(r"\s+", " ", name or "").strip()
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not name:
        raise ValidationError("Give the template a name.")
    if not body:
        raise ValidationError("Add the text reps should send.")
    if len(name) > NAME_MAX:
        raise ValidationError(f"Keep the name to {NAME_MAX} characters or fewer.")
    if len(body) > BODY_MAX:
        raise ValidationError(f"Keep the text to {BODY_MAX:,} characters or fewer.")
    clash = TextTemplate.objects.filter(name__iexact=name)
    if exclude is not None:
        clash = clash.exclude(pk=exclude.pk)
    if clash.exists():
        raise ValidationError(f"A template named “{name}” already exists.")
    return name, body


def _save(item):
    try:
        with transaction.atomic():
            item.save()
    except IntegrityError as error:  # two admins saving the same name at once
        raise ValidationError(f"A template named “{item.name}” already exists.") from error


def create_template(actor, *, name, body):
    from .services import _require_manager, write_audit
    _require_manager(actor)
    name, body = _clean(name, body)
    item = TextTemplate(name=name, body=body, created_by=actor)
    _save(item)
    write_audit(actor, "text_template.created", item, {"name": item.name})
    return item


def update_template(actor, item, *, name, body):
    from .services import _require_manager, write_audit
    _require_manager(actor)
    name, body = _clean(name, body, exclude=item)
    changes = {"renamed": name != item.name, "text_changed": body != item.body}
    item.name, item.body = name, body
    _save(item)
    write_audit(actor, "text_template.updated", item, {"name": item.name, **changes})
    return item


def delete_template(actor, item):
    from .services import _require_manager, write_audit
    _require_manager(actor)
    write_audit(actor, "text_template.deleted", item, {"name": item.name})
    item.delete()
