"""Existing ADB PostgreSQL analysis; preserve its established SQL semantics."""

from .base import EngineAdapter, EngineCapabilities


class AdbPostgresAdapter(EngineAdapter):
    name = "adb_pg"
    capabilities = EngineCapabilities(analysis=True, partition_evidence=True)

    def analyze_profile(self, profile, catalog):
        return analyze_profile(profile, catalog)

    def resolve_access_evidence(self, analysis, catalog):
        from .adb_pg_evidence import resolve_access_evidence
        return resolve_access_evidence(analysis, catalog)


def analyze_profile(profile, catalog):
    import re,collections
    partition_columns = catalog.partition_columns
    configured_columns = catalog.configured_columns
    from sql_partition_analysis.partitioning.scope import PartitionResolver, resolve_direct_column
    from sql_partition_analysis.partitioning.operations import parse_alter, split_sql_statements, write_assignments, describe_partition_action, recover_copy_header
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope,Scope
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    from sql_partition_analysis.partitioning.parser import parse_postgres, prepare_sql, is_sql_statement, recover_identifier_quote_encoding
    from sql_partition_analysis.partitioning.constants import value as val, render_value as datestr, normalize_expression as normalize
    P=profile
    SQLS={s['id']:s for s in P['sqls']}
    def sql(n):return n.sql(dialect='postgres',comments=False) if isinstance(n,exp.Expression) else str(n)
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
    def parse_statement(statement):
     alter=parse_alter(statement)
     if alter is not None:return [('alter',alter[0],alter[1])]
     return [normalize_identifiers(t,dialect='postgres') for t in parse_postgres(statement) if t is not None]
    def parse_trees(text):
     trees=[];notes=[]
     for statement in split_sql_statements(text):
      try:
       parsed=parse_statement(statement)
      except Exception:
       # Recover within this statement so one unsupported COPY cannot consume
       # preceding or following SQL, or reset its table occurrence numbering.
       parsed=None;recovered=recover_identifier_quote_encoding(statement)
       if recovered is not None:
        try:
         recovered,recovery_notes=prepare_sql(recovered)
         candidate=parse_statement(recovered)
         if candidate and not any(isinstance(t,exp.Command) for t in candidate):
          parsed=candidate;notes.extend(recovery_notes)
          notes.append('仅解析时按完整标识符双重引号编码兼容还原；原始SQL保留')
        except Exception:pass
       if parsed is None:
        copy=recover_copy_header(statement)
        if copy is None:raise
        parsed=[('copy',)+copy]
      trees.extend(parsed)
     return trees,notes
    DETAIL=[];PRED=[];INDEX=[];UNRES=[]
    for s in P['sqls']:
     raw=s['sql'];sid=s['id'];statistics_eligible=True
     wrapper=re.match(r'^\s*An exception was encountered during the execution of statement:\s*',raw,re.I)
     body=raw[wrapper.end():] if wrapper else raw
     text,notes=prepare_sql(body)
     if wrapper:
      statistics_eligible=False;notes.append('异常包装中的SQL证据；执行失败消息不纳入访问统计')
     meta={'id':sid,'status':'','notes':'; '.join(notes),'tables':[]}
     if not statistics_eligible:meta['statistics_eligible']=False
     if not is_sql_statement(text):
      meta['status']='非SQL日志消息';INDEX.append(meta);continue
     # Tokenize statements before dispatch: DDL must not consume a following query.
     try:
      trees,recovery_notes=parse_trees(text)
      notes.extend(recovery_notes);meta['notes']='; '.join(dict.fromkeys(notes))
     except Exception as e:
      # An encoded identifier may itself contain a semicolon. Recover the
      # whole quote frame before retrying statement boundaries in that case.
      trees=None;recovered=recover_identifier_quote_encoding(text)
      if recovered is not None:
       try:
        recovered,recovery_notes=prepare_sql(recovered)
        candidate,statement_notes=parse_trees(recovered)
        if candidate and not any(isinstance(t,exp.Command) for t in candidate):
         trees=candidate;notes.extend(recovery_notes+statement_notes)
         notes.append('仅解析时按完整标识符双重引号编码兼容还原；原始SQL保留')
         meta['notes']='; '.join(dict.fromkeys(notes))
       except Exception:pass
      if trees is None:
       meta['status']='解析失败';meta['notes']+='; '+re.sub(r'\x1b\[[0-9;]*m','',str(e))[:500];INDEX.append(meta);continue
     copies=[t for t in trees if isinstance(t,tuple) and t[0]=='copy']
     if any(isinstance(t,exp.Command) and t.name.upper() not in ('SHOW','SET','BEGIN','START','END') for t in trees):meta['status']='语法未展开'
     elif copies:meta['status']='部分解析（COPY截断）' if any(t[3] for t in copies) else '部分解析（COPY语法）'
     elif trees and all(isinstance(t,tuple) for t in trees):meta['status']='已解析（显式DDL）'
     else:meta['status']='已解析'
     occnum=0
     for tree in trees:
      if isinstance(tree,tuple) and tree[0]=='copy':
       _,t,direction,incomplete=tree;occnum+=1;name=ident_name(t)
       if t.meta.get('identifier_quote_recovery'):
        notes.append('仅解析时按完整COPY对象标识符双重引号编码兼容还原；截断字段列表和原始SQL保留')
       reason='字段列表或COPY头部未完整记录，方向和分区值未知' if incomplete else 'COPY语法未完全展开；仅保留完整目标和明确方向证据'
       notes.append(reason);meta['notes']='; '.join(dict.fromkeys(notes))
       detail={'id':sid,'occurrence':occnum,'table':name,'category':category(t),'role':direction or 'COPY方向未知','alias':'','candidates':'','all_conditions':'','status':reason,'notes':'未补造数据正文；分区值未知'}
       if not statistics_eligible:detail['statistics_eligible']=False
       DETAIL.append(detail);meta['tables'].append(name)
       continue
      if isinstance(tree,tuple):
       _,t,part=tree;occnum+=1
       name=ident_name(t)
       role='分区维护' if describe_partition_action(part,partition_columns(name)) is not None else '结构变更'
       det={'id':sid,'occurrence':occnum,'table':name,'category':category(t),'role':role,'alias':'','candidates':part if role=='分区维护' else '', 'all_conditions':part,'status':'显式分区操作' if role=='分区维护' else '结构操作','notes':''}
       if not statistics_eligible:det['statistics_eligible']=False
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
       if not statistics_eligible:d['statistics_eligible']=False
       if not t.db:d['notes'].append('SQL未指定schema，保留原样，不自动补全')
       detmap[id(t)]=d
      # Field lineage is only used as a filter when crossing scopes is safe.
      resolver=PartitionResolver(scopes,detmap)
      resolve=resolver.resolve
      def owner(n):return n.find_ancestor(exp.Select,exp.Delete,exp.Update)
      def resolve_col(scope,node,c):
       if scope:return resolve(scope,c)
       direct=[t for t in tables if owner(t) is node]
       return resolve_direct_column(direct,c)
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
