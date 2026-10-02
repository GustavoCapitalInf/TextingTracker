"""Browser endpoints. Every data lookup applies role and ownership restrictions."""
from datetime import date, timedelta
from functools import wraps
from hashlib import sha256

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import PasswordChangeView, PasswordChangeDoneView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_http_methods

from .appearance import THEME_COOKIE, THEME_COOKIE_AGE, THEMES
from .authentication import is_throttled, register_failure, reset_account_limit, client_ip
from .forms import (
    UploadForm, PublishForm, ListTypeForm, TemplateForm, TemplateListForm, TextingListCreateForm,
    TextingListRepsForm, TextingListSettingsForm, TextTemplateForm,
)
from .account_forms import RepCreationForm, RepMembershipForm
from .accounts import create_rep, reset_rep_password, update_rep_memberships
from .audit_presenter import present_events
from .models import (
    Assignment, ImportBatch, ImportRow, PlanSkip, RepBatch, AuditEvent, RepListMembership, ScheduleTemplate, TemplateList,
    TextingList, TextTemplate,
)
from . import planning, services, text_templates, texting_lists


def manager_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied('This page is available to managers only.')
        return view(request, *args, **kwargs)
    return wrapped


def _page(request, items, per_page=50):
    return Paginator(items, per_page).get_page(request.GET.get('page'))


def _batch_list(queryset):
    return queryset.select_related('rep', 'upload').annotate(
        total_count=Count('assignments'),
    ).order_by('scheduled_date', 'rep__username', 'pk')


def _visible_batch(request, batch_id):
    batches = RepBatch.objects.select_related('rep', 'upload')
    if not request.user.is_staff:
        batches = batches.filter(rep=request.user, upload__list_type__in=_rep_types(request.user))
    return get_object_or_404(batches, pk=batch_id)


def _rep_types(rep):
    return RepListMembership.objects.filter(rep=rep).values_list('list_type', flat=True)


def _active_today(batch):
    return (batch.status == RepBatch.Status.OPEN and batch.upload.status == ImportBatch.Status.PUBLISHED
            and batch.scheduled_date == timezone.localdate() and batch.rep.is_active
            and RepListMembership.objects.filter(rep_id=batch.rep_id, list_type=batch.upload.list_type).exists())


def _batch_revision(batch, user):
    # A changed access state invalidates an already displayed list.
    state = '|'.join([str(batch.pk), batch.status, batch.upload.status,
                      batch.scheduled_date.isoformat(), timezone.localdate().isoformat(),
                      str(batch.rep.is_active), str(user.is_staff), batch.upload.list_type,
                      str(_active_today(batch))])
    return sha256(state.encode()).hexdigest()[:24]


@require_http_methods(['GET', 'POST'])
def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    throttled = request.method == 'POST' and is_throttled(request)
    if throttled:
        # Never bind or validate the form here: authenticate() would run and the page
        # would differ for a correct password, turning the lockout into a guessing oracle.
        form = AuthenticationForm(request, initial={'username': request.POST.get('username', '')[:150]})
    else:
        form = AuthenticationForm(request, data=request.POST if request.method == 'POST' else None)
    form.fields['username'].widget.attrs.update({'autocomplete': 'username', 'autofocus': True})
    form.fields['password'].widget.attrs.update({'autocomplete': 'current-password'})
    status = 429 if throttled else 200
    if request.method == 'POST' and not throttled:
        if form.is_valid():
            user = form.get_user()
            reset_account_limit(request)
            login(request, user)
            services.write_audit(user, 'account.signed_in', user, {'ip': client_ip(request)})
            destination = request.POST.get('next', '') or request.GET.get('next', '')
            if not url_has_allowed_host_and_scheme(destination, {request.get_host()}, require_https=request.is_secure()):
                destination = ''
            return redirect(destination or 'dashboard')
        else:
            register_failure(request)
            services.write_audit(None, 'account.sign_in_failed', None, {'ip': client_ip(request)})
    return render(request, 'registration/login.html', {'form': form, 'throttled': throttled, 'next': request.GET.get('next', request.POST.get('next', ''))}, status=status)


