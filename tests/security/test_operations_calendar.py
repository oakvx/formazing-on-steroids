"""Offline operation/calendar tests: no Microsoft, Telegram or Notion calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = '_formazing_offline_operations'
pkg = ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / 'app/services')]
sys.modules[PACKAGE] = pkg


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        PACKAGE + '.' + name, ROOT / 'app/services' / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


journal_module = load('operation_journal', 'operation_journal.py')
recovery = load('operation_recovery', 'operation_recovery.py')
schedule = load('schedule_service', 'schedule_service.py')
OperationJournal = journal_module.OperationJournal
DuplicateOperation = journal_module.DuplicateOperation
InvalidTransition = journal_module.InvalidTransition


def training(name, notion_id, when, areas, state='Programmata', **extra):
    return {'Nome': name, '_notion_id': notion_id, 'Data/Ora': when,
            'Area': areas, 'Stato': state, **extra}


def journal(tmp_path):
    return OperationJournal(tmp_path / 'private' / 'operations.sqlite3')


def test_idempotency_survives_new_instances_and_audits_actor(tmp_path):
    first = journal(tmp_path)
    attempt = first.begin('notion-1', 'notification', 'admin@example.it')
    attempt.step('microsoft_attempted')
    attempt.step('notion_pending', metadata={'code': 'IT-001', 'teams_link': 'https://teams.test/a'})
    attempt.review()
    second = journal(tmp_path)
    row = second.get(attempt.id, include_metadata=True)
    assert row['status'] == 'needs_review'
    assert row['checkpoint'] == 'notion_pending'
    assert row['metadata']['code'] == 'IT-001'
    assert second.events(attempt.id)[0]['event'] == 'manual_review_required'
    with pytest.raises(DuplicateOperation):
        second.begin('notion-1', 'notification', 'admin@example.it')
    # Feedback is a different, independent action.
    second.begin('notion-1', 'feedback', 'admin@example.it')
    assert len(second.recent()) == 2
    # No private metadata is returned by the list API.
    assert 'metadata' not in second.recent()[0]


def test_atomic_concurrent_confirmations_allow_only_one_attempt(tmp_path):
    db = journal(tmp_path)
    def start(_):
        try:
            return db.begin('same-notion-id', 'notification', 'admin@example.it').id
        except DuplicateOperation:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(start, range(8)))
    assert len([value for value in results if value]) == 1
    assert len(db.recent()) == 1


class FakeNotion:
    def __init__(self, row):
        self.row = dict(row)
        self.updates = []

    async def get_formazione_by_id(self, notion_id):
        return dict(self.row)

    async def update_formazione(self, notion_id, values):
        self.updates.append((notion_id, dict(values)))
        self.row.update(values)
        return True


class FakeTelegram:
    def __init__(self, results=None):
        self.calls = 0
        self.results = results if results is not None else {'IT': True}

    async def send_training_notification(self, row):
        self.calls += 1
        return self.results


def setup_notion_failure(tmp_path, initial=None):
    db = journal(tmp_path)
    attempt = db.begin('notion-2', 'notification', 'admin@example.it')
    attempt.step('microsoft_attempted')
    attempt.step('notion_pending', metadata={
        'code': 'ABC-001', 'teams_link': 'https://teams.test/meeting'
    })
    attempt.review()
    notion = FakeNotion(initial or {
        'Stato': 'Programmata', 'Codice': '', 'Link Teams': '',
        'Nome': 'Sessione IT', 'Area': ['IT'],
    })
    telegram = FakeTelegram()
    return db, attempt, SimpleNamespace(notion_service=notion, telegram_service=telegram)


def test_notional_retry_only_and_safe_unsent_telegram_resume(tmp_path):
    db, attempt, services = setup_notion_failure(tmp_path)
    result = asyncio.run(recovery.reconcile_notion(
        db, attempt.id, 'admin@example.it', services
    ))
    assert result == 'ready_telegram'
    assert len(services.notion_service.updates) == 1
    assert services.telegram_service.calls == 0
    assert db.get(attempt.id)['checkpoint'] == 'telegram_not_started'
    asyncio.run(recovery.resume_unsent_telegram(
        db, attempt.id, 'admin@example.it', services
    ))
    assert services.telegram_service.calls == 1
    assert db.get(attempt.id)['status'] == 'completed'
    with pytest.raises(InvalidTransition):
        asyncio.run(recovery.resume_unsent_telegram(
            db, attempt.id, 'admin@example.it', services
        ))
    assert services.telegram_service.calls == 1


def test_notional_receipt_mismatch_blocks_recovery(tmp_path):
    db, attempt, services = setup_notion_failure(tmp_path, {
        'Stato': 'Calendarizzata', 'Codice': 'OTHER-CODE',
        'Link Teams': 'https://teams.test/another',
    })
    with pytest.raises(recovery.ReconciliationError):
        asyncio.run(recovery.reconcile_notion(db, attempt.id, 'admin@example.it', services))
    assert services.notion_service.updates == []
    assert services.telegram_service.calls == 0
    assert db.get(attempt.id)['status'] == 'needs_review'


def test_telegram_must_not_retry_after_unknown_send_outcome(tmp_path):
    db, attempt, services = setup_notion_failure(tmp_path)
    attempt.step('telegram_attempted')
    attempt.review()
    with pytest.raises(InvalidTransition):
        asyncio.run(recovery.resume_unsent_telegram(
            db, attempt.id, 'admin@example.it', services
        ))
    assert services.telegram_service.calls == 0


def test_failed_resumed_telegram_never_runs_twice(tmp_path):
    db, attempt, services = setup_notion_failure(tmp_path)
    asyncio.run(recovery.reconcile_notion(db, attempt.id, 'admin@example.it', services))
    services.telegram_service.results = {'IT': False}
    with pytest.raises(recovery.ReconciliationError):
        asyncio.run(recovery.resume_unsent_telegram(
            db, attempt.id, 'admin@example.it', services
        ))
    assert db.get(attempt.id)['status'] == 'needs_review'
    with pytest.raises(InvalidTransition):
        asyncio.run(recovery.resume_unsent_telegram(
            db, attempt.id, 'admin@example.it', services
        ))
    assert services.telegram_service.calls == 1


def test_feedback_recovers_only_notion_after_confirmed_messages(tmp_path):
    db = journal(tmp_path)
    attempt = db.begin('notion-3', 'feedback', 'admin@example.it')
    attempt.step('telegram_attempted')
    attempt.step('feedback_notion_pending')
    attempt.review()
    notion = FakeNotion({'Stato': 'Calendarizzata'})
    tg = FakeTelegram()
    result = asyncio.run(recovery.reconcile_notion(
        db, attempt.id, 'admin@example.it',
        SimpleNamespace(notion_service=notion, telegram_service=tg),
    ))
    assert result == 'completed'
    assert notion.updates == [('notion-3', {'Stato': 'Conclusa'})]
    assert tg.calls == 0


def test_notional_calendar_search_and_conflicts():
    data = {
        'Programmata': [
            training('Cyber security', 'a', '09/11/2026 10:00', ['IT'], 'Programmata',
                     **{'Numero Partecipanti': 12}),
            training('Network security', 'b', '09/11/2026 10:00', ['IT'], 'Programmata'),
            training('HR session', 'c', '09/11/2026 10:00', ['HR'], 'Programmata'),
        ],
        'Calendarizzata': [
            training('Cyber security', 'd', '10/11/2026 15:00', ['IT'], 'Calendarizzata'),
        ],
        'Conclusa': [],
    }
    model = schedule.build_calendar(data, '2026-11')
    assert model['month'] == '2026-11'
    assert sum(len(day['events']) for week in model['weeks'] for day in week) == 4
    assert len(model['conflicts']) == 1
    assert model['conflicts'][0]['left_id'] == 'a'
    assert model['conflicts'][0]['right_id'] == 'b'
    filtered = schedule.build_calendar(data, '2026-11', query='cyber', area='IT')
    assert len(filtered['records']) == 2
    assert filtered['conflicts'] == []
    by_state = schedule.filter_trainings(schedule.all_trainings(data), status='Calendarizzata')
    assert len(by_state) == 1
    assert schedule.filter_trainings(schedule.all_trainings(data), query='12') == []


def test_calendar_month_boundaries_duplicates_and_invalid_inputs():
    data = {'Programmata': [
        training('A', 'a', '01/01/2027 08:00', ['IT']),
        training('a', 'b', '01/01/2027 08:00', ['HR']),
    ], 'Calendarizzata': [], 'Conclusa': []}
    january = schedule.build_calendar(data, '2027-01')
    assert january['previous'] == '2026-12'
    assert january['following'] == '2027-02'
    assert january['conflicts'][0]['reason'] == 'Possibile duplicato'
    assert schedule.parse_training_date('not a date') is None
    for wrong in ('2026-13', 'xx-11', '2026-11-01', '0000-01'):
        with pytest.raises(ValueError):
            schedule.build_calendar(data, wrong)
    with pytest.raises(ValueError):
        schedule.filter_trainings([], query='x' * 101)
    with pytest.raises(ValueError):
        schedule.filter_trainings([], status='Superuser')


def test_http_contract_covers_csrf_admin_and_no_duplicate_replay():
    routes = (ROOT / 'app/routes.py').read_text(encoding='utf8')
    service = (ROOT / 'app/services/training_service.py').read_text(encoding='utf8')
    operations_ui = (ROOT / 'app/templates/pages/operations.html').read_text(encoding='utf8')
    assert "journal.begin(training_id, 'notification', actor)" in routes
    assert "journal.begin(training_id, 'feedback', actor)" in routes
    assert "progress=attempt" in routes
    assert "@main.route('/operations/<operation_id>/reconcile-notion', methods=['POST'])" in routes
    assert "@main.route('/operations/<operation_id>/resume-telegram', methods=['POST'])" in routes
    assert 'def operations():' in routes and '@admin_required' in routes
    assert "progress.step('microsoft_attempted')" in service
    assert "progress.step('telegram_attempted')" in service
    assert "progress.step('feedback_notion_pending')" in service
    assert "name=\"csrf_token\"" in operations_ui
    assert 'Riconcilia solo Notion' in operations_ui


def test_calendar_and_participants_pages_do_not_mutate_notion_on_get():
    routes = (ROOT / 'app/routes.py').read_text()
    calendar_html = (ROOT / 'app/templates/pages/calendar.html').read_text()
    participants = (ROOT / 'app/templates/pages/participants.html').read_text()
    assert "@main.route('/calendar')" in routes
    assert "@main.route('/participants/<training_id>')" in routes
    assert 'build_calendar(' in routes
    assert 'method="POST"' in participants
    assert 'name="csrf_token"' in participants
    assert 'method="get"' in calendar_html
