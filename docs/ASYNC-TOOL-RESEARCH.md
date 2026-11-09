# 异步工具调用：外部研究与工程证据

核对日期：2026-11-09；论文中的近期工作限定为 2024–2025，在线文档按本次读取快照解读。
范围：只讨论外部机制；以“文本流式模型、await 工具、后台队列”为迁移问题，不评判仓库架构，也不复述内部分析。
标记：**事实**＝论文、官方文档或源码直接支持；**推断**＝本文据此提出的迁移判断；没有实测本文所列系统的性能。
方法：逐项读取 arXiv 摘要及必要正文、官方文档和 GitHub 文件；链接集中列于文末，源码默认分支可能继续变化。

## A. 学术工作

### A1. LLMCompiler（2023；arXiv:2312.04511）
**事实·机制**：*An LLM Compiler for Parallel Function Calling* 将 Planner、Task Fetching Unit、Executor 分离，以 DAG 表达依赖，执行已就绪的独立任务。[论文与正文][P1]
**事实·延迟**：减少 ReAct 逐次“推理—执行”的串行开销；流式规划允许一项完整任务及依赖就绪后立即派发，无须等整张计划生成完。[正文 §3、§4][P1]
**事实·回灌**：前序结果替换占位变量并解锁依赖任务；需要重规划时，把中间结果交还 Planner，最终结果仍依赖实际执行。[正文 §3][P1]
**推断·可借**：借鉴依赖标识、就绪队列、增量派发；这证明规划与工具执行可重叠，不等于证明可以提前对用户确认成功。[依据][P1]

### A2. ReWOO（2023；arXiv:2305.18323）
**事实·机制**：*ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language Models* 先由 Planner 生成计划及 `#E` 引用，再由 Worker 取证，最后由 Solver 综合。[论文][P2]
**事实·延迟**：减少每次观察后重新调用模型、重复输入上下文的成本；摘要报告 HotpotQA 上五倍 token 效率，不能换写成五倍端到端提速。[摘要、正文 §3][P2]
**事实·边界**：后续工具仍可能依赖先前证据，最终答案由计划和证据共同产生；论文并未把“一次规划”定义成所有工具自动并行。[正文 §3、讨论][P2]
**推断·可借**：把尚未获得的证据保留为引用，结果回来后统一汇总；“未看结果先规划”可借鉴，“未看结果先下结论”没有得到支持。[依据][P2]

### A3. ToolLLM / ToolBench（2023；arXiv:2307.16789）
**事实·机制**：*ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs* 用 API 指令、搜索得到的调用轨迹训练 ToolLLaMA，DFSDT 探索不同路径。[论文][P3]
**事实·延迟边界**：主目标是工具选择、调用与泛化能力；不能从其多工具训练或搜索机制推出非阻塞执行、低延迟回灌保证。[论文][P3]
**事实·格式**：`DFS.py` 先调用 `step()` 得到 observation，再追加旧式 `role="function", name, content`；`rapidapi.py` 返回 observation 字符串和状态码，常以 JSON 承载 `error/response`。[搜索源码][S1] [环境源码][S2]
**推断·可借**：结果内容与执行状态分开，错误也作为观察；这里按函数名组织的顺序轨迹不足以关联同名并发调用，应另有唯一调用标识。[依据][S1] [对照现代协议][D4]

### A4. Gorilla（2023；arXiv:2305.15334）
**事实·机制**：*Gorilla: Large Language Model Connected with Massive APIs* 将 API 调用微调与文档检索结合，提高参数和 API 选择准确性，并适应文档变化。[论文][P4]
**事实·延迟边界**：它研究调用生成及 API 幻觉，未建立后台任务、结果注入或对话等待的调度机制；不能把调用准确率当成异步延迟指标。[论文][P4]
**推断·可借**：在派发前校验 API 名称、版本与参数；减少无效调用及其返工，但“调用合法”仍不等于“外部动作完成”。[依据][P4]

### A5. WebArena（2023；arXiv:2307.13854）
**事实·机制**：*WebArena: A Realistic Web Environment for Building Autonomous Agents* 用真实可操作网站和长程任务评估 agent，以环境中的功能结果判断任务完成。[论文][P5]
**事实·延迟边界**：官方 `run.py` 的基线顺序为 `agent.next_action → env.step → observation 入 trajectory`，再决策下一步；长程任务不是“先聊别的、结果稍后注入”的证明。[运行源码][S3]
**推断·可借**：把动作、环境观察与最终完成判定分开；该论文没有提供等待期间先对用户说话的协议，不能补写成它的既有能力。[依据][P5] [源码][S3]

