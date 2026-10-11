"""只在独立Docker任务容器执行的固定文件检查与代码入口；应用后端不导入此模块。"""
import csv
import io
import json
import math
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd
import openpyxl

MAX_CELLS=1_000_000
MAX_ROWS=200_000


def load_frames(files):
    """接收固定输入清单，在隔离客体验证并解析CSV/XLSX；返回数据框和真实检查元数据。"""
    frames,metadata={},[]
    cells=0
    for index,item in enumerate(files):
        path=Path('/input')/item['path']
        sheets=[]
        if item['format']=='csv':
            raw=path.read_bytes()
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('gb18030')
            sheets=[('CSV',csv.reader(io.StringIO(text)))]
        else:
            with zipfile.ZipFile(path) as archive:
                entries=archive.infolist()
                if len(entries)>2000 or sum(x.file_size for x in entries)>100*1024*1024:raise ValueError('XLSX解压规模超限')
                for entry in entries:
                    if entry.file_size>max(1024*1024,entry.compress_size*200) or 'vba' in entry.filename.lower() or 'externallinks/' in entry.filename.lower():raise ValueError('不支持宏、外链或异常压缩文件')
                    if entry.filename.endswith('.rels') and b'TargetMode="External"' in archive.read(entry):raise ValueError('不支持外部链接')
            workbook=openpyxl.load_workbook(path,read_only=True,data_only=False,keep_links=False)
            sheets=[(sheet.title,sheet.iter_rows(values_only=True)) for sheet in workbook.worksheets]
        for sheet_index,(sheet_name,iterator) in enumerate(sheets):
            iterator=iter(iterator)
            header=next(iterator,[])
            columns=[str(v) if v is not None else f'column_{i+1}' for i,v in enumerate(header)]
            if not columns or len(columns)>2000 or len(set(columns))!=len(columns):raise ValueError('空表、重复列名或列数超限')
            rows=[]
            for row in iterator:
                cells+=len(columns)
                if cells>MAX_CELLS or len(rows)>=MAX_ROWS:raise ValueError('文件超过100万单元格或20万行限制')
                values=list(row)
                if any(isinstance(v,str) and v.startswith('=') for v in values) and item['format']=='xlsx':raise ValueError('不执行Excel公式，请先导出为值')
                if any(isinstance(v,str) and len(v)>10000 for v in values):raise ValueError('单元格文本超限')
                rows.append((values+[None]*len(columns))[:len(columns)])
            frame=pd.DataFrame(rows,columns=columns)
            for column in columns:
                converted=pd.to_numeric(frame[column].replace('',None),errors='coerce')
                if converted.notna().sum()==frame[column].replace('',None).notna().sum():frame[column]=converted
            table=f'f{index+1}_s{sheet_index+1}'
            frames[table]=frame
            metadata.append({'attachment_id':item['attachment_id'],'file':item['name'],'sheet':sheet_name,'table':table,'columns':columns,
                             'types':{c:str(frame[c].dtype) for c in columns},'row_count':len(frame),'missing':{c:int(frame[c].isna().sum()) for c in columns},
                             'sample':json.loads(frame.head(3).to_json(orient='records',date_format='iso',force_ascii=False))})
    return frames,metadata


def emit_result(rows,chart=None,description=''):
    """接收模型代码计算出的记录、图表字段和口径，保存JSON；真实图表及安全CSV由固定入口生成。"""
    if isinstance(rows,pd.DataFrame):rows=json.loads(rows.to_json(orient='records',date_format='iso',force_ascii=False))
    Path('/output/result.json').write_text(json.dumps({'rows':rows,'chart':chart or {},'description':description},ensure_ascii=False,allow_nan=False),encoding='utf-8')


