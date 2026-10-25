from pathlib import Path
import json, html, hashlib
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter

ROOT=Path(__file__).resolve().parents[2]
BASE=Path(__file__).resolve().parent
OUT=ROOT/'output/pdf'; OUT.mkdir(parents=True,exist_ok=True)
main=json.loads((BASE/'evidence.json').read_text(encoding='utf-8'))
extra=json.loads((BASE/'supplement.json').read_text(encoding='utf-8'))
traces=main['traces']+extra['traces']
def events(t,kind):return [e['payload'] for e in t['events'] if e['event_type']==kind]
req=events(traces[0],'llm_request')[0]
assert all(t['status']=='completed' for t in traces)
assert all(r['sent'] for t in traces for r in events(t,'llm_request'))
assert len(extra['knowledge']['markers_sent'])==4 and len(extra['knowledge']['markers_not_sent'])==2
skill=events(traces[1],'tool_result')[0]['result']['instructions']
requests=events(traces[1],'llm_request')
assert skill not in json.dumps(requests[0]['payload'],ensure_ascii=False)
assert skill in requests[1]['payload']['messages'][-1]['content'] or skill in json.loads(requests[1]['payload']['messages'][-1]['content']).get('instructions','')
pdfmetrics.registerFont(TTFont('CN','C:/Windows/Fonts/msyh.ttc',subfontIndex=0))
pdfmetrics.registerFont(TTFont('CNB','C:/Windows/Fonts/msyhbd.ttc',subfontIndex=0))
red=colors.HexColor('#8c0000'); ink=colors.HexColor('#252525')
styles={
 'body':ParagraphStyle('body',fontName='CN',fontSize=10,leading=17,textColor=ink,spaceAfter=9,wordWrap='CJK'),
 'small':ParagraphStyle('small',fontName='CN',fontSize=8,leading=12,textColor=ink,spaceAfter=6,wordWrap='CJK'),
 'h':ParagraphStyle('h',fontName='CNB',fontSize=21,leading=30,textColor=red,spaceAfter=17),
 'sub':ParagraphStyle('sub',fontName='CNB',fontSize=12,leading=18,textColor=red,spaceBefore=9,spaceAfter=8),
 'code':ParagraphStyle('code',fontName='CN',fontSize=8,leading=12,textColor=ink,backColor=colors.HexColor('#f7f3f1'),borderPadding=9,spaceBefore=5,spaceAfter=13,wordWrap='CJK'),
}
story=[]; md=[]
def p(s,kind='body'):
 story.append(Paragraph(html.escape(str(s)).replace('\n','<br/>'),styles[kind]));md.append(str(s)+'\n')
def title(n,s):
 if story:story.append(PageBreak())
 p(f'{n:02d}  /  {s}','h')
def table(rows,widths):
 cells=[[Paragraph(html.escape(str(c)).replace('\n','<br/>'),styles['small']) for c in row] for row in rows]
 t=Table(cells,colWidths=widths,repeatRows=1,hAlign='LEFT')
 t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#f1e4e1')),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),.7,red),('LINEBELOW',(0,1),(-1,-1),.25,colors.HexColor('#ded8d5')),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
 story.append(t);story.append(Spacer(1,12));md.extend([' | '.join(map(str,row))+'\n' for row in rows])
def code(obj):p(json.dumps(obj,ensure_ascii=False,indent=2) if not isinstance(obj,str) else obj,'code')

