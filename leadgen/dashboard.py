"""Cabinet dashboard report: every lead metric from one scan of the owner's leads for the period."""

SOURCES_LIMIT=20

# The owner's delivered leads of the period are read once (MATERIALIZED) and every block aggregates
# that set. lead_feedback has one row per (user_id, lead_id), so the LEFT JOIN never duplicates leads.
# Each branch numbers its rows (ord) in the order the page shows them; the columns a branch does not use are NULL.
REPORT_SQL=f"""WITH base AS MATERIALIZED (
    SELECT ul.user_id,ul.lead_id,ul.pipeline_status,ul.deal_amount,ul.sent_at,l.source,
        (l.first_seen AT TIME ZONE 'UTC')::date AS day,f.label
    FROM user_leads ul JOIN leads l ON l.id=ul.lead_id
    LEFT JOIN lead_feedback f ON f.user_id=ul.user_id AND f.lead_id=ul.lead_id
    WHERE ul.user_id=? AND ul.delivery_status<>'filtered' AND ul.updated_at IS NOT NULL
        AND l.first_seen >= now()-(? * interval '1 day'))
SELECT 'stats' AS kind,1::bigint AS ord,NULL::text AS key,NULL::date AS day,count(*) AS count,
    count(*) FILTER (WHERE pipeline_status='won') AS won,
    count(*) FILTER (WHERE label='fit') AS fit,
    NULL::bigint AS reviewed,NULL::bigint AS ads,
    sum(deal_amount) FILTER (WHERE pipeline_status='won') AS revenue,
    NULL::numeric AS minutes,NULL::bigint AS samples
FROM base
UNION ALL
SELECT 'stage',row_number() OVER (),pipeline_status,NULL,count(*),NULL,NULL,NULL,NULL,NULL,NULL,NULL
FROM base GROUP BY pipeline_status
UNION ALL
SELECT 'source',ord,name,NULL,count,won,fit,reviewed,ads,NULL,NULL,NULL FROM (
    SELECT source AS name,count(*) AS count,count(label) AS reviewed,
        count(*) FILTER (WHERE label='fit') AS fit,count(*) FILTER (WHERE label='ad') AS ads,
        count(*) FILTER (WHERE pipeline_status='won') AS won,
        row_number() OVER (ORDER BY count(*) FILTER (WHERE pipeline_status='won') DESC,
            count(*) FILTER (WHERE label='fit') DESC,count(*) DESC) AS ord
    FROM base GROUP BY source) sources
WHERE ord<={SOURCES_LIMIT}
UNION ALL
SELECT 'day',row_number() OVER (ORDER BY day),NULL,day,count(*),NULL,NULL,NULL,NULL,NULL,NULL,NULL
FROM base GROUP BY day
UNION ALL
SELECT 'quality',row_number() OVER (ORDER BY count(*) DESC),label,NULL,count(*),NULL,NULL,NULL,NULL,NULL,NULL,NULL
FROM base WHERE label IS NOT NULL GROUP BY label
UNION ALL
SELECT 'response',1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
    avg(extract(epoch FROM (a.first_response-b.sent_at)))/60,count(a.first_response)
FROM base b JOIN LATERAL (SELECT min(created_at) AS first_response FROM lead_activity
    WHERE user_id=b.user_id AND lead_id=b.lead_id AND kind='status' AND detail='Написали') a ON true
WHERE b.sent_at IS NOT NULL AND a.first_response>=b.sent_at
UNION ALL
SELECT 'project',row_number() OVER (ORDER BY count(*) DESC),p.name,NULL,count(*),
    count(*) FILTER (WHERE b.pipeline_status='won'),NULL,NULL,NULL,NULL,NULL,NULL
FROM base b JOIN project_leads pl ON pl.user_id=b.user_id AND pl.lead_id=b.lead_id
JOIN projects p ON p.id=pl.project_id AND p.user_id=pl.user_id
GROUP BY p.id
ORDER BY kind,ord"""


def dashboard_report(db,owner_id,days):
    """Totals, funnel, sources, daily counts, feedback quality, first-response time and projects
    of the owner's leads first seen in the last `days` days, as the dashboard template expects them."""
    rows=db.execute(REPORT_SQL,(owner_id,days)).fetchall()
    by_kind={}
    for row in rows:by_kind.setdefault(row['kind'],[]).append(row)
    stats=by_kind['stats'][0];response=by_kind['response'][0]
    return {
        'stats':{'total':stats['count'],'won':stats['won'],'fit':stats['fit'],'revenue':stats['revenue']},
        'stages':[{'pipeline_status':r['key'],'count':r['count']} for r in by_kind.get('stage',[])],
        'sources':[{'name':r['key'],'count':r['count'],'reviewed':r['reviewed'],'fit':r['fit'],'ads':r['ads'],'won':r['won']}
                   for r in by_kind.get('source',[])],
        'daily':[{'day':r['day'],'count':r['count']} for r in by_kind.get('day',[])],
        'quality':[{'label':r['key'],'count':r['count']} for r in by_kind.get('quality',[])],
        'response':{'minutes':response['minutes'],'samples':response['samples']},
        'projects':[{'name':r['key'],'count':r['count'],'won':r['won']} for r in by_kind.get('project',[])],
    }
