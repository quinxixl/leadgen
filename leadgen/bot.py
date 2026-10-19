"""Telegram worker: polls the bot API for the multi-user ProductController and runs background delivery (PostgreSQL only).

Two loops: run_bot long-polls getUpdates and handles updates; DeliveryWorker (own thread and connection) runs scans
and every outbound queue, so a slow webhook or delivery pass never delays a reply."""
import copy
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
# getUpdates long-poll timeout. The HTTP read timeout is longer (app.bot_api_timeout); nothing else runs on this loop.
LONG_POLL_SECONDS=25
WORKER_TICK_SECONDS=1  # DeliveryWorker pass interval, as the old ~1 s getUpdates iteration; wake() skips the wait
# Outbox queues (reminders, webhooks, assignment notices) and first-run batches are polled on these intervals, not on
# every ~1 s worker pass: each poll is several queries even when the queues are empty.
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
        # Few per poll: an external endpoint can be slow, and the delivery worker waits for it (replies do not).
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


class FirstLeads:
    """One-time welcome batches requested by bot handlers and sent by the delivery worker.

    A handler reserve()s the user before switching monitoring on and release()s after its confirmation messages:
    while reserved, the worker's live delivery skips the user, so the welcome batch always goes first (backfill_first_leads
    skips users who already received a card). Thread-safe: the update loop writes, the worker reads."""
    def __init__(self):
        self._lock=threading.Lock();self._held={};self._ready=[]

    def reserve(self,user_id):
        with self._lock:self._held[user_id]=self._held.get(user_id,0)+1

    def release(self,user_id):
        with self._lock:self._ready.append(user_id)

    def next(self):
        """The next released user id, or None."""
        with self._lock:return self._ready.pop(0) if self._ready else None

    def done(self,user_id):
        with self._lock:
            left=self._held.get(user_id,0)-1
            if left>0:self._held[user_id]=left
            else:self._held.pop(user_id,None)

    def held(self):
        with self._lock:return frozenset(self._held)


