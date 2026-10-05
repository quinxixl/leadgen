"""Multi-user product layer: registration, preferences, feedback and lead CRM."""
import copy
import json
import re

from .app import classify_for_config
from .core import TOPICS, Lead, message, topics, budget
from .billing import PLAN_NAMES, days_left, subscription_active, support_contact
from .drafts import reply_drafts

MENU={'keyboard':[
    [{'text':'▶️ Начать'},{'text':'⏸ Пауза'}],
    [{'text':'📥 Лиды'},{'text':'🗂 Воронка'}],
    [{'text':'🎯 Услуги'},{'text':'💰 Фильтры'}],
    [{'text':'➕ Добавить чат'},{'text':'🔌 Аккаунт'}],
    [{'text':'🧰 Портфолио'},{'text':'📈 Статистика'}],
    [{'text':'💳 Подписка'}]],
    'resize_keyboard':True,'is_persistent':True}

REGISTER={'inline_keyboard':[[{'text':'Зарегистрироваться','callback_data':'register:confirm'}]]}
PUBLIC_CHAT=re.compile(r'^(?:https?://t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,})/?$')
PIPELINE={'saved':'Новый / сохранён','viewed':'Просмотрен','working':'В работе','contacted':'Написали',
          'discussing':'Получен ответ','meeting':'Назначена встреча','proposal':'Отправлено предложение','won':'Получил заказ',
          'lost':'Не подошёл','not_fit':'Не подходит'}
FEEDBACK={'fit':'Подходит','ad':'Реклама','job':'Ищет работу','not_service':'Не моя услуга',
          'too_cold':'Слишком холодный','wrong_geo':'Неверная география','competitor':'Конкурент',
          'vacancy':'Вакансия','duplicate':'Дубль'}


def amount(text,maximum):
    """Whole rubles within a column's range; anything else is not an amount."""
    value=text.replace(' ','').replace('\u00a0','')
    if not (value.isascii() and value.isdigit() and len(value)<=10):return None
    return int(value) if int(value)<=maximum else None


def lead_buttons(lead_id,url):
    keys={'inline_keyboard':[
        [{'text':'Открыть оригинал','url':url},{'text':'Подготовить отклик','callback_data':f'reply:{lead_id}'}],
        [{'text':'✅ Подходит','callback_data':f'feedback:fit:{lead_id}'},
         {'text':'📢 Реклама','callback_data':f'feedback:ad:{lead_id}'}],
        [{'text':'💼 Ищет работу','callback_data':f'feedback:job:{lead_id}'},
         {'text':'🚫 Не моя услуга','callback_data':f'feedback:not_service:{lead_id}'}],
        [{'text':'Изменить статус','callback_data':f'pipeline:menu:{lead_id}'},
         {'text':'Напомнить через час','callback_data':f'remind:{lead_id}'}]]}
    from .telegram_replies import source_message
    try:source_message(url)
    except ValueError:pass
    else:keys['inline_keyboard'].insert(1,[{'text':'✉️ Ответить через Telegram','callback_data':f'tgcompose:{lead_id}'}])
    return keys


def user_config(base,row):
    result=copy.deepcopy(base)
    result['min_budget']=row['min_budget']
    result['topics']=row['topics'] if isinstance(row['topics'],list) else json.loads(row['topics'])
    result['notify_without_budget']=False
    switches=row['source_switches'] if isinstance(row['source_switches'],dict) else json.loads(row['source_switches'])
    for source in result['sources']:
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


