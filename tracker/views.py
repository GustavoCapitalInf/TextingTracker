"""Browser endpoints. Every data lookup applies role and ownership restrictions."""
from datetime import date, timedelta
from functools import wraps
from hashlib import sha256

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
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_http_methods

from .authentication import is_throttled, register_failure, reset_account_limit, client_ip
from .forms import UploadForm, PublishForm, ListTypeForm
from .account_forms import RepCreationForm, RepMembershipForm
from .accounts import create_rep, reset_rep_password, update_rep_memberships
from .audit_presenter import present_events
from .models import ImportBatch, ImportRow, RepBatch, AuditEvent, RepListMembership
from . import services


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
    form = AuthenticationForm(request, data=request.POST if request.method == 'POST' else None)
    form.fields['username'].widget.attrs.update({'autocomplete': 'username', 'autofocus': True})
    form.fields['password'].widget.attrs.update({'autocomplete': 'current-password'})
    status = 200
    if request.method == 'POST':
        if is_throttled(request):
            form.add_error(None, 'Too many sign-in attempts. Try again in 15 minutes.')
            status = 429
        elif form.is_valid():
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
    return render(request, 'registration/login.html', {'form': form, 'next': request.GET.get('next', request.POST.get('next', ''))}, status=status)


@login_required
@require_POST
def logout_view(request):
    services.write_audit(request.user, 'account.signed_out', request.user)
    logout(request)
    return redirect('login')


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
        try:
            selected = date.fromisoformat(request.GET.get('date', today.isoformat()))
        except ValueError:
            messages.error(request, 'Choose a valid date.')
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
    if not request.user.is_staff:
        context.update(_weekly_schedule(request, today))
    return render(request, 'tracker/dashboard.html', context)


def _weekly_schedule(request, today):
    try:
        anchor = date.fromisoformat(request.GET.get('week', today.isoformat()))
        # Bound navigation arithmetic while permitting ordinary historic schedules.
        if not 1900 <= anchor.year <= 9998:
            raise ValueError
    except (TypeError, ValueError):
        anchor = today
        messages.error(request, 'Choose a valid week.')
    week_start = anchor - timedelta(days=anchor.weekday())
    week_end = week_start + timedelta(days=6)
    batches = _batch_list(RepBatch.objects.filter(
        rep=request.user, upload__list_type__in=_rep_types(request.user),
        status=RepBatch.Status.OPEN, upload__status=ImportBatch.Status.PUBLISHED,
        scheduled_date__range=(week_start, week_end),
    ))
    by_date = {week_start + timedelta(days=day): [] for day in range(7)}
    for batch in batches:
        batch.is_available = batch.scheduled_date == today
        by_date[batch.scheduled_date].append(batch)
    return {
        'week_start': week_start, 'week_end': week_end,
        'previous_week': week_start - timedelta(days=7),
        'next_week': week_start + timedelta(days=7),
        'calendar_days': [
            {'date': day, 'is_today': day == today, 'batches': items}
            for day, items in by_date.items()
        ],
    }


@manager_required
@require_http_methods(['GET', 'POST'])
def upload_new(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            upload = services.import_workbook(form.cleaned_data['file'], request.user, form.cleaned_data['label'], list_type=form.cleaned_data['list_type'])
        except ValidationError as exc:
            form.add_error('file', exc)
        else:
            messages.success(request, 'Spreadsheet checked. Choose dates and reps to create batches.')
            return redirect('upload_detail', upload_id=upload.pk)
    return render(request, 'tracker/upload_form.html', {'form': form})


def _upload_context(request, upload, form=None, preview=None):
    totals = dict(upload.rows.values('classification').annotate(n=Count('pk')).values_list('classification', 'n'))
    counts = {name: totals.get(name, 0) for name in ['ready', 'previous', 'duplicate', 'invalid', 'blocked']}
    counts['total'] = sum(totals.values())
    rows = _page(request, upload.rows.select_related('phone'))
    for row in rows:
        # Historical import messages may contain legacy sales outcomes. Keep
        # those records intact while presenting only assignment information.
        if row.classification in (ImportRow.Classification.PREVIOUS, ImportRow.Classification.BLOCKED):
            row.display_message = 'Phone number uploaded previously. Excluded from new assignments.'
        else:
            row.display_message = row.message
    return {'upload': upload, 'rows': rows,
            'counts': counts, 'form': form if form is not None else PublishForm(list_type=upload.list_type),
            'classification_form': ListTypeForm(),
            'split_preview': preview, 'batches': _batch_list(upload.batches.all())}


@manager_required
def upload_detail(request, upload_id):
    upload = get_object_or_404(ImportBatch.objects.select_related('uploaded_by'), pk=upload_id)
    services.write_audit(request.user, 'upload.viewed', upload)
    return render(request, 'tracker/upload_detail.html', _upload_context(request, upload))


@manager_required
@require_POST
def upload_publish(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
    form = PublishForm(request.POST, list_type=upload.list_type)
    preview = None
    if form.is_valid():
        try:
            if request.POST.get('intent') == 'preview':
                preview = services.preview_split(upload, form.cleaned_data['dates'], list(form.cleaned_data['reps']))
            elif request.POST.get('intent') == 'create':
                services.publish_batches(upload, form.cleaned_data['dates'], list(form.cleaned_data['reps']), request.user)
                messages.success(request, 'Batches created. Each rep can open their list on its assigned date.')
                return redirect('upload_detail', upload_id=upload.pk)
            else:
                form.add_error(None, 'Choose Preview split or Create batches.')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'tracker/upload_detail.html', _upload_context(request, upload, form, preview))


@manager_required
@require_POST
def upload_classify(request, upload_id):
    upload = get_object_or_404(ImportBatch, pk=upload_id)
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
        rep.list_labels = [membership.get_list_type_display() for membership in rep.list_memberships.all()]
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
    rep.list_labels = [membership.get_list_type_display() for membership in rep.list_memberships.all()]
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
