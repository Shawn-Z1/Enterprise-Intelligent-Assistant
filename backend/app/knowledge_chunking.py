"""按标题层级或递归分隔符生成含上下文的知识切片，不把标题段落单独入库。"""
import re
from dataclasses import dataclass, field

# 切片长度按 Unicode 字符数计，包含继承标题、标点与换行；分隔符按用户指定优先级递归降级。
SEPARATORS = (r'\n[ \t]*\n+', r'\n', r'[。！？]+|[.!?]+(?=\s|$)', r'[；;]+', r'[，,]+', r'[：:]+', r'[ \t]+')
STRATEGY = 'hierarchical-recursive-v2'


@dataclass
class Section:
    """标题树节点保存实际级别、父路径和原文范围；正文包含下级章节，过长时才向下细分。"""
    level: int
    title: str
    start: int
    body_start: int
    end: int
    path: list[dict]
    children: list = field(default_factory=list)


def normalized(value: str) -> str:
    """接收解析文本，统一换行符，返回完整字符串；不删除正文、标点或改变段落内容。"""
    return value.replace('\r\n', '\n').replace('\r', '\n')


def heading_level(block: dict) -> int | None:
    """接收 DOCX 解析块，从明确的标题样式/大纲级别读取级别，返回 1～9 或无标题，避免把普通段落误判标题。"""
    value = block.get('heading_level')
    if type(value) is int and 1<=value<=9:
        return value
    if block.get('style','').strip().lower() in ('title','标题'):
        return 1
    if block.get('style','').strip().lower() in ('subtitle','副标题'):
        return 2
    match = re.fullmatch(r'(?:heading|标题)\s*([1-9])', block.get('style',''), re.I)
    return int(match[1]) if match else None


def numbered_heading(text: str) -> int | None:
    """接收无样式的短独立行，识别规范章节编号并返回层级；含正文句末标点、多行或长段落不作为标题。"""
    value=text.strip()
    if not value or len(value)>100 or '\n' in value or value.endswith(('。','！','？','；','，','.','!','?',';',',')):
        return None
    match=re.fullmatch(r'(\d{1,3}(?:\.\d{1,3}){0,8})[ \t\u3000]+(\S.*)',value)
    if match:
        return len(match[1].split('.')) if re.search(r'[a-zA-Z\u4e00-\u9fff]',match[2]) else None
    if re.match(r'^第[\d一二三四五六七八九十百]+[章篇部]',value):
        return 1
    if re.match(r'^第[\d一二三四五六七八九十百]+节',value):
        return 2
    if re.match(r'^[一二三四五六七八九十百]+[、．.]\s*\S',value):
        return 1
    return None


def source_text(parsed: dict) -> tuple[str, list[tuple[int,int,int]], list[tuple[int,int,int,str]]]:
    """接收原件解析，返回规范化全文、各 PDF 页的真实范围和 DOCX 标题事件；跨页合并仍保留所有页码。"""
    if parsed['format']=='pdf':
        texts, pages, cursor = [], [], 0
        for page in parsed.get('pages',[]):
            value=normalized(page['text'])
            if texts:
                texts.append('\n\n');cursor+=2
            pages.append((cursor,cursor+len(value),page['page']))
            texts.append(value);cursor+=len(value)
        return ''.join(texts), pages, []
    if parsed['format']=='docx':
        texts, headings, cursor = [], [], 0
        # 连续多处规范编号才启用无样式层级识别，适配原件中的“8 / 8.1 / 8.2”，不猜测任意短句。
        infer_numbers=sum(numbered_heading(block.get('text','')) is not None for block in parsed.get('blocks',[]) if block['type']!='table')>=2
        for block in parsed.get('blocks',[]):
            value=normalized('\n'.join('\t'.join(row) for row in block['rows']) if block['type']=='table' else block.get('text',''))
            if not value.strip():
                continue
            if texts:
                texts.append('\n\n');cursor+=2
            level=heading_level(block) or (numbered_heading(value) if infer_numbers else None)
            display=('#'*level+' '+value.strip()) if level else value
            if level:
                headings.append((cursor,cursor+len(display),level,value.strip()))
            texts.append(display);cursor+=len(display)
        return ''.join(texts), [], headings
    return normalized(parsed.get('text','')), [], []


