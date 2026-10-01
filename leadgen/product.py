"""Multi-user product layer: registration, preferences, feedback and lead CRM."""
import copy
import json
import re

from .app import classify_for_config
from .core import TOPICS, Lead, message, topics, budget

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
FEEDBACK={'fit':'Подходит','ad':'Реклама','job':'Ищет работу','not_service':'Не моя услуга'}


def lead_buttons(lead_id,url):
    return {'inline_keyboard':[
        [{'text':'Открыть оригинал','url':url},{'text':'Подготовить отклик','callback_data':f'reply:{lead_id}'}],
        [{'text':'✅ Подходит','callback_data':f'feedback:fit:{lead_id}'},
         {'text':'📢 Реклама','callback_data':f'feedback:ad:{lead_id}'}],
        [{'text':'💼 Ищет работу','callback_data':f'feedback:job:{lead_id}'},
         {'text':'🚫 Не моя услуга','callback_data':f'feedback:not_service:{lead_id}'}],
        [{'text':'Изменить статус','callback_data':f'pipeline:menu:{lead_id}'},
         {'text':'Напомнить через час','callback_data':f'remind:{lead_id}'}]]}


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
          AND s.status IN ('stub_active','active') ORDER BY u.id''').fetchall()
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
            s.status AS subscription_status,s.plan_code
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
        keys={'inline_keyboard':[[{'text':('✅ ' if name in selected else '⬜️ ')+name,
            'callback_data':f'topic:{i}'}] for i,name in enumerate(TOPICS)]}
        self.say('Какие услуги вы оказываете?',keys)

    def _lead(self,lead_id):
        row=self.db.execute('''SELECT l.id,l.payload,l.reason FROM leads l JOIN user_leads ul ON ul.lead_id=l.id
            JOIN app_users u ON u.id=ul.user_id WHERE l.id=? AND u.telegram_chat_id=?
            AND ul.delivery_status<>'filtered' ''',(lead_id,int(self.last_chat))).fetchone()
        return (row,Lead(**json.loads(row['payload']))) if row else (None,None)

    def _draft(self,user,lead):
        service=', '.join(topics(lead.title+' '+lead.text)) or user['profile_services'] or 'разработке и автоматизации'
        proof=(' Из релевантного опыта: '+user['portfolio'].strip()+'.') if user['portfolio'].strip() else ''
        return (f'Здравствуйте! Увидел ваш запрос по направлению: {service}. '
                f'Могу уточнить текущий процесс и предложить решение с этапами, сроками и оценкой стоимости.{proof} '
                'Подскажите, какой результат для вас приоритетен и есть ли желаемый срок запуска?')[:3500]

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
            from .web_auth import login_record, approve_login
            token=callback['data'][6:] if callback else text[len('/start web_'):]
            try:
                if user and user['status']=='blocked':
                    self.say('Доступ заблокирован.');return
                record=login_record(self.db,token)
                if callback:
                    self.sender('answerCallbackQuery',{'callback_query_id':callback['id']})
                    if not user:user=self._register(actor,chat['id'])
                    approve_login(self.db,token,user['id'])
                    self.say('Вход подтверждён. Вернитесь на сайт и нажмите «Завершить вход».')
                else:
                    self.say('Вход в веб-кабинет. Код на сайте: '+record['code']+
                        '\nПодтверждайте только собственный запрос с таким же кодом. Доступ к чтению вашего Telegram не предоставляется.',
                        {'inline_keyboard':[[{'text':'Подтвердить мой вход','callback_data':'webok:'+token}]]})
            except ValueError as exc:self.say(str(exc))
            return
        if not user:
            if callback and callback.get('data')=='register:confirm':
                self.sender('answerCallbackQuery',{'callback_query_id':callback['id']});user=self._register(actor,chat['id'])
                self.say('Регистрация завершена. Бесплатный доступ активирован. Списаний нет.',chat_id=chat_id)
            else:self.say('IT Lead Finder находит прямые заказы и возможные потребности. Для начала зарегистрируйтесь.',REGISTER,chat_id)
            return
        with self.db:self.db.execute('UPDATE app_users SET last_seen_at=now() WHERE id=?',(user['id'],))
        if user['status']=='blocked':
            self.say('Аккаунт заблокирован. Обратитесь в поддержку.');return
        if user['subscription_status'] not in ('stub_active','active') and text!='💳 Подписка':
            self.say('Доступ приостановлен. Откройте раздел «Подписка».');return
        if callback:
            self.sender('answerCallbackQuery',{'callback_query_id':callback['id']})
            parts=callback.get('data','').split(':');action=parts[0]
            if action=='topic' and len(parts)==2 and parts[1].isdigit() and int(parts[1])<len(TOPICS):
                selected=user['topics'] if isinstance(user['topics'],list) else json.loads(user['topics']);name=list(TOPICS)[int(parts[1])]
                selected.remove(name) if name in selected else selected.append(name);self._update_pref(user['id'],'topics',json.dumps(selected));self._services(self._user(actor['id']))
            elif action=='toggle' and parts[1]=='no_budget':self._update_pref(user['id'],'show_without_budget',not user['show_without_budget']);self._filters(self._user(actor['id']))
            elif action=='toggle' and parts[1]=='possible':self._update_pref(user['id'],'show_possible_needs',not user['show_possible_needs']);self._filters(self._user(actor['id']))
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
                _,lead=self._lead(int(parts[1]));self.say('Черновик отклика:\n\n'+self._draft(user,lead) if lead else 'Лид не найден.')
            elif action=='subscribe':
                self.say('Оплата пока не подключена. Тестовая подписка остаётся активной; списаний не будет.')
            return
        state=user['state'] if isinstance(user['state'],dict) else json.loads(user['state'])
        if text=='/cancel':self._update_pref(user['id'],'state',json.dumps({}));self.say('Ввод отменён.');return
        if state.get('await')=='budget' and text.replace(' ','').isdigit():
            value=int(text.replace(' ',''));self._update_pref(user['id'],'min_budget',value);self._update_pref(user['id'],'state',json.dumps({}));self.say(f'Минимальный бюджет: {value:,} ₽'.replace(',',' '));return
        if state.get('await')=='deal_amount' and text.replace(' ','').isdigit():
            value=int(text.replace(' ',''));
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
        elif text=='🔌 Аккаунт':self.say('Аккаунт приложения подключён через Telegram. Realtime-монитор пока использует общий серверный аккаунт. Не отправляйте боту пароль, код входа или облачный пароль Telegram. Свои публичные группы добавляйте через «Добавить чат».')
        elif text=='🧰 Портфолио':self._update_pref(user['id'],'state',json.dumps({'await':'portfolio'}));self.say('Отправьте краткое описание услуг и 1–3 примера работ. Они будут использованы в черновике отклика.')
        elif text=='💳 Подписка':self.say('Бесплатный доступ. Приём платежей отключён, списаний нет.')
        elif text=='📥 Лиды':self.say('Новые подходящие лиды приходят автоматически. Используйте кнопки под карточками для оценки и изменения статуса.')
        else:self.say('Выберите действие в меню.')
