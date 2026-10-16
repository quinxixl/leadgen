"""Mini-CRM on top of user_leads: the pipeline, legacy status aliases and the results widget.

Timestamps (replied_at, won_at, status_changed_at) are maintained by a database trigger
(migration 20261010120000_mini_crm_pipeline.sql), so every writer gets them for free."""

# Order matters: it is the order of pipeline columns and status buttons.
PIPELINE = {'new': 'Новый', 'replied': 'Написали', 'in_progress': 'В работе',
            'won': 'Сделка', 'lost': 'Не сложилось', 'archived': 'Архив'}
# Statuses before the mini-CRM. Old bot buttons, bookmarks and API clients may still send them.
LEGACY = {'saved': 'new', 'viewed': 'new', 'contacted': 'replied', 'working': 'in_progress',
          'discussing': 'in_progress', 'meeting': 'in_progress', 'proposal': 'in_progress',
          'not_fit': 'archived'}
ACTIVE = ('new', 'replied', 'in_progress')
REMIND_DAYS = (1, 3)
NOTE_LIMIT = 1000


def normalize(status):
    """Canonical status for a current or legacy value; None for anything else."""
    status = LEGACY.get(status, status)
    return status if status in PIPELINE else None


def money(value):
    return f'{int(value or 0):,}'.replace(',', ' ')


def results(db, owner_id):
    """Earned money (30 days / all time), won deals and reply→win conversion for one owner."""
    row = db.execute('''SELECT
        coalesce(sum(deal_amount) FILTER (WHERE pipeline_status='won' AND won_at>=now()-interval '30 days'),0) AS month,
        coalesce(sum(deal_amount) FILTER (WHERE pipeline_status='won'),0) AS total,
        count(*) FILTER (WHERE pipeline_status='won' AND won_at>=now()-interval '30 days') AS won_month,
        count(*) FILTER (WHERE pipeline_status='won') AS won,
        count(*) FILTER (WHERE replied_at IS NOT NULL) AS replied
        FROM user_leads WHERE user_id=? AND delivery_status<>'filtered' ''', (owner_id,)).fetchone()
    data = {key: int(row[key] or 0) for key in ('month', 'total', 'won_month', 'won', 'replied')}
    data['conversion'] = round(data['won'] * 100 / data['replied'], 1) if data['replied'] else None
    return data


def results_text(data):
    conversion = f"{data['conversion']}%" if data['conversion'] is not None else 'пока нет откликов'
    return ('💰 Результаты\n\n'
            f"Через Сигналид вы заработали ₽{money(data['month'])} за месяц / ₽{money(data['total'])} всего.\n"
            f"Сделок: {data['won']} (за месяц: {data['won_month']}).\n"
            f"Конверсия отклик → сделка: {conversion}.\n\n"
            'Отмечайте статус под карточкой лида кнопкой «📋 Статус», при сделке укажите сумму.')


def save_note(db, owner_id, lead_id, note):
    """Replace the lead's CRM note and log it; the caller has checked ownership and the role."""
    note = note.strip()[:NOTE_LIMIT]
    with db:
        db.execute('UPDATE user_leads SET crm_note=?,updated_at=now() WHERE user_id=? AND lead_id=?',
                   (note, owner_id, lead_id))
        db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'note',?)",
                   (owner_id, lead_id, 'Заметка по сделке: ' + note if note else 'Заметка по сделке удалена'))


def pipeline_board(db, owner_id, statuses, per_column=50):
    """Leads grouped by status with the next pending reminder; newest status change first."""
    from .core import load_lead
    rows = db.execute('''SELECT * FROM (SELECT l.id,l.payload,ul.pipeline_status,ul.deal_amount,ul.crm_note,
            ul.status_changed_at,r.due_at AS remind_at,
            row_number() OVER (PARTITION BY ul.pipeline_status
                ORDER BY ul.status_changed_at DESC NULLS LAST,l.id DESC) AS position
        FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
        LEFT JOIN lead_reminders r ON r.user_id=ul.user_id AND r.lead_id=ul.lead_id AND r.status='pending'
        WHERE ul.user_id=? AND ul.delivery_status<>'filtered' AND ul.pipeline_status IN (''' +
        ','.join('?' for _ in statuses) + ')) t WHERE position<=?',
        [owner_id, *statuses, per_column]).fetchall()
    counts = {r['pipeline_status']: r['n'] for r in db.execute(
        "SELECT pipeline_status,count(*) AS n FROM user_leads WHERE user_id=? AND delivery_status<>'filtered' GROUP BY pipeline_status",
        (owner_id,)).fetchall()}
    board = {status: [] for status in statuses}
    for row in rows:
        board[row['pipeline_status']].append(dict(row) | {'lead': load_lead(row['payload'])})
    return board, counts


def register_crm_routes(app, db):
    from flask import abort, g, render_template, request

    @app.get('/app/pipeline')
    def pipeline():
        """Pipeline board: one column per status; archive only on request. Readable by every role."""
        status = request.args.get('status', '')
        if status and not normalize(status):
            abort(400)
        status = normalize(status) if status else ''
        statuses = (status,) if status else tuple(key for key in PIPELINE if key != 'archived')
        board, counts = pipeline_board(db(), g.owner_id, statuses)
        return render_template('pipeline.html', board=board, counts=counts, status=status,
                               results=results(db(), g.owner_id))
