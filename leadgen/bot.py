"""Telegram worker: polls the bot API for the multi-user ProductController and runs background delivery (PostgreSQL only)."""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from .adapters import collect, error_label
from .app import telegram, ingest, env_load, record_health
from .heartbeat import beat
from .observability import capture_exception

def reader_ready():
    """The group reader can start: API parameters and a session exist (re-reads .env, setup can finish at runtime)."""
    env_load()
    from .telegram_monitor import reader_configured
    return reader_configured()

def collect_batch(config):
    result=[]
    for source in config['sources']:
        if source['enabled']:
            try:
                result.append((source['name'],collect(source),None))
            except Exception as exc:
                capture_exception(exc,component='parser',source=source.get('id',''))
                result.append((source['name'],[],error_label(exc)))
    return result

DB_RETRY_MAX_SECONDS=30
# Outbox queues (reminders, webhooks, assignment notices) and first-run batches are polled on these intervals, not on
# every ~1 s getUpdates iteration: each poll is several queries even when the queues are empty.
QUEUE_SECONDS=15
QUEUE_BATCH=5
WEBHOOK_BATCH=2
FIRST_RUN_CHECK_SECONDS=30


class Cadence:
    """due() is True on the first call and then at most once per `seconds`; wake() makes the next call due."""
    def __init__(self,seconds,clock=time.monotonic):
        self.seconds,self.clock,self.next_at=seconds,clock,None

    def due(self):
        now=self.clock()
        if self.next_at is not None and now<self.next_at:return False
        self.next_at=now+self.seconds
        return True

    def wake(self):
        self.next_at=None


def deliver_queues(db,send,limit=QUEUE_BATCH):
    """One poll of the durable outboxes: due reminders, webhook events, assignment notifications.

    True when a queue delivered a full batch: more may be waiting, so the caller polls again on the next iteration
    instead of after QUEUE_SECONDS."""
    from .reminders import deliver_due
    full=deliver_due(db,send,limit=limit)>=limit
    from .integrations import integrations_enabled
    if integrations_enabled() and os.getenv('TELEGRAM_SESSION_ENCRYPTION_KEY'):
        from .integrations import deliver_webhooks
        # Few per poll: an external endpoint can be slow, and the bot loop waits for it.
        full=deliver_webhooks(db,os.environ['TELEGRAM_SESSION_ENCRYPTION_KEY'],limit=WEBHOOK_BATCH)>=WEBHOOK_BATCH or full
    from .team_notifications import deliver_assignments
    return deliver_assignments(db,send,limit=limit)>=limit or full


def db_retry_delay(failures):
    """Back-off after consecutive database errors in the worker loop: 2, 4, 8 ... up to 30 s."""
    return min(DB_RETRY_MAX_SECONDS,2**max(1,failures))


def releasing(db,send):
    """Telegram sender that first ends the implicit read transaction (Database.release).

    Every network call of the loop goes through it, so the worker is never idle in transaction while waiting
    for Telegram; inside `with db:` blocks it changes nothing."""
    def sender(method,payload):
        db.release()
        return send(method,payload)
    return sender


