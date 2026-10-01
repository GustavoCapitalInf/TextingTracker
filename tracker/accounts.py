"""Administrator-managed rep credentials and texting-list memberships."""

import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError

from .models import RepListMembership, TextingListType
from .services import _phone_transaction, _require_manager, write_audit


def validate_list_types(list_types):
    if isinstance(list_types, (str, bytes)):
        raise ValidationError("Choose at least one valid texting list.")
    try:
        values = set(list_types)
    except (TypeError, ValueError):
        raise ValidationError("Choose at least one valid texting list.")
    if not values or not values.issubset(set(TextingListType.values)):
        raise ValidationError("Choose at least one valid texting list.")
    return sorted(values)


def _locked_rep(rep):
    if not getattr(rep, "pk", None):
        raise ValidationError("Choose an existing representative.")
    current = get_user_model().objects.select_for_update().filter(pk=rep.pk).first()
    if current is None:
        raise ValidationError("Choose an existing representative.")
    if current.is_staff or current.is_superuser:
        raise PermissionDenied("This action is available for representative accounts only.")
    return current


def _generated_password(user):
    password = secrets.token_urlsafe(18)
    validate_password(password, user)
    return password


def _replace_memberships(rep, list_types):
    current = set(rep.list_memberships.values_list("list_type", flat=True))
    desired = set(list_types)
    if current == desired:
        return None
    rep.list_memberships.exclude(list_type__in=desired).delete()
    RepListMembership.objects.bulk_create([
        RepListMembership(rep=rep, list_type=value) for value in sorted(desired - current)
    ])
    return sorted(current)


def create_rep(actor, first_name, last_name, username, list_types):
    """Return the created user and a generated password; never persist plaintext."""
    _require_manager(actor)
    selected = validate_list_types(list_types)
    with _phone_transaction():
        User = get_user_model()
        user = User(
            first_name=(first_name or "").strip(), last_name=(last_name or "").strip(),
            username=User.normalize_username((username or "").strip()),
            is_staff=False, is_superuser=False, is_active=True,
        )
        if not user.first_name or not user.last_name or not user.username:
            raise ValidationError("First name, last name, and username are required.")
        if User.objects.filter(username__iexact=user.username).exists():
            raise ValidationError("This username is already in use.")
        password = _generated_password(user)
        user.set_password(password)
        user.full_clean()
        user.save()
        _replace_memberships(user, selected)
        write_audit(actor, "account.rep_created", user, {"list_types": selected})
    return user, password


def reset_rep_password(actor, rep):
    """Return a newly generated rep password; Django invalidates prior sessions."""
    _require_manager(actor)
    with _phone_transaction():
        user = _locked_rep(rep)
        password = _generated_password(user)
        user.set_password(password)
        user.save(update_fields=["password"])
        write_audit(actor, "account.rep_password_reset", user)
    return password


def update_rep_memberships(actor, rep, list_types):
    """Replace list eligibility without changing existing batches or account access."""
    _require_manager(actor)
    selected = validate_list_types(list_types)
    with _phone_transaction():
        user = _locked_rep(rep)
        previous = _replace_memberships(user, selected)
        if previous is not None:
            write_audit(actor, "account.rep_memberships_changed", user, {
                "previous_list_types": previous, "list_types": selected,
            })
    return user
