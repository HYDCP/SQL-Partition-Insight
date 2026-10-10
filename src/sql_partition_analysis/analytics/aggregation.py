"""Aggregate resolved access evidence without SQL dialect or parser dependencies."""

from sql_partition_analysis.engines import get_engine_adapter


def aggregate_accesses(profile, analysis, catalog, engine="adb_pg"):
    import datetime, collections
    adapter = get_engine_adapter(engine)
    CONFIG = catalog.config
    A = analysis
    S = {x['id']: x for x in profile['sqls']}

    def timestamp(s):
        return datetime.datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')

    G={};MAP=[]
    evidence = list(adapter.resolve_access_evidence(analysis, catalog))
    selected_details = [item.detail for item in evidence]
    for item in evidence:
     d = item.detail
     for candidate in item.candidates:
      role,key,basis=candidate.role,candidate.condition,candidate.basis
      groupkey=(d['table'],candidate.key,role)
      if groupkey not in G:G[groupkey]={'ids':set(),'basis':set(),'evidence':set(),'categories':set(),'condition':key}
      G[groupkey]['categories'].add(d['category'])
      G[groupkey]['ids'].add(d['id']);G[groupkey]['basis'].add(basis)
      G[groupkey]['evidence'].add(d['candidates'] or d['status'])
      MAP.append({'id':d['id'],'table':d['table'],'partition':key,'partition_identity':candidate.key,'role':role,'basis':basis,'occurrence':d['occurrence']})
    def stat(ids):
     n=sum(S[x]['count'] for x in ids);first=min(S[x]['first_time'] for x in ids);last=max(S[x]['last_time'] for x in ids)
     span=(timestamp(last)-timestamp(first)).total_seconds()
     return {'count':n,'first':first,'last':last,'mean_seconds':span/(n-1) if n>1 else None,'interval_count':n-1}
    RESULT=[]
    for (table,part,role),g in G.items():
     RESULT.append({'table':table,'partition':g['condition'],'partition_identity':part,'role':role,'category':'；'.join(sorted(g['categories'])),'basis':'；'.join(sorted(g['basis'])),'ids':sorted(g['ids']),'evidence':'\n'.join(sorted(g['evidence'])),**stat(g['ids'])})
    RESULT.sort(key=lambda x:(-x['count'],x['table'],x['partition'],x['role']))
    TYPES=[]
    for role in sorted({r['role'] for r in RESULT}):
     rs=[r for r in RESULT if r['role']==role];ids={x for r in rs for x in r['ids']};tableids=collections.defaultdict(set)
     for r in rs:tableids[r['table']].update(r['ids'])
     TYPES.append({'role':role,'logs':sum(S[x]['count'] for x in ids),'tables':len(tableids),'table_access_count':sum(sum(S[x]['count'] for x in ids) for ids in tableids.values()),'partition_groups':len(rs)})
    TYPES.sort(key=lambda x:-x['table_access_count'])
    activeids={x for r in RESULT for x in r['ids']}
    metrics={**A['metrics'],'stat_groups':len(RESULT),'access_log_count':sum(S[x]['count'] for x in activeids),'no_table_sql_logs':sum(S[x['id']]['count'] for x in A['index'] if x['status']!='非SQL日志消息' and not x['tables']),'type_count':len(TYPES)}
    selected_categories={d['table']:d['category'] for d in selected_details}
    metrics.update({'tables':len(selected_categories),'table_categories':dict(collections.Counter(selected_categories.values())),
                    'detail_rows':len(selected_details),'unresolved_fields':sum(row[0] in activeids for row in A['unresolved']),
                    'excluded_table_sql_logs':sum(S[x['id']]['count'] for x in A['index'] if x['tables'] and x['id'] not in activeids)})
    out={'metrics':metrics,'partition_config':CONFIG,'results':RESULT,'types':TYPES,'mapping':MAP}
    return out
