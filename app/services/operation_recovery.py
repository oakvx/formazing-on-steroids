"""Explicit, selective recovery for proven Notion-only gaps.

The original Teams invitation/email and Telegram messages are NEVER replayed
when their outcome is unknown. A Notion update is idempotent only after the
current remote fields have been checked for conflicting edits.
"""
from .operation_journal import InvalidTransition


class ReconciliationError(RuntimeError):
    pass


async def _get_training(service, training_id):
    training = await service.notion_service.get_formazione_by_id(training_id)
    if not isinstance(training, dict):
        raise ReconciliationError('Formazione Notion non trovata.')
    return training


def _matches(training, fields):
    return all(training.get(name) == value for name, value in fields.items())


async def reconcile_notion(journal, operation_id, actor, training_service):
    record = journal.get(operation_id, include_metadata=True)
    if not record or record['status'] != 'needs_review':
        raise InvalidTransition('Solo operazioni da verificare sono riconciliabili.')
    if record['action'] == 'notification' and record['checkpoint'] == 'notion_pending':
        before, target = 'Programmata', {
            'Codice': record['metadata'].get('code'),
            'Link Teams': record['metadata'].get('teams_link'),
            'Stato': 'Calendarizzata',
        }
        if not all(isinstance(value, str) and value for value in target.values()):
            raise ReconciliationError('Mancano prove persistenti del risultato Microsoft.')
        next_status, next_step = 'ready_telegram', 'telegram_not_started'
    elif record['action'] == 'feedback' and record['checkpoint'] == 'feedback_notion_pending':
        before, target = 'Calendarizzata', {'Stato': 'Conclusa'}
        next_status, next_step = 'completed', 'completed'
    else:
        raise InvalidTransition(
            'Questa fase non è recuperabile automaticamente: controllare i provider.'
        )

    journal.claim(
        operation_id, actor, status='needs_review',
        checkpoint=record['checkpoint'], next_checkpoint='notion_reconcile_attempted',
    )
    try:
        current = await _get_training(training_service, record['training_id'])
        if not _matches(current, target):
            if current.get('Stato') != before:
                raise ReconciliationError('Stato Notion cambiato: è necessaria una verifica manuale.')
            if record['action'] == 'notification':
                # Another operator or an earlier attempt may already have
                # inserted a different meeting link/code. Never overwrite it.
                for name in ('Codice', 'Link Teams'):
                    existing = current.get(name)
                    if existing and existing != target[name]:
                        raise ReconciliationError('Notion contiene dati diversi: non sovrascrivere.')
            await training_service.notion_service.update_formazione(
                record['training_id'], target,
            )
            verified = await _get_training(training_service, record['training_id'])
            if not _matches(verified, target):
                raise ReconciliationError('Impossibile confermare l’aggiornamento Notion.')
        journal.checkpoint(
            operation_id, next_step, actor, status=next_status,
        )
        return next_status
    except Exception:
        journal.review(operation_id, actor)
        raise


async def resume_unsent_telegram(journal, operation_id, actor, training_service):
    """Send only when the journal proves no Telegram dispatch was attempted.

    This resumes the *default* message templates (not the old edited preview).
    A failed send becomes needs_review, never a blind retry.
    """
    record = journal.get(operation_id, include_metadata=True)
    if not record or record['action'] != 'notification':
        raise InvalidTransition('Solo una calendarizzazione può completare Telegram.')
    if record['status'] != 'ready_telegram' or record['checkpoint'] != 'telegram_not_started':
        raise InvalidTransition('Telegram potrebbe già essere stato contattato.')
    journal.claim(
        operation_id, actor, status='ready_telegram',
        checkpoint='telegram_not_started', next_checkpoint='telegram_attempted',
    )
    try:
        remote = await _get_training(training_service, record['training_id'])
        expected = {
            'Codice': record['metadata'].get('code'),
            'Link Teams': record['metadata'].get('teams_link'),
            'Stato': 'Calendarizzata',
        }
        if not _matches(remote, expected):
            raise ReconciliationError('Notion è cambiato: invio Telegram bloccato.')
        results = await training_service.telegram_service.send_training_notification(remote)
        if not all(results.values()):
            raise ReconciliationError(
                'Invio Telegram parziale o incerto: controlla i gruppi manualmente.'
            )
        journal.checkpoint(operation_id, 'completed', actor, status='completed')
        return results
    except Exception:
        journal.review(operation_id, actor)
        raise
