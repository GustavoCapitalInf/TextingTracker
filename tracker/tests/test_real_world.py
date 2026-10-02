"""Realistic admin and rep journeys, messy spreadsheets, and creative misuse.

These tests drive the app over HTTP the way people use it: an admin uploads a
single-column spreadsheet and splits it, reps sign in and read their lists, and
some reps try every trick they can think of.
"""
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Count
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from tracker.models import (
    Assignment, AuditEvent, ImportBatch, ImportRow, RepBatch, RepListMembership, TextingListType,
)
from tracker.services import import_workbook, normalize_phone, publish_batches


COMPANY = ZoneInfo('America/New_York')
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PASSWORD = 'Correct-Horse-Battery-42'


def xlsx(rows, *, name='leads.xlsx', extra_sheets=(), hide_extra=False):
    """A workbook whose rows are single values, or lists of cells for wrong layouts."""
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row if isinstance(row, list) else [row])
    for index, values in enumerate(extra_sheets, start=2):
        extra = book.create_sheet(f'Sheet{index}')
        for value in values:
            extra.append([value])
        if hide_extra:
            extra.sheet_state = 'hidden'
    stream = BytesIO()
    book.save(stream)
    book.close()
    return SimpleUploadedFile(name, stream.getvalue(), content_type=XLSX)


def tampered_xlsx(doctype=b'', cell_text=None, extra_entries=()):
    """A real workbook whose insides were edited by hand, the way a malicious file would be."""
    original = ZipFile(BytesIO(xlsx(['(312) 555-0100']).read()))
    output = BytesIO()
    with original, ZipFile(output, 'w', ZIP_DEFLATED) as tampered:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == 'xl/worksheets/sheet1.xml':
                declaration_end = data.index(b'?>') + 2 if data.startswith(b'<?xml') else 0
                data = data[:declaration_end] + doctype + data[declaration_end:]
                if cell_text is not None:
                    data = data.replace(b'(312) 555-0100', cell_text)
            tampered.writestr(item, data)
        for name, data in extra_entries:
            tampered.writestr(name, data)
    return SimpleUploadedFile('from-a-vendor.xlsx', output.getvalue(), content_type=XLSX)


def fake_numbers(count, area='312'):
    return [f'({area}) 555-{1000 + index:04d}' for index in range(count)]


@contextmanager
def company_time(moment):
    """Pretend the server clock reads `moment`, given in company (New York) time."""
    aware = moment.replace(tzinfo=COMPANY).astimezone(ZoneInfo('UTC'))
    with patch('django.utils.timezone.now', return_value=aware):
        yield