def finish_result(metadata):
    """验证客体结果结构并导出固定CSV/PNG；返回有限模型观察，完整有限产物由节点收集。"""
    path=Path('/output/result.json')
    if not path.exists() or path.stat().st_size>2*1024*1024:raise ValueError('代码须调用emit_result，结果不得超过2MiB')
    value=json.loads(path.read_text(encoding='utf-8'))
    rows=value.get('rows')
    if not isinstance(rows,list) or len(rows)>200000 or any(not isinstance(r,dict) for r in rows):raise ValueError('结果必须为记录列表')
    columns=list(rows[0]) if rows else []
    if len(columns)>100 or any(set(r)!=set(columns) for r in rows):raise ValueError('结果字段不一致或超限')
    for row in rows:
        if any(not isinstance(v,(str,int,float,bool,type(None))) or isinstance(v,float) and not math.isfinite(v) for v in row.values()):raise ValueError('结果单元格须为有限标量')
    with Path('/output/result.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(["'"+c if c.startswith(('=','+','-','@')) else c for c in columns])
        for row in rows:writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v for v in row.values()])
    chart=value.get('chart') or {}
    points=[]
    if chart.get('type') in ('bar','line'):
        x,y=chart.get('x'),chart.get('y')
        if x not in columns or y not in columns:raise ValueError('图表字段不在结果中')
        for row in rows[:20]:
            if not isinstance(row[y],(float,int)) or isinstance(row[y],bool):raise ValueError('图表纵轴必须为数值')
            points.append({'label':str(row[x])[:120],'value':row[y]})
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif']=['Noto Sans CJK JP','DejaVu Sans']
        plt.rcParams['axes.unicode_minus']=False
        fig,ax=plt.subplots(figsize=(8,4))
        labels=[p['label'] for p in points];numbers=[p['value'] for p in points]
        (ax.bar if chart['type']=='bar' else ax.plot)(labels,numbers)
        ax.set_title(str(chart.get('title',''))[:120])
        ax.set_ylabel(str(chart.get('unit',''))[:40])
        ax.tick_params(axis='x',labelrotation=30)
        fig.tight_layout();fig.savefig('/output/chart.png');plt.close(fig)
    return {'rows':rows[:200],'columns':columns,'row_count':len(rows),'truncated':len(rows)>200,'chart':{**chart,'points':points,'partial':len(rows)>20} if points else None,
            'description':str(value.get('description',''))[:2000],'files':metadata,'artifacts':['result.csv']+(['chart.png'] if points else [])}


def main():
    """读取只读控制清单，执行固定inspect或经批准的Python/DuckDB；将回执写入独立固定文件。"""
    request=json.loads(Path('/input/request.json').read_text())
    try:
        frames,metadata=load_frames(request['files'])
        if request['operation']=='inspect':result={'files':metadata,'message':'已检查完整文件，sample仅前3行，不作为全量统计'}
        elif request['language']=='python':
            namespace={'pd':pd,'tables':frames,'emit_result':emit_result,'__name__':'__main__'}
            exec(compile(request['code'],'<approved-code>','exec'),namespace)
            result=finish_result(metadata)
        else:
            import duckdb
            import sqlglot
            from sqlglot import exp
            statements=sqlglot.parse(request['code'],read='duckdb')
            if len(statements)!=1 or not isinstance(statements[0],(exp.Select,exp.Union)):raise ValueError('文件SQL只允许单条只读查询')
            connection=duckdb.connect(config={'enable_external_access':False,'autoload_known_extensions':False,'autoinstall_known_extensions':False,'threads':2,'memory_limit':'512MB'})
            for name,frame in frames.items():connection.register(name,frame)
            data=connection.execute(request['code']).fetchdf()
            emit_result(data,request.get('chart',{}),'DuckDB仅查询本任务文件表，外部访问和扩展安装关闭')
            result=finish_result(metadata)
        receipt={'ok':True,'result':result}
    except Exception as error:
        receipt={'ok':False,'error':{'code':'SANDBOX_CODE_ERROR','message':str(error)[:2000]}}
    Path('/output/receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    Path('/output/.ready').write_text('ready')
    # 保留只读输入和tmpfs产物直到控制器收集并销毁；该进程不复用于下一个任务。
    import time
    while True:time.sleep(1)


if __name__=='__main__':main()
