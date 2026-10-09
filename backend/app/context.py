from app.model_gateway import GatewayError, ModelSlot, estimate_tokens
from app.timezones import beijing_now


class ContextAssembler:
    """M04 普通文本上下文：保留已完成历史和当前输入，超窗明确拒绝；自动压缩在 M10 接入。"""

    def __init__(self, rows: list[dict], run_id: str, output_tokens: int):
        """接收本人有序消息、当前执行编号和输出预留，保存上下文来源，不修改原始消息。"""
        self.rows,self.run_id,self.output_tokens = rows,run_id,output_tokens
        self.timestamp = beijing_now().isoformat()

    def assemble(self, slot: ModelSlot) -> list[dict]:
        """接收目标槽位，按其窗口重新组装完整成功轮次和本次输入；返回标准消息，超预算不调用模型。"""
        messages = [{"role":"system","content":"你是知行企业智能助理，用中文清晰回答。工作台已支持知识文件提交、审核与预览，但对话尚未接入企业知识检索或业务数据库工具，不得声称已经查询或执行这些功能。所有日期和时间使用北京时间（Asia/Shanghai，UTC+08:00）。当前北京时间："+self.timestamp}]
        successful = {row['run_id'] for row in self.rows if row['role']=='assistant' and row.get('run_id')}
        for row in self.rows:
            if row.get('run_id') and row['run_id']!=self.run_id and row['run_id'] not in successful:
                continue
            value = {"role":row['role'],"content":row['content']}
            protocol = row.get('protocol_json') or {}
            if row['role']=='assistant':
                value.update({key:protocol.get('message',{}).get(key) for key in ('reasoning_content','tool_calls') if key in protocol.get('message',{})})
                value['_provider'] = protocol.get('provider')
            messages.append(value)
        # 预算包括真实协议、系统提示和输出预留，并保留 20% 安全余量；备用槽位重新计算。
        adapted = slot.request(messages,self.output_tokens)['messages']
        if estimate_tokens(adapted)+self.output_tokens > int(slot.window*0.8):
            raise GatewayError("CONTEXT_TOO_LARGE","这段会话超过当前模型的安全上下文范围，请新建会话或缩短输入")
        return messages