def run_bot(db,config):
    if db.backend!='postgres':
        raise ValueError('Бот работает только с PostgreSQL: задайте DATABASE_URL')
    from .database import transient_errors
    from .filtered_cleanup import FilteredCleanup
    from .product import DeliveryGate, ProductController
    send=releasing(db,telegram)
    ui=ProductController(db,config,send)
    gate=DeliveryGate()
    cleanup=FilteredCleanup()
    queues=Cadence(QUEUE_SECONDS)
    webhook=send('getWebhookInfo',{})
    if webhook.get('url'):
        raise ValueError('У бота настроен webhook. Используйте отдельного бота без webhook для локального запуска.')
    print('Бот работает. Откройте Telegram и отправьте /start. Ctrl+C — выход.',flush=True)
    db_target=db.target
    from .telegram_monitor import ReaderThread
    def read_groups():
        from .telegram_monitor import run_monitor
        run_monitor(db_target,config)
    # One supervised reader thread; if it ever dies, the polling loop below starts it again.
    reader=ReaderThread(read_groups)
    def start_reader_if_ready():
        return reader_ready() and reader.ensure()=='started'
    if not start_reader_if_ready():
        print('Чтение групп не запущено: выполните setup-telegram после добавления API-параметров.',flush=True)
    personal=None
    if all(os.getenv(key) for key in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_SESSION_ENCRYPTION_KEY')):
        def personal_monitors():
            from .telegram_accounts import run_personal_monitors
            run_personal_monitors(db_target, config)
        # Supervised like the shared reader: if the manager thread dies, the polling loop below restarts it.
        personal=ReaderThread(personal_monitors,name='personal-monitors',label='Менеджер личных Telegram-подключений',
                              component='personal_monitors')
        personal.ensure()
        print('Менеджер личных Telegram-подключений запущен.', flush=True)
    else:
        print('Личные Telegram-подключения отключены: добавьте API-параметры и ключ шифрования.', flush=True)
    from .ai_scoring import scoring_enabled
    if scoring_enabled():
        # Off the Telegram polling loop: a slow LLM call never delays updates or delivery.
        from .ai_scoring import run_scorer
        threading.Thread(target=run_scorer, args=(db_target,), daemon=True).start()
    else:
        print('AI-оценка лидов отключена: задайте GROQ_API_KEY.', flush=True)
    future=None;next_scan=0;telegram_retry_delay=2;next_subscription_check=0;next_first_run_check=0;db_failures=0
    with ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            try:
                # The setup wizard can finish while the bot is already running.
                if start_reader_if_ready():print('Сессия найдена, чтение групп подключается.',flush=True)
                if personal:personal.ensure()
                try:
                    updates=send('getUpdates',{'offset':ui.get('offset',0),'timeout':1,'allowed_updates':['message','callback_query']})
                    telegram_retry_delay=2
                except RuntimeError as exc:
                    # Short network interruptions must not restart the container and
                    # reconnect the MTProto reader while Telegram is rate-limiting it.
                    if not str(exc).startswith('Telegram: '):
                        raise
                    print(f'{exc}; повтор через {telegram_retry_delay} с.',flush=True)
                    beat(db_target)  # the loop itself is alive; Telegram is unreachable
                    time.sleep(telegram_retry_delay)
                    telegram_retry_delay=min(60,telegram_retry_delay*2)
                    continue
                for update in updates:
                    # Persist before side effects: an uncertain reply is not replayed on restart.
                    ui.put('offset',update['update_id']+1)
                    ui.handle(update)
                if future and future.done():
                    batch=future.result();future=None
                    with db:
                        for source,leads,error in batch:
                            for lead in leads:ingest(db,lead,ui.config())
                            record_health(db,source,len(leads),error)
                    if ui.manual or ui.get('active',False):
                        ui.drain=True
                    ui.manual=False
                    next_scan=time.monotonic()+config['poll_seconds']
                if (ui.manual or (ui.get('active',False) and time.monotonic()>=next_scan)) and future is None:
                    future=pool.submit(collect_batch,ui.config())
                if updates:queues.wake()  # a bot action may have queued something: check it right away
                if queues.due() and deliver_queues(db,send):
                    queues.wake()  # a full batch went out: drain the rest on the next iteration
                if time.monotonic()>=next_subscription_check:
                    next_subscription_check=time.monotonic()+300
                    try:
                        from .product import notify_subscriptions
                        notify_subscriptions(db,send)
                    except Exception as exc:
                        # Trial reminders are optional; only RuntimeError is survivable in this loop.
                        capture_exception(exc,component='subscription_reminders')
                        print('Напоминания о подписке: '+type(exc).__name__,flush=True)
                        db.recover()
                    try:
                        from .feedback import send_surveys
                        send_surveys(db,send)
                    except Exception as exc:
                        # Beta surveys are optional and must never stop lead delivery.
                        capture_exception(exc,component='beta_surveys')
                        print('Опрос беты: '+type(exc).__name__,flush=True)
                        db.recover()
                if time.monotonic()>=next_first_run_check:
                    # Before the live stream: monitoring enabled on the site gets the welcome batch too.
                    next_first_run_check=time.monotonic()+FIRST_RUN_CHECK_SECONDS
                    try:
                        from .product import backfill_pending
                        backfill_pending(db,config,send)
                    except RuntimeError:raise
                    except Exception as exc:
                        capture_exception(exc,component='first_leads')
                        print('Первые лиды: '+type(exc).__name__,flush=True)
                        db.recover()
                    try:
                        from .feedback import deliver_notifications
                        deliver_notifications(db,send)
                    except Exception as exc:
                        # Feedback is already saved; only the owner notification is skipped.
                        capture_exception(exc,component='feedback_notifications')
                        print('Уведомление об обратной связи: '+type(exc).__name__,flush=True)
                        db.recover()
                try:
                    # Hourly, not per iteration: old 'filtered' markers, bounded batches, cursors untouched.
                    cleanup.run_if_due(db,config)
                except RuntimeError:raise
                except Exception as exc:
                    # Housekeeping only; the next interval retries, delivery goes on.
                    capture_exception(exc,component='filtered_cleanup')
                    print('Очистка filtered-строк: '+type(exc).__name__,flush=True)
                    db.recover()
                if ui.get('active',False) or ui.drain:
                    # Cadence: only when new leads arrived, a backlog drains, a scan finished or the idle interval passed.
                    if gate.due(db,force=ui.drain):
                        from .product import deliver_registered
                        gate.ran(deliver_registered(db,config,send,limit_per_user=1))
                    ui.drain=False
                db.release()
                db_failures=0
                # Only after a full pass: a loop failing every iteration (DB errors, crashes) goes stale and unhealthy.
                beat(db_target)  # throttled, own autocommit connection, never raises
            except RuntimeError as exc:
                # Telegram network/API errors are expected here; report only unexpected ones.
                if not str(exc).startswith('Telegram'):capture_exception(exc,component='bot_loop')
                print(str(exc),flush=True)
                try:db.release()
                except transient_errors():pass  # the next iteration reports and recovers
                time.sleep(3)
            except transient_errors() as exc:
                # statement_timeout (QueryCanceled), a dropped or closed connection, deadlocks: roll back or
                # reconnect and continue instead of exiting and making Docker restart the whole worker.
                # Programming errors (ProgrammingError, IntegrityError...) still stop the loop.
                db_failures+=1;delay=db_retry_delay(db_failures)
                capture_exception(exc,component='bot_loop')
                print(f'База данных: {type(exc).__name__}: {str(exc).strip()[:300]}; повтор через {delay} с.',flush=True)
                try:db.recover()
                except (RuntimeError,*transient_errors()) as again:
                    print('База данных недоступна: '+type(again).__name__,flush=True)
                time.sleep(delay)