### A6. AsyncLM（2024；arXiv:2412.07017）
**事实·机制**：*Asynchronous LLM Function Calling* 提出 AsyncLM：模型发出调用后继续生成，执行器并发运行，完成结果通过 interrupt 注入进行中的推理。[论文][P6]
**事实·延迟**：重叠生成与工具执行、减少调用逐个阻塞；论文给出 BFCL 任务上的延迟实验，但其收益不能直接外推到另一模型或工作负载。[摘要与正文][P6]
**事实·实现边界**：论文设计上下文内的调用/中断协议、适配训练和推理过程实现；不是普通 HTTP 文本流天然具备的输入通道。[正文 §3–§4][P6]
**推断·可借**：借鉴调用标识、未决任务及依赖处等待；单向文本流可用分段请求模仿结果续接，但不能宣称复现了原论文的在途中断能力。[依据][P6]
**推断·诚实边界**：调度协议能约束何时拿到结果，不能据此保证生成文本绝不虚报完成；本文未在该论文中找到对此的专项可靠性保证。[依据][P6]

### A7. Speculative Tool Calls（2025；arXiv:2512.15834）
**事实·机制**：*Optimizing Agentic Language Model Inference via Speculative Tool Calls* 用小模型预测工具调用并提前执行；主模型实际调用命中时复用 future/缓存，也研究推理引擎内工具缓存。[论文][P7]
**事实·延迟**：客户端方案把工具工作与主模型生成重叠；引擎方案另减少序列移出、重新调度及重复 prefill 的开销。[正文 §3][P7]
**事实·边界**：作者明确限定无状态、低成本工具；修改状态需要 undo/rollback，超出论文范围；预测结果须匹配真正调用才复用。[正文 §3.1–§3.2][P7]
**推断·可借**：缓存完整调用与真实结果，命中后再消费；投机的是“将调用什么”，不是编造工具返回值，也不是提前发布任务完成声明。[依据][P7]

### A8. 不确定性：Language Models (Mostly) Know What They Know（2022）
**事实·机制**：arXiv:2207.05221 分别研究回答正确概率 `P(True)` 和自知概率 `P(IK)`，在适当格式下得到较好校准，但新任务上的 `P(IK)` 校准仍困难。[论文][P8]
**事实·延迟边界**：这不是加速论文，也未验证异步工具回执；它提供的是如何评估模型是否知道答案的证据，不能证明一句“不确定”提示即可可靠阻止虚报。[论文][P8]
**推断·可借**：把“尚无回执”作为明确状态，区分未知、失败、成功；模型自评置信度只能辅助措辞，不能代替真实执行证据。[依据][P8]

## B. 公开工程实现

### B1. LangGraph / LangChain
**事实**：LangGraph 节点支持同步函数和 `async` 函数；但预置 `ToolNode._afunc` 明确 `await asyncio.gather(*coros)`，并行工具全部返回后才合并节点输出。[Graph API][D1] [源码][S4]
**事实**：`interrupt()` 配合 checkpointer 和 `thread_id` 保存状态并暂停；调用方通过相同线程的 `Command(resume=...)` 恢复，恢复会从被中断节点开头重新执行。[官方文档][D2]
**事实**：文档要求中断前的副作用具备幂等性，并提醒恢复、失败重试可能重复执行；生产需要持久化 checkpointer。[官方文档][D2]
**推断**：默认 ToolNode 是“并发后汇合”，不是发起后自动放任当前模型继续；若要后台执行，可把任务登记和结果恢复拆为独立流程，但这是应用编排选择。[依据][S4] [D2]

### B2. OpenAI：历史 Assistants、Responses 与显式 async
**事实·日期**：官方弃用表给 Assistants API 的移除日期为 **2026-08-26**；以下 run polling 作为历史机制记录，不作为当前新接入建议。[弃用表][D3]
**事实·历史**：Assistants 的 Run 在服务端推进；自定义函数遇到 `requires_action` 后由客户端执行，再 `submit_tool_outputs`；示例轮询 `queued/in_progress`，提交结果后恢复服务端 Run。[官方示例][D5]
**事实·常规 Responses**：自定义函数由应用执行，再用带原始 `call_id` 的 `function_call_output` 发起后续请求；服务端托管工具与应用自定义函数不可混为一谈。[函数调用][D4] [工具说明][D6]
**事实·后台生成**：`background=true` 让响应生成在服务端后台进行，客户端查询状态；它本身不是“自定义工具仍未结束，模型就继续回答”的开关。[后台模式][D7] [对照 async][D8]
**事实·显式异步**：当前官方另有 `async: true` 工具定义，支持模型先做独立工作，结果通过后续 Responses 请求及原始 `call_id` 交回；文档列明支持 GPT-6 Astra 及后续模型，不能外推到任意模型。[Async tool calling][D8]
**事实·诚实边界**：官方示例要求不编造待到结果，并演示独立问题先答、依赖结果时再 wait；`wait_for_tasks` 是应用定义工具，不是内置工具，也不代表已完成结果。[官方指南][D8]
**推断**：polling、服务端后台生成、工具执行异步是三件事；现有文本流若没有该协议能力，只能借鉴应用侧登记、续接和依赖等待，不能仅凭 `await` 消失就宣称等价。[依据][D4] [D7] [D8]

