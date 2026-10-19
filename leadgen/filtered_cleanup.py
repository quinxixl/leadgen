"""Age-based cleanup of user_leads rows with delivery_status='filtered'.

A 'filtered' row only marks "this lead was evaluated for the user and rejected", so the delivery pass does not
evaluate it again. deliver_registered and backfill_first_leads never look at leads at or below the freshness
window floor (lead_id_floor(window_hours(base))): classify() rejects them as 'вне окна свежести'. Below that floor
the markers are dead weight (99.9% of the table on 2026-10-13), and nothing else reads filtered rows: every
cabinet, CRM, API and statistics query filters on delivery_status<>'filtered'.

The cursor trigger (user_leads_reset_delivery_cursor) resets delivery_cursor to 0 when a user's filtered rows are
deleted, because a filter change must re-evaluate them. The cleanup deletes only rows below the floor, which are
never re-evaluated anyway, so it opts out of that reset for its own transactions via the transaction-local
setting leadgen.keep_delivery_cursor (migration 20261014120000). Without it every hourly batch would send all
users back to the window floor and make the next pass re-classify about three days of leads for each of them.

The same hourly run also deletes old leads with status 'rejected' or 'duplicate' that nobody worked with
(cleanup_rejected): ~95% of ingested messages, otherwise kept forever with their full payload. The protections
match reviews/cleanup-2026-10-19.sql; see cleanup_rejected.
"""
import json
import os
import time
from datetime import datetime, timezone

from .product import lead_id_floor, window_hours

# Extra age beyond the freshness window: a lead must be this much older than classify() could ever accept.
RETENTION_MARGIN_HOURS=24
BATCH_SIZE=5000
MAX_BATCHES=20
INTERVAL_SECONDS=3600
KEEP_CURSOR_SETTING='leadgen.keep_delivery_cursor'


def _env_int(name,default,low,high):
    try:value=int(os.environ.get(name,'').strip() or default)
    except ValueError:return default
    return min(high,max(low,value))


def retention_hours(base):
    """Filtered markers of leads found more than this many hours ago are deleted."""
    return window_hours(base)+RETENTION_MARGIN_HOURS


def cleanup_filtered(db,base,batch_size=BATCH_SIZE,max_batches=MAX_BATCHES):
    """Delete old 'filtered' rows in bounded batches; returns the number of rows deleted.

    Each batch is its own short transaction (row locks and FK cascade checks stay small, the worker loop is never
    blocked for long). A run stops after max_batches; the rest goes in the next run."""
    if db.backend!='postgres':return 0
    floor=lead_id_floor(db,retention_hours(base))
    db.release()
    if not floor:return 0
    deleted=0
    for _ in range(max_batches):
        with db:
            db.execute('SELECT set_config(?,?,true)',(KEEP_CURSOR_SETTING,'on'))
            count=db.execute('''DELETE FROM user_leads WHERE (user_id,lead_id) IN (
                SELECT user_id,lead_id FROM user_leads WHERE delivery_status='filtered' AND lead_id<=? LIMIT ?)''',
                (floor,batch_size)).rowcount
        deleted+=max(count,0)
        if count<batch_size:break
    return deleted


REJECTED_RETENTION_DAYS=3
REJECTED_BATCH_SIZE=1000
REJECTED_MAX_BATCHES=30
LOCK_TIMEOUT='3s'
LOCK_ERRORS=('55P03','40P01')  # lock_not_available, deadlock_detected: the batch rolled back, retried next run
STATS_KEY='cleanup:last'

# Work a user did on a lead. Every child table of user_leads is ON DELETE CASCADE, so it is checked up front; a
# 'filtered' row counts as no work only when it carries no CRM data. lead_feedback and project_leads reference
# leads directly (also ON DELETE CASCADE). lead_ai_scores is derived data and goes with the lead.
NO_USER_WORK='''NOT EXISTS (SELECT 1 FROM lead_feedback fb WHERE fb.lead_id=l.id)
    AND NOT EXISTS (SELECT 1 FROM project_leads pl WHERE pl.lead_id=l.id)
    AND NOT EXISTS (SELECT 1 FROM user_leads ul WHERE ul.lead_id=l.id AND NOT (
        ul.delivery_status='filtered' AND ul.pipeline_status='new' AND ul.crm_note='' AND ul.deal_amount IS NULL
        AND ul.replied_at IS NULL AND ul.won_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM lead_activity x WHERE x.user_id=ul.user_id AND x.lead_id=ul.lead_id)
        AND NOT EXISTS (SELECT 1 FROM lead_reminders x WHERE x.user_id=ul.user_id AND x.lead_id=ul.lead_id)
        AND NOT EXISTS (SELECT 1 FROM lead_tags x WHERE x.user_id=ul.user_id AND x.lead_id=ul.lead_id)
        AND NOT EXISTS (SELECT 1 FROM lead_assignments x WHERE x.owner_user_id=ul.user_id AND x.lead_id=ul.lead_id)
        AND NOT EXISTS (SELECT 1 FROM telegram_replies x WHERE x.owner_id=ul.user_id AND x.lead_id=ul.lead_id)
        AND NOT EXISTS (SELECT 1 FROM ai_offer_generations x WHERE x.owner_user_id=ul.user_id AND x.lead_id=ul.lead_id)))'''
