#!/usr/bin/env python3
"""One-time, repeatable SQLite -> Supabase Postgres data migration."""
import argparse
import json
import sqlite3
from pathlib import Path

TABLES={
    'leads':(
        ['url','fingerprint','payload','status','reason','first_seen','sent_at','attempts','next_attempt'],
        'url text,fingerprint text,payload text,status text,reason text,first_seen text,sent_at text,attempts integer,next_attempt double precision',
        'url'),
    'health':(
        ['source','checked','ok','count','error'],
        'source text,checked text,ok integer,count integer,error text',
        'source'),
    'telegram_sources':(
        ['username','url','title','segment','priority','selection_group','original_members','members','online','status','enabled','checked_at','last_message_at','check_reason','last_message_id','metadata'],
        'username text,url text,title text,segment text,priority integer,selection_group text,original_members integer,members integer,online integer,status text,enabled integer,checked_at text,last_message_at text,check_reason text,last_message_id bigint,metadata text',
        'username'),
    'settings':(['key','value'],'key text,value text','key'),
}


def batch_sql(source,table,offset,limit):
    columns,definition,key=TABLES[table]
    db=sqlite3.connect(source);db.row_factory=sqlite3.Row
    rows=[dict(row) for row in db.execute(
        f"SELECT {','.join(columns)} FROM {table} ORDER BY {key} LIMIT ? OFFSET ?",(limit,offset))]
    db.close()
    if table=='telegram_sources':
        # Local workbook row references are audit details, not runtime data.
        for row in rows:
            try:
                metadata=json.loads(row['metadata'] or '{}')
                metadata.pop('source_rows',None)
                row['metadata']=json.dumps(metadata,ensure_ascii=False,separators=(',',':'))
            except (TypeError,json.JSONDecodeError):
                row['metadata']='{}'
    if not rows:return ''
    payload=json.dumps(rows,ensure_ascii=False,separators=(',',':')).replace("'","''")
    updates=','.join(f'{column}=excluded.{column}' for column in columns if column!=key)
    return f'''insert into leadgen.{table} ({','.join(columns)})
select {','.join(columns)} from jsonb_to_recordset('{payload}'::jsonb) as x({definition})
on conflict ({key}) do update set {updates};'''


def migrate(source,dsn,batch_size=500):
    try:
        import psycopg
    except ImportError:
        raise SystemExit('Install requirements.txt first')
    with psycopg.connect(dsn,options='-c search_path=leadgen,public') as target:
        for table in TABLES:
            offset=0
            while sql:=batch_sql(source,table,offset,batch_size):
                target.execute(sql);target.commit();offset+=batch_size
                print(f'{table}: {offset}',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=Path('leadgen/data/leads.sqlite'))
    parser.add_argument('--dsn')
    parser.add_argument('--emit-batch',choices=TABLES)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--limit',type=int,default=500)
    args=parser.parse_args()
    if args.emit_batch:
        print(batch_sql(args.source,args.emit_batch,args.offset,args.limit),end='')
    elif args.dsn:
        migrate(args.source,args.dsn,args.limit)
    else:
        parser.error('--dsn or --emit-batch is required')


if __name__=='__main__':main()
