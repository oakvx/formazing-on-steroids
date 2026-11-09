"""Pure calendar/search functions over the existing Notion dashboard snapshot.

Notion's 'Durata' is populated by attendance reconciliation and does NOT
necessarily represent the booked time slot. Therefore the conflict detector
flags only coincident start times, not ungrounded duration overlaps.
"""
import calendar
from datetime import datetime, date
from itertools import combinations


STATES = ('Programmata', 'Calendarizzata', 'Conclusa')


def parse_training_date(value):
    if not isinstance(value, str):
        return None
    for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S',
                '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            pass
    return None


def all_trainings(snapshot):
    """Return uniquely identified records without modifying the cache."""
    results, seen = [], set()
    for state in STATES:
        for training in snapshot.get(state, []):
            if not isinstance(training, dict):
                continue
            notion_id = training.get('_notion_id') or training.get('id')
            if not isinstance(notion_id, str) or not notion_id or notion_id in seen:
                continue
            seen.add(notion_id)
            row = dict(training)
            row['Stato'] = row.get('Stato') or state
            results.append(row)
    return results


def _areas(record):
    area = record.get('Area') or []
    if isinstance(area, str):
        area = area.split(',')
    return [str(item).strip() for item in area if str(item).strip()]


def filter_trainings(records, *, query='', area='', status=''):
    if len(query) > 100 or len(area) > 60 or status not in ('', *STATES):
        raise ValueError('Filtri non validi')
    needle = query.strip().casefold()
    area = area.strip().casefold()
    filtered = []
    for training in records:
        fields = (
            training.get('Nome') or '',
            training.get('Codice') or '',
            training.get('Periodo') or '',
            training.get('Stato') or '',
            training.get('Data/Ora') or '',
            training.get('_notion_id') or '',
            training.get('Partecipanti') or '',
            ' '.join(_areas(training)),
        )
        if needle and needle not in ' '.join(str(field) for field in fields).casefold():
            continue
        if area and not any(item.casefold() == area for item in _areas(training)):
            continue
        if status and training.get('Stato') != status:
            continue
        filtered.append(training)
    return filtered


def potential_conflicts(records):
    """Flag overlapping areas at the same start, or duplicate titles.

    Unknown durations are *not* assumed. Results are warnings only and do not
    block any Notion operation. Recheck with Microsoft before booking.
    """
    indexed = {}
    for training in records:
        moment = parse_training_date(training.get('Data/Ora'))
        if moment:
            indexed.setdefault(moment, []).append(training)
    collisions = []
    for moment, entries in sorted(indexed.items()):
        for left, right in combinations(entries, 2):
            left_areas = {a.casefold() for a in _areas(left)}
            right_areas = {a.casefold() for a in _areas(right)}
            intersects = bool(left_areas & right_areas or
                              'all' in left_areas or 'all' in right_areas)
            duplicate = bool(
                (left.get('Nome') or '').strip().casefold() and
                (left.get('Nome') or '').strip().casefold() ==
                (right.get('Nome') or '').strip().casefold()
            )
            if intersects or duplicate:
                collisions.append({
                    'when': moment.strftime('%d/%m/%Y %H:%M'),
                    'left_id': left.get('_notion_id') or left.get('id'),
                    'right_id': right.get('_notion_id') or right.get('id'),
                    'left_name': left.get('Nome', ''),
                    'right_name': right.get('Nome', ''),
                    'reason': 'Possibile duplicato' if duplicate else 'Partenza coincidente per area',
                })
    return collisions


def build_calendar(snapshot, month=None, *, query='', area='', status=''):
    """Calendar weeks and filtered training events, safe for Jinja2 display."""
    today = date.today()
    if not month:
        year, number = today.year, today.month
    else:
        try:
            if not isinstance(month, str) or len(month) != 7 or month[4] != '-':
                raise ValueError
            year, number = map(int, month.split('-'))
            if not 2000 <= year <= 2100 or not 1 <= number <= 12:
                raise ValueError
        except ValueError as exc:
            raise ValueError('Il mese deve avere formato YYYY-MM.') from exc

    all_rows = all_trainings(snapshot)
    visible = filter_trainings(all_rows, query=query, area=area, status=status)
    event_days = {}
    for training in visible:
        start = parse_training_date(training.get('Data/Ora'))
        if start and start.year == year and start.month == number:
            event_days.setdefault(start.day, []).append({
                'id': training.get('_notion_id') or training.get('id'),
                'name': training.get('Nome') or 'Senza titolo',
                'time': start.strftime('%H:%M'),
                'area': ', '.join(_areas(training)),
                'status': training.get('Stato', ''),
                'participants': training.get('Numero Partecipanti'),
            })
    for entries in event_days.values():
        entries.sort(key=lambda event: (event['time'], event['name'].casefold()))

    grid = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, number):
        grid.append([{
            'date': day.isoformat(),
            'day': day.day,
            'in_month': day.month == number,
            'today': day == today,
            'events': event_days.get(day.day, []) if day.month == number else [],
        } for day in week])

    previous = date(year - 1, 12, 1) if number == 1 else date(year, number - 1, 1)
    following = date(year + 1, 1, 1) if number == 12 else date(year, number + 1, 1)
    selected = date(year, number, 1)
    return {
        'month': selected.strftime('%Y-%m'),
        'month_label': selected.strftime('%m/%Y'),
        'previous': previous.strftime('%Y-%m'),
        'following': following.strftime('%Y-%m'),
        'weeks': grid,
        'records': sorted(visible, key=lambda item:
                          parse_training_date(item.get('Data/Ora')) or datetime.max),
        'conflicts': [item for item in potential_conflicts(visible)
                      if parse_training_date(item['when']) and
                      parse_training_date(item['when']).year == year and
                      parse_training_date(item['when']).month == number],
        'areas': sorted({a for row in all_rows for a in _areas(row)}, key=str.casefold),
        'query': query,
        'area': area,
        'status': status,
    }
