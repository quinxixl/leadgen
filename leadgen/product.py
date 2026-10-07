"""Multi-user product layer: registration, preferences, feedback and lead CRM."""
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone

from . import accounts
from .app import classify_for_config
from .core import TOPIC_GROUPS, TOPICS, Lead, message, topics, budget
from .billing import PLAN_NAMES, days_left, subscription_active, support_contact
from .drafts import reply_drafts

MONITOR_ON='🔔 Мониторинг: вкл'
MONITOR_OFF='🔕 Мониторинг: выкл'
LATEST='📥 Последние 5'
SETUP='🎯 Услуги и бюджет'
CABINET='🌐 Кабинет'
BILLING='💳 Подписка'
HELP='❓ Помощь'
# Labels of the previous menu. Clients keep showing them until a message carries the new keyboard.
LEGACY_LABELS=('▶️ Начать','⏸ Пауза','📥 Лиды','🗂 Воронка','🎯 Услуги','💰 Фильтры',
               '➕ Добавить чат','🔌 Аккаунт','🧰 Портфолио','📈 Статистика')


def menu(active=False):
    """Reply keyboards are static per message, so the monitoring label is rebuilt from the current state."""
    return {'keyboard':[
        [{'text':MONITOR_ON if active else MONITOR_OFF},{'text':LATEST}],
        [{'text':SETUP},{'text':CABINET}],
        [{'text':BILLING},{'text':HELP}]],
        'resize_keyboard':True,'is_persistent':True}


MENU=menu(False)
MENU_TEXTS={MONITOR_ON,MONITOR_OFF,LATEST,SETUP,CABINET,BILLING,HELP,*LEGACY_LABELS}
REGISTER={'inline_keyboard':[[{'text':'Зарегистрироваться','callback_data':'register:confirm'}]]}
PUBLIC_CHAT=re.compile(r'^(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,})/?$')
PIPELINE={'saved':'Новый / сохранён','viewed':'Просмотрен','working':'В работе','contacted':'Написали',
          'discussing':'Получен ответ','meeting':'Назначена встреча','proposal':'Отправлено предложение','won':'Получил заказ',
          'lost':'Не подошёл','not_fit':'Не подходит'}
FEEDBACK={'fit':'Подходит','ad':'Реклама','job':'Ищет работу','not_service':'Не моя услуга',
          'too_cold':'Слишком холодный','wrong_geo':'Неверная география','competitor':'Конкурент',
          'vacancy':'Вакансия','duplicate':'Дубль'}
MISS_REASONS=tuple(key for key in FEEDBACK if key!='fit')
BUDGET_PRESETS=((0,'Без ограничения'),(10000,'от 10 000 ₽'),(30000,'от 30 000 ₽'),(100000,'от 100 000 ₽'))
TOPIC_INDEX={name:i for i,name in enumerate(TOPICS)}
FIRST_RUN_LIMIT=5
FIRST_RUN_HOURS=72
MSK=timezone(timedelta(hours=3))
# Callbacks that confirm with a toast or edit the card in place answer the query themselves.
DEFERRED_ANSWER={'ob','feedback','miss','card','topic','toggle'}


def amount(text,maximum):
    """Whole rubles within a column's range; anything else is not an amount."""
    value=text.replace(' ','').replace(' ','')
    if not (value.isascii() and value.isdigit() and len(value)<=10):return None
    return int(value) if int(value)<=maximum else None


def money(value):
    return f'{value:,}'.replace(',',' ')


def web_url(path):
    base=os.environ.get('WEB_PUBLIC_URL','').strip().rstrip('/')
    return base+path if base else ''


def web_button(text,app_path,tg_path):
    """Button into the cabinet: Mini App when MINIAPP_ENABLED=1, a plain link otherwise, none without WEB_PUBLIC_URL.

    web_app buttons work only in private chats; every chat of this bot is private."""
    base=os.environ.get('WEB_PUBLIC_URL','').strip().rstrip('/')
    if not base:return None
    if os.environ.get('MINIAPP_ENABLED','').strip()=='1':
        return {'text':text,'web_app':{'url':base+tg_path}}
    return {'text':text,'url':base+app_path}


def lead_link(lead_id,text='Открыть'):
    return web_button(text,f'/app/leads/{lead_id}',f'/tg/leads/{lead_id}')


def support_button():
    contact=support_contact()
    return {'text':'Поддержка','url':'https://t.me/'+contact} if contact else None


def billing_buttons():
    row=[button for button in (web_button('Выбрать тариф','/app/billing','/tg/billing'),support_button()) if button]
    return {'inline_keyboard':[row]} if row else None


def register_text():
    terms,privacy=web_url('/terms'),web_url('/privacy')
    documents=f'\n\nОферта: {terms}\nПолитика конфиденциальности: {privacy}' if terms else ''
    return ('Сигналид находит заказы на ваши услуги в Telegram-чатах и на биржах и присылает их сюда за минуты. '
            'Пробный период — 7 дней, без привязки карты.\n\n'
            'Нажимая «Зарегистрироваться», вы принимаете условия публичной оферты и даёте согласие '
            'на обработку персональных данных в соответствии с политикой конфиденциальности.'+documents)


def lead_buttons(lead_id,url,feedback=None):
    """Compact card: open in the cabinet, the original, two-way rating and an optional Telegram reply."""
    fit='✓ 👍 Подходит' if feedback=='fit' else '👍 Подходит'
    miss='✓ 👎 '+FEEDBACK[feedback] if feedback in MISS_REASONS else '👎 Мимо'
    original={'text':'Оригинал','url':url} if (url or '').startswith(('https://','http://')) else None
    rows=[[button for button in (lead_link(lead_id),original) if button],
          [{'text':fit,'callback_data':f'feedback:fit:{lead_id}'},{'text':miss,'callback_data':f'miss:{lead_id}'}]]
    from .telegram_replies import source_message
    try:source_message(url)
    except ValueError:pass
    else:rows.append([{'text':'✉️ Ответить','callback_data':f'tgcompose:{lead_id}'}])
    return {'inline_keyboard':[row for row in rows if row]}


def miss_buttons(lead_id):
    reasons=[{'text':FEEDBACK[key],'callback_data':f'feedback:{key}:{lead_id}'} for key in MISS_REASONS]
    rows=[reasons[i:i+2] for i in range(0,len(reasons),2)]
    rows.append([{'text':'← Назад','callback_data':f'card:{lead_id}'}])
    return {'inline_keyboard':rows}


