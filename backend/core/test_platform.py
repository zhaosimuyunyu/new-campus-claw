"""Behavior and isolation checks for the lesson-six platform."""
import asyncio
import json
import os
from io import BytesIO
from zipfile import ZipFile
from unittest.mock import patch, AsyncMock
from django.test import TransactionTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User, Course, Membership, AgentThread, MCPServer, AssistantConfig, AgentRun
from .skill_runtime import parse_upload
from .agent_runtime import trace_record


class PlatformTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user('platform@example.com','test-password',can_create_course=True)
        self.other = User.objects.create_user('platform-other@example.com','test-password',can_create_course=True)
        self.student = User.objects.create_user('platform-student@example.com','test-password')
        self.course = Course.objects.create(name='MCP 实验',created_by=self.owner)
        for user in [self.owner,self.student]:
            Membership.objects.create(user=user,course=self.course,role='teacher' if user==self.owner else 'student')
        self.thread = AgentThread.objects.create(owner=self.owner,course=self.course)
        self.client = self.client_for(self.owner)
        self.oc = self.client_for(self.other)
        self.sc = self.client_for(self.student)
        self.config = AssistantConfig.objects.create(owner=self.owner,course=self.course,template='课程：{{course}}；问题：{{question}}',skill_ids=['data-analysis'])

    def client_for(self,user):
        client=APIClient();client.credentials(HTTP_AUTHORIZATION='Bearer '+str(RefreshToken.for_user(user).access_token));return client

    def stream(self, content='计算数据'):
        response=self.client.post(f'/api/agent/threads/{self.thread.id}/messages/stream/',{'content':content},format='json')
        async def consume():
            return b''.join([part async for part in response.streaming_content]).decode()
        return asyncio.run(consume())

    def test_catalog_upload_and_config_are_private(self):
        catalog=self.client.get('/api/agent/catalog/').json()
        self.assertGreaterEqual(len(catalog['skills']),23)
        self.assertNotIn('instructions',catalog['skills'][0])
        upload=SimpleUploadedFile('SKILL.md',b'---\nname: private\ndescription: private only\n---\nSecret instruction.',content_type='text/markdown')
        response=self.client.post('/api/agent/skills/upload/',{'file':upload})
        self.assertEqual(response.status_code,201)
        sid=response.json()['id']
        self.assertEqual(self.oc.get(f'/api/agent/skills/{sid}/').status_code,404)
        self.assertEqual(self.oc.get(f'/api/courses/{self.course.id}/assistant/').status_code,404)
        self.assertEqual(self.sc.put(f'/api/courses/{self.course.id}/assistant/',{'skill_ids':[sid]},format='json').status_code,400)
        self.assertEqual(self.oc.get(f'/api/agent/threads/{self.thread.id}/traces/').status_code,404)

    def test_zip_supports_scripts_but_rejects_traversal(self):
        def pack(files):
            buf=BytesIO()
            with ZipFile(buf,'w') as z:
                for name,body in files.items():z.writestr(name,body)
            return SimpleUploadedFile('skill.zip',buf.getvalue())
        result=parse_upload(pack({'demo/SKILL.md':'# Demo\nRun script.','demo/scripts/main.py':'print(42)'}))
        self.assertIn('scripts/main.py',result[3])
        with self.assertRaises(ValueError):
            parse_upload(pack({'SKILL.md':'# Demo\nOK','../escape.py':'print(42)'}))
        with self.assertRaises(ValueError):
            parse_upload(pack({'SKILL.md':'# Demo\nOK','evil.exe':'binary'}))

    def test_mcp_management_discovery_and_account_isolation(self):
        self.assertEqual(self.sc.get('/api/agent/mcp/').status_code,403)
        with patch.dict(os.environ,{'MCP_TEST_TOKEN':'never-return-this-secret'}):
            response=self.client.post('/api/agent/mcp/',{'name':'my-tools','url':'http://campus-tools:8100/mcp','token_env':'MCP_TEST_TOKEN'},format='json')
            self.assertEqual(response.status_code,201)
            server=response.json()
            self.assertNotIn('never-return-this-secret',json.dumps(server))
            self.assertTrue(server['has_token'])
        self.assertEqual(self.oc.get(f"/api/agent/mcp/{server['id']}/").status_code,404)
        tool={'name':'formula_hint','description':'A hint','inputSchema':{'type':'object'}}
        with patch('core.platform_views.discover',new=AsyncMock(return_value=([tool],''))):
            connected=self.client.post(f"/api/agent/mcp/{server['id']}/",{'action':'connect'},format='json').json()
        self.assertTrue(connected['connected'])
        response=self.client.put(f'/api/courses/{self.course.id}/assistant/',{'mcp_refs':['formula_hint@my-tools']},format='json')
        self.assertEqual(response.status_code,200)
        self.client.post(f"/api/agent/mcp/{server['id']}/",{'action':'disconnect'},format='json')
        response=self.client.put(f'/api/courses/{self.course.id}/assistant/',{'mcp_refs':['missing@my-tools']},format='json')
        self.assertEqual(response.status_code,400)
        server=MCPServer.objects.get(id=server['id']);self.assertFalse(server.connected);self.assertEqual(server.tools,[])

    def test_lazy_skill_loading_template_and_tool_result_round_trip(self):
        from .agent_runtime import model_stream
        seen=[]
        async def scripted(messages, attachments=(), tools=()):
            snapshot=json.loads(json.dumps(messages));seen.append(snapshot)
            await trace_record('llm_request',{'provider':'fixture','sent':False,'payload':{'messages':snapshot,'tools':tools}})
            if len(seen)==1:
                yield {'tool_calls':[{'id':'read1','type':'function','function':{'name':'read_skill','arguments':'{"skill_id":"data-analysis"}'}}]},'fixture'
            elif len(seen)==2:
                yield {'tool_calls':[{'id':'read2','type':'function','function':{'name':'read_skill_resource','arguments':'{"skill_id":"data-analysis","path":"scripts/summarize_numbers.py"}'}}]},'fixture'
            else:
                yield '已经读取技能和脚本。','fixture'
        with patch('core.agent_runtime._model_stream_impl',scripted):
            body=self.stream('分析 1、2、3')
        self.assertIn('event: message_end',body)
        initial=seen[0][0]['content']
        self.assertIn('课程：MCP 实验；问题：分析 1、2、3',initial)
        self.assertNotIn('先确认字段、样本、缺失和单位',initial)
        self.assertIn('先确认字段、样本、缺失和单位',seen[1][-1]['content'])
        self.assertIn('statistics.mean',seen[2][-1]['content'])
        self.assertEqual(seen[1][-1]['role'],'tool')
        events=AgentRun.objects.filter(thread=self.thread).get().events
        self.assertEqual(events.filter(event_type='llm_request').count(),3)
        self.assertEqual(events.filter(event_type='llm_response').count(),3)
        self.assertEqual(events.filter(event_type='tool_result').count(),2)

    def test_mock_trace_explicitly_marks_no_external_request(self):
        with patch.dict(os.environ,{'MODEL_PROVIDER':'mock'}):
            self.assertIn('event: message_end',self.stream())
        request=AgentRun.objects.get(thread=self.thread).events.get(event_type='llm_request').payload
        self.assertFalse(request['sent'])
        self.assertIsNone(request['endpoint'])
        self.assertNotIn('headers',request)

    def test_retrieval_only_sends_returned_chunks(self):
        hit={'title':'相关资料','chunk_index':1,'excerpt':'唯一相关片段','material_id':1,'chunk_id':1}
        with patch.dict(os.environ,{'MODEL_PROVIDER':'mock'}),patch('core.agent_views.search',new=AsyncMock(return_value=[hit])):
            response=self.client.post(f'/api/agent/threads/{self.thread.id}/messages/stream/',{'content':'检索','use_course_materials':True},format='json')
            async def collect():
                return b''.join([p async for p in response.streaming_content])
            asyncio.run(collect())
        run=AgentRun.objects.get(thread=self.thread)
        retrieval=run.events.get(event_type='retrieval').payload
        self.assertEqual(retrieval['top_k'],4)
        self.assertFalse(retrieval['whole_knowledge_base_sent'])
        self.assertEqual(retrieval['returned_chunks'],[hit])
        self.assertIn('唯一相关片段',run.events.get(event_type='llm_request').payload['payload']['messages'][0]['content'])

    def test_local_tool_failure_is_returned_to_model(self):
        seen=[]
        async def model(messages,*args):
            seen.append(json.loads(json.dumps(messages)))
            if len(seen)==1:
                yield {'tool_calls':[{'id':'bad','type':'function','function':{'name':'unregistered','arguments':'{}'}}]},'fixture'
            else:
                yield '该工具不可用。','fixture'
        with patch('core.agent_views.model_stream',model):
            body=self.stream()
        self.assertIn('event: message_end',body)
        self.assertTrue(json.loads(seen[1][-1]['content'])['isError'])
        self.assertEqual(AgentRun.objects.get(thread=self.thread).events.get(event_type='tool_result').payload['status'],'rejected')


    def test_students_can_bind_shared_tools_without_server_management(self):
        service=User.objects.create_user('service@example.com',None,is_active=False)
        server=MCPServer.objects.create(owner=service,name='platform-tools',url='http://campus-tools:8100/mcp',
            token_env='MCP_CAMPUS_TOKEN',is_shared=True,connected=True,
            tools=[{'name':'formula_hint','description':'hint','inputSchema':{'type':'object'}}])
        result=self.sc.get('/api/agent/platform-tools/').json()
        self.assertEqual(result[0]['id'],server.id)
        self.assertNotIn('token_env',result[0]);self.assertNotIn('url',result[0])
        bound=self.sc.put(f'/api/courses/{self.course.id}/assistant/',{'mcp_refs':['formula_hint@platform-tools']},format='json')
        self.assertEqual(bound.status_code,200)
        self.assertEqual(self.sc.get('/api/agent/mcp/').status_code,403)


    def test_malformed_tool_arguments_fail_locally(self):
        counter=[]
        async def model(messages,*args):
            counter.append(1)
            if len(counter)==1:
                yield {'tool_calls':[{'id':'invalid','type':'function','function':{'name':'read_skill','arguments':'{invalid'}}]},'fixture'
            else:
                yield '参数错误，未执行。','fixture'
        with patch('core.agent_views.model_stream',model):
            body=self.stream()
        self.assertIn('event: message_end',body)
        result=AgentRun.objects.get(thread=self.thread).events.get(event_type='tool_result').payload
        self.assertEqual(result['status'],'rejected')

    def test_tool_loop_has_a_budget_and_completes(self):
        counter=[]
        async def model(messages,*args):
            counter.append(1)
            yield {'tool_calls':[{'id':str(len(counter)),'type':'function','function':{'name':'read_skill','arguments':'{"skill_id":"data-analysis"}'}}]},'fixture'
        with patch('core.agent_views.model_stream',model):
            body=self.stream()
        self.assertEqual(len(counter),6)
        self.assertIn('event: message_end',body)
        run=AgentRun.objects.get(thread=self.thread)
        self.assertEqual(run.status,'completed')
        self.assertLessEqual(run.events.filter(event_type='tool_result',payload__status='ok').count(),5)


    def test_codex_catalog_is_pinned_and_does_not_send_full_assets(self):
        from .skill_catalog import BUILTIN_SKILLS
        codex=[s for s in BUILTIN_SKILLS if s['id'].startswith('codex-')]
        self.assertEqual(len(codex),43)
        self.assertEqual(len({s['id'] for s in BUILTIN_SKILLS}),len(BUILTIN_SKILLS))
        for skill in codex:
            self.assertIn('/49f948faa9258a0c61caceaf225e179651397431/',skill['source_url'])
            self.assertIn('name:',skill['instructions'])
            self.assertTrue(skill['license'])
            self.assertTrue(skill['requirements'])
        notebook=next(s for s in codex if s['id']=='codex-jupyter-notebook')
        self.assertIn('scripts/new_notebook.py',notebook['resources'])
        self.assertTrue(any(p.endswith('.ipynb') for p in notebook['resources']))
        catalog=self.client.get('/api/agent/catalog/').json()['skills']
        for skill in catalog:
            self.assertNotIn('instructions',skill)
            self.assertNotIn('binary_resources',skill)
            self.assertNotIn('resources',skill)
        detail=self.client.get('/api/agent/skills/codex-jupyter-notebook/').json()
        self.assertNotIn('binary_resources',detail)
        self.assertIn('scripts/new_notebook.py',detail['resources'])

    def test_codex_bindings_remain_private_and_resources_are_loaded_on_demand(self):
        response=self.client.put(f'/api/courses/{self.course.id}/assistant/',{'skill_ids':['codex-jupyter-notebook']},format='json')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.sc.get(f'/api/courses/{self.course.id}/assistant/').json()['skill_ids'],[])
        with patch.dict(os.environ,{'MODEL_PROVIDER':'mock'}):
            self.stream('创建 Notebook')
        request=AgentRun.objects.get(thread=self.thread).events.get(event_type='llm_request').payload
        system=request['payload']['messages'][0]['content']
        self.assertIn('codex-jupyter-notebook',system)
        self.assertNotIn('Create clean, reproducible Jupyter notebooks',system)
        self.assertNotIn('new_notebook.py',system)
