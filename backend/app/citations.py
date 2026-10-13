"""只从Markdown叙述识别来源标号，代码中的数组下标不参与引用审核。"""
import re


def citation_labels(text):
    """接收最终Markdown文本，排除围栏代码和行内代码后返回来源标号；不执行或改写原答案。"""
    lines=[];fence=None
    for line in text.splitlines():
        marker=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if marker:
            token=marker[1]
            if fence is None:fence=token
            elif token[0]==fence[0] and len(token)>=len(fence):fence=None
            continue
        if fence is None:lines.append(line)
    prose='\n'.join(lines)
    prose=re.sub(r'(`+).*?\1','',prose,flags=re.S)
    return re.findall(r'\[(\d+)\]',prose)
