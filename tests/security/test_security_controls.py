"""Offline security tests: no Microsoft, Telegram or Notion connections."""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask

ROOT = Path(__file__).resolve().parents[2]


def load_source(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


security = load_source('formazing_security_test', 'app/security.py')
logs = load_source('formazing_logs_test', 'app/services/log_service.py')


def test_csrf_is_per_session_and_rejects_missing_or_invalid_tokens():
    app = Flask(__name__)
    app.secret_key = 'test-only-' * 6

    @app.get('/token')
    def get_token():
        return security.csrf_token()

    @app.post('/send')
    def send():
        security.verify_csrf()
        return 'accepted'

    client = app.test_client()
    assert client.post('/send').status_code == 403
    token = client.get('/token').get_data(as_text=True)
    assert len(token) >= 32
    assert client.get('/token').get_data(as_text=True) == token
    assert client.post('/send', data={'csrf_token': 'wrong'}).status_code == 403
    assert client.post('/send', data={'csrf_token': token}).status_code == 200
    assert app.test_client().post('/send', data={'csrf_token': token}).status_code == 403


@pytest.mark.parametrize('email,domains,allowed', [
    ('me@jemore.it', 'jemore.it', True),
    ('ME@JEMORE.IT', 'other.it, JEMORE.IT ', True),
    ('me@jemore.it.attacker.com', 'jemore.it', False),
    ('me@fakejemore.it', 'jemore.it', False),
    ('me@sub.jemore.it', 'jemore.it', False),
    ('me@jemore.it@other.com', 'jemore.it', False),
    ('missing-at', 'jemore.it', False),
    ('', 'jemore.it', False),
])
def test_domain_whitelist_matches_exactly(email, domains, allowed):
    assert security.allowed_domain(email, domains) is allowed


@pytest.mark.parametrize('candidate', [
    'https://evil.test', '//evil.test', '/%2fevil.test',
    '/%5c%5cevil.test', '/\\evil.test', 'javascript:alert(1)',
    ' /dashboard', '/dashboard\nLocation: https://evil.test',
])
def test_loading_redirect_rejects_external_and_encoded_targets(candidate):
    assert security.safe_local_path(candidate) is None


@pytest.mark.parametrize('candidate', [
    '/', '/dashboard', '/dashboard?force_refresh=1',
    '/preview/feedback/abc-123', '/analytics',
])
def test_loading_redirect_accepts_absolute_local_paths(candidate):
    assert security.safe_local_path(candidate) == candidate


def test_log_search_filters_level_query_and_retains_recent_bounded_lines(tmp_path):
    root = tmp_path / 'formazing.log'
    root.write_text(
        '2026-11-09 09:00:00 | INFO  | app | startup\n'
        '2026-11-09 09:00:01 | ERROR | app | api_key=TOPSECRET failed\n'
        'Traceback: token=ALSOSECRET\n'
        '2026-11-09 09:00:02 | ERROR | app | another failure\n',
        encoding='utf-8',
    )
    svc = logs.LogService.__new__(logs.LogService)
    svc._log_dir = str(tmp_path)
    svc._log_basename = 'formazing.log'
    start = datetime(2026, 11, 9, 0, 0)
    end = datetime(2026, 11, 9, 23, 59)
    result = svc.get_filtered_logs(start, end, level='ERROR', limit=2)
    assert result['count'] == 2
    assert result['truncated'] is True
    assert 'another failure' in result['logs'][-1]
    assert 'TOPSECRET' not in '\n'.join(result['logs'])
    found = svc.get_filtered_logs(start, end, level='ERROR', query='api_key', limit=50)
    assert found['truncated'] is False
    assert found['count'] == 2
    assert '[REDACTED]' in '\n'.join(found['logs'])
    assert 'TOPSECRET' not in '\n'.join(found['logs'])
    assert 'ALSOSECRET' not in '\n'.join(found['logs'])
    assert svc.get_filtered_logs(start, end, level='WARNING')['count'] == 0


@pytest.mark.parametrize('kwargs', [
    {'limit': 0}, {'limit': 501}, {'level': 'SUPERUSER'}, {'query': 'A' * 101},
])
def test_log_search_fails_closed_for_invalid_filters(tmp_path, kwargs):
    svc = logs.LogService.__new__(logs.LogService)
    svc._log_dir = str(tmp_path)
    svc._log_basename = 'formazing.log'
    with pytest.raises(ValueError):
        svc.get_filtered_logs(datetime(2026, 1, 1), datetime(2026, 1, 2), **kwargs)


def test_log_reader_does_not_follow_symlinks(tmp_path):
    outside = tmp_path.parent / 'outside-formazing.log'
    outside.write_text('2026-11-09 09:00:00 | ERROR | app | secret\n')
    (tmp_path / 'formazing.log').symlink_to(outside)
    svc = logs.LogService.__new__(logs.LogService)
    svc._log_dir = str(tmp_path)
    svc._log_basename = 'formazing.log'
    assert svc._discover_log_files() == []


def test_route_and_template_regressions():
    routes = (ROOT / 'app/routes.py').read_text()
    factory = (ROOT / 'app/__init__.py').read_text()
    loading = (ROOT / 'app/templates/pages/loading.html').read_text()
    confirmation = (ROOT / 'app/templates/molecules/preview_action_form.html').read_text()
    row = (ROOT / 'app/templates/molecules/formazione_row.html').read_text()
    assert "@main.route('/sync-attendance/<training_id>', methods=['POST'])" in routes
    assert 'oauth_state' in routes and 'compare_digest' in routes
    assert 'safe_local_path(request.args.get' in routes
    assert 'request.method in' in factory and 'verify_csrf()' in factory
    assert 'dev-secret-key' in factory and 'raise RuntimeError' in factory
    assert 'next_url|tojson' in loading
    assert 'next_url|safe' not in loading
    assert 'name="csrf_token"' in confirmation
    assert 'name="csrf_token"' in row
    assert "url_for('main.preview_notification_page'" in row
    assert "url_for('main.preview_feedback_page'" in row
    assert 'showPreview(' not in row
    assert 'get_filtered_logs' in routes and 'len(query) > 100' in routes
    requirements = (ROOT / 'requirements.txt').read_text()
    assert 'python-telegram-bot==22.4' in requirements
    assert '\ntelegram==0.0.1' not in requirements
