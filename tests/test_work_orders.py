import csv,io
import pytest
from app.workflow import run,load
from app.work_orders import build_work_orders,reconcile
from app import store
from app.errors import DomainError

HEADER='id,bucket,version,kind,left,right,count,unit,note\n'
def row(rid,hour,kind,left,right,count):
    return f'{rid},2026-11-16T{hour:02d}:00:00Z,1,{kind},{left},{right},{count},人,x\n'
def zones(hour,vals=(100,100,100)):
    return (row(f'z{hour}a',hour,'zone','A','',vals[0])+row(f'z{hour}b',hour,'zone','B','',vals[1])+row(f'z{hour}c',hour,'zone','C','',vals[2]))
def pairs(hour,vals=(80,80,0)):
    return (row(f'p{hour}ab',hour,'pair','A','B',vals[0])+row(f'p{hour}ac',hour,'pair','A','C',vals[1])+row(f'p{hour}bc',hour,'pair','B','C',vals[2]))
def good(hour):
    return zones(hour,(120,110,90))+pairs(hour,(40,30,20))

def test_conflict_sample_loads_and_isolates_two_buckets():
    s=run(load('conflict.csv'),{})
    assert len(s['bucket_issues'])==2
    assert {i['code'] for i in s['bucket_issues']}=={'CONSTRAINT'}
    assert s['reconcile']['balanced'] is True

def test_m4_bucket_issue_keeps_evidence_and_all_devices():
    # 交集超出 min(A,B)：m4 阶段整桶隔离
    csv_text=HEADER+zones(1)+pairs(1,(130,20,20))
    s=run(csv_text.encode(),{})
    issue=next(i for i in s['bucket_issues'] if i['stage']=='m4_segments')
    assert issue['code']=='CONSTRAINT'
    assert len(issue['devices'])==6
    assert {e['kind'] for e in issue['evidence']}=={'zone','pair'}

def test_m5_infeasible_triple_becomes_work_order():
    # TRAP-19 场景：两两交集互相矛盾，无可行三方交集
    s=run((HEADER+zones(1)+pairs(1)).encode(),{})
    issue=next(i for i in s['bucket_issues'] if i['stage']=='m5_calculate')
    assert issue['model']['pairs']=={'A|B':80,'A|C':80,'B|C':0}
    assert len(s['work_orders'])==1
    wo=s['work_orders'][0]
    assert wo['id'].startswith('WO-') and wo['status']=='待检'

def test_work_order_lists_every_device_not_single_blame():
    s=run((HEADER+zones(1)+pairs(1)).encode(),{})
    wo=s['work_orders'][0]
    # 六台设备全部在同一张工单，没有点名单台送修
    assert sorted(wo['devices'])==sorted({e['id'] for c in wo['conflicts'] for e in c['evidence']})
    assert len(wo['devices'])==6

def test_work_order_records_affected_span_and_conflict_basis():
    s=run((HEADER+zones(1)+pairs(1)+zones(2)+pairs(2)).encode(),{})
    wo=s['work_orders'][0]
    assert wo['affected']['start'].endswith('T01:00:00+00:00')
    assert wo['affected']['end'].endswith('T02:00:00+00:00')
    assert len(wo['affected']['buckets'])==2
    assert {c['code'] for c in wo['conflicts']}=={'CONSTRAINT'}

def test_adjacent_conflict_buckets_merge_gap_splits():
    # 01、02 连续矛盾合并；04 与 02 不相邻，拆成第二张工单
    text=HEADER+zones(1)+pairs(1)+zones(2)+pairs(2)+good(3)+zones(4)+pairs(4)
    s=run(text.encode(),{})
    assert len(s['work_orders'])==2
    first,second=s['work_orders']
    assert len(first['affected']['buckets'])==2 and len(second['affected']['buckets'])==1

