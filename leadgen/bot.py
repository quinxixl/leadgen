"""Telegram UI. PostgreSQL uses the multi-user product controller; SQLite keeps legacy tests."""
import copy
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from .adapters import collect
from .app import ROOT, telegram, ingest, deliver, send_lead, classify_for_config, env_load, notification_chat_ids
from .core import TOPICS, Lead, UTC, classify

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
                result.append((source['name'],[],type(exc).__name__))
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
        keys=[[{'text':('✅ ' if name in chosen else '⬜️ ')+name,'callback_data':'topic:'+str(i)}] for i,name in enumerate(TOPICS)]
        self.say('Выберите услуги. Можно включить несколько:',{'inline_keyboard':keys})

    def sources(self):
        keys=[]
        for source in self.config()['sources']:
            if source['kind']=='pending':continue
            keys.append([{'text':('✅ ' if source['enabled'] else '⬜️ ')+source['name'],'callback_data':'source:'+source['id']}])
        registry={row['status']:row['total'] for row in self.db.execute('SELECT status,count(*) AS total FROM telegram_sources GROUP BY status')}
        note=(f"\n\nTelegram-база: {sum(registry.values())} источников; "
              f"активны {registry.get('active',0)}, ожидают проверки {registry.get('pending',0)+registry.get('retry',0)}.")
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
                lines.append(f"Telegram-база: активны {registry.get('active',0)}, неактивны {registry.get('inactive',0)}, "
                             f"не чаты {registry.get('not_chat',0)}, ждут проверки {registry.get('pending',0)+registry.get('retry',0)}")
                reader=all(os.getenv(k) for k in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_PHONE')) and (ROOT/'data'/'telegram_reader.session').exists()
                lines.append('Чтение групп: '+('подключено' if reader else 'нужна однократная настройка Telegram-аккаунта'))
            self.say('\n'.join(lines))
        else:self.say('Выберите кнопку меню. Сумма должна быть целым числом от 0 до 100 000 000.' if self.get('await_budget') else 'Нажмите кнопку меню или /start.')

def re_amount(text):
    value=text.replace(' ','').replace('\u00a0','')
    return int(value) if value.isascii() and value.isdigit() and len(value)<=9 and int(value)<=100000000 else None

def run_bot(db,config):
    product_mode=db.backend=='postgres'
    if product_mode:
        from .product import ProductController
        ui=ProductController(db,config,telegram)
    else:
        ui=Controller(db,config)
    webhook=telegram('getWebhookInfo',{})
    if webhook.get('url'):
        raise ValueError('У бота настроен webhook. Используйте отдельного бота без webhook для локального запуска.')
    print('Бот работает. Откройте Telegram и отправьте /start. Ctrl+C — выход.',flush=True)
    db_target=db.target;monitor_started=False
    def start_reader_if_ready():
        nonlocal monitor_started
        env_load()
        if monitor_started or not (all(os.getenv(k) for k in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_PHONE')) and (ROOT/'data'/'telegram_reader.session').exists()):
            return False
        def monitor():
            try:
                from .telegram_monitor import run_monitor
                run_monitor(db_target,config)
            except Exception as exc:
                print('Монитор Telegram остановлен: '+type(exc).__name__,flush=True)
        threading.Thread(target=monitor,daemon=True).start()
        monitor_started=True
        return True
    if not start_reader_if_ready():
        print('Чтение групп не запущено: выполните setup-telegram после добавления API-параметров.',flush=True)
    if product_mode and all(os.getenv(key) for key in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_SESSION_ENCRYPTION_KEY')):
        def personal_monitors():
            try:
                from .telegram_accounts import run_personal_monitors
                run_personal_monitors(db_target, config)
            except Exception as exc:
                print('Личные Telegram-подключения остановлены: '+type(exc).__name__, flush=True)
        threading.Thread(target=personal_monitors, daemon=True).start()
        print('Менеджер личных Telegram-подключений запущен.', flush=True)
    elif product_mode:
        print('Личные Telegram-подключения отключены: добавьте API-параметры и ключ шифрования.', flush=True)
    future=None;next_scan=0;telegram_retry_delay=2
    with ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            try:
                # The setup wizard can finish while the bot is already running.
                if start_reader_if_ready():print('Сессия найдена, чтение групп подключается.',flush=True)
                try:
                    updates=telegram('getUpdates',{'offset':ui.get('offset',0),'timeout':1,'allowed_updates':['message','callback_query']})
                    telegram_retry_delay=2
                except RuntimeError as exc:
                    # Short network interruptions must not restart the container and
                    # reconnect the MTProto reader while Telegram is rate-limiting it.
                    if not str(exc).startswith('Telegram: '):
                        raise
                    print(f'{exc}; повтор через {telegram_retry_delay} с.',flush=True)
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
                        if ui.manual:
                            ui.say(f'Проверка завершена: прочитано {sum(len(x) for _,x,_ in batch)} записей. Ошибок источников: {failures}. Результаты — «Объявления».')
                    ui.manual=False
                    next_scan=time.monotonic()+config['poll_seconds']
                if (ui.manual or (ui.get('active',False) and time.monotonic()>=next_scan)) and future is None:
                    future=pool.submit(collect_batch,ui.config())
                if product_mode:
                    from .reminders import deliver_due
                    deliver_due(db,telegram)
                    if os.getenv('TELEGRAM_SESSION_ENCRYPTION_KEY'):
                        from .integrations import deliver_webhooks
                        deliver_webhooks(db,os.environ['TELEGRAM_SESSION_ENCRYPTION_KEY'],limit=2)
                    from .team_notifications import deliver_assignments
                    deliver_assignments(db,telegram,limit=2)
                if ui.get('active',False) or ui.drain:
                    if product_mode:
                        from .product import deliver_registered
                        deliver_registered(db,config,telegram,limit_per_user=1)
                        ui.drain=False
                    else:
                        deliver(db,ui.config(),limit=1)
                        ui.drain=bool(db.execute("SELECT 1 FROM leads WHERE status='ready' AND next_attempt<=? LIMIT 1",(time.time(),)).fetchone())
            except RuntimeError as exc:
                print(str(exc),flush=True)
                time.sleep(3)