def selected_topics(user):
    value=user['topics']
    return list(value) if isinstance(value,list) else json.loads(value)


def _selection_line(selected):
    if not selected:return 'Пока ничего не выбрано.'
    names=', '.join(selected)
    if len(names)>600:names=names[:600].rsplit(', ',1)[0]+'…'
    return f'Выбрано услуг: {len(selected)} — {names}'


def wizard_directions(selected):
    rows=[]
    for gi,(section,group) in enumerate(TOPIC_GROUPS):
        count=sum(name in selected for name,_ in group)
        rows.append([{'text':section+(f' · {count}' if count else ''),'callback_data':f'ob:g:{gi}'}])
    if selected:rows.append([{'text':'Далее →','callback_data':'ob:budget'}])
    return ('Шаг 1 из 4. Выберите направление и отметьте услуги, которые вы оказываете. '
            'Можно выбрать услуги из нескольких направлений.\n\n'+_selection_line(selected)),{'inline_keyboard':rows}


def wizard_services(gi,selected):
    section,group=TOPIC_GROUPS[gi]
    buttons=[{'text':('✅ ' if name in selected else '⬜️ ')+name,'callback_data':f'ob:t:{gi}:{TOPIC_INDEX[name]}'}
             for name,_ in group]
    rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows.append([{'text':'← Направления','callback_data':'ob:dir'},{'text':'Далее →','callback_data':'ob:budget'}])
    return (f'Шаг 2 из 4 · {section}\nОтметьте свои услуги. Услуги из другого направления добавьте через «← Направления».\n\n'
            +_selection_line(selected)),{'inline_keyboard':rows}


def wizard_budget(user):
    current=user['min_budget']
    presets=[{'text':('✅ ' if current==value else '')+label,'callback_data':f'ob:b:{value}'} for value,label in BUDGET_PRESETS]
    rows=[presets[:2],presets[2:],
          [{'text':('✅ ' if user['show_without_budget'] else '⬜️ ')+'Показывать запросы без бюджета','callback_data':'ob:nb'}],
          [{'text':'✏️ Своя сумма','callback_data':'budget:custom'}],
          [{'text':'← Услуги','callback_data':'ob:dir'},{'text':'Далее →','callback_data':'ob:launch'}]]
    custom='' if current in dict(BUDGET_PRESETS) else f'\nСейчас: от {money(current)} ₽.'
    return ('Шаг 3 из 4. Минимальный бюджет заказа.'+custom+'\n\nЕсли бюджет в объявлении не указан, сумма не придумывается: '
            'такие запросы приходят, только когда включено «Показывать запросы без бюджета».'),{'inline_keyboard':rows}


def wizard_launch(user,selected):
    if not selected:
        return 'Сначала выберите хотя бы одну услугу.',{'inline_keyboard':[[{'text':'← Выбрать услуги','callback_data':'ob:dir'}]]}
    floor='без ограничения' if not user['min_budget'] else f"от {money(user['min_budget'])} ₽"
    without='показывать' if user['show_without_budget'] else 'не показывать'
    return ('Шаг 4 из 4. Проверьте настройки.\n\n'+_selection_line(selected)+
            f'\nБюджет: {floor}\nЗапросы без бюджета: {without}\n\n'
            'Нажмите «Запустить» — заказы будут приходить сюда сразу после публикации.'),{'inline_keyboard':[
                [{'text':'🚀 Запустить','callback_data':'ob:go'}],[{'text':'← Назад','callback_data':'ob:budget'}]]}


def user_config(base,row):
    result=copy.deepcopy(base)
    result.setdefault('max_age_hours',72)
    result['min_budget']=row['min_budget']
    result['topics']=row['topics'] if isinstance(row['topics'],list) else json.loads(row['topics'])
    result['notify_without_budget']=False
    switches=row['source_switches'] if isinstance(row['source_switches'],dict) else json.loads(row['source_switches'])
    for source in result.get('sources',[]):
        source['enabled']=switches.get(source['id'],source['enabled'])
    return result


def eligible_for_user(lead,config,prefs):
    state,reason=classify_for_config(lead,config)
    if state=='ready':
        return True,reason
    if reason in ('бюджет не указан','указан только потолок бюджета') and prefs['show_without_budget']:
        return True,'запрос без подтверждённого бюджета'
    if reason.startswith('возможная потребность:') and prefs['show_possible_needs']:
        amount,_=budget(lead.budget_text)
        if amount is None and not prefs['show_without_budget']:
            return False,'возможная потребность без бюджета: показ отключён'
        return True,reason
    return False,reason


def _eligibility(lead,config,prefs,projects):
    """Projects are created disabled; only enabled ones replace the general preferences."""
    allowed,reason=eligible_for_user(lead,config,prefs)
    matches=[]
    if projects:
        from .projects import project_eligibility
        for project in projects:
            match,why=project_eligibility(lead,config,project)
            if match:matches.append(project['id']);reason=why
        allowed=bool(matches)
        if not allowed:reason='не соответствует фильтрам проектов'
    return allowed,reason,matches


def _send_card(db,sender,prefs,lead_id,lead,reason,matches,prefix=''):
    """Send one card and record it; False means the send is uncertain and the batch must stop."""
    if matches:
        with db:
            for project_id in matches:
                db.execute('''INSERT INTO project_leads(project_id,user_id,lead_id) VALUES(?,?,?)
                    ON CONFLICT(project_id,lead_id) DO NOTHING''',(project_id,prefs['id'],lead_id))
    try:
        sender('sendMessage',{'chat_id':str(prefs['telegram_chat_id']),'text':prefix+message(lead,reason),
            'link_preview_options':{'is_disabled':True},'reply_markup':lead_buttons(lead_id,lead.url)})
    except RuntimeError as exc:
        with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason,updated_at)
            VALUES(?,?,'uncertain',?,now()) ON CONFLICT(user_id,lead_id) DO UPDATE SET
            delivery_status='uncertain',filter_reason=excluded.filter_reason,updated_at=now()''',
            (prefs['id'],lead_id,str(exc)))
        return False
    with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason,sent_at,updated_at)
        VALUES(?,?,'sent',?,now(),now()) ON CONFLICT(user_id,lead_id) DO UPDATE SET
        delivery_status='sent',filter_reason=excluded.filter_reason,sent_at=now(),updated_at=now()''',
        (prefs['id'],lead_id,reason))
    with db:
        from .integrations import enqueue_lead_event
        enqueue_lead_event(db,prefs['id'],lead_id,'lead.created')
    return True