@login_required
@require_POST
def logout_view(request):
    services.write_audit(request.user, 'account.signed_out', request.user)
    logout(request)
    return redirect('login')


@require_POST
def appearance(request):
    """Remember Dark, Light or Auto for this browser. Open to signed-out visitors for the sign-in page."""
    theme = request.POST.get('theme', '')
    valid = theme in THEMES
    if request.headers.get('Accept') == 'application/json':
        response = JsonResponse({'theme': theme} if valid else {'error': 'Choose Dark, Light or Auto.'}, status=200 if valid else 400)
    else:
        destination = request.POST.get('next', '')
        if not url_has_allowed_host_and_scheme(destination, {request.get_host()}, require_https=request.is_secure()):
            destination = ''
        response = redirect(destination or 'dashboard')
    if valid:
        response.set_cookie(THEME_COOKIE, theme, max_age=THEME_COOKIE_AGE, secure=settings.SESSION_COOKIE_SECURE,
                            httponly=True, samesite='Lax')
    return response


@method_decorator(manager_required, name='dispatch')
class PasswordChange(PasswordChangeView):
    template_name = 'registration/password_change_form.html'
    success_url = reverse_lazy('password_change_done')

    def form_valid(self, form):
        response = super().form_valid(form)
        services.write_audit(self.request.user, 'account.password_changed', self.request.user)
        return response


@method_decorator(manager_required, name='dispatch')
class PasswordChangeDone(PasswordChangeDoneView):
    template_name = 'registration/password_change_done.html'


@login_required
def dashboard(request):
    today = timezone.localdate()
    selected = today
    if request.user.is_staff:
        selected = _schedule_anchor(request, 'date', today, 'Choose a valid date.')
    query = RepBatch.objects.filter(scheduled_date=selected, status=RepBatch.Status.OPEN,
                                     upload__status=ImportBatch.Status.PUBLISHED)
    if not request.user.is_staff:
        query = query.filter(rep=request.user, upload__list_type__in=_rep_types(request.user))
    batches = list(_batch_list(query))
    stats = {'assigned': sum(b.total_count for b in batches), 'batches': len(batches)}
    uploads = ImportBatch.objects.select_related('uploaded_by')[:12] if request.user.is_staff else []
    context = {
        'today': today, 'selected_date': selected, 'batches': batches, 'uploads': uploads,
        'stats': stats,
        'has_reps': get_user_model().objects.filter(is_active=True, is_staff=False, is_superuser=False, list_memberships__isnull=False).exists(),
    }
    if request.user.is_staff:
        context.update(_manager_schedule(selected, today))
    else:
        context.update(_weekly_schedule(request, today))
    return render(request, 'tracker/dashboard.html', context)


def _schedule_anchor(request, param, today, error):
    try:
        anchor = date.fromisoformat(request.GET.get(param, today.isoformat()))
        # Bound navigation arithmetic while permitting ordinary historic schedules.
        if not 1900 <= anchor.year <= 9998:
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, error)
        return today
    return anchor


def _calendar_week(week_start, today, entries, selected=None):
    by_date = {week_start + timedelta(days=day): [] for day in range(7)}
    for day, entry in entries:
        by_date[day].append(entry)
    return {
        'week_start': week_start, 'week_end': week_start + timedelta(days=6),
        'calendar_days': [
            {'date': day, 'is_today': day == today, 'is_selected': day == selected, 'batches': items}
            for day, items in by_date.items()
        ],
    }


def _weekly_schedule(request, today):
    anchor = _schedule_anchor(request, 'week', today, 'Choose a valid week.')
    week_start = anchor - timedelta(days=anchor.weekday())
    batches = _batch_list(RepBatch.objects.filter(
        rep=request.user, upload__list_type__in=_rep_types(request.user),
        status=RepBatch.Status.OPEN, upload__status=ImportBatch.Status.PUBLISHED,
        scheduled_date__range=(week_start, week_start + timedelta(days=6)),
    ))
    for batch in batches:
        batch.is_available = batch.scheduled_date == today
    context = _calendar_week(week_start, today, [(batch.scheduled_date, batch) for batch in batches])
    context.update(previous_week=week_start - timedelta(days=7), next_week=week_start + timedelta(days=7))
    return context


