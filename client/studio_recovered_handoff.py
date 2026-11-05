"""Recognize only a missing owned HANDOFF probe restored by the same call.

The raw SDK file stays private. Only tool-use/result relations are examined;
thinking/text/auth are neither exposed nor interpreted. Error counts stay intact.
"""
from __future__ import annotations

import json
from pathlib import Path

from studio_source_repair_evidence import _plain
from studio_tool_contracts import tool_result_outcome

MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_HANDOFF_BYTES = 512 * 1024


def _owned_path(value, art):
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    path=Path(value)
    if '..' in path.parts:
        return None
    return path.absolute() if path.is_absolute() else (art/path).absolute()


def recovered_optional_handoff(t, attempt, tools):
    """Return true only for the exact recovered optional probe; never mutate."""
    try:
        if type(attempt) is not int or attempt not in (2,3) or not isinstance(tools, dict):
            return False
        if type(tools.get('failed_tool_count')) is not int or tools['failed_tool_count'] != 1 or tools.get('failed_tool_names') != ['Read']:
            return False
        if type(tools.get('rejected_tool_count')) is not int or tools['rejected_tool_count'] != 0 or tools.get('rejected_tool_names', []) != []:
            return False
        if any(name.startswith('mcp__') for name in tools.get('failed_tool_names', []) + tools.get('rejected_tool_names', [])):
            return False
        if not {'Write', 'StructuredOutput'}.issubset(tools.get('successful_tool_names', [])):
            return False
        art=_plain(t['art'], directory=True)
        handoff=_plain(art/'HANDOFF.md')
        if not 0<handoff.stat().st_size<=MAX_HANDOFF_BYTES:
            return False
        log=_plain(art/'.pipeline'/('claude_vfx-%d.log'%attempt))
        if log.stat().st_size>MAX_LOG_BYTES:
            return False
        uses={};results={};index=0;failed=[];sdk_done=None
        with log.open('rb') as stream:
            for raw in stream:
                if len(raw)>MAX_LINE_BYTES:
                    return False
                try:event=json.loads(raw)
                except (UnicodeDecodeError,ValueError):return False
                if not isinstance(event,dict):return False
                if event.get('type')=='result':
                    if sdk_done is not None:return False
                    sdk_done=(index,event);index+=1
                    continue
                message=event.get('message')
                if not isinstance(message,dict):continue
                content=message.get('content')
                if not isinstance(content,list):continue
                for block in content:
                    if not isinstance(block,dict):continue
                    if block.get('type')=='tool_use':
                        if event.get('type')!='assistant' or message.get('role')!='assistant':return False
                        identity=block.get('id');name=block.get('name')
                        if not isinstance(identity,str) or identity in uses or not isinstance(name,str):return False
                        uses[identity]=(index,block);index+=1
                    elif block.get('type')=='tool_result':
                        if event.get('type')!='user' or message.get('role')!='user':return False
                        identity=block.get('tool_use_id')
                        if identity not in uses or identity in results:return False
                        results[identity]=(index,block);index+=1
                        outcome=tool_result_outcome(block,uses[identity][1]['name'])
                        if outcome=='rejected':return False
                        if outcome=='failed':failed.append(identity)
        if len(failed)!=1 or not sdk_done or set(uses)!=set(results):
            return False
        fail_id=failed[0];read_index,read=uses[fail_id];error_index,error=results[fail_id]
        if read['name']!='Read' or not isinstance(read.get('input'),dict) or _owned_path(read['input'].get('file_path'),art)!=handoff:
            return False
        if not isinstance(error.get('content'),str) or not error['content'].startswith('File does not exist.'):
            return False
        done_index,done=sdk_done
        if done.get('subtype')!='success' or done.get('is_error') is not False or not isinstance(done.get('structured_output'),dict) or done['structured_output'].get('status')!='done':
            return False
        restored=[];formal=[]
        for identity,(start,use) in uses.items():
            end,result=results[identity]
            if result.get('is_error') is True:continue
            if use['name']=='Write' and isinstance(use.get('input'),dict) and _owned_path(use['input'].get('file_path'),art)==handoff:
                value=use['input'].get('content')
                reply=result.get('content')
                if not isinstance(value,str) or not isinstance(reply,str) or not reply.startswith('File created successfully at: '):continue
                reply_path=reply.removeprefix('File created successfully at: ').split(' (file state is current in your context ',1)[0].strip()
                if _owned_path(reply_path,art)!=handoff or handoff.read_bytes()!=value.encode('utf8'):continue
                if error_index<start<end<done_index:restored.append(end)
            elif use['name']=='StructuredOutput' and isinstance(use.get('input'),dict) and use['input'].get('status')=='done':
                if isinstance(result.get('content'),str) and result['content'].startswith('Structured output provided successfully') and end<done_index:
                    formal.append(start)
        return len(restored)==1 and len(formal)==1 and restored[0]<formal[0]
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        return False
