import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
const root=process.env.SQL_ANALYSIS_OUTPUT_DIR??path.dirname(fileURLToPath(import.meta.url));
const data=JSON.parse(await fs.readFile(path.join(root,'report_data.json'),'utf8'));
const analysis=JSON.parse(await fs.readFile(path.join(root,'analysis.json'),'utf8'));
const profile=JSON.parse(await fs.readFile(path.join(root,'profile.json'),'utf8'));
const sqls=new Map(profile.sqls.map(x=>[x.id,x]));
const accessedTables=new Set(data.results.map(r=>r.table));
const includedIds=new Set(data.results.flatMap(r=>r.ids));
const partitionedMatches=[...accessedTables].filter(t=>data.partition_config.tables[t]).length;
const wb=Workbook.create();
const main=wb.worksheets.add('表分区访问统计');
const types=wb.worksheets.add('类型汇总与口径');
const mapping=wb.worksheets.add('SQL访问映射');
const index=wb.worksheets.add('SQL索引');
const font='Arial';
const safe=x=>typeof x==='string'?x.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,' '):x;
const short=(x,n=180)=>x.length>n?x.slice(0,n)+'…（完整描述见CSV，解析条件见右侧）':x;
const serial=s=>(Date.parse(s.slice(0,19).replace(' ','T')+'Z')-Date.UTC(1899,11,30))/86400000;
function sheetSetup(sheet,title,notes,headers,rows,widths,tableName){
 sheet.showGridLines=false;
 const last=Math.max(8,7+rows.length),cols=headers.length;
 sheet.getRangeByIndexes(0,0,last,cols).format.font={name:font,size:10,color:'#243247'};
 sheet.getRangeByIndexes(0,0,last,cols).format.verticalAlignment='center';
 sheet.getRange('A2').values=[[title]];sheet.getRange('A2').format.font={name:font,size:14,bold:true,color:'#1D3557'};
 notes.forEach((n,i)=>{sheet.getCell(i+2,0).values=[[n]];sheet.getCell(i+2,0).format.font={name:font,size:10,color:'#536579'};});
 sheet.getRangeByIndexes(6,0,1,cols).values=[headers];
 if(rows.length)sheet.getRangeByIndexes(7,0,rows.length,cols).values=rows.map(r=>r.map(safe));
 const table=sheet.tables.add(`A7:${String.fromCharCode(64+cols)}${last}`,true,tableName);
 table.showFilterButton=true;
 sheet.getRangeByIndexes(6,0,1,cols).format={fill:'#254564',font:{name:font,bold:true,color:'#FFFFFF'},rowHeight:34,wrapText:true,horizontalAlignment:'center',verticalAlignment:'center'};
 sheet.getRangeByIndexes(7,0,Math.max(1,rows.length),cols).format.rowHeight=32;
 widths.forEach((w,i)=>{sheet.getRangeByIndexes(0,i,last,1).format.columnWidth=w;});
 sheet.freezePanes.freezeRows(7);
 return last;
}
const mainRows=data.results.map(r=>[r.table,r.table_partition_info,short(r.partition),r.role,r.count,null,serial(r.first),serial(r.last),r.basis,r.category,r.group_id,r.ids.join(', '),r.raw_partition]);
let last=sheetSetup(main,'SQL 表与分区访问统计',[
 `日志范围：${data.metrics.time_min} 至 ${data.metrics.time_max}；按全部日志计数，重复 FETCH 计入。`,
 `仅统计用户清单中明确有分区的表：${partitionedMatches} 张。SQL未指定schema时不自动补全。`,
 '默认按表名、首次访问时间排序。同条日志命中同表、同分区、同类型计 1 次；IN 多个日期各计 1 次，范围和动态值保留条件。'
],['表名','表分区信息','分区或条件摘要','访问类型','访问次数','平均相邻间隔（秒）','首次访问（CST）','最后访问（CST）','判断依据','对象类别','统计组ID','关联SQL编号','解析条件或分区操作'],mainRows,[48,42,48,23,15,22,24,24,44,30,14,42,100],'AccessStats');
main.tabColor='#254564';
main.getRange(`E8:E${last}`).setNumberFormat('#,##0');main.getRange(`F8:F${last}`).setNumberFormat('0.000000');main.getRange(`G8:H${last}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
if(data.results.length)main.getRange(`F8:F${last}`).formulas=data.results.map((_,i)=>[`=IF(E${i+8}>1,(H${i+8}-G${i+8})*86400/(E${i+8}-1),"")`]);
main.getRange(`B8:D${last}`).format.wrapText=true;
main.getRange(`I8:I${last}`).format.wrapText=true;
for(let i=0;i<data.results.length;i++){const l=Math.max(mainRows[i][2].length*1.7,mainRows[i][8].length*1.7);if(l>38)main.getRange(`A${i+8}:M${i+8}`).format.rowHeight=Math.min(150,30+Math.ceil(l/40)*14);}
const typeRows=data.types.map(x=>[x.role,x.logs,x.tables,x.table_access_count,x.partition_groups]);
sheetSetup(types,'访问类型汇总',[
 `源日志 ${data.metrics.source_rows.toLocaleString()} 条；识别对象 ${data.metrics.tables.toLocaleString()} 个；统计组合 ${data.metrics.stat_groups.toLocaleString()} 组。`,
 `日志：命中目标分区表 ${data.metrics.access_log_count.toLocaleString()} 条；仅其他表 ${data.metrics.excluded_table_sql_logs.toLocaleString()} 条；无表SQL ${data.metrics.no_table_sql_logs.toLocaleString()} 条；非SQL ${(data.metrics.status_logs['非SQL日志消息']??0).toLocaleString()} 条。`,
 '一条 INSERT SELECT 日志可同时贡献写入和读取；多表/多分区访问使各行次数之和大于原始日志数。'
],['访问类型','触发该类型的日志数','涉及表数','表访问次数','表分区类型组合数'],typeRows,[25,30,20,24,29],'AccessTypes');
types.getRange(`B8:E${Math.max(8,7+typeRows.length)}`).setNumberFormat('#,##0');
const notes=[
 ['计数单位','每条原始日志独立计数，不合并重复 SQL/FETCH；同一条日志内同一统计组只计一次。'],
 ['间隔计算','按 log_timestamp 排序，相邻间隔平均值=(最后时间-首次时间)/(次数-1)。只访问一次为空白。'],
 ['时间精度','源日志为整秒；同秒多条日志的间隔记为0。平均值可小于1秒，但不代表原日志具有微秒精度。'],
 ['分区字段配置','由query_result.txt导入partition_config.json，按每张表的实际分区字段解析。只统计清单中明确有分区的表，NULL或未配置的表不纳入。'],
 ['字段识别','只解析该表配置的分区字段。不依据日期字段名或SQL中的分区操作自动认定某表有分区。'],
 ['复杂条件','元数据只有分区方式和字段，没有物理分区名或起止边界。按SQL条件统计；范围、参数、子查询、OR/NOT保留条件，不枚举物理分区。'],
 ['读写分类','INSERT/CTAS 的目标计写入，SELECT/FROM/JOIN 来源计读取；UPDATE、DELETE、COPY、表维护分别分类。'],
 ['源字段核验',`输入字段${profile.header.length}列，数据33列。按标准位置读取：第22列SQL、第1列时间、第3列用户名、第19列语句类型。`],
 ['表范围','按SQL中的schema.表名与用户清单中的分区表精确匹配。不补schema；非分区表、未指定schema和清单外对象不纳入统计。'],
 ['缺失信息','COPY正文截断时仅确认可识别的目标表；不补造缺失正文或分区值。'],
 ['逻辑引用范围','统计SQL文本直接引用的对象，不展开视图/函数/存储过程内部，不等同执行计划中的真实物理扫描。'],
 ['追溯方式',`SQL原文索引保留命中目标表的完整SQL；逐条映射仅保存命中的${data.metrics.access_log_count}条日志与组编号，源记录序号沿用输入读取顺序。`],
 ['来源',`访问日志：${profile.source?.label??'输入日志'}；分区配置：${data.partition_config.source?.path??'用户配置'}。`]
];
const start=26;types.getRange(`A${start}:B${start+notes.length-1}`).values=notes;
types.getRange(`A${start}:A${start+notes.length-1}`).format.font={bold:true,color:'#254564'};
for(let i=0;i<notes.length;i++){types.getRange(`B${start+i}:E${start+i}`).merge();types.getRange(`B${start+i}:E${start+i}`).format.wrapText=true;types.getRange(`A${start+i}:E${start+i}`).format.rowHeight=45;}
const mapRows=[];
for(const r of data.results)for(const id of r.ids){const s=sqls.get(id);mapRows.push([r.group_id,id,r.table,short(r.partition),r.role,s.count,serial(s.first_time),serial(s.last_time),s.first_record,r.raw_partition]);}
last=sheetSetup(mapping,'访问统计与原始 SQL 对应',[
 '每行是一条 SQL 正文对一个统计组的贡献。日志贡献数保留该 SQL 在原始数据中的全部出现次数。',
 '源记录序号按输入读取顺序计数；CSV不含表头，MySQL跨配置表连续编号；SQL中的换行不增加记录序号。',
 '完整 SQL 见 SQL原文索引.csv；本页用于核对分组与计数。'
],['统计组ID','SQL编号','表名','分区或条件摘要','访问类型','日志贡献数','首次访问（CST）','最后访问（CST）','首次源记录序号','解析条件或分区操作'],mapRows,[14,16,48,50,24,18,24,24,22,100],'SQLAccessMap');
mapping.getRange(`D8:D${last}`).format.wrapText=true;
for(let i=0;i<mapRows.length;i++){const l=mapRows[i][3].length*1.7;if(l>38)mapping.getRange(`A${i+8}:J${i+8}`).format.rowHeight=Math.min(150,30+Math.ceil(l/40)*14);}
mapping.getRange(`F8:F${last}`).setNumberFormat('#,##0');mapping.getRange(`G8:H${last}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');mapping.getRange(`I8:I${last}`).setNumberFormat('#,##0');
const indexRows=analysis.index.filter(x=>includedIds.has(x.id)).map(x=>{const s=sqls.get(x.id);return [x.id,s.count,s.first_record,s.types.join(', '),x.status,x.notes,s.sql.replace(/\x18/g,'\n').replace(/\s+/g,' ').slice(0,200)+(s.sql.length>200?'…（完整正文见CSV）':''),x.tables.filter(t=>accessedTables.has(t)).join(', '),s.user_name];});
last=sheetSetup(index,'SQL 日志索引',[
 'SQL 编号按正文和日志用户名共同区分；日志条数保留重复记录，不以 SQL 种类数代替访问次数。',
 '本页正文显示前200个字符，完整正文保存在 SQL原文索引.csv。',
 '仅收录引用清单内分区表的SQL；多表SQL保留完整正文用于追溯。'
],['SQL编号','原始日志条数','首次源记录序号','日志语句类型','解析状态','解析说明','SQL摘要（前200字符）','关联表','日志用户名'],indexRows,[16,20,23,24,30,60,110,80,20],'SQLIndex');
index.getRange(`B8:D${last}`).setNumberFormat('#,##0');
wb.recalculate();
const computed=data.results.length?main.getRange(`F8:F${7+data.results.length}`).values:[];
for(let i=0;i<computed.length;i++){const expected=data.results[i].mean_seconds;const got=computed[i][0];if(expected==null){if(got!==''&&got!==null)throw new Error(`single visit ${i}: ${got}`);}else if(Math.abs(Number(got)-expected)>0.000002)throw new Error(`mean mismatch ${i}: ${got} vs ${expected}`);}
console.log((await wb.inspect({kind:'table',range:'表分区访问统计!A7:H11',include:'values,formulas',tableMaxRows:5,tableMaxCols:8,maxChars:3500})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:20},maxChars:1000})).ndjson);
for(const [sheet,range,name] of [[main,'A1:H15','preview_main'],[types,'A1:E23','preview_types'],[types,'A26:E38','preview_schema_notes'],[mapping,'A1:F13','preview_mapping'],[index,'A1:E13','preview_index']]){
 const b=await wb.render({sheetName:sheet.name,range,scale:1.3,format:'png'});await fs.writeFile(path.join(root,`${name}.png`),new Uint8Array(await b.arrayBuffer()));
}
const file=await SpreadsheetFile.exportXlsx(wb);await file.save(path.join(root,'表分区访问统计.xlsx'));
console.log(JSON.stringify({exported:path.join(root,'表分区访问统计.xlsx'),groups:data.results.length,mappingRows:mapRows.length,formulasChecked:computed.length}));
