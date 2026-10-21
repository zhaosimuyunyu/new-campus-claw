"""Reproducible, real-provider acceptance run, isolated from existing course data."""
import asyncio
import json
import os
import secrets
from pathlib import Path
from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from core.models import User, Course, Membership, AssistantConfig, MCPServer, AgentThread, StoredFile, Material
from core.mcp_client import discover
from core.agent_runtime import index_material


class Command(BaseCommand):
    help = 'Run lesson 6 acceptance with a separate local demo account and real configured providers.'

    def add_arguments(self, parser):
        parser.add_argument('--only',default='',help='Only run checks whose label contains this text')
        parser.add_argument('--output',default='/app/lesson6-evidence.json')
        parser.add_argument('--credentials',default='/tmp/lesson6-demo.json')

    def handle(self,*args,**options):
        user,created=User.objects.get_or_create(email='lesson6-demo@campusclaw.local',defaults={'display_name':'第六课验收演示','can_create_course':True})
        if created:
            password=secrets.token_urlsafe(18);user.set_password(password);user.save()
            Path(options['credentials']).write_text(json.dumps({'email':user.email,'password':password}),encoding='utf-8')
        course,_=Course.objects.get_or_create(name='第六课 · AI 能力实验室',created_by=user)
        Membership.objects.get_or_create(user=user,course=course,defaults={'role':'teacher'})
        config,_=AssistantConfig.objects.get_or_create(owner=user,course=course)
        server,_=MCPServer.objects.get_or_create(owner=user,name='campus-tools',defaults={'url':'http://campus-tools:8100/mcp','token_env':'MCP_CAMPUS_TOKEN'})
        server.tools,server.session_id=async_to_sync(discover)(server);server.connected=True;server.error='';server.save()
        report={'provider':os.getenv('MODEL_PROVIDER'),'course_id':course.id,'checks':[],'traces':[]}
        self.stdout.write('MCP discovered: '+', '.join(t['name'] for t in server.tools))
        client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+str(RefreshToken.for_user(user).access_token))
        config.app_id='general';config.template='你是 {{course}} 的实验助教。请用中文回答，先给结论，再列证据。';config.use_knowledge=False
        config.skill_ids=['data-analysis'];config.mcp_refs=['formula_hint@campus-tools','python_execute@campus-tools','shell_execute@campus-tools','browser_open@campus-tools'];config.save()

        def ask(label,content,knowledge=False):
            if options['only'] and options['only'] not in label:
                return None
            thread=AgentThread.objects.create(owner=user,course=course,title=label)
            with override_settings(ALLOWED_HOSTS=['testserver','localhost']):
                response=client.post(f'/api/agent/threads/{thread.id}/messages/stream/',{'content':content,'use_course_materials':knowledge},format='json')
                async def collect():
                    return b''.join([c async for c in response.streaming_content]).decode()
                stream=asyncio.run(collect())
            run=thread.runs.latest('id')
            trace={'id':run.id,'label':label,'thread_id':thread.id,'status':run.status,'error':run.error_message,
                   'events':list(run.events.exclude(event_type='delta').order_by('sequence').values('event_type','payload','sequence'))}
            report['traces'].append(trace)
            calls=[e['payload']['name'] for e in trace['events'] if e['event_type']=='tool_call']
            response=thread.messages.filter(role='assistant').last()
            report['checks'].append({'name':label,'run_id':run.id,'status':run.status,'tools':calls,'answer':response.content if response else '', 'error':run.error_message})
            self.stdout.write(f'{label}: {run.status}; tools={calls}')
            Path(options['output']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            return trace
        ask('01 模板与普通问答','请用两句话解释 Skill 和 MCP 的关系。')
        ask('02 技能与脚本执行','请使用已绑定的数据分析技能，先 read_skill，再读取 scripts/summarize_numbers.py，然后用 run_skill_script 执行它，参数为 12、18、24、30。根据真实输出报告平均数和中位数，不要改用其他计算工具。')
        ask('03 MCP 公式提示','请实际调用 formula_hint 工具获取复合函数求导提示，并据此解释链式法则。')
        ask('04 MCP Python 与命令行','请分别实际调用 python_execute 和 shell_execute：Python 计算 1 到 100 的整数和；Shell 用 node 计算 6 的阶乘。给出真实返回值。')
        ask('05 真实浏览器','请调用 browser_open 打开 https://example.com ，告诉我页面真实标题和正文概要，并提供截图。')
        if options['only'] and '06' not in options['only']:
            return
        text='课程阶段验收规则：本次 CampusClaw 项目的验收标识是 CC6-2026。项目需要五类能力：多个 AI 应用、提示词模板、知识库检索、Skill 技能上传、MCP 工具接入。知识库采用相关片段检索，不将所有材料发送给模型。验收应导出模型请求、返回与工具调用证据。'
        if not Material.objects.filter(course=course,title='第六课验收说明').exists():
            stored=StoredFile.objects.create(course=course,file=SimpleUploadedFile('lesson6.txt',text.encode()),original_name='lesson6.txt',size=len(text.encode()),kind='material',uploaded_by=user)
            material=Material.objects.create(course=course,title='第六课验收说明',attachment=stored)
        else:
            material=Material.objects.get(course=course,title='第六课验收说明')
        try:
            index_material(material)
            ask('06 知识库检索','课程阶段验收规则是什么？验收标识是什么？项目需要哪五类能力？',True)
        except Exception as exc:
            report['checks'].append({'name':'06 知识库检索','status':'failed','error':str(exc)[:300]})
        Path(options['output']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        self.stdout.write('Evidence saved; no API keys or login tokens are included.')
