"""Group repeated SQL for parsing while preserving original occurrence counts."""

def profile_logs(source, label=None, progress=None):
    import csv, collections
    from pathlib import Path
    from sql_partition_analysis.ingestion.records import sql_identity, FIELD_COUNT, TIME_COLUMN, TYPE_COLUMN
    csv.field_size_limit(20_000_000)
    src=Path(source)
    widths=collections.Counter(); types=collections.Counter(); sqls={}; bad=[]
    with src.open(encoding='utf-8-sig',newline='') as f:
     r=csv.reader(f); header=next(r)
     for i,row in enumerate(r,1):
      if progress and i%100000==0:progress(f'已扫描 {i:,} 条日志；SQL/用户组合 {len(sqls):,}')
      widths[len(row)]+=1
      if len(row)!=FIELD_COUNT:
       raise ValueError(f'第 {i} 条记录有 {len(row)} 列，预期 33 列；请检查导出字段顺序')
      if not row[TIME_COLUMN]:raise ValueError(f'第 {i} 条记录缺少日志时间')
      types[row[TYPE_COLUMN]]+=1
      # Retain the original user for traceability and session evidence.
      key=sql_identity(row)
      if key not in sqls: sqls[key]={'sql':key[0],'user_name':key[1],'count':0,'first_record':i,'first_time':row[TIME_COLUMN],'last_time':row[TIME_COLUMN],'types':set()}
      s=sqls[key]; s['count']+=1; s['types'].add(row[TYPE_COLUMN]); s['first_time']=min(s['first_time'],row[TIME_COLUMN]); s['last_time']=max(s['last_time'],row[TIME_COLUMN])
    if not sqls:raise ValueError('输入日志为空，请检查文件或查询时间范围')
    out=[]
    for n,s in enumerate(sqls.values(),1):
     s['id']=f'SQL{n:06d}';s['types']=sorted(s['types']);out.append(s)
    return {'source':{'path':str(src),'label':label or src.name},'header':header,'widths':dict(widths),'types':dict(types),'bad':bad,'sqls':out}