DEAD_LEAD="l.status IN ('rejected','duplicate') AND l.first_seen < now()-(? * interval '1 hour') AND "+NO_USER_WORK
REJECTED_CANDIDATES=('SELECT l.id FROM leads l WHERE l.id>? AND l.id<=? AND '+DEAD_LEAD+
                     ' ORDER BY l.id LIMIT ? FOR UPDATE OF l SKIP LOCKED')
REJECTED_DELETE='DELETE FROM leads l WHERE l.id = ANY(?) AND '+DEAD_LEAD


def rejected_retention_hours(base):
    """Age after which unworked rejected/duplicate leads are deleted; 0 when LEAD_REJECTED_RETENTION_DAYS=0.

    LEAD_REJECTED_RETENTION_DAYS (default 3), but never less than the freshness window plus margin
    (retention_hours, 98 h for max_age_hours=72): inside the window the delivery pass re-classifies globally
    rejected leads per user, and past it a re-scraped copy of a deleted lead is rejected as 'вне окна свежести'."""
    days=_env_int('LEAD_REJECTED_RETENTION_DAYS',REJECTED_RETENTION_DAYS,0,3650)
    return max(days*24,retention_hours(base)) if days else 0


def _lock_error(exc):
    return getattr(exc,'sqlstate',None) in LOCK_ERRORS


def cleanup_rejected(db,base,batch_size=REJECTED_BATCH_SIZE,max_batches=REJECTED_MAX_BATCHES):
    """Delete old rejected/duplicate leads without user work in bounded batches; returns the number deleted.

    Never deleted: leads with any other status (ready, review, sent, sending, uncertain), leads younger than
    rejected_retention_hours, and leads with user work (NO_USER_WORK). Each batch is one short transaction: it locks
    its candidate leads (SKIP LOCKED), which makes new user_leads / lead_feedback / project_leads rows for them wait
    for the commit and then fail; it locks their user_leads rows (new CRM children wait the same way) and the
    DELETE re-checks every condition. The cascade removes their 'filtered' rows and AI scores; the transaction-local
    leadgen.keep_delivery_cursor keeps delivery cursors, as these leads are below the freshness floor and never
    scanned again. Dedup is unaffected: find_duplicate matches live statuses only (app.LIVE_STATUSES)."""
    if db.backend!='postgres':return 0
    hours=rejected_retention_hours(base)
    floor=lead_id_floor(db,hours) if hours else 0
    db.release()
    if not floor:return 0
    deleted,after=0,0
    for _ in range(max_batches):
        try:
            with db:
                db.execute('SELECT set_config(?,?,true),set_config(?,?,true)',
                           (KEEP_CURSOR_SETTING,'on','lock_timeout',LOCK_TIMEOUT))
                ids=[row['id'] for row in db.execute(REJECTED_CANDIDATES,(after,floor,hours,batch_size)).fetchall()]
                if ids:
                    db.execute('SELECT 1 FROM user_leads WHERE lead_id = ANY(?) FOR UPDATE',(ids,)).fetchall()
                    deleted+=max(db.execute(REJECTED_DELETE,(ids,hours)).rowcount,0)
        except Exception as exc:
            if not _lock_error(exc):raise
            db.recover()
            print('Очистка отклонённых лидов: строки заняты, продолжим в следующий запуск.',flush=True)
            break
        if len(ids)<batch_size:break
        after=ids[-1]
    return deleted


def save_stats(db,stats):
    with db:
        db.execute('''INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''',(STATS_KEY,json.dumps(stats,ensure_ascii=False)))


def last_stats(db):
    """Counts of the last hourly run, for `python -m leadgen.app status`; {} before the first run."""
    row=db.execute('SELECT value FROM settings WHERE key=?',(STATS_KEY,)).fetchone()
    try:return json.loads(row['value']) if row else {}
    except (TypeError,ValueError):return {}


class FilteredCleanup:
    """Runs cleanup_filtered and cleanup_rejected from the worker loop at most once per FILTERED_CLEANUP_INTERVAL_SECONDS (default 1 h).

    The first call runs it; the next run is scheduled before the work starts, so a failing cleanup is retried on
    the next interval, not on every ~1 s loop iteration."""
    def __init__(self,interval_seconds=None,clock=time.monotonic):
        self.interval=_env_int('FILTERED_CLEANUP_INTERVAL_SECONDS',INTERVAL_SECONDS,60,86400) \
            if interval_seconds is None else interval_seconds
        self.clock=clock;self.next_run=None

    def due(self):
        now=self.clock()
        if self.next_run is not None and now<self.next_run:return False
        self.next_run=now+self.interval
        return True

    def run_if_due(self,db,base):
        """Returns {'filtered': rows, 'leads': rows} deleted by this run, or None when the timer has not fired."""
        if not self.due():return None
        started=self.clock()
        filtered=cleanup_filtered(db,base)
        leads=cleanup_rejected(db,base)
        seconds=round(self.clock()-started,1)
        print(f'Очистка: filtered-строк {filtered}, отклонённых лидов {leads}, {seconds} с.',flush=True)
        save_stats(db,{'at':datetime.now(timezone.utc).isoformat(),'filtered':filtered,'leads':leads,
                       'seconds':seconds,'rejected_retention_hours':rejected_retention_hours(base)})
        return {'filtered':filtered,'leads':leads}
