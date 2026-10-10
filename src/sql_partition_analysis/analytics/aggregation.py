"""Aggregate SQL access evidence independently of files and report formatting."""

def aggregate_accesses(profile, analysis, catalog):
    import datetime,re,itertools,collections
    from dataclasses import replace
    from sql_partition_analysis.domain.partition import PartitionCandidate as Candidate
    from decimal import Decimal, InvalidOperation
    import sqlglot
    from sqlglot import exp
    from sql_partition_analysis.partitioning.parser import parse_one_postgres
    from sql_partition_analysis.partitioning.constants import value as constant_value, render_value as constant_text
    CONFIG = catalog.config
    partition_columns = catalog.partition_columns
    from sql_partition_analysis.partitioning.operations import describe_partition_action
    A=analysis;P=profile;S={x['id']:x for x in P['sqls']}
    ROLE={'读取':'查询读取','COPY读取':'COPY读取','插入':'INSERT写入','COPY写入':'COPY写入','建表写入':'CTAS写入','更新':'UPDATE更新','删除':'DELETE删除','清空':'TRUNCATE清空','删除对象':'DROP删除对象','建表':'CREATE建表','建索引':'CREATE建索引','结构变更':'ALTER结构变更','ANALYZE':'ANALYZE统计','注释':'COMMENT注释','COPY方向未知':'COPY方向未知'}
    pr=collections.defaultdict(list)
    for x in A['predicates']:pr[(x['id'],x['occurrence'])].append(x)
    def timestamp(s):return datetime.datetime.strptime(s[:19],'%Y-%m-%d %H:%M:%S')

    def parsed_condition(item):
     try:return parse_one_postgres(item['condition'])
     except sqlglot.errors.ParseError:return None

    def unparen(node):
     while isinstance(node,exp.Paren):node=node.this
     return node

    def is_field(node,field):
     node=unparen(node)
     return isinstance(node,(exp.Column,exp.Identifier)) and node.name==field

    def literal(node):
     """Keep SQL literal types while using the existing readable group keys."""
     node=unparen(node)
     if isinstance(node,exp.Literal):
      if node.is_string:return ('string',node.this)
      try:return ('number',Decimal(node.this))
      except InvalidOperation:return None
     if isinstance(node,exp.Boolean):return ('boolean',node.this)
     if isinstance(node,exp.Neg):
      value=literal(node.this)
      if value and value[0]=='number':return ('number',-value[1])
     value=constant_value(node)
     if isinstance(value,datetime.datetime):return ('timestamp',constant_text(value))
     if isinstance(value,datetime.date):return ('date',value.isoformat())
     return None

    def literal_text(value):
     if value[0]=='boolean':return 'TRUE' if value[1] else 'FALSE'
     if value[0]=='number':
      text=format(value[1],'f')
      if '.' in text:text=text.rstrip('0').rstrip('.')
      return '0' if text=='-0' else text
     return str(value[1])

    def literal_label(value):
     text=literal_text(value)
     if value[0]=='string' and (' AND ' in text or ' / ' in text):
      return exp.Literal.string(text).sql(dialect='postgres')
     return text

    def finite_values(node,field):
     """Read EQ/IN from the AST: commas and quotes can belong to one literal."""
     node=unparen(node)
     if isinstance(node,(exp.EQ,exp.PropertyEQ)):
      if is_field(node.this,field):value=literal(node.expression)
      elif is_field(node.expression,field):value=literal(node.this)
      else:return None
      return {value} if value is not None else None
     if isinstance(node,exp.In) and is_field(node.this,field) and not node.args.get('query'):
      values=[literal(value) for value in node.expressions]
      if values and all(value is not None for value in values):return set(values)
     return None

    def ordered_compare(left,right):
     # Column types/collations are absent from the metadata. Order only numbers
     # and ISO dates; do not guess the ordering of arbitrary string partitions.
     if left[0]!=right[0]:return None
     if left[0]=='number':a,b=left[1],right[1]
     elif left[0]=='date':
      a,b=datetime.date.fromisoformat(left[1]),datetime.date.fromisoformat(right[1])
     elif left[0]=='timestamp':
      a,b=datetime.datetime.fromisoformat(left[1]),datetime.datetime.fromisoformat(right[1])
     elif left[0]=='string':
      # Unknown string literals are coerced by the field type and collation.
      # Date-looking text must not silently acquire date ordering semantics.
      return None
     else:return None
     return (a>b)-(a<b)

    def satisfies_static_constraint(value,node,field):
     """Return False only for a proven contradiction, None if not evaluable."""
     node=unparen(node)
     comparisons=(exp.EQ,exp.NEQ,exp.GT,exp.GTE,exp.LT,exp.LTE)
     if isinstance(node,comparisons):
      reverse=False
      if is_field(node.this,field):bound=literal(node.expression)
      elif is_field(node.expression,field):bound=literal(node.this);reverse=True
      else:return None
      if bound is None:return None
      if isinstance(node,(exp.EQ,exp.NEQ)):
       if value[0]!=bound[0]:return None
       same=value==bound
       return same if isinstance(node,exp.EQ) else not same
      comparison=ordered_compare(value,bound)
      if comparison is None:return None
      if reverse:comparison=-comparison
      if isinstance(node,exp.GT):return comparison>0
      if isinstance(node,exp.GTE):return comparison>=0
      if isinstance(node,exp.LT):return comparison<0
      return comparison<=0
     if isinstance(node,exp.Between) and is_field(node.this,field):
      low,high=literal(node.args.get('low')),literal(node.args.get('high'))
      if low is None or high is None:return None
      lower,upper=ordered_compare(value,low),ordered_compare(value,high)
      if lower is None or upper is None:return None
      return lower>=0 and upper<=0
     if isinstance(node,exp.Is) and is_field(node.this,field) and isinstance(node.expression,exp.Null):return False
     if isinstance(node,exp.Not) and isinstance(node.this,exp.Is):
      inner=node.this
      if is_field(inner.this,field) and isinstance(inner.expression,exp.Null):return True
     return None

    def has_static_constraint(node,field):
     """Unverified static expressions stay conditions rather than false keys."""
     if node is None:return False
     if any(isinstance(n,(exp.Subquery,exp.Parameter,exp.Placeholder,exp.CurrentDate,exp.CurrentTimestamp)) for n in node.walk()):return False
     columns=list(node.find_all(exp.Column))
     return (len(columns)==1 and columns[0].name==field
             and any(isinstance(n,(exp.Literal,exp.Null,exp.Boolean)) for n in node.walk()))

    def infer_conditions(d,items,role,basis='按表分区配置解析'):
     safe=[item for item in items if item.get('safe_for_partition',True)]
     unsafe=[item for item in items if not item.get('safe_for_partition',True)]
     if any('OR' in item['logic'] or 'NOT' in item['logic'] for item in safe):
      return [Candidate(role,'复杂逻辑：'+d['all_conditions'],'OR/NOT完整条件，未展开分区')]
     if not safe:
      if unsafe:
       return [Candidate(role,'分区未确定（条件不能安全用于分区推断）：'+' AND '.join(sorted({x['condition'] for x in unsafe})),
                '连接或跨查询条件仅保留证据，未用于确定分区取值')]
      status='分区未知（未发现配置字段条件：'+', '.join(partition_columns(d['table']))+'）'
      return [Candidate(role,status,status)]
     fields=collections.defaultdict(list)
     for item in safe:fields[item['field']].append(item)
     options=[];complexes=[];additional=False
     for field,field_items in sorted(fields.items()):
      allowed=None;remaining=[];ambiguous_strings=False
      value_types=set()
      for item in field_items:
       values=finite_values(parsed_condition(item),field)
       if values is not None:value_types.update(value[0] for value in values)
      if len(value_types)>1:
       # Unknown column types can coerce an untyped string to a typed date or
       # number. Different SQL literal representations do not prove a conflict.
       complexes.extend(sorted({item['condition'] for item in field_items}));continue
      for item in field_items:
       node=parsed_condition(item);values=finite_values(node,field)
       if values is not None:
        if allowed is not None and allowed != values and 'string' in value_types:
         # Even a nonempty raw intersection can drop values that are equal
         # after the unknown column's input conversion or collation.
         ambiguous_strings=True
        allowed=values if allowed is None else allowed & values
       else:remaining.append((item,node))
      if ambiguous_strings:
       complexes.extend(sorted({item['condition'] for item in field_items}));continue
      if allowed is not None:
       uncertain_static=False
       for item,node in remaining:
        decisions={value:satisfies_static_constraint(value,node,field) for value in allowed}
        allowed={value for value,decision in decisions.items() if decision is not False}
        if has_static_constraint(node,field) and any(decision is None for decision in decisions.values()):uncertain_static=True
       if not allowed:
        return [Candidate(role,'条件矛盾（未确定分区）：'+' AND '.join(sorted({x['condition'] for x in safe})),
                 '静态条件交集为空，未输出确定分区候选')]
       if not uncertain_static:
        options.append([(field+'='+literal_label(value), (field,value[0],literal_text(value)))
                        for value in sorted(allowed,key=lambda v:(v[0],literal_text(v)))])
        additional=additional or bool(remaining)
        continue
      complexes.extend(sorted({item['condition'] for item in field_items}))
     if complexes:
      return [Candidate(role,' AND '.join([' / '.join(value[0] for value in option) for option in options]+complexes),
               '条件范围/动态表达式，未展开物理分区'+('；不安全条件仅保留证据' if unsafe else ''),
               ('mixed',tuple(tuple(value[1] for value in option) for option in options),tuple(complexes)))]
     if additional:basis+='；附加条件保留证据，候选不保证实际命中'
     if unsafe:basis+='；连接或跨查询条件仅保留证据，未用于确定取值'
     return [Candidate(role,' AND '.join(value[0] for value in candidate),basis,
                       ('values',tuple(value[1] for value in candidate)))
             for candidate in itertools.product(*options)]

    def assignment_branches(d,writes,role,unchanged=()):
     branches=collections.defaultdict(list)
     for item in writes:branches[item.get('assignment_branch',0)].append(item)
     result=[]
     for items in branches.values():
      static=[];dynamic=[]
      for item in items:
       node=parsed_condition(item);values=finite_values(node,item['field'])
       if values is not None:static.append(item)
       else:dynamic.append(item['condition'])
      if not dynamic:
       result.extend(infer_conditions(d,list(unchanged)+static,role,'按写入分支解析分区取值'))
      else:
       # Dynamic writes are a separate destination possibility, never an AND
       # restriction on the old value. Carry only unchanged partition fields.
       known_conditions=list(unchanged)+static
       if known_conditions:
        known=infer_conditions(d,known_conditions,role)
        result.extend(Candidate(role,item.condition+' AND '+' AND '.join(dynamic),
                       '写入值含动态表达式，具体新分区未确定；'+item.basis,
                       ('assignment',item.key,tuple(dynamic)))
                      for item in known)
       else:result.append(Candidate(role,' AND '.join(dynamic),'写入值含动态表达式，具体新分区未确定'))
     return result

    def distinct_candidates(rows):
     merged={};bases=collections.defaultdict(set)
     for row in rows:
      key=(row.role,row.key);merged.setdefault(key,row);bases[key].add(row.basis)
     return [replace(row,basis='；'.join(sorted(bases[key]))) for key,row in merged.items()]

    def partition(d):
     ps=pr[(d['id'],d['occurrence'])]
     role=ROLE.get(d['role'],d['role'])
     if d['role']=='分区维护':
      text=d['all_conditions'];role='ALTER新增分区' if re.match(r'ADD\s+(?:DEFAULT\s+)?PARTITION\b',text,re.I) else 'ALTER清空分区' if re.match(r'TRUNCATE\s+PARTITION\b',text,re.I) else 'ALTER分区维护'
     columns=partition_columns(d['table'])
     if not columns:return [Candidate(role,'非分区表','元数据标明无分区（NULL）')]
     if d['role']=='分区维护':
      key=describe_partition_action(text,columns)
      return [Candidate(role,key or text,'已配置分区表的显式分区操作',('partition_ddl',text))]
     core=[x for x in ps if x['field'] in columns]
     # INSERT branches/VALUES rows are alternative writes, not conjunctive filters.
     # Preserve each branch's key tuple to avoid inventing cross-branch combinations.
     writes=[x for x in core if x['origin']=='写入赋值']
     if d['role'] in ('插入','建表写入') and writes:
      return distinct_candidates(assignment_branches(d,writes,role))
     if d['role']=='更新':
      filters=[x for x in core if x['origin']!='写入赋值']
      old=infer_conditions(d,filters,role,'UPDATE旧分区筛选候选')
      if not writes:return old
      written_fields={item['field'] for item in writes}
      unchanged=[item for item in filters if item['field'] not in written_fields]
      new=assignment_branches(d,writes,role,unchanged)
      new=[replace(item,basis='UPDATE新分区写入候选；'+item.basis) for item in new]
      return distinct_candidates(old+new)
     if not core:
      if d['role'] in ('COPY写入','COPY方向未知','插入','建表写入','更新'):status='分区值未知（写入值未完整记录）'
      elif d['role'] in ('清空','删除对象','建表','建索引','ANALYZE','注释','结构变更'):status='表级操作（未指定分区）'
      else:status='分区未知（未发现配置字段条件：'+', '.join(columns)+'）'
      return [Candidate(role,status,status)]
     return infer_conditions(d,core,role)

    G={};MAP=[]
    # Only explicitly configured partitioned tables are statistical targets.
    # Keep all parsed sources upstream so joins/lineage retain their scope.
    selected_details=[d for d in A['details'] if partition_columns(d['table']) and d.get('statistics_eligible',True)]
    for d in selected_details:
     for candidate in partition(d):
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