def deliver_registered(db,base,sender,limit_per_user=1):
    users=db.execute('''SELECT u.id,u.telegram_chat_id,u.registered_at,p.* FROM app_users u
        JOIN user_preferences p ON p.user_id=u.id
        JOIN subscriptions s ON s.user_id=u.id
        WHERE u.status='active' AND p.monitoring_active=true
          AND s.status IN ('stub_active','trial','active')
          AND (s.ends_at IS NULL OR s.ends_at>now()) ORDER BY u.id''').fetchall()
    delivered=0
    for prefs in users:
        config=user_config(base,prefs);processed=0
        projects=db.execute('SELECT * FROM projects WHERE user_id=? ORDER BY id',(prefs['id'],)).fetchall()
        rows=db.execute('''SELECT l.id,l.payload FROM leads l
            WHERE l.status<>'duplicate' AND (l.origin_user_id IS NULL OR l.origin_user_id=?)
              AND NOT EXISTS (SELECT 1 FROM user_leads ul WHERE ul.user_id=? AND ul.lead_id=l.id)
              AND NOT EXISTS (SELECT 1 FROM user_leads seen JOIN leads original ON original.id=seen.lead_id
                  WHERE seen.user_id=? AND seen.delivery_status<>'filtered' AND original.fingerprint=l.fingerprint)
              AND l.first_seen::timestamptz >= ?
            ORDER BY l.id DESC LIMIT 100''',(prefs['id'],prefs['id'],prefs['id'],prefs['registered_at'])).fetchall()
        for row in rows:
            lead=Lead(**json.loads(row['payload']))
            allowed,reason=eligible_for_user(lead,config,prefs)
            matches=[]
            if projects:
                from .projects import project_eligibility
                for project in projects:
                    match,why=project_eligibility(lead,config,project)
                    if match:matches.append(project['id']);reason=why
                allowed=bool(matches)
                if not allowed:reason='не соответствует фильтрам проектов'
            if not allowed:
                with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason)
                    VALUES(?,?,'filtered',?) ON CONFLICT(user_id,lead_id) DO NOTHING''',(prefs['id'],row['id'],reason))
                continue
            if matches:
                with db:
                    for project_id in matches:
                        db.execute('''INSERT INTO project_leads(project_id,user_id,lead_id) VALUES(?,?,?)
                            ON CONFLICT(project_id,lead_id) DO NOTHING''',(project_id,prefs['id'],row['id']))
            try:
                sender('sendMessage',{'chat_id':str(prefs['telegram_chat_id']),'text':message(lead,reason),
                    'link_preview_options':{'is_disabled':True},'reply_markup':lead_buttons(row['id'],lead.url)})
            except RuntimeError as exc:
                with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason,updated_at)
                    VALUES(?,?,'uncertain',?,now()) ON CONFLICT(user_id,lead_id) DO UPDATE SET
                    delivery_status='uncertain',filter_reason=excluded.filter_reason,updated_at=now()''',
                    (prefs['id'],row['id'],str(exc)))
                break
            else:
                with db:db.execute('''INSERT INTO user_leads(user_id,lead_id,delivery_status,filter_reason,sent_at,updated_at)
                    VALUES(?,?,'sent',?,now(),now()) ON CONFLICT(user_id,lead_id) DO UPDATE SET
                    delivery_status='sent',filter_reason=excluded.filter_reason,sent_at=now(),updated_at=now()''',
                    (prefs['id'],row['id'],reason))
                with db:
                    from .integrations import enqueue_lead_event
                    enqueue_lead_event(db,prefs['id'],row['id'],'lead.created')
                delivered+=1;processed+=1
                if processed>=limit_per_user:break
    return delivered