def _manager_schedule(selected, today):
    """Every rep's open lists for the selected date's week, one entry per upload and day."""
    week_start = selected - timedelta(days=selected.weekday())
    lists = list(RepBatch.objects.filter(
        status=RepBatch.Status.OPEN, upload__status=ImportBatch.Status.PUBLISHED,
        scheduled_date__range=(week_start, week_start + timedelta(days=6)),
    ).values('scheduled_date', 'upload_id', 'upload__label', 'upload__list_type').annotate(
        rep_count=Count('rep', distinct=True), number_count=Count('assignments'),
    ).order_by('scheduled_date', 'upload__label', 'upload_id'))
    for entry in lists:
        entry['list_type_label'] = texting_lists.short_label(entry['upload__list_type'])
    context = _calendar_week(week_start, today, [(entry['scheduled_date'], entry) for entry in lists], selected)
    plan = planning.planned_for_week(week_start, today)
    for day in context['calendar_days']:
        day['planned'] = plan.get(day['date'], [])
    context.update(previous_week=selected - timedelta(days=7), next_week=selected + timedelta(days=7),
                   has_active_template=ScheduleTemplate.objects.filter(is_active=True).exists(),
                   skipped=planning.skipped_for_week(week_start))
    return context


@manager_required
@require_http_methods(['GET', 'POST'])
def upload_new(request):
    source = request.POST if request.method == 'POST' else request.GET
    slot, week = planning.planned_slot(source.get('slot'), source.get('week'), timezone.localdate())
    initial = {'label': slot.label, 'list_type': slot.list_type} if slot else None
    form = UploadForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        try:
            upload = services.import_workbook(form.cleaned_data['file'], request.user, form.cleaned_data['label'], list_type=form.cleaned_data['list_type'])
        except ValidationError as exc:
            form.add_error('file', exc)
        else:
            if slot:
                planning.link_upload(upload, slot, week, request.user)
                if upload.list_type != slot.list_type:
                    messages.warning(request, f'Heads up: the planned “{slot.label}” list is for {slot.list_type_label}, but this file went to {upload.list_type_label}. It still counts as that planned list. If that wasn’t intended, clear this upload and upload it again.')
            messages.success(request, 'Spreadsheet checked. Choose dates and reps to create batches.')
            return redirect('upload_detail', upload_id=upload.pk)
    planned_days = [day for day in slot.days_in_week(week) if day >= timezone.localdate()] if slot else []
    already_uploaded = planning.uploads_for_plan(slot, week) if slot else []
    return render(request, 'tracker/upload_form.html', {'form': form, 'slot': slot, 'week': week,
                                                        'planned_days': planned_days, 'already_uploaded': already_uploaded})


def _describe_previous_rows(upload, rows):
    # Historical import messages may contain legacy sales outcomes. Keep those
    # records intact and rebuild the warning from upload and assignment history.
    previous = (ImportRow.Classification.PREVIOUS, ImportRow.Classification.BLOCKED)
    phone_ids = {row.phone_id for row in rows if row.classification in previous and row.phone_id}
    first_uploads, last_sent = {}, {}
    if phone_ids:
        for item in ImportRow.objects.filter(phone_id__in=phone_ids).exclude(upload=upload).order_by(
                'phone_id', 'upload__created_at', 'pk').values('phone_id', 'upload__label', 'upload__created_at'):
            first_uploads.setdefault(item['phone_id'], item)
        for item in Assignment.objects.filter(phone_id__in=phone_ids).exclude(batch__upload=upload).order_by(
                'phone_id', '-batch__scheduled_date', '-pk').values(
                'phone_id', 'batch__scheduled_date', 'batch__status',
                'batch__rep__first_name', 'batch__rep__last_name', 'batch__rep__username'):
            last_sent.setdefault(item['phone_id'], item)
    today = timezone.localdate()
    for row in rows:
        if row.classification not in previous:
            row.display_message = row.message
            continue
        first = first_uploads.get(row.phone_id)
        if first:
            uploaded_on = timezone.localtime(first['upload__created_at']).date()
            parts = [f"First uploaded {uploaded_on:%b} {uploaded_on.day}, {uploaded_on.year} in “{first['upload__label']}”."]
        else:
            parts = ['Phone number uploaded previously.']
        sent = last_sent.get(row.phone_id)
        if sent:
            rep = ' '.join(filter(None, [sent['batch__rep__first_name'], sent['batch__rep__last_name']])) or sent['batch__rep__username']
            day = sent['batch__scheduled_date']
            verb = 'Scheduled for' if day > today else 'Sent to'
            cleared = ' (list cleared)' if sent['batch__status'] == RepBatch.Status.CLOSED else ''
            parts.append(f"{verb} {rep} on {day:%a}, {day:%b} {day.day}, {day.year}{cleared}.")
        else:
            parts.append('Not sent to a rep yet.')
        row.display_message = ' '.join(parts)


