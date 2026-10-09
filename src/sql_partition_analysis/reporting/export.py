"""Export human-facing reports without coupling presentation to analysis services."""

def export_reports(profile, analysis, aggregate, source_path, output_path, progress=None):
    from copy import deepcopy
    import csv,json,collections,re,gzip
    from pathlib import Path
    from sql_partition_analysis.ingestion.records import sql_identity, TIME_COLUMN, USER_COLUMN
    from .access_data import AccessDatasetWriter
    R=Path(output_path)
    G=deepcopy(aggregate);A=deepcopy(analysis);P=profile;S={s['id']:s for s in P['sqls']}
    selected_tables={r['table'] for r in G['results']};selected_ids={sid for r in G['results'] for sid in r['ids']}
    A['details']=[d for d in A['details'] if d['table'] in selected_tables]
    A['index']=[dict(x,tables=[t for t in x['tables'] if t in selected_tables]) for x in A['index'] if x['id'] in selected_ids]
    A['unresolved']=[row for row in A['unresolved'] if row[0] in selected_ids]
    def write(name,head,rows):
     with (R/name).open('w',encoding='utf-8-sig',newline='') as f:
      w=csv.writer(f);w.writerow(head);w.writerows(rows)
    def partition_display(table,condition,role=''):
     columns=G['partition_config']['tables'].get(table,{}).get('partition_columns',[])
     if condition=='非分区表':return condition
     match=re.fullmatch(r'FOR\((\d{4}-\d{2}-\d{2})\)',condition)
     if match and len(columns)==1:
      return f'分区定位：包含 {columns[0]} = {match[1]} 的分区'
     match=re.fullmatch(r'分区名=(.+)；范围([\[(])(\d{4}-\d{2}-\d{2}),(\d{4}-\d{2}-\d{2})([\])])',condition)
     if match and len(columns)==1:
      lower='≥' if match[2]=='[' else '>'
      upper='≤' if match[5]==']' else '<'
      return f'分区范围：{columns[0]} {lower} {match[3]} 且 {upper} {match[4]}；分区名：{match[1]}'
     match=re.fullmatch(r'分区名=(.+)；VALUES\((.+)\)',condition)
     if match and len(columns)==1:
      return f'分区取值：{columns[0]} 属于 {{{match[2]}}}；分区名：{match[1]}'
     match=re.fullmatch(r'分区未知（未发现配置字段条件：(.+)）',condition)
     if match:return f'分区未确定：未识别到 {match[1]} 的筛选条件'
     if condition=='分区值未知（写入值未完整记录）':return '分区未确定：无法从 SQL 确定写入值'
     if condition=='表级操作（未指定分区）':return '表级操作：未指定分区'
     if condition.startswith('分区未确定（'):
      return condition.replace('分区未确定（','分区未确定：',1).replace('）：','；',1)
     if condition.startswith('条件矛盾（'):
      return '分区未确定：筛选条件矛盾；'+condition.split('：',1)[-1]
     match=re.fullmatch(r'(\w+) := (.+)',condition)
     if match:
      if re.fullmatch(r'(?:\w+\.)?\*',match[2]):
       return f'分区未确定：{match[1]} 的写入值来自 SELECT *，尚未解析'
      return f'写入取值：{match[1]} ← 来源字段 {match[2]}（具体值未确定）'
     match=re.fullmatch(r'(\w+) = CURRENT_DATE - 1',condition)
     if match:return f'筛选条件：{match[1]} = 数据库当前日期的前一天（动态日期：CURRENT_DATE）'
     now_pattern=r"(\w+) = \(CAST\(\(CAST\('now' AS TEXT\)\) AS DATE\) - 1\)"
     match=re.fullmatch(now_pattern,condition)
     if match:return f'筛选条件：{match[1]} = 数据库当前日期的前一天（动态日期：now）'
     match=re.fullmatch(now_pattern+r' AND \1 = CURRENT_DATE - 1',condition)
     if match:return f'筛选条件：{match[1]} = 数据库当前日期的前一天（动态日期：now 与 CURRENT_DATE 两项条件）'
     match=re.fullmatch(r'(\w+)=(.+)',condition)
     if match:
      label='写入取值' if role in ('INSERT写入','CTAS写入','COPY写入') else '筛选条件'
      return f'{label}：{match[1]} = {match[2]}'
     if condition.startswith('复杂逻辑：'):return '筛选条件（复杂逻辑）：'+condition[len('复杂逻辑：'):]
     if role.startswith('ALTER'):return '分区操作：'+condition
     return '筛选条件：'+condition

    def basis_display(table,basis):
     if basis.startswith('分区未知（') or basis=='分区值未知（写入值未完整记录）' or basis=='表级操作（未指定分区）':
      return partition_display(table,basis)
     return basis
    for i,r in enumerate(G['results'],1):r['group_id']=f'G{i:05d}'
    # Sort aggregate rows by table, then their first visit; break timestamp ties
    # using original log order. Assign IDs beforehand to preserve trace links.
    G['results'].sort(key=lambda r:(r['table'],r['first'],min(S[sid]['first_record'] for sid in r['ids'])))
    for r in G['results']:
     entry=G['partition_config']['tables'].get(r['table'])
     r['table_partition_info']=(dict(RANGE='范围分区',LIST='列表分区',HASH='哈希分区').get(entry.get('partition_type'), '分区表')+'（字段：'+', '.join(entry['partition_columns'])+'）') if entry else '非分区表'
     r['raw_partition']=r['partition']
     r['partition']=partition_display(r['table'],r['partition'],r['role'])
     r['basis']=basis_display(r['table'],r['basis'])
    for r in G['mapping']:
     r['raw_partition']=r['partition']
     r['partition']=partition_display(r['table'],r['partition'],r['role'])
     r['basis']=basis_display(r['table'],r['basis'])
    write('表分区访问统计.csv',['统计组ID','表名','表分区信息','分区或完整条件','访问类型','访问次数','平均访问间隔_秒','首次访问_CST','最后访问_CST','对象类别','判断依据','SQL编号','解析条件或分区操作'],[[r['group_id'],r['table'],r['table_partition_info'],r['partition'],r['role'],r['count'],r['mean_seconds'] if r['mean_seconds'] is not None else '',r['first'],r['last'],r['category'],r['basis'],','.join(r['ids']),r['raw_partition']] for r in G['results']])
    write('SQL解析明细.csv',['SQL编号','表引用编号','表名','访问类型','配置分区字段条件或分区操作','完整WHERE和JOIN条件','解析备注','日志用户名'],[[d['id'],d['occurrence'],d['table'],d['role'],d['candidates'],d['all_conditions'],d['notes'],S[d['id']].get('user_name','')] for d in A['details']])
    write('SQL原文索引.csv',['SQL编号','日志条数_未去重','首次数据记录序号','首次时间','最后时间','日志语句类型','解析状态','解析说明','原始SQL正文','日志用户名'],[[x['id'],S[x['id']]['count'],S[x['id']]['first_record'],S[x['id']]['first_time'],S[x['id']]['last_time'],','.join(S[x['id']]['types']),x['status'],x['notes'],S[x['id']]['sql'].replace('\x18','\n'),S[x['id']].get('user_name','')] for x in A['index']])
    write('待确认字段.csv',['SQL编号','字段','条件','说明'],A['unresolved'])
    table_ids=collections.defaultdict(set)
    for d in A['details']:table_ids[d['table']].add(d['id'])
    config=G['partition_config']
    coverage=[]
    for table,ids in sorted(table_ids.items()):
     entry=config['tables'][table]
     assert entry['partition_columns']
     status='分区表'
     coverage.append([table,status,entry.get('partition_type',''),','.join(entry.get('partition_columns',[])),entry.get('partition_definition',''),sum(S[sid]['count'] for sid in ids),','.join(sorted(ids))])
    write('表分区配置核对.csv',['表名','元数据匹配状态','分区方式','分区字段','分区定义','访问日志条数_同条日志同表计一次','关联SQL编号'],coverage)
    (R/'report_data.json').write_text(json.dumps(G,ensure_ascii=False),encoding='utf8')
    # Re-read all source records independently. Validate every final aggregate and record log-to-group links.
    sql_to_id={(s['sql'],s['user_name']):s['id'] for s in P['sqls']};id_to_groups=collections.defaultdict(list)
    for r in G['results']:
     for sid in r['ids']:id_to_groups[sid].append(r['group_id'])
    check={r['group_id']:{'n':0,'first':None,'last':None} for r in G['results']}
    csv.field_size_limit(20_000_000)
    with Path(source_path).open(encoding='utf-8-sig',newline='') as f, gzip.open(R/'逐条日志统计映射.csv.gz','wt',encoding='utf-8-sig',newline='') as out, AccessDatasetWriter(R,P,G['results'],G['partition_config']) as access_data:
     rd=csv.reader(f);next(rd);w=csv.writer(out);w.writerow(['源数据记录序号_不含表头','日志时间_CST','SQL编号','统计组ID','日志用户名'])
     for i,row in enumerate(rd,1):
      if progress and i%100000==0:progress(f'已核对 {i:,} 条源日志')
      sid=sql_to_id[sql_identity(row)]
      gids=id_to_groups[sid]
      if not gids:continue
      w.writerow([i,row[TIME_COLUMN],sid,','.join(gids),row[USER_COLUMN]])
      access_data.write(i,row[TIME_COLUMN],sid,row[USER_COLUMN],gids)
      for gid in gids:
       c=check[gid];c['n']+=1;c['first']=row[TIME_COLUMN] if c['first'] is None else min(c['first'],row[TIME_COLUMN]);c['last']=row[TIME_COLUMN] if c['last'] is None else max(c['last'],row[TIME_COLUMN])
    for r in G['results']:
     c=check[r['group_id']];assert (c['n'],c['first'],c['last'])==(r['count'],r['first'],r['last']),r
    access_data.finish()
    return G
