import unittest
from app.knowledge_chunking import split_content,recursive_ranges


def parsed(text,kind='md'):
    """接收测试正文和格式，返回纯解析夹具，算法验收不伪造检索或文件解析结果。"""
    return {'format':kind,'text':text,'blocks':[{'type':'text','text':text}]}


class ChunkingTests(unittest.TestCase):
    """按实际用户规则验证标题下钻、继承、递归合并、跨页及完整覆盖，而非照抄实现结果。"""
    def assert_chunks(self,rows,maximum=600):
        """接收输出片段和上限，统一核验字数、顺序、正文和层级前缀，返回无值。"""
        self.assertTrue(rows)
        self.assertEqual([row['position'] for row in rows],list(range(len(rows))))
        self.assertTrue(all(0<len(row['content'])<=maximum and row['body_content'].strip() for row in rows))
        for row in rows:
            for title in row['heading_path']:
                self.assertIn(title['title'],row['content'])

    def test_small_level_one_sections_remain_separate_with_children(self):
        """每个一级标题单独成片，不因短小合并跨章；章节不超长时不继续拆短二级和三级节。"""
        rows=split_content(parsed('# 第一章\n概述。\n## 范围\n部门适用。\n### 例外\n例外说明。\n# 第二章\n执行要求。'))
        self.assert_chunks(rows)
        self.assertEqual(len(rows),2)
        self.assertIn('### 例外',rows[0]['body_content'])
        self.assertEqual([item['title'] for item in rows[0]['section_paths'][-1]],['第一章','范围','例外'])
        self.assertEqual(rows[1]['heading_path'],[{'level':1,'title':'第二章'}])

    def test_long_level_one_splits_at_level_two(self):
        """一级章节超过600字时仅按二级切，两块分别重复章名与小节名，绝不只保留最近标题。"""
        rows=split_content(parsed('# 第二章 总体要求\n## 2.1 硬件\n'+'甲'*340+'\n## 2.2 软件\n'+'乙'*340))
        self.assert_chunks(rows)
        self.assertEqual(len(rows),2)
        self.assertEqual([item['title'] for item in rows[1]['heading_path']],['第二章 总体要求','2.2 软件'])
        self.assertTrue(rows[1]['content'].startswith('# 第二章 总体要求\n## 2.2 软件\n\n'))

    def test_level_three_only_splits_if_level_two_is_long(self):
        """长二级节继续按三级下钻；短二级节连同其三级节保持整片，不无条件按每个标题碎切。"""
        text='# 第二章\n## 2.3 安全\n### 2.3.1 权限\n'+'甲'*330+'\n### 2.3.2 日志\n'+'乙'*330+'\n## 2.4 运维\n### 2.4.1 监控\n指标说明。'
        rows=split_content(parsed(text))
        self.assert_chunks(rows)
        self.assertEqual(len(rows),3)
        self.assertEqual(len(rows[0]['heading_path']),3)
        self.assertEqual(len(rows[-1]['heading_path']),2)
        self.assertIn('### 2.4.1 监控',rows[-1]['body_content'])

    def test_deep_and_skipped_heading_levels_are_inherited(self):
        """标题跳级仍继承真实父标题，继续下钻到四/五级，不凭空添加缺失标题。"""
        text='# 第一章\n### 三级节\n#### 四级节\n##### 五级甲\n'+'甲'*320+'\n##### 五级乙\n'+'乙'*320
        rows=split_content(parsed(text))
        self.assert_chunks(rows)
        self.assertEqual([item['level'] for item in rows[0]['heading_path']],[1,3,4,5])

    def test_docx_headings_are_attached_to_body_and_paragraphs_merge(self):
        """真实 DOCX 标题样式单独段落不独立成片；同节多个正文段落合并，复现软件要求标题问题。"""
        blocks=[{'type':'paragraph','style':'Heading 1','text':'第八章 技术要求'},
                {'type':'paragraph','style':'Heading 2','text':'8.1 硬件要求'},{'type':'paragraph','text':'甲'*340},
                {'type':'paragraph','style':'Heading 2','text':'8.2 软件要求'},{'type':'paragraph','text':'操作系统应支持指定环境。'},
                {'type':'paragraph','text':'客户端应支持统一登录。'},{'type':'table','rows':[['组件','版本'],['数据库','17']]}]
        rows=split_content({'format':'docx','blocks':blocks,'text':'未用于分块的原始预览'})
        self.assert_chunks(rows)
        self.assertEqual(len(rows),1)
        self.assertIn('8.2 软件要求',rows[0]['content'])
        self.assertIn('客户端应支持统一登录',rows[0]['content'])
        self.assertIn('数据库\t17',rows[0]['content'])

    def test_no_heading_only_chunks_even_when_empty_subsections_exist(self):
        """空标题节不单独入库，标题和正文预算一起计算，即使长正文降级分片也重复完整祖先路径。"""
        rows=split_content(parsed('# 第八章\n## 8.1 空节\n## 8.2 软件要求\n'+'正文要求。'*170))
        self.assert_chunks(rows)
        self.assertTrue(all('正文要求' in row['body_content'] for row in rows))
        self.assertTrue(all('第八章' in row['content'] and '8.2 软件要求' in row['content'] for row in rows))

    def test_docx_without_word_styles_uses_standard_chapter_numbers(self):
        """复现实际原件样式为空：规范8/8.1/8.2编号仍组成标题树，标题不会脱离正文，继承真实上级名称。"""
        blocks=[{'type':'paragraph','style':'','text':'8 技术要求'}, {'type':'paragraph','style':'','text':'8.1 硬件要求'},
            {'type':'paragraph','style':'','text':'甲'*340+'。'}, {'type':'paragraph','style':'','text':'8.2 软件要求'},
            {'type':'paragraph','style':'','text':'乙'*340+'。'}]
        rows=split_content({'format':'docx','blocks':blocks})
        self.assert_chunks(rows)
        self.assertEqual(len(rows),2)
        self.assertEqual([item['title'] for item in rows[1]['heading_path']],['8 技术要求','8.2 软件要求'])

    def test_numbered_sentences_are_not_headings_and_word_title_is_not_alone(self):
        """以编号起始的完整句子仍是正文；DOCX文档标题没有正文时不产生独立标题切片。"""
        rows=split_content(parsed('1 审批需要提交申请。\n2 报销需要原始发票。','txt'))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['heading_path'],[])
        rows=split_content({'format':'docx','blocks':[{'type':'paragraph','style':'Title','text':'企业制度'},
            {'type':'paragraph','style':'Heading 1','text':'第一章'},{'type':'paragraph','text':'正文规则。'}]})
        self.assertEqual(len(rows),1)
        self.assertIn('正文规则',rows[0]['content'])

    def test_word_outline_metadata_overrides_missing_style_names(self):
        """Word仅存大纲级别而无样式名时，父标题和子标题仍按真实级别保留，兼容实际原件的结构。"""
        blocks=[{'type':'paragraph','style':'','heading_level':1,'text':'系统介绍'},
            {'type':'paragraph','style':'','heading_level':2,'text':'八、部署环境要求'},
            {'type':'paragraph','style':'','heading_level':3,'text':'8.1 硬件要求'},{'type':'paragraph','text':'甲'*340+'。'},
            {'type':'paragraph','style':'','heading_level':3,'text':'8.2 软件要求'},{'type':'paragraph','text':'乙'*340+'。'}]
        rows=split_content({'format':'docx','blocks':blocks})
        self.assertEqual(len(rows),2)
        self.assertEqual([item['title'] for item in rows[-1]['heading_path']],['系统介绍','八、部署环境要求','8.2 软件要求'])

    def test_fenced_and_indented_code_do_not_create_sections(self):
        """代码围栏中的井号不是标题，也不能从上一个代码块继承出虚假的章节路径。"""
        text='# 正式章\n说明。\n```python\n# 代码注释\n## 代码内容\n```\n    # 缩进代码\n# 第二章\n结束正文。'
        rows=split_content(parsed(text))
        self.assertEqual(len(rows),2)
        self.assertEqual([item['title'] for item in rows[0]['heading_path']],['正式章'])
        self.assertIn('# 代码注释',rows[0]['body_content'])

    def test_setext_headings_and_plain_text_markdown(self):
        """规范 Setext 标题及 TXT 中的 Markdown 标题遵循同一标题树，不依赖扩展名伪装正文。"""
        rows=split_content(parsed('第一章\n===\n正文甲。\n第二章\n===\n正文乙。','txt'))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['heading_path'][0]['title'],'第一章')

    def test_paragraph_40_60_then_700_recurses_in_priority_order(self):
        """40和60字的段落先合并；700字的段落只在内部按换行再拆，前两个不被重新拆散。"""
        text='甲'*40+'\n\n'+'乙'*60+'\n\n'+'丙'*350+'\n'+'丁'*350
        rows=split_content(parsed(text,'txt'))
        self.assert_chunks(rows)
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]['body_content'],'甲'*40+'\n\n'+'乙'*60)
        self.assertEqual([len(row['body_content']) for row in rows],[102,350,350])

    def test_sentences_then_commas_then_characters(self):
        """分隔符逐级降级；先完整合并句子，不够才按逗号，再无分隔符时使用最后字符兜底。"""
        cases=[('甲'*350+'。'+'乙'*350+'。',[351,351]),('甲'*350+'，'+'乙'*350+'，',[351,351]),('甲'*1300,[600,600,100])]
        for text,lengths in cases:
            rows=split_content(parsed(text,'txt'))
            self.assert_chunks(rows)
            self.assertEqual([len(row['body_content']) for row in rows],lengths)
            self.assertEqual(''.join(row['body_content'] for row in rows),text)

    def test_pdf_pages_merge_and_all_provenance_is_preserved(self):
        """PDF页边界不强制产生小片；不足600的跨页内容合并，页集合与初始预览页准确保留。"""
        rows=split_content({'format':'pdf','pages':[{'page':1,'text':'甲'*40},{'page':2,'text':'乙'*60},{'page':3,'text':'丙'*700}]})
        self.assert_chunks(rows)
        self.assertEqual(rows[0]['page_numbers'],[1,2])
        self.assertEqual(rows[0]['page_number'],1)
        self.assertEqual(rows[-1]['page_numbers'],[3])

    def test_plain_numbered_heading_does_not_become_orphan_before_big_paragraph(self):
        """非标准纯文本中的软件要求标题遇到超长正文时一起降级拆，不先输出只有编号的一片。"""
        rows=split_content(parsed('8.2 软件要求\n'+'内容'*350,'txt'))
        self.assert_chunks(rows)
        self.assertEqual(len(rows),2)
        self.assertIn('8.2 软件要求',rows[0]['content'])
        self.assertTrue(all('内容' in row['body_content'] for row in rows))

    def test_content_is_not_dropped_duplicated_or_overlapped(self):
        """含多种标点与换行的全文切片保持所有非空白字符顺序，旧 overlap 参数不会重复正文。"""
        text=('条款甲'*50+'。\n条款乙'*35+'，备注。\n\n')*12
        rows=split_content(parsed(text,'txt'),600,50)
        self.assert_chunks(rows)
        compact=lambda value:''.join(value.split())
        self.assertEqual(compact(''.join(row['body_content'] for row in rows)),compact(text))

    def test_limit_counts_inherited_titles_and_rejects_title_only_document(self):
        """600字包含重复的父标题，纯标题文档不能伪装成功知识；过长层级明确拒绝而非截断标题。"""
        rows=split_content(parsed('# '+('标题'*40)+'\n## 小节\n'+'正'*1100))
        self.assert_chunks(rows)
        self.assertTrue(all(len(row['body_content'])<600 for row in rows))
        with self.assertRaises(ValueError):split_content(parsed('# 第一章\n## 空标题'))
        with self.assertRaises(ValueError):split_content(parsed('# '+'标题'*300+'\n正文。'*20))


if __name__=='__main__':unittest.main()
