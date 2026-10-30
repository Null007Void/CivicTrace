"""CT-QC-062 reference-client driver: a real browser against the real workbench.

Run by ``tests/test_browser_reference_client.py`` with an interpreter that has the ``playwright``
package (not necessarily the lane venv). It drives the shipped pages only -- no API shortcuts --
and prints one JSON document describing every step.

    python browser_reference_driver.py flow --base-url http://127.0.0.1:PORT --work-dir DIR --out out.json
    python browser_reference_driver.py attack --attacker-url http://127.0.0.1:PORT2/attacker.html --out out.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

TXT = ('SYNTHETIC payment register, row 12: invoice 4471, Synthetic Harbor Lane Lighting Pilot, '
       'paid $4,200 on 2026-08-02 (fixture, not a real record).\n')
EXCERPT = 'paid $4,200 on 2026-08-02 (fixture, not a real record)'
CLAIM_TEXT = 'The synthetic payment register recorded $4,200 paid for the Harbor Lane pilot.'


def _flow(page, base_url: str, workdir: pathlib.Path, steps: list[dict]) -> dict:
    upload = workdir / 'synthetic-payments.txt'
    upload.write_text(TXT, encoding='utf-8')

    page.goto(base_url + '/workbench', wait_until='load')
    steps.append({'step': 'open workbench', 'url': page.url,
                  'referrer': page.evaluate('document.referrer'),
                  'title': page.title()})

    page.fill('input[name="case_title"]', 'SYNTHETIC: browser reference client case')
    page.fill('input[name="project_name"]', 'Synthetic Harbor Lane Lighting Pilot')
    page.fill('input[name="municipality"]', 'Synthetic Bay Municipality (fictional test fixture)')
    page.fill('input[name="jurisdiction_id"]', 'synthetic-bay')
    page.fill('input[name="publisher"]', 'Synthetic Bay Municipality')
    page.fill('input[name="allowed_hosts"]', 'records.synthetic-bay.invalid')

    # The .txt is read *in the page* by /assets/workbench.js: the picker's change handler fills the
    # hidden `filename` and `text_base64` fields the server already accepts. Wait for both, and
    # record them, so a run that silently fell back to the paste box (no upload proof) is visible
    # in the evidence instead of passing.
    page.set_input_files('input[type="file"]', str(upload))
    page.wait_for_function(
        '() => { var f = document.querySelector(\'input[name="filename"]\');'
        ' var b = document.querySelector(\'input[name="text_base64"]\');'
        ' return !!f && !!b && f.value === "synthetic-payments.txt" && b.value.length > 0; }',
        timeout=20000)
    picked = {
        'filename_field': page.input_value('input[name="filename"]'),
        'text_base64_length': len(page.input_value('input[name="text_base64"]')),
        'picker_value_count': page.evaluate(
            "() => document.querySelector('input[type=\"file\"]').files.length"),
        'upload_status': page.inner_text('[data-upload-status]'),
    }
    steps.append({'step': 'pick a .txt file (read in this page, not uploaded elsewhere)',
                  **picked})
    if picked['filename_field'] != 'synthetic-payments.txt' or not picked['text_base64_length']:
        raise AssertionError(f'the picked-file helper did not carry the bytes: {picked}')

    page.fill('input[name="source_title"]', 'SYNTHETIC payment register (fixture)')
    page.fill('input[name="official_url"]',
              'https://records.synthetic-bay.invalid/synthetic-payments.txt')
    page.select_option('select[name="document_role"]', 'OFFICIAL_PAYMENT_REGISTER')
    page.fill('input[name="document_date"]', '2026-08-05')
    page.fill('input[name="as_of"]', '2026-08-05')
    page.fill('input[name="source_anchor"]', 'Row 12')
    with page.expect_navigation(wait_until='load'):
        page.click('form[action="/workbench/cases"] button[type="submit"]')
    steps.append({'step': 'create case with a picked .txt', 'url': page.url,
                  'imported_notice': 'was imported' in page.inner_text('body')})
    if '/workbench/cases/case_' not in page.url:
        raise AssertionError(f'create did not land on a case page: {page.url}\n'
                             f'{page.inner_text("body")[:600]}')
    case_id = page.url.split('/workbench/cases/')[1].split('?')[0]
    if 'created_source=' not in page.url:
        raise AssertionError(f'the picked .txt was not imported by the create form: {page.url}')

    # The source page is the first link in the case page's source list.
    page.click('a[href^="/workbench/cases/"][href*="/sources/src_"]')
    page.wait_for_load_state('load')
    source_url = page.url
    steps.append({'step': 'open source page', 'url': source_url})

    page.fill('input[name="anchor"]', 'Row 12')
    page.fill('textarea[name="exact_excerpt"]', EXCERPT)
    page.select_option('select[name="predicate"]', 'PAID_AMOUNT')
    page.fill('input[name="subject_id"]', 'harbor-lane-lighting')
    page.fill('input[name="action_id"]', 'INVOICE-4471')
    page.fill('input[name="amount_cents"]', '420000')
    page.fill('input[name="event_date"]', '2026-08-02')
    with page.expect_navigation(wait_until='load'):
        page.click('form[action$="/facts"] button[type="submit"]')
    steps.append({'step': 'add fact mapping', 'url': page.url,
                  'notice': 'PENDING_HUMAN_REVIEW' in page.inner_text('body')})
    if 'created_fact=' not in page.url:
        raise AssertionError(f'fact was not recorded: {page.url}\n{page.inner_text("body")[:600]}')
    fact_id = page.url.split('created_fact=')[1].split('&')[0]

    page.goto(f'{base_url}/workbench/cases/{case_id}/facts/{fact_id}', wait_until='load')
    page.select_option('select[name="target"]', 'HUMAN_REVIEWED')
    page.fill('input[name="reviewer"]', 'synthetic-browser-reviewer')
    with page.expect_navigation(wait_until='load'):
        page.click('form[action$="/review"] button[type="submit"]')
    steps.append({'step': 'record human review', 'url': page.url,
                  'state': 'HUMAN_REVIEWED' in page.inner_text('body')})

    page.goto(f'{base_url}/workbench/cases/{case_id}/claims/new?template=PAID_AMOUNT',
              wait_until='load')
    page.fill('textarea[name="text"]', CLAIM_TEXT)
    page.fill('input[name="subject_id"]', 'harbor-lane-lighting')
    page.fill('input[name="action_id"]', 'INVOICE-4471')
    page.fill('input[name="additional_record"]', 'Synthetic bank reconciliation (fixture)')
    page.fill('input[name="amount_cents"]', '420000')
    page.fill('input[name="as_of"]', '2026-08-05')
    page.check(f'input[name="fact_ids"][value="{fact_id}"]')
    with page.expect_navigation(wait_until='load'):
        page.click('form[action$="/claims"] button[type="submit"]')
    steps.append({'step': 'add claim citing the reviewed mapping', 'url': page.url})
    if '/claims/claim_' not in page.url:
        raise AssertionError(f'claim was not recorded: {page.url}\n{page.inner_text("body")[:600]}')
    claim_id = page.url.rsplit('/', 1)[-1]

    page.check('input[name="attest"]')
    page.fill('input[name="reviewer"]', 'synthetic-browser-reviewer')
    with page.expect_navigation(wait_until='load'):
        page.click('button[name="action"][value="confirm"]')
    body = page.inner_text('body')
    steps.append({'step': 'confirm claim with attestation', 'url': page.url,
                  'confirmed': 'CONFIRMED' in body})

    page.goto(f'{base_url}/workbench/cases/{case_id}', wait_until='load')
    case_body = page.inner_text('body')
    steps.append({'step': 'reopen the case page', 'url': page.url,
                  'shows_claim': CLAIM_TEXT in case_body,
                  'shows_revision': 'REVISION' in case_body})
    return {'case_id': case_id, 'claim_id': claim_id, 'case_url': page.url,
            'case_body': case_body}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('flow', 'attack'))
    parser.add_argument('--base-url', default='')
    parser.add_argument('--attacker-url', default='')
    parser.add_argument('--work-dir', default='.')
    parser.add_argument('--out', required=True)
    parser.add_argument('--channel', default='chrome')
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    workdir = pathlib.Path(args.work_dir)
    steps: list[dict] = []
    result: dict = {'mode': args.mode, 'channel': args.channel, 'steps': steps}
    posts: list[dict] = []
    write_responses: list[dict] = []

    with sync_playwright() as play:
        try:
            browser = play.chromium.launch(channel=args.channel, headless=True)
            result['launched'] = args.channel
        except Exception as exc:  # bundled build, and say so
            result['channel_error'] = f'{type(exc).__name__}: {exc}'
            browser = play.chromium.launch(headless=True)
            result['launched'] = 'bundled-chromium'
        context = browser.new_context()
        page = context.new_page()
        page.on('request', lambda r: posts.append({
            'method': r.method, 'url': r.url,
            'origin': r.headers.get('origin'),
            'content_type': r.headers.get('content-type')})
            if r.method in ('POST', 'PUT', 'PATCH', 'DELETE') else None)
        # The status a write actually received. Playwright's request object does not expose the
        # Fetch Metadata headers or the response, so the browser's own view is recorded here and the
        # server-side header shape is asserted by tests/test_browser_origin_proof.py.
        page.on('response', lambda r: write_responses.append({
            'method': r.request.method, 'url': r.url, 'status': r.status})
            if r.request.method in ('POST', 'PUT', 'PATCH', 'DELETE') else None)
        result['user_agent'] = page.evaluate('navigator.userAgent')
        try:
            if args.mode == 'flow':
                result.update(_flow(page, args.base_url, workdir, steps))
            else:
                page.goto(args.attacker_url, wait_until='load')
                page.wait_for_timeout(2500)
                result['final_url'] = page.url
                result['final_body'] = page.evaluate(
                    'document.body ? document.body.innerText : ""')[:600]
        finally:
            context.close()
            browser.close()

    result['posts'] = posts
    result['write_responses'] = write_responses
    pathlib.Path(args.out).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'mode': result['mode'], 'launched': result.get('launched'),
                      'steps': steps, 'posts': posts,
                      'write_responses': write_responses}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
