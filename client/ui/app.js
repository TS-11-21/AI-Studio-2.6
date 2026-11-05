"use strict";
(() => {
  const PERSONAL_MODE = document.body.dataset.personalMode === "true";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const ROLES = {gameplay:{label:"玩法与剧情",stage:"gpt_base",symbol:"G"},vfx:{label:"动画 / VFX",stage:"claude_vfx",symbol:"V"},integration:{label:"游戏整合",stage:"gpt_integrate",symbol:"↔"},research:{label:"资料研究",stage:"research",symbol:"R"}};
  const ADAPTERS = {codex:"Codex · ChatGPT 订阅",claude:"Claude Code · 已有网关",kimi:"Kimi Code · 会员 CLI","openrouter-research":"OpenRouter · 研究 API","external-command":"外部交接程序","manual-desktop":"桌面人工交接","openai-compatible-research":"API 兼容 · 研究"};
  const STATUSES = {CANCELLED:["本次任务已取消 · 配置保留","neutral"],CANCELLING:["正在取消本次任务","working"],CANCEL_FAILED:["取消未完成 · 需要处理","bad"],DECLINED:["已拒绝增额","wait"],SUPERSEDED:["已被新计划替代","neutral"],WAITING_BUDGET:["等待预算批准","wait"],APPROVED:["已批准增额","good"],REJECTED:["已拒绝增额","wait"],NOT_REQUIRED:["无需额外汇总","neutral"],CURATING:["正在汇总交接","working"],WAITING:["等待依赖 / 交接","wait"],NEEDS_ATTENTION:["需要处理","bad"],DONE:["已完成","good"],RUNNING:["执行中","working"],READY:["待执行","neutral"],DORMANT:["休眠","neutral"],FAILED:["失败","bad"],BLOCKED:["需要处理","bad"],INTERRUPTED:["已中断","wait"],NEEDS_REFERENCE:["等待资料","wait"],WAITING_EXTERNAL:["等待桌面交接","wait"],PAUSED:["已暂停 · 进度已保存","wait"],PAUSING:["正在暂停并保存","working"],RESUMING:["正在恢复","working"],WAITING_CONNECTION:["等待接入 / 费用批准","wait"],WAITING_APPROVAL:["等待费用批准","wait"],BUDGET_STOP:["预算 / 次数停止","bad"],UNKNOWN:["未验证","neutral"],CONNECTED:["已连接","good"],AVAILABLE:["本地可用","good"],READY_LOCAL:["本地可用","good"],CONFIGURED:["已配置 · 待验证","neutral"],OFFLINE:["离线","bad"],ERROR:["连接异常","bad"],CHECKING:["检查中","working"],NOT_CHECKED:["未验证","neutral"],IDLE:["空闲","neutral"]};
  const PAGE = {home:["流水线工作室","每条流水线独立制作，多成员协作，按实际费用与事件执行。"],roles:["角色与接入","选择执行程序与模型，保留统一的工位交接。"],mcp:["MCP 插件","管理本地工具配置与工位归属。"],policy:["运行策略","设置有限调用、保存方式与 API 预算。"],project:["流水线配置","分别设置这条流水线的目标、团队、模型与预算。"],logs:["日志记录","查看实际模型、工具与机器验收记录。"],license:["版本与许可","比较真实功能与拟采用的商业规则；销售、支付与设备授权尚未开放。"],about:["关于 AI Studio","当前设备上的本地创作与交接控制面板。"]};
  const store = {state:{jobs:[],connections:{},app:{}},settings:{},settingsLoaded:false,selected:null,view:"home",refreshing:false,previous:new Map(),editingProvider:null,editingMcp:null,credentialProvider:null,approval:null};

  function node(tag, attrs = {}, children = []) { const element = document.createElement(tag); for (const [key,value] of Object.entries(attrs)) { if (value == null) continue; if(key === "text") element.textContent = String(value); else if(key === "class") element.className=value; else if(key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2),value); else if(key === "checked" || key === "disabled") element[key]=Boolean(value); else element.setAttribute(key,String(value)); } for (const child of children) if (child != null) element.append(typeof child === "string" ? document.createTextNode(child) : child); return element; }
  function text(selector, value) { const element = $(selector); if(element) element.textContent = String(value ?? "—"); }
  function toast(message, kind = "") { const item=node("div",{class:"toast "+kind,text:message}); $("#toast-region").append(item); setTimeout(()=>item.remove(),kind === "error" ? 10000 : 5500); }
  function statusInfo(value) { const key=String(value || "UNKNOWN").toUpperCase(); return STATUSES[key] || [String(value || "未验证"),"neutral"]; }
  function badge(status) { const [label,kind]=statusInfo(status); return node("span",{class:"badge "+kind,text:label}); }
  function plain(value) { if(value == null) return ""; if(typeof value === "string") return value; if(typeof value === "object") return String(value.summary || value.detail || value.message || value.action || value.tool || JSON.stringify(value)); return String(value); }
  function truncate(value, count=160) { const content=plain(value); return content.length>count ? content.slice(0,count)+"…" : content; }
  function money(value) { if(value == null || value === "") return "—"; const amount=Number(value); return Number.isFinite(amount) ? "$"+amount.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:5}) : "—"; }
  function providers() { return store.settings.providers || store.settings.registry?.providers || store.settings.workers?.providers || {}; }
  function effectiveModel(provider) { return provider.model || (provider.adapter === "codex" ? store.settings.config?.codex_model : provider.adapter === "claude" ? store.settings.config?.claude_model : null) || ""; }
  function providerRoleReady(provider,role) {
    const required=role === "vfx" ? ["asset-authoring","structured-output"] : role === "research" ? ["web-research","structured-output"] : ["code-edit","structured-output"];
    if(["codex","claude"].includes(provider.adapter) && role !== "research")required.push(role === "vfx" ? "blender-mcp" : "godot-mcp");
    return required.every(capability=>(provider.capabilities || []).includes(capability)) && (provider.ready_roles || []).includes(role);
  }
  function routes() { return store.settings.role_routes || store.settings.registry?.role_routes || store.settings.workers?.role_routes || {}; }
  function policy() { return store.settings.policy || {}; }
  function updatePolicyCapabilities() { const enabled=(store.state.app?.capabilities?.auto_retry ?? store.settings.capabilities?.auto_retry) === true; const form=$("#policy-form"); form.elements.auto_retry.disabled=!enabled; form.elements.max_retries.disabled=!enabled; text("#policy-capability-note",enabled ? "" : "自动返修尚未声明可用，当前保持关闭。"); }
  function jobs() { return Array.isArray(store.state.jobs) ? store.state.jobs : []; }
  function job() { return jobs().find(item=>item.id === store.selected || item.job_id === store.selected) || null; }
  function jobId(item) { return item?.pipeline_id || item?.id || item?.job_id || ""; }

  function jobKind(item=job()) { return item?.plan?.kind || item?.kind || item?.task?.kind || "fan-wave-demo"; }

  function roleLabel(role,item=job()) { return role === "vfx" && jobKind(item) === "software" ? "设计 / 资源" : role === "gameplay" && jobKind(item) === "software" ? "软件实现" : ROLES[role].label; }
  function stageData(item,stage) { const data=item?.stages || {},row=Array.isArray(data)?data.find(s=>s.name===stage) || {}:data[stage] || {};return {...row,...(item?.team?.stages?.[stage] || {}),...(stage === "qa" && item?.revalidation_required?{status:"READY",revalidation_required:true}:{})}; }
  function runningId() { const running=store.state.running_job; return typeof running === "object" ? running?.id || running?.job_id : running; }
  function providerFor(role,item=job()) { const members=planFor(item)?.roles?.[role]?.members || []; if(members.length){const member=members[0];return {id:member.provider_id,...pipelineProvider(item,member.provider_id),model:member.model || pipelineProvider(item,member.provider_id).model || null};} const stage=stageData(item,ROLES[role].stage),actual=stage.provider,local=item?.role_routes || routes(),fallback=Array.isArray(local[role])?local[role][0]:local[role],id=(typeof actual === "object"?actual?.provider_id || actual?.id:actual)||fallback;return {id:id || "",...(providers()[id] || {}),...(typeof actual === "object"?actual:{})}; }

  function budgetFor(item=job()) { return item?.budget || store.state.budgets?.[jobId(item)] || {}; }
  function unresolvedCosts(item=job()) { const budget=budgetFor(item),accounting=item?.checkpoint?.accounting || {},entries=budget.invocations || accounting.invocations || {},explicit=new Set([...(budget.unknown_invocations || []),...(budget.interrupted_invocations || [])]),values=Array.isArray(entries)?entries.map(entry=>[entry.invocation_id || entry.id,entry]):Object.entries(entries);return values.filter(([id,entry])=>id && (entry.state === "unknown" || explicit.has(id) || entry.state === "pending" && !isRunning(item) && (accounting.cost_reconciliation_required || ["PAUSED","INTERRUPTED","WAITING_CONNECTION"].includes(jobStatus(item))))).map(([id,entry])=>({id,...entry})); }

  function openReconciliation() {
    const item=job(), entries=unresolvedCosts(item);
    if(!item || !entries.length){toast("当前没有本地服务确认的未知或中断调用需要核对。","error");return;}
    store.reconcileJob=jobId(item);
    const form=$("#reconcile-form"); form.reset();
    form.elements.invocation_id.replaceChildren(...entries.map(entry=>node("option",{value:entry.id,text:entry.id})));
    form.elements.reported_cost_usd.value="";
    form.elements.confirmed_from_usage.checked=false;
    text("#reconcile-invocation-info","仅核对实际用量；已有支出、预留与总预算不会清零。");
    $("#reconcile-dialog").showModal();
  }
  function jobStatus(item) { if(["CANCELLED","CANCELLING","CANCEL_FAILED"].includes(item?.status))return item.status; if(!item)return "DORMANT";if(["PAUSING","PAUSED","RESUMING","WAITING_CONNECTION","WAITING_APPROVAL","WAITING_BUDGET","FAILED","BLOCKED","INTERRUPTED","BUDGET_STOP","DONE"].includes(item.status))return item.status;if(item.paused)return "PAUSED";if(stageData(item,"qa").status === "DONE")return "DONE";if(isRunning(item))return "RUNNING";const stages=Object.values(item.stages || {}),failed=stages.find(s=>["FAILED","BUDGET_STOP","BLOCKED","INTERRUPTED","WAITING_EXTERNAL","NEEDS_REFERENCE","WAITING_CONNECTION","WAITING_APPROVAL"].includes(s.status));return failed?.status || item.status || "READY"; }

  async function api(path, options={}) { const method=options.method || "GET"; const headers={"Accept":"application/json"}; if(method !== "GET") { headers["Content-Type"]="application/json"; headers["X-Studio-Token"]=String(window.STUDIO_TOKEN || ""); } const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),20000); try { const response=await fetch(path,{method,headers,credentials:"same-origin",cache:"no-store",signal:controller.signal,...(options.body !== undefined ? {body:JSON.stringify(options.body)} : {})}); let data; try { data=await response.json(); } catch { throw new Error("本地服务没有返回有效状态，请确认桌面服务已启动。"); } if(!response.ok || data.ok === false) throw new Error(plain(data.error || data.detail || data.message) || "操作失败（"+response.status+"）"); return data.data ?? data; } catch(error) { if(error.name === "AbortError") throw new Error("本地服务响应超时。任务可能仍在执行，请刷新真实状态。"); throw error; } finally { clearTimeout(timer); } }
  async function action(path, body, success) { try { const response=await api(path,{method:"POST",body}); if(success) toast(success,"success"); await refreshState(); return response; } catch(error) { toast(error.message,"error"); return null; } }
  async function withBusy(button, fn) { const old=button.textContent; button.disabled=true; button.textContent="处理中…"; try { return await fn(); } finally { button.textContent=old; button.disabled=false; if(store.view === "home") renderHome(); } }
  function showView(view) { if(PERSONAL_MODE && view === "license")view="home";if(!PAGE[view])return;store.view=view;$$(".view").forEach(item=>item.classList.toggle("active",item.id === "view-"+view));$$(".nav-item").forEach(item=>item.classList.toggle("active",item.dataset.view===view));text("#page-title",PAGE[view][0]);text("#page-description",PAGE[view][1]);if(view === "logs")renderLogs();if(view === "policy")updatePolicyCapabilities();if(view === "project" && !store.pipelineDraft && store.settingsLoaded)openPipelineEditor(null,false); }

  function connectionFor(id) { const data=store.state.connections || {}; return Array.isArray(data) ? data.find(item=>item.id === id || item.provider_id === id) || {} : data[id] || {}; }
  function renderWorkers() {
    const item=job(),plan=planFor(item),container=$("#worker-cards");
    if(!item){container.replaceChildren(node("div",{class:"empty panel",text:"先创建流水线。团队状态将来自真实成员记录。"}));return;}
    const rows=Object.entries(ROLES).map(([role,meta])=>{
      const config=plan.roles?.[role] || {},runtime=roleRuntime(item,role),budget=runtime.budget || item.team?.budget?.roles?.[role] || budgetFor(item).roles?.[role] || {},members=config.members || [];
      const cards=members.map(member=>{
        const real=memberRuntime(item,role,member.id),provider=pipelineProvider(item,member.provider_id),connection=connectionFor(member.provider_id),cost=real.budget || item.team?.budget?.members?.[member.id] || budgetFor(item).members?.[member.id] || real;
        const state=real.status || (members.length===1?stageData(item,meta.stage).status:null) || (role === "research"?"DORMANT":"READY");
        const latest=real.latest_tool_event || real.tool_event,tail=Array.isArray(real.log_tail)?real.log_tail.at(-1):real.log_tail;
        const stageKey=real.stage || real.stage_id || memberStageKey(item,role,member.id);
        const actions=node("div",{class:"member-actions"},[node("button",{class:"button small quiet",text:"查看成员日志",onclick:()=>{selectJob(jobId(item));$("#log-stage").value="all";$("#log-member").value=member.id;showView("logs");}})]);
        if(["FAILED","BLOCKED","INTERRUPTED"].includes(state))actions.append(node("button",{class:"button small danger",text:"重试此成员",disabled:isRunning(item)||!stageKey,onclick:()=>{selectJob(jobId(item));openApproval("retry",null,stageKey,member.id);}}));
        return node("article",{class:"team-member"},[node("div",{class:"worker-header"},[node("strong",{text:member.id}),badge(state === "RUNNING" && String(real.phase || "").toUpperCase().includes("CURAT")?"CURATING":state)]),node("div",{class:"provider-name",text:member.provider_id}),node("div",{class:"worker-model",text:real.effective_model || real.model || member.model || effectiveModel(provider) || "使用已保存的默认模型"}),node("div",{class:"member-connection"},[badge(connection.status || "NOT_CHECKED"),node("span",{text:provider.billing === "metered"?"API 用量":provider.billing === "manual"?"人工交接":"订阅额度"})]),node("div",{class:"member-assignment",title:member.assignment,text:member.assignment || "共同完成该角色职责"}),memberBudgetDisplay(provider,member,cost),node("div",{class:"worker-activity","data-i18n-raw":Boolean(real.activity || real.summary || real.error || real.detail),title:plain(real.activity || real.summary || real.error || real.detail),text:truncate(real.activity || real.detail || (state === "DORMANT"?"按需唤醒，目前没有模型调用。":"尚无实际执行事件"),180)}),node("div",{class:"worker-event","data-i18n-raw":Boolean(latest || tail),title:plain(latest || tail),text:latest?"工具事件 · "+truncate(latest,110):tail?"最新日志 · "+truncate(tail,110):"暂无实际工具事件"}),node("div",{class:"required-label",text:(member.required === false?"可选成员 · 失败不阻塞必需产物":"必需成员 · 参与交接验收")+(real.attempts!=null?" · 阶段尝试 "+real.attempts+" 次":"")}),actions]);
      });
      return node("section",{class:"panel team-role"},[node("div",{class:"panel-heading"},[node("div",{class:"role-title"},[node("span",{class:"role-symbol "+role,text:meta.symbol}),node("h2",{text:roleLabel(role,item)}),node("span",{class:"muted",text:members.length+" 位成员"})]),badge(members.some(member=>{const actual=memberRuntime(item,role,member.id);return actual.status === "RUNNING" && String(actual.phase || "").toUpperCase().includes("CURAT");})?"CURATING":runtime.status || stageData(item,meta.stage).status || "DORMANT")]),roleBudgetDisplay(item,role,config,budget),...(runtime.curation_status?[node("div",{class:"curation-note",text:"汇总交接："+statusInfo(runtime.curation_status)[0]+(runtime.lead_id?" · 汇总成员 "+runtime.lead_id:"")+(Array.isArray(runtime.conflicts)&&runtime.conflicts.length?" · 存在 "+runtime.conflicts.length+" 项冲突，请查看日志":"")})]:[]),node("div",{class:"team-member-grid"},cards.length?cards:[node("div",{class:"empty",text:"该角色尚未配置成员"})])]);
    });
    container.replaceChildren(...rows);
    const ownIds=new Set(Object.values(plan.roles || {}).flatMap(r=>(r.members || []).map(m=>m.provider_id))),failed=Object.entries(store.state.connections || {}).filter(([id,value])=>ownIds.has(id)&&["FAILED","OFFLINE","ERROR"].includes(String(value.status).toUpperCase()));
    text("#connection-warning",failed.length?"本流水线接入异常："+failed.map(([id,value])=>id+"（"+truncate(value.detail || value.status,100)+"）").join("；")+"。其他就绪成员及其他流水线保留各自进度；依赖此成员的产物会等待。":"");$("#connection-warning").classList.toggle("hidden",!failed.length);
  }

  function renderHome() {
    const item=job(),state=jobStatus(item),active=isRunning(item),plan=planFor(item);
    text("#line-count",jobs().length);text("#running-count",jobs().filter(isRunning).length);text("#paused-count",jobs().filter(j=>jobStatus(j)==="PAUSED").length);
    text("#job-title",item?jobId(item):"先创建一条流水线");$("#job-goal").dataset.i18nEmpty=plan.goal || item?.goal ? "false":"true";text("#job-goal",plan.goal || item?.goal || "每条流水线拥有自己的成员、模型、预算和进度。");const [label,kind]=statusInfo(item?state:"DORMANT");$("#job-status").className="badge "+kind;text("#job-status",item?label:"未选择流水线");
    const steps=[["gpt_base","玩法"],["claude_vfx","资源"],["gpt_integrate","整合"],["qa","机器验收"]];$("#stage-flow").replaceChildren(...steps.flatMap(([id,title],index)=>{const current=stageData(item,id),role=Object.entries(ROLES).find(([,r])=>r.stage===id)?.[0];return [node("div",{class:"flow-step "+(current.status === "DONE"?"done":current.status === "RUNNING"?"running":["FAILED","BLOCKED","BUDGET_STOP"].includes(current.status)?"failed":"")},[node("div",{class:"flow-label",text:(role?roleLabel(role,item):title)+(index<2?" · 并行":"")}),node("div",{class:"flow-status",text:current.revalidation_required?"待重验":statusInfo(current.status || "DORMANT")[0]})]),...(index<3?[node("span",{class:"flow-arrow",text:index===0?"+":"→"})]:[])];}));
    const activeStage=Object.values(item?.stages || {}).find(s=>s.status === "RUNNING") || Object.values(item?.stages || {}).find(s=>["FAILED","BLOCKED","WAITING_CONNECTION","WAITING_EXTERNAL","NEEDS_REFERENCE","WAITING_APPROVAL","PAUSING"].includes(s.status));
    text("#job-activity",state === "CANCELLED" ? "本次任务已取消，已生成内容不可恢复；配置与已发生费用保留，可编辑后重新开始。" : item?.revalidation_required?"任务已修改，恢复后重新制作受影响部分并验收":activeStage?truncate(activeStage.activity || activeStage.detail || statusInfo(activeStage.status)[0],320):state === "DONE"?"本流水线机器验收已完成，请核对实际交付文件。":"等待实际事件；本地状态刷新不会调用模型。");
    $("#start-job").disabled=!item || !store.settingsLoaded || active || state === "DONE" || state === "CANCELLED" || item?.can_start === false;$("#resume-job").disabled=!item || !store.settingsLoaded || active || state === "DONE" || state === "CANCELLED";$("#pause-job").disabled=!item || state === "PAUSING";
    const locked=!planEditable(item);$("#edit-pipeline").disabled=!item || locked;$("#clone-pipeline").disabled=!item;$("#budget-edit-pipeline").disabled=!item;$("#budget-edit-pipeline").textContent=locked?"查看已保存的团队配置":"编辑团队与预算";text("#plan-lock-note",!item?"先选择流水线。":state === "CANCELLED" ? "本次任务已取消，请先编辑并保存新任务，再明确开始。流水线配置及历史费用保留。" :editMode(item)==="ready"?"新任务已保存，可继续调整任务与预算；旧成员身份保持。点击开始并确认费用后才执行。":editMode(item)==="paused"?"本线已暂停且无执行进程，可调整目标、分工和预算。账号、模型与成员身份保持原值；保存不会自动恢复。":locked?"执行中或已完成的配置不能直接编辑。暂停并收尾后可修改预算与分工；需要改变接入组合请克隆。":"尚未开始，可完整编辑本线配置。");
    const retry=$("#retry-stage"),old=retry.value,eligible=retryChoices(item);retry.replaceChildren(...(eligible.length?eligible.map(([id,s])=>node("option",{value:id,text:(s.member_id? s.member_id+" · ":"")+(Object.values(ROLES).find(r=>r.stage===id)?.label || (id === "qa"?"机器验收":id))})):[node("option",{value:"",text:"没有失败项"})]));if(eligible.some(([id])=>id===old))retry.value=old;$("#retry-job").disabled=!item || active || !eligible.length || state === "CANCELLED";
    $$("[data-open]").forEach(button=>button.disabled=!item || state === "CANCELLED" || button.dataset.open === "play" && state !== "DONE");renderBudget();renderJobs();renderWorkers();renderBudgetRequests();
  }

  function renderBudget() {
    const item=job(),budget=budgetFor(item),plan=planFor(item),cap=budget.total_cap_usd ?? budget.total_cap ?? budget.cap ?? budget.job_cap_usd ?? plan.total_budget_usd,spent=budget.spent_usd ?? budget.spent ?? budget.production_reported_cost,reserved=budget.reserved_usd ?? budget.reserved,available=budget.available_usd ?? budget.available ?? budget.remaining_usd;
    $("#budget-total").replaceChildren(document.createTextNode(cap == null?"—":money(cap)),node("span",{text:"USD"}));text("#budget-spent",money(spent));text("#budget-reserved",money(reserved));text("#budget-available",money(available));
    const unknown=(budget.unknown_invocations || []).length || budget.costs_complete === false && budget.blocked;let message=unknown?"费用未知或有遗留预留，本流水线新的收费调用停止，需核对真实费用。":budget.blocked?plain(budget.block_reasons || "预算条件不满足，新的收费调用已停止。"):"本流水线收费成员共用累计总预算，同时受角色共享限额和成员限额约束。各流水线独立记账。";if(budget.connection_reported_cost!=null)message+=" 另有连接测试费用 "+money(budget.connection_reported_cost)+"。";text("#budget-note",item?message:"创建流水线后显示独立预算与真实用量。");$("#budget-note").classList.toggle("danger-text",Boolean(budget.blocked || unknown));const button=$("#reconcile-cost"),entries=unresolvedCosts();if(button){button.classList.toggle("hidden",!entries.length && !budget.blocked && !item?.checkpoint?.accounting?.cost_reconciliation_required);button.disabled=!entries.length;}
  }

  function renderJobs() {
    const list=$("#job-list"),signature=JSON.stringify([store.selected,jobs().map(item=>({id:jobId(item),plan:item.plan,status:jobStatus(item),budget:item.budget,stages:item.stages,team:item.team,editable:item.editable,edit_mode:item.edit_mode}))]);
    if(store.jobsSignature!==signature || !list.childElementCount){store.jobsSignature=signature;list.replaceChildren(...(jobs().length?jobs().map(item=>{const plan=planFor(item),members=Object.values(plan.roles || {}).flatMap(r=>r.members || []),budget=budgetFor(item),actions=node("div",{class:"pipeline-card-actions"});actions.append(node("button",{class:"button small "+(jobId(item)===store.selected?"primary":"quiet"),text:jobId(item)===store.selected?"当前查看":"查看团队",onclick:()=>selectJob(jobId(item))}),node("button",{class:"button small quiet",text:"克隆",onclick:()=>openClone(item)}),node("button",{class:"button small quiet",text:"编辑",disabled:!planEditable(item),onclick:()=>openPipelineEditor(item)}),node("button",{class:"button small",text:isRunning(item)?"本线执行中":jobStatus(item)==="PAUSED"?"恢复":"开始",disabled:!store.settingsLoaded || isRunning(item) || jobStatus(item)==="DONE",onclick:()=>{selectJob(jobId(item));openApproval(jobStatus(item)==="PAUSED"?"resume":"start");}}));return node("article",{class:"pipeline-card "+(jobId(item)===store.selected?"selected":"")},[node("div",{class:"pipeline-card-head"},[node("strong",{text:jobId(item)}),badge(jobStatus(item))]),node("div",{class:"pipeline-card-kind",text:kindName(jobKind(item))}),node("p",{class:"pipeline-card-goal","data-i18n-raw":Boolean(plan.goal || item.goal),title:plan.goal || item.goal,text:truncate(plan.goal || item.goal || "尚未填写目标",110)}),node("div",{class:"pipeline-card-stats"},[node("span",{text:members.length+" 位成员"}),node("span",{text:"总限额 "+money(plan.total_budget_usd)}),node("span",{text:"API已记录 "+money(budget.spent_usd ?? budget.spent)})]),node("div",{class:"pipeline-role-counts",text:Object.entries(ROLES).map(([role])=>roleLabel(role,item)+" "+(plan.roles?.[role]?.members?.length || 0)).join(" · ")}),actions]);}):[node("div",{class:"empty",text:"暂无流水线。新建只保存配置，不会开始模型。"})]));}
    const logJob=$("#log-job"),ids=jobs().map(jobId),logSignature=JSON.stringify(ids);if(store.logJobsSignature!==logSignature){store.logJobsSignature=logSignature;const old=logJob.value;logJob.replaceChildren(...ids.map(id=>node("option",{value:id,text:id})));logJob.value=ids.includes(old)?old:store.selected || "";}
  }

  function selectJob(id) { store.selected=id;$("#log-job").value=id;store.logMembersSignature=null;$("#log-member").value="all";renderHome();renderLogs(); }

  function roleChecks(container, name, selected=[]) { container.replaceChildren(...Object.entries(ROLES).map(([role,meta])=>node("label",{class:"check-label"},[node("input",{type:"checkbox",name,value:role,checked:selected.includes(role)}),meta.label]))); }
  function roleCapabilities(roles, adapter) { const caps=new Set([...(providers()[store.editingProvider]?.capabilities || []),"structured-output"]); roles.forEach(role=>{if(["gameplay","integration"].includes(role)){caps.add("code-edit");caps.add("story-writing");caps.add("godot-mcp");}if(role === "vfx"){caps.add("asset-authoring");caps.add("blender-mcp");}if(role === "research") caps.add("web-research");}); if(adapter === "manual-desktop") caps.add("manual-handoff"); return [...caps]; }
  function availableAdapters() { const declared=store.settings.available_adapters || store.state.app?.supported_adapters; const values=declared ? (Array.isArray(declared) ? declared : Object.keys(declared)) : Object.keys(ADAPTERS).filter(id=>id!=="openai-compatible-research"); return [...new Set([...values,...Object.values(providers()).map(p=>p.adapter)])].filter(Boolean); }
  function renderSettings() {
    $("#role-config").replaceChildren(...Object.entries(ROLES).map(([role,meta])=>{const current=routes()[role],selected=Array.isArray(current)?current[0]:current,select=node("select",{name:role,"aria-label":meta.label,required:"required"});select.append(...Object.entries(providers()).filter(([,p])=>providerRoleReady(p,role)).map(([id,p])=>node("option",{value:id,text:id+" · "+(effectiveModel(p) || ADAPTERS[p.adapter] || p.adapter)})));if(selected)select.value=selected;return node("label",{class:"field"},[node("span",{text:meta.label+" · 新流水线默认"}),select,node("small",{text:"只是初始成员的默认接入；每条流水线可分别增删成员。"})]);}));renderProviders();renderMcp();const form=$("#policy-form"),settings=policy();for(const field of ["token_saving","auto_retry","autosave","notify"])form.elements[field].checked=Boolean(settings[field]);for(const field of ["max_retries","job_budget_usd","connection_budget_usd"])if(settings[field]!=null)form.elements[field].value=settings[field];updatePolicyCapabilities();const details=store.settings.project || {};$("#workspace-details").replaceChildren(...Object.entries({"2.6工作室":details.studio_path || store.state.app?.studio_path || store.state.app?.root,"团队快照":"每条流水线分别保存提供方、模型、工具与执行策略","当前流水线":store.selected || "未选择"}).map(([label,value])=>node("div",{},[node("dt",{text:label}),node("dd",{text:value || "服务尚未提供"})])));$("#about-details").replaceChildren(...Object.entries({"桌面版本":store.state.app?.version || "AI Studio 2.6","运行设备":store.state.app?.host || store.state.app?.platform || "当前 Windows 设备","正在执行":jobs().filter(isRunning).length+" 条流水线","既有版本保护":"一代、二代与2.6目录、入口和账本独立"}).map(([label,value])=>node("div",{},[node("dt",{text:label}),node("dd",{text:value})])));if(store.view === "project"&&!store.pipelineDraft)openPipelineEditor(null,false);
  }

  function renderProviders() {
    const container=$("#provider-list"), signature=JSON.stringify([providers(),store.state.connections,store.settings.config?.codex_model,store.settings.config?.claude_model]);
    if(store.providerSignature===signature && container.childElementCount) return;
    store.providerSignature=signature;
    const rows=Object.entries(providers()).map(([id,p])=>{
      const connection=connectionFor(id);
      const actions=node("div",{class:"provider-actions"},[
        badge(connection.status || "NOT_CHECKED"),
        node("button",{class:"button small quiet",text:"检查连接",onclick:event=>withBusy(event.currentTarget,()=>checkConnection(id))}),
        node("button",{class:"button small quiet",text:"修改",onclick:()=>openProvider(id)}),
        node("button",{class:"button small danger",text:"移除",onclick:()=>removeProvider(id)})
      ]);
      if(p.adapter === "codex")actions.prepend(node("button",{class:"button small quiet",text:"登录账号",onclick:event=>withBusy(event.currentTarget,()=>action("/api/login",{provider_id:id},"登录流程已交给本地接入；登录或验证需要你本人完成"))}));
      if(p.billing === "metered")actions.prepend(node("button",{class:"button small quiet",text:"设置凭据",onclick:()=>openCredentials(id)}));
      return node("div",{class:"provider-row"},[node("div",{},[
        node("strong",{"data-i18n-raw":"true",text:id}),
        node("div",{class:"provider-meta",text:(ADAPTERS[p.adapter] || p.adapter)+(effectiveModel(p) ? " · "+effectiveModel(p) : "")}),
        node("div",{class:"provider-meta",text:plain(connection.detail) || "未进行真实连接验证"})
      ]),actions]);
    });
    container.replaceChildren(...(rows.length ? rows : [node("div",{class:"empty",text:"还没有登记智能体。添加接入配置后再选择工位。"})]));
  }
  function mcpEntries() { const data=store.settings.mcp || []; return Array.isArray(data) ? data : Object.entries(data).map(([id,entry])=>({id,...entry})); }
  function renderMcp() {
    const rows=mcpEntries().map(entry=>{
      const references=[...new Set([...JSON.stringify(entry.config || {}).matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map(match=>match[1]))];
      const actions=node("div",{class:"provider-actions"},[
        node("label",{class:"check-label"},[node("input",{type:"checkbox",checked:entry.enabled,onchange:async event=>{
          const result=await action("/api/mcp",{action:"update",id:entry.id,entry:{...entry,enabled:event.target.checked}},"MCP 配置已更新");
          if(result) await loadSettings(); else event.target.checked=!event.target.checked;
        }}),entry.enabled ? "已启用配置" : "已停用配置"]),
        ...references.map(name=>node("button",{class:"button small quiet",text:"设置 "+name,onclick:()=>openCredentials("mcp-variable:"+name)})),
        node("button",{class:"button small quiet",text:"配置",onclick:()=>openMcp(entry.id)}),
        node("button",{class:"button small danger",text:"移除",onclick:async()=>{
          if(window.confirm("移除 MCP 配置「"+entry.id+"」？现有素材文件不会删除。")) {
            const response=await action("/api/mcp",{action:"remove",id:entry.id},"MCP 配置已移除");
            if(response) await loadSettings();
          }
        }})
      ]);
      return node("div",{class:"provider-row"},[node("div",{},[
        node("strong",{"data-i18n-raw":"true",text:entry.name || entry.id}),
        node("div",{class:"provider-meta",text:(entry.roles || []).map(role=>ROLES[role]?.label || role).join(" / ") || "未分配工位"}),
        node("div",{class:"provider-meta",text:entry.detail || "配置由本地服务管理，连接状态需实际检查。"}),
        references.length ? node("div",{class:"provider-meta",text:"私有环境变量引用："+references.join("、")}) : null
      ]),actions]);
    });
    $("#mcp-list").replaceChildren(...(rows.length ? rows : [node("div",{class:"empty",text:"暂无 MCP 接入配置。可添加服务或导入 mcpServers JSON。"})]));
  }
  function renderLogs() {
    const selected=jobs().find(item=>jobId(item)===$("#log-job").value) || job(),filter=$("#log-filter").value.toLowerCase(),chosen=$("#log-stage").value,memberSelect=$("#log-member"),allMembers=Object.entries(planFor(selected).roles || {}).flatMap(([role,r])=>(r.members || []).map(m=>({role,...m}))),signature=JSON.stringify([jobId(selected),allMembers.map(m=>m.id)]);
    if(store.logMembersSignature!==signature){store.logMembersSignature=signature;const old=memberSelect.value;memberSelect.replaceChildren(node("option",{value:"all",text:"全部成员"}),...allMembers.map(m=>node("option",{value:m.id,text:m.id+" · "+roleLabel(m.role,selected)})));memberSelect.value=allMembers.some(m=>m.id===old)?old:"all";}
    const member=memberSelect.value,lines=[],seen=new Set();
    const push=(stage,memberId,raw)=>{if(chosen!=="all" && stage!==chosen && !String(stage).startsWith(chosen+"::"))return;if(member!=="all"&&memberId!==member)return;(Array.isArray(raw)?raw:String(raw || "").split("\n")).forEach(line=>{const value=plain(line),textValue="["+stage+(memberId?" / "+memberId:"")+"] "+value;if(value&&(!filter || textValue.toLowerCase().includes(filter))&&!seen.has(textValue)){seen.add(textValue);lines.push(textValue);}});};
    for(const [stage,entry] of Object.entries(selected?.stages || {}))push(stage,entry.member_id || memberIdFromStage(selected,stage),entry.log_tail || entry.logs || []);
    for(const m of allMembers){const real=memberRuntime(selected,m.role,m.id);push(ROLES[m.role].stage,m.id,real.log_tail || real.logs || []);}
    const output=$("#log-output");output.dataset.i18nRaw=lines.length?"true":"false";const content=lines.length?lines.slice(-500).join("\n"):selected?"当前筛选条件下暂无真实日志。":"请选择一条流水线。";if(output.textContent!==content){output.textContent=content;if($("#log-follow").checked)output.scrollTop=output.scrollHeight;}text("#log-caption",(selected?jobId(selected)+" · ":"")+"本地实际日志 · "+Math.min(500,lines.length)+" 行 · 不调用模型");
  }

  async function loadSettings() { const response=await api("/api/settings"); store.settings=response.settings || response; store.settingsLoaded=true; renderSettings(); renderHome(); }
  async function refreshState() { if(store.refreshing) return; store.refreshing=true; try { const response=await api("/api/state"); store.state=response.state || response;document.dispatchEvent(new CustomEvent("studio:state",{detail:store.state}));document.dispatchEvent(new CustomEvent("studio:public-policy",{detail:store.state.app?.commerce || null})); store.state.jobs=(store.state.jobs || []).map(item=>({...item,stages:Array.isArray(item.stages) ? Object.fromEntries(item.stages.map(stage=>[stage.name,stage])) : item.stages})); if(!store.state.running_job) store.state.running_job=store.state.jobs.find(item=>item.running)?.job_id || store.state.jobs.find(item=>item.running)?.id || null; if(!jobs().some(item=>jobId(item)===store.selected)) store.selected=runningId() || jobId(jobs()[0]) || null; text("#server-status","本地服务已连接"); $("#server-dot").className="status-dot good"; $("#live-dot").className="status-dot good"; text("#last-sync","更新于 "+new Date().toLocaleTimeString("zh-CN",{hour12:false,timeZone:"Asia/Shanghai"})); $("#service-warning").classList.add("hidden"); for(const item of jobs()) for(const [stage,entry] of Object.entries(item.stages || {})) { const id=jobId(item)+":"+stage, old=store.previous.get(id); if(old && old!==entry.status && ["DONE","FAILED","BLOCKED"].includes(entry.status) && policy().notify) toast(jobId(item)+" · "+(Object.values(ROLES).find(role=>role.stage===stage)?.label || stage)+"："+statusInfo(entry.status)[0],entry.status === "DONE" ? "success" : "error"); store.previous.set(id,entry.status); } renderHome(); renderLogs(); updatePolicyCapabilities(); if(store.view === "roles") renderProviders(); } catch(error) { text("#server-status","本地服务暂不可用"); $("#server-dot").className="status-dot bad"; $("#live-dot").className="status-dot bad"; text("#service-warning",error.message+" 已保留上次状态；当前状态可能已过期。"); $("#service-warning").classList.remove("hidden"); } finally { store.refreshing=false; } }
  function openProvider(id=null) { store.editingProvider=id; const form=$("#provider-form"), provider=id ? providers()[id] : {}; form.reset(); form.elements.id.value=id || ""; form.elements.id.disabled=Boolean(id); const adapter=$("#provider-adapter"); adapter.replaceChildren(...availableAdapters().map(value=>node("option",{value,text:ADAPTERS[value] || value}))); adapter.value=provider.adapter || "external-command"; form.elements.model.value=provider.model || ""; form.elements.model.placeholder=effectiveModel(provider) ? "留空继承："+effectiveModel(provider) : "使用已登记的模型名称"; form.elements.billing.value=provider.billing || "metered"; form.elements.command.value=provider.command?.length ? JSON.stringify(provider.command,null,2) : ""; form.elements.settings_ref.value=provider.settings_ref || ""; form.elements.endpoint.value=provider.settings?.endpoint || provider.endpoint || provider.base_url || ""; const selected=provider.ready_roles || (provider.adapter === "openrouter-research" ? ["research"] : ["gameplay","integration"]); roleChecks($("#provider-role-checks"),"roles",selected); text("#provider-dialog-title",id ? "修改智能体 · "+id : "添加智能体"); updateProviderFields(); $("#provider-dialog").showModal(); }
  function updateProviderFields() { const form=$("#provider-form"), adapter=form.elements.adapter.value; if(["codex","kimi"].includes(adapter)) form.elements.billing.value="subscription"; else if(["claude","openrouter-research","openai-compatible-research"].includes(adapter)) form.elements.billing.value="metered"; else if(adapter === "manual-desktop") form.elements.billing.value="manual"; form.elements.billing.disabled=adapter !== "external-command"; $("#provider-command-field").classList.toggle("hidden",!["external-command","kimi"].includes(adapter)); if(adapter === "kimi")form.elements.model.placeholder="留空使用隔离会员配置中的模型，仍需核对认证"; const endpointEnabled=["claude","openai-compatible-research"].includes(adapter); $("#provider-endpoint-field").classList.toggle("hidden",!endpointEnabled); form.elements.endpoint.disabled=!endpointEnabled; form.elements.endpoint.placeholder=adapter === "claude" && store.settings.config?.claude_base_url ? "留空继承："+store.settings.config.claude_base_url : adapter === "claude" ? "https://兼容 Anthropic 的服务地址" : "https://服务地址/v1"; let endpointNote=$("#provider-endpoint-note"); if(!endpointNote){endpointNote=node("small",{id:"provider-endpoint-note"});$("#provider-endpoint-field").append(endpointNote);} endpointNote.textContent=adapter === "claude" ? "需兼容 Anthropic Messages API。留空使用现有默认地址；此处不填写 API 密钥。" : "填写支持的 API 兼容服务地址；密钥请通过设置凭据单独保存。"; if(adapter.includes("research")) $$("input[name=roles]",form).forEach(input=>input.checked=input.value === "research"); }
  async function removeProvider(id) { if(!window.confirm("移除智能体「"+id+"」？如果仍被工位引用，服务会拒绝移除。")) return; const result=await action("/api/providers",{action:"remove",id},"智能体已移除"); if(result) await loadSettings(); }
  function openMcp(id=null) { store.editingMcp=id; const entry=mcpEntries().find(item=>item.id===id) || {}; const form=$("#mcp-form"); form.reset(); form.elements.id.value=id || ""; form.elements.id.disabled=Boolean(id); form.elements.name.value=entry.name || ""; form.elements.enabled.checked=entry.enabled !== false; form.elements.config.value=entry.config ? JSON.stringify(entry.config,null,2) : ""; roleChecks($("#mcp-role-checks"),"roles",entry.roles || []); text("#mcp-dialog-title",id ? "配置 MCP · "+id : "添加 MCP 插件"); $("#mcp-dialog").showModal(); }
  function openCredentials(id) {
    store.credentialProvider=id;
    $("#credential-form").reset();
    const variable=id.startsWith("mcp-variable:");
    text("#credential-label",variable ? "私有环境变量值" : "API Key / Token");
    text("#credential-provider",(variable ? "MCP 变量："+id.slice(13) : "智能体："+id)+"。保存凭据不会启动模型。");
    $("#credential-dialog").showModal();
  }
  async function checkConnection(id) { const result=await action("/api/connections/check",{provider_id:id,check:"local",approve_paid:false},"本地接入检查已完成"); if(result?.requires_paid_confirmation) openApproval("connection",id); }
  function openApproval(actionName,providerId=null,stageOverride=null,memberOverride=null) {
    const item=job();if(actionName!=="connection"&&!item){toast("请先选择一条流水线。","error");return;}
    const plan=planFor(item),instances=actionName === "connection"?[{id:providerId,provider_id:providerId,role:"connection",model:effectiveModel(providers()[providerId] || {}),budget_usd:policy().connection_budget_usd ?? .2,provider:providers()[providerId] || {}}]:Object.entries(plan.roles || {}).flatMap(([role,r])=>(r.members || []).map(member=>({role,...member,provider:pipelineProvider(item,member.provider_id),role_budget_usd:r.budget_usd})));
    const chosen=[...new Map(instances.map(m=>[m.provider_id,{id:m.provider_id,...m.provider}])).values()];const retryTarget=stageOverride?{stage:stageOverride,member_id:memberOverride}:retryChoice($("#retry-stage").value,item);store.approval={action:actionName,job_id:jobId(item),provider_id:providerId,stage:actionName === "retry"?retryTarget.stage:null,member_id:actionName === "retry"?retryTarget.member_id:null,providers:chosen,instances};
    text("#approval-title",actionName === "connection"?"确认一次连接实测":actionName === "retry"?"确认重试失败项":actionName === "resume"?"确认恢复此流水线":"确认开始此流水线");text("#approval-description",actionName === "connection"?"此操作会对「"+providerId+"」发送一次模型请求，单次连接预算最高 0.20 美元。":"流水线「"+jobId(item)+"」将使用以下已保存成员、模型和预算。批准只针对这条流水线，其他流水线费用分别计算；已有费用不清零。");
    $("#approval-job-budget").value=plan.total_budget_usd ?? budgetFor(item).total_cap_usd ?? 3;$("#approval-job-budget").readOnly=actionName!=="connection";$("#approval-connection-budget").value=Math.min(.2,Number(policy().connection_budget_usd ?? .2));$("#approval-paid").checked=false;


    const approvalRows=chosen.map(provider=>{
      const paid=provider.billing === "metered",members=instances.filter(m=>m.provider_id===provider.id);
      const header=node("div",{class:"approval-provider"},[
        ...(actionName === "connection"?[node("input",{type:"checkbox",name:"approve_provider",value:provider.id,checked:!paid,disabled:!paid})]:[]),
        node("span",{},[document.createTextNode(provider.id),node("small",{text:paid?"API 按用量 · 逐位选择本次批准的成员":provider.billing === "manual"?"人工平台费用不自动计入账本":"订阅额度 · 不折算美元"})])
      ]);
      const detail=members.map(m=>node("label",{class:"approval-member"},[
        ...(actionName !== "connection"?[node("input",{type:"checkbox",name:"approve_member",value:m.id,checked:!paid,disabled:!paid})]:[]),
        node("span",{},[node("strong",{text:(ROLES[m.role]?roleLabel(m.role,item):"连接测试")+" / "+m.id}),node("span",{text:m.model || effectiveModel(m.provider) || "使用已保存的默认模型"}),node("small",{text:approvalBudgetText(m)})])
      ]));
      return node("div",{class:"approval-provider-group"},[header,node("div",{class:"approval-members"},detail)]);
    });
    $("#approval-provider-list").replaceChildren(...approvalRows);

    text("#approval-confirm",actionName === "connection"?"确认一次实测":actionName === "retry"?"确认重试":"确认并"+(actionName === "resume"?"恢复":"开始"));$("#approval-dialog").showModal();
  }

  function isRunning(item) { if(!item)return false;return item.running === true || item.active === true || ["RUNNING","PAUSING","RESUMING"].includes(item.status) || Object.values(item.stages || {}).some(s=>s.status === "RUNNING") || (Array.isArray(store.state.running_jobs)?store.state.running_jobs.some(id=>(typeof id === "object"?jobId(id):id)===jobId(item)):false); }
  function kindName(kind) { return {"fan-wave-demo":"扇气验证模板","custom-game":"自定义 Godot 游戏",software:"Python / HTML / Node 软件"}[kind] || kind; }
  function planFor(item=job()) { if(item?.plan)return item.plan;if(!item)return {};const total=item?.budget?.total_cap_usd ?? item?.budget?.production_job_threshold ?? 3;return {pipeline_id:jobId(item),kind:item.kind || item.task?.kind || "fan-wave-demo",goal:item.goal || item.task?.goal || "",total_budget_usd:total,roles:Object.fromEntries(Object.entries(ROLES).map(([role,meta])=>{const r=item.role_routes || routes(),id=(Array.isArray(r[role])?r[role][0]:r[role]) || stageData(item,meta.stage).provider_id || "";return [role,{budget_usd:total,members:id?[{id:role+"-1",provider_id:id,model:null,assignment:defaultAssignment(role),budget_usd:total,required:true}]:[]}];}))}; }
  function pipelineProvider(item,id) { return item?.plan?.providers?.[id] || item?.plan?.frozen_providers?.[id] || item?.frozen_providers?.[id] || providers()[id] || {}; }
  function roleRuntime(item,role) { const team=item?.team || {};return team.roles?.[role] || team.stages?.[ROLES[role].stage] || team[role] || {}; }
  function memberRuntime(item,role,id) { const team=item?.team || {},r=roleRuntime(item,role),data=r.members || team.members || [];if(Array.isArray(data)){const found=data.find(m=>(m.id || m.member_id)===id);if(found)return found;}else if(data[id])return data[id];const entry=Object.values(item?.stages || {}).find(s=>s.member_id===id);return entry || {}; }
  function memberStageKey(item,role,id) { const direct=memberRuntime(item,role,id).stage;if(direct)return direct;const match=Object.entries(item?.stages || {}).find(([key,s])=>s.member_id===id || key===ROLES[role].stage+"::"+id);return match?.[0] || (planFor(item).roles?.[role]?.members?.length===1?ROLES[role].stage:null); }
  function memberIdFromStage(item,stage) { const entry=stageData(item,stage);if(entry.member_id)return entry.member_id;return String(stage).includes("::")?String(stage).split("::").at(-1):null; }
  function planEditable(item) { return Boolean(item && editMode(item)!=="locked"); }
  function openClone(item=job()) { if(!item)return;store.cloneSource=jobId(item);$("#clone-form").reset();text("#clone-description","将「"+store.cloneSource+"」的目标与团队配置保存为新的独立流水线。原进度和账本保留。");$("#clone-dialog").showModal(); }
  function defaultAssignment(role) { const value = {gameplay:"实现本流水线目标的玩法、剧情或软件功能，交付源码与实际检查结果。",vfx:"制作本流水线目标所需的动画、特效或软件资源，交付源文件与资源清单。",integration:"整合各成员实际产物，解决交接冲突，完成目标对应的实际验收。",research:"仅在具体资料请求出现时研究来源、相关性与许可，并保存可追溯的参考结果。"}[role]; return window.StudioI18n ? window.StudioI18n.t(value) : value; }
  function defaultPipelineDraft() { const total=Number(policy().job_budget_usd || 3);return {pipeline_id:"",kind:"fan-wave-demo",goal:"",total_budget_usd:total,roles:Object.fromEntries(Object.entries(ROLES).map(([role])=>{const available=Object.entries(providers()).filter(([,p])=>providerRoleReady(p,role)),route=Array.isArray(routes()[role])?routes()[role][0]:routes()[role],id=available.some(([id])=>id===route)?route:available[0]?.[0] || "";return [role,{budget_usd:total,members:[{id:role+"-1",provider_id:id,model:null,assignment:defaultAssignment(role),budget_usd:total,required:true}]}];}))}; }
  function openPipelineEditor(item=null,navigate=true,readOnly=false) {
    const mode=item?editMode(item):"full";if(item && mode === "locked" && !readOnly){toast("请先暂停并等待执行收尾，再修改目标、分工与预算；需要改变接入组合时可克隆。","error");return;}
    store.editingPipeline=item?jobId(item):null;store.pipelineEditMode=readOnly?"locked":mode;store.pipelineReadOnly=readOnly;store.pipelineDraft=JSON.parse(JSON.stringify(item?planFor(item):defaultPipelineDraft()));
    const form=$("#project-form"),draft=store.pipelineDraft,paused=mode === "paused",floor=budgetFloor(budgetFor(item));
    form.elements.job_id.value=draft.pipeline_id || jobId(item);form.elements.job_id.disabled=Boolean(item)||readOnly;form.elements.kind.value=draft.kind;form.elements.goal.value=draft.goal || "";form.elements.budget_usd.value=draft.total_budget_usd;form.elements.budget_usd.min=String(floor);form.elements.kind.disabled=readOnly||paused;form.elements.goal.readOnly=readOnly;form.elements.budget_usd.readOnly=readOnly;
    $("#save-pipeline").classList.toggle("hidden",readOnly);text("#pipeline-editor-title",readOnly?"查看本线保存配置 · "+jobId(item):paused?"暂停编辑预算与分工 · "+jobId(item):item?"编辑流水线 · "+jobId(item):"新建独立流水线");text("#save-pipeline",item?"保存本线修改":"创建流水线");
    text("#pipeline-editor-note",readOnly?"执行中或已完成的配置保持锁定。":paused?"只允许修改目标、分工和预算；保存后不会自动恢复或获得收费调用批准。"+(item?.revalidation_required?" 任务已修改，恢复后重新制作受影响部分并验收。":""):"保存配置不会启动模型，收费成员需要明确批准。");
    const stats=budgetFor(item);text("#budget-consumption-hint",paused?"本线 API 已记录费用 "+money(stats.spent_usd)+"，执行中预留 "+money(stats.reserved_usd)+"。总预算最低为 "+money(floor)+"；各角色与工位也不能低于既有消耗，未知账目须先核对。":"");$("#budget-consumption-hint").classList.toggle("hidden",!paused);renderPipelineEditor();if(navigate)showView("project");
  }
  function nextMemberId(role) { const ids=new Set(Object.values(store.pipelineDraft.roles).flatMap(r=>r.members || []).map(m=>m.id));let index=1;while(ids.has(role+"-"+index))index++;return role+"-"+index; }
  function renderPipelineEditor() {
    const draft=store.pipelineDraft;if(!draft)return;const mode=store.pipelineEditMode || "full",readOnly=mode === "locked",identityLocked=mode!=="full",item=store.editingPipeline?jobs().find(j=>jobId(j)===store.editingPipeline):null;
    $("#pipeline-role-editors").replaceChildren(...Object.entries(ROLES).map(([role,meta])=>{
      const config=draft.roles[role],hasApi=config.members.some(m=>editorBilling(m)==="metered"),roleStats=budgetFor(item).roles?.[role] || {},cap=node("input",{type:"number",min:budgetFloor(roleStats),max:draft.total_budget_usd,step:"any",value:config.budget_usd,required:"required",disabled:readOnly,"aria-label":meta.label+"角色预算",oninput:e=>{config.budget_usd=Number(e.target.value);updateEditorWarning();}});
      const roleBudget=hasApi?node("label",{class:"role-cap-field"},[node("span",{text:"API角色共享预算 · USD"}),cap,node("small",{text:"已消耗 "+money(roleStats.spent_usd)+" · 最低 "+money(budgetFloor(roleStats))})]):node("div",{class:"quota-role-note",text:config.members.every(m=>editorBilling(m)==="subscription")?"本团队按会员额度，不设置美元团队预算":config.members.every(m=>editorBilling(m)==="manual")?"本团队外部用量独立核对，不设置 API 团队预算":"本团队的会员或外部用量独立核对，不设置 API 团队预算"});
      const list=node("div",{class:"member-editor-list"},config.members.map((member,index)=>{
        const billing=editorBilling(member),memberStats=budgetFor(item).members?.[member.id] || {},providerSelect=node("select",{required:"required",disabled:identityLocked,"aria-label":meta.label+"成员"+(index+1)+"提供方"});
        const available=item?{...providers(),...(item.plan?.providers || {})}:providers(),choices=Object.entries(available).filter(([,p])=>providerRoleReady(p,role));if(member.provider_id&&!choices.some(([id])=>id===member.provider_id))choices.push([member.provider_id,pipelineProvider(item,member.provider_id)]);providerSelect.append(...choices.map(([id,p])=>node("option",{value:id,text:id+" · "+(ADAPTERS[p.adapter] || p.adapter)})));providerSelect.value=member.provider_id;
        providerSelect.addEventListener("change",e=>{member.provider_id=e.target.value;if(editorBilling(member)==="metered"){if(config.budget_usd<=0)config.budget_usd=Number(draft.total_budget_usd)||3;if(member.budget_usd<=0)member.budget_usd=config.budget_usd;}renderPipelineEditor();});
        const model=node("input",{value:member.model || "",placeholder:"留空使用已保存默认："+(effectiveModel(editorProvider(member)) || "接入默认"),disabled:identityLocked,"aria-label":meta.label+"成员"+(index+1)+"模型",oninput:e=>{member.model=e.target.value.trim() || null;}}),idInput=node("input",{value:member.id,required:"required",pattern:"[A-Za-z0-9][A-Za-z0-9_\\-]{0,63}",maxlength:"64",disabled:identityLocked,"aria-label":meta.label+"成员"+(index+1)+"标识",oninput:e=>{member.id=e.target.value.trim();updateEditorWarning();}});
        const field=(label,input,small)=>node("label",{class:"field"},[node("span",{text:label}),input,small?node("small",{text:small}):null]);
        const quota=billing==="metered"?field("API工位累计预算 · USD",node("input",{type:"number",min:budgetFloor(memberStats),step:"any",value:member.budget_usd,required:"required",disabled:readOnly,"aria-label":meta.label+"成员"+(index+1)+"预算",oninput:e=>{member.budget_usd=Number(e.target.value);updateEditorWarning();}}),"已消耗 "+money(memberStats.spent_usd)+"，最低 "+money(budgetFloor(memberStats))+"；不得超过API角色预算。"):node("div",{class:"field quota-member-note"},[node("span",{text:"工位用量标准"}),node("strong",{text:billing==="subscription"?"工位预算不可用，以你的会员额度作为标准":"外部用量独立核对，不按会员额度计算"})]);
        return node("div",{class:"member-editor"},[node("div",{class:"member-editor-heading"},[node("strong",{text:"成员 "+(index+1)}),node("button",{type:"button",class:"button small danger",text:"移除",disabled:identityLocked||config.members.length<=1,onclick:()=>{config.members.splice(index,1);renderPipelineEditor();}})]),node("div",{class:"member-editor-fields"},[field("成员标识",idInput,"成员标识在本线内唯一。"),field("执行提供方",providerSelect),field("模型",model,"暂停编辑不改变模型和账号。"),quota,node("label",{class:"field assignment-field"},[node("span",{text:"分工 / 交付要求"}),node("textarea",{rows:"2",required:"required",disabled:readOnly,"aria-label":meta.label+"成员"+(index+1)+"分工",oninput:e=>{member.assignment=e.target.value;}},[member.assignment || ""])]),node("label",{class:"check-label required-field"},[node("input",{type:"checkbox",checked:member.required!==false,disabled:identityLocked,onchange:e=>{member.required=e.target.checked;}}),"必需成员 · 其产物参与角色交接"])])]);
      }));
      const add=node("button",{type:"button",class:"button small",text:"＋ 添加成员",disabled:identityLocked,onclick:()=>{const first=config.members[0];config.members.push({id:nextMemberId(role),provider_id:first?.provider_id || "",model:null,assignment:defaultAssignment(role),budget_usd:config.budget_usd,required:true});renderPipelineEditor();}});
      return node("section",{class:"editor-role"},[node("div",{class:"editor-role-head"},[node("div",{class:"role-title"},[node("span",{class:"role-symbol "+role,text:meta.symbol}),node("h2",{text:meta.label}),node("span",{class:"muted",text:config.members.length+" 位成员"})]),roleBudget,add]),list]);
    }));updateEditorWarning();
  }
  function updateEditorWarning() {
    const draft=store.pipelineDraft;if(!draft)return;const issues=[],ids=new Set(),item=store.editingPipeline?jobs().find(j=>jobId(j)===store.editingPipeline):null,stats=budgetFor(item),total=Number(draft.total_budget_usd);let sum=0;
    if(!Number.isFinite(total)||total<budgetFloor(stats))issues.push("总预算不能低于已消耗与保留中的额度 "+money(budgetFloor(stats)));
    for(const [role,r] of Object.entries(draft.roles)){const metered=r.members.filter(m=>editorBilling(m)==="metered");if(metered.length&&(!Number.isFinite(r.budget_usd)||r.budget_usd<budgetFloor(stats.roles?.[role])||r.budget_usd>total))issues.push(roleLabel(role)+"API团队预算应在已消耗最低额与总预算之间");for(const m of r.members){if(ids.has(m.id))issues.push("成员标识重复："+m.id);ids.add(m.id);if(!m.provider_id)issues.push(roleLabel(role)+"需要选择已登记提供方");if(!String(m.assignment || "").trim())issues.push(m.id+"需要填写分工与交付要求");if(editorBilling(m)==="metered"){sum+=Number(m.budget_usd)||0;if(!Number.isFinite(m.budget_usd)||m.budget_usd<budgetFloor(stats.members?.[m.id])||m.budget_usd>r.budget_usd)issues.push(m.id+"API工位预算应在已消耗最低额与角色预算之间");}}}
    text("#pipeline-editor-warning",[...new Set(issues)].join("；"));$("#pipeline-editor-warning").classList.toggle("hidden",!issues.length);
    text("#budget-suggestion-detail","API工位预算之和 "+money(sum)+"，不含会员额度与人工外部用量。"+(total<=sum?" 当前总预算等于或小于合计，仅提醒，不禁止保存；执行仍受共享上限约束。":""));$("#budget-suggestion-detail").classList.toggle("recommendation-warning",total<=sum);return issues;
  }
  function retryChoices(item) { const failed=["FAILED","BLOCKED","INTERRUPTED"],members=Object.values(item?.team?.members || {}).filter(m=>failed.includes(m.status)),rows=members.map(m=>["member::"+m.id,{member_id:m.id,stage:m.stage}]);for(const [id,s] of Object.entries(item?.stages || {}))if(failed.includes(s.status)&&!members.some(m=>m.stage===id))rows.push([id,s]);return rows; }
  function retryChoice(value,item) { if(String(value).startsWith("member::")){const id=String(value).slice(8),real=item?.team?.members?.[id];return {stage:real?.stage,member_id:id};}return {stage:value || null,member_id:null}; }



  function editMode(item) { if(!item)return "full";if(isRunning(item))return "locked";if(["full","paused","cancelled","ready","locked"].includes(item.edit_mode))return item.edit_mode;if(jobStatus(item)==="DONE")return "locked";if(item.editable===true)return "full";return "locked"; }
  function editorProvider(member) { const item=store.editingPipeline?jobs().find(j=>jobId(j)===store.editingPipeline):null;return item?.plan?.providers?.[member.provider_id] || providers()[member.provider_id] || {}; }
  function editorBilling(member) { return editorProvider(member).billing || "manual"; }
  function budgetFloor(stats={}) { const floor=Number(stats?.minimum_cap_usd ?? stats?.min_cap_usd);if(Number.isFinite(floor))return Math.max(0,floor);const spent=Number(stats?.spent_usd ?? stats?.spent ?? 0),reserved=Number(stats?.reserved_usd ?? stats?.reserved ?? 0);return Math.max(0,(Number.isFinite(spent)?spent:0)+(Number.isFinite(reserved)?reserved:0)); }
  function memberBudgetDisplay(provider,member,cost) { if(provider.billing!=="metered")return node("div",{class:"member-quota"},[node("span",{text:provider.billing==="subscription"?"工位预算不可用，以你的会员额度作为标准":"外部用量独立核对，不按会员额度计算"})]);return node("div",{class:"member-budget"},[node("span",{text:"API上限 "+money(member.budget_usd)}),node("span",{text:"已消耗 "+money(cost.spent_usd ?? cost.spent ?? cost.actual_cost_usd)}),node("span",{text:"预留 "+money(cost.reserved_usd ?? cost.reserved)})]); }
  function roleBudgetDisplay(item,role,config,budget) { const members=config.members || [],metered=members.some(m=>pipelineProvider(item,m.provider_id).billing==="metered");return metered?node("div",{class:"role-budget"},[node("span",{text:"API角色共享上限 "+money(config.budget_usd)}),node("span",{text:"已消耗 "+money(budget.spent_usd ?? budget.spent)}),node("span",{text:"预留 "+money(budget.reserved_usd ?? budget.reserved)})]):node("div",{class:"role-quota",text:members.every(m=>pipelineProvider(item,m.provider_id).billing==="subscription")?"本团队按会员额度，不设置美元团队预算":members.every(m=>pipelineProvider(item,m.provider_id).billing==="manual")?"本团队外部用量独立核对，不设置API团队预算":"本团队会员或外部用量独立核对，不设置API团队预算"}); }
  function approvalBudgetText(member) { if(member.role === "connection")return "单次连接实测上限 "+money(member.budget_usd);if(member.provider.billing==="subscription")return "工位预算不可用，以你的会员额度作为标准";if(member.provider.billing!=="metered")return "外部用量独立核对，不按会员额度计算";return "API工位上限 "+money(member.budget_usd)+" · 角色共享上限 "+money(member.role_budget_usd); }
  function requestRows(item=job()) { const raw=item?.budget_requests || store.state.budget_requests?.[jobId(item)] || [];return Array.isArray(raw)?raw:raw.requests || Object.values(raw); }
  function renderBudgetRequests() {
    const item=job(),rows=requestRows(item),panel=$("#budget-requests-panel"),list=$("#budget-request-list"),signature=JSON.stringify([jobId(item),rows,budgetFor(item),planFor(item).revision,isRunning(item)]);panel.classList.toggle("hidden",!rows.length);if(store.budgetRequestSignature===signature)return;store.budgetRequestSignature=signature;
    list.replaceChildren(...rows.map(request=>{
      const id=request.id || request.request_id,status=String(request.status || "PENDING").toUpperCase(),pending=["PENDING","OPEN","REQUESTED","WAITING_BUDGET"].includes(status),memberId=request.member_id || request.member,current=request.current_member_cap_usd ?? request.current_cap_usd ?? request.current_budget_usd ?? request.member_budget_usd,spent=request.spent_member_usd ?? request.spent_usd ?? request.member_spent_usd,requested=request.requested_increase_usd ?? request.suggested_increase_usd ?? request.request_increase_usd ?? request.increase_usd,totalRemaining=request.total_remaining_usd ?? request.line_remaining_usd ?? budgetFor(item).available_usd;
      const card=node("article",{class:"budget-request-card","data-request-id":id}),heading=node("div",{class:"panel-heading"},[node("h3",{class:"request-question",text:"是否同意从总预算里增加工位预算"}),badge(pending?"WAITING_BUDGET":status === "APPROVED"?"APPROVED":status === "REJECTED"?"REJECTED":status)]),identity=node("p",{class:"muted",text:(memberId || "待确认成员")+" · "+(request.provider_id || "本线API成员")+" · 调整预算不会自动开始模型调用"}),stats=node("div",{class:"request-budget-stats"},[node("span",{text:(pending?"当前工位上限 ":"申请时工位上限 ")+money(current)}),node("span",{text:"已消耗 "+money(spent)}),node("span",{text:"申请增加 "+money(requested)}),node("span",{text:"总预算可用 "+money(totalRemaining)})]);
      card.append(heading,identity,...(request.reason?[node("p",{class:"muted request-reason",text:request.reason})]:[]),stats);if(!pending){card.append(node("p",{class:"muted",text:status === "APPROVED"?"本次实际批准增加 "+money(request.approved_increase_usd)+"；恢复仍需明确收费调用批准。":status === "SUPERSEDED"?"此申请已被新计划替代，请核对最新申请。":"该增额申请已拒绝，已有费用与成果保留。"}));return card;}
      const amount=node("input",{type:"number",min:"0.000001",step:"any",value:requested ?? "","aria-label":memberId+"批准增加金额"}),warning=node("div",{class:"notice hidden request-warning"}),approve=node("button",{type:"button",class:"button primary small",text:"同意增加预算"}),deny=node("button",{type:"button",class:"button quiet small",text:"拒绝增额"});
      const validate=()=>{const increase=Number(amount.value),remaining=Number(totalRemaining),short=Number.isFinite(remaining)&&increase>remaining,owner=isRunning(item)||item?.can_decide_budget===false;textValue(warning,owner?"当前还不能处理预算申请，请等待执行收尾或按实际状态暂停。":short?"总额度不足，请先暂停并修改本线总预算。":"");warning.classList.toggle("hidden",!owner&&!short);approve.disabled=owner||short||!amount.value||!Number.isFinite(increase)||increase<=0;deny.disabled=owner;};
      const decide=approved=>withBusy(approved?approve:deny,()=>action("/api/v25/budget/decide",{job_id:jobId(item),request_id:id,approved,increase_usd:approved?Number(amount.value):0,expected_revision:request.expected_revision ?? request.plan_revision ?? request.revision ?? planFor(item).revision ?? 0},approved?"已保存预算增额；没有开始模型调用，恢复仍需明确批准":"已拒绝该增额申请；本线现有成果与账目保留"));
      amount.addEventListener("input",validate);approve.addEventListener("click",()=>decide(true));deny.addEventListener("click",()=>decide(false));card.append(node("div",{class:"request-decision"},[node("label",{class:"field"},[node("span",{text:"本次同意增加 · USD"}),amount]),approve,deny]),warning);validate();return card;
    }));
  }
  function textValue(element,value) { element.textContent=String(value ?? ""); }

  document.addEventListener("click",event=>{const view=event.target.closest("[data-view]");if(view) showView(view.dataset.view); const close=event.target.closest("[data-close]");if(close) close.closest("dialog").close();const open=event.target.closest("[data-open]");if(open && job()) withBusy(open,()=>action("/api/job/open",{job_id:jobId(job()),target:open.dataset.open}));});
  $$("dialog").forEach(dialog=>dialog.addEventListener("close",()=>{if(dialog.id === "credential-dialog") $("#credential-form").elements.secret.value="";}));
  $("#refresh-state").addEventListener("click",event=>withBusy(event.currentTarget,async()=>{await refreshState();try{await loadSettings();}catch(error){toast(error.message,"error");}}));
  $("#start-job").addEventListener("click",()=>openApproval("start")); $("#resume-job").addEventListener("click",()=>openApproval("resume"));
  $("#pause-job").addEventListener("click",event=>withBusy(event.currentTarget,()=>action("/api/job/pause",{job_id:jobId(job()),save:true},"已发送暂停并保存请求；请以实际工位状态为准")));
  $("#retry-job").addEventListener("click",()=>openApproval("retry"));
  $("#roles-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget, values={};Object.keys(ROLES).forEach(role=>values[role]=role === "research" ? [form.elements[role].value].filter(Boolean) : form.elements[role].value);await withBusy($("button[type=submit]",form),async()=>{const result=await action("/api/settings",{role_routes:values},"新流水线的默认成员选择已保存");if(result) await loadSettings();});});
  $("#policy-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget, next={};for(const name of ["token_saving","auto_retry","autosave","notify"])next[name]=form.elements[name].checked;next.max_retries=Math.max(0,Math.min(2,Number(form.elements.max_retries.value)));next.job_budget_usd=Number(form.elements.job_budget_usd.value);next.connection_budget_usd=Number(form.elements.connection_budget_usd.value);await withBusy($("button[type=submit]",form),async()=>{const response=await action("/api/settings",{policy:next},"运行策略已保存");if(response) await loadSettings();});});

  $("#project-form").addEventListener("submit",async event=>{event.preventDefault();if(store.pipelineReadOnly)return;const form=event.currentTarget,draft=store.pipelineDraft;if(!draft)return;draft.pipeline_id=store.editingPipeline || form.elements.job_id.value.trim();draft.kind=form.elements.kind.value;draft.goal=form.elements.goal.value.trim();draft.total_budget_usd=Number(form.elements.budget_usd.value);if(updateEditorWarning().length){toast("请先修正团队预算与成员配置。","error");return;}await withBusy($("#save-pipeline"),async()=>{const payload={pipeline_id:draft.pipeline_id,kind:draft.kind,goal:draft.goal,total_budget_usd:draft.total_budget_usd,roles:draft.roles,...(store.editingPipeline?{revision:draft.revision ?? 0}:{})},response=await action("/api/v2/pipelines/"+(store.editingPipeline?"update":"create"),payload,store.editingPipeline?"流水线配置已保存；未启动模型":"独立流水线已创建；未启动模型");if(response){store.selected=response.job?.pipeline_id || response.job?.job_id || response.job?.id || response.pipeline_id || response.job_id || draft.pipeline_id;store.pipelineDraft=null;store.editingPipeline=null;await refreshState();showView("home");}});});
  $("#project-form").elements.budget_usd.addEventListener("input",event=>{if(store.pipelineDraft){store.pipelineDraft.total_budget_usd=Number(event.target.value);$$(".role-cap-field input").forEach(input=>input.max=event.target.value);updateEditorWarning();}});
  $("#create-pipeline").addEventListener("click",()=>openPipelineEditor());
  $("#edit-pipeline").addEventListener("click",()=>openPipelineEditor(job()));
  $("#budget-edit-pipeline").addEventListener("click",()=>openPipelineEditor(job(),true,!planEditable(job())));
  $("#clone-pipeline").addEventListener("click",()=>openClone());
  $("#cancel-pipeline-edit").addEventListener("click",()=>{store.pipelineDraft=null;store.editingPipeline=null;showView("home");});
  $("#clone-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;await withBusy($("button[type=submit]",form),async()=>{const id=form.elements.pipeline_id.value.trim(),response=await action("/api/v2/pipelines/clone",{source_id:store.cloneSource,pipeline_id:id},"克隆流水线已创建；未复制费用或启动模型");if(response){$("#clone-dialog").close();store.selected=response.job?.pipeline_id || response.job?.job_id || response.job?.id || response.pipeline_id || response.job_id || id;await refreshState();}});});
  $("#add-provider").addEventListener("click",()=>openProvider()); $("#provider-adapter").addEventListener("change",updateProviderFields);
  $("#provider-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;await withBusy($("button[type=submit]",form),async()=>{try{const id=store.editingProvider || form.elements.id.value.trim(), adapter=form.elements.adapter.value, selected=$$("input[name=roles]:checked",form).map(input=>input.value);if(!selected.length)throw new Error("请选择至少一个逻辑工位。");const original=store.editingProvider ? providers()[id] : {};const spec={...original,adapter,billing:form.elements.billing.value,model:form.elements.model.value.trim() || null,settings_ref:form.elements.settings_ref.value.trim() || null,ready_roles:selected,capabilities:roleCapabilities(selected,adapter)};if(["external-command","kimi"].includes(adapter))spec.command=JSON.parse(form.elements.command.value || (adapter === "kimi" ? '["kimi"]' : "[]"));else if(adapter === "manual-desktop" || adapter.includes("research"))spec.command=[];else spec.command=original.command || [adapter];const payload={action:store.editingProvider ? "update" : "add",id,provider:spec};if(["claude","openai-compatible-research"].includes(adapter))spec.settings={...(original.settings || {}),endpoint:form.elements.endpoint.value.trim()};const result=await action("/api/providers",payload,"智能体配置已保存；未启动模型");if(result){$("#provider-dialog").close();await loadSettings();}}catch(error){toast(error.message,"error");}});});
  $("#add-mcp").addEventListener("click",()=>openMcp()); $("#import-mcp").addEventListener("click",()=>{$("#mcp-import-form").reset();$("#import-dialog").showModal();});
  $("#mcp-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;await withBusy($("button[type=submit]",form),async()=>{try{const id=store.editingMcp || form.elements.id.value.trim(),entry={id,name:form.elements.name.value.trim(),enabled:form.elements.enabled.checked,roles:$$("input[name=roles]:checked",form).map(input=>input.value),config:JSON.parse(form.elements.config.value)};const result=await action("/api/mcp",{action:store.editingMcp ? "update" : "add",id,entry},"MCP 配置已保存");if(result){$("#mcp-dialog").close();await loadSettings();}}catch(error){toast("MCP 配置错误："+error.message,"error");}});});
  $("#mcp-import-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;await withBusy($("button[type=submit]",form),async()=>{try{const config=JSON.parse(form.elements.config.value);const result=await action("/api/mcp",{action:"import",config},"MCP JSON 已导入");if(result){$("#import-dialog").close();await loadSettings();}}catch(error){toast("JSON 无效："+error.message,"error");}});});
  $("#credential-form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget;await withBusy($("button[type=submit]",form),async()=>{try{await api("/api/credentials",{method:"POST",body:{provider_id:store.credentialProvider,secret:form.elements.secret.value,remember:form.elements.remember.checked}});form.elements.secret.value="";$("#credential-dialog").close();toast("凭据已交给本地私有接入服务；未启动模型","success");await refreshState();}catch{form.elements.secret.value="";toast("凭据保存失败，请检查本地接入服务后重新输入。","error");}});});

  $("#approval-form").addEventListener("submit",async event=>{
    event.preventDefault();const approval=store.approval;if(!approval)return;const approved=$("#approval-paid").checked;
    const selectedMembers=$$("input[name=approve_member]:checked",event.currentTarget).map(input=>input.value),paidMembers=approval.instances.filter(m=>m.provider.billing === "metered"&&selectedMembers.includes(m.id));
    const selectedProviders=$$("input[name=approve_provider]:checked",event.currentTarget).map(input=>input.value),paid=approval.action === "connection"?approval.providers.filter(p=>p.billing === "metered"&&selectedProviders.includes(p.id)).map(p=>p.id):[...new Set(paidMembers.map(m=>m.provider_id))];
    if(approval.action === "connection"&&(!approved||!paid.length)){toast("连接模型实测需要选择该 API 接入，并明确批准测试预算。","error");return;}
    if(approved&&approval.providers.some(p=>p.billing === "metered")&&!paid.length){toast("请选择本次批准的收费成员，或取消 API 批准仅运行订阅工位。","error");return;}
    const paid_approval={approved,providers:approved?paid:[],members:approved?paidMembers.map(m=>m.id):[],job_budget_usd:Number($("#approval-job-budget").value),connection_budget_usd:Number($("#approval-connection-budget").value)};
    await withBusy($("#approval-confirm"),async()=>{const path=approval.action === "connection"?"/api/connections/check":"/api/job/"+approval.action,body=approval.action === "connection"?{provider_id:approval.provider_id,test_model:true,paid_approval}:{job_id:approval.job_id,paid_approval,...(approval.stage?{stage:approval.stage}:{}),...(approval.member_id?{member_id:approval.member_id}:{})};const result=await action(path,body,approval.action === "connection"?"已提交明确批准的连接实测":"仅对本流水线提交执行请求，请查看实际成员状态");if(result)$("#approval-dialog").close();});
  });

  ["#log-job","#log-stage","#log-member","#log-filter"].forEach(selector=>$(selector).addEventListener(selector === "#log-filter" ? "input" : "change",renderLogs));
  $("#copy-logs").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("#log-output").textContent);toast("当前显示日志已复制","success");}catch{toast("无法访问剪贴板，可选中日志手动复制。","error");}});
  $(".budget-panel").append(node("button",{id:"reconcile-cost",class:"button quiet wide hidden",text:"核对未知 / 中断费用",onclick:openReconciliation}));
  $("#reconcile-form").addEventListener("submit",async event=>{
    event.preventDefault(); const form=event.currentTarget;
    const raw=form.elements.reported_cost_usd.value.trim(), id=form.elements.invocation_id.value;
    if(!raw || !Number.isFinite(Number(raw)) || Number(raw)<0 || !form.elements.confirmed_from_usage.checked){toast("请输入已从供应商用量记录确认的非负费用，并勾选确认。","error");return;}
    const selected=jobs().find(item=>jobId(item)===store.reconcileJob);
    if(!unresolvedCosts(selected).some(entry=>entry.id===id)){toast("此调用已不在待核对列表，请刷新实际状态。","error");return;}
    await withBusy($("button[type=submit]",form),async()=>{
      const response=await action("/api/job/reconcile",{job_id:store.reconcileJob,invocation_id:id,reported_cost_usd:raw,confirmed_from_usage:true},"实际费用已核对；原任务预算与已记录支出保留");
      if(response)$("#reconcile-dialog").close();
    });
  });

  // Nonsecret catalog and explicit configuration registration only.
  let assistantTemplates=[];
  function selectedAssistantTemplate(){return assistantTemplates.find(item=>item.id===$('#assistant-template-select').value);}
  function renderAssistantTemplate(){
    const template=selectedAssistantTemplate(), details=$('#assistant-template-details');details.replaceChildren();
    if(!template)return;
    const provider=template.provider || {}, verification=template.verification || {};
    details.append(node('strong',{text:template.label || template.id}),node('p',{text:provider.adapter==='manual-desktop'?'本轮使用桌面人工交接：生成任务包，由你在原助手内完成后回交；不承诺自动操作其界面。':provider.adapter==='kimi'?'Kimi 原生 CLI · 本轮仅会员 Managed OAuth；会员额度不计入美元预算，不接 API Key 计费 profile。':'原生助手/工具接入，以模板实际能力为准。'}));
    const requirements=Array.isArray(template.requirements)?template.requirements:[];
    if(requirements.length)details.append(node('ul',{},requirements.map(value=>node('li',{text:String(value)}))));
    details.append(node('p',{class:'muted',text:verification.status==='cli_help_verified'?'本机CLI与help已核验；未调用模型，模型制作与MCP端到端尚未实测。':'验证状态：'+plain(verification)}));
    if(template.registration_enabled===false)details.append(node('p',{text:'此模板仅提供接入说明，尚需配置/核对；本轮不能一键登记为可运行工位。'}));
    let id=template.suggested_provider_id || template.id || 'assistant-own',count=2,base=id;
    while(providers()[id])id=base+'-'+count++;
    $('#assistant-template-provider-id').value=id;
    $('#assistant-template-provider-id').disabled=template.registration_enabled===false;
    $('#assistant-template-add').disabled=template.registration_enabled===false;
  }
  async function openAssistantTemplates(){
    try{
      const response=await api('/api/assistant/templates');assistantTemplates=response.templates || [];
      if(!Array.isArray(assistantTemplates)||!assistantTemplates.length)throw new Error('本地尚未提供已核实的助手模板。可使用原添加智能体入口，或查看接入说明书。');
      $('#assistant-template-select').replaceChildren(...assistantTemplates.map(item=>node('option',{value:item.id,text:item.label || item.id})));
      renderAssistantTemplate();$('#assistant-template-dialog').showModal();$('#assistant-template-select').focus();
    }catch(error){toast(error.message,'error');}
  }
  $('#add-assistant-template')?.addEventListener('click',openAssistantTemplates);
  $('#assistant-template-select')?.addEventListener('change',renderAssistantTemplate);
  $('#assistant-template-form')?.addEventListener('submit',async event=>{
    event.preventDefault();const template=selectedAssistantTemplate();
    if(!template||template.registration_enabled===false){toast('此模板尚需设置，不能直接登记运行。','error');return;}
    const id=$('#assistant-template-provider-id').value.trim();
    if(providers()[id]){toast('此编号已存在，请换一个新编号，现有配置不会被覆盖。','error');return;}
    await withBusy($('#assistant-template-add'),async()=>{
      const spec=JSON.parse(JSON.stringify(template.provider || {}));
      if(template.settings && Object.keys(template.settings).length)spec.settings={...(spec.settings || {}),...template.settings};
      const result=await action('/api/providers',{action:'add',id,provider:spec},'已登记助手配置；未登录、未启动模型。请按模板要求核对接入。');
      if(result){$('#assistant-template-dialog').close();await loadSettings();}
    });
  });

  showView("home"); renderHome();
  Promise.allSettled([refreshState(),loadSettings()]).then(results=>{const failed=results.find(result=>result.status === "rejected");if(failed)toast("配置读取失败："+failed.reason.message,"error");});
  setInterval(()=>{if(!document.hidden)refreshState();},2000);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)refreshState();});
})();