def _upload_context(request, upload, form=None, preview=None):
    totals = dict(upload.rows.values('classification').annotate(n=Count('pk')).values_list('classification', 'n'))
    counts = {name: totals.get(name, 0) for name in ['ready', 'previous', 'duplicate', 'invalid', 'blocked']}
    counts['total'] = sum(totals.values())
    rows = _page(request, upload.rows.select_related('phone'))
    _describe_previous_rows(upload, rows)
    reusable = services.reusable_previous_rows(upload).count()
    open_draft = upload.status == ImportBatch.Status.DRAFT and bool(upload.list_type) and not upload.batches.exists()
    prefill, plan_notes = planning.split_prefill(upload, timezone.localdate()) if open_draft else (None, [])
    if form is None:
        form = PublishForm(list_type=upload.list_type, initial=prefill)
    return {'upload': upload, 'rows': rows,
            'reusable_previous': reusable,
            'needs_previous_decision': open_draft and reusable > 0 and upload.include_previous is None,
            'can_change_previous': open_draft and reusable > 0,
            'assignable_count': services.assignable_rows(upload).count() if open_draft else None,
            'plan_notes': plan_notes,
            'counts': counts, 'form': form,
            'classification_form': ListTypeForm(),
            'split_preview': preview, 'batches': _batch_list(upload.batches.all())}


@manager_required
def upload_detail(request, upload_id):
    upload = get_object_or_404(ImportBatch.objects.select_related('uploaded_by'), pk=upload_id)
    services.write_audit(request.user, 'upload.viewed', upload)
    return render(request, 'tracker/upload_detail.html', _upload_context(request, upload))


def _back_to_upload(request, upload):
    """Links on a page rendered by a form post (Next page, appearance switch) land back on the upload."""
    page = request.GET.get('page', '')
    return redirect(upload.get_absolute_url() + (f'?page={page}' if page.isdigit() else ''))


@manager_required
@require_http_methods(['GET', 'POST'])
def upload_publish(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
    if request.method == 'GET':
        return _back_to_upload(request, upload)
    form = PublishForm(request.POST, list_type=upload.list_type)
    preview = None
    if form.is_valid():
        try:
            if request.POST.get('intent') == 'preview':
                preview = services.preview_split(upload, form.cleaned_data['dates'], list(form.cleaned_data['reps']))
            elif request.POST.get('intent') == 'create':
                services.publish_batches(upload, form.cleaned_data['dates'], list(form.cleaned_data['reps']), request.user)
                messages.success(request, 'Lists created and added to each rep’s account for their assigned days. No link is needed.')
                return redirect('upload_detail', upload_id=upload.pk)
            else:
                form.add_error(None, 'Choose Preview split or Create batches.')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'tracker/upload_detail.html', _upload_context(request, upload, form, preview))


