import sqlite3,json,os
from datetime import datetime,timezone
from pathlib import Path
from app.errors import DomainError
from app.work_orders import reconcile

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS work_orders (id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT "待检", dispatch_version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS wo_events (id INTEGER PRIMARY KEY AUTOINCREMENT, work_order TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL, at TEXT NOT NULL, dispatch_version INTEGER NOT NULL DEFAULT 0)')
    return db

def _now():return datetime.now(timezone.utc).isoformat()

def save(run_id, body):
    derived=body.get('work_orders',[])
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        existing=[_load_order(db,row) for row in db.execute('SELECT id FROM work_orders').fetchall()]
        orders,created,resolved=reconcile(existing,derived)
        for oid in created:
            order=next(o for o in orders if o['id']==oid)
            db.execute('INSERT INTO work_orders(id,body,status,dispatch_version,updated_at) VALUES (?,?,?,?,?)',
                (oid,json.dumps(order,ensure_ascii=False),order['status'],0,_now()))
            db.execute('INSERT INTO wo_events(work_order,event,message,at,dispatch_version) VALUES (?,?,?,?,0)',
                (oid,'created','整桶隔离形成待检工单，全部相关设备待检',_now()))
        for oid in resolved:
            order=next(o for o in orders if o['id']==oid)
            db.execute('UPDATE work_orders SET body=?,status=?,updated_at=? WHERE id=?',
                (json.dumps(order,ensure_ascii=False),order['status'],_now(),oid))
            db.execute('INSERT INTO wo_events(work_order,event,message,at,dispatch_version) VALUES (?,?,?,?,(SELECT dispatch_version FROM work_orders WHERE id=?))',
                (oid,'resolved','补录后相关时间桶重判通过',_now(),oid))
        for order in orders:
            if order['id'] not in created and order['id'] not in resolved:
                db.execute('UPDATE work_orders SET body=?,updated_at=? WHERE id=?',
                    (json.dumps(order,ensure_ascii=False),_now(),order['id']))
    return get(run_id)

def _load_order(db,row):
    r=db.execute('SELECT * FROM work_orders WHERE id=?',(row['id'],)).fetchone()
    order=json.loads(r['body']);order['status']=r['status'];order['dispatch_version']=r['dispatch_version']
    order['events']=[dict(e) for e in db.execute('SELECT event,message,at,dispatch_version FROM wo_events WHERE work_order=? ORDER BY id',(r['id'],)).fetchall()]
    return order

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    body['work_orders']=list_work_orders()
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def list_work_orders():
    with connection() as db:
        rows=db.execute('SELECT id FROM work_orders ORDER BY rowid').fetchall()
        return [_load_order(db,row) for row in rows]

def get_work_order(work_order_id):
    with connection() as db:
        row=db.execute('SELECT id FROM work_orders WHERE id=?',(work_order_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','待检工单不存在',404)
        return _load_order(db,row)

def dispatch(work_order_id, expected_version, note):
    """条件更新派单：expected_version 不匹配时保留旧版派单并返回409。

    派单成功仅追加审计事件并提升 dispatch_version，绝不覆盖历史记录。
    拒绝事件用独立事务落库，避免随 409 异常一起回滚而丢失记录。
    """
    if not isinstance(note,str) or not note.strip():raise DomainError('PARAMETERS','派单说明不能为空')
    db=connection()
    try:
        row=db.execute('SELECT dispatch_version,status FROM work_orders WHERE id=?',(work_order_id,)).fetchone()
        if not row:raise DomainError('NOT_FOUND','待检工单不存在',404)
        if row['status']=='已解除':raise DomainError('RESOLVED','工单已解除，无需派单')
        if row['dispatch_version']!=expected_version:
            db.execute('INSERT INTO wo_events(work_order,event,message,at,dispatch_version) VALUES (?,?,?,?,?)',
                (work_order_id,'dispatch_rejected','交接版本过期，已保留旧版派单',_now(),row['dispatch_version']))
            db.commit()
            raise DomainError('STALE','交接失败：派单版本已更新，旧版派单保持有效',409)
        new_version=expected_version+1
        order=json.loads(db.execute('SELECT body FROM work_orders WHERE id=?',(work_order_id,)).fetchone()['body'])
        order['dispatch']={'version':new_version,'note':note.strip(),'at':_now()}
        db.execute('UPDATE work_orders SET body=?,dispatch_version=?,updated_at=? WHERE id=?',
            (json.dumps(order,ensure_ascii=False),new_version,_now(),work_order_id))
        db.execute('INSERT INTO wo_events(work_order,event,message,at,dispatch_version) VALUES (?,?,?,?,?)',
            (work_order_id,'dispatched',note.strip(),_now(),new_version))
        db.commit()
    finally:
        db.close()
    return get_work_order(work_order_id)