class DeliveryWorker:
    """Outbound work of the bot process on its own thread and its own database connection.

    Source scans, outboxes (reminders, webhooks, assignment notices), lead delivery, first-run batches, subscription
    reminders, surveys, feedback notices and cleanup used to run inside the getUpdates loop, so a slow webhook or a
    long delivery pass delayed every user's reply. The update loop now only handles updates and pokes this worker:
    wake() (an update may have queued something), request_scan() (monitoring switched on in the bot).
    Error handling is the old loop's: Telegram/RuntimeError and transient database errors are retried, anything
    else is stored in `fatal` and re-raised by the update loop, which stops the process as before."""
    def __init__(self,open_db,config,telegram_call,first_leads,tick=WORKER_TICK_SECONDS):
        self.open_db,self.config,self.telegram_call,self.first_leads,self.tick=open_db,config,telegram_call,first_leads,tick
        self.db=None;self.send=None;self.fatal=None;self.drain=False;self.manual=False
        self.future=None;self.pool=None;self.next_scan=0;self.next_subscription_check=0;self.next_first_run_check=0
        self._active=None
        self._poked=threading.Event();self._scan=threading.Event();self._wakeup=threading.Event();self._stop=threading.Event()
        from .filtered_cleanup import FilteredCleanup
        from .product import DeliveryGate
        self.gate=DeliveryGate();self.cleanup=FilteredCleanup();self.queues=Cadence(QUEUE_SECONDS)

    # --- called from the update loop --------------------------------------------------------------

    def wake(self):
        self._poked.set();self._wakeup.set()

    def request_scan(self):
        self._scan.set();self._wakeup.set()

    def stop(self):
        self._stop.set();self._wakeup.set()

    # --- worker thread --------------------------------------------------------------------------------

    def _connect(self):
        if self.pool is None:self.pool=ThreadPoolExecutor(max_workers=1)  # source scans
        if self.db is None:
            self.db=self.open_db();self.send=releasing(self.db,self.telegram_call)

    def active(self):
        """Any user with monitoring on; cached like ProductController.get('active'), reset by wake()."""
        from .product import ACTIVE_CACHE_SECONDS
        now=time.monotonic()
        if self._active and self._active[0]>now:return self._active[1]
        row=self.db.execute('SELECT 1 FROM user_preferences WHERE monitoring_active=true LIMIT 1').fetchone()
        self._active=(now+ACTIVE_CACHE_SECONDS,bool(row))
        return bool(row)

    def run(self):
        """Loop until stop(); see the class docstring for error handling."""
        from .database import transient_errors
        failures=0
        try:
            while not self._stop.is_set():
                try:
                    self._connect()
                    self.run_once()
                    self.db.release()
                    failures=0
                except RuntimeError as exc:
                    if not str(exc).startswith('Telegram'):capture_exception(exc,component='delivery_worker')
                    print(str(exc),flush=True)
                    try:
                        if self.db:self.db.release()
                    except transient_errors():pass
                    time.sleep(3)
                except transient_errors() as exc:
                    failures+=1;delay=db_retry_delay(failures)
                    capture_exception(exc,component='delivery_worker')
                    print(f'База данных: {type(exc).__name__}: {str(exc).strip()[:300]}; повтор через {delay} с.',flush=True)
                    try:self.db.recover()
                    except (RuntimeError,*transient_errors()) as again:
                        print('База данных недоступна: '+type(again).__name__,flush=True)
                    time.sleep(delay)
                self._wakeup.wait(self.tick);self._wakeup.clear()
        except BaseException as exc:
            self.fatal=exc
            raise
        finally:
            if self.pool is not None:
                self.pool.shutdown(wait=False,cancel_futures=True);self.pool=None;self.future=None
            if self.db is not None:
                try:self.db.close()
                except Exception:pass
                self.db=None

    def run_once(self):
        """One pass, in the order the update loop used to run it."""
        db,send=self.db,self.send
        if self._poked.is_set():
            self._poked.clear();self.queues.wake();self._active=None  # a bot action may have queued something
        if self._scan.is_set():
            self._scan.clear();self.manual=True
        self._scan_sources()
        self._welcome()
        if self.queues.due() and deliver_queues(db,send):
            self.queues.wake()  # a full batch went out: drain the rest on the next pass
        self._periodic()
        try:
            # Hourly, not per pass: old 'filtered' markers, bounded batches, cursors untouched.
            self.cleanup.run_if_due(db,self.config)
        except RuntimeError:raise
        except Exception as exc:
            capture_exception(exc,component='filtered_cleanup')
            print('Очистка filtered-строк: '+type(exc).__name__,flush=True)
            db.recover()
        if self.active() or self.drain:
            # Cadence: only when new leads arrived, a backlog drains, a scan finished or the idle interval passed.
            if self.gate.due(db,force=self.drain):
                self._welcome()  # released while this pass was waiting: before the live stream
                from .product import deliver_registered
                self.gate.ran(deliver_registered(db,self.config,send,limit_per_user=1,skip=self.first_leads.held()))
            self.drain=False

    def _scan_sources(self):
        if self.future and self.future.done():
            batch=self.future.result();self.future=None
            config=copy.deepcopy(self.config)
            with self.db:
                for source,leads,error in batch:
                    for lead in leads:ingest(self.db,lead,config)
                    record_health(self.db,source,len(leads),error)
            if self.manual or self.active():self.drain=True
            self.manual=False
            self.next_scan=time.monotonic()+self.config['poll_seconds']
        if (self.manual or (self.active() and time.monotonic()>=self.next_scan)) and self.future is None:
            self.future=self.pool.submit(collect_batch,copy.deepcopy(self.config))

    def _welcome(self):
        """Welcome batches released by handlers, one user at a time; each reservation ends even on errors."""
        from .product import backfill_first_leads
        while (user_id:=self.first_leads.next()) is not None:
            try:backfill_first_leads(self.db,self.config,self.send,user_id)
            except RuntimeError:raise
            except Exception as exc:
                # The welcome batch is optional; backfill_pending retries users whose marker was not claimed.
                capture_exception(exc,user_id=user_id,component='first_leads')
                print('Первые лиды: '+type(exc).__name__,flush=True)
                self.db.recover()
            finally:self.first_leads.done(user_id)

    def _periodic(self):
        db,send=self.db,self.send
        if time.monotonic()>=self.next_subscription_check:
            self.next_subscription_check=time.monotonic()+300
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
        if time.monotonic()>=self.next_first_run_check:
            # Before the live stream: monitoring enabled on the site gets the welcome batch too.
            self.next_first_run_check=time.monotonic()+FIRST_RUN_CHECK_SECONDS
            try:
                from .product import backfill_pending
                backfill_pending(db,self.config,send)
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


def run_bot(db,config):
    if db.backend!='postgres':
        raise ValueError('Бот работает только с PostgreSQL: задайте DATABASE_URL')
    from .database import db_open, transient_errors
    from .product import ProductController
    send=releasing(db,telegram)
    first_leads=FirstLeads()
    ui=ProductController(db,config,send,first_leads=first_leads)
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
    # Scans and every outbound queue run here, on a second connection: replies never wait for them.
    worker=DeliveryWorker(lambda:db_open(db_target),config,telegram,first_leads)
    delivery=ReaderThread(worker.run,name='delivery-worker',label='Поток доставки лидов',component='delivery_worker')
    delivery.ensure()
    telegram_retry_delay=2;db_failures=0
    try:
        while True:
            try:
                # The setup wizard can finish while the bot is already running.
                if start_reader_if_ready():print('Сессия найдена, чтение групп подключается.',flush=True)
                if personal:personal.ensure()
                if worker.fatal is not None:raise worker.fatal  # programming errors still stop the process
                delivery.ensure()
                try:
                    # Long poll: Telegram answers as soon as an update arrives; nothing else waits on this loop.
                    updates=send('getUpdates',{'offset':ui.get('offset',0),'timeout':LONG_POLL_SECONDS,
                                               'allowed_updates':['message','callback_query']})
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
                if ui.manual:
                    worker.request_scan();ui.manual=False
                if updates:worker.wake()
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
    finally:
        worker.stop()
