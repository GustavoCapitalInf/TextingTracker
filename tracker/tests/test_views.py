from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from tracker.models import Assignment, AuditEvent, ImportBatch, PhoneNumber, RepBatch, SecurityThrottle, RepListMembership, TextingListType
from tracker.services import import_workbook, publish_batches, close_batch, start_call, record_outcome


def sheet(numbers):
    workbook = Workbook()
    workbook.active.append(['Phone'])
    for number in numbers:
        workbook.active.append([number])
    stream = BytesIO()
    workbook.save(stream)
    return SimpleUploadedFile('phones.xlsx', stream.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def long_date(day):
    return f'{day:%a}, {day:%b} {day.day}, {day.year}'


@override_settings(ALLOWED_HOSTS=['testserver'], PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BrowserPermissionsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user('manager', password='Valid-Admin-Secret-42', is_staff=True)
        self.rep = User.objects.create_user('emilio', password='Valid-Rep-Secret-42', first_name='Emilio', last_name='Arguello')
        self.other = User.objects.create_user('anthony', password='Valid-Other-Secret-42')
        for rep in (self.rep, self.other):
            RepListMembership.objects.create(rep=rep, list_type=TextingListType.GFS)
        self.today = timezone.localdate()
        self.upload = import_workbook(sheet(['2125550100', '2125550101', '2125550102', '2125550103']), self.manager, 'Daily list', list_type=TextingListType.GFS)
        self.batches = publish_batches(self.upload, [self.today, self.today + timedelta(days=1)], [self.rep, self.other], self.manager)
        self.batch = next(b for b in self.batches if b.rep_id == self.rep.pk and b.scheduled_date == self.today)
        self.assignment = self.batch.assignments.select_related('phone').first()
        self.future = next(b for b in self.batches if b.rep_id == self.rep.pk and b.scheduled_date > self.today)

    def test_every_business_page_requires_login(self):
        paths = [reverse('dashboard'), reverse('team'), reverse('audit_log'), reverse('upload_new'),
                 reverse('team_detail', args=[self.rep.pk]),
                 self.upload.get_absolute_url(), self.batch.get_absolute_url(), reverse('batch_status', args=[self.batch.pk])]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response.url)

    def test_rep_cannot_read_other_batch_or_manager_pages(self):
        self.client.force_login(self.rep)
        other = next(b for b in self.batches if b.rep_id == self.other.pk)
        self.assertEqual(self.client.get(other.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse('batch_status', args=[other.pk])).status_code, 404)
        for name in ['team', 'audit_log', 'upload_new']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.get(self.upload.get_absolute_url()).status_code, 403)

    def test_rep_has_full_own_list_and_never_future_numbers(self):
        self.client.force_login(self.rep)
        response = self.client.get(self.batch.get_absolute_url())
        self.assertContains(response, self.assignment.phone.canonical)
        future_number = self.future.assignments.select_related('phone').first().phone.canonical
        response = self.client.get(self.future.get_absolute_url())
        self.assertNotContains(response, future_number)
        self.assertNotContains(self.client.get(reverse('dashboard') + '?date=' + self.future.scheduled_date.isoformat()), future_number)
        self.assertFalse(self.client.get(reverse('batch_status', args=[self.future.pk])).json()['active'])

    def test_rep_list_is_read_only_and_call_routes_are_removed(self):
        for user in (self.rep, self.manager):
            self.client.force_login(user)
            response = self.client.get(self.batch.get_absolute_url())
            for control in ('Start call', 'Call outcome', 'Record outcome', 'outcome-form'):
                self.assertNotContains(response, control)
            for action in ('start', 'outcome'):
                url = f'/calls/{self.assignment.pk}/{action}/'
                self.assertEqual(self.client.post(url, {'outcome': 'interested'}).status_code, 404)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.PENDING)
        self.assertFalse(self.assignment.attempts.exists())

    def test_clear_requires_manager_post_and_csrf(self):
        clear_url = reverse('batch_close', args=[self.batch.pk])
        self.client.force_login(self.rep)
        self.assertEqual(self.client.post(clear_url).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        self.assertEqual(csrf_client.post(clear_url).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(clear_url).status_code, 405)

    def test_clear_hides_numbers_and_invalidates_the_displayed_list(self):
        self.client.force_login(self.rep)
        initial_revision = self.client.get(reverse('batch_status', args=[self.batch.pk])).json()['revision']
        self.client.force_login(self.manager)
        self.client.post(reverse('batch_close', args=[self.batch.pk]))
        self.client.force_login(self.rep)
        self.assertNotContains(self.client.get(self.batch.get_absolute_url()), self.assignment.phone.canonical)
        self.assertFalse(self.client.get(reverse('batch_status', args=[self.batch.pk])).json()['active'])
        self.assertNotEqual(initial_revision, self.client.get(reverse('batch_status', args=[self.batch.pk])).json()['revision'])
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.CANCELLED)
        self.assertTrue(PhoneNumber.objects.filter(pk=self.assignment.phone_id).exists())

    def test_clear_hides_legacy_in_progress_assignments_and_preserves_history(self):
        start_call(self.assignment, self.rep)
        close_batch(self.batch, self.manager)
        self.client.force_login(self.rep)
        self.assertNotContains(self.client.get(self.batch.get_absolute_url()), self.assignment.phone.canonical)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action='call.started', target_id=str(self.assignment.pk)).exists())
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.batch.get_absolute_url()), self.assignment.phone.canonical)

    def test_past_date_has_no_legacy_completion_exception(self):
        start_call(self.assignment, self.rep)
        self.batch.scheduled_date = self.today - timedelta(days=1)
        self.batch.save(update_fields=['scheduled_date'])
        self.client.force_login(self.rep)
        response = self.client.get(self.batch.get_absolute_url())
        self.assertNotContains(response, self.assignment.phone.canonical)
        self.assertFalse(response.context['can_view_numbers'])

    def test_closed_batches_leave_both_active_dashboards(self):
        close_batch(self.batch, self.manager)
        for user in (self.manager, self.rep):
            self.client.force_login(user)
            response = self.client.get(reverse('dashboard'))
            self.assertNotIn(self.batch.pk, [batch.pk for batch in response.context['batches']])

    def test_navigation_does_not_expose_other_reps_future_work_or_manager_uploads(self):
        hidden = ImportBatch.objects.create(label='Manager-only confidential source', filename='private.xlsx', uploaded_by=self.manager)
        self.client.force_login(self.rep)
        other_batch = next(b for b in self.batches if b.rep_id == self.other.pk)
        for url in (reverse('dashboard'), self.batch.get_absolute_url()):
            response = self.client.get(url)
            self.assertNotContains(response, other_batch.get_absolute_url())
            self.assertNotContains(response, self.future.get_absolute_url())
            self.assertNotContains(response, hidden.label)
            self.assertNotContains(response, hidden.get_absolute_url())

    def test_cleared_legacy_assignment_leaves_navigation_and_dashboard(self):
        start_call(self.assignment, self.rep)
        close_batch(self.batch, self.manager)
        self.client.force_login(self.rep)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['batches'], [])
        self.assertNotContains(response, 'Finish an earlier call')
        self.assertNotContains(response, self.batch.get_absolute_url())

    def test_upload_publish_and_close_workflow(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('upload_new'), {'label': 'New import', 'list_type': 'gfs', 'file': sheet(['4165550190', '4165550191'])})
        self.assertEqual(response.status_code, 302)
        new = ImportBatch.objects.get(label='New import')
        params = {'dates': self.today.isoformat(), 'rep_count': 2, 'reps': [self.rep.pk, self.other.pk], 'intent': 'preview'}
        preview = self.client.post(reverse('upload_publish', args=[new.pk]), params)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(new.batches.count(), 0)
        params['intent'] = 'create'
        self.assertEqual(self.client.post(reverse('upload_publish', args=[new.pk]), params).status_code, 302)
        self.assertEqual(new.batches.count(), 2)
        self.client.post(reverse('upload_close', args=[new.pk]))
        new.refresh_from_db()
        self.assertEqual(new.status, ImportBatch.Status.CLOSED)
        self.assertFalse(new.batches.exclude(status='closed').exists())

    def test_create_rep_rejects_privilege_injection(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('team'), {'action': 'create', 'first_name': 'Kip', 'last_name': 'Langat',
            'username': 'kip', 'password1': 'Separate-Strong-Pass-921', 'password2': 'Separate-Strong-Pass-921',
            'list_types': ['ringcentral'],
            'is_staff': True, 'is_superuser': True})
        self.assertEqual(response.status_code, 200)
        new = get_user_model().objects.get(username='kip')
        self.assertFalse(new.is_staff)
        self.assertFalse(new.is_superuser)
        self.assertFalse(new.check_password('Separate-Strong-Pass-921'))
        self.assertTrue(new.check_password(response.context['credential']['password']))
        self.assertEqual(list(new.list_memberships.values_list('list_type', flat=True)), ['ringcentral'])

    def test_disabled_user_loses_access_with_existing_session(self):
        self.client.force_login(self.rep)
        self.rep.is_active = False
        self.rep.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.batch.get_absolute_url()).status_code, 302)

    def test_disable_is_idempotent_and_rejects_bad_account_id(self):
        self.client.force_login(self.manager)
        for _ in range(2):
            self.assertEqual(self.client.post(reverse('team'), {'action': 'disable', 'user_id': self.rep.pk}).status_code, 302)
        self.rep.refresh_from_db()
        self.assertFalse(self.rep.is_active)
        self.assertEqual(AuditEvent.objects.filter(action='account.rep_disabled', target_id=str(self.rep.pk)).count(), 1)
        self.assertEqual(self.client.post(reverse('team'), {'action': 'disable', 'user_id': 'invalid'}).status_code, 403)

    def test_sensitive_response_security_headers(self):
        self.client.force_login(self.rep)
        response = self.client.get(self.batch.get_absolute_url())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        self.assertIn("frame-ancestors 'none'", response['Content-Security-Policy'])

    def test_audit_batch_view_is_not_call_evidence(self):
        self.client.force_login(self.rep)
        self.client.get(self.batch.get_absolute_url())
        self.assertTrue(AuditEvent.objects.filter(actor=self.rep, action='batch.viewed').exists())
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.PENDING)

    def test_audit_renders_failed_sign_ins_and_readable_history(self):
        self.client.post(reverse('login'), {'username': 'unknown', 'password': 'Incorrect-Secret-42'})
        self.client.force_login(self.manager)
        response = self.client.get(reverse('audit_log'))
        self.assertContains(response, 'Sign-in failed')
        self.assertContains(response, 'Spreadsheet uploaded')
        self.assertContains(response, 'Daily list')
        self.assertNotContains(response, 'account.sign_in_failed')

    def test_legacy_sales_data_is_preserved_but_not_shown_in_lead_management(self):
        start_call(self.assignment, self.rep)
        record_outcome(self.assignment, self.rep, 'not_interested', 'Legacy private sales note')
        repeated = import_workbook(sheet([self.assignment.phone.canonical]), self.manager, 'Repeated leads', list_type=TextingListType.GFS)
        repeated.rows.update(message='Previously called: Not interested. Legacy private sales note')
        self.client.force_login(self.manager)
        for url in (repeated.get_absolute_url(), self.batch.get_absolute_url(), reverse('audit_log')):
            response = self.client.get(url)
            self.assertNotContains(response, 'Not interested')
            self.assertNotContains(response, 'Legacy private sales note')
            self.assertNotContains(response, 'Call outcome recorded')
        response = self.client.get(repeated.get_absolute_url())
        self.assertContains(response, 'First uploaded')
        self.assertContains(response, f'Sent to Emilio Arguello on {long_date(self.today)}.')
        self.assertTrue(self.assignment.attempts.filter(outcome='not_interested').exists())
        self.assertTrue(AuditEvent.objects.filter(action='call.outcome_recorded').exists())

    def test_password_change_updates_audit(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('password_change'), {'old_password': 'Valid-Admin-Secret-42',
            'new_password1': 'Replacement-Strong-Pass-771', 'new_password2': 'Replacement-Strong-Pass-771'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(actor=self.manager, action='account.password_changed').exists())

    def test_reps_cannot_change_passwords_or_manage_accounts(self):
        self.client.force_login(self.rep)
        for url in (reverse('password_change'), reverse('password_change_done'), reverse('team_detail', args=[self.rep.pk])):
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'action': 'reset_password'}).status_code, 403)
        self.assertNotContains(self.client.get(reverse('dashboard')), reverse('password_change'))

    def test_upload_requires_a_valid_type_and_filters_eligible_reps(self):
        self.client.force_login(self.manager)
        for list_type in ('', 'other'):
            response = self.client.post(reverse('upload_new'), {'label': 'Invalid type', 'list_type': list_type, 'file': sheet(['4165550190'])})
            self.assertEqual(response.status_code, 200)
            self.assertIn('list_type', response.context['form'].errors)
        self.assertFalse(ImportBatch.objects.filter(label='Invalid type').exists())
        self.rep.list_memberships.create(list_type='ringcentral')
        upload = import_workbook(sheet(['4165550190']), self.manager, 'Ringcentral only', list_type='ringcentral')
        response = self.client.get(upload.get_absolute_url())
        self.assertEqual(list(response.context['form'].fields['reps'].queryset), [self.rep])
        for intent in ('preview', 'create'):
            response = self.client.post(reverse('upload_publish', args=[upload.pk]), {
                'dates': self.today.isoformat(), 'rep_count': 1, 'reps': [self.other.pk], 'intent': intent,
            })
            self.assertIn('reps', response.context['form'].errors)
            self.assertFalse(upload.batches.exists())

    def test_legacy_draft_can_be_classified_without_losing_numbers(self):
        draft = import_workbook(sheet(['4165550190']), self.manager, 'Legacy draft', list_type='gfs')
        ImportBatch.objects.filter(pk=draft.pk).update(list_type='')
        phone_id = draft.rows.get().phone_id
        url = reverse('upload_classify', args=[draft.pk])
        self.client.force_login(self.rep)
        self.assertEqual(self.client.post(url, {'list_type': 'ringcentral'}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(draft.get_absolute_url()), 'Set list type')
        self.assertEqual(self.client.post(url, {'list_type': 'ringcentral'}).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.list_type, 'ringcentral')
        self.assertEqual(draft.rows.get().phone_id, phone_id)

    def test_calendar_shows_only_own_schedule_metadata_and_today_links(self):
        self.rep.list_memberships.create(list_type='ringcentral')
        upload = import_workbook(sheet(['4165550190']), self.manager, 'Organic text - Clean', list_type='ringcentral')
        scheduled = publish_batches(upload, [self.future.scheduled_date], [self.rep], self.manager)[0]
        self.client.force_login(self.rep)
        response = self.client.get(reverse('dashboard') + '?week=' + self.future.scheduled_date.isoformat())
        entries = [batch for day in response.context['calendar_days'] for batch in day['batches']]
        self.assertIn(scheduled.pk, [b.pk for b in entries])
        self.assertTrue(all(b.rep_id == self.rep.pk for b in entries))
        self.assertContains(response, 'Organic text - Clean')
        self.assertContains(response, 'Ringcentral · Clean')  # compact group name on calendar entries
        for entry in entries:
            for phone in entry.assignments.select_related('phone'):
                self.assertNotContains(response, phone.phone.canonical)
            if entry.scheduled_date != self.today:
                self.assertNotContains(response, entry.get_absolute_url())
                self.assertFalse(entry.is_available)
        self.assertNotContains(self.client.get(scheduled.get_absolute_url()), '+14165550190')

    def test_membership_removal_revokes_batch_access_and_calendar(self):
        self.client.force_login(self.rep)
        self.assertContains(self.client.get(self.batch.get_absolute_url()), self.assignment.phone.canonical)
        self.rep.list_memberships.all().delete()
        self.assertEqual(self.client.get(self.batch.get_absolute_url()).status_code, 404)
        self.assertEqual(self.client.get(reverse('batch_status', args=[self.batch.pk])).status_code, 404)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['batches'], [])
        self.assertFalse(any(day['batches'] for day in response.context['calendar_days']))

    def test_admin_reset_shows_password_once_and_invalidates_rep_session(self):
        rep_client = Client()
        rep_client.force_login(self.rep)
        self.client.force_login(self.manager)
        url = reverse('team_detail', args=[self.rep.pk])
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        self.assertEqual(csrf_client.post(url, {'action': 'reset_password'}).status_code, 403)
        response = self.client.post(url, {'action': 'reset_password'})
        self.assertEqual(response.status_code, 200)
        password = response.context['credential']['password']
        self.rep.refresh_from_db()
        self.assertTrue(self.rep.check_password(password))
        self.assertFalse(self.rep.check_password('Valid-Rep-Secret-42'))
        self.assertEqual(rep_client.get(reverse('dashboard')).status_code, 302)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotContains(self.client.get(url), password)
        self.assertNotIn(password, str(dict(self.client.session)))
        self.assertNotIn(password, str(list(AuditEvent.objects.values('details'))))

    def test_admin_can_see_exact_sign_in_history_without_other_account_events(self):
        self.client.post(reverse('login'), {'username': 'emilio', 'password': 'Valid-Rep-Secret-42'})
        event = AuditEvent.objects.get(actor=self.rep, action='account.signed_in')
        self.client.logout()
        self.client.force_login(self.manager)
        response = self.client.get(reverse('team_detail', args=[self.rep.pk]))
        self.assertEqual(list(response.context['login_events']), [event])
        expected = timezone.localtime(event.created_at).strftime('%Y-%m-%d %H:%M:%S')
        self.assertContains(response, expected)
        self.assertContains(response, 'America/New_York')
        self.assertContains(self.client.get(reverse('team')), 'Emilio Arguello')

    def test_admin_can_update_memberships_but_not_elevate_rep_or_edit_admin(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('team_detail', args=[self.rep.pk]), {
            'action': 'update_memberships', 'list_types': ['gfs', 'ringcentral'], 'is_staff': True,
        })
        self.assertEqual(response.status_code, 302)
        self.rep.refresh_from_db()
        self.assertFalse(self.rep.is_staff)
        self.assertEqual(set(self.rep.list_memberships.values_list('list_type', flat=True)), {'gfs', 'ringcentral'})
        for action in ('update_memberships', 'reset_password', 'disable'):
            self.assertEqual(self.client.post(reverse('team_detail', args=[self.manager.pk]), {'action': action}).status_code, 403)

    def test_repeat_upload_shows_when_each_number_was_sent(self):
        sent_today = self.batch.assignments.select_related('phone').get().phone.canonical
        other_future = next(b for b in self.batches if b.rep_id == self.other.pk and b.scheduled_date > self.today)
        scheduled = other_future.assignments.select_related('phone').get().phone.canonical
        draft = import_workbook(sheet(['4165550190']), self.manager, 'Unsent draft', list_type=TextingListType.GFS)
        repeated = import_workbook(sheet([sent_today, scheduled, '4165550190', '4165550191']), self.manager, 'Repeat check', list_type=TextingListType.GFS)
        self.client.force_login(self.manager)
        response = self.client.get(repeated.get_absolute_url())
        self.assertContains(response, 'These 3 numbers were previously uploaded and flagged. Do you wish to upload them again?', count=2)
        self.client.post(reverse('upload_previous', args=[repeated.pk]), {'include': 'no'})
        response = self.client.get(repeated.get_absolute_url())
        self.assertContains(response, '3 numbers in this file were uploaded before.')
        uploaded_on = timezone.localtime(self.upload.created_at).date()
        self.assertContains(response, f'First uploaded {uploaded_on:%b} {uploaded_on.day}, {uploaded_on.year} in “Daily list”.', count=2)
        self.assertContains(response, f'Sent to Emilio Arguello on {long_date(self.today)}.')
        self.assertContains(response, f'Scheduled for anthony on {long_date(other_future.scheduled_date)}.')
        self.assertContains(response, 'in “Unsent draft”. Not sent to a rep yet.')
        close_batch(self.batch, self.manager)
        self.assertContains(self.client.get(repeated.get_absolute_url()), f'Sent to Emilio Arguello on {long_date(self.today)} (list cleared).')
        self.assertNotContains(self.client.get(draft.get_absolute_url()), 'uploaded before')

    def test_previous_numbers_prompt_until_the_manager_answers(self):
        sent = self.batch.assignments.select_related('phone').get().phone.canonical
        repeated = import_workbook(sheet([sent, '4165550190']), self.manager, 'Repeat check', list_type=TextingListType.GFS)
        url = reverse('upload_previous', args=[repeated.pk])
        self.client.force_login(self.manager)
        response = self.client.get(repeated.get_absolute_url())
        self.assertContains(response, '<dialog class="confirm-dialog" data-auto-open')
        self.assertContains(response, 'This number was previously uploaded and flagged. Do you wish to upload it again?')
        self.assertNotContains(response, 'data-split-form')
        self.assertTrue(response.context['needs_previous_decision'])
        self.client.post(url, {'include': 'maybe'})
        repeated.refresh_from_db()
        self.assertIsNone(repeated.include_previous)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.manager)
        self.assertEqual(csrf_client.post(url, {'include': 'yes'}).status_code, 403)
        self.client.force_login(self.rep)
        self.assertEqual(self.client.post(url, {'include': 'yes'}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertRedirects(self.client.post(url, {'include': 'yes'}), repeated.get_absolute_url())
        response = self.client.get(repeated.get_absolute_url())
        self.assertNotContains(response, 'data-auto-open')
        self.assertContains(response, 'data-split-form')
        self.assertContains(response, '1 previously uploaded number is included in this list.')
        self.assertContains(response, 'Leave them out instead')
        self.assertEqual(response.context['assignable_count'], 2)
        self.client.post(url, {'include': 'no'})
        response = self.client.get(repeated.get_absolute_url())
        self.assertContains(response, 'Upload them again instead')
        self.assertEqual(response.context['assignable_count'], 1)

    def test_assigned_lists_reach_the_rep_account_without_a_shared_link(self):
        self.client.force_login(self.manager)
        for url in (self.upload.get_absolute_url(), self.batch.get_absolute_url(), reverse('dashboard')):
            response = self.client.get(url)
            self.assertNotContains(response, 'data-copy-link')
            self.assertNotContains(response, 'Copy batch link')
        self.assertContains(self.client.get(self.batch.get_absolute_url()), 'This list is in Emilio Arguello’s account')
        self.client.force_login(self.rep)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual([batch.pk for batch in response.context['batches']], [self.batch.pk])
        self.assertContains(response, self.batch.get_absolute_url())
        future = self.client.get(reverse('dashboard'), {'week': self.future.scheduled_date.isoformat()})
        scheduled = [batch.pk for day in future.context['calendar_days'] for batch in day['batches']]
        self.assertIn(self.future.pk, scheduled)

    def test_manager_calendar_shows_every_reps_lists_for_the_week(self):
        tomorrow = self.today + timedelta(days=1)
        yesterday = self.today - timedelta(days=1)
        RepBatch.objects.filter(upload=self.upload, scheduled_date=tomorrow).update(scheduled_date=yesterday)
        self.client.force_login(self.manager)
        for selected, label in ((self.today, 'Available today'), (yesterday, 'Sent')):
            with self.subTest(selected=selected):
                response = self.client.get(reverse('dashboard'), {'date': selected.isoformat()})
                week_start = selected - timedelta(days=selected.weekday())
                self.assertEqual(response.context['week_start'], week_start)
                self.assertEqual(response.context['previous_week'], selected - timedelta(days=7))
                day = next(d for d in response.context['calendar_days'] if d['date'] == selected)
                self.assertTrue(day['is_selected'])
                self.assertEqual([(e['upload__label'], e['rep_count'], e['number_count']) for e in day['batches']], [('Daily list', 2, 2)])
                self.assertEqual(day['batches'][0]['list_type_label'], 'GFS · Donut')
                self.assertContains(response, self.upload.get_absolute_url())
                self.assertContains(response, label)
                for batch_day in response.context['calendar_days']:
                    for entry in batch_day['batches']:
                        self.assertGreaterEqual(entry['scheduled_date'], week_start)
                        self.assertLessEqual(entry['scheduled_date'], week_start + timedelta(days=6))
        future = import_workbook(sheet(['4165550190']), self.manager, 'Next list', list_type=TextingListType.GFS)
        publish_batches(future, [tomorrow], [self.rep], self.manager)
        response = self.client.get(reverse('dashboard'), {'date': tomorrow.isoformat()})
        day = next(d for d in response.context['calendar_days'] if d['date'] == tomorrow)
        self.assertEqual([(e['upload__label'], e['rep_count'], e['number_count']) for e in day['batches']], [('Next list', 1, 1)])
        self.assertContains(response, 'Upcoming')
        close_batch(self.batch, self.manager)
        response = self.client.get(reverse('dashboard'))
        day = next(d for d in response.context['calendar_days'] if d['date'] == self.today)
        self.assertEqual([(e['rep_count'], e['number_count']) for e in day['batches']], [(1, 1)])

    def test_invalid_manager_date_falls_back_without_server_error(self):
        self.client.force_login(self.manager)
        for value in ('not-a-date', '0001-01-01', '9999-12-31'):
            response = self.client.get(reverse('dashboard'), {'date': value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['selected_date'], self.today)

    def test_texting_groups_show_clean_and_donut_names(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse('upload_new'))
        self.assertContains(response, 'Ringcentral Texting (Clean)')
        self.assertContains(response, 'GFS Texting (Donut)')
        self.assertContains(self.client.get(reverse('team')), 'GFS Texting (Donut)')

    def test_invalid_calendar_week_falls_back_without_server_error(self):
        self.client.force_login(self.rep)
        for week in ('not-a-date', '0001-01-01', '9999-12-31'):
            response = self.client.get(reverse('dashboard'), {'week': week})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['week_start'], self.today - timedelta(days=self.today.weekday()))

    def test_invalid_account_action_is_rejected_without_server_error(self):
        self.client.force_login(self.manager)
        for url in (reverse('team'), reverse('team_detail', args=[self.rep.pk])):
            for payload in ({}, {'action': 'invalid'}):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Choose a valid account action.')


@override_settings(ALLOWED_HOSTS=['testserver'], PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AuthenticationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('rep', password='Very-Strong-Password-123')

    def test_safe_redirect_and_audit(self):
        response = self.client.post(reverse('login'), {'username': 'rep', 'password': 'Very-Strong-Password-123', 'next': 'https://example.org/leak'})
        self.assertRedirects(response, reverse('dashboard'))
        self.assertTrue(AuditEvent.objects.filter(action='account.signed_in', actor=self.user).exists())

    def test_persistent_login_throttle(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'rep', 'password': 'wrong'})
        response = Client().post(reverse('login'), {'username': 'rep', 'password': 'Very-Strong-Password-123'})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(SecurityThrottle.objects.filter(failures=5).exists())
        self.assertFalse(AuditEvent.objects.filter(action='account.signed_in').exists())

    def test_unicode_equivalent_username_cannot_bypass_throttle(self):
        for _ in range(5):
            self.client.post(reverse('login'), {'username': 'rep', 'password': 'wrong'})
        response = self.client.post(reverse('login'), {'username': '\uff52\uff45\uff50', 'password': 'Very-Strong-Password-123'})
        self.assertEqual(response.status_code, 429)

    def test_inactivity_expires_and_poll_does_not_extend_session(self):
        self.client.force_login(self.user)
        session = self.client.session
        session['last_activity'] = 1
        session.save()
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_logout_requires_post(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.assertEqual(self.client.post(reverse('logout')).status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action='account.signed_out').exists())

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=['phones.office.example'])
    def test_invalid_host_returns_400_without_authentication_context(self):
        self.assertEqual(self.client.get('/login/', HTTP_HOST='untrusted.example').status_code, 400)