### B3. Anthropic Claude tool use
**事实**：标准客户端工具由 assistant 的 `tool_use(id,name,input)` 提出，应用执行后在下一条 `user` 消息放 `tool_result(tool_use_id,content)`；`stop_reason=tool_use` 表示交还控制权。[官方文档][D9]
**事实**：结果消息必须紧跟包含调用的 assistant 消息，且结果块在 user 消息内容开头；同轮并行调用的结果一起返回，异常可用 `is_error` 表达。[结果格式][D9] [并行规则][D13]
**推断**：这里同步的是“获得结果后再继续”的轮次因果，不是限制必须阻塞线程或保持 HTTP 连接；可以后台执行后再请求，但标准循环未提供任意插话穿过未结调用的许可。[依据][D9]

### B4. BabyAGI：有任务队列，不自动等于后台 agent
**事实**：经典归档实现 `babyagi.py` 从队列取任务，调用 `execution_agent` 获取结果，写结果存储，然后创建、排序后续任务；执行时检索历史结果作为上下文。[源码][S5]
**事实**：该主循环先拿到执行结果才推进，不能把任务列表或结果存储描述成独立 worker，也不能据此声称“当前对话继续、后台结果即时注入”。[源码][S5]
**推断**：可借鉴任务身份、完成结果持久化、后续步骤检索；后台执行与用户对话的生命周期仍需另外定义，本文不把归档版能力外推到新版 BabyAGI。[依据][S5]

### B5. Temporal：持久执行与“只消费一次”的实际边界
**事实**：Temporal 用追加事件历史恢复工作流，重放时对照既有历史推进；架构明确要求 Activity 幂等或不重试，不能把工作流恢复说成外部副作用恰好一次。[工作流][D10] [架构源码][S6]
**事实**：异步 Activity completion 可保存 task token，让 Activity 返回后仍保持未完成，外部进程稍后 heartbeat、complete 或 fail；不必占住原 worker 调用栈等待。[官方示例][D11]
**事实**：完成处理检查 Activity 是否仍运行及 token 是否匹配，然后追加完成事件；重复或过时的完成请求可能被拒绝，并非再次接受一个新完成。[完成处理源码][S7]
**推断**：可借鉴“稳定身份＋持久完成记录＋重放既有结果”；把结果写进对话、调用模型、向用户发送仍是额外副作用，必须分别去重，不能从工作流保证推导端到端 exactly-once。[依据][D10] [S6] [S7]

### B6. Home Assistant：成熟的启动与等待分离
**事实**：官方脚本文档演示 `script.turn_on` 启动另一脚本后继续其他步骤，必要时用实体状态 `off` 等待结束；并提供 single/restart/queued/parallel 等运行模式。[官方文档源码][S8]
**推断**：可借鉴“先启动、后等待”的显式语义；实体不再运行不等于某个设备动作已成功，业务回执应与调度状态分开。[依据][S8]

## C. 共识、分歧与流式现实

### C1. 共识应落在语义，而不是统一消息角色
**归纳·推断**：上述系统共同采用可识别的任务或调用、执行结果与后续推理分离、依赖处汇合；这比“所有工具同步”或“所有工具异步”更符合证据。[P1] [P2] [D4] [D9]
**事实·格式差异**：Responses 用 `function_call_output + call_id`，Claude 用 user 内的 `tool_result + tool_use_id`，LangGraph 用 `ToolMessage.tool_call_id`；旧 ToolBench 用 function 角色，不能统一写成 user/tool 二选一。[D4] [D9] [S4] [S1]
**归纳·推断**：调用关联 ID 解决“结果属于谁”，幂等键解决“重试是否重复生效”，两者不等价；重放与副作用去重是持久执行的共识，具体业务去重边界仍由应用定义。[D4] [D2] [S6]