@override_settings(ALLOWED_HOSTS=['testserver'], PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class Scenario(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.admin = get_user_model().objects.create_user(
            'ada.admin', password=PASSWORD, is_staff=True, first_name='Ada', last_name='Admin')
        self.anthony = self.make_rep('anthony.diaz', 'Anthony', 'Diaz', 'ringcentral')
        self.erik = self.make_rep('erik.anderson', 'Erik', 'Anderson', 'ringcentral')
        self.blake = self.make_rep('blake.fiorito', 'Blake', 'Fiorito', 'gfs')
        self.emilio = self.make_rep('emilio.arguello', 'Emilio', 'Arguello', 'gfs', 'ringcentral')
        self.manager = self.signed_in(self.admin)

    def make_rep(self, username, first, last, *groups):
        rep = get_user_model().objects.create_user(username, password=PASSWORD, first_name=first, last_name=last)
        for group in groups:
            RepListMembership.objects.create(rep=rep, list_type=group)
        return rep

    def signed_in(self, user, **environ):
        client = Client(**environ)
        client.force_login(user)
        return client

    def upload_via_ui(self, file, label='Leads', list_type='ringcentral'):
        response = self.manager.post(reverse('upload_new'), {'label': label, 'list_type': list_type, 'file': file})
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['form'].errors)
        return ImportBatch.objects.get(pk=response.url.strip('/').split('/')[-1])

    def split_via_ui(self, upload, dates, reps, intent='create'):
        return self.manager.post(reverse('upload_publish', args=[upload.pk]), {
            'dates': '\n'.join(day.isoformat() for day in dates), 'rep_count': len(reps),
            'reps': [rep.pk for rep in reps], 'intent': intent,
        })

    def assign(self, count, dates, reps, label='Leads', list_type='ringcentral', area='312'):
        upload = import_workbook(xlsx(fake_numbers(count, area)), self.admin, label, list_type=list_type)
        return upload, publish_batches(upload, dates, reps, self.admin)

    def batch_for(self, batches, rep, day=None):
        return next(b for b in batches if b.rep_id == rep.pk and (day is None or b.scheduled_date == day))

    def numbers(self, batch):
        return set(batch.assignments.values_list('phone__canonical', flat=True))

    def shown(self, client, batch, **params):
        """The phone numbers a person actually sees on a list page (None if refused)."""
        response = client.get(batch.get_absolute_url(), params)
        if response.status_code != 200:
            return None
        return {assignment.phone.canonical for assignment in response.context['assignments']}


class PhoneFormatTests(Scenario):
    def counts(self, upload):
        return dict(upload.rows.values_list('classification').annotate(n=Count('pk')))

    def test_the_same_number_typed_twenty_ways_is_one_lead(self):
        ways = [
            '(415) 555-0123', '415-555-0123', '415.555.0123', '415 555 0123', '4155550123',
            '1-415-555-0123', '1 (415) 555-0123', '+1 415 555 0123', '+1 (415) 555-0123', '+14155550123',
            '  415-555-0123  ', '415 - 555 - 0123', '(415)555-0123', '415 555 0123',  # web copy-paste spaces
            '1.415.555.0123', '1 415 555 0123', 4155550123, 14155550123, 4155550123.0, '(415) 555 - 0123',
        ]
        upload = import_workbook(xlsx(['Phone number', *ways]), self.admin, 'Twenty ways', list_type='gfs')
        self.assertEqual(self.counts(upload), {'ready': 1, 'duplicate': len(ways) - 1})
        self.assertEqual(set(upload.rows.values_list('phone__canonical', flat=True)), {'+14155550123'})

    def test_canadian_and_toll_free_numbers_are_welcome(self):
        for raw, expected in [('(416) 555-0199', '+14165550199'), ('604-555-0123', '+16045550123'),
                              ('1-800-275-2273', '+18002752273'), ('888.555.0123', '+18885550123')]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), expected)

    def test_junk_cells_are_flagged_without_breaking_the_upload(self):
        junk = [
            '555-0123',                     # forgot the area code
            '415-555-012',                  # a digit short
            '2-415-555-0123',               # wrong country prefix
            '+44 20 7946 0958',             # London
            '+52 55 1234 5678',             # Mexico City
            '+1 876 555 0123',              # Jamaica shares +1 but is not US/Canada
            '415-555-0123 ext 12', '415-555-0123 x12',
            'tel:+14155550123', '1-800-FLOWERS', '415-555-CALL',
            '415-555-0123 / 415-555-0124',  # two numbers crammed in one cell
            '(000) 555-0123', '123-456-7890', '911',
            'N/A', 'call back later', 'DO NOT TEXT', '😀😀😀', '-', '4.15555E+09',
            '9' * 200,
            4155550123.5, True, datetime(2026, 10, 1),  # a decimal, a checkbox, a date Excel "helped" with
        ]
        upload = import_workbook(xlsx(['Phone', '(312) 555-0100', *junk]), self.admin, 'Junk drawer', list_type='gfs')
        self.assertEqual(self.counts(upload), {'ready': 1, 'invalid': len(junk)})
        self.assertFalse(upload.rows.filter(classification='invalid', message='').exists())
        self.assertFalse(upload.rows.filter(classification='invalid', phone__isnull=False).exists())

    def test_numbers_pasted_from_word_or_web_pages_are_accepted(self):
        pasted = ['415–555–0123', '415‑555‑0124', '415—555—0125', '−415 555 0126',
                  '４１５５５５０１２７', '（４１５）５５５－０１２８', '＋１ 415 555 0129', '415\t555\t0130']
        upload = import_workbook(xlsx(pasted), self.admin, 'Pasted', list_type='gfs')
        self.assertEqual(self.counts(upload), {'ready': len(pasted)})
        self.assertEqual(sorted(upload.rows.values_list('phone__canonical', flat=True)),
                         [f'+1415555{number:04d}' for number in range(123, 131)])
        # The review still shows exactly what was in the spreadsheet.
        self.assertEqual(upload.rows.get(row_number=1).raw_value, '415–555–0123')

    def test_excel_number_cells_keep_every_digit(self):
        upload = import_workbook(xlsx([4155550123, 14155550124, 4155550125.0]), self.admin, 'Numeric cells', list_type='gfs')
        self.assertEqual(set(upload.rows.values_list('phone__canonical', flat=True)),
                         {'+14155550123', '+14155550124', '+14155550125'})

    def test_excel_cannot_give_back_digits_it_already_rounded(self):
        """Limitation (see README): a cell Excel already rounded to 4155550000 is a different, valid number."""
        upload = import_workbook(xlsx([4155550000.0]), self.admin, 'Rounded', list_type='gfs')
        self.assertEqual(upload.rows.get().phone.canonical, '+14155550000')

    def test_recognised_header_rows_are_skipped(self):
        for index, header in enumerate(['Phone', 'PHONE NUMBER', 'Phone #', 'Phone Numbers:', 'Mobile',
                                        'Mobile Number', 'Cell', 'Telephone', 'Number', 'Contact Number']):
            with self.subTest(header=header):
                upload = import_workbook(xlsx([header, f'(312) 555-{2000 + index}']), self.admin, header, list_type='gfs')
                self.assertEqual(self.counts(upload), {'ready': 1})

    def test_a_plain_column_with_no_header_counts_every_row(self):
        upload = self.upload_via_ui(xlsx(fake_numbers(5)), label='No header')
        self.assertEqual(self.counts(upload), {'ready': 5})
        self.assertEqual(list(upload.rows.values_list('row_number', flat=True)), [1, 2, 3, 4, 5])
        bare = import_workbook(xlsx([4155550199, '(415) 555-0198']), self.admin, 'Bare digits', list_type='gfs')
        self.assertEqual(self.counts(bare), {'ready': 2})

    def test_only_the_first_row_can_be_a_header(self):
        upload = import_workbook(xlsx(['Phone', '(312) 555-0100', 'Phone', '(312) 555-0101']),
                                 self.admin, 'Header twice', list_type='gfs')
        self.assertEqual(self.counts(upload), {'ready': 2, 'invalid': 1})
        numbered = import_workbook(xlsx(['Phone 1', '(312) 555-0102']), self.admin, 'Numbered header', list_type='gfs')
        self.assertEqual(self.counts(numbered), {'ready': 1})

    def test_other_common_header_names_are_skipped_too(self):
        for index, header in enumerate(['Cell Phone Number', 'Mobile Phone', 'Phone No.', 'Tel', 'Mobiles', 'Leads']):
            with self.subTest(header=header):
                upload = import_workbook(xlsx([header, f'(312) 555-{3000 + index}']), self.admin, header, list_type='gfs')
                self.assertEqual(upload.invalid_count, 0)