def test_same_batch_retry_is_idempotent(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    content=(HEADER+zones(1)+pairs(1)).encode()
    a=store.save(*_save_args(content));b=store.save(*_save_args(content))
    assert len(store.list_work_orders())==1
    assert a['work_orders'][0]['id']==b['work_orders'][0]['id']

def _save_args(content):
    s=run(content,{})
    return s['run_id'],s

def test_rejudge_only_affected_buckets_after_supplement(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    bad=HEADER+good(1)+zones(2)+pairs(2)+good(3)
    s1=run(bad.encode(),{});store.save(s1['run_id'],s1)
    before=store.list_work_orders()[0]
    assert before['status']=='待检' and len(before['fingerprints'])==1
    # 补录仅修正 02 桶（变成可行交集）
    fixed=HEADER+good(1)+good(2)+good(3)
    s2=run(fixed.encode(),{});store.save(s2['run_id'],s2)
    orders=store.list_work_orders()
    closed=next(o for o in orders if o['id']==before['id'])
    assert closed['status']=='已解除'
    assert any(e['event']=='resolved' for e in closed['events'])
    assert len(s2['work_orders'])==0

def test_unrelated_bucket_change_keeps_existing_order(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    s1=run((HEADER+zones(1)+pairs(1)+good(3)).encode(),{})
    store.save(s1['run_id'],s1)
    original=store.list_work_orders()[0]
    # 只改动 03 正常桶的数值，01 矛盾桶证据不变 -> 指纹不变，不新增工单
    changed=HEADER+zones(1)+pairs(1)+zones(3,(121,111,91))+pairs(3,(44,34,24))
    s2=run(changed.encode(),{});store.save(s2['run_id'],s2)
    orders=store.list_work_orders()
    assert len(orders)==1 and orders[0]['id']==original['id'] and orders[0]['status']=='待检'

def test_dispatch_uses_conditional_version_and_keeps_old_on_stale(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    s=run((HEADER+zones(1)+pairs(1)).encode(),{});store.save(s['run_id'],s)
    wid=store.list_work_orders()[0]['id']
    updated=store.dispatch(wid,0,'早班检修')
    assert updated['dispatch_version']==1 and updated['dispatch']['note']=='早班检修'
    # 旧版本号交接失败：抛 409，旧版派单原样保留
    with pytest.raises(DomainError) as e:store.dispatch(wid,0,'晚班抢单')
    assert e.value.status==409
    again=store.get_work_order(wid)
    assert again['dispatch']['note']=='早班检修'
    assert any(ev['event']=='dispatch_rejected' for ev in again['events'])

def test_dispatch_history_is_append_only(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    s=run((HEADER+zones(1)+pairs(1)).encode(),{});store.save(s['run_id'],s)
    wid=store.list_work_orders()[0]['id']
    store.dispatch(wid,0,'第一次交接')
    store.dispatch(wid,1,'第二次交接')
    wo=store.get_work_order(wid)
    assert wo['dispatch_version']==2
    assert [e['event'] for e in wo['events']].count('dispatched')==2
    assert wo['dispatch']['note']=='第二次交接'

def test_resolved_order_rejects_dispatch_but_kept_in_store(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'r.db'))
    s1=run((HEADER+zones(1)+pairs(1)).encode(),{});store.save(s1['run_id'],s1)
    wid=store.list_work_orders()[0]['id']
    s2=run((HEADER+good(1)).encode(),{});store.save(s2['run_id'],s2)
    with pytest.raises(DomainError) as e:store.dispatch(wid,0,'x')
    assert e.value.code=='RESOLVED'
    assert store.get_work_order(wid)['status']=='已解除'

def test_export_contains_work_order_devices_basis_and_span(state):
    from app.modules.m7_archive.export_out import bundle
    text=bundle(state)
    rows=list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    wo_rows=[r for r in rows if r[0]=='待检工单']
    # clean 派生的 state 无矛盾时允许为空；冲突样例必须完整落档
    conflict=run(load('conflict.csv'),{})
    crows=[r for r in csv.reader(io.StringIO(bundle(conflict).lstrip('﻿'))) if r[0]=='待检工单']
    assert len(crows)>=1
    import json as _json
    payload=_json.loads(crows[0][5])
    assert payload['devices'] and payload['affected']['buckets'] and payload['conflicts']

def test_dispatch_api_stale_returns_409_and_old_dispatch_survives(client):
    r=client.post('/api/runs',json={'sample':'conflict.csv'})
    assert r.status_code==200
    wid=client.get('/api/work_orders').get_json()[0]['id']
    ok=client.post(f'/api/work_orders/{wid}/dispatch',json={'version':0,'note':'早班'})
    assert ok.status_code==200 and ok.get_json()['dispatch_version']==1
    stale=client.post(f'/api/work_orders/{wid}/dispatch',json={'version':0,'note':'晚班'})
    assert stale.status_code==409
    kept=client.get(f'/api/work_orders/{wid}').get_json()
    assert kept['dispatch']['note']=='早班'

def test_m5_page_renders_work_order_panel(client):
    client.post('/api/runs',json={'sample':'conflict.csv'})
    html=client.get('/m5_calculate').data.decode()
    assert '待检工单' in html and '受影响区间' in html and '冲突依据' in html

def test_archive_export_includes_pending_orders(client):
    client.post('/api/runs',json={'sample':'conflict.csv'})
    rid=client.get('/api/runs').get_json()[0]['id']
    csv_text=client.get(f'/api/archive/{rid}/export.csv').data.decode('utf-8-sig')
    assert '待检工单' in csv_text and 'CONSTRAINT' in csv_text
