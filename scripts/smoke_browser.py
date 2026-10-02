"""Exercise the real browser workflow on an explicitly marked local demo only.

Run after seed_demo using requirements-dev.txt. This adds a synthetic test upload
and closes it; it never deletes history or uses a live company account.
"""
import argparse
from datetime import date, timedelta
from io import BytesIO
import json
from pathlib import Path
import random
from datetime import datetime, timezone
from urllib.parse import urlparse

from openpyxl import Workbook
from playwright.sync_api import sync_playwright, expect


def run(base_url, credentials_path):
    if urlparse(base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('Browser smoke tests are restricted to loopback demo environments.')
    credentials = json.loads(credentials_path.read_text())
    if credentials['manager']['username'] != 'demo.manager':
        raise SystemExit('Use generated demo accounts only.')
    output = credentials_path.parent / 'browser-smoke'
    output.mkdir(parents=True, exist_ok=True)
    errors = []

    def sign_in(page, account):
        page.get_by_label('Username', exact=True).fill(account['username'])
        page.get_by_label('Password', exact=True).fill(account['password'])
        page.get_by_role('button', name='Sign in', exact=False).click()
        expect(page.get_by_text('Demo workspace', exact=True)).to_be_visible()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        manager_context = browser.new_context(viewport={'width': 1440, 'height': 1050})
        manager = manager_context.new_page()
        manager.on('pageerror', lambda err: errors.append(str(err)))
        manager.goto(base_url)
        sign_in(manager, credentials['manager'])
        manager.goto(base_url + '/uploads/new/')
        label = 'DEMO · Browser workflow verification ' + str(random.randrange(100000, 999999))
        manager.get_by_label('List name', exact=False).fill(label)
        area = random.choice(['415', '646', '312', '617', '202'])
        offset = random.randrange(0, 80)
        numbers = [f'{area}55501{index:02}' for index in range(offset, offset + 20)]
        book = Workbook()
        book.active.append(['Phone'])
        for number in numbers:
            book.active.append([number])
        book.active.append([numbers[0]])
        book.active.append(['invalid-number'])
        stream = BytesIO()
        book.save(stream)
        manager.get_by_label('List type', exact=True).select_option('ringcentral')
        manager.locator('input[type=file]').set_input_files({'name': 'browser-check.xlsx', 'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'buffer': stream.getvalue()})
        manager.get_by_role('button', name='Upload and review').click()
        expect(manager.get_by_role('heading', name=label)).to_be_visible()
        expect(manager.locator('.row-duplicate')).to_have_count(1)
        expect(manager.locator('.row-invalid')).to_have_count(1)
        expect(manager.locator('.row-ready')).to_have_count(20)
        form = manager.locator('[data-split-form]')
        today = date.fromisoformat(form.get_attribute('data-today'))
        for day in (today, today + timedelta(days=1)):  # day buttons; today starts ticked
            if not form.locator(f'input[name=days][value="{day.isoformat()}"]').is_checked():
                form.locator(f'label:has(input[name=days][value="{day.isoformat()}"])').click()
        manager.get_by_label('Split into how many people?', exact=True).fill('2')
        form.locator('input[name=reps]').nth(0).check()
        form.locator('input[name=reps]').nth(1).check()
        manager.get_by_role('button', name='Preview split', exact=True).click()
        expect(manager.get_by_role('heading', name='Distribution preview')).to_be_visible()
        manager.get_by_role('button', name='Create batches', exact=True).click()
        expect(manager.locator('.assignment-row')).to_have_count(4)
        upload_url = manager.url
        cards = manager.locator('.assignment-row')
        first_rep_name = cards.nth(0).locator('h3').inner_text().split()[0].lower()
        own_path = cards.nth(0).locator('a[href^="/b/"]').get_attribute('href')
        other_path = cards.nth(1).locator('a[href^="/b/"]').get_attribute('href')
        future_path = cards.nth(2).locator('a[href^="/b/"]').get_attribute('href')
        account = next(rep for rep in credentials['reps'] if rep['username'] == 'demo.' + first_rep_name)

        rep_context = browser.new_context(viewport={'width': 1280, 'height': 950})
        rep = rep_context.new_page()
        rep.on('pageerror', lambda err: errors.append(str(err)))
        rep.goto(base_url + own_path)
        sign_in(rep, account)
        assert rep.url == base_url + own_path, 'Batch shortcut did not survive sign-in.'
        # Assigned lists appear in the rep's own account without a shared link.
        rep.goto(base_url + '/')
        expect(rep.locator(f'#assigned-batches a[href="{own_path}"]')).to_be_visible()
        rep.goto(base_url + own_path)
        expect(rep.locator('.lead-number-table tbody tr')).to_have_count(5)
        expect(rep.locator('.phone-number')).to_have_count(5)
        expect(rep.locator('.number-data form')).to_have_count(0)
        expect(rep.locator('.number-data button, .number-data input, .number-data select, .number-data textarea')).to_have_count(0)
        rep.screenshot(path=str(output / 'rep-list.png'), full_page=True)
        # Visibility protection is only a privacy aid, not screenshot detection.
        rep.evaluate("window.dispatchEvent(new Event('blur'))")
        expect(rep.locator('#privacy-overlay')).to_be_visible()
        rep.get_by_role('button', name='Resume workspace').click()
        expect(rep.locator('#privacy-overlay')).to_be_hidden()
        response = rep.goto(base_url + other_path)
        assert response.status == 404, 'Another rep can read the batch.'
        rep.goto(base_url + future_path)
        expect(rep.locator('.phone-number')).to_have_count(0)
        expect(rep.get_by_role('heading', name='This list is unavailable', exact=True)).to_be_visible()
        rep.goto(base_url + own_path)
        expect(rep.locator('.phone-number')).to_have_count(5)
        # Revoke only the synthetic batch created by this test.
        manager.goto(base_url + own_path)
        manager.on('dialog', lambda dialog: dialog.accept())
        manager.get_by_role('button', name='Clear this batch', exact=True).click()
        (output / 'resume.json').write_text(json.dumps({'batch_path': own_path, 'upload_url': upload_url, 'username': account['username'], 'label': label}))
        expect(rep.locator('[data-batch-expired]')).to_be_visible(timeout=18000)
        expect(rep.locator('.number-data')).to_be_hidden()
        rep.get_by_role('link', name='Refresh this list', exact=True).click()
        expect(rep.locator('.phone-number')).to_have_count(0)
        expect(rep.get_by_role('heading', name='This list is unavailable', exact=True)).to_be_visible()

        manager.goto(upload_url)
        if manager.locator('#privacy-overlay').is_visible():
            manager.get_by_role('button', name='Resume workspace').click()
        expect(manager.get_by_role('heading', name=label, exact=True)).to_be_visible()
        manager.get_by_role('button', name='Clear this upload', exact=True).click()
        expect(manager.get_by_text('This upload is closed.', exact=True)).to_be_visible()
        expect(manager.locator('.assignment-row')).to_have_count(4)
        expect(manager.locator('.row-ready')).to_have_count(20)
        rep.goto(base_url + own_path)
        expect(rep.locator('.phone-number')).to_have_count(0)
        manager.goto(base_url + '/audit/')
        expect(manager.get_by_role('heading', name='History', exact=True)).to_be_visible()
        manager.screenshot(path=str(output / 'history.png'), full_page=True)
        assert not errors, errors
        browser.close()
    (output / 'result.json').write_text(json.dumps({'passed': True, 'completed_at': datetime.now(timezone.utc).isoformat()}))
    print('PASS: upload, warnings, split preview, assignment, account links, read-only rep list, future/owner denial, privacy overlay, live clear, zero numbers after refresh, retained history. No browser exceptions.')


def resume(base_url, credentials_path):
    """Finish a demo-only regression that was interrupted after clearing a batch."""
    if urlparse(base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('Resume is restricted to loopback demo environments.')
    credentials = json.loads(credentials_path.read_text())
    output = credentials_path.parent / 'browser-smoke'
    state = json.loads((output / 'resume.json').read_text())
    if credentials['manager']['username'] != 'demo.manager' or not state['username'].startswith('demo.'):
        raise SystemExit('Use generated demo accounts only.')
    if not state['batch_path'].startswith('/b/') or not state['upload_url'].startswith(base_url + '/uploads/'):
        raise SystemExit('Invalid demo resume paths.')
    account = next(rep for rep in credentials['reps'] if rep['username'] == state['username'])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        rep = browser.new_page(viewport={'width': 1280, 'height': 950})
        rep.goto(base_url + state['batch_path'])
        rep.get_by_label('Username', exact=True).fill(account['username'])
        rep.get_by_label('Password', exact=True).fill(account['password'])
        rep.get_by_role('button', name='Sign in').click()
        expect(rep.get_by_text('Demo workspace', exact=True)).to_be_visible()
        expect(rep.locator('.phone-number')).to_have_count(0)
        expect(rep.get_by_role('heading', name='This list is unavailable', exact=True)).to_be_visible()
        manager = browser.new_page(viewport={'width': 1440, 'height': 1050})
        manager.goto(state['upload_url'])
        manager.get_by_label('Username', exact=True).fill(credentials['manager']['username'])
        manager.get_by_label('Password', exact=True).fill(credentials['manager']['password'])
        manager.get_by_role('button', name='Sign in').click()
        expect(manager.get_by_text('Demo workspace', exact=True)).to_be_visible()
        upload_label = manager.locator('h1').inner_text()
        if not upload_label.startswith('DEMO · Browser workflow verification '):
            raise SystemExit('Resume may clear only a synthetic browser-verification upload.')
        if state.get('label') and upload_label != state['label']:
            raise SystemExit('The resume upload does not match this test.')
        manager.on('dialog', lambda dialog: dialog.accept())
        clear_upload = manager.get_by_role('button', name='Clear this upload', exact=True)
        if clear_upload.count():
            clear_upload.click()
        expect(manager.get_by_text('This upload is closed.', exact=True)).to_be_visible()
        expect(manager.locator('.assignment-row')).to_have_count(4)
        expect(manager.locator('.row-ready')).to_have_count(20)
        rep.reload()
        expect(rep.locator('.phone-number')).to_have_count(0)
        manager.goto(base_url + '/audit/')
        expect(manager.get_by_role('heading', name='History', exact=True)).to_be_visible()
        manager.screenshot(path=str(output / 'history.png'), full_page=True)
        browser.close()
    (output / 'result.json').write_text(json.dumps({'passed': True, 'resumed': True, 'completed_at': datetime.now(timezone.utc).isoformat()}))
    print('PASS: the cleared rep list exposes zero numbers; the synthetic upload is closed with history retained; History displayed. Earlier upload/split/access/clear assertions completed before interruption.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--credentials', type=Path, default=Path('.local/preview/demo-credentials.json'))
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    (resume if args.resume else run)(args.base_url.rstrip('/'), args.credentials)