class SpreadsheetLayoutTests(Scenario):
    def refused(self, file, expected):
        before = ImportBatch.objects.count()
        response = self.manager.post(reverse('upload_new'), {'label': 'Layout check', 'list_type': 'gfs', 'file': file})
        self.assertEqual(response.status_code, 200)
        errors = ' '.join(response.context['form'].errors.get('file', []))
        self.assertIn(expected, errors)
        self.assertEqual(ImportBatch.objects.count(), before)

    def test_wrong_layouts_are_refused_with_a_clear_message(self):
        good = '(312) 555-0100'
        cases = {
            'name next to number': (xlsx([['Name', 'Phone'], ['Ann', good]]), 'single column'),
            'numbers hop columns': (xlsx([[good], [None, '(312) 555-0101']]), 'same column'),
            'a formula': (xlsx([good, '=A1']), 'Formulas are not allowed'),
            'second sheet with numbers': (xlsx([good], extra_sheets=[['(312) 555-0102']]), 'one worksheet'),
            'hidden sheet with numbers': (xlsx([good], extra_sheets=[['(312) 555-0103']], hide_extra=True), 'one worksheet'),
            'header only': (xlsx(['Phone number']), 'no phone-number rows'),
            'csv renamed to xlsx': (SimpleUploadedFile('leads.xlsx', b'phone\n4155550123\n'), 'not a valid .xlsx'),
            'html renamed to xlsx': (SimpleUploadedFile('leads.xlsx', b'<script>alert(1)</script>'), 'not a valid .xlsx'),
            'a csv': (SimpleUploadedFile('leads.csv', b'phone\n4155550123\n'), '.xlsx'),
            'old excel': (SimpleUploadedFile('leads.xls', b'\xd0\xcf\x11\xe0'), '.xlsx'),
            'macro workbook': (SimpleUploadedFile('leads.xlsm', b'PK'), '.xlsx'),
            'empty file': (SimpleUploadedFile('leads.xlsx', b''), 'empty'),
        }
        for name, (file, expected) in cases.items():
            with self.subTest(name):
                self.refused(file, expected)

    def test_booby_trapped_spreadsheets_from_a_vendor_are_refused(self):
        laughs = b''.join(b'<!ENTITY lol%d "&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;&lol%d;">' % ((n, *(n - 1,) * 10)) for n in range(1, 10))
        cases = {
            'reads a file off the server': tampered_xlsx(b'<!DOCTYPE worksheet [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>', b'&xxe;'),
            'billion laughs memory bomb': tampered_xlsx(b'<!DOCTYPE worksheet [<!ENTITY lol0 "lol">' + laughs + b']>', b'&lol9;'),
            'zip bomb': tampered_xlsx(extra_entries=[('xl/media/padding.bin', b'\0' * (40 * 1024 * 1024))]),
            'hidden macro': tampered_xlsx(extra_entries=[('xl/vbaProject.bin', b'Attribute VB_Name = "Evil"')]),
        }
        for name, file in cases.items():
            with self.subTest(name):
                before = ImportBatch.objects.count()
                response = self.manager.post(reverse('upload_new'), {'label': 'Vendor file', 'list_type': 'gfs', 'file': file})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors.get('file'))
                self.assertNotIn('root:', response.content.decode())
                self.assertEqual(ImportBatch.objects.count(), before)

    def test_friendly_layouts_are_accepted(self):
        upload = self.upload_via_ui(xlsx([
            [None, None, None], [None, None, 'Phone Number'], [None, None, '(312) 555-0100'],
            [None, None, None], [None, None, '(312) 555-0101'], [None, None, None], [None, None, '312.555.0102'],
        ], extra_sheets=[[]]), label='Column C with gaps')
        self.assertEqual(upload.ready_count, 3)
        self.assertEqual(upload.rows.count(), 3)

    def test_sneaky_names_are_shown_as_plain_text(self):
        attack = '<script>alert("pwned")</script>'
        upload = self.upload_via_ui(xlsx(['(312) 555-0100'], name='<img src=x onerror=alert(1)>.xlsx'), label=attack)
        sneaky = self.make_rep('sneaky.rep', '<img src=x onerror=alert(2)>', 'Rep', 'ringcentral')
        self.split_via_ui(upload, [self.today], [sneaky])
        rep_client = self.signed_in(sneaky)
        pages = [self.manager.get(upload.get_absolute_url()), self.manager.get(reverse('dashboard')),
                 self.manager.get(reverse('audit_log')), self.manager.get(reverse('team')),
                 rep_client.get(reverse('dashboard')), rep_client.get(upload.batches.get().get_absolute_url())]
        for response in pages:
            with self.subTest(page=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 200)
                for raw in ('<script>alert("pwned")', '<img src=x onerror'):
                    self.assertNotIn(raw, response.content.decode())


