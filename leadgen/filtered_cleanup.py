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
"""
import os
import time

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


class FilteredCleanup:
    """Runs cleanup_filtered from the worker loop at most once per FILTERED_CLEANUP_INTERVAL_SECONDS (default 1 h).

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
        """Returns the number of deleted rows, or None when the timer has not fired."""
        if not self.due():return None
        deleted=cleanup_filtered(db,base)
        if deleted:print(f'Очистка filtered-строк: удалено {deleted}.',flush=True)
        return deleted
