"""Demand report derived only from a workspace's actual leads."""
import json
from collections import Counter
from datetime import datetime

from flask import abort, g, render_template, request

from .core import Lead, enrich


WEEKDAYS=('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')


def register_demand(app,db):
    @app.get('/app/demand')
    def demand_report():
        try:days=int(request.args.get('days','30'))
        except ValueError:abort(400)
        if days not in (7,30,90):abort(400)
        rows=db().execute('''SELECT l.payload FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
            WHERE ul.user_id=? AND ul.delivery_status<>'filtered'
            AND l.first_seen::timestamptz>=now()-(? * interval '1 day') ORDER BY l.id DESC LIMIT 10000''',
            (g.owner_id,days)).fetchall()
        topics=Counter();temperatures=Counter();weekdays=Counter();hours=Counter();direct=0;possible=0
        for row in rows:
            lead=enrich(Lead(**json.loads(row['payload'])))
            if 'Направление: ' in lead.summary:
                topics.update(lead.summary.split('Направление: ',1)[1].split('\n',1)[0].split(', '))
            temperatures[lead.temperature]+=1
            if lead.intent=='possible':possible+=1
            elif lead.intent=='direct':direct+=1
            try:
                moment=datetime.fromisoformat(lead.published)
                weekdays[moment.weekday()]+=1;hours[moment.hour]+=1
            except (TypeError,ValueError):pass
        topic_rows=[{'name':name,'count':count} for name,count in topics.most_common() if name!='не определено']
        weekday_rows=[{'name':WEEKDAYS[index],'count':weekdays[index]} for index in range(7)]
        hour_rows=[{'name':f'{hour:02d}:00–{hour:02d}:59 UTC','count':hours[hour]} for hour in range(24) if hours[hour]]
        return render_template('demand.html',days=days,total=len(rows),topics=topic_rows,
            temperatures=temperatures,weekdays=weekday_rows,hours=hour_rows,direct=direct,possible=possible,
            peak_topic=max((row['count'] for row in topic_rows),default=1),
            peak_day=max((row['count'] for row in weekday_rows),default=1),
            peak_hour=max((row['count'] for row in hour_rows),default=1))