def markdown_headings(text: str) -> list[tuple[int,int,int,str]]:
    """接收 Markdown 全文，识别 ATX/Setext 标题并排除围栏及缩进代码，返回标题实际范围、级别和名称。"""
    events, lines, cursor, fence = [], [], 0, None
    for raw in text.splitlines(keepends=True):
        line=raw.rstrip('\n')
        token=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
        if token:
            marker=token[1]
            if fence is None:
                fence=(marker[0],len(marker))
            elif marker[0]==fence[0] and len(marker)>=fence[1] and not line[token.end():].strip():
                fence=None
            lines.append((cursor,line,True));cursor+=len(raw);continue
        if fence is None and not line.startswith(('    ','\t')):
            match=re.match(r'^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$',line)
            if match:
                title=re.sub(r'[ \t]+#+[ \t]*$','',match[2]).strip()
                if title:
                    events.append((cursor,cursor+len(line),len(match[1]),title))
            elif re.fullmatch(r' {0,3}(?:={3,}|-{3,})[ \t]*',line) and lines:
                previous_start,previous,blocked=lines[-1]
                if previous.strip() and not blocked and not any(item[0]==previous_start for item in events):
                    events.append((previous_start,cursor+len(line),1 if line.lstrip().startswith('=') else 2,previous.strip()))
        lines.append((cursor,line,fence is not None));cursor+=len(raw)
    return events


def looks_like_heading(text: str) -> bool:
    """接收短段落，保守识别没有正文的显式/编号标题，返回是否疑似孤立标题，用于合并而非生成知识条目。"""
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(len(line)<=100 and (re.match(r'^#{1,9}\s+',line) or numbered_heading(line) is not None) for line in lines)