title(1,'个人项目阶段性验收报告')
p('CampusClaw · 面向课程学习的 AI 应用平台','sub')
p('验收日期：2026 年 10 月 25 日（北京时间）\n作业：【个人项目】阶段性验收\n提交截止：2026 年 10 月 28 日 00:00')
p('Git 仓库：https://github.com/zhaosimuyunyu/new-campus-claw')
p('核查代码基线：18e20612bbb4f2f9bc729be838e9152d37fe5184。本次前端优化及报告仍为本地工作区改动，尚未推送。后端使用现有 Docker 服务执行真实验证。','small')
p('项目简介','sub')
p('CampusClaw 将课程、资料、作业管理与 AI 助手整合在一个平台内。用户可选择不同 AI 应用、编辑提示词模板、检索课程知识库、绑定或上传 Skill，并调用平台工具及外部 MCP 服务。每次运行记录模型请求、流式返回和工具结果，可在能力中心的运行追踪中查看及导出。')
p('技术组成：Vue 3 + TypeScript + Vite 前端；Django / Django REST Framework 后端；PostgreSQL + pgvector 知识库；OpenAI 兼容模型接口；MCP HTTP 服务；临时容器中的 Python、Node、Shell 与 Chromium 执行环境。')
table([['作业要求','实现与验证结果'],['多个 AI 应用','5 项：课程 AI 助手、论文研读、编程搭档、学术写作、复习教练。目录接口实读。'],['提示词模板','支持 {{course}} 和 {{question}}；真实请求中可见展开后的 system 内容。'],['知识库检索','课程范围内混合检索；6 份资料对照实验仅发送 4 个命中片段。'],['内置与上传 Skill','66 项内置技能；本轮上传 ZIP 返回 201，并执行脚本生成文件。'],['外部工具及 MCP','发现 6 个 MCP 工具；真实完成公式提示、Python、Shell 和浏览器调用。']],[100,405])
p('结论：五类功能均有实现与验证证据。正文逐题回答，完整逐轮请求及返回作为 JSON 附件嵌入本 PDF；无需仅凭截图判断。','small')

title(2,'验证方法与证据目录')
p('使用独立验收账号与课程执行实验，未改动用户现有课程资料。2026-10-25 新跑 8 组真实模型实验；所有模型请求记录均标记 sent=true，provider=openai，模型名称为 course-chat。模拟测试仅作为行为回归依据，不计作真实模型验证。')
table([['实验 / Run ID','请求轮数','观测结果']]+[[f"{t['label']} / {t['id']}",len(events(t,'llm_request')),{'40':'普通回答；模板已展开','41':'读取技能、读取源码、执行脚本；均值与中位数 21','42':'formula_hint 成功，随后生成解释','43':'Shell 输出 720；Python 补调用后输出 5050','44':'浏览器获取 Example Domain 并生成截图','45':'检索验收说明并引用 CC6-2026','46':'上传技能执行成功并生成 summary.json','47':'6 份资料中命中 4 份，另 2 份未发送'}[str(t['id'])]] for t in traces],[220,55,230])
p('证据分层','sub')
p('1. 真实接口：evidence.json 保存 Run 40-45；supplement.json 保存上传响应、目录、对照资料和 Run 46-47。\n2. 代码核查：agent_views.py 组装上下文；agent_runtime.py 生成请求并记录返回；skill_runtime.py 加载与执行技能；mcp_client.py 执行协议交互。\n3. 自动回归：本轮后端 28 项通过；前端在本次界面修改后 20 项通过，类型检查及生产构建通过。')
p('可复现入口','sub')
code('docker compose exec -T backend python manage.py verify_lesson6 --output /tmp/acceptance.json\ndocker compose exec -T -e MODEL_PROVIDER=mock backend python manage.py test core --noinput\ncd frontend\nnpm test\nnpm run build')
p('前提：配置真实模型及向量服务，启动数据库、MCP 和执行代理，构建 campusclaw-sandbox:local 镜像。verify_lesson6 使用专用演示账号，复跑会创建新的对话记录。','small')