class AdminJourneyTests(Scenario):
    def test_admin_builds_the_week_from_the_spreadsheet(self):
        monday = self.today + timedelta(days=7 - self.today.weekday())
        week = [monday + timedelta(days=offset) for offset in range(5)]
        sms = self.upload_via_ui(xlsx(['Phone', *fake_numbers(10, '312')]), label='SMS Magic Response- clean')
        self.assertEqual(self.split_via_ui(sms, week[:1], [self.anthony, self.erik]).status_code, 302)
        organic = self.upload_via_ui(xlsx(['Phone', *fake_numbers(16, '773')]), label='organic text- Clean')
        self.assertEqual(self.split_via_ui(organic, week[1:], [self.anthony, self.erik]).status_code, 302)

        self.assertEqual(Assignment.objects.count(), 26)
        self.assertEqual(Assignment.objects.values('phone').distinct().count(), 26)
        calendar = self.manager.get(reverse('dashboard'), {'date': monday.isoformat()}).context['calendar_days']
        planned = {day['date']: [(e['upload__label'], e['number_count'], e['rep_count']) for e in day['batches']] for day in calendar}
        self.assertEqual(planned[week[0]], [('SMS Magic Response- clean', 10, 2)])
        for day in week[1:]:
            self.assertEqual(planned[day], [('organic text- Clean', 4, 2)])

        rep_calendar = self.signed_in(self.anthony).get(reverse('dashboard'), {'week': monday.isoformat()}).context['calendar_days']
        labels = {day['date']: [b.upload.label for b in day['batches']] for day in rep_calendar}
        self.assertEqual(labels[week[0]], ['SMS Magic Response- clean'])
        self.assertEqual([labels[day] for day in week[1:]], [['organic text- Clean']] * 4)
        batches = list(RepBatch.objects.filter(rep=self.anthony))
        for day in (week[0], week[2]):
            with company_time(datetime.combine(day, time(9))):
                client = self.signed_in(self.anthony)
                for batch in batches:
                    expected = self.numbers(batch) if batch.scheduled_date == day else set()
                    self.assertEqual(self.shown(client, batch), expected)

    def test_admin_creates_a_rep_who_signs_in_with_the_generated_password(self):
        response = self.manager.post(reverse('team'), {
            'action': 'create', 'first_name': 'Kip', 'last_name': 'Langat', 'username': 'kip.langat',
            'list_types': ['ringcentral'],
        })
        password = response.context['credential']['password']
        newcomer = Client()
        self.assertRedirects(newcomer.post(reverse('login'), {'username': 'kip.langat', 'password': password}),
                             reverse('dashboard'))
        self.assertContains(newcomer.get(reverse('dashboard')), 'Your leads')
        self.assertNotContains(self.manager.get(reverse('team')), password)
        self.assertFalse(get_user_model().objects.get(username='kip.langat').is_staff)
        duplicate = self.manager.post(reverse('team'), {
            'action': 'create', 'first_name': 'Kip', 'last_name': 'Again', 'username': 'KIP.LANGAT',
            'list_types': ['gfs'],
        })
        self.assertIn('username', duplicate.context['form'].errors)

    def test_uneven_split_stays_fair(self):
        days = [self.today + timedelta(days=offset) for offset in range(1, 5)]
        _, batches = self.assign(10, days, [self.anthony, self.erik, self.emilio])
        per_day = sorted((sum(b.assignments.count() for b in batches if b.scheduled_date == day) for day in days), reverse=True)
        self.assertEqual(per_day, [3, 3, 2, 2])
        per_rep = [sum(b.assignments.count() for b in batches if b.rep_id == rep.pk) for rep in (self.anthony, self.erik, self.emilio)]
        self.assertLessEqual(max(per_rep) - min(per_rep), 1)
        self.assertEqual(Assignment.objects.values('phone').distinct().count(), 10)

    def test_reps_are_not_given_empty_lists(self):
        days = [self.today + timedelta(days=offset) for offset in range(1, 5)]
        upload = self.upload_via_ui(xlsx(fake_numbers(3)))
        preview = self.split_via_ui(upload, days, [self.anthony, self.erik], intent='preview').context['split_preview']
        self.assertEqual(sorted(item['count'] for item in preview), [1, 1, 1])
        for _ in range(2):  # a double-click must still be accepted
            self.assertEqual(self.split_via_ui(upload, days, [self.anthony, self.erik]).status_code, 302)
        self.assertEqual(upload.batches.count(), 3)
        self.assertFalse(upload.batches.annotate(n=Count('assignments')).filter(n=0).exists())
        response = self.split_via_ui(upload, days[:2], [self.anthony, self.erik])
        self.assertIn('different split settings', str(response.context['form'].non_field_errors()))

    def test_double_clicking_create_does_not_assign_twice(self):
        upload = self.upload_via_ui(xlsx(fake_numbers(8)))
        for _ in range(2):
            self.assertEqual(self.split_via_ui(upload, [self.today], [self.anthony, self.erik]).status_code, 302)
        self.assertEqual(upload.batches.count(), 2)
        self.assertEqual(Assignment.objects.filter(batch__upload=upload).count(), 8)

    def test_bad_dates_are_refused(self):
        upload = self.upload_via_ui(xlsx(fake_numbers(4)))
        tomorrow = self.today + timedelta(days=1)
        for dates in ['yesterday', (self.today - timedelta(days=1)).isoformat(), '2026-02-30', '10/02/2026',
                      'tomorrow', f'{tomorrow}\n{tomorrow}', '\n'.join(str(tomorrow + timedelta(days=n)) for n in range(32)), '']:
            with self.subTest(dates=dates[:30]):
                response = self.manager.post(reverse('upload_publish', args=[upload.pk]), {
                    'dates': dates, 'rep_count': 1, 'reps': [self.anthony.pk], 'intent': 'create'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
        self.assertFalse(upload.batches.exists())

    def test_rep_in_both_groups_gets_both_lists_the_same_day(self):
        _, gfs = self.assign(4, [self.today], [self.emilio], label='Donut list', list_type='gfs', area='312')
        _, ringcentral = self.assign(4, [self.today], [self.emilio], label='Clean list', list_type='ringcentral', area='773')
        client = self.signed_in(self.emilio)
        todays = client.get(reverse('dashboard')).context['batches']
        self.assertEqual({b.upload.label for b in todays}, {'Donut list', 'Clean list'})
        for batch in (gfs[0], ringcentral[0]):
            self.assertEqual(self.shown(client, batch), self.numbers(batch))

    def test_accidental_double_upload_cannot_double_book_numbers(self):
        numbers = fake_numbers(6)
        first = self.upload_via_ui(xlsx(numbers), label='Monday list')
        self.split_via_ui(first, [self.today], [self.anthony])
        again = self.upload_via_ui(xlsx(numbers), label='Monday list (oops)')
        self.assertTrue(self.manager.get(again.get_absolute_url()).context['needs_previous_decision'])
        refused = self.split_via_ui(again, [self.today], [self.erik])
        self.assertIn('Choose whether', str(refused.context['form'].non_field_errors()))
        self.manager.post(reverse('upload_previous', args=[again.pk]), {'include': 'no'})
        response = self.split_via_ui(again, [self.today], [self.erik])
        self.assertIn('no eligible numbers', str(response.context['form'].non_field_errors()))
        self.assertEqual(Assignment.objects.count(), 6)

    def test_clearing_one_reps_list_leaves_everyone_else_alone(self):
        _, batches = self.assign(6, [self.today], [self.anthony, self.erik])
        mine, theirs = self.batch_for(batches, self.anthony), self.batch_for(batches, self.erik)
        anthony, erik = self.signed_in(self.anthony), self.signed_in(self.erik)
        before = anthony.get(reverse('batch_status', args=[mine.pk])).json()
        self.manager.post(reverse('batch_close', args=[mine.pk]))
        after = anthony.get(reverse('batch_status', args=[mine.pk])).json()
        self.assertTrue(before['active'])
        self.assertFalse(after['active'])
        self.assertNotEqual(before['revision'], after['revision'])
        self.assertEqual(self.shown(anthony, mine), set())
        self.assertEqual(self.shown(erik, theirs), self.numbers(theirs))


class RepJourneyTests(Scenario):
    def test_a_reps_morning(self):
        _, batches = self.assign(6, [self.today, self.today + timedelta(days=1)], [self.anthony, self.erik])
        mine = self.batch_for(batches, self.anthony, self.today)
        rep = Client()
        self.assertRedirects(rep.post(reverse('login'), {'username': 'anthony.diaz', 'password': PASSWORD}), reverse('dashboard'))
        home = rep.get(reverse('dashboard'))
        self.assertEqual([b.pk for b in home.context['batches']], [mine.pk])
        for manager_page in ('upload_new', 'team', 'audit_log'):
            self.assertNotContains(home, reverse(manager_page))
        self.assertEqual(self.shown(rep, mine), self.numbers(mine))
        for other in batches:
            if other.rep_id != self.anthony.pk:
                for number in self.numbers(other):
                    self.assertNotContains(rep.get(mine.get_absolute_url()), number)
        rep.post(reverse('logout'))
        self.assertEqual(rep.get(mine.get_absolute_url()).status_code, 302)

    def test_lists_unlock_at_midnight_company_time(self):
        first, second = self.today + timedelta(days=1), self.today + timedelta(days=2)
        _, batches = self.assign(4, [first, second], [self.anthony])
        thursday, friday = self.batch_for(batches, self.anthony, first), self.batch_for(batches, self.anthony, second)
        with company_time(datetime.combine(first, time(23, 59))):
            client = self.signed_in(self.anthony)
            self.assertEqual(self.shown(client, thursday), self.numbers(thursday))
            self.assertEqual(self.shown(client, friday), set())
        with company_time(datetime.combine(second, time(0, 1))):
            client = self.signed_in(self.anthony)
            self.assertEqual(self.shown(client, thursday), set())
            self.assertEqual(self.shown(client, friday), self.numbers(friday))

    def test_an_open_list_tab_goes_stale_at_midnight(self):
        day = self.today + timedelta(days=1)
        _, batches = self.assign(2, [day], [self.anthony])
        status = reverse('batch_status', args=[batches[0].pk])
        with company_time(datetime.combine(day, time(23, 59))):
            before = self.signed_in(self.anthony).get(status).json()
        with company_time(datetime.combine(day + timedelta(days=1), time(0, 1))):
            after = self.signed_in(self.anthony).get(status).json()
        self.assertTrue(before['active'])
        self.assertFalse(after['active'])
        self.assertNotEqual(before['revision'], after['revision'])

    def test_paging_through_a_long_list_only_shows_your_numbers(self):
        _, batches = self.assign(260, [self.today], [self.anthony, self.erik])
        mine, theirs = self.batch_for(batches, self.anthony), self.batch_for(batches, self.erik)
        client = self.signed_in(self.anthony)
        seen = set()
        for page in ['1', '2', '3', '99', '-1', '0', 'abc', '1e9', '999999999999999999999']:
            seen |= self.shown(client, mine, page=page)
        self.assertEqual(seen, self.numbers(mine))
        self.assertFalse(seen & self.numbers(theirs))


class RepMischiefTests(Scenario):
    """Things a creative rep might try. None of them should work."""

    def setUp(self):
        super().setUp()
        self.tomorrow = self.today + timedelta(days=1)
        self.upload, self.batches = self.assign(8, [self.today, self.tomorrow], [self.anthony, self.erik])
        self.mine = self.batch_for(self.batches, self.anthony, self.today)
        self.my_tomorrow = self.batch_for(self.batches, self.anthony, self.tomorrow)
        self.coworkers = self.batch_for(self.batches, self.erik, self.today)
        self.draft = import_workbook(xlsx(fake_numbers(3, '646')), self.admin, 'Draft', list_type='ringcentral')
        self.rep = self.signed_in(self.anthony)

    def assertNoLeak(self, response, *batches):
        body = response.content.decode(errors='replace')
        for batch in batches:
            for number in self.numbers(batch):
                self.assertNotIn(number, body)

    def test_guessing_list_addresses_gets_nowhere(self):
        coworker = str(self.coworkers.pk)
        for path in [f'/b/{uuid4()}/', f'/b/{coworker}/', f'/b/{coworker.upper()}/', f'/b/{self.coworkers.pk.hex}/',
                     '/b/1/', f'/b/{self.mine.pk}/../{coworker}/', f'/b/{coworker}/status/', f'/b/{coworker}%00/',
                     f'/uploads/{self.upload.pk}/', f'/b/{coworker}/?rep={self.anthony.pk}']:
            with self.subTest(path=path):
                response = self.rep.get(path)
                self.assertIn(response.status_code, (403, 404))
                self.assertNoLeak(response, self.coworkers, self.my_tomorrow)

    def test_a_coworkers_forwarded_link_is_useless(self):
        response = self.rep.get(self.coworkers.get_absolute_url())
        self.assertEqual(response.status_code, 404)
        self.assertNoLeak(response, self.coworkers)

    def test_poking_every_manager_page_is_refused(self):
        attempts = [
            ('get', reverse('upload_new'), {}), ('post', reverse('upload_new'), {'label': 'x', 'list_type': 'gfs', 'file': xlsx(['(312) 555-0199'])}),
            ('get', self.draft.get_absolute_url(), {}),
            ('post', reverse('upload_publish', args=[self.draft.pk]), {'dates': str(self.today), 'rep_count': 1, 'reps': [self.anthony.pk], 'intent': 'create'}),
            ('post', reverse('upload_previous', args=[self.draft.pk]), {'include': 'yes'}),
            ('post', reverse('upload_classify', args=[self.draft.pk]), {'list_type': 'gfs'}),
            ('post', reverse('upload_close', args=[self.upload.pk]), {}),
            ('post', reverse('batch_close', args=[self.mine.pk]), {}),
            ('post', reverse('batch_close', args=[self.coworkers.pk]), {}),
            ('get', reverse('team'), {}),
            ('post', reverse('team'), {'action': 'create', 'first_name': 'Evil', 'last_name': 'Twin', 'username': 'evil.twin', 'list_types': ['gfs']}),
            ('post', reverse('team'), {'action': 'disable', 'user_id': self.erik.pk}),
            ('get', reverse('team_detail', args=[self.erik.pk]), {}),
            ('post', reverse('team_detail', args=[self.erik.pk]), {'action': 'reset_password'}),
            ('get', reverse('audit_log'), {}),
            ('post', reverse('password_change'), {'old_password': PASSWORD, 'new_password1': 'Totally-New-Pass-99', 'new_password2': 'Totally-New-Pass-99'}),
        ]
        for method, url, data in attempts:
            with self.subTest(method=method, url=url, data=sorted(data)):
                self.assertEqual(getattr(self.rep, method)(url, data).status_code, 403)
        self.assertEqual(ImportBatch.objects.count(), 2)
        self.assertFalse(self.draft.batches.exists())
        self.draft.refresh_from_db()
        self.assertIsNone(self.draft.include_previous)
        self.assertEqual(self.draft.list_type, 'ringcentral')
        self.assertFalse(RepBatch.objects.filter(status='closed').exists())
        self.erik.refresh_from_db()
        self.assertTrue(self.erik.is_active)
        self.assertTrue(self.erik.check_password(PASSWORD))
        self.assertTrue(get_user_model().objects.get(pk=self.anthony.pk).check_password(PASSWORD))
        self.assertFalse(get_user_model().objects.filter(username='evil.twin').exists())

    def test_borrowing_the_managers_date_picker_to_peek_at_tomorrow(self):
        response = self.rep.get(reverse('dashboard'), {'date': self.tomorrow.isoformat()})
        self.assertEqual(response.context['selected_date'], self.today)
        self.assertEqual([b.pk for b in response.context['batches']], [self.mine.pk])
        self.assertEqual(self.shown(self.rep, self.my_tomorrow, date=self.tomorrow.isoformat()), set())

    def test_faking_the_clock_or_timezone_does_not_unlock_tomorrow(self):
        self.rep.cookies['django_timezone'] = 'Pacific/Kiritimati'  # UTC+14, the first place on Earth to see tomorrow
        response = self.rep.get(self.my_tomorrow.get_absolute_url(), HTTP_DATE='Sat, 01 Jan 2050 00:00:00 GMT',
                                HTTP_X_TIMEZONE='Pacific/Kiritimati', HTTP_X_CLIENT_DATE=self.tomorrow.isoformat())
        self.assertFalse(response.context['can_view_numbers'])
        self.assertNoLeak(response, self.my_tomorrow)
        week = self.rep.get(reverse('dashboard'), {'week': self.tomorrow.isoformat()})
        self.assertNoLeak(week, self.my_tomorrow)
        self.assertNotContains(week, self.my_tomorrow.get_absolute_url())

    def test_asking_for_a_spreadsheet_or_json_export_gets_nothing_extra(self):
        url = self.mine.get_absolute_url()
        for suffix in ['?format=csv', '?export=1', '?download=xlsx', '?print=1']:
            with self.subTest(suffix=suffix):
                response = self.rep.get(url + suffix)
                self.assertTrue(response['Content-Type'].startswith('text/html'))
                self.assertNotIn('attachment', response.get('Content-Disposition', ''))
        for path in ['export/', 'export.csv', 'numbers.json', 'download/']:
            with self.subTest(path=path):
                self.assertEqual(self.rep.get(url + path).status_code, 404)
        for accept in ['text/csv', 'application/json', 'application/vnd.ms-excel']:
            with self.subTest(accept=accept):
                self.assertTrue(self.rep.get(url, HTTP_ACCEPT=accept)['Content-Type'].startswith('text/html'))
        status = self.rep.get(reverse('batch_status', args=[self.mine.pk])).json()
        self.assertEqual(set(status), {'active', 'revision', 'reason'})
        self.assertNoLeak(self.rep.get(reverse('batch_status', args=[self.mine.pk])), self.mine)

    def test_unusual_http_methods_change_nothing(self):
        assigned = self.numbers(self.mine)
        for method in ('put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.rep, method)(reverse('batch_close', args=[self.mine.pk])).status_code, 403)
                self.assertEqual(getattr(self.rep, method)(reverse('upload_close', args=[self.upload.pk])).status_code, 403)
                getattr(self.rep, method)(self.mine.get_absolute_url())
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.status, 'open')
        self.assertEqual(self.numbers(self.mine), assigned)
        self.assertEqual(self.rep.get(reverse('logout')).status_code, 405)  # a link or image can't sign you out

    def test_a_booby_trapped_link_cannot_make_the_admin_do_anything(self):
        """A rep emails the admin a link, or hides one in an image tag. Opening it must change nothing."""
        links = [
            reverse('upload_close', args=[self.upload.pk]), reverse('batch_close', args=[self.coworkers.pk]),
            reverse('upload_publish', args=[self.draft.pk]) + f'?dates={self.today}&rep_count=1&reps={self.anthony.pk}&intent=create',
            reverse('upload_previous', args=[self.draft.pk]) + '?include=yes',
            reverse('team') + f'?action=disable&user_id={self.erik.pk}',
            reverse('team_detail', args=[self.erik.pk]) + '?action=reset_password', reverse('logout'),
        ]
        for link in links:
            with self.subTest(link=link):
                self.assertIn(self.manager.get(link).status_code, (200, 302, 405))
        self.assertFalse(self.draft.batches.exists())
        # A look-alike form on another website can't supply the admin's CSRF token either.
        forged = Client(enforce_csrf_checks=True)
        forged.force_login(self.admin)
        for url, data in [(reverse('upload_close', args=[self.upload.pk]), {}),
                          (reverse('batch_close', args=[self.coworkers.pk]), {}),
                          (reverse('upload_previous', args=[self.draft.pk]), {'include': 'yes'}),
                          (reverse('team'), {'action': 'disable', 'user_id': self.erik.pk}),
                          (reverse('team_detail', args=[self.erik.pk]), {'action': 'reset_password'})]:
            with self.subTest(forged=url):
                self.assertEqual(forged.post(url, data).status_code, 403)
        self.assertEqual(self.manager.get(reverse('dashboard')).status_code, 200)  # still signed in
        self.assertFalse(RepBatch.objects.filter(status='closed').exists())
        self.assertFalse(self.draft.batches.exists())
        self.draft.refresh_from_db()
        self.assertIsNone(self.draft.include_previous)
        self.erik.refresh_from_db()
        self.assertTrue(self.erik.is_active and self.erik.check_password(PASSWORD))

    def test_trying_to_promote_yourself_or_join_another_group(self):
        self.rep.post(reverse('team'), {'action': 'create', 'first_name': 'A', 'last_name': 'D', 'username': 'anthony.admin',
                                        'list_types': ['gfs'], 'is_staff': 'on', 'is_superuser': 'on'})
        self.rep.post(reverse('team_detail', args=[self.anthony.pk]), {'action': 'update_memberships', 'list_types': ['gfs', 'ringcentral']})
        self.rep.post(reverse('team_detail', args=[self.anthony.pk]), {'action': 'reset_password'})
        self.anthony.refresh_from_db()
        self.assertFalse(self.anthony.is_staff or self.anthony.is_superuser)
        self.assertEqual(list(self.anthony.list_memberships.values_list('list_type', flat=True)), ['ringcentral'])
        self.assertTrue(self.anthony.check_password(PASSWORD))

    def test_password_guessing_is_stopped_after_five_tries(self):
        guesser = Client(REMOTE_ADDR='10.0.0.66')
        for username in ['erik.anderson', 'Erik.Anderson', ' erik.anderson ', 'ＥＲＩＫ.ＡＮＤＥＲＳＯＮ', 'ERIK.ANDERSON']:
            guesser.post(reverse('login'), {'username': username, 'password': 'Erik2026!'})
        response = guesser.post(reverse('login'), {'username': 'erik.anderson', 'password': PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn('_auth_user_id', guesser.session)

    def test_a_rep_cannot_lock_a_coworker_out_of_their_account(self):
        prankster = Client(REMOTE_ADDR='10.0.0.66')
        for _ in range(5):
            prankster.post(reverse('login'), {'username': 'erik.anderson', 'password': 'wrong'})
        self.assertEqual(prankster.post(reverse('login'), {'username': 'erik.anderson', 'password': PASSWORD}).status_code, 429)
        victim = Client(REMOTE_ADDR='10.0.0.12')
        self.assertEqual(victim.post(reverse('login'), {'username': 'erik.anderson', 'password': PASSWORD}).status_code, 302)

    def test_guessing_spread_across_many_devices_is_still_capped(self):
        for device in range(25):
            Client(REMOTE_ADDR=f'10.2.0.{device}').post(reverse('login'), {'username': 'erik.anderson', 'password': f'guess{device}'})
        response = Client(REMOTE_ADDR='10.2.0.200').post(reverse('login'), {'username': 'erik.anderson', 'password': PASSWORD})
        self.assertEqual(response.status_code, 429)

    def test_spoofed_forwarding_headers_do_not_dodge_the_address_limit(self):
        attacker = Client(REMOTE_ADDR='10.0.0.66')
        for attempt in range(50):
            attacker.post(reverse('login'), {'username': f'nobody{attempt}', 'password': 'x'},
                          HTTP_X_FORWARDED_FOR=f'203.0.113.{attempt}')
        response = attacker.post(reverse('login'), {'username': 'anthony.diaz', 'password': PASSWORD},
                                 HTTP_X_FORWARDED_FOR='198.51.100.1')
        self.assertEqual(response.status_code, 429)
        # In production Apache discards any client-supplied forwarding header before adding the real address.
        apache = (Path(settings.BASE_DIR) / 'deploy/templates/httpd.conf.in').read_text()
        self.assertIn('RequestHeader unset X-Forwarded-For early', apache)

    def test_junk_in_the_login_form_never_crashes(self):
        for username, password in [("' OR '1'='1' --", "' OR '1'='1"), ('anthony.diaz\x00admin', 'x'), ('a' * 10000, 'b'),
                                   ('🦄', '🌈' * 50), ('', ''), ('ada.admin', ''), ('<script>alert(1)</script>', 'x'),
                                   ('anthony.diaz', PASSWORD + '\x00')]:
            with self.subTest(username=username[:20]):
                client = Client(REMOTE_ADDR=f'10.1.{len(username) % 250}.{len(password) % 250}')
                response = client.post(reverse('login'), {'username': username, 'password': password})
                self.assertIn(response.status_code, (200, 400, 429))
                self.assertNotIn('_auth_user_id', client.session)

    def test_login_redirect_cannot_bounce_to_another_site(self):
        for target in ['https://evil.example/', '//evil.example/', '/\\evil.example/', 'https:evil.example',
                       'javascript:alert(1)', '\\\\evil.example', 'http://testserver@evil.example/']:
            with self.subTest(target=target):
                client = Client()
                response = client.post(reverse('login') + '?next=' + quote(target), {
                    'username': 'anthony.diaz', 'password': PASSWORD, 'next': target})
                self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)

    def test_signup_reset_and_admin_backdoors_do_not_exist(self):
        for path in ['/admin/', '/admin/login/', '/register/', '/signup/', '/accounts/signup/', '/password_reset/',
                     '/accounts/password/reset/', '/api/', '/api/batches/', '/.env', '/static/../config/settings.py',
                     '/media/', '/b/', '/uploads/', '/export/', '/__debug__/']:
            with self.subTest(path=path):
                self.assertEqual(self.rep.get(path).status_code, 404)

    def test_garbage_query_strings_never_crash(self):
        garbage = ['\x00', "' OR 1=1--", '9999-12-31', '0001-01-01', '0000-00-00', '2026-13-45', 'a' * 5000,
                   '-1', '1e9', '999999999999999999999', '{{7*7}}', '../../etc/passwd', '%s%s%s']
        manager = self.manager
        for value in garbage:
            with self.subTest(value=value[:20]):
                self.assertEqual(self.rep.get(reverse('dashboard'), {'week': value}).status_code, 200)
                self.assertEqual(self.rep.get(self.mine.get_absolute_url(), {'page': value}).status_code, 200)
                self.assertEqual(manager.get(reverse('dashboard'), {'date': value}).status_code, 200)
                self.assertEqual(manager.get(reverse('audit_log'), {'page': value}).status_code, 200)

    def test_your_calendar_never_mentions_coworkers(self):
        for week in (self.today, self.tomorrow):
            response = self.rep.get(reverse('dashboard'), {'week': week.isoformat()})
            for word in ('Erik', 'Anderson', 'erik.anderson'):
                self.assertNotContains(response, word)
            self.assertNoLeak(response, self.coworkers, self.my_tomorrow)

    def test_a_shared_password_shows_up_in_login_history(self):
        """Reps can't be stopped from sharing passwords, but the admin can see every sign-in and its address."""
        for address in ('203.0.113.7', '198.51.100.9'):
            Client(REMOTE_ADDR=address).post(reverse('login'), {'username': 'anthony.diaz', 'password': PASSWORD})
        history = self.manager.get(reverse('team_detail', args=[self.anthony.pk]))
        self.assertContains(history, '203.0.113.7')
        self.assertContains(history, '198.51.100.9')

    def test_flooding_the_form_with_fields_is_rejected_cleanly(self):
        flood = {f'field{n}': 'x' for n in range(1500)}
        response = Client().post(reverse('login'), {'username': 'anthony.diaz', 'password': PASSWORD, **flood})
        self.assertEqual(response.status_code, 400)

    def test_logging_out_then_pressing_back(self):
        page = self.rep.get(self.mine.get_absolute_url())
        self.assertIn('no-store', page['Cache-Control'])
        self.rep.post(reverse('logout'))
        self.assertEqual(self.rep.get(self.mine.get_absolute_url()).status_code, 302)
        self.assertEqual(self.rep.get(reverse('batch_status', args=[self.mine.pk])).status_code, 302)

    def test_dual_group_rep_removed_from_one_group_keeps_only_the_other(self):
        _, gfs = self.assign(2, [self.today], [self.emilio], label='Donut', list_type='gfs', area='718')
        _, clean = self.assign(2, [self.today], [self.emilio], label='Clean', list_type='ringcentral', area='917')
        self.manager.post(reverse('team_detail', args=[self.emilio.pk]), {'action': 'update_memberships', 'list_types': ['ringcentral']})
        client = self.signed_in(self.emilio)
        self.assertIsNone(self.shown(client, gfs[0]))
        self.assertEqual(self.shown(client, clean[0]), self.numbers(clean[0]))
        self.assertEqual([b.upload.label for b in client.get(reverse('dashboard')).context['batches']], ['Clean'])

    def test_printing_is_blocked_by_the_stylesheet(self):
        css = (Path(settings.BASE_DIR) / 'static/tracker/app.css').read_text()
        print_rules = css.split('@media print', 1)[1]
        self.assertIn('.app-shell, .auth-shell, .privacy-overlay { display: none !important; }', print_rules)