def recursive_ranges(text: str, start: int, end: int, budget: int, level: int=0) -> list[tuple[int,int]]:
    """接收全文范围、正文预算及分隔符级别，先合并相邻短块，只将超长块降级拆分，返回有序且不丢正文的范围。"""
    if len(text[start:end].strip())<=budget:
        return [(start,end)] if text[start:end].strip() else []
    if level==len(SEPARATORS):
        return [(offset,min(offset+budget,end)) for offset in range(start,end,budget)]
    matches=list(re.finditer(SEPARATORS[level],text[start:end]))
    if level==len(SEPARATORS)-1:
        # 最后一级空格也不能把“8.2 软件要求”拆成编号与标题；标题行内部的空格不作为边界。
        heading_spans,cursor=[],start
        for line in text[start:end].splitlines(keepends=True):
            if looks_like_heading(line):
                heading_spans.append((cursor,cursor+len(line.rstrip('\n'))))
            cursor+=len(line)
        filtered,heading_index=[],0
        for match in matches:
            offset=start+match.start()
            while heading_index<len(heading_spans) and heading_spans[heading_index][1]<=offset:
                heading_index+=1
            if heading_index==len(heading_spans) or offset<heading_spans[heading_index][0]:
                filtered.append(match)
        matches=filtered
    if not matches:
        return recursive_ranges(text,start,end,budget,level+1)
    boundaries=[start]+[start+match.end() for match in matches]+[end]
    result, pending = [], None
    for left,right in zip(boundaries,boundaries[1:]):
        if not text[left:right].strip():
            if pending:
                pending=(pending[0],right)
            continue
        if len(text[left:right].strip())>budget:
            # 标题缓冲必须带上后续正文降级拆分，不能在长段落之前被单独刷新成一个切片。
            if pending and looks_like_heading(text[pending[0]:pending[1]]):
                left=pending[0];pending=None
            if pending:
                result.append(pending);pending=None
            result.extend(recursive_ranges(text,left,right,budget,level+1))
        elif pending and len(text[pending[0]:right].strip())>budget:
            if looks_like_heading(text[pending[0]:pending[1]]):
                result.extend(recursive_ranges(text,pending[0],right,budget,level+1));pending=None
            else:
                result.append(pending);pending=(left,right)
        else:
            pending=(pending[0] if pending else left,right)
    if pending:
        if looks_like_heading(text[pending[0]:pending[1]]) and result:
            # 文末空标题归入上一片；需要时重新分配上一片尾部正文，避免标题单独成片或突破上限。
            left,right=result.pop()[0],pending[1]
            if len(text[left:right].strip())<=budget:
                result.append((left,right))
            else:
                cut=max(left+1,pending[0]-min(64,budget//4))
                result.extend(recursive_ranges(text,left,cut,budget,level+1))
                result.append((cut,right))
        else:
            result.append(pending)
    return result


def title_prefix(path: list[dict]) -> str:
    """接收真实父标题路径，按级别依次写出全部标题，返回置于切片正文前的完整上下文。"""
    return '\n'.join('#'*item['level']+' '+item['title'] for item in path)+'\n\n' if path else ''


def split_content(parsed: dict, size: int=600, overlap: int=0) -> list[dict]:
    """接收解析结果和总字符上限，规范文档逐级按标题切、非规范全文递归分隔合并；返回正文/层级/页码完整的切片，旧 overlap 参数不再重复正文。"""
    if not 100<=size<=600:
        raise ValueError('切片总长度必须在 100～600 字符内')
    text,pages,headings=source_text(parsed)
    if parsed['format'] in ('md','txt'):
        headings=markdown_headings(text)
        if not headings:
            numbered,cursor=[],0
            for raw in text.splitlines(keepends=True):
                level=numbered_heading(raw.rstrip('\n'))
                if level:
                    numbered.append((cursor,cursor+len(raw.rstrip('\n')),level,raw.strip()))
                cursor+=len(raw)
            if len(numbered)>=2:
                headings=numbered
    root=Section(0,'',0,0,len(text),[])
    stack=[root]
    for start,body_start,level,title in headings:
        while len(stack)>1 and stack[-1].level>=level:
            stack.pop().end=start
        parent=stack[-1]
        node=Section(level,title,start,body_start,len(text),parent.path+[{'level':level,'title':title}])
        parent.children.append(node);stack.append(node)
    pieces=[]
    nodes=[]
    def collect(node: Section):
        """接收标题树节点，收集片内各小节的实际路径；多个兄弟节不能拼成错误的单一祖先链。"""
        for child in node.children:
            nodes.append(child);collect(child)
    collect(root)

    def has_body(start: int,end: int) -> bool:
        """接收正文范围，忽略其中的真实标题事件，返回是否含非空正文，避免只有标题的节点入库。"""
        cursor=start
        for left,right,_,_ in headings:
            if right<=start or left>=end:
                continue
            if text[cursor:max(cursor,left)].strip():
                return True
            cursor=max(cursor,right)
        return bool(text[cursor:end].strip())

    def emit(start: int,end: int,path: list[dict]):
        """接收实际正文范围和继承路径，生成含全部父标题的切片及 PDF 页集合，无正文时不生成条目。"""
        body=text[start:end].strip()
        if not body or not has_body(start,end) or not headings and looks_like_heading(body):
            return
        content=title_prefix(path)+body
        if len(content)>size:
            raise ValueError('切片正文与继承标题超过限制')
        page_numbers=[number for left,right,number in pages if left<end and right>start and text[max(left,start):min(right,end)].strip()]
        section_paths=[path] if path else []
        section_paths.extend(node.path for node in nodes if start<=node.start<end and node.path not in section_paths)
        pieces.append({'position':len(pieces),'page_number':page_numbers[0] if page_numbers else None,'page_numbers':page_numbers,
            'heading':' > '.join(item['title'] for item in path) or None,'heading_path':path,
            'section_paths':section_paths,
            'body_content':body,'content':content,'chunk_strategy':STRATEGY,'identifiers':[]})

    def leaf(start: int,end: int,path: list[dict]):
        """接收没有可继续下钻标题的正文，扣除完整标题预算后递归分隔，返回时逐片继承相同上下文。"""
        if not text[start:end].strip():
            return
        budget=size-len(title_prefix(path))
        if budget<16 and text[start:end].strip():
            raise ValueError('标题层级本身过长，无法在 600 字内保留完整标题和正文')
        for left,right in recursive_ranges(text,start,end,budget):
            emit(left,right,path)

    def visit(node: Section):
        """接收章节节点，整章不超上限则保留；超长才按下一级标题拆分，叶正文仍按分隔符递归降级。"""
        if not has_body(node.body_start,node.end):
            return
        if len(title_prefix(node.path))+len(text[node.body_start:node.end].strip())<=size:
            emit(node.body_start,node.end,node.path)
        elif node.children:
            leaf(node.body_start,node.children[0].start,node.path)
            for child in node.children:
                visit(child)
        else:
            leaf(node.body_start,node.end,node.path)

    if root.children:
        leaf(0,root.children[0].start,[])
        for child in root.children:
            visit(child)
    else:
        leaf(0,len(text),[])
    if not pieces:
        raise ValueError('文档没有可索引正文，不能把空标题当作知识')
    # 编号/date 由索引层抽取；模块保持纯文本处理，独立测试无需数据库或检索依赖。
    return pieces
