"""Demand report derived only from a workspace's actual leads."""
from collections import Counter
from datetime import datetime

from flask import abort, g, render_template, request

from .core import stored_lead


WEEKDAYS=('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')
REPORT_LIMIT=10000
TOPICS_PREFIX='Направление: '

# The newest REPORT_LIMIT delivered leads of the period, aggregated in Postgres over the fields ingest() stored in
# leads.payload (score, summary, intent are written already enriched). Only distinct publication times leave the
# database: Python parses them with datetime.fromisoformat exactly as before. Rows stored without a summary
# (older payloads) are counted under 'legacy' and enriched in Python, see legacy_payloads().
REPORT_SQL='''WITH recent AS (
    SELECT l.payload FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
    WHERE ul.user_id=? AND ul.delivery_status<>'filtered' AND l.first_seen>=now()-(? * interval '1 day')
    ORDER BY l.id DESC LIMIT ?),
facts AS (
    SELECT coalesce(payload->>'summary','')<>'' AND coalesce(payload->>'temperature','')<>'' AS enriched,
        payload->>'summary' AS summary,payload->>'temperature' AS temperature,
        payload->>'intent' AS intent,payload->>'published' AS published
    FROM recent)
SELECT 'total' AS kind,NULL AS name,count(*) AS count FROM facts
UNION ALL SELECT 'legacy',NULL,count(*) FROM facts WHERE NOT enriched
UNION ALL SELECT 'temperature',temperature,count(*) FROM facts WHERE enriched GROUP BY temperature
UNION ALL SELECT 'intent',intent,count(*) FROM facts WHERE enriched GROUP BY intent
UNION ALL SELECT 'topic',topic,count(*) FROM facts
    CROSS JOIN LATERAL unnest(string_to_array(split_part(
        substr(summary,strpos(summary,?)+length(?)),E'\\n',1),', ')) AS topic
    WHERE enriched AND strpos(summary,?)>0 GROUP BY topic
UNION ALL SELECT 'published',published,count(*) FROM facts WHERE published IS NOT NULL GROUP BY published'''

LEGACY_SQL='''SELECT payload FROM (
    SELECT l.payload FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
    WHERE ul.user_id=? AND ul.delivery_status<>'filtered' AND l.first_seen>=now()-(? * interval '1 day')
    ORDER BY l.id DESC LIMIT ?) recent
WHERE coalesce(payload->>'summary','')='' OR coalesce(payload->>'temperature','')=''
'''


def legacy_payloads(db,owner_id,days):
    return [row['payload'] for row in db.execute(LEGACY_SQL,(owner_id,days,REPORT_LIMIT)).fetchall()]


def summary_topics(summary):
    if TOPICS_PREFIX not in summary:return []
    return summary.split(TOPICS_PREFIX,1)[1].split('\n',1)[0].split(', ')


def demand_counts(db,owner_id,days):
    """total, topics, temperatures, intents, publication times (value -> count) for the report."""
    rows=db.execute(REPORT_SQL,(owner_id,days,REPORT_LIMIT,TOPICS_PREFIX,TOPICS_PREFIX,TOPICS_PREFIX)).fetchall()
    total=legacy=0;topics=Counter();temperatures=Counter();intents=Counter();published=Counter()
    buckets={'topic':topics,'temperature':temperatures,'intent':intents,'published':published}
    for row in rows:
        if row['kind']=='total':total=row['count']
        elif row['kind']=='legacy':legacy=row['count']
        else:buckets[row['kind']][row['name']]+=row['count']
    if legacy:
        for payload in legacy_payloads(db,owner_id,days):
            lead=stored_lead(payload)
            topics.update(summary_topics(lead.summary));temperatures[lead.temperature]+=1;intents[lead.intent]+=1
    return total,topics,temperatures,intents,published


def publication_counts(published):
    weekdays=Counter();hours=Counter()
    for value,count in published.items():
        try:moment=datetime.fromisoformat(value)
        except (TypeError,ValueError):continue
        weekdays[moment.weekday()]+=count;hours[moment.hour]+=count
    return weekdays,hours


def register_demand(app,db):
    @app.get('/app/demand')
    def demand_report():
        try:days=int(request.args.get('days','30'))
        except ValueError:abort(400)
        if days not in (7,30,90):abort(400)
        total,topics,temperatures,intents,published=demand_counts(db(),g.owner_id,days)
        weekdays,hours=publication_counts(published)
        topic_rows=[{'name':name,'count':count} for name,count in topics.most_common() if name!='не определено']
        weekday_rows=[{'name':WEEKDAYS[index],'count':weekdays[index]} for index in range(7)]
        hour_rows=[{'name':f'{hour:02d}:00–{hour:02d}:59 UTC','count':hours[hour]} for hour in range(24) if hours[hour]]
        return render_template('demand.html',days=days,total=total,topics=topic_rows,
            temperatures=temperatures,weekdays=weekday_rows,hours=hour_rows,
            direct=intents['direct'],possible=intents['possible'],
            peak_topic=max((row['count'] for row in topic_rows),default=1),
            peak_day=max((row['count'] for row in weekday_rows),default=1),
            peak_hour=max((row['count'] for row in hour_rows),default=1))
