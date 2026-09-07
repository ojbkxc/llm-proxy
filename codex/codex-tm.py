#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

TMUX='tmux'; WORKDIR='/opt/Codex'; DEFAULT_SCENE='Codex'; SCENARIOS_CONF=os.path.join(WORKDIR,'Codex-scenarios.conf'); CONF_SEP='|'
DEFAULTS={'model':os.environ.get('CODEX_MODEL','kimi-k2.7-code'),'sandbox':os.environ.get('CODEX_SANDBOX','workspace-write'),'approval':os.environ.get('CODEX_APPROVAL','never')}
BUILTIN_SCENES={'glm':{'model':'glm-5.3','sandbox':'workspace-write','approval':'never'},'deep':{'model':'DeepSeek-v4-pro-0813','sandbox':'workspace-write','approval':'never'},'kimi':{'model':'kimi-k2.7-code','sandbox':'workspace-write','approval':'never'},'dfast':{'model':'DeepSeek-v4-flash-0731','sandbox':'workspace-write','approval':'never'},'gfast':{'model':'glm-5.3-flash','sandbox':'workspace-write','approval':'never'}}
def sh(cmd):
 try:
  p=subprocess.run(cmd,capture_output=True,text=True,timeout=30); return p.returncode,p.stdout.strip()
 except subprocess.TimeoutExpired:return 124,''
def sh_tmux(args):return sh([TMUX]+args)
def ensure_tmux():return shutil.which(TMUX) is not None
def load_scenarios():
 scenes={k:dict(v) for k,v in BUILTIN_SCENES.items()}; scenes[DEFAULT_SCENE]=dict(DEFAULTS)
 if os.path.exists(SCENARIOS_CONF):
  for line in open(SCENARIOS_CONF,encoding='utf-8'):
   parts=line.strip().split(CONF_SEP)
   if parts and parts[0] and not parts[0].startswith('#'): scenes[parts[0]]={'model':parts[1] if len(parts)>1 else DEFAULTS['model'],'sandbox':parts[2] if len(parts)>2 else DEFAULTS['sandbox'],'approval':parts[3] if len(parts)>3 else DEFAULTS['approval']}
 return scenes
def session_exists(name):return sh_tmux(['has-session','-t',name])[0]==0
def is_codex_running(name):return 'Codex' in sh_tmux(['list-panes','-t',name,'-F','#{pane_current_command}'])[1]
def start(scene_name):
 s=load_scenarios()[scene_name]
 if not session_exists(scene_name):sh_tmux(['new-session','-d','-s',scene_name,'-c',WORKDIR])
 key=os.environ.get('CUSTOM_API_KEY')
 env=('export CUSTOM_API_KEY='+shlex.quote(key)+'; ') if key else ''
 run=env+f'cd {shlex.quote(WORKDIR)} && Codex -c model={shlex.quote(s["model"])} -c sandbox_mode={shlex.quote(s["sandbox"])} -c approval_policy={shlex.quote(s["approval"])}'
 sh_tmux(['send-keys','-t',scene_name,run,'Enter']); print('[启动] '+run)
def attach(name):sh_tmux(['attach','-t',name])
def stop(name=None):
 for n in ([name] if name else load_scenarios()):
  if session_exists(n):sh_tmux(['kill-session','-t',n])
def list_sessions():
 for n,s in load_scenarios().items():print(n,s)
def exec_msg(prompt,scene_name):return subprocess.run(['Codex','exec',prompt],timeout=1800).returncode
def start_all():
 for n in load_scenarios():start(n)
def main():
 p=argparse.ArgumentParser();p.add_argument('action',nargs='?',default='menu');p.add_argument('scene',nargs='?');p.add_argument('prompt',nargs='?');a=p.parse_args()
 if a.action=='start':start(a.scene or DEFAULT_SCENE)
 elif a.action=='start-all':start_all()
 elif a.action=='attach':attach(a.scene or DEFAULT_SCENE)
 elif a.action=='stop':stop(a.scene)
 elif a.action=='list':list_sessions()
 elif a.action=='exec':exec_msg(a.prompt or '',a.scene or DEFAULT_SCENE)
if __name__=='__main__':main()
