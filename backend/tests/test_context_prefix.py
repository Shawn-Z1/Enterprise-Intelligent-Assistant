"""固定提示/历史前缀与动态运行信息位置的定向测试，不发真实模型请求。"""
import unittest
from app.context import ContextAssembler
from app.model_gateway import ModelSlot


class ContextPrefix(unittest.TestCase):
    """确认时间/目录变化只发生在本轮输入后部，固定规则和既有普通历史仍保持一致。"""

    def test_runtime_changes_do_not_change_earlier_messages(self):
        """不同时间/目录构造同一普通历史，检查系统规则及旧消息相同，并保留用户原文和动态定位。"""
        rows=[{'role':'user','content':'较早的长文本'+('背景资料。'*2000),'run_id':'old'},
              {'role':'assistant','content':'已读到背景资料。','run_id':'old','protocol_json':{}},
              {'role':'user','content':'继续整理。','run_id':'current'}]
        first=ContextAssembler(rows,'current',8192,[{'attachment_id':'file-a','filename':'first.md'}])
        second=ContextAssembler(rows,'current',8192,[{'attachment_id':'file-b','filename':'second.md'}])
        first.timestamp='2026-10-12T22:00:00+08:00';second.timestamp='2026-10-12T22:01:00+08:00'
        slot=ModelSlot('primary','https://api.deepseek.com','fixture','deepseek-flash',1000000)
        a=first.assemble(slot);b=second.assemble(slot)
        self.assertEqual(a[:-1],b[:-1]);self.assertNotEqual(a[-1]['content'],b[-1]['content'])
        self.assertNotIn(first.timestamp,a[0]['content']);self.assertNotIn('first.md',a[0]['content'])
        self.assertTrue(a[-1]['content'].startswith('继续整理。'));self.assertIn(first.timestamp,a[-1]['content'])
        self.assertEqual(rows[-1]['content'],'继续整理。')


if __name__=='__main__':unittest.main()
