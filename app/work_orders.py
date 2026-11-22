"""待检工单裁决。

裁决原则：两两交集互相矛盾时不点名单台设备，而是把同一时间桶内全部相关
设备列入同一张待检工单；连续的矛盾时间桶合并为一个受影响区间。工单身份
由冲突指纹决定，同源输入重复提交得到同一指纹，不产生新工单。
"""
import hashlib,json


def _fingerprint(issue):
    """冲突指纹：依据桶内全部证据与原因码，与具体批次解耦。

    同批重试内容不变 -> 指纹不变；补录只改变相关桶 -> 仅相关桶指纹变化。
    """
    payload=json.dumps(
        {'bucket':issue['bucket'],'code':issue['code'],'stage':issue['stage'],
         'devices':issue['devices'],'evidence':issue['evidence']},
        ensure_ascii=False,sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def _structure(issue):
    """设备组结构签名：同一组区域/交集设备的相邻矛盾桶才可合并。

    业务 ID 每桶本就不同，不能直接用 ID 集合分组；用证据的类型与区域
    结构（zone/pair/triple + 区域对）刻画“同一组设备”。
    """
    parts=sorted((e['kind'],e['left'],e.get('right','')) for e in issue['evidence'])
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False).encode()).hexdigest()[:12]


def _span(buckets):
    ordered=sorted(buckets)
    return {'start':ordered[0],'end':ordered[-1],'buckets':ordered}


def build_work_orders(state):
    """由 m4/m5 产出的 bucket_issues 纯函数派生待检工单。

    同结构设备组的相邻矛盾桶合并成区间工单；结构不同或中间被正常桶隔开
    则分别成单，避免把无关设备卷入检修。
    """
    issues=sorted(state.get('bucket_issues',[]),key=lambda x:x['bucket'])
    groups=[]
    for issue in issues:
        sig=_structure(issue)
        if groups and groups[-1]['sig']==sig and _is_adjacent(groups[-1]['last_bucket'],issue['bucket']):
            groups[-1]['issues'].append(issue)
            groups[-1]['last_bucket']=issue['bucket']
        else:
            groups.append({'sig':sig,'last_bucket':issue['bucket'],'issues':[issue]})
    orders=[]
    for g in groups:
        fingerprints=[_fingerprint(i) for i in g['issues']]
        devices=sorted({d for i in g['issues'] for d in i['devices']})
        identity=hashlib.sha256(
            (g['sig']+'#'+','.join(fingerprints)).encode('utf-8')).hexdigest()[:16]
        span=_span([i['bucket'] for i in g['issues']])
        codes=sorted({i['code'] for i in g['issues']})
        stages=sorted({i['stage'] for i in g['issues']})
        orders.append({
            'id':'WO-'+identity,
            'status':'待检',
            'devices':devices,
            'affected':span,
            'reasons':codes,
            'stages':stages,
            'structure':g['sig'],
            'fingerprints':fingerprints,
            'conflicts':[{'bucket':i['bucket'],'stage':i['stage'],'code':i['code'],
                          'reason':i['reason'],'evidence':i['evidence'],
                          **({'model':i['model']} if 'model' in i else {})}
                         for i in g['issues']],
            'line':min(i['line'] for i in g['issues']),
        })
    state['work_orders']=orders
    return state


def _is_adjacent(prev_bucket,bucket):
    """小时桶是否相邻（UTC 整点，间隔一小时）。"""
    from datetime import datetime,timedelta
    a=datetime.fromisoformat(prev_bucket);b=datetime.fromisoformat(bucket)
    return b-a==timedelta(hours=1)


def reconcile(existing,derived):
    """把本次派生工单与持久化工单对账，只重判受冲突影响的桶。

    返回 (orders,created,resolved)：
    - 指纹命中既有工单：沿用旧记录与旧版派单，不新增（同批重试幂等）；
    - 新指纹：生成待检工单；
    - 旧工单的冲突指纹在本次不再出现：标记已解除并保留全部记录。
    """
    live_fingerprints={fp for o in derived for fp in o['fingerprints']}
    by_fingerprint={}
    for old in existing:
        for fp in old.get('fingerprints',[]):by_fingerprint.setdefault(fp,old)
    orders=[];created=[];resolved=[]
    for o in derived:
        prior=next((by_fingerprint[fp] for fp in o['fingerprints'] if fp in by_fingerprint),None)
        if prior:
            merged=dict(o)
            merged['status']=prior['status']
            if prior.get('dispatch'):merged['dispatch']=prior['dispatch']
            if prior.get('events'):merged['events']=prior['events']
            orders.append(merged)
        else:
            orders.append(o);created.append(o['id'])
    active_ids={o['id'] for o in orders}
    for old in existing:
        if old['id'] not in active_ids and not any(fp in live_fingerprints for fp in old.get('fingerprints',[])):
            closed=dict(old);closed['status']='已解除'
            closed.setdefault('events',[]).append({'event':'resolved','message':'补录后相关时间桶重判通过'})
            orders.append(closed);resolved.append(old['id'])
    return orders,created,resolved