class ProductController:
    def __init__(self,db,base,sender):
        self.db,self.base,self.sender=db,base,sender
        self.manual=False;self.drain=False;self.last_chat=None

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
        return self.sender('sendMessage',{'chat_id':target,'text':text,'reply_markup':markup or MENU})

    def _user(self,telegram_id):
        return self.db.execute('''SELECT u.*,p.monitoring_active,p.min_budget,p.show_without_budget,
            p.show_possible_needs,p.topics,p.source_switches,p.profile_services,p.portfolio,p.state,
            s.status AS subscription_status,s.plan_code,s.ends_at
            FROM app_users u JOIN user_preferences p ON p.user_id=u.id
            JOIN subscriptions s ON s.user_id=u.id WHERE u.telegram_user_id=?''',(telegram_id,)).fetchone()

    def _register(self,actor,chat_id):
        name=' '.join(x for x in (actor.get('first_name',''),actor.get('last_name','')) if x).strip()
        with self.db:
            row=self.db.execute('''INSERT INTO app_users(telegram_user_id,telegram_chat_id,username,display_name)
                VALUES(?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET telegram_chat_id=excluded.telegram_chat_id,
                username=excluded.username,display_name=excluded.display_name,last_seen_at=now() RETURNING id''',
                (actor['id'],chat_id,actor.get('username'),name)).fetchone()
            self.db.execute("INSERT INTO subscriptions(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",(row['id'],))
            self.db.execute("INSERT INTO user_preferences(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",(row['id'],))
        return self._user(actor['id'])

    def _update_pref(self,user_id,column,value):
        allowed={'monitoring_active','min_budget','show_without_budget','show_possible_needs','topics',
                 'profile_services','portfolio','state','source_switches'}
        if column not in allowed:raise ValueError('unknown preference')
        with self.db:
            self.db.execute(f'UPDATE user_preferences SET {column}=?,updated_at=now() WHERE user_id=?',(value,user_id))
            self.db.execute("DELETE FROM user_leads WHERE user_id=? AND delivery_status='filtered'",(user_id,))

    def _filters(self,user):
        keys={'inline_keyboard':[
            [{'text':('✅ ' if user['show_without_budget'] else '⬜️ ')+'Без бюджета','callback_data':'toggle:no_budget'}],
            [{'text':('✅ ' if user['show_possible_needs'] else '⬜️ ')+'Возможные потребности','callback_data':'toggle:possible'}],
            [{'text':f"Минимум: {user['min_budget']} ₽",'callback_data':'budget:custom'}]]}
        self.say('Настройте поток лидов. Неизвестный бюджет остаётся неизвестным и не заменяется выдуманной суммой.',keys)

    def _services(self,user):
        selected=user['topics'] if isinstance(user['topics'],list) else json.loads(user['topics'])
        buttons=[{'text':('✅ ' if name in selected else '⬜️ ')+name,
            'callback_data':f'topic:{i}'} for i,name in enumerate(TOPICS)]
        keys={'inline_keyboard':[buttons[i:i+2] for i in range(0,len(buttons),2)]}
        self.say('Какие услуги вы оказываете?',keys)

    def _lead(self,lead_id):
        row=self.db.execute('''SELECT l.id,l.payload,l.reason FROM leads l JOIN user_leads ul ON ul.lead_id=l.id
            JOIN app_users u ON u.id=ul.user_id WHERE l.id=? AND u.telegram_chat_id=?
            AND ul.delivery_status<>'filtered' ''',(lead_id,int(self.last_chat))).fetchone()
        return (row,Lead(**json.loads(row['payload']))) if row else (None,None)

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
        tail=f'\n\nПродлить или сменить тариф — в веб-кабинете в разделе «Подписка» или у поддержки: @{contact}' if contact else '\n\nПродлить или сменить тариф можно в веб-кабинете, раздел «Подписка».'
        return line+tail

    def _reply_services(self):
        import os
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

    def _stats(self,user_id):
        feedback={r['label']:r['total'] for r in self.db.execute(
            'SELECT label,count(*) AS total FROM lead_feedback WHERE user_id=? GROUP BY label',(user_id,))}
        pipeline={r['pipeline_status']:r['total'] for r in self.db.execute(
            'SELECT pipeline_status,count(*) AS total FROM user_leads WHERE user_id=? GROUP BY pipeline_status',(user_id,))}
        money=self.db.execute("SELECT coalesce(sum(deal_amount),0) AS total FROM user_leads WHERE user_id=? AND pipeline_status='won'",(user_id,)).fetchone()['total']
        sources=self.db.execute('''SELECT l.payload::jsonb->>'source' AS source,count(*) AS found,
            count(*) FILTER (WHERE f.label='fit') AS fit
            FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
            LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
            WHERE ul.user_id=? AND ul.delivery_status='sent'
            GROUP BY l.payload::jsonb->>'source' ORDER BY found DESC LIMIT 5''',(user_id,)).fetchall()
        lines=['Качество отбора:']+[f'• {FEEDBACK[k]}: {feedback.get(k,0)}' for k in FEEDBACK]
        lines+=['','Воронка:']+[f'• {PIPELINE[k]}: {pipeline.get(k,0)}' for k in PIPELINE]
        lines.append(f'• Сумма выигранных сделок: {money:,} ₽'.replace(',',' '))
        if sources:
            lines+=['','Качество источников:']+[f"• {r['source']}: найдено {r['found']}, подходит {r['fit']}" for r in sources]
        self.say('\n'.join(lines))

    def handle(self,update):
        callback=update.get('callback_query');msg=(callback or {}).get('message') if callback else update.get('message')
        actor=(callback or msg or {}).get('from',{});chat=(msg or {}).get('chat',{})
        if chat.get('type')!='private' or str(chat.get('id'))!=str(actor.get('id')):return
        chat_id=str(chat['id']);self.last_chat=chat_id
        user=self._user(actor['id'])
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
                    self.sender('answerCallbackQuery',{'callback_query_id':callback['id']})
                    if not user:user=self._register(actor,chat['id'])
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
            if callback and callback.get('data')=='register:confirm':
                self.sender('answerCallbackQuery',{'callback_query_id':callback['id']});user=self._register(actor,chat['id'])
                self.say('Регистрация завершена. Пробный период — 7 дней, без привязки карты. Настройте услуги и включите мониторинг.',chat_id=chat_id)
            else:self.say('Сигналид находит прямые заказы и возможные потребности. Для начала зарегистрируйтесь.',REGISTER,chat_id)
            return
        with self.db:self.db.execute('UPDATE app_users SET last_seen_at=now() WHERE id=?',(user['id'],))
        if user['status']=='blocked':
            self.say('Аккаунт заблокирован. Обратитесь в поддержку.');return
        if not subscription_active({'status':user['subscription_status'],'ends_at':user['ends_at']}) and text!='💳 Подписка':
            self.say('Доступ приостановлен. Откройте раздел «Подписка».');return
        if callback:
            self.sender('answerCallbackQuery',{'callback_query_id':callback['id']})
            parts=callback.get('data','').split(':');action=parts[0]
            if self._reply_callback(user,parts):return
            if action=='topic' and len(parts)==2 and parts[1].isdigit() and int(parts[1])<len(TOPICS):
                selected=user['topics'] if isinstance(user['topics'],list) else json.loads(user['topics']);name=list(TOPICS)[int(parts[1])]
                selected.remove(name) if name in selected else selected.append(name);self._update_pref(user['id'],'topics',json.dumps(selected));self._services(self._user(actor['id']))
            elif action=='toggle' and len(parts)==2 and parts[1]=='no_budget':self._update_pref(user['id'],'show_without_budget',not user['show_without_budget']);self._filters(self._user(actor['id']))
            elif action=='toggle' and len(parts)==2 and parts[1]=='possible':self._update_pref(user['id'],'show_possible_needs',not user['show_possible_needs']);self._filters(self._user(actor['id']))
            elif action=='budget':self._update_pref(user['id'],'state',json.dumps({'await':'budget'}));self.say('Введите минимальный бюджет числом в рублях. /cancel — отменить.')
            elif action=='feedback' and len(parts)==3 and parts[1] in FEEDBACK and parts[2].isdigit():
                row,_=self._lead(int(parts[2]))
                if not row:self.say('Лид не найден.');return
                with self.db:self.db.execute('''INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,?)
                    ON CONFLICT(user_id,lead_id) DO UPDATE SET label=excluded.label,updated_at=now()''',(user['id'],int(parts[2]),parts[1]))
                self.say('Оценка сохранена. Она попадёт в статистику качества и поможет улучшать правила отбора.')
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
                from datetime import datetime,timedelta,timezone
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
                self.say(self._subscription_text(user))
            return
        state=user['state'] if isinstance(user['state'],dict) else json.loads(user['state'])
        if text=='/cancel':
            if state.get('await')=='telegram_reply_confirm':
                from .telegram_replies import cancel
                try:cancel(self.db,state['reply_id'],user['id'],user['id'])
                except ValueError:pass
            self._update_pref(user['id'],'state',json.dumps({}));self.say('Ввод отменён.');return
        if state.get('await')=='telegram_reply':self._reply_text(user,state,text);return
        if state.get('await')=='telegram_reply_confirm':
            self.say('Нажмите «Подтвердить и отправить» под откликом или /cancel для отмены и редактирования.');return
        if state.get('await')=='budget' and amount(text,100000000) is not None:
            value=amount(text,100000000);self._update_pref(user['id'],'min_budget',value);self._update_pref(user['id'],'state',json.dumps({}));self.say(f'Минимальный бюджет: {value:,} ₽'.replace(',',' '));return
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
        if text in ('/start','/menu'):self.say('Аккаунт зарегистрирован. Настройте услуги и фильтры, затем включите мониторинг.')
        elif text=='▶️ Начать':self._update_pref(user['id'],'monitoring_active',True);self.manual=True;self.say('Мониторинг включён.')
        elif text=='⏸ Пауза':self._update_pref(user['id'],'monitoring_active',False);self.say('Мониторинг приостановлен.')
        elif text=='🎯 Услуги':self._services(user)
        elif text=='💰 Фильтры':self._filters(user)
        elif text=='📈 Статистика':self._stats(user['id'])
        elif text=='🗂 Воронка':self._stats(user['id'])
        elif text=='➕ Добавить чат':self._update_pref(user['id'],'state',json.dumps({'await':'chat'}));self.say('Отправьте публичную ссылку на группу или её @username.')
        elif text=='🔌 Аккаунт':self.say('Подключите свой Telegram-аккаунт в веб-кабинете, раздел «Telegram». После подключения можно отвечать на объявления с сайта и кнопкой «Ответить через Telegram» под лидом. Не отправляйте боту коды входа и пароли.')
        elif text=='🧰 Портфолио':self._update_pref(user['id'],'state',json.dumps({'await':'portfolio'}));self.say('Отправьте краткое описание услуг и 1–3 примера работ. Они будут использованы в черновике отклика.')
        elif text=='💳 Подписка':self.say(self._subscription_text(user))
        elif text=='📥 Лиды':self.say('Новые подходящие лиды приходят автоматически. Используйте кнопки под карточками для оценки и изменения статуса.')
        else:self.say('Выберите действие в меню.')
