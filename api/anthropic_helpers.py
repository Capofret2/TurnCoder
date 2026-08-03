"""Api mixin: Anthropic protocol formatting, rendering, and SSE construction."""
import json
import uuid



class AnthropicMixin:
    """Api mixin: Anthropic protocol formatting, rendering, and SSE construction."""

    def _format_tools_as_text(self, tools):
        """将 CC 工具定义 JSON 数组转换为描述符格式的纯文本工具说明"""
        return self._format_tools_descriptor_text(tools)

    def _format_tools_descriptor_text(self, tools):
        """生成描述符格式的工具使用说明（开关ON时替代旧JSON标记格式）"""
        _planned_mode = getattr(self, 'global_settings', {}).get('enable_planned_tools', False)
        # 申请审批会让托管停下来等人，而托管的价值是无人值守连续推进，所以这个能力
        # 默认不给模型。**关闭时的做法是让它不出现在清单里，而不是调用后拒绝**：后者
        # 白费一次完整的 API 调用，而且模型收到的是一个失败的工具结果，它无从区分
        # 「这个工具被禁用了」与「我参数写错了」，下一轮很可能重试同样的调用。
        #
        # 过滤必须在下面 len(tools) 之前完成。那个数字会写进「共 N 个」，在别处过滤
        # 会让模型看到的计数与实际列出的条数不一致，而模型对此的反应是怀疑自己漏读。
        if not getattr(self, 'global_settings', {}).get('enable_approval_tool', False):
            tools = [t for t in tools if t.get('name') != '申请审批']
        lines = []
        lines.append(f"\n[可用工具 - 共 {len(tools)} 个]")
        lines.append("当你需要执行操作时，在回复正文中使用以下描述符格式调用工具（可在一次回复中包含多个）。")
        if _planned_mode:
            lines.append("")
            lines.append("工具调用分为两种类型：")
            lines.append("- **立即调用**：你现在就知道全部参数，不依赖任何其他工具的返回结果。所有立即调用并行执行，不互相等待。")
            lines.append("- **计划调用**：你预期在某些前序工具成功返回后才需要执行的操作。计划调用必须声明等待哪些前序工具完成后才执行。如果等待的任何一个工具失败，该计划调用将被自动拒绝。")
            lines.append("")
            lines.append("# 核心行为原则")
            lines.append("")
            lines.append("**每次回复必须最大化工具调用数量。多往后想几步。**")
            lines.append("")
            lines.append("写计划调用是零成本的。如果你的依赖失败了，计划调用自动被拒绝，没有任何坏处。所以你应该大胆地为未来规划：")
            lines.append("- 编辑了文件？立刻写一个计划调用去运行它、读取它、或验证它")
            lines.append("- 运行了一个命令？立刻写一个计划调用去读取它的输出文件")
            lines.append("- 安装了一个库？立刻写一个计划调用去使用它")
            lines.append("- 创建了一个文件？立刻写一个计划调用去执行语法检查")
            lines.append("")
            lines.append("**永远不要说「我要等结果出来才写下一步」。** 这是绝对禁止的。如果你知道下一步要做什么，现在就写成计划调用。不要等到下一轮回复。你现在就能写，为什么要等？等待是浪费。")
            lines.append("")
            lines.append("**不需要在 Bash 命令中用 && 串联多个操作。** 分开写多个独立的立即调用或计划调用。每个操作一个工具调用，独立、原子、可单独失败。")
            lines.append("")
            lines.append("**好的回复模式示例：**")
            lines.append("- 同时编辑 3 个文件 → 3 个立即 Edit")
            lines.append("- 编辑完后跑测试 → 1 个计划 Bash 等待全部 Edit")
            lines.append("- 测试通过后读取结果 → 1 个计划 Read 等待测试")
            lines.append("- 一共 5 个工具调用，一次回复全部写完")
            lines.append("")
            lines.append("**坏的回复模式（禁止）：**")
            lines.append("- 只写 1 个 Edit，然后说「编辑完成后我会运行测试」← 禁止！现在就写计划调用")
            lines.append("- 写 1 个 Bash 命令 sleep 5 && echo done && cat output.txt ← 禁止！拆成 3 个独立调用")
            lines.append("- 说「让我先看看结果」← 禁止！你已经知道下一步做什么，写计划调用")
            lines.append("")
            lines.append("# 格式规则")
            lines.append("")
            lines.append("- 立即调用使用 [ToolName立即开始] 和 [ToolName立即结束] 标记")
            lines.append("- 计划调用使用 [ToolName计划开始] 和 [ToolName计划结束] 标记")
            lines.append("- 计划调用的描述文字之后，第一个参数必须是 [等待参数]，值为空格分隔的工具序号（1-based），表示等待哪些前序工具成功后才执行")
            lines.append("- 每个描述符标记必须独占一行")
            lines.append("- 参数值可以多行，直到下一个标记行")
            lines.append("- 参数值的首尾各一个换行符会被自动 strip")
            lines.append("- 不要包含 type、id 或 status 字段，系统自动生成")
            lines.append("- 数值类型参数（offset/limit/timeout/count等）：值为纯数字时自动转为整数")
            lines.append("- 布尔类型参数（replace_all/run_in_background等）：写 true 或 false")
            lines.append("- 数组类型参数（allowed_domains/message_ids等）：用 JSON 数组格式如 [1, 2, 3]")
            lines.append("")
            lines.append("立即调用示例：")
            lines.append("[Read立即开始]")
            lines.append("读取配置文件")
            lines.append("[file_path参数]")
            lines.append("/path/to/config.json")
            lines.append("[Read立即结束]")
            lines.append("")
            lines.append("计划调用示例（等待第1个和第2个工具都成功后执行）：")
            lines.append("[Bash计划开始]")
            lines.append("运行修改后的脚本验证修改结果")
            lines.append("[等待参数]")
            lines.append("1 2")
            lines.append("[command参数]")
            lines.append("python -m pytest tests/ -v")
            lines.append("[Bash计划结束]")
        else:
            lines.append("格式规则：")
            lines.append("- 每个描述符标记（[Edit开始]、[param参数]、[Edit结束]）必须独占一行")
            lines.append("- 参数值可以多行，直到下一个标记行")
            lines.append("- 参数值的首尾各一个换行符会被自动 strip")
            lines.append("- 不要包含 type、id 或 status 字段，系统自动生成（ID格式为 toolu_气泡ID_序号）")
            lines.append("- 数值类型参数（offset/limit/timeout/count等）：值为纯数字时自动转为整数")
            lines.append("- 布尔类型参数（replace_all/run_in_background等）：写 true 或 false")
            lines.append("- 数组类型参数（allowed_domains/message_ids等）：用 JSON 数组格式如 [1, 2, 3]")
            lines.append("")
            lines.append("调用格式：")
            lines.append("[Edit开始]")
            lines.append("描述文本（说明调用目的，可多行）")
            lines.append("[file_path参数]")
            lines.append("/path/to/file（必填）")
            lines.append("[old_string参数]")
            lines.append("要替换的文本（必填）")
            lines.append("[new_string参数]")
            lines.append("替换后的文本（必填）")
            lines.append("[Edit结束]")
        lines.append("")
        for tool in tools:
            name = tool.get("name", "unknown")
            desc = (tool.get("description", "") or "").strip()
            if _planned_mode and desc:
                # Strip serial-execution descriptions that contradict planned mode
                _serial_phrases = [
                    '多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。如果你需要执行耗时较长的多个任务并且希望并行以减少等待时间，在同一个消息中写一个python文件并且运行来科学且现代化地实现并行。',
                    '多个工具调用总是会串行执行，只有前一个工具调用执行完毕返回结果之后下一个才会开始执行。',
                ]
                for _sp in _serial_phrases:
                    desc = desc.replace(_sp, '')
                desc = desc.strip()
            lines.append(f"**{name}**")
            if desc:
                lines.append(desc)
            schema = tool.get("input_schema", {})
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            if props:
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    pdesc = (pinfo.get("description", "") or "")
                    req_mark = "*" if pname in required else ""
                    lines.append(f"  - {pname}{req_mark} ({ptype}): {pdesc}")
            # 生成该工具的调用示例
            if props:
                lines.append(f"  示例：")
                if _planned_mode:
                    lines.append(f"  [{name}立即开始]")
                else:
                    lines.append(f"  [{name}开始]")
                lines.append(f"  你的描述（说明调用目的）")
                for pname, pinfo in props.items():
                    pdesc_short = (pinfo.get("description", "") or pname).split('.')[0].split('\n')[0][:40]
                    req_label = "必填" if pname in required else "可选"
                    lines.append(f"  [{pname}参数]")
                    lines.append(f"  {pdesc_short}（{req_label}）")
                if _planned_mode:
                    lines.append(f"  [{name}立即结束]")
                else:
                    lines.append(f"  [{name}结束]")
            lines.append("")
        lines.append("[工具列表结束]\n")
        return "\n".join(lines)


    def _render_anthropic_content(self, content):
        """将 Anthropic content blocks 渲染为可显示的文本"""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content)
        bt = chr(96) * 3
        parts = []
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                preview = thinking[:300] + "..." if len(thinking) > 300 else thinking
                parts.append("> **Thinking**\n> " + preview)
            elif btype == "tool_use":
                input_str = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
                parts.append("**" + block.get("name", "") + "** (id: " + block.get("id", "")[:20] + ")\n" + bt + "json\n" + input_str + "\n" + bt)
            elif btype == "tool_result":
                content_val = block.get("content", "")
                if isinstance(content_val, list):
                    content_val = "\n".join(b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in content_val)
                is_err = block.get("is_error", False)
                prefix = "Error" if is_err else "Result"
                tool_id = block.get("tool_use_id", "")
                preview = str(content_val)[:500]
                parts.append(prefix + " (tool: " + tool_id + ")\n" + bt + "\n" + preview + "\n" + bt)
        return "\n\n".join(parts)


    def _parse_sse_response_local(self, sse_text):
        """将Anthropic SSE 流式响应重组为完整消息对象"""
        content_blocks = {}
        model = ""
        stop_reason = ""
        usage = {}
        for line in sse_text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except Exception:
                continue
            etype = evt.get("type", "")
            if etype == "message_start":
                msg = evt.get("message", {})
                model = msg.get("model", "")
                usage = msg.get("usage", {})
            elif etype == "content_block_start":
                idx = evt.get("index", 0)
                block = evt.get("content_block", {})
                content_blocks[idx] = {"type": block.get("type", "text"), "text": "", "thinking": "", "tool_name": block.get("name", ""), "tool_id": block.get("id", ""), "input_json": "", "signature": block.get("signature", "")}
            elif etype == "content_block_delta":
                idx = evt.get("index", 0)
                delta = evt.get("delta", {})
                if idx not in content_blocks:
                    content_blocks[idx] = {"type": "text", "text": "", "thinking": "", "tool_name": "", "tool_id": "", "input_json": ""}
                dt = delta.get("type", "")
                if dt == "text_delta":
                    content_blocks[idx]["text"] += delta.get("text", "")
                elif dt == "thinking_delta":
                    content_blocks[idx]["thinking"] += delta.get("thinking", "")
                elif dt == "input_json_delta":
                    content_blocks[idx]["input_json"] += delta.get("partial_json", "")
            elif etype == "message_delta":
                d = evt.get("delta", {})
                stop_reason = d.get("stop_reason", stop_reason)
                usage.update(evt.get("usage", {}))
        content = []
        for idx in sorted(content_blocks.keys()):
            b = content_blocks[idx]
            if b["type"] == "thinking":
                entry = {"type": "thinking", "thinking": b["thinking"]}
                if b.get("signature"):
                    entry["signature"] = b["signature"]
                if b["thinking"] or b.get("signature"):
                    content.append(entry)
            elif b["type"] == "tool_use":
                tool_input = {}
                if b["input_json"]:
                    try:
                        tool_input = json.loads(b["input_json"])
                    except Exception:
                        tool_input = {"_raw": b["input_json"]}
                content.append({"type": "tool_use", "id": b["tool_id"], "name": b["tool_name"], "input": tool_input})
            elif b["text"]:
                content.append({"type": "text", "text": b["text"]})
        if not content:
            return None
        return {"type": "message", "role": "assistant", "model": model, "content": content, "stop_reason": stop_reason, "usage": usage}



    def _construct_anthropic_sse(self, content_blocks, model, stop_reason="end_turn"):
        """从 content blocks 构造 Anthropic SSE 事件流"""
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        lines = []
        lines.append(f'event: message_start\ndata: {json.dumps({"type":"message_start","message":{"id":msg_id,"type":"message","role":"assistant","content":[],"model":model,"stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":0,"output_tokens":0}}})}\n')
        for idx, block in enumerate(content_blocks):
            btype = block.get("type", "text")
            if btype == "thinking":
                start_block = {"type": "thinking", "thinking": ""}
                if block.get("signature"):
                    start_block["signature"] = block["signature"]
            elif btype == "tool_use":
                start_block = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", "")}
            else:
                start_block = {"type": "text", "text": ""}
            lines.append(f'event: content_block_start\ndata: {json.dumps({"type":"content_block_start","index":idx,"content_block":start_block})}\n')
            if btype == "thinking":
                text = block.get("thinking", "")
                lines.append(f'event: content_block_delta\ndata: {json.dumps({"type":"content_block_delta","index":idx,"delta":{"type":"thinking_delta","thinking":text}})}\n')
            elif btype == "tool_use":
                input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
                lines.append(f'event: content_block_delta\ndata: {json.dumps({"type":"content_block_delta","index":idx,"delta":{"type":"input_json_delta","partial_json":input_json}})}\n')
            else:
                text = block.get("text", "")
                for i in range(0, max(1, len(text)), 80):
                    chunk = text[i:i+80]
                    if chunk:
                        lines.append(f'event: content_block_delta\ndata: {json.dumps({"type":"content_block_delta","index":idx,"delta":{"type":"text_delta","text":chunk}})}\n')
            lines.append(f'event: content_block_stop\ndata: {json.dumps({"type":"content_block_stop","index":idx})}\n')
        total_chars = sum(len(b.get("text", "") or b.get("thinking", "")) for b in content_blocks)
        lines.append(f'event: message_delta\ndata: {json.dumps({"type":"message_delta","delta":{"stop_reason":stop_reason,"stop_sequence":None},"usage":{"output_tokens":max(1, total_chars // 4)}})}\n')
        lines.append(f'event: message_stop\ndata: {json.dumps({"type":"message_stop"})}\n')
        return "\n".join(lines)