### C2. “先说后看”确有支持，但没有通用诚实保证
**事实**：AsyncLM 支持执行期间继续生成；当前 OpenAI async 指南还明确示范先回答独立问题、稍后交回工具结果，因此“无人这么做，行业默认避免”不成立。[P6] [D8]
**归纳·推断**：应分清先规划、先做无关工作、先说进度、先断言结果四件事；前三者分别有上述机制支持，第四者不能从异步机制获得事实依据。[P1] [P2] [P6] [D8]
**事实与限制**：OpenAI 用独立工作指令与依赖处 wait 约束行为；投机论文通过匹配真实调用再使用真实结果保护执行路径，但二者都不能被引用为“自然语言绝不虚报”的证明。[D8] [P7]
**推断**：未收到回执时只陈述已核实的阶段，例如已登记或仍等待；成功断言应由真实结果放行，不把模型自报置信度当作执行成功的证据。[P8] [D8]

### C3. 流式参数不等于可立即执行，也不等于在途回灌
**事实**：OpenAI 流式会发送参数 delta，再发送 `response.function_call_arguments.done`；async 指南建议在完整调用项到达时提前派发，并继续消费响应。[D4] [D8]
**事实**：Claude fine-grained streaming 可在未缓冲、未校验完整 JSON 时发参数片段；官方仍要求在 `content_block_stop` 后拼接并解析，还要处理截断及无效 JSON。[细粒度流式][D12]
**归纳·推断**：普通整参数工具的安全派发点是完整输入校验之后；可与后续生成重叠，但不能把未闭合 JSON 当作授权完备的调用；专门设计的流式输入消费不等同于提前提交副作用。[D4] [D12]
**推断·文本协议**：没有原生 tool_calls 时，可借 LLMCompiler/ReWOO 定义可解析的完整调用记录；调度器须校验记录与依赖，再把带身份的真实结果放入下一生成阶段，这属于协议迁移方案而非论文对任意文本模型的保证。[P1] [P2] [P6]

## 出处索引

[P1]: https://arxiv.org/abs/2312.04511 "已核对摘要与 v3 正文"
[P2]: https://arxiv.org/abs/2305.18323 "已核对摘要与 v1 正文"
[P3]: https://arxiv.org/abs/2307.16789
[P4]: https://arxiv.org/abs/2305.15334
[P5]: https://arxiv.org/abs/2307.13854
[P6]: https://arxiv.org/abs/2412.07017 "已核对摘要与 v1 正文"
[P7]: https://arxiv.org/abs/2512.15834 "已核对摘要与 v1 正文"
[P8]: https://arxiv.org/abs/2207.05221
[S1]: https://github.com/OpenBMB/ToolBench/blob/master/toolbench/inference/Algorithms/DFS.py#L268-L303
[S2]: https://github.com/OpenBMB/ToolBench/blob/master/toolbench/inference/Downstream_tasks/rapidapi.py#L280-L385
[S3]: https://github.com/web-arena-x/webarena/blob/main/run.py#L296-L323
[S4]: https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py#L836-L868
[S5]: https://github.com/yoheinakajima/babyagi_archive/blob/main/babyagi.py#L495-L602
[S6]: https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md
[S7]: https://github.com/temporalio/temporal/blob/main/service/history/api/respondactivitytaskcompleted/api.go#L75-L115
[S8]: https://github.com/home-assistant/home-assistant.io/blob/current/source/_integrations/script.markdown
[D1]: https://docs.langchain.com/oss/python/langgraph/graph-api
[D2]: https://docs.langchain.com/oss/python/langgraph/interrupts
[D3]: https://developers.openai.com/api/docs/deprecations
[D4]: https://developers.openai.com/api/docs/guides/function-calling
[D5]: https://developers.openai.com/cookbook/examples/assistants_api_overview_python
[D6]: https://developers.openai.com/api/docs/guides/tools
[D7]: https://developers.openai.com/api/docs/guides/background
[D8]: https://developers.openai.com/api/docs/guides/async-tool-calling
[D9]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
[D10]: https://docs.temporal.io/workflow-execution
[D11]: https://docs.temporal.io/develop/python/activities/asynchronous-activity
[D12]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming
[D13]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use

## 三条可直接落地的建议（本文推断）

1. 定义含稳定调用 ID、完整参数、依赖、执行状态及结果引用的文本记录协议，只在参数完整且依赖满足时派发，并在后续生成中显式关联真实结果。[依据：LLMCompiler][P1] [Responses][D4]
2. 把“已登记／执行中／结果未知／成功／失败”做成可核验状态，结果未决时只输出已知进度或独立内容，依赖结果的成功声明等待真实回执。[依据：async 指南][D8] [校准研究][P8]
3. 分别为外部执行、结果消费与用户通知设置稳定去重键及持久记录，并验证“副作用后崩溃、重复回调、恢复重放”不会导致重复业务效果。[依据：Temporal][S6] [S7] [LangGraph][D2]
