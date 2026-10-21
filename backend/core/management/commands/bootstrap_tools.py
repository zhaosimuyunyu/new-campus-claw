from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand
from core.models import User, MCPServer
from core.mcp_client import discover
import time


class Command(BaseCommand):
    help = 'Install the local platform MCP tool catalogue for every authenticated user.'
    def handle(self,*args,**kwargs):
        user,created=User.objects.get_or_create(email='platform-tools@campusclaw.local',defaults={'display_name':'平台工具服务','is_active':False})
        if created:
            user.set_unusable_password();user.save()
        server,_=MCPServer.objects.get_or_create(owner=user,name='platform-tools',defaults={'url':'http://campus-tools:8100/mcp','token_env':'MCP_CAMPUS_TOKEN','is_shared':True})
        for attempt in range(3):
            try:
                server.tools,server.session_id=async_to_sync(discover)(server)
                server.connected,server.error,server.is_shared=True,'',True;server.save()
                self.stdout.write(f'Platform MCP ready: {len(server.tools)} tools');return
            except Exception as exc:
                server.connected,server.error=False,str(exc)[:300];server.save()
                if attempt<2:time.sleep(2)
        self.stderr.write('Platform MCP unavailable; normal chat remains available. Run bootstrap_tools again after starting campus-tools.')