@manager_required
@require_POST
def upload_previous(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
    choice = request.POST.get('include')
    if choice not in ('yes', 'no'):
        messages.error(request, 'Choose Yes or No for the previously uploaded numbers.')
    else:
        try:
            services.decide_previous_numbers(upload, choice == 'yes', request.user)
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        else:
            messages.success(request, 'Previously uploaded numbers will be included in this list.' if choice == 'yes'
                             else 'Previously uploaded numbers will be left out of this list.')
    return redirect('upload_detail', upload_id=upload.pk)


@manager_required
@require_http_methods(['GET', 'POST'])
def upload_classify(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
    if request.method == 'GET':
        return _back_to_upload(request, upload)
    form = ListTypeForm(request.POST)
    if form.is_valid():
        try:
            services.classify_upload(upload, form.cleaned_data['list_type'], request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'List type set. Choose dates and eligible reps below.')
            return redirect('upload_detail', upload_id=upload.pk)
    context = _upload_context(request, upload)
    context['classification_form'] = form
    return render(request, 'tracker/upload_detail.html', context)


@manager_required
@require_POST
def upload_close(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
    services.close_upload(upload, request.user)
    messages.success(request, 'Upload cleared from every rep’s queue. All history is retained.')
    return redirect('upload_detail', upload_id=upload.pk)


@login_required
def batch_detail(request, batch_id):
    batch = _visible_batch(request, batch_id)
    manager = request.user.is_staff
    active = _active_today(batch)
    query = batch.assignments.select_related('phone')
    available = manager or active
    availability_message = ''
    if not available:
        query = query.none()
        if batch.status == RepBatch.Status.CLOSED or batch.upload.status == ImportBatch.Status.CLOSED:
            availability_message = 'This list has been cleared by your manager.'
        elif batch.scheduled_date > timezone.localdate():
            availability_message = 'This batch will be available on its assigned date.'
        else:
            availability_message = 'This list’s assigned date has passed.'
    assignments = _page(request, query)
    total = batch.assignments.count()
    services.write_audit(request.user, 'batch.viewed', batch, {'numbers_displayed': len(assignments), 'page': assignments.number})
    return render(request, 'tracker/batch_detail.html', {
        'batch': batch, 'assignments': assignments,
        'batch_revision': _batch_revision(batch, request.user),
        'can_view_numbers': available,
        'availability_message': availability_message,
        'stats': {'assigned': total},
    })


@login_required
def batch_status(request, batch_id):
    batch = _visible_batch(request, batch_id)
    active = _active_today(batch)
    return JsonResponse({'active': active, 'revision': _batch_revision(batch, request.user),
                         'reason': 'This batch has changed. Refresh to see its current status.'})


@manager_required
@require_POST
def batch_close(request, batch_id):
    batch = get_object_or_404(RepBatch, pk=batch_id)
    services.close_batch(batch, request.user)
    messages.success(request, 'Batch cleared. All assignment history is retained.')
    return redirect('upload_detail', upload_id=batch.upload_id)


@manager_required
def audit_log(request):
    events = AuditEvent.objects.select_related('actor').exclude(action__startswith='call.')
    page = _page(request, events)
    page.object_list = present_events(page.object_list)
    return render(request, 'tracker/audit_log.html', {'events': page})


@manager_required
@require_http_methods(['GET', 'POST'])
def team(request):
    form = RepCreationForm(request.POST if request.method == 'POST' and request.POST.get('action') == 'create' else None)
    credential = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create' and form.is_valid():
            try:
                rep, password = create_rep(request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, ValidationError(exc.messages))
            else:
                credential = _credential(rep, password)
                form = RepCreationForm()
                messages.success(request, 'Rep account created. Share these credentials privately.')
        elif action in ('enable', 'disable'):
            try:
                rep_id = int(request.POST.get('user_id', ''))
            except (TypeError, ValueError):
                raise PermissionDenied('Choose a valid representative.')
            _set_rep_access(request.user, rep_id, action == 'enable')
            messages.success(request, 'Rep access updated. Their assignments and history are retained.')
            return redirect('team')
        elif action not in ('create', 'enable', 'disable'):
            form = RepCreationForm(request.POST)
            form.add_error(None, 'Choose a valid account action.')
    reps = list(get_user_model().objects.prefetch_related('list_memberships').order_by('-is_staff', 'first_name', 'last_name', 'username'))
    for rep in reps:
        rep.list_labels = [membership.label for membership in rep.list_memberships.all()]
    return render(request, 'tracker/team.html', {'form': form, 'reps': reps, 'credential': credential})


def _credential(rep, password):
    # One response only: never persist a generated password in a session or audit event.
    return {'name': rep.get_full_name() or rep.username, 'username': rep.username, 'password': password}


def _set_rep_access(actor, rep_id, active):
    with services._phone_transaction():
        rep = get_object_or_404(get_user_model().objects.select_for_update(), pk=rep_id, is_staff=False, is_superuser=False)
        if rep.is_active != active:
            rep.is_active = active
            rep.save(update_fields=['is_active'])
            services.write_audit(actor, 'account.rep_enabled' if active else 'account.rep_disabled', rep)


@manager_required
@require_http_methods(['GET', 'POST'])
def team_detail(request, rep_id):
    rep = get_object_or_404(get_user_model().objects.prefetch_related('list_memberships'), pk=rep_id)
    rep.list_labels = [membership.label for membership in rep.list_memberships.all()]
    is_rep = not rep.is_staff and not rep.is_superuser
    action = request.POST.get('action') if request.method == 'POST' else None
    form = RepMembershipForm(request.POST if action == 'update_memberships' else None, rep=rep) if is_rep else None
    credential = None
    if request.method == 'POST':
        if not is_rep:
            raise PermissionDenied('Administrator accounts cannot be changed here.')
        if action == 'update_memberships':
            if form.is_valid():
                try:
                    update_rep_memberships(request.user, rep, form.cleaned_data['list_types'])
                except ValidationError as exc:
                    form.add_error(None, ValidationError(exc.messages))
                else:
                    messages.success(request, 'List memberships updated.')
                    return redirect('team_detail', rep_id=rep.pk)
        elif action == 'reset_password':
            credential = _credential(rep, reset_rep_password(request.user, rep))
            messages.success(request, 'New password generated. The previous password no longer works.')
        elif action in ('enable', 'disable'):
            _set_rep_access(request.user, rep.pk, action == 'enable')
            messages.success(request, 'Account access updated. Assignments and history are retained.')
            return redirect('team_detail', rep_id=rep.pk)
        else:
            form = RepMembershipForm(request.POST, rep=rep)
            form.add_error(None, 'Choose a valid account action.')
    login_events = _page(request, AuditEvent.objects.filter(action='account.signed_in', actor=rep))
    return render(request, 'tracker/team_detail.html', {
        'rep': rep, 'is_rep_account': is_rep, 'membership_form': form,
        'login_events': login_events, 'credential': credential,
    })


def _error(request, status, heading, message):
    return render(request, 'tracker/error.html', {'status': status, 'heading': heading, 'message': message}, status=status)


def error_400(request, exception=None):
    return _error(request, 400, 'Check your request', 'This request could not be processed. Return to your workspace and try again.')


def error_403(request, exception=None):
    return _error(request, 403, 'Access restricted', 'Your account does not have access to this action.')


def error_404(request, exception=None):
    return _error(request, 404, 'Page unavailable', 'This page does not exist or is not assigned to your account.')


def error_500(request):
    return _error(request, 500, 'Something went wrong', 'Please try again. If the problem continues, contact your administrator.')


@manager_required
@require_http_methods(['GET', 'POST'])
def calendar_management(request):
    form = TemplateForm(request.POST if request.POST.get('action') == 'create' else None, initial={'is_active': False})
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'suggest':
                template = planning.suggest_template(request.user)
                messages.success(request, 'Weekly plan built from your last 4 weeks. Check the lists, then turn it on.')
                return redirect(template)
            if action == 'create' and form.is_valid():
                template = planning.create_template(request.user, form.cleaned_data['name'])
                messages.success(request, 'Weekly plan created. Now add the lists you send each week.')
                return redirect(template)
            if action not in ('suggest', 'create'):
                messages.error(request, 'Choose a valid weekly plan action.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    templates = ScheduleTemplate.objects.prefetch_related('lists')
    return render(request, 'tracker/calendar_management.html', {'templates': templates, 'form': form})


@manager_required
@require_http_methods(['GET', 'POST'])
def calendar_plan(request, template_id):
    template = get_object_or_404(ScheduleTemplate, pk=template_id)
    action = request.POST.get('action') if request.method == 'POST' else None
    list_id = request.POST.get('list_id', '')
    settings_form = TemplateForm(request.POST if action == 'update_template' else None,
                                 initial={'name': template.name, 'is_active': template.is_active})
    new_form = TemplateListForm(request.POST if action == 'add_list' else None, prefix='new')
    if action == 'update_template' and settings_form.is_valid():
        try:
            planning.update_template(request.user, template, **settings_form.cleaned_data)
        except ValidationError as exc:
            settings_form.add_error(None, exc)
        else:
            messages.success(request, 'Weekly plan turned on. Its lists now show up as reminders on the calendar.' if template.is_active else 'Weekly plan saved. It’s off, so it shows no reminders on the calendar.')
            return redirect(template)
    elif action in ('add_list', 'save_list'):
        slot = None
        bound = new_form
        if action == 'save_list':
            slot = get_object_or_404(TemplateList, pk=list_id if list_id.isdecimal() else 0, template=template)
            bound = TemplateListForm(request.POST, prefix=f'list-{slot.pk}')
        if bound.is_valid():
            try:
                planning.save_template_list(request.user, template, slot=slot, **bound.cleaned_data)
            except ValidationError as exc:
                bound.add_error(None, exc)
            else:
                messages.success(request, 'List saved.')
                return redirect(template)
        if slot is not None:
            slot.bound_form = bound
    elif action == 'remove_list':
        slot = get_object_or_404(TemplateList, pk=list_id if list_id.isdecimal() else 0, template=template)
        planning.remove_template_list(request.user, slot)
        messages.success(request, 'List removed from the weekly plan.')
        return redirect(template)
    elif action == 'delete_template':
        planning.delete_template(request.user, template)
        messages.success(request, 'Weekly plan deleted. Lists you already sent are unchanged.')
        return redirect('calendar_management')
    elif action is not None and action != 'update_template':
        messages.error(request, 'Choose a valid weekly plan action.')
    slots = list(template.lists.prefetch_related('reps'))
    for slot in slots:
        if not hasattr(slot, 'bound_form'):
            slot.bound_form = TemplateListForm(prefix=f'list-{slot.pk}', initial={
                'label': slot.label, 'list_type': slot.list_type, 'weekdays': slot.weekdays,
                'reps': [rep.pk for rep in slot.reps.all()],
            })
    return render(request, 'tracker/calendar_plan.html', {
        'template': template, 'settings_form': settings_form, 'slots': slots, 'new_form': new_form,
    })


@manager_required
@require_POST
def plan_skip(request):
    today = timezone.localdate()
    slot, _ = planning.planned_slot(request.POST.get('slot'), request.POST.get('day'), today)
    try:
        day = date.fromisoformat(request.POST.get('day', ''))
        if slot is None:
            raise ValidationError('This planned list is no longer available.')
        planning.skip_planned_day(request.user, slot, day)
    except ValueError:
        messages.error(request, 'Choose a valid day to skip.')
        return redirect('dashboard')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
        return redirect('dashboard')
    messages.success(request, f'Skipped {slot.label} on {day:%a}, {day:%b} {day.day}. Use Undo below the calendar to bring it back.')
    return redirect(reverse('dashboard') + f'?date={day.isoformat()}')


@manager_required
@require_POST
def plan_unskip(request, skip_id):
    skip = get_object_or_404(PlanSkip, pk=skip_id)
    day = skip.day
    planning.unskip_planned_day(request.user, skip)
    messages.success(request, f'{skip.label} is planned again on {day:%a}, {day:%b} {day.day}.')
    return redirect(reverse('dashboard') + f'?date={day.isoformat()}')


@manager_required
@require_http_methods(['GET', 'POST'])
def texting_lists_index(request):
    form = TextingListCreateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            item = texting_lists.create_list(request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f'Texting list “{item.name}” created. It’s ready to pick when uploading.')
            return redirect(item)
    reps = dict(RepListMembership.objects.filter(rep__is_active=True).values('list_type').annotate(n=Count('pk')).values_list('list_type', 'n'))
    uploads = dict(ImportBatch.objects.values('list_type').annotate(n=Count('pk')).values_list('list_type', 'n'))
    items = list(TextingList.objects.all())
    for item in items:
        item.rep_count, item.upload_count = reps.get(item.key, 0), uploads.get(item.key, 0)
    return render(request, 'tracker/texting_lists.html', {'items': items, 'form': form})


@manager_required
@require_http_methods(['GET', 'POST'])
def texting_list_detail(request, list_id):
    item = get_object_or_404(TextingList, pk=list_id)
    action = request.POST.get('action') if request.method == 'POST' else None
    settings_form = TextingListSettingsForm(request.POST if action == 'update' else None,
                                            initial={'name': item.name, 'nickname': item.nickname, 'is_active': item.is_active})
    members = list(RepListMembership.objects.filter(list_type=item.key).values_list('rep_id', flat=True))
    reps_form = TextingListRepsForm(request.POST if action == 'members' else None, initial={'reps': members})
    try:
        if action == 'update' and settings_form.is_valid():
            texting_lists.update_list(request.user, item, **settings_form.cleaned_data)
            messages.success(request, 'Texting list saved.' if item.is_active else 'Texting list saved. It’s hidden from new uploads and weekly plans.')
            return redirect(item)
        if action == 'members' and reps_form.is_valid():
            added, removed = texting_lists.set_members(request.user, item, reps_form.cleaned_data['reps'])
            messages.success(request, f'Reps updated: {added} added, {removed} removed.' if added or removed else 'No changes to the reps.')
            return redirect(item)
        if action == 'delete':
            name = item.name
            texting_lists.delete_list(request.user, item)
            messages.success(request, f'Texting list “{name}” deleted.')
            return redirect('texting_lists_index')
        if action not in (None, 'update', 'members'):
            messages.error(request, 'Choose a valid texting list action.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    usage = texting_lists.usage(item)
    return render(request, 'tracker/texting_list_detail.html', {
        'item': item, 'settings_form': settings_form, 'reps_form': reps_form, 'usage': usage,
        'can_delete': not any(usage.values()), 'member_count': len(members),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def text_templates_index(request):
    """Every signed-in rep can read and copy templates; only admins add them."""
    form = TextTemplateForm(request.POST or None) if request.user.is_staff else None
    if request.method == 'POST':
        if not request.user.is_staff:
            raise PermissionDenied('Only admins can add templates.')
        if form.is_valid():
            try:
                item = text_templates.create_template(request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                messages.success(request, f'Template “{item.name}” added. Reps can see it now.')
                return redirect('text_templates')
    return render(request, 'tracker/text_templates.html', {'items': TextTemplate.objects.all(), 'form': form})


@manager_required
@require_http_methods(['GET', 'POST'])
def text_template_detail(request, template_id):
    item = get_object_or_404(TextTemplate, pk=template_id)
    action = request.POST.get('action') if request.method == 'POST' else None
    form = TextTemplateForm(request.POST if action == 'update' else None, initial={'name': item.name, 'body': item.body})
    if action == 'update' and form.is_valid():
        try:
            text_templates.update_template(request.user, item, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f'Template “{item.name}” saved.')
            return redirect('text_templates')
    elif action == 'delete':
        name = item.name
        text_templates.delete_template(request.user, item)
        messages.success(request, f'Template “{name}” deleted.')
        return redirect('text_templates')
    elif action not in (None, 'update'):
        messages.error(request, 'Choose a valid template action.')
    if action == 'update':
        item.refresh_from_db()
    return render(request, 'tracker/text_template_detail.html', {'item': item, 'form': form})