title(3,'问题一：每轮参数、返回与模板')
p('结论：提示词模板会传入 LLM。每次用户提问可触发一次或多次模型请求；工具调用后还会继续请求模型，因此“一次对话”和“一次接口请求”不能混为一谈。')
p('实际接口：POST '+req['endpoint'],'small')
table([['请求字段','Run 40 第一轮实际值 / 含义'],['model','course-chat'],['stream','true；以 SSE 接收流式返回'],['messages','2 条：system（平台约束、应用角色、展开模板、绑定技能摘要）与 user（本次问题）；续聊可包含历史。'],['tools','10 项候选工具声明；即使本轮不调用，绑定后的声明仍可能随请求发送。'],['其他参数','本次请求体未显式设置 temperature、top_p、max_tokens、tool_choice，不能把供应商默认值当成实际传值。']],[100,405])
p('模板与用户消息原文','sub')
code('配置模板：你是 {{course}} 的实验助教。请用中文回答，先给结论，再列证据。\nsystem 中实际文本：你是 第六课 · AI 能力实验室 的实验助教。请用中文回答，先给结论，再列证据。\nuser：请用两句话解释 Skill 和 MCP 的关系。')
p('Run 40 聚合正文原文','sub')
p(main['checks'][0]['answer'],'code')
p('这段返回证明接口确有回应，但把 MCP 说成“基础工具集合”不够准确。MCP 本质是模型应用与工具/资源服务之间的通信协议；本平台通过该协议接入具体工具。报告不把模型回答本身视为权威定义。','small')
p('返回记录还包括 frames（原始 SSE JSON 帧）及 tool_calls。Run 40 最终 usage：prompt_tokens=1764、completion_tokens=557、total_tokens=2321；增量帧在 choices[].delta，结束原因取自实际 finish_reason。工具响应可出现正文为空但 tool_calls 非空的情形。认证头与密钥不进入追踪附件。','small')

title(4,'问题二：是否发送整个知识库')
p('结论：不会固定发送全部知识库。实现先在当前课程内检索，关键词与向量结果通过 RRF 融合排序，最多取 4 个相关片段。仅将选中片段的标题、位置与正文写入 system。索引阶段的 embedding 调用与回答阶段的 LLM 调用是不同步骤。')
p('对照设计：新建独立课程，上传并索引 6 份短文本，每份各有唯一标记 RAG-CONTROL-1 至 RAG-CONTROL-6；开启资料检索，询问验收规则和命中标记。该设计避免“知识库只有一小份资料，返回全文也看起来像检索”的歧义。')
table([['Run 47 观测项','实际结果'],['资料总数','6 份'],['retrieval.top_k','4'],['returned_chunks','4 个，按实际返回顺序为材料 1、6、3、2'],['实际请求包含的标记',', '.join(extra['knowledge']['markers_sent'])],['实际请求未包含的标记',', '.join(extra['knowledge']['markers_not_sent'])],['请求中命中片段一致性','逐项核对 returned_chunks.excerpt 与 system；命中正文进入请求，未命中标记缺席。']],[130,375])
p('命中片段实例','sub')
code(events(traces[-1],'retrieval')[0]['returned_chunks'][0]['excerpt'])
p('判断依据不是仅看 whole_knowledge_base_sent=false，而是对实际 llm_request.payload 做标记对照。原始 JSON 中可查到材料 ID、切片 ID、位置及检索得分。')
p('边界说明','sub')
p('若知识库内容很少且全部被命中，发送片段可能恰好覆盖全库；“最多 4 个相关片段”并不保证每次一定少于全库。多轮会话还会携带历史文本，之前回答中的资料信息可能再次进入上下文。若无命中，当前实现直接回复未找到资料，不调用 LLM。关闭检索后，显式绑定的检索技能仍可能允许模型按需调用搜索工具。')

title(5,'问题三：绑定 Skill 会发送全文吗')
p('结论：初始请求只发送已绑定技能的摘要与技能工具声明，不自动发送整个技能包。模型调用 read_skill 后，所选技能的完整指令会以 tool 消息进入后续请求；read_skill_resource 再按路径读取具体参考文件或脚本。')
table([['Run 41 轮次','本轮内容 / 返回','下一轮新增上下文'],['1','system 含 data-analysis 名称和描述；模型请求 read_skill','完整 instructions、资源文件名及运行说明'],['2','模型请求 read_skill_resource，路径 scripts/summarize_numbers.py','该脚本的实际源码'],['3','模型请求 run_skill_script，传入 12、18、24、30','stdout、stderr、exit_code、duration_ms 等结果'],['4','模型依据工具结果生成最终回答','平均数 21、中位数 21']],[60,235,210])
p('初始请求中的实际摘要','sub')
code('data-analysis: 数据分析 — 从数据质量、统计方法到结论边界。')
p('验证：以 read_skill 返回的完整 instructions 为对照，确认第一轮请求不含全文；第二轮最后一条 tool 消息包含该全文。源码包含 statistics.mean，直到读取脚本后才进入后续请求。上述比较在生成本报告时再次作了断言检查。')
p('不能将“按需加载”误写成“全文永远不进入模型”。在同一次运行的工具循环内，已经加载的指令和源码会保留在后续 messages 中，因此后续请求可能包含完整 SKILL.md 和已读取的资源。未读取的整个资源目录不会自动拼接进 LLM 请求。')
p('执行服务与模型的输入不同','sub')
p('run_skill_script 会把技能所需文件交给隔离执行服务，以支持脚本相对路径依赖；这不等于把所有文件内容发给 LLM。二进制资源由执行器使用，读取接口返回资源说明，不直接把二进制字节塞进模型文本。')

