"""Local publication-preview controls; no entitlement or payment service."""
from pathlib import Path
from studio_subscription_policy import describe,local_limit,read_config

VERSION = '2.6'
LOCAL_PIPELINE_LIMIT = 1  # Backward-compatible default preview value.

def public_policy(config=None):
    plans=describe(VERSION,config)
    return {'schema':'ai-studio-commercial-preview/v2','version':VERSION,
        'sales_enabled':False,'payments_enabled':False,'entitlement_connected':False,
        'device_activation_enforced':False,'account_login_limit_enforced':False,
        'account_pipeline_limit_enforced':False,
        'lower_plan_limits_enforced':VERSION=='3.0',
        'membership_allowance_in_api_budget':False,
        'build_revision':'bilingual-policy-20261008',
        'product_workflow':'Connect existing AI coding assistants; they use their own tools and subscription/API accounts.',
        'audience':'Not recommended for complete beginners; for personal developers and team creators with AI experience.',
        'intended_device_limits_all_versions':{'registered_devices':3,'simultaneous_signed_in_devices':2},
        'pause_releases_slot':'After saving and owned workers exiting, not merely closing the panel.',
        'external_ai_refusal_guaranteed':False,'complete_drm_implemented':False,
        'intended_v3_limits':{'activated_devices':3,'simultaneous_signed_in_devices':2,
            'pipelines_per_device':plans['selected_membership']['pipelines_per_device'],
            'pipelines_across_two_devices':plans['selected_membership']['pipelines_across_two_devices'],
            'account_limit_status':plans['selected_membership']['account_limit_status'],
            'buyout_and_membership_same_device_limits':False,'lower_plan_uses_smaller_limits':True},
        **plans}

def active_job_ids(transport):
    candidates=set(transport.processes)
    folder=Path(transport.root)/'jobs'
    if folder.is_dir():
        candidates.update(path.name for path in folder.iterdir() if path.is_dir() and not path.is_symlink()
                          and not getattr(path.stat(),'st_file_attributes',0)&0x400 and (path/'.pipeline_job').is_file())
    return {identifier for identifier in candidates if transport.get_job_runtime(identifier).get('running')}

def check_local_capacity(transport,job_id,starting_ids=(),config=None):
    # Caller holds the transport RLock through dispatch and registration.
    config=read_config(transport.root) if config is None else config
    cap=local_limit(VERSION,config)
    active=active_job_ids(transport)|set(starting_ids)
    if job_id in active:return
    if len(active)>=cap:
        raise RuntimeError('本版本所选本机预览最多同时运行 '+str(cap)+' 条流水线。请先暂停并保存另一条，等待其后台工位退出后再开始。 / '
                           'The selected local preview permits '+str(cap)+' concurrent pipelines. Pause/save another pipeline and wait for its owned workers to exit first.')

def check_application_capacity(application,job_id):
    with application.transport.lock:
        starting=[identifier for identifier,thread in application.service._threads.items() if thread.is_alive()]
        check_local_capacity(application.transport,job_id,starting,application.service._config())

def validate_preview_change(application,patch):
    from studio_subscription_policy import PREVIEW_FIELD,selected_tier
    if not isinstance(patch,dict):raise ValueError('Settings patch must be an object.')
    config=patch.get('config',{})
    if not isinstance(config,dict):raise ValueError('Configuration patch must be an object.')
    if PREVIEW_FIELD not in config:return
    new=selected_tier({PREVIEW_FIELD:config[PREVIEW_FIELD]})
    old=selected_tier(application.service._config())
    if new==old:return
    if VERSION!='3.0':raise ValueError('Membership includes the full 3.0 client. Legacy buyout builds remain single-line. / 会员使用完整3.0客户端，旧版买断仍单线。')
    if active_job_ids(application.transport) or any(thread.is_alive() for thread in application.service._threads.values()) or application.checks:
        raise ValueError('Pause/save all running pipelines and wait for their owned workers and connection checks to exit before changing a local preview tier. / 请先暂停保存并等待后台工位和连接检查结束，再切换本机预览套餐。')

def choose_preview(application,data):
    from studio_subscription_policy import PREVIEW_FIELD
    if not isinstance(data,dict) or set(data)!={'tier'}:raise ValueError('Preview selection accepts only a tier, never a price, license or quota override.')
    patch={'config':{PREVIEW_FIELD:data['tier']}}
    with application.guard,application.transport.lock:
        validate_preview_change(application,patch)
        application.service.save_settings(patch)
        return public_policy(application.service._config())

def reject_payment(path):
    if path in {'/api/commerce/purchase','/api/commerce/checkout','/api/commerce/subscribe','/api/commerce/activate','/api/commerce/intent'}:
        raise ValueError('销售与授权服务尚未开放；本候选不会下单、收款、激活许可或登记购买意向。 / '
                         'Sales and licensing are not available. This preview cannot order, charge, activate or register a purchase intent.')
