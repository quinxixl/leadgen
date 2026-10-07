"""Shared account lifecycle: registration and terms consent for the bot and the web cabinet."""


def register_user(db,actor,chat_id,accepted_terms=False):
    """Upsert a Telegram user with subscription and preferences rows; return app_users.id."""
    name=' '.join(x for x in (actor.get('first_name',''),actor.get('last_name','')) if x).strip()
    columns,values,terms='','',''
    if accepted_terms:
        columns,values=',terms_accepted_at',',now()'
        terms=',terms_accepted_at=coalesce(app_users.terms_accepted_at,now())'
    with db:
        row=db.execute(f'''INSERT INTO app_users(telegram_user_id,telegram_chat_id,username,display_name{columns})
            VALUES(?,?,?,?{values}) ON CONFLICT(telegram_user_id) DO UPDATE SET telegram_chat_id=excluded.telegram_chat_id,
            username=excluded.username,display_name=excluded.display_name,last_seen_at=now(){terms} RETURNING id''',
            (actor['id'],chat_id,actor.get('username'),name)).fetchone()
        db.execute("INSERT INTO subscriptions(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",(row['id'],))
        db.execute("INSERT INTO user_preferences(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",(row['id'],))
    return row['id']


def accept_terms(db,user_id):
    """Record consent once; a repeated acceptance keeps the original timestamp."""
    with db:db.execute('UPDATE app_users SET terms_accepted_at=coalesce(terms_accepted_at,now()) WHERE id=?',(user_id,))