title(6,'问题四：Skill 的执行逻辑')
p('实际流程：绑定技能 → 在 system 中声明摘要 → 模型产生工具调用 → 宿主检查技能是否已绑定 → read_skill 加载指令 → 按需读取资源 → run_skill_script 调用执行服务 → 结果写入 tool 消息 → 再次请求模型 → 输出答案与附件。Skill 本身是指令和资源包，不是自动执行任意代码的入口。')
p('内置技能真实调用参数（Run 41）','sub')
code({'name':'run_skill_script','arguments':{'skill_id':'data-analysis','path':'scripts/summarize_numbers.py','args':['12','18','24','30']}})
p('执行服务真实返回（摘录）','sub')
code(events(traces[1],'tool_result')[-1]['result'])
p('上传技能闭环（Run 46）','sub')
p('通过 /api/agent/skills/upload/ 上传 sample-skill.zip，HTTP 201，技能 ID 为 upload-2。技能包含 SKILL.md 与 scripts/summarize.py。绑定后，真实执行 read_skill → read_skill_resource → run_skill_script，三项工具均为 ok，并生成 outputs/summary.json，作为可下载产物保存。')
p('生成内容：count=4、mean=21.0、median=21.0。上传是经正式应用接口完成，执行经过同一技能运行链，并非直接在报告脚本中计算来替代平台验证。')
p('执行约束','sub')
p('脚本路径需位于已绑定技能的 scripts/ 目录，参数按 JSON Schema 验证。普通脚本在临时容器内运行，非 root、根文件系统只读、默认无网络，并限制 CPU、内存、进程数和运行时间（35 秒）。输出中的文件由宿主保存为受账号与课程权限保护的产物。仅指令型技能可能不执行脚本，而是指导模型按方法完成任务。','small')
p('上游技能依赖特定插件、Git 工作区或外部账户时仍需对应环境；“内置了技能”不代表其所有外部能力均可无条件使用。','small')

title(7,'问题五：接入 MCP 后的请求')
p('结论：LLM 仍接收 model、stream、messages，并额外接收当前绑定候选工具的 tools 声明。MCP 握手、服务 URL、会话和认证由宿主管理；模型不直接向 MCP 服务发送 HTTP 请求。')
p('实际工具声明：Run 42 第一轮','sub')
tool=next(t for t in events(traces[2],'llm_request')[0]['payload']['tools'] if t['function']['name']=='mcp_1_formula_hint')
code(tool)
p('模型返回调用 → 宿主调用 MCP','sub')
code({'LLM tool_call':{'name':'mcp_1_formula_hint','arguments':'{"topic":"复合函数求导"}'},'MCP method':'tools/call','MCP params':{'name':'formula_hint','arguments':{'topic':'复合函数求导'}}})
p('宿主连接时执行 initialize → notifications/initialized → tools/list，将 name、description、inputSchema 转换为 LLM function 定义。模型返回 tool_calls 后，宿主验证工具候选集、参数及权限，再通过 JSON-RPC 2.0 调用 MCP。结果作为 role=tool 且关联 tool_call_id 的消息进入第二轮；随后模型给出链式法则说明。')
p('本轮发现 6 项工具：formula_hint、unit_convert、learning_checklist、python_execute、shell_execute、browser_open；实验配置只绑定其中 4 项，因此请求中未声明另外 2 项。平台自己的 list_course_materials、create_pdf、create_docx 和技能工具也可能同时存在。','small')
p('协议边界：目前实现 HTTP MCP（JSON / SSE），不支持 stdio。代码限制最多 6 轮模型请求、12 次工具调用；最后一轮移除工具并要求依据已有结果回答。','small')

