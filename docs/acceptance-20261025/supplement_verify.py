import asyncio,json
from pathlib import Path
from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from core.models import User,Course,Membership,AssistantConfig,AgentThread,StoredFile,Material,KnowledgeChunk
from core.agent_runtime import index_material

user=User.objects.get(email='lesson6-demo@campusclaw.local')
course,_=Course.objects.get_or_create(name='2026-10-25 阶段验收对照实验',created_by=user)
Membership.objects.get_or_create(user=user,course=course,defaults={'role':'teacher'})
config,_=AssistantConfig.objects.get_or_create(owner=user,course=course)
config.template='你是 {{course}} 的助教。本次问题：{{question}}。先给结论，再列证据。'
config.skill_ids=[];config.mcp_refs=[];config.use_knowledge=False;config.save()
client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+str(RefreshToken.for_user(user).access_token))
report={'date':'2026-10-25','traces':[]}
def ask(label,content,knowledge=False):
    thread=AgentThread.objects.create(owner=user,course=course,title=label)
    with override_settings(ALLOWED_HOSTS=['testserver','localhost']):
        response=client.post(f'/api/agent/threads/{thread.id}/messages/stream/',{'content':content,'use_course_materials':knowledge},format='json')
        async def consume():
            return b''.join([p async for p in response.streaming_content])
        asyncio.run(consume())
    run=thread.runs.latest('id')
    trace={'id':run.id,'label':label,'status':run.status,'events':list(run.events.exclude(event_type='delta').order_by('sequence').values('event_type','payload','sequence'))}
    report['traces'].append(trace)
    Path('/tmp/supplement.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(label,run.id,run.status,flush=True)
    return trace
with override_settings(ALLOWED_HOSTS=['testserver','localhost']):
    catalog=client.get('/api/agent/catalog/').json()
    report['catalog']={'apps':[{'id':a['id'],'name':a['name']} for a in catalog['apps']],'skills_count':len(catalog['skills'])}
    response=client.post('/api/agent/skills/upload/',{'file':SimpleUploadedFile('sample-skill.zip',Path('/tmp/sample-skill.zip').read_bytes())})
    report['upload']={'status_code':response.status_code,'result':response.json()}
    assert response.status_code==201
    config.skill_ids=[response.json()['id']];config.save()
ask('07 上传技能真实执行','请读取已绑定技能及 scripts/summarize.py，用 run_skill_script 对 12、18、24、30 计算均值和中位数，并生成 summary.json。')
config.skill_ids=[];config.save()
for i in range(6):
    body=f'阶段验收实验材料 {i+1}。唯一标记 RAG-CONTROL-{i+1}。本条规则是：项目验收需要演示第 {i+1} 个功能的具体请求和返回，并保留证据。'
    stored=StoredFile.objects.create(course=course,file=SimpleUploadedFile(f'control-{i+1}.txt',body.encode()),original_name=f'control-{i+1}.txt',size=len(body.encode()),kind='material',uploaded_by=user)
    material=Material.objects.create(course=course,title=f'验收对照材料 {i+1}',attachment=stored)
    index_material(material)
report['knowledge']={'materials':Material.objects.filter(course=course).count(),'markers':[f'RAG-CONTROL-{i+1}' for i in range(6)]}
trace=ask('08 六份材料检索对照','阶段验收实验材料中的项目验收规则是什么？请列出命中资料的唯一标记。',True)
request=next(e['payload']['payload'] for e in trace['events'] if e['event_type']=='llm_request')
text=json.dumps(request,ensure_ascii=False)
report['knowledge']['markers_sent']=[m for m in report['knowledge']['markers'] if m in text]
report['knowledge']['markers_not_sent']=[m for m in report['knowledge']['markers'] if m not in text]
Path('/tmp/supplement.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
