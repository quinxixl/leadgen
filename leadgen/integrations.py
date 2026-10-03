"""Workspace API tokens and signed HTTPS webhook delivery."""
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import secrets
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

from flask import abort, current_app, flash, g, jsonify, redirect, render_template, request, session

from .core import Lead, enrich
from .product import FEEDBACK, PIPELINE
from .teams import EDIT_SETTINGS, require_role
from .telegram_accounts import SessionCipher, TelegramAccountError


def token_hash(token):return hashlib.sha256(token.encode()).hexdigest()


def resolve_public(hostname, resolver=socket.getaddrinfo):
    try:
        records=resolver(hostname,443,type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('Не удалось определить адрес webhook-сервера.') from None
    addresses=[]
    for record in records:
        address=record[4][0]
        try:ip=ipaddress.ip_address(address.split('%',1)[0])
        except ValueError:continue
        # ::ffff:127.0.0.1 must be judged as the IPv4 address it maps to.
        if ip.version==6 and ip.ipv4_mapped:ip=ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise ValueError('Webhook не может вести на локальный или служебный адрес.')
        addresses.append(address)
    if not addresses:raise ValueError('У webhook-сервера нет доступного публичного адреса.')
    return addresses[0]


def validate_webhook_url(url, resolver=socket.getaddrinfo):
    parts=urlsplit((url or '').strip())
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError('Webhook должен быть полным HTTPS-адресом без логина и фрагмента.')
    try:port=parts.port
    except ValueError:raise ValueError('Некорректный порт webhook.') from None
    if port not in (None,443):raise ValueError('Webhook поддерживает только стандартный HTTPS-порт 443.')
    resolve_public(parts.hostname,resolver)
    return parts.geturl()


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self,hostname,address,timeout=10):
        super().__init__(hostname,443,timeout=timeout,context=ssl.create_default_context());self.address=address
    def connect(self):
        sock=socket.create_connection((self.address,443),self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


def send_webhook(url,body,headers,resolver=socket.getaddrinfo):
    parts=urlsplit(url);address=resolve_public(parts.hostname,resolver)
    path=urlunsplit(('', '', parts.path or '/', parts.query, ''))
    connection=PinnedHTTPSConnection(parts.hostname,address)
    try:
        connection.request('POST',path,body=body,headers=headers)
        response=connection.getresponse();response.read(1024)
        if not 200<=response.status<300:raise RuntimeError('HTTP '+str(response.status))
        return response.status
    finally:connection.close()


def enqueue_lead_event(db,owner_user_id,lead_id,event_type='lead.updated'):
    row=db.execute('''SELECT w.id AS workspace_id,l.payload,ul.pipeline_status,ul.deal_amount,
        f.label AS feedback,a.assignee_user_id,
        coalesce((SELECT jsonb_agg(t.tag ORDER BY t.tag) FROM lead_tags t
            WHERE t.user_id=ul.user_id AND t.lead_id=ul.lead_id),'[]'::jsonb) AS tags,
        (SELECT jsonb_build_object('kind',activity.kind,'detail',activity.detail,
                'created_at',activity.created_at)
            FROM lead_activity activity WHERE activity.user_id=ul.user_id
                AND activity.lead_id=ul.lead_id ORDER BY activity.id DESC LIMIT 1) AS last_activity
        FROM workspaces w JOIN user_leads ul ON ul.user_id=w.owner_user_id
        JOIN leads l ON l.id=ul.lead_id
        LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
        LEFT JOIN lead_assignments a ON a.workspace_id=w.id AND a.lead_id=ul.lead_id
        WHERE w.owner_user_id=? AND l.id=?''',
        (owner_user_id,lead_id)).fetchone()
    if not row:return 0
    lead=enrich(Lead(**json.loads(row['payload'])))
    payload={'event':event_type,'lead':{'id':lead_id,'title':lead.title,'text':lead.text,'source':lead.source,
        'url':lead.url,'published':lead.published,'score':lead.score,'temperature':lead.temperature,
        'summary':lead.summary,'pipeline_status':row['pipeline_status'],'deal_amount':row['deal_amount'],
        'feedback':row['feedback'],'tags':row['tags'],'assignee_user_id':row['assignee_user_id'],
        'last_activity':row['last_activity']}}
    suffix=str(lead_id) if event_type=='lead.created' else str(lead_id)+':'+secrets.token_hex(8)
    hooks=db.execute('SELECT id FROM webhooks WHERE workspace_id=? AND active=true',(row['workspace_id'],)).fetchall()
    for hook in hooks:
        db.execute('''INSERT INTO webhook_outbox(webhook_id,event_type,event_key,payload)
            VALUES(?,?,?,?::jsonb) ON CONFLICT(webhook_id,event_key) DO NOTHING''',
            (hook['id'],event_type,event_type+':'+suffix,json.dumps(payload,ensure_ascii=False)))
    return len(hooks)


def deliver_webhooks(db,cipher_key,sender=send_webhook,resolver=socket.getaddrinfo,limit=5):
    cipher=SessionCipher(cipher_key);delivered=0
    with db:db.execute("UPDATE webhook_outbox SET status='failed',last_error='Предыдущая отправка прервана' WHERE status='processing' AND next_attempt_at<now()-interval '5 minutes'")
    for _ in range(limit):
        with db:
            row=db.execute('''SELECT o.*,w.url,w.secret_cipher,w.active FROM webhook_outbox o
                JOIN webhooks w ON w.id=o.webhook_id WHERE o.status IN ('pending','failed')
                AND o.next_attempt_at<=now() ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1''').fetchone()
            if not row:break
            if not row['active']:
                db.execute("UPDATE webhook_outbox SET status='dead',last_error='Webhook отключён' WHERE id=?",(row['id'],));continue
            db.execute("UPDATE webhook_outbox SET status='processing',attempts=attempts+1,next_attempt_at=now() WHERE id=?",(row['id'],))
        body=json.dumps(row['payload'],ensure_ascii=False,separators=(',',':')).encode()
        secret=cipher.decrypt(row['secret_cipher'])['secret'].encode()
        signature='sha256='+hmac.new(secret,body,hashlib.sha256).hexdigest()
        try:sender(row['url'],body,{'Content-Type':'application/json','User-Agent':'Signalid-Webhook/1.0',
            'X-Signalid-Signature':signature,'X-Leadfinder-Signature':signature},resolver)
        except Exception as exc:
            attempts=row['attempts']+1;dead=attempts>=8;delay=min(3600,30*(2**min(attempts,6)))
            with db:db.execute("""UPDATE webhook_outbox SET status=?,last_error=?,next_attempt_at=now()+(? * interval '1 second')
                WHERE id=?""",('dead' if dead else 'failed',type(exc).__name__,delay,row['id']))
        else:
            with db:db.execute("UPDATE webhook_outbox SET status='sent',sent_at=now(),last_error='' WHERE id=?",(row['id'],))
            delivered+=1
    return delivered


def api_identity(db):
    header=request.headers.get('Authorization','')
    if not header.startswith('Bearer lf_'):abort(401)
    token=header[7:]
    if len(token)>100:abort(401)
    row=db.execute('''SELECT t.*,w.owner_user_id FROM api_tokens t JOIN workspaces w ON w.id=t.workspace_id
        WHERE t.token_hash=? AND t.revoked=false''',(token_hash(token),)).fetchone()
    if not row:abort(401)
    from .rate_limit import RateLimited,hit
    try:hit(db,'api',row['id'],120,60)
    except RateLimited:abort(429)
    with db:db.execute('UPDATE api_tokens SET last_used_at=now() WHERE id=?',(row['id'],))
    return row


def register_integrations(app,db):
    @app.get('/app/integrations')
    def integrations_page():
        require_role(*EDIT_SETTINGS)
        tokens=db().execute('SELECT id,name,access,last_used_at,created_at,revoked FROM api_tokens WHERE workspace_id=? ORDER BY id DESC',(g.workspace['id'],)).fetchall()
        hooks=db().execute('''SELECT w.id,w.name,w.url,w.active,w.created_at,
            count(o.id) FILTER(WHERE o.status='sent') AS sent,count(o.id) FILTER(WHERE o.status IN ('failed','dead')) AS failed
            FROM webhooks w LEFT JOIN webhook_outbox o ON o.webhook_id=w.id WHERE w.workspace_id=? GROUP BY w.id ORDER BY w.id DESC''',(g.workspace['id'],)).fetchall()
        return render_template('integrations.html',tokens=tokens,hooks=hooks,new_token=session.pop('new_api_token',None),
                               new_secret=session.pop('new_webhook_secret',None))

    @app.post('/app/integrations/tokens')
    def create_token():
        require_role(*EDIT_SETTINGS);name=request.form.get('name','').strip();access=request.form.get('access','')
        if not name or len(name)>100 or access not in ('read','write'):abort(400)
        raw='lf_'+secrets.token_urlsafe(32)
        with db():db().execute('INSERT INTO api_tokens(workspace_id,name,token_hash,access,created_by) VALUES(?,?,?,?,?)',
                               (g.workspace['id'],name,token_hash(raw),access,g.user['id']))
        session['new_api_token']=raw;flash('API-токен создан. Скопируйте его сейчас: повторно он не показывается.')
        return redirect('/app/integrations')

    @app.post('/app/integrations/tokens/<int:token_id>/revoke')
    def revoke_token(token_id):
        require_role(*EDIT_SETTINGS)
        with db():row=db().execute('UPDATE api_tokens SET revoked=true WHERE id=? AND workspace_id=? RETURNING id',(token_id,g.workspace['id'])).fetchone()
        if not row:abort(404)
        flash('API-токен отозван.');return redirect('/app/integrations')

    @app.post('/app/integrations/webhooks')
    def create_webhook():
        require_role(*EDIT_SETTINGS);name=request.form.get('name','').strip()
        if not name or len(name)>100:abort(400)
        try:
            resolver=current_app.config.get('WEBHOOK_RESOLVER',socket.getaddrinfo)
            url=validate_webhook_url(request.form.get('url',''),resolver)
            cipher=SessionCipher(current_app.config.get('TELEGRAM_CIPHER_KEY',''))
        except (ValueError,TelegramAccountError) as exc:
            flash(str(exc));return redirect('/app/integrations')
        secret='whsec_'+secrets.token_urlsafe(32)
        with db():db().execute('INSERT INTO webhooks(workspace_id,name,url,secret_cipher,created_by) VALUES(?,?,?,?,?)',
                               (g.workspace['id'],name,url,cipher.encrypt({'secret':secret}),g.user['id']))
        session['new_webhook_secret']=secret;flash('Webhook создан. Секрет подписи показывается один раз.')
        return redirect('/app/integrations')

    @app.post('/app/integrations/webhooks/<int:hook_id>/toggle')
    def toggle_webhook(hook_id):
        require_role(*EDIT_SETTINGS)
        with db():row=db().execute('''UPDATE webhooks SET active=not active,updated_at=now()
            WHERE id=? AND workspace_id=? RETURNING active''',(hook_id,g.workspace['id'])).fetchone()
        if not row:abort(404)
        flash('Webhook включён.' if row['active'] else 'Webhook выключен.');return redirect('/app/integrations')

    @app.get('/api/v1/leads')
    def api_leads():
        identity=api_identity(db())
        try:limit=max(1,min(100,int(request.args.get('limit','50'))));after=max(0,int(request.args.get('after','0')))
        except ValueError:abort(400)
        rows=db().execute('''SELECT l.id,l.payload,ul.pipeline_status,ul.deal_amount,f.label AS feedback,
            a.assignee_user_id,coalesce((SELECT jsonb_agg(t.tag ORDER BY t.tag) FROM lead_tags t
                WHERE t.user_id=ul.user_id AND t.lead_id=ul.lead_id),'[]'::jsonb) AS tags
            FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
            LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
            LEFT JOIN lead_assignments a ON a.workspace_id=? AND a.lead_id=ul.lead_id
            WHERE ul.user_id=? AND ul.delivery_status<>'filtered' AND l.id>?
            ORDER BY l.id LIMIT ?''',(identity['workspace_id'],identity['owner_user_id'],after,limit)).fetchall()
        result=[]
        for row in rows:
            lead=enrich(Lead(**json.loads(row['payload'])))
            result.append({'id':row['id'],'title':lead.title,'text':lead.text,'source':lead.source,'url':lead.url,
                           'published':lead.published,'score':lead.score,'temperature':lead.temperature,
                           'pipeline_status':row['pipeline_status'],'deal_amount':row['deal_amount'],
                           'feedback':row['feedback'],'tags':row['tags'],'assignee_user_id':row['assignee_user_id']})
        return jsonify({'data':result,'next_after':result[-1]['id'] if result else after})

    @app.patch('/api/v1/leads/<int:lead_id>')
    def api_update_lead(lead_id):
        identity=api_identity(db())
        if identity['access']!='write':abort(403)
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)-{'status','feedback','deal_amount'}:abort(400)
        status=data.get('status');feedback=data.get('feedback');amount=data.get('deal_amount')
        if status is not None and status not in PIPELINE:abort(400)
        if feedback is not None and feedback not in FEEDBACK:abort(400)
        if amount is not None and (not isinstance(amount,int) or isinstance(amount,bool) or not 0<=amount<=1000000000):abort(400)
        owner=identity['owner_user_id'];row=db().execute("SELECT * FROM user_leads WHERE user_id=? AND lead_id=? AND delivery_status<>'filtered'",(owner,lead_id)).fetchone()
        if not row:abort(404)
        with db():
            if status is not None:db().execute('UPDATE user_leads SET pipeline_status=?,updated_at=now() WHERE user_id=? AND lead_id=?',(status,owner,lead_id))
            if amount is not None:db().execute('UPDATE user_leads SET deal_amount=?,updated_at=now() WHERE user_id=? AND lead_id=?',(amount,owner,lead_id))
            if feedback is not None:db().execute('''INSERT INTO lead_feedback(user_id,lead_id,label) VALUES(?,?,?)
                ON CONFLICT(user_id,lead_id) DO UPDATE SET label=excluded.label,updated_at=now()''',(owner,lead_id,feedback))
            enqueue_lead_event(db(),owner,lead_id,'lead.updated')
        return jsonify({'updated':True,'id':lead_id})
