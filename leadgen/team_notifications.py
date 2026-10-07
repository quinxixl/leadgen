"""Durable Telegram notification for a lead assignment."""
import json


def deliver_assignments(db,sender,limit=5):
    delivered=0
    with db:
        db.execute("""UPDATE lead_assignments SET notification_status='uncertain',
            notification_error='Предыдущая отправка прервана',notification_updated_at=now()
            WHERE notification_status='sending' AND notification_updated_at<now()-interval '5 minutes'""")
    for _ in range(limit):
        with db:
            row=db.execute('''SELECT a.*,u.telegram_chat_id,w.name AS workspace_name,l.payload
                FROM lead_assignments a JOIN app_users u ON u.id=a.assignee_user_id
                JOIN workspaces w ON w.id=a.workspace_id JOIN leads l ON l.id=a.lead_id
                WHERE a.notification_status='pending' AND u.status='active'
                ORDER BY a.notification_updated_at FOR UPDATE OF a SKIP LOCKED LIMIT 1''').fetchone()
            if not row:break
            db.execute("""UPDATE lead_assignments SET notification_status='sending',notification_updated_at=now()
                WHERE workspace_id=? AND lead_id=?""",(row['workspace_id'],row['lead_id']))
        lead=json.loads(row['payload'])
        text=(f"Вам назначен лид в команде «{row['workspace_name']}».\n\n"
              f"{lead['title']}\n{lead['source']}\n\n{lead['url']}")[:3900]
        payload={'chat_id':str(row['telegram_chat_id']),'text':text,'disable_web_page_preview':True}
        from .product import lead_link
        button=lead_link(row['lead_id'])
        if button:payload['reply_markup']={'inline_keyboard':[[button]]}
        try:sender('sendMessage',payload)
        except Exception as exc:
            with db:db.execute("""UPDATE lead_assignments SET notification_status='uncertain',
                notification_error=?,notification_updated_at=now() WHERE workspace_id=? AND lead_id=?""",
                (type(exc).__name__,row['workspace_id'],row['lead_id']))
        else:
            with db:db.execute("""UPDATE lead_assignments SET notification_status='sent',
                notification_error='',notification_updated_at=now() WHERE workspace_id=? AND lead_id=?""",
                (row['workspace_id'],row['lead_id']))
            delivered+=1
    return delivered
