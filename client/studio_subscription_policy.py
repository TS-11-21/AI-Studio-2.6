"""Data-only commercial plan preview; no entitlement/payment network client.



Local tier selection is a preview preference, never proof of a paid license.

Account-wide devices/sign-ins/line counts require the future authorization service.

"""

from __future__ import annotations

import copy,json

from pathlib import Path



SCHEMA='ai-studio-commercial-plan-policy/v2'

PREVIEW_FIELD='commercial_preview_tier'

DEFAULT_TIER='maximum'  # Preserve previous local-preview 8-line behavior.

TIERS={

 'standard':{'name':'Standard','name_zh':'标准会员','monthly_price_usd':None,
             'introductory_price_cents':None,'introductory_months':0,'regular_monthly_price_cents':None,
             'price_status':'not_published',

             'product_version':'3.0','complete_v3_features':True,

             'pipelines_per_device':2,'pipelines_across_two_devices':3,

             'account_limit_status':'intended_not_server_enforced'},

 'deluxe':{'name':'Deluxe','name_zh':'豪华会员','monthly_price_usd':None,

           'introductory_price_cents':None,'introductory_months':0,'regular_monthly_price_cents':None,

           'product_version':'3.0','complete_v3_features':True,

           'pipelines_per_device':5,'pipelines_across_two_devices':8,

           'account_limit_status':'intended_not_server_enforced'},

 'maximum':{'name':'Maximum','name_zh':'最高档会员','monthly_price_usd':None,
            'regular_monthly_price_cents':None,'price_status':'not_published',

            'product_version':'3.0','complete_v3_features':True,

            'pipelines_per_device':8,'pipelines_across_two_devices':10,

            'account_limit_status':'intended_not_server_enforced'},

}



def selected_tier(config=None):

    config={} if config is None else config

    if not isinstance(config,dict):raise ValueError('Commercial preview configuration must be an object.')

    tier=config.get(PREVIEW_FIELD,DEFAULT_TIER)

    if not isinstance(tier,str) or tier not in TIERS:raise ValueError('Choose standard, deluxe or maximum for the local commercial preview.')

    return tier



def read_config(root):

    path=Path(root)/'config.local.json'

    if not path.exists():return {}

    if path.is_symlink() or getattr(path.stat(),'st_file_attributes',0)&0x400 or path.stat().st_size>2*1024*1024:

        raise ValueError('A plain bounded local preview configuration is required.')

    data=json.loads(path.read_text(encoding='utf8'))

    if not isinstance(data,dict):raise ValueError('Local preview configuration must be an object.')

    return data



def local_limit(version,config=None):

    # The physical legacy buyout builds remain one line even if a preview

    # membership tier was saved. Membership product is the full 3.0 client.

    if version in ('1.0','2.6'):return 1

    if version!='3.0':raise ValueError('Unsupported commercial client version.')

    return TIERS[selected_tier(config)]['pipelines_per_device']



def describe(version,config=None):

    tier=selected_tier(config);selected=copy.deepcopy(TIERS[tier])

    selected.update(id=tier,preview_only=True,license_verified=False)

    options=[]

    for name,value in TIERS.items():

        entry=copy.deepcopy(value)

        entry.update(id=name,title=value['name_zh'],title_en=value['name'],

                     local_pipeline_limit=value['pipelines_per_device'],

                     account_pipeline_limit=value['pipelines_across_two_devices'])

        entry['price']={'currency':'USD','regular_monthly_cents':value.get('regular_monthly_price_cents'),
                        'preview_monthly_cents':None,

                        'introductory_monthly_cents':value.get('introductory_price_cents'),

                        'introductory_months':value.get('introductory_months',0),

                        'sales_enabled':False,'price_status':'not_published'}

        options.append(entry)

    return {'schema':SCHEMA,'status':'local_preview_only','selected_tier':tier,'tier_options':options,'selected_preview_tier':tier,

        'preview_selection_field':PREVIEW_FIELD,'selected_membership':selected,

        'membership_plans':[dict(copy.deepcopy(value),id=name) for name,value in TIERS.items()],

        'all_membership_tiers_complete_v3':True,

        'legacy_buyout_pipeline_limits':{'1.0':1,'2.6':1},

        'membership_requires_v3_client':version!='3.0',

        'local_controller_pipeline_limit':local_limit(version,config),

        'activated_devices_per_account':3,'simultaneous_signed_in_devices_per_account':2,

        'device_activation_enforced':False,'account_login_limit_enforced':False,

        'account_pipeline_limit_enforced':False,'selection_is_entitlement':False,

        'cross_controller_single_device_limit_enforced':False,

        'limits_count':'distinct running production pipelines; not saved plans, AI stations or individual members',

        'local_limit_scope':'one commercial desktop controller and its data directory; not an account entitlement'}