RECIPIENTS='''SELECT u.id,u.telegram_chat_id,u.registered_at,p.* FROM app_users u
    JOIN user_preferences p ON p.user_id=u.id
    JOIN subscriptions s ON s.user_id=u.id
    WHERE u.status='active' AND p.monitoring_active=true
      AND s.status IN ('stub_active','trial','active')
      AND (s.ends_at IS NULL OR s.ends_at>now())'''
UNSEEN='''l.status<>'duplicate' AND (l.origin_user_id IS NULL OR l.origin_user_id=?)
      AND NOT EXISTS (SELECT 1 FROM user_leads ul WHERE ul.user_id=? AND ul.lead_id=l.id)
      AND NOT EXISTS (SELECT 1 FROM user_leads seen JOIN leads original ON original.id=seen.lead_id
          WHERE seen.user_id=? AND seen.delivery_status<>'filtered' AND original.fingerprint=l.fingerprint)'''


def deliver_registered(db,base,sender,limit_per_user=1):
    users=db.execute(RECIPIENTS+' ORDER BY u.id').fetchall()
    delivered=0
    for prefs in users:
        config=user_config(base,prefs);processed=0
        projects=db.execute('SELECT * FROM projects WHERE user_id=? AND enabled=true ORDER BY id',(prefs['id'],)).fetchall()
        # The live stream starts at registration; older leads reach a user only through the one-time first-run batch.
        rows=db.execute('SELECT l.id,l.payload FROM leads l WHERE '+UNSEEN+''' AND l.first_seen::timestamptz >= ?
            ORDER BY l.id DESC LIMIT 100''',(prefs['id'],prefs['id'],prefs['id'],prefs['registered_at'])).fetchall()
        for row in rows:
            lead=Lead(**json.loads(row['payload']))
            allowed,reason,matches=_eligibility(lead,config,prefs,projects)
            if not allowed:
                with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason)
                    VALUES(?,?,'filtered',?) ON CONFLICT(user_id,lead_id) DO NOTHING''',(prefs['id'],row['id'],reason))
                continue
            if not _send_card(db,sender,prefs,row['id'],lead,reason,matches):break
            delivered+=1;processed+=1
            if processed>=limit_per_user:break
    return delivered


def backfill_first_leads(db,base,sender,user_id,limit=FIRST_RUN_LIMIT,hours=FIRST_RUN_HOURS):
    """When monitoring starts for the first time, send the best matching leads of the last hours once.

    A settings marker makes the batch one-time; user_leads rows keep the live stream from repeating them."""
    prefs=db.execute(RECIPIENTS+' AND u.id=?',(user_id,)).fetchone()
    if not prefs:return 0
    with db:
        claimed=db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING RETURNING key',
            (f'first_run:{user_id}',json.dumps(datetime.now(timezone.utc).isoformat()))).fetchone()
    if not claimed:return 0
    if db.execute("SELECT 1 FROM user_leads WHERE user_id=? AND delivery_status IN ('sent','uncertain') LIMIT 1",(user_id,)).fetchone():
        return 0
    config=user_config(base,prefs)
    projects=db.execute('SELECT * FROM projects WHERE user_id=? AND enabled=true ORDER BY id',(user_id,)).fetchall()
    rows=db.execute('SELECT l.id,l.payload,l.fingerprint FROM leads l WHERE '+UNSEEN+''' AND
        l.first_seen::timestamptz >= now()-(? * interval '1 hour') ORDER BY l.id DESC LIMIT 500''',
        (user_id,user_id,user_id,hours)).fetchall()
    candidates=[]
    for row in rows:
        lead=Lead(**json.loads(row['payload']))
        allowed,reason,matches=_eligibility(lead,config,prefs,projects)
        if allowed:candidates.append((lead.score,row['id'],row['fingerprint'],lead,reason,matches))
    candidates.sort(key=lambda item:(item[0],item[1]),reverse=True)
    sent=0;fingerprints=set()
    for _,lead_id,fingerprint,lead,reason,matches in candidates:
        if sent>=limit:break
        if fingerprint in fingerprints:continue
        fingerprints.add(fingerprint)
        if not _send_card(db,sender,prefs,lead_id,lead,reason,matches,'🕘 Из недавних\n'):break
        sent+=1
    return sent


def backfill_pending(db,base,sender,limit_users=5):
    """First-run batches for users who enabled monitoring outside the bot: web cabinet or Mini App."""
    rows=db.execute(RECIPIENTS+''' AND NOT EXISTS (SELECT 1 FROM settings st WHERE st.key='first_run:'||u.id)
        ORDER BY u.id LIMIT ?''',(limit_users,)).fetchall()
    return sum(backfill_first_leads(db,base,sender,row['id']) for row in rows)


def notify_subscriptions(db,sender,limit=20):
    """Warn once two days before the access ends and once on the last day; markers are keyed by ends_at."""
    rows=db.execute('''SELECT * FROM (SELECT u.id,u.telegram_chat_id,s.status,s.plan_code,s.ends_at,
            s.ends_at<=now()+interval '1 day' AS last_day,
            'subnote:'||u.id||':'||(CASE WHEN s.ends_at<=now()+interval '1 day' THEN 'last' ELSE 'soon' END)
                ||':'||floor(extract(epoch FROM s.ends_at))::bigint AS marker
            FROM app_users u JOIN subscriptions s ON s.user_id=u.id
            WHERE u.status='active' AND s.status IN ('stub_active','trial','active')
              AND s.ends_at IS NOT NULL AND s.ends_at>now() AND s.ends_at<=now()+interval '2 days') due
        WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key=due.marker) ORDER BY ends_at LIMIT ?''',(limit,)).fetchall()
    sent=0
    for row in rows:
        with db:
            claimed=db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING RETURNING key',
                (row['marker'],json.dumps(datetime.now(timezone.utc).isoformat()))).fetchone()
        if not claimed:continue
        when=row['ends_at'].astimezone(MSK).strftime('%d.%m в %H:%M МСК')
        trial=row['status']=='trial' or row['plan_code']=='trial'
        period='пробного периода' if trial else f"тарифа «{PLAN_NAMES.get(row['plan_code'],row['plan_code'])}»"
        if row['last_day']:
            text=f'Сегодня последний день {period}: доступ закончится {when}.'
        else:
            text=('Пробный период' if trial else f"Оплаченный период {period}")+f' закончится {when}.'
        markup=billing_buttons()
        text+=' Чтобы мониторинг и уведомления о заказах не остановились, выберите тариф.'
        if not markup:text+=' Это можно сделать в веб-кабинете, раздел «Подписка».'
        payload={'chat_id':str(row['telegram_chat_id']),'text':text}
        if markup:payload['reply_markup']=markup
        # At most once: an ambiguous send is not repeated, the marker stays.
        try:sender('sendMessage',payload)
        except Exception as exc:print('Напоминание о подписке: '+type(exc).__name__,flush=True)
        else:sent+=1
    return sent


class ProductController:
    def __init__(self,db,base,sender):
        self.db,self.base,self.sender=db,base,sender
        self.manual=False;self.drain=False;self.last_chat=None
        self.active=False;self.callback=None;self._answered=True

    def get(self,key,default=None):
        if key=='active':
            row=self.db.execute('SELECT 1 FROM user_preferences WHERE monitoring_active=true LIMIT 1').fetchone()
            return bool(row)
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else default

    def put(self,key,value):
        with self.db:self.db.execute('''INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value''',(key,json.dumps(value)))

    def config(self):return copy.deepcopy(self.base)

    def say(self,text,markup=None,chat_id=None):
        target=str(chat_id or self.last_chat)
        return self.sender('sendMessage',{'chat_id':target,'text':text,'reply_markup':markup or menu(self.active)})

    def _answer(self,text=None):
        """Answer the pending callback query once; a text becomes a toast instead of a chat message."""
        if self._answered or not self.callback:return
        self._answered=True
        payload={'callback_query_id':self.callback['id']}
        if text:payload['text']=text[:200]
        self.sender('answerCallbackQuery',payload)

    def _message_id(self):
        return ((self.callback or {}).get('message') or {}).get('message_id')

    def _quiet(self,method,payload):
        # 400 means "not modified" or a message too old to edit: the change is already saved.
        try:return self.sender(method,payload)
        except RuntimeError as exc:
            if 'HTTP 400' in str(exc):return None
            raise

    def _edit(self,text,markup=None):
        if not self._message_id():return self.say(text,markup)
        payload={'chat_id':self.last_chat,'message_id':self._message_id(),'text':text,
                 'link_preview_options':{'is_disabled':True}}
        if markup:payload['reply_markup']=markup
        return self._quiet('editMessageText',payload)

    def _edit_markup(self,markup):
        if not self._message_id():return None
        return self._quiet('editMessageReplyMarkup',{'chat_id':self.last_chat,'message_id':self._message_id(),'reply_markup':markup})

    def _user(self,telegram_id):
        return self.db.execute('''SELECT u.*,p.monitoring_active,p.min_budget,p.show_without_budget,
            p.show_possible_needs,p.topics,p.source_switches,p.profile_services,p.portfolio,p.state,
            s.status AS subscription_status,s.plan_code,s.ends_at
            FROM app_users u JOIN user_preferences p ON p.user_id=u.id
            JOIN subscriptions s ON s.user_id=u.id WHERE u.telegram_user_id=?''',(telegram_id,)).fetchone()

    def _register(self,actor,chat_id,accepted_terms=True):
        accounts.register_user(self.db,actor,chat_id,accepted_terms=accepted_terms)
        return self._user(actor['id'])

    def _update_pref(self,user_id,column,value):
        allowed={'monitoring_active','min_budget','show_without_budget','show_possible_needs','topics',
                 'profile_services','portfolio','state','source_switches'}
        if column not in allowed:raise ValueError('unknown preference')
        with self.db:
            self.db.execute(f'UPDATE user_preferences SET {column}=?,updated_at=now() WHERE user_id=?',(value,user_id))
            self.db.execute("DELETE FROM user_leads WHERE user_id=? AND delivery_status='filtered'",(user_id,))

    def _filters(self,user,edit=False):
        keys={'inline_keyboard':[
            [{'text':('✅ ' if user['show_without_budget'] else '⬜️ ')+'Без бюджета','callback_data':'toggle:no_budget'}],
            [{'text':('✅ ' if user['show_possible_needs'] else '⬜️ ')+'Возможные потребности','callback_data':'toggle:possible'}],
            [{'text':f"Минимум: {user['min_budget']} ₽",'callback_data':'budget:custom'}]]}
        if edit:return self._edit_markup(keys)
        self.say('Настройте поток лидов. Неизвестный бюджет остаётся неизвестным и не заменяется выдуманной суммой.',keys)

    def _services(self,user,edit=False):
        selected=selected_topics(user)
        buttons=[{'text':('✅ ' if name in selected else '⬜️ ')+name,
            'callback_data':f'topic:{i}'} for i,name in enumerate(TOPICS)]
        keys={'inline_keyboard':[buttons[i:i+2] for i in range(0,len(buttons),2)]}
        if edit:return self._edit_markup(keys)
        self.say('Какие услуги вы оказываете?',keys)

    def _lead(self,lead_id):
        row=self.db.execute('''SELECT l.id,l.payload,l.reason FROM leads l JOIN user_leads ul ON ul.lead_id=l.id
            JOIN app_users u ON u.id=ul.user_id WHERE l.id=? AND u.telegram_chat_id=?
            AND ul.delivery_status<>'filtered' ''',(lead_id,int(self.last_chat))).fetchone()
        return (row,Lead(**json.loads(row['payload']))) if row else (None,None)

    def _feedback(self,user_id,lead_id):
        row=self.db.execute('SELECT label FROM lead_feedback WHERE user_id=? AND lead_id=?',(user_id,lead_id)).fetchone()
        return row['label'] if row else None

    def _draft(self,user,lead,lead_id):
        project=self.db.execute('''SELECT p.* FROM project_leads pl JOIN projects p
            ON p.id=pl.project_id AND p.user_id=pl.user_id
            WHERE pl.user_id=? AND pl.lead_id=? ORDER BY p.id LIMIT 1''',(user['id'],lead_id)).fetchone()
        settings=dict(project) if project else None
        try:
            from .ai_offers import generate_for_lead
            from .teams import ensure_workspace
            workspace_id=ensure_workspace(self.db,user)
            drafts,_,_=generate_for_lead(self.db,workspace_id,user['id'],user['id'],lead_id,lead,
                user['profile_services'],user['portfolio'],settings)
            heading='AI-офферы от Groq. Проверьте факты, цену и сроки перед отправкой.'
        except Exception as exc:
            from .ai_offers import AIOfferError
            drafts=reply_drafts(lead,user['profile_services'],user['portfolio'],settings)
            reason=str(exc) if isinstance(exc,AIOfferError) else 'AI временно недоступен.'
            heading=reason+' Ниже быстрые локальные шаблоны.'
        return (heading+'\n\n'+'\n\n'.join(name+':\n'+text for name,text in drafts.items()))[:3900]

    def _subscription_text(self,user):
        row={'status':user['subscription_status'],'ends_at':user['ends_at']}
        name=PLAN_NAMES.get(user['plan_code'],user['plan_code'])
        if not subscription_active(row):
            line='Доступ приостановлен: пробный период или оплаченный срок закончился.'
        elif days_left(row) is None:
            line=f'Тариф «{name}» активен.'
        else:
            line=f'Тариф «{name}». Осталось дней: {days_left(row)}.'
        contact=support_contact()
        if web_url('/app/billing'):tail='\n\nВыбрать или продлить тариф можно кнопкой ниже.'
        elif contact:tail=f'\n\nПродлить или сменить тариф — в веб-кабинете в разделе «Подписка» или у поддержки: @{contact}'
        else:tail='\n\nПродлить или сменить тариф можно в веб-кабинете, раздел «Подписка».'
        return line+tail

    def _reply_services(self):
        from .telegram_replies import ReplyGateway
        from .telegram_accounts import SessionCipher
        return SessionCipher(os.environ.get('TELEGRAM_SESSION_ENCRYPTION_KEY','')),ReplyGateway()

    def _reply_callback(self,user,parts):
        from . import telegram_replies as replies
        from .telegram_accounts import TelegramAccountError
        action=parts[0]
        if action not in ('tgcompose','tgmode','tgsend','tgcancel'):return False
        try:
            if action=='tgcompose' and len(parts)==2 and parts[1].isdigit():
                lid=int(parts[1]);replies.authorized_lead(self.db,user['id'],user['id'],lid)
                replies.connection(self.db,user['id'])
                self.say('Куда отправить отклик с вашего Telegram-аккаунта?',{'inline_keyboard':[
                    [{'text':label,'callback_data':f'tgmode:{mode}:{lid}'}] for mode,label in replies.MODES.items()]})
            elif action=='tgmode' and len(parts)==3 and parts[1] in replies.MODES and parts[2].isdigit():
                lid=int(parts[2]);replies.authorized_lead(self.db,user['id'],user['id'],lid)
                self._update_pref(user['id'],'state',json.dumps({'await':'telegram_reply','lead_id':lid,'mode':parts[1]}))
                self.say(replies.MODES[parts[1]]+'. Напишите текст отклика (до 3000 символов). Можно вставить подготовленный оффер. Сначала покажу подтверждение. /cancel — отменить.')
            elif action in ('tgsend','tgcancel') and len(parts)==2:
                if action=='tgcancel':row=replies.cancel(self.db,parts[1],user['id'],user['id'])
                else:
                    cipher,gateway=self._reply_services()
                    row=replies.confirm(self.db,parts[1],user['id'],user['id'],cipher,gateway)
                self._update_pref(user['id'],'state',json.dumps({}))
                self.say(replies.STATUSES[row['status']]+('. '+row['error'] if row['error'] else ''))
        except TelegramAccountError as exc:self.say(str(exc))
        except RuntimeError:raise
        except Exception as exc:
            # The bot loop only survives RuntimeError; an unexpected error here must not
            # restart the bot. A claimed send stays 'sending' and is later shown as uncertain.
            print('Telegram-отклик: '+type(exc).__name__,flush=True)
            self.say('Не удалось обработать отклик. Проверьте его статус на сайте перед повтором.')
        return True

    def _reply_text(self,user,state,text):
        from . import telegram_replies as replies
        from .telegram_accounts import TelegramAccountError
        try:
            cipher,gateway=self._reply_services()
            row=replies.prepare(self.db,user['id'],user['id'],state['lead_id'],state['mode'],text,cipher,gateway)
            self._update_pref(user['id'],'state',json.dumps({'await':'telegram_reply_confirm','reply_id':row['id']}))
            self.say('Проверьте отклик. Подтверждение действует 15 минут.\n\nОт: '+row['sender_label']+
                '\n'+replies.MODES[row['mode']]+': '+row['recipient_label']+'\n\n'+row['body'],{'inline_keyboard':[
                    [{'text':'Подтвердить и отправить','callback_data':'tgsend:'+row['id']}],
                    [{'text':'Отменить','callback_data':'tgcancel:'+row['id']}]]})
        except TelegramAccountError as exc:self.say(str(exc))
        except RuntimeError:raise
        except Exception as exc:
            print('Telegram-отклик: '+type(exc).__name__,flush=True)
            self._update_pref(user['id'],'state',json.dumps({}))
            self.say('Не удалось подготовить отклик. Попробуйте ещё раз кнопкой под лидом.')

    def _stats(self,user_id):
        feedback={r['label']:r['total'] for r in self.db.execute(
            'SELECT label,count(*) AS total FROM lead_feedback WHERE user_id=? GROUP BY label',(user_id,))}
        pipeline={r['pipeline_status']:r['total'] for r in self.db.execute(
            'SELECT pipeline_status,count(*) AS total FROM user_leads WHERE user_id=? GROUP BY pipeline_status',(user_id,))}
        money_total=self.db.execute("SELECT coalesce(sum(deal_amount),0) AS total FROM user_leads WHERE user_id=? AND pipeline_status='won'",(user_id,)).fetchone()['total']
        sources=self.db.execute('''SELECT l.payload::jsonb->>'source' AS source,count(*) AS found,
            count(*) FILTER (WHERE f.label='fit') AS fit
            FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
            LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
            WHERE ul.user_id=? AND ul.delivery_status='sent'
            GROUP BY l.payload::jsonb->>'source' ORDER BY found DESC LIMIT 5''',(user_id,)).fetchall()
        lines=['Качество отбора:']+[f'• {FEEDBACK[k]}: {feedback.get(k,0)}' for k in FEEDBACK]
        lines+=['','Воронка:']+[f'• {PIPELINE[k]}: {pipeline.get(k,0)}' for k in PIPELINE]
        lines.append(f'• Сумма выигранных сделок: {money(money_total)} ₽')
        if sources:
            lines+=['','Качество источников:']+[f"• {r['source']}: найдено {r['found']}, подходит {r['fit']}" for r in sources]
        self.say('\n'.join(lines))

    def _configured(self,user_id):
        if self.db.execute('SELECT 1 FROM settings WHERE key=?',(f'first_run:{user_id}',)).fetchone():return True
        return bool(self.db.execute("SELECT 1 FROM user_leads WHERE user_id=? AND delivery_status IN ('sent','uncertain') LIMIT 1",(user_id,)).fetchone())

    def _needs_onboarding(self,user):
        return not selected_topics(user) or (not user['monitoring_active'] and not self._configured(user['id']))

    def _first_leads(self,user):
        try:return backfill_first_leads(self.db,self.base,self.sender,user['id'])
        except RuntimeError:raise
        except Exception as exc:
            # Only RuntimeError is survivable in the bot loop; the welcome batch is optional.
            print('Первые лиды: '+type(exc).__name__,flush=True)
            self.db.connection.rollback()
            return 0

    def _launch(self,user):
        selected=selected_topics(user)
        if not selected:
            self._answer('Сначала выберите хотя бы одну услугу')
            return self._edit(*wizard_directions(selected))
        self._answer('Мониторинг запущен')
        self._update_pref(user['id'],'monitoring_active',True);self.active=True;self.manual=True
        self._edit('🚀 Мониторинг запущен.\n\n'+_selection_line(selected)+
                   '\n\nИзменить услуги и бюджет можно кнопкой «🎯 Услуги и бюджет».')
        self.say('🔔 Мониторинг включён. Новые заказы по вашим услугам будут приходить сюда сразу после публикации.')
        self._first_leads(user)

    def _wizard(self,user,parts):
        selected=selected_topics(user);step=parts[1] if len(parts)>1 else ''
        index=lambda value,size:value.isdigit() and int(value)<size
        if step=='dir':view=wizard_directions(selected)
        elif step=='g' and len(parts)==3 and index(parts[2],len(TOPIC_GROUPS)):
            view=wizard_services(int(parts[2]),selected)
        elif step=='t' and len(parts)==4 and index(parts[2],len(TOPIC_GROUPS)) and index(parts[3],len(TOPICS)):
            group=int(parts[2]);name=list(TOPICS)[int(parts[3])]
            if name not in dict(TOPIC_GROUPS[group][1]):return self._answer()
            if name in selected:selected.remove(name)
            else:selected.append(name)
            self._update_pref(user['id'],'topics',json.dumps(selected,ensure_ascii=False))
            view=wizard_services(group,selected)
        elif step=='budget':view=wizard_budget(user)
        elif step=='b' and len(parts)==3 and parts[2].isdigit() and int(parts[2]) in dict(BUDGET_PRESETS):
            self._update_pref(user['id'],'min_budget',int(parts[2]))
            view=wizard_budget(dict(user)|{'min_budget':int(parts[2])})
        elif step=='nb':
            self._update_pref(user['id'],'show_without_budget',not user['show_without_budget'])
            view=wizard_budget(dict(user)|{'show_without_budget':not user['show_without_budget']})
        elif step=='launch':view=wizard_launch(user,selected)
        elif step=='go':return self._launch(user)
        else:return self._answer()
        self._answer()
        self._edit(*view)

    def _set_monitoring(self,user,on):
        if not on:
            self._update_pref(user['id'],'monitoring_active',False);self.active=False
            return self.say('🔕 Мониторинг приостановлен. Новые заказы не будут приходить, пока вы его не включите.')
        if not selected_topics(user):
            self.say('Сначала выберите услуги — без них мониторинг не найдёт подходящих заказов.')
            return self.say(*wizard_directions([]))
        self._update_pref(user['id'],'monitoring_active',True);self.active=True;self.manual=True
        self.say('🔔 Мониторинг включён. Новые заказы по вашим услугам будут приходить сюда сразу после публикации.')
        self._first_leads(user)

    def _latest(self,user):
        rows=self.db.execute('''SELECT l.id,l.payload,ul.filter_reason,f.label AS feedback FROM user_leads ul
            JOIN leads l ON l.id=ul.lead_id
            LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
            WHERE ul.user_id=? AND ul.delivery_status='sent'
            ORDER BY ul.sent_at DESC NULLS LAST,l.id DESC LIMIT 5''',(user['id'],)).fetchall()
        if not rows:
            hint='Новые придут сюда автоматически.' if user['monitoring_active'] else f'Включите мониторинг кнопкой «{MONITOR_OFF}».'
            return self.say('Пока нет присланных заказов. '+hint)
        # Oldest first, so the newest card ends up at the bottom of the chat.
        for row in reversed(rows):
            lead=Lead(**json.loads(row['payload']))
            self.sender('sendMessage',{'chat_id':self.last_chat,'text':message(lead,row['filter_reason'] or 'ранее присланный заказ'),
                'link_preview_options':{'is_disabled':True},'reply_markup':lead_buttons(row['id'],lead.url,row['feedback'])})

    def _cabinet(self):
        text=('Веб-кабинет: все лиды и воронка, статусы и напоминания, проекты, команда '
              'и подключение своего Telegram-аккаунта для откликов.')
        button=web_button('🌐 Открыть кабинет','/app','/tg')
        if button:return self.say(text,{'inline_keyboard':[[button]]})
        self.say(text+' Войдите на сайте Сигналид через Telegram.')

    def _help(self):
        text=('Как это работает\n\n'
              '• Сигналид круглосуточно читает Telegram-чаты и биржи и присылает сюда заказы по вашим услугам.\n'
              '• Под карточкой: «Открыть» — статус, напоминание и отклик в кабинете; «Оригинал» — исходное сообщение; '
              '«👍 Подходит» и «👎 Мимо» — оценка, по ней улучшается отбор.\n'
              f'• «{SETUP}» — что искать и от какого бюджета. «🔔 Мониторинг» — включить или поставить на паузу.\n'
              f'• «{LATEST}» — повторить последние присланные заказы.')
        rows=[[{'text':'📈 Статистика','callback_data':'stats'}]]
        extra=[button for button in (web_button('🌐 Кабинет','/app','/tg'),support_button()) if button]
        if extra:rows.append(extra)
        self.say(text,{'inline_keyboard':rows})

    def _moved(self,label):
        """Old menu sections now live in the cabinet; the reply carries the new keyboard."""
        target={'➕ Добавить чат':('Свои Telegram-группы подключаются в веб-кабинете, раздел «Мой Telegram».','/app/telegram'),
                '🔌 Аккаунт':('Свой Telegram-аккаунт подключается в веб-кабинете, раздел «Мой Telegram». '
                             'Не отправляйте боту коды входа и пароли.','/app/telegram'),
                '🧰 Портфолио':('Услуги и примеры работ для откликов заполняются в веб-кабинете, раздел «Настройки».','/app/settings')}[label]
        link=web_url(target[1])
        self.say(target[0]+(f'\n\n{link}' if link else '')+'\n\nМеню бота обновлено.')

    def handle(self,update):
        callback=update.get('callback_query');msg=(callback or {}).get('message') if callback else update.get('message')
        actor=(callback or msg or {}).get('from',{});chat=(msg or {}).get('chat',{})
        if chat.get('type')!='private' or str(chat.get('id'))!=str(actor.get('id')):return
        self.last_chat=str(chat['id']);self.callback=callback;self._answered=not callback;self.active=False
        try:self._handle(callback,msg,actor,chat)
        finally:
            if callback and not self._answered:
                try:self._answer()
                except RuntimeError:pass

    def _handle(self,callback,msg,actor,chat):
        chat_id=self.last_chat
        user=self._user(actor['id']);self.active=bool(user and user['monitoring_active'])
        text=(msg or {}).get('text','').strip()
        if text.startswith('/start web_') or (callback and callback.get('data','').startswith('webok:')):
            from .web_auth import login_record, approve_login, code_choices
            if callback:
                token,_,code=callback['data'][6:].partition(':')
            else:
                token,code=text[len('/start web_'):],''
            try:
                if user and user['status']=='blocked':
                    self.say('Доступ заблокирован.');return
                record=login_record(self.db,token)
                if callback:
                    self._answer()
                    # The site requires accepting the offer and the privacy policy before a login starts.
                    if not user:user=self._register(actor,chat['id'],accepted_terms=True)
                    approve_login(self.db,token,user['id'],code)
                    self.say('Вход подтверждён. Вернитесь на сайт и нажмите «Завершить вход».')
                else:
                    # The code is never shown here: the user must read it on the site.
                    keys=[[{'text':choice,'callback_data':'webok:'+token+':'+choice} for choice in code_choices(record)]]
                    requester=record.get('requester') or 'неизвестный браузер'
                    self.say('Вход в веб-кабинет Сигналид.\n\nЗапрос: '+requester+
                        '\nВыберите код, который сейчас показан на сайте. Если вы не открывали сайт сами — '
                        'ничего не нажимайте: кто-то пытается войти в ваш аккаунт.\n\n'
                        'Доступ к вашей переписке при входе не предоставляется.',{'inline_keyboard':keys})
            except ValueError as exc:self.say(str(exc))
            return
        if not user:
            self._answer()
            if callback and callback.get('data')=='register:confirm':
                user=self._register(actor,chat['id'],accepted_terms=True)
                self.say('Регистрация завершена. Пробный период — 7 дней, без привязки карты.\n\n'
                         'Осталось выбрать услуги — это займёт минуту.',chat_id=chat_id)
                self.say(*wizard_directions(selected_topics(user)),chat_id=chat_id)
            else:self.say(register_text(),REGISTER,chat_id)
            return
        with self.db:self.db.execute('UPDATE app_users SET last_seen_at=now() WHERE id=?',(user['id'],))
        if user['status']=='blocked':
            self._answer();self.say('Аккаунт заблокирован. Обратитесь в поддержку.');return
        if not subscription_active({'status':user['subscription_status'],'ends_at':user['ends_at']}) and text!=BILLING:
            self._answer()
            self.say('Доступ приостановлен: пробный период или оплаченный срок закончился. '
                     'Выберите тариф, чтобы снова получать заказы.',billing_buttons());return
        if callback:
            parts=callback.get('data','').split(':');action=parts[0]
            if action not in DEFERRED_ANSWER:self._answer()
            if self._reply_callback(user,parts):return
            if action=='ob':return self._wizard(user,parts)
            if action=='topic' and len(parts)==2 and parts[1].isdigit() and int(parts[1])<len(TOPICS):
                selected=selected_topics(user);name=list(TOPICS)[int(parts[1])]
                if name in selected:selected.remove(name)
                else:selected.append(name)
                self._update_pref(user['id'],'topics',json.dumps(selected,ensure_ascii=False))
                self._answer();self._services(dict(user)|{'topics':selected},edit=True)
            elif action=='toggle' and len(parts)==2 and parts[1] in ('no_budget','possible'):
                column='show_without_budget' if parts[1]=='no_budget' else 'show_possible_needs'
                self._update_pref(user['id'],column,not user[column])
                self._answer();self._filters(dict(user)|{column:not user[column]},edit=True)
            elif action=='budget':self._update_pref(user['id'],'state',json.dumps({'await':'budget'}));self.say('Введите минимальный бюджет числом в рублях. /cancel — отменить.')
            elif action=='feedback' and len(parts)==3 and parts[1] in FEEDBACK and parts[2].isdigit():
                lid=int(parts[2]);row,lead=self._lead(lid)
                if not row:return self._answer('Лид не найден')
                with self.db:self.db.execute('''INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,?)
                    ON CONFLICT(user_id,lead_id) DO UPDATE SET label=excluded.label,updated_at=now()''',(user['id'],lid,parts[1]))
                self._answer('👍 Отмечено: подходит' if parts[1]=='fit' else '👎 Отмечено: '+FEEDBACK[parts[1]].lower())
                self._edit_markup(lead_buttons(lid,lead.url,parts[1]))
            elif action in ('miss','card') and len(parts)==2 and parts[1].isdigit():
                lid=int(parts[1]);row,lead=self._lead(lid)
                if not row:return self._answer('Лид не найден')
                self._answer('Почему не подходит?' if action=='miss' else None)
                self._edit_markup(miss_buttons(lid) if action=='miss' else lead_buttons(lid,lead.url,self._feedback(user['id'],lid)))
            elif action=='stats':self._stats(user['id'])
            elif action=='register':self.say('Вы уже зарегистрированы. Выберите действие в меню.')
            elif action=='pipeline' and len(parts)==3 and parts[1]=='menu' and parts[2].isdigit():
                lid=parts[2];keys={'inline_keyboard':[[{'text':label,'callback_data':f'status:{key}:{lid}'}] for key,label in PIPELINE.items()]}
                self.say('Выберите результат работы с лидом:',keys)
            elif action=='status' and len(parts)==3 and parts[1] in PIPELINE and parts[2].isdigit():
                row,_=self._lead(int(parts[2]))
                if not row:self.say('Лид не найден.');return
                with self.db:
                    self.db.execute('''UPDATE user_leads SET pipeline_status=?,updated_at=now()
                        WHERE user_id=? AND lead_id=?''',(parts[1],user['id'],int(parts[2])))
                    self.db.execute("INSERT INTO lead_activity(user_id,lead_id,kind,detail) VALUES(?,?,'status',?)",
                        (user['id'],int(parts[2]),PIPELINE[parts[1]]))
                if parts[1]=='won':self._update_pref(user['id'],'state',json.dumps({'await':'deal_amount','lead_id':int(parts[2])}));self.say('Заказ отмечен полученным. Напишите сумму сделки в рублях или 0, если не хотите указывать.')
                else:self.say('Статус сохранён: '+PIPELINE[parts[1]])
            elif action=='remind' and len(parts)==2 and parts[1].isdigit():
                from .reminders import schedule
                try:
                    schedule(self.db,user['id'],int(parts[1]),datetime.now(timezone.utc)+timedelta(hours=1))
                    self.say('Напомню об этом лиде через час.')
                except ValueError as exc:self.say(str(exc))
            elif action=='reply' and len(parts)==2 and parts[1].isdigit():
                lead_id=int(parts[1]);_,lead=self._lead(lead_id)
                if lead:
                    self.say('Готовлю персональные офферы…')
                    self.say(self._draft(user,lead,lead_id))
                    self.say('Можно отредактировать выбранный вариант и отправить его со своего аккаунта.',lead_buttons(lead_id,lead.url))
                else:self.say('Лид не найден.')
            elif action=='subscribe':
                self.say(self._subscription_text(user),billing_buttons())
            return
        state=user['state'] if isinstance(user['state'],dict) else json.loads(user['state'])
        if text=='/cancel':
            if state.get('await')=='telegram_reply_confirm':
                from .telegram_replies import cancel
                try:cancel(self.db,state.get('reply_id',''),user['id'],user['id'])
                except ValueError:pass
            self._update_pref(user['id'],'state',json.dumps({}));self.say('Ввод отменён.');return
        if state.get('await') and (text in MENU_TEXTS or text.startswith('/')):
            # Menu buttons and commands leave any text-input mode instead of becoming its value.
            if state.get('await')=='telegram_reply_confirm':
                from .telegram_replies import cancel
                try:cancel(self.db,state.get('reply_id',''),user['id'],user['id'])
                except ValueError:pass
            self._update_pref(user['id'],'state',json.dumps({}));state={}
        if state.get('await')=='telegram_reply':
            if not text:self.say('Отправьте текст отклика сообщением или /cancel.');return
            self._reply_text(user,state,text);return
        if state.get('await')=='telegram_reply_confirm':
            self.say('Нажмите «Подтвердить и отправить» под откликом или /cancel для отмены и редактирования.');return
        if state.get('await')=='budget' and amount(text,100000000) is not None:
            value=amount(text,100000000);self._update_pref(user['id'],'min_budget',value);self._update_pref(user['id'],'state',json.dumps({}));self.say(f'Минимальный бюджет: {money(value)} ₽');return
        if state.get('await')=='deal_amount' and amount(text,1000000000) is not None:
            value=amount(text,1000000000);
            with self.db:self.db.execute('UPDATE user_leads SET deal_amount=?,updated_at=now() WHERE user_id=? AND lead_id=?',(value,user['id'],state['lead_id']))
            self._update_pref(user['id'],'state',json.dumps({}));self.say('Сумма сделки сохранена.');return
        if state.get('await')=='portfolio':self._update_pref(user['id'],'portfolio',text[:3000]);self._update_pref(user['id'],'state',json.dumps({}));self.say('Примеры работ сохранены.');return
        if state.get('await')=='chat':
            match=PUBLIC_CHAT.match(text);self._update_pref(user['id'],'state',json.dumps({}))
            if not match:self.say('Нужна публичная ссылка вида https://t.me/example_chat или @example_chat.');return
            username=match.group(1).lower();source=self.db.execute("SELECT username FROM telegram_sources WHERE username=? AND enabled=1 AND status='active'",(username,)).fetchone()
            if not source:self.say('Этот чат пока не входит в проверенный realtime-реестр.');return
            with self.db:self.db.execute('''INSERT INTO user_chat_sources(user_id,username) VALUES(?,?)
                ON CONFLICT(user_id,username) DO UPDATE SET enabled=true''',(user['id'],username))
            self.say('Чат добавлен. Сообщения поступят, если мониторинговый Telegram-аккаунт состоит в этой группе.');return
        command=text.split(' ',1)[0]
        if command in ('/start','/menu'):
            if self._needs_onboarding(user):
                self.say('Давайте настроим поиск заказов — это займёт минуту.')
                self.say(*wizard_directions(selected_topics(user)))
            else:
                self.say('Мониторинг '+('включён: новые заказы приходят сюда автоматически.' if self.active
                         else f'выключен. Включить — кнопка «{MONITOR_OFF}».')+' Остальное — в меню ниже.')
        elif text in (MONITOR_ON,MONITOR_OFF):self._set_monitoring(user,text==MONITOR_OFF)
        elif text=='▶️ Начать':self._set_monitoring(user,True)
        elif text=='⏸ Пауза':self._set_monitoring(user,False)
        elif text in (LATEST,'📥 Лиды'):self._latest(user)
        elif text in (SETUP,'🎯 Услуги'):self.say(*wizard_directions(selected_topics(user)))
        elif text=='💰 Фильтры':self.say(*wizard_budget(user))
        elif text==CABINET:self._cabinet()
        elif text==BILLING:self.say(self._subscription_text(user),billing_buttons())
        elif text==HELP:self._help()
        elif text in ('📈 Статистика','🗂 Воронка'):self._stats(user['id'])
        elif text in ('➕ Добавить чат','🔌 Аккаунт','🧰 Портфолио'):self._moved(text)
        else:self.say('Выберите действие в меню.')
