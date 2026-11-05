"""Explicit assistant presets. Catalog access never registers or runs anything."""
from __future__ import annotations
import copy
from pathlib import Path

CODE_CAPS=['code-edit','story-writing','asset-authoring','structured-output','godot-mcp','blender-mcp']
MANUAL_CAPS=CODE_CAPS+['manual-handoff','web-research']

def catalog():
    home=Path.home()
    rows=[{
        'id':'kimi-code-cli','label':'Kimi Code 原生 CLI（会员）','suggested_provider_id':'kimi-code-cli',
        'registration_enabled':True,'mode':'native-cli','provider':{
            'adapter':'kimi','billing':'subscription','command':['kimi'],'model':None,
            'capabilities':CODE_CAPS,'ready_roles':['gameplay','vfx','integration'],
            'settings_ref':'current-kimi-profile','name':'Kimi Code 原生 CLI',
            'settings':{'assistant_product':'Kimi Code','profile_dir':str(home/'.kimi-code'),
                        'managed_oauth_only':True}},
        'requirements':['需要已有 Kimi Code 会员 OAuth 登录；仅复制所选会员凭据到隔离私有工位，不继承 API Key。',
                        '模型为空时使用所选会员配置中的默认模型；CLI 命令可填已安装原生程序绝对路径数组。',
                        '研究角色需先确认搜索工具；默认只登记玩法、素材和整合能力。'],
        'verification':{'status':'cli_help_verified','local_version':'2.1.1','model_tested':False,'mcp_e2e_tested':False},
        'official_url':'https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html'
    },{
        'id':'zcode-desktop','label':'ZCode 桌面助手（人工交接）','suggested_provider_id':'zcode-desktop',
        'registration_enabled':True,'mode':'manual-desktop','provider':{
            'adapter':'manual-desktop','billing':'manual','command':[],'model':None,
            'capabilities':MANUAL_CAPS,'ready_roles':['gameplay','vfx','integration','research'],
            'settings_ref':'zcode-desktop-profile','name':'ZCode 桌面助手',
            'settings':{'assistant_product':'ZCode','handoff_mode':'local-task-packet'}},
        'requirements':['在已安装的 ZCode 桌面助手中打开本次隔离任务包，使用它自己的登录和模型。',
                        '完成后返回身份匹配的 completion.json 和实际文件；等待文件时 Studio 不调用模型。',
                        '已查官方入口为桌面；本轮未核实自动 headless CLI，不能标为无人值守端到端通过。'],
        'verification':{'status':'desktop_installed_manual_protocol','model_tested':False,'headless_verified':False},
        'official_url':'https://zcode.z.ai/en/docs/install'
    },{
        'id':'minimax-code-desktop','label':'MiniMax Code 桌面助手（人工交接）','suggested_provider_id':'minimax-code-desktop',
        'registration_enabled':True,'mode':'manual-desktop','provider':{
            'adapter':'manual-desktop','billing':'manual','command':[],'model':None,
            'capabilities':MANUAL_CAPS,'ready_roles':['gameplay','vfx','integration','research'],
            'settings_ref':'minimax-code-desktop-profile','name':'MiniMax Code 桌面助手',
            'settings':{'assistant_product':'MiniMax Code','handoff_mode':'local-task-packet'}},
        'requirements':['在 MiniMax Code 桌面助手中使用自己的账号/会员或余额，并仅打开所选任务目录。',
                        '通过实际 completion.json/资源文件交接；Studio 不直接替它请求模型 API。',
                        '官方另有 mcode exec 原生 CLI；本机尚未发现该独立 CLI，本轮不安装或声称模型实测。'],
        'verification':{'status':'desktop_installed_manual_protocol','model_tested':False,'headless_verified':False},
        'official_url':'https://agent.minimax.io/docs/cli/quick-start'
    },{
        'id':'minimax-code-cli-planned','label':'MiniMax Code 原生 CLI（待安装核验）','suggested_provider_id':'minimax-code-cli',
        'registration_enabled':False,'mode':'planned-native-cli','provider':None,
        'requirements':['官方已明确 mcode exec 和 ACP；本机独立 mcode 未安装/未发现。',
                        '需明确安装授权、本人登录和模型额度后，再验证实际帮助与协议；不是直接 API 适配。'],
        'verification':{'status':'official_headless_documented_not_local_cli_verified','model_tested':False},
        'official_url':'https://github.com/MiniMax-AI/minimax-code'
    },{
        'id':'claude-code-minimax-setup','label':'Claude Code 使用 MiniMax 官方模型服务（接入说明）',
        'suggested_provider_id':'claude-code-minimax','registration_enabled':False,'mode':'required_setup','provider':None,
        'requirements':['这是 Claude Code 助手使用 MiniMax 官方服务，不是 MiniMax Code 本体，也不是 Studio 直调 API。',
                        '需由使用者明确 Token Plan/按量计费和自己的凭据；本轮不扩大既有 Claude 账务或登录。'],
        'verification':{'status':'official_integration_documented_required_setup','model_tested':False},
        'official_url':'https://platform.minimax.io/docs/token-plan/claude-code'
    }]
    return copy.deepcopy(rows)
