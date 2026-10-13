"""Telegram UI. PostgreSQL uses the multi-user product controller; SQLite keeps legacy tests."""
import copy
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from .adapters import collect, error_label
from .app import telegram, ingest, deliver, send_lead, classify_for_config, env_load, notification_chat_ids
from .core import TOPICS, Lead, UTC, classify
from .heartbeat import beat
from .observability import capture_exception

MENU = {'keyboard':[[{'text':'▶️ Начать'},{'text':'⏸ Остановить'}],
    [{'text':'🔎 Найти сейчас'},{'text':'📋 Объявления'}],
    [{'text':'🧩 Услуги'},{'text':'💰 Бюджет'}],
    [{'text':'⚙️ Источники'},{'text':'📊 Статус'}]],'resize_keyboard':True,'is_persistent':True}

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

class Controller:
    def __init__(self,db,base,sender=telegram):
        self.db,self.base,self.sender=db,base,sender
        self.owner=str(os.environ['TELEGRAM_CHAT_ID'])
        if not self.owner.isdigit():
            raise ValueError('Для управления нужен ID личного чата; повторите setup')
        self.manual=False
        self.drain=False

    def get(self,key,default=None):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else default

    def put(self,key,value):
        with self.db:
            self.db.execute('''INSERT INTO settings(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value''',(key,json.dumps(value)))

    def config(self):
        result=copy.deepcopy(self.base)
        result['min_budget']=self.get('budget',result['min_budget'])
        result['topics']=self.get('topics',list(TOPICS))
        switches=self.get('sources',{})
        for source in result['sources']:
            source['enabled']=switches.get(source['id'],source['enabled'])
        return result

    def say(self,text,markup=None):
        return self.sender('sendMessage',{'chat_id':self.owner,'text':text,'reply_markup':markup or MENU})

    def refresh(self):
        config=self.config()
        rows=self.db.execute("SELECT payload FROM leads WHERE status NOT IN ('sent','sending','uncertain')").fetchall()
        with self.db:
            # Rebuild eligible queue when filters change, preserving sent/uncertain entries.
            self.db.execute("UPDATE leads SET status='review' WHERE status NOT IN ('sent','sending','uncertain')")
            for row in rows:ingest(self.db,Lead(**json.loads(row['payload'])),config)

    def services(self):
        chosen=self.config()['topics']
        buttons=[{'text':('✅ ' if name in chosen else '⬜️ ')+name,'callback_data':'topic:'+str(i)} for i,name in enumerate(TOPICS)]
        keys=[buttons[i:i+2] for i in range(0,len(buttons),2)]
        self.say('Выберите услуги. Можно включить несколько:',{'inline_keyboard':keys})

    def sources(self):
        keys=[]
        for source in self.config()['sources']:
            if source['kind']=='pending':continue
            keys.append([{'text':('✅ ' if source['enabled'] else '⬜️ ')+source['name'],'callback_data':'source:'+source['id']}])
        from .source_registry import read_count
        registry={row['status']:row['total'] for row in self.db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
        note=(f"\n\nTelegram: служебный аккаунт читает {read_count(self.db)} чатов в реальном времени; "
              f"в базе {sum(registry.values())} источников, ожидают проверки {registry.get('pending',0)+registry.get('retry',0)}.")
        self.say('Источники. hh показывает проектные вакансии только на проверке. Авито и Kwork пока недоступны.'+note,{'inline_keyboard':keys})

    def listing(self,review=False,page=0):
        config=self.config()
        candidates=[]
        identity='id' if self.db.backend=='postgres' else 'rowid AS id'
        for row in self.db.execute(f'SELECT {identity},payload,reason FROM leads ORDER BY first_seen DESC,id DESC'):
            lead=Lead(**json.loads(row['payload']))
            state,reason=classify_for_config(lead,config)
            if state==('review' if review else 'ready'):
                candidates.append((row,lead,reason))
        selected=candidates[page*5:page*5+5]
        lines=['На проверке' if review else 'Подходящие объявления']
        keys=[]
        for i,(row,lead,reason) in enumerate(selected,1):
            lines.append(f'{i}. {lead.title[:150]}\n{lead.source} · {reason}')
            keys.append([{'text':f'Показать {i}','callback_data':f"show:{row['id']}"}])
        if not selected:lines.append('Пока нет объявлений по текущим фильтрам.')
        mode='review' if review else 'leads'
        nav=[]
        if page:nav.append({'text':'← Назад','callback_data':f'{mode}:{page-1}'})
        if len(candidates)>(page+1)*5:nav.append({'text':'Далее →','callback_data':f'{mode}:{page+1}'})
        if nav:keys.append(nav)
        keys.append([{'text':'На проверке' if not review else 'Подходящие','callback_data':'review:0' if not review else 'leads:0'}])
        self.say('\n\n'.join(lines),{'inline_keyboard':keys})

    def handle(self,update):
        callback=update.get('callback_query')
        msg=(callback or {}).get('message') if callback else update.get('message')
        actor=(callback or msg or {}).get('from',{})
        chat=(msg or {}).get('chat',{})
        chat_id=str(chat.get('id'))
        if chat.get('type')!='private' or chat_id!=str(actor.get('id')):
            return
        if chat_id!=self.owner:
            if (not callback and chat_id in notification_chat_ids(self.db) and
                    msg.get('text','').strip() in ('/start','/menu')):
                self.sender('sendMessage',{'chat_id':chat_id,
                    'text':'Аккаунт подключён к уведомлениям о новых лидах. Управление настройками доступно владельцу бота.'})
            return
        if callback:
            self.sender('answerCallbackQuery',{'callback_query_id':callback['id']})
            action=callback.get('data','')
            prefix,_,value=action.partition(':')
            if prefix=='topic' and value.isdigit() and int(value)<len(TOPICS):
                name=list(TOPICS)[int(value)];chosen=self.config()['topics']
                if name in chosen:chosen.remove(name)
                else:chosen.append(name)
                self.put('topics',chosen);self.refresh();self.services()
            elif prefix=='source':
                source=next((s for s in self.config()['sources'] if s['id']==value and s['kind']!='pending'),None)
                if source:
                    if source['kind']=='hh' and not os.getenv('HH_USER_AGENT'):
                        self.say('Для hh сначала задайте HH_USER_AGENT в .env и перезапустите бота.');return
                    switches=self.get('sources',{});switches[value]=not source['enabled']
                    self.put('sources',switches);self.refresh();self.sources()
            elif prefix=='budget' and value.isdigit() and 0<=int(value)<=100000000:
                self.put('budget',int(value));self.put('await_budget',False);self.refresh()
                self.say(f'Минимальный бюджет: {int(value):,} ₽'.replace(',',' '))
            elif prefix=='budget' and value=='custom':
                self.put('await_budget',True);self.say('Напишите сумму в рублях, например 7500. /cancel — отменить.')
            elif prefix in ('leads','review') and value.isdigit() and int(value)<10000:
                self.listing(prefix=='review',int(value))
            elif prefix=='show' and value.isdigit():
                identity='id' if self.db.backend=='postgres' else 'rowid'
                row=self.db.execute(f'SELECT payload,reason FROM leads WHERE {identity}=?',(int(value),)).fetchone()
                if row:send_lead(Lead(**json.loads(row['payload'])),row['reason'],self.owner,self.sender)
            return
        text=msg.get('text','').strip()
        if text=='/cancel':
            self.put('await_budget',False);self.say('Ввод отменён.');return
        if self.get('await_budget',False) and re_amount(text) is not None:
            self.put('budget',re_amount(text));self.put('await_budget',False);self.refresh()
            self.say(f'Бюджет сохранён: {re_amount(text)} ₽');return
        if text in ('/start','/menu'):
            self.say('Поиск IT-заказов. Выберите услуги и бюджет, затем нажмите «Начать» или «Найти сейчас».')
        elif text=='▶️ Начать':
            self.put('active',True);self.manual=True;self.say('Мониторинг включён. Буду искать каждые 5 минут.')
        elif text=='⏸ Остановить':
            self.put('active',False);self.manual=False;self.drain=False;self.say('Мониторинг и новые уведомления остановлены. Меню продолжает работать.')
        elif text=='🔎 Найти сейчас':
            self.manual=True;self.say('Проверяю источники. Новые подходящие объявления пришлю сюда.')
        elif text=='📋 Объявления':self.listing()
        elif text=='🧩 Услуги':self.services()
        elif text=='⚙️ Источники':self.sources()
        elif text=='💰 Бюджет':
            self.say(f"Сейчас: от {self.config()['min_budget']} ₽. Выберите сумму:",{'inline_keyboard':[
                [{'text':f'{n} ₽','callback_data':f'budget:{n}'} for n in (5000,10000,25000)],
                [{'text':'Своя сумма','callback_data':'budget:custom'}]]})
        elif text=='📊 Статус':
            rows=self.db.execute('SELECT * FROM health').fetchall()
            lines=['Мониторинг: '+('включён' if self.get('active',False) else 'остановлен'),f"Бюджет: от {self.config()['min_budget']} ₽"]
            lines += [f"{'✅' if r['ok'] else '⚠️'} {r['source']}: {r['count']} записей · {r['checked']}" for r in rows]
            uncertain=self.db.execute("SELECT count(*) AS total FROM leads WHERE status='uncertain'").fetchone()['total']
            if uncertain:lines.append(f'Не подтверждена отправка {uncertain} объявлений; проверьте историю чата.')
            registry={row['status']:row['total'] for row in self.db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
            if registry:
                from .source_registry import read_count
                lines.append(f"Telegram: служебный аккаунт читает {read_count(self.db)} чатов; в базе активны {registry.get('active',0)}, "
                             f"неактивны {registry.get('inactive',0)}, не чаты {registry.get('not_chat',0)}, "
                             f"ждут проверки {registry.get('pending',0)+registry.get('retry',0)}")
                from .telegram_monitor import reader_configured, reader_status
                state=reader_status(self.db)['state']
                reader=('нужна однократная настройка Telegram-аккаунта' if not reader_configured() else
                        'подключено' if state=='running' else
                        'сессия не авторизована, выполните setup-telegram' if state=='unauthorized' else
                        'не работает ('+state+')')
                lines.append('Чтение групп и каналов: '+reader)
            self.say('\n'.join(lines))
        else:self.say('Выберите кнопку меню. Сумма должна быть целым числом от 0 до 100 000 000.' if self.get('await_budget') else 'Нажмите кнопку меню или /start.')

def re_amount(text):
    value=text.replace(' ','').replace('\u00a0','')
    return int(value) if value.isascii() and value.isdigit() and len(value)<=9 and int(value)<=100000000 else None

DB_RETRY_MAX_SECONDS=30


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
    product_mode=db.backend=='postgres'
    from .database import transient_errors
    send=releasing(db,telegram)
    if product_mode:
        from .product import DeliveryGate, ProductController
        ui=ProductController(db,config,send)
        gate=DeliveryGate()
    else:
        ui=Controller(db,config,send)
    webhook=send('getWebhookInfo',{})
    if webhook.get('url'):
        raise ValueError('У бота настроен webhook. Используйте отдельного бота без webhook для локального запуска.')
    print('Бот работает. Откройте Telegram и отправьте /start. Ctrl+C — выход.',flush=True)
    db_target=db.target
    from .telegram_monitor import ReaderThread, reader_configured
    def read_groups():
        from .telegram_monitor import run_monitor
        run_monitor(db_target,config)
    # One supervised reader thread; if it ever dies, the polling loop below starts it again.
    reader=ReaderThread(read_groups)
    def start_reader_if_ready():
        env_load()
        if not reader_configured():
            return False
        return reader.ensure()=='started'
    if not start_reader_if_ready():
        print('Чтение групп не запущено: выполните setup-telegram после добавления API-параметров.',flush=True)
    if product_mode and all(os.getenv(key) for key in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_SESSION_ENCRYPTION_KEY')):
        def personal_monitors():
            try:
                from .telegram_accounts import run_personal_monitors
                run_personal_monitors(db_target, config)
            except Exception as exc:
                capture_exception(exc,component='personal_monitors')
                print('Личные Telegram-подключения остановлены: '+type(exc).__name__, flush=True)
        threading.Thread(target=personal_monitors, daemon=True).start()
        print('Менеджер личных Telegram-подключений запущен.', flush=True)
    elif product_mode:
        print('Личные Telegram-подключения отключены: добавьте API-параметры и ключ шифрования.', flush=True)
    from .ai_scoring import scoring_enabled
    if product_mode and scoring_enabled():
        # Off the Telegram polling loop: a slow LLM call never delays updates or delivery.
        from .ai_scoring import run_scorer
        threading.Thread(target=run_scorer, args=(db_target,), daemon=True).start()
    elif product_mode:
        print('AI-оценка лидов отключена: задайте GROQ_API_KEY.', flush=True)
    future=None;next_scan=0;telegram_retry_delay=2;next_subscription_check=0;next_first_run_check=0;db_failures=0
    with ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            try:
                # The setup wizard can finish while the bot is already running.
                if start_reader_if_ready():print('Сессия найдена, чтение групп подключается.',flush=True)
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
                            db.execute('''INSERT INTO health(source,checked,ok,count,error) VALUES(?,?,?,?,?)
                                ON CONFLICT(source) DO UPDATE SET checked=excluded.checked,ok=excluded.ok,
                                count=excluded.count,error=excluded.error''',
                                (source,datetime.now(UTC).isoformat(),int(error is None),len(leads),error))
                    if ui.manual or ui.get('active',False):
                        ui.drain=True
                        failures=sum(bool(e) for _,_,e in batch)
                        # Product users get leads as cards; the owner-only summary refers to the legacy menu.
                        if ui.manual and not product_mode:
                            ui.say(f'Проверка завершена: прочитано {sum(len(x) for _,x,_ in batch)} записей. Ошибок источников: {failures}. Результаты — «Объявления».')
                    ui.manual=False
                    next_scan=time.monotonic()+config['poll_seconds']
                if (ui.manual or (ui.get('active',False) and time.monotonic()>=next_scan)) and future is None:
                    future=pool.submit(collect_batch,ui.config())
                if product_mode:
                    from .reminders import deliver_due
                    deliver_due(db,send)
                    from .integrations import integrations_enabled
                    if integrations_enabled() and os.getenv('TELEGRAM_SESSION_ENCRYPTION_KEY'):
                        from .integrations import deliver_webhooks
                        deliver_webhooks(db,os.environ['TELEGRAM_SESSION_ENCRYPTION_KEY'],limit=2)
                    from .team_notifications import deliver_assignments
                    deliver_assignments(db,send,limit=2)
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
                        next_first_run_check=time.monotonic()+10
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
                if ui.get('active',False) or ui.drain:
                    if product_mode:
                        # Cadence: only when new leads arrived, a backlog drains, a scan finished or the idle interval passed.
                        if gate.due(db,force=ui.drain):
                            from .product import deliver_registered
                            gate.ran(deliver_registered(db,config,send,limit_per_user=1))
                        ui.drain=False
                    else:
                        deliver(db,ui.config(),limit=1)
                        ui.drain=bool(db.execute("SELECT 1 FROM leads WHERE status='ready' AND next_attempt<=? LIMIT 1",(time.time(),)).fetchone())
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
