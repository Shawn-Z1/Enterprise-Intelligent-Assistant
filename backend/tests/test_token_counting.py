"""分词器变更只测试计数、预算和实际usage优先规则，不重复业务全量验收。"""
import unittest
from app.model_gateway import estimate_tokens, usage_from_response
from app.token_counting import count_text, count_request, load_tokenizer


class TokenCounting(unittest.TestCase):
    """主备官方参考分词器计数与完整请求预算的目标测试。"""
    def test_chinese_english_and_long_text(self):
        """无参数验证中文/英文/长文本按实际分词，不按字节1:1且不截断长输入。"""
        texts=['你好，企业智能助理。','Hello, this is a token counting example.','上下文预算测试 '*3000]
        for model in ('deepseek-flash','qwen3.8-flash'):
            tokenizer,_=load_tokenizer(model)
            for text in texts:
                with self.subTest(model=model,length=len(text)):
                    actual=count_text(text,model)
                    reference=len(tokenizer.encode(text,add_special_tokens=False).ids) if model=='deepseek-flash' else len(tokenizer.encode(text,allowed_special=set(),disallowed_special=()))
                    self.assertEqual(actual.count,reference)
                    self.assertLess(actual.count,len(text.encode('utf-8')))
                    self.assertEqual(actual.method,'local_reference_tokenizer_v1')
            self.assertGreater(count_text(texts[-1],model).count,512)

    def test_request_tools_and_reported_priority(self):
        """无参数验证工具schema计入预算，模型reported值保持原值，无usage才使用带方法的tokenizer估算。"""
        messages=[{'role':'user','content':'请查询公开资料'}]
        tools=[{'type':'function','function':{'name':'search_web','parameters':{'type':'object','properties':{'query':{'type':'string'}}}}}]
        for model in ('deepseek-flash','qwen3.8-flash'):
            self.assertGreater(count_request(messages,tools,model).count,count_request(messages,None,model).count)
            reported=usage_from_response({'prompt_tokens':101,'completion_tokens':7,'total_tokens':108},messages,'回答',model,tools)
            self.assertEqual((reported.source,reported.input_tokens,reported.output_tokens),('reported',101,7))
            estimated=usage_from_response(None,messages,'回答',model,tools)
            self.assertEqual(estimated.source,'estimated')
            self.assertIn('local_reference_tokenizer',estimated.method)
        self.assertGreater(estimate_tokens('中文测试','unknown-model'),estimate_tokens('中文测试','deepseek-flash'))
        self.assertEqual(usage_from_response(None,messages,'回答','unknown-model').method,'utf8_bytes_upper_bound_v1')


if __name__=='__main__':unittest.main()
