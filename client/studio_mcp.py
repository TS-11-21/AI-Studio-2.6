"""Apply explicitly enabled custom MCP connections to their selected roles."""
import json
import os
from pathlib import Path
import re


def role_servers(p,c,role,base):
    path=Path(c.get('mcp_file',p.ROOT/'mcp.local.json'))
    if not path.is_file(): return dict(base)
    p.ensure_plain_path(path)
    document=p.load(path)
    entries=document.get('mcpServers',{})
    if not isinstance(entries,dict): raise RuntimeError('MCP configuration must contain a server mapping.')
    result=dict(base)
    for name,entry in entries.items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',name) or not isinstance(entry,dict):
            raise RuntimeError('Invalid MCP server ID/configuration.')
        roles=entry.get('roles',['gameplay','vfx','integration','research'])
        if role not in roles: continue
        if not entry.get('enabled',True):
            result.pop(name,None); continue
        config=entry.get('config',entry)
        if name in base:
            # Keep the installed scratch-role bridge and its verified safety settings.
            result[name]=base[name]; continue
        if config.get('url'):
            url=config['url']
            if not isinstance(url,str) or not (url.startswith('https://') or url.startswith(('http://127.0.0.1:','http://localhost:'))):
                raise RuntimeError('MCP HTTP connections require HTTPS or an explicit loopback endpoint.')
            server={'url':url}
            if config.get('headers'):
                server['headers']=resolve_env_values(config['headers'],c)
        else:
            command=config.get('command')
            args=config.get('args',[])
            if not isinstance(command,str) or not isinstance(args,list) or not all(isinstance(arg,str) for arg in args):
                raise RuntimeError('A stdio MCP server needs a command and argument array.')
            prefix=[command] if Path(command).is_absolute() else p.executable(command)
            if Path(prefix[0]).suffix.lower() in ('.cmd','.bat','.ps1'):
                raise RuntimeError('Use a native executable for an MCP server.')
            server={'command':prefix[0],'args':prefix[1:]+args}
            if config.get('env'): server['env']=resolve_env_values(config['env'],c)
        result[name]=server
    return result


def resolve_env_values(values,c):
    if not isinstance(values,dict): raise RuntimeError('MCP environment/headers must be a JSON object.')
    resolved={}
    for name,value in values.items():
        if not isinstance(name,str) or not isinstance(value,str): raise RuntimeError('MCP environment/headers must contain strings.')
        match=re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}',value)
        if match:
            value=c.get('_mcp_environment',{}).get(match[1],os.environ.get(match[1]))
            if value is None: raise RuntimeError('A configured MCP environment reference is not available: '+match[1])
        resolved[name]=value
    return resolved


def append_codex_server(name,tool,quoted):
    text='\n[mcp_servers.'+name+']\n'
    if 'url' in tool:
        text+='url = '+quoted(tool['url'])+'\n'
        if tool.get('headers'):
            text+='[mcp_servers.'+name+'.http_headers]\n'
            text+=''.join(quoted(key)+' = '+quoted(value)+'\n' for key,value in tool['headers'].items())
    else:
        text+='command = '+quoted(tool['command'])+'\nargs = ['+', '.join(quoted(arg) for arg in tool['args'])+']\n'
        text+='required = true\nstartup_timeout_sec = 60\ntool_timeout_sec = 180\n'
        if tool.get('env'):
            text+='[mcp_servers.'+name+'.env]\n'
            text+=''.join(quoted(key)+' = '+quoted(value)+'\n' for key,value in tool['env'].items())
    return text