title(8,'实际结果、限制与提交说明')
p('需要如实保留的观察','sub')
p('Run 43 第一轮 Python 工具退出码为 0，但 stdout 为空，不能据此声称已取得数值。模型第二轮补调用后才得到 5050；Shell/Node 输出 720。报告以工具真实返回为准，而不只看最终回答。')
p('Run 44 成功打开 Example Domain 并生成截图，说明 MCP 浏览器链可用；这不能证明任意网站、登录操作或其他网络场景均可用。每次模型响应存在随机性，复跑的调用顺序、文本和 token 数可能不同。')
p('本次前端完成北大红配色、课程与聊天布局、能力中心样式优化，补修导航竖向滚动条、短屏聊天高度与中等宽度预览拥挤问题。真实课程资料、技能广场、历史聊天及 HTML 预览已检查；当前用户课程无作业数据，未宣称本轮实测了作业详情。')
p('证据附件','sub')
p('本 PDF 内嵌 evidence.json、supplement.json 与 round-index.json。支持附件面板的 PDF 阅读器可提取；另提供独立 ZIP 证据包。round-index.json 为每个模型请求记录 Run ID、轮次、请求字段、消息角色、候选工具、完整聚合正文和工具调用，便于定位。')
p('源码定位','sub')
table([['文件','对应内容'],['backend/core/skill_catalog.py','5 个 AI 应用与内置技能目录'],['backend/core/agent_views.py','模板展开、检索片段、技能摘要、工具循环'],['backend/core/agent_runtime.py','混合检索、LLM 请求体、流式返回追踪'],['backend/core/skill_runtime.py','上传包解析、按需读取、隔离执行与 MCP 转发'],['backend/core/mcp_client.py','握手、发现、HTTP / SSE JSON-RPC'],['backend/core/test_platform.py','模板、惰性加载、检索、上传与权限回归测试']],[220,285])
p('仓库：https://github.com/zhaosimuyunyu/new-campus-claw\n本报告及本次新证据在本地生成，尚未提交到课程平台，也未推送 Git。提交时以此 PDF 为报告，证据 ZIP 可作为补充材料。')

index=[]
for t in traces:
 responses=events(t,'llm_response')
 for r in events(t,'llm_request'):
  response=next((s for s in responses if s['round']==r['round']),{})
  index.append({'run_id':t['id'],'label':t['label'],'round':r['round'],'sent':r['sent'],'request_keys':list(r['payload']),'message_roles':[m['role'] for m in r['payload']['messages']],'tools':[x['function']['name'] for x in r['payload'].get('tools',[])],'content':response.get('content'),'tool_calls':response.get('tool_calls')})
(BASE/'round-index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2),encoding='utf-8')
(BASE/'报告正文.md').write_text('\n'.join(md),encoding='utf-8')
def footer(c,doc):
 c.setStrokeColor(red);c.setLineWidth(.6);c.line(45,802,550,802)
 c.setFont('CN',8);c.setFillColor(colors.HexColor('#777777'));c.drawString(45,815,'CAMPUSCLAW  /  个人项目阶段性验收')
 c.drawString(45,24,'2026-10-25 · 真实调用验证');c.drawRightString(550,24,str(doc.page))
raw=ROOT/'tmp/pdfs/report-base.pdf'
raw.parent.mkdir(parents=True,exist_ok=True)
doc=SimpleDocTemplate(str(raw),pagesize=(595.28,841.89),rightMargin=45,leftMargin=45,topMargin=55,bottomMargin=48,title='CampusClaw 个人项目阶段性验收报告',author='CampusClaw 项目')
doc.build(story,onFirstPage=footer,onLaterPages=footer)
writer=PdfWriter();writer.append(str(raw))
for name in ['evidence.json','supplement.json','round-index.json']:writer.add_attachment(name,(BASE/name).read_bytes())
target=OUT/'CampusClaw_个人项目阶段性验收报告.pdf'
with target.open('wb') as f:writer.write(f)
reader=PdfReader(target)
assert len(reader.pages)==8, len(reader.pages)
print('PDF pages:',len(reader.pages),'requests:',len(index),'bytes:',target.stat().st_size)
