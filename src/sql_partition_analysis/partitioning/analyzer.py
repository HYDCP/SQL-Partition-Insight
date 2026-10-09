"""Extract SQL access evidence without file I/O or process-global configuration."""

def analyze_profile(profile, catalog):
    import re,collections,datetime,calendar
    partition_columns = catalog.partition_columns
    configured_columns = catalog.configured_columns
    from sql_partition_analysis.partitioning.scope import PartitionResolver
    from sql_partition_analysis.partitioning.operations import parse_alter, split_sql_statements, write_assignments, describe_partition_action
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope,Scope
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    from sql_partition_analysis.partitioning.parser import parse_postgres
    P=profile
    SQLS={s['id']:s for s in P['sqls']}
    def sql(n):return n.sql(dialect='postgres',comments=False) if isinstance(n,exp.Expression) else str(n)
    def date(s):
     for fmt in ('%Y-%m-%d','%Y%m%d','%Y-%m-%d %H:%M:%S'):
      try:return datetime.datetime.strptime(str(s),fmt).date()
      except ValueError:pass
     return None
    def val(n):
     if n is None:return None
     if isinstance(n,exp.Paren):return val(n.this)
     if isinstance(n,exp.Literal):
      d=date(n.this)
      if d:return d
      if not n.is_string:
       try:return int(n.this)
       except:pass
      return n.this
     if isinstance(n,exp.Cast):
      v=val(n.this)
      return v if isinstance(v,datetime.date) or 'DATE' not in sql(n.args.get('to')).upper() else date(v)
     if isinstance(n,(exp.StrToDate,exp.StrToTime)):return date(val(n.this))
     if isinstance(n,exp.Date):return date(val(n.this))
     if isinstance(n,(exp.DateTrunc,exp.TimestampTrunc)):
      v=val(n.this); u=sql(n.args.get('unit')).strip("'").upper()
      if isinstance(v,datetime.date):
       if u=='MONTH':return v.replace(day=1)
       if u=='YEAR':return v.replace(month=1,day=1)
       if u=='DAY':return v
     if isinstance(n,exp.Interval):
      v=val(n.this);u=sql(n.args.get('unit')).upper()
      try:return ('interval',int(v),u)
      except:return None
     if isinstance(n,(exp.Add,exp.Sub)):
      a,b=val(n.this),val(n.expression);sign=-1 if isinstance(n,exp.Sub) else 1
      if isinstance(a,datetime.date):
       if isinstance(b,int):return a+datetime.timedelta(days=sign*b)
       if isinstance(b,tuple):
        k=sign*b[1];u=b[2]
        if u in ('DAY','DAYS'):return a+datetime.timedelta(days=k)
        if u in ('WEEK','WEEKS'):return a+datetime.timedelta(days=k*7)
        if u in ('MONTH','MONTHS','YEAR','YEARS'):
         m=a.year*12+a.month-1+k*(12 if u.startswith('YEAR') else 1);y,m=divmod(m,12);m+=1
         return a.replace(year=y,month=m,day=min(a.day,calendar.monthrange(y,m)[1]))
      if isinstance(a,int) and isinstance(b,int):return a+sign*b
     if isinstance(n,exp.Anonymous) and n.name.lower() in ('date','to_date') and n.expressions:return date(val(n.expressions[0]))
     return None
    def datestr(v):return v.isoformat() if isinstance(v,datetime.date) else str(v)
    def normalize(n):
     def transform(x):
      if not isinstance(x,(exp.Column,exp.Identifier,exp.DataType)):
       v=val(x)
       if isinstance(v,datetime.date):return exp.Literal.string(v.isoformat())
      return x
     return n.copy().transform(transform)
    def ident_name(t):
     # SQLGlot has already normalized unquoted identifiers. Keep identifier values,
     # not SQL rendering, so optional quotes do not split the same table's visits.
     parts=[x.name for x in t.parts]
     if len(parts)==3 and parts[0]=='bohzedw':parts=parts[1:]
     return '.'.join(parts) if len(parts)>1 else '[未指定schema].'+parts[0]
    def category(t):
     schema=t.db.lower();name=t.name.lower()
     if schema in ('pg_catalog','information_schema','gp_toolkit') or (not schema and name.startswith('pg_')):return '系统对象'
     if schema:return '显式schema对象'
     return '临时表候选/未指定schema'
    def clean(s):
     n=s.replace('\x18','\n');notes=[]
     # Only remove a terminal Greenplum distribution clause, irrelevant to reads.
     n,c=re.subn(r'\s*DISTRIBUTED\s+(?:BY\s*\([^;]*\)|RANDOMLY)\s*;?\s*$','',n,flags=re.I)
     if c:notes.append('仅解析时移除末尾DISTRIBUTED子句')
     if re.search(r'\b0END\b',n,re.I):n=re.sub(r'\b0END\b','0 END',n,flags=re.I);notes.append('仅解析时将0END分词为0 END')
     if re.search(r'WHEN\s+IS\s+NOT\s+DISTINCT\s+FROM',n,re.I):
      n=re.sub(r'WHEN\s+IS\s+NOT\s+DISTINCT\s+FROM','WHEN',n,flags=re.I);notes.append('仅兼容SELECT投影中的简单CASE语法，条件区未改动')
     return n,notes
    DETAIL=[];PRED=[];INDEX=[];UNRES=[]
    for s in P['sqls']:
     raw=s['sql'];text,notes=clean(raw);sid=s['id'];stripped=re.sub(r'/\*[\s\S]*?\*/|--[^\n]*',' ',text).strip()
     meta={'id':sid,'status':'','notes':'; '.join(notes),'tables':[]}
     if not re.match(r'^(select|with|create|insert|update|delete|copy|alter|drop|truncate|analyze|vacuum|comment|set|show|begin|commit|rollback|start|end|explain)\b',stripped,re.I):
      meta['status']='非SQL日志消息';INDEX.append(meta);continue
     # Tokenize statements before dispatch: DDL must not consume a following query.
     try:
      trees=[]
      for statement in split_sql_statements(text):
       alter=parse_alter(statement)
       if alter is not None:
        trees.append(('alter',alter[0],alter[1]))
       else:
        trees.extend(normalize_identifiers(t,dialect='postgres') for t in parse_postgres(statement) if t is not None)
     except Exception as e:
      copy=re.match(r'^COPY\s+((?:"[^"]+"|[\w]+)(?:\.(?:"[^"]+"|[\w]+))*)\s*\(',stripped,re.I)
      if copy:
       t=normalize_identifiers(sqlglot.parse_one('SELECT * FROM '+copy[1],read='postgres'),dialect='postgres').find(exp.Table)
       name=ident_name(t);DETAIL.append({'id':sid,'occurrence':1,'table':name,'category':category(t),'role':'COPY方向未知','alias':'','candidates':'','all_conditions':'','status':'SQL截断，COPY方向和分区值未知','notes':'仅识别完整目标表名，未补造缺失正文'})
       meta['status']='部分解析（COPY截断）';meta['tables']=[name];meta['notes']='原文在字段列表中截断，仅提取目标表名';INDEX.append(meta);continue
      meta['status']='解析失败';meta['notes']+='; '+re.sub(r'\x1b\[[0-9;]*m','',str(e))[:500];INDEX.append(meta);continue
     if any(isinstance(t,exp.Command) and t.name.upper() not in ('SHOW','SET','BEGIN','START','END') for t in trees):meta['status']='语法未展开'
     elif trees and all(isinstance(t,tuple) for t in trees):meta['status']='已解析（显式DDL）'
     else:meta['status']='已解析'
     occnum=0
     for tree in trees:
      if isinstance(tree,tuple):
       _,t,part=tree;occnum+=1
       name=ident_name(t)
       role='分区维护' if describe_partition_action(part,partition_columns(name)) is not None else '结构变更'
       det={'id':sid,'occurrence':occnum,'table':name,'category':category(t),'role':role,'alias':'','candidates':part if role=='分区维护' else '', 'all_conditions':part,'status':'显式分区操作' if role=='分区维护' else '结构操作','notes':''}
       DETAIL.append(det);meta['tables'].append(name)
       if role=='分区维护' and partition_columns(name):PRED.append({'id':sid,'occurrence':occnum,'table':name,'category':category(t),'role':role,'field':'[显式分区]','condition':part,'raw':part,'origin':'ALTER TABLE','logic':'显式分区操作','value':'','status':'显式分区操作'})
       continue
      if isinstance(tree,exp.Comment) and isinstance(tree.this,exp.Column):
       c=tree.this
       tree.set('this',exp.Table(this=c.args.get('table'),db=c.args.get('db'),catalog=c.args.get('catalog')))
      scopes=list(traverse_scope(tree));scope_by_node={id(x.expression):x for x in scopes}
      cteids=set()
      for scope in scopes:
       for t in scope.tables:
        if isinstance(scope.sources.get(t.alias_or_name),Scope):cteids.add(id(t))
      tables=[t for t in tree.find_all(exp.Table) if id(t) not in cteids and isinstance(t.this,exp.Identifier)]
      detmap={}
      target=tree.this
      if isinstance(target,exp.Schema):target=target.this
      if isinstance(target,exp.Index):target=target.args.get('table')
      for t in tables:
       occnum+=1;role='读取'
       if t is target:
        if isinstance(tree,exp.Insert):role='插入'
        elif isinstance(tree,exp.Update):role='更新'
        elif isinstance(tree,exp.Delete):role='删除'
        elif isinstance(tree,exp.Copy):role='COPY写入' if tree.args.get('kind') else 'COPY读取'
        elif isinstance(tree,exp.Create):role='建索引' if tree.args.get('kind')=='INDEX' else '建表写入' if tree.args.get('expression') else '建表'
        elif isinstance(tree,exp.Drop):role='删除对象'
        elif isinstance(tree,exp.Comment):role='注释'
        elif isinstance(tree,exp.Analyze):role='ANALYZE'
       if isinstance(tree,exp.TruncateTable):role='清空'
       if isinstance(tree,exp.Drop):role='删除对象'
       if isinstance(tree,exp.Analyze):role='ANALYZE'
       d={'id':sid,'occurrence':occnum,'table':ident_name(t),'category':category(t),'role':role,'alias':t.alias_or_name,'candidates':[],'all_conditions':[],'status':'未发现候选条件','notes':[]}
       if not t.db:d['notes'].append('SQL未指定schema，保留原样，不自动补全')
       detmap[id(t)]=d
      # Field lineage is only used as a filter when crossing scopes is safe.
      resolver=PartitionResolver(scopes,detmap)
      resolve=resolver.resolve
      def owner(n):return n.find_ancestor(exp.Select,exp.Delete,exp.Update)
      def resolve_col(scope,node,c):
       if scope:return resolve(scope,c)
       direct=[t for t in tables if owner(t) is node]
       if c.table:direct=[t for t in direct if t.alias_or_name==c.table or t.name==c.table]
       return [(direct[0],c.copy(),False)] if len(direct)==1 else []
      atomtypes=(exp.EQ,exp.NEQ,exp.GT,exp.GTE,exp.LT,exp.LTE,exp.Between,exp.In,exp.Is,exp.Like,exp.ILike)
      for container in tree.walk():
       cond=container.this if isinstance(container,(exp.Where,exp.Having)) else container.args.get('on') if isinstance(container,exp.Join) else None
       if cond is None:continue
       node=owner(container);scope=scope_by_node.get(id(node));origin='JOIN ON' if isinstance(container,exp.Join) else 'HAVING' if isinstance(container,exp.Having) else 'WHERE'
       full=sql(cond)
       for a in cond.walk():
        if not isinstance(a,atomtypes) or owner(a) is not node:continue
        # SQLGlot 26 represents IS NOT NULL as Not(Is(..., Null())).
        # Keep that complete predicate together, as newer versions do.
        negated_null=isinstance(a,exp.Is) and isinstance(a.expression,exp.Null) and isinstance(a.parent,exp.Not)
        if negated_null:a=a.parent
        # Exclude comparisons embedded inside CASE, aggregate/functions, or another predicate.
        parent=a.parent;valid=True;logic=[]
        while parent is not container:
         if parent is None:valid=False;break
         if isinstance(parent,exp.Or):logic.append('OR分支')
         elif isinstance(parent,exp.Not):logic.append('NOT')
         elif not isinstance(parent,(exp.And,exp.Paren)):valid=False;break
         parent=parent.parent
        if not valid:continue
        for c in a.find_all(exp.Column):
         if owner(c) is not node:continue
         targets=resolve_col(scope,node,c)
         for t,mapped,derived in targets:
          d=detmap[id(t)]
          if origin+': '+full not in d['all_conditions']:d['all_conditions'].append(origin+': '+full)
         reason=resolver.reason(scope,c)
         relevant=any(mapped.name in partition_columns(detmap[id(t)]['table']) for t,mapped,_ in targets)
         if (reason and (not targets or relevant)) or (not targets and c.name in configured_columns()):
          unresolved=[sid,c.sql(),sql(a),reason or '字段归属不明确或来自计算列；未分配到底表']
          if unresolved not in UNRES:UNRES.append(unresolved)
         for t,mapped,derived in targets:
          if mapped.name not in partition_columns(detmap[id(t)]['table']):continue
          d=detmap[id(t)];expr=normalize(a)
          safe=origin!='JOIN ON' or resolver.safe_on(container,scope,t)
          if not safe:
           reason=resolver.on_reason(container,scope,t)
           if reason not in d['notes']:d['notes'].append(reason)
          # A UNION column may map to several branches. Rewrite separately for
          # this occurrence, never choose the first branch's column for all tables.
          allcols=list(a.find_all(exp.Column))
          def mapped_column(x):
           matches=[z[1] for z in resolve_col(scope,node,x) if id(z[0])==id(t)]
           names={z.name for z in matches}
           return exp.column(next(iter(names))) if len(names)==1 else None
          if all(mapped_column(x) is not None for x in allcols):
           def repl(x):
            if isinstance(x,exp.Column):
             mapped_x=mapped_column(x)
             if mapped_x is not None:return mapped_x
            return x
           expr=expr.transform(repl)
          condition=(sql(expr.this.this)+' IS NOT NULL') if negated_null else sql(expr)
          status='静态条件' if not a.find(exp.Subquery) and not a.find(exp.Parameter) and not a.find(exp.CurrentDate) and not a.find(exp.CurrentTimestamp) else '动态条件'
          if len(allcols)>1:status='关联/表达式条件'
          lg='、'.join(dict.fromkeys(logic)) or 'AND条件'
          if origin=='JOIN ON':lg+='；连接条件不保证物理裁剪'
          if derived:lg+='；经简单投影追溯'
          values=[]
          if isinstance(a,exp.In) and not a.args.get('query'):
           values=[datestr(val(x)) for x in a.expressions if val(x) is not None]
          elif isinstance(a,exp.Between):values=[datestr(val(a.args.get(k))) for k in ('low','high') if val(a.args.get(k)) is not None]
          elif isinstance(a,exp.Binary):
           for v in (val(a.this),val(a.expression)):
            if v is not None:values.append(datestr(v))
          item={'id':sid,'occurrence':d['occurrence'],'table':d['table'],'category':d['category'],'role':d['role'],'field':mapped.name,'condition':condition,'raw':sql(a),'safe_for_partition':safe,'origin':origin,'logic':lg,'value':', '.join(values),'status':status}
          if item not in PRED:PRED.append(item)
          v=f'{origin}: {condition} [{lg}]'
          if v not in d['candidates']:d['candidates'].append(v)
      # Assignments have their own branch identity, separate from read predicates.
      if isinstance(tree,(exp.Insert,exp.Update,exp.Create)) and id(target) in detmap:
       d=detmap[id(target)];assign=write_assignments(tree)
       for field,v,branch in assign:
        if field not in partition_columns(d['table']):continue
        cond=f'{field} := {sql(normalize(v))}';status='写入值（静态）' if val(v) is not None else '写入值（动态/参数）'
        PRED.append({'id':sid,'occurrence':d['occurrence'],'table':d['table'],'category':d['category'],'role':d['role'],'field':field,'condition':cond,'raw':f'{field} := {sql(v)}','origin':'写入赋值','assignment_branch':branch,'logic':'写入值，不是筛选条件','value':datestr(val(v)) if val(v) is not None else '', 'status':status})
        d['candidates'].append(cond+' [写入值]')
      for d in detmap.values():
       d['candidates']='\n'.join(d['candidates']);d['all_conditions']='\n'.join(d['all_conditions']);d['notes']='; '.join(d['notes'])
       if not partition_columns(d['table']):d['status']='非分区表（未列入配置）'
       elif d['candidates']:d['status']='有配置分区字段条件/写入值'
       elif d['role']=='COPY写入':d['status']='COPY数据正文未记录，分区值未知'
       elif d['role'] in ('建表写入','插入','更新'):d['status']='写入分区值未确定'
       DETAIL.append(d);meta['tables'].append(d['table'])
     meta['tables']=sorted(set(meta['tables']));INDEX.append(meta)

    # Access aggregation belongs to aggregate.py; retain only parse results here.
    table_categories=collections.defaultdict(set)
    for d in DETAIL:table_categories[d['table']].add(d['category'])
    METRICS={'sqlglot_version':sqlglot.__version__,'source_rows':sum(P['widths'].values()),'raw_distinct_messages':len({s['sql'] for s in P['sqls']}),'sql_user_contexts':len(P['sqls']),'status_sql_counts':dict(collections.Counter(x['status'] for x in INDEX)),'status_logs':{},'tables':len(table_categories),'table_categories':dict(collections.Counter('；'.join(sorted(v)) for v in table_categories.values())),'detail_rows':len(DETAIL),'unresolved_fields':len(UNRES),'time_min':min(s['first_time'] for s in P['sqls']),'time_max':max(s['last_time'] for s in P['sqls'])}
    for x in INDEX:METRICS['status_logs'][x['status']]=METRICS['status_logs'].get(x['status'],0)+SQLS[x['id']]['count']
    OUT={'metrics':METRICS,'details':DETAIL,'predicates':PRED,'index':INDEX,'unresolved':UNRES}
    return OUT
