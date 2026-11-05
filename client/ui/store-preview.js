(() => {
  'use strict';
  const main=document.querySelector('main') || document.body;
  const panel=document.createElement('section');
  panel.className='panel';
  panel.style.margin='18px 0';
  panel.innerHTML=`<h2>首次使用准备 · 上架候选版</h2>
  <p>请在这台电脑上安装流水线要用的软件，并准备可用的 AI 会员额度或 API 额度。软件授权不包含第三方 AI 费用。你需要的工具可包括 Codex、Claude Code、Blender 和 Godot。</p>
  <p>此候选版尚未开放购买，账号授权、跨设备限额和团队管理正在准备。</p>
  <details><summary>设置本机软件与模型</summary><form id="store-setup" style="display:grid;gap:12px;margin-top:16px">
  <label>Godot 完整 exe 路径<input name="godot_path" placeholder="选择你已安装的 Godot 程序路径"></label>
  <label>Blender 完整 exe 路径<input name="blender_path" placeholder="选择你已安装的 Blender 程序路径"></label>
  <label>Codex 模型（可在运行前修改）<input name="codex_model"></label>
  <label>Claude Code 模型<input name="claude_model"></label>
  <label>Claude API / 网关 HTTPS 地址<input name="claude_base_url" type="url" placeholder="https://你的网关地址"></label>
  <label>网关认证方式<select name="claude_auth"><option value="bearer">Bearer</option><option value="api_key">API Key</option></select></label>
  <button class="button" type="submit">保存本机配置</button><span id="store-setup-status" role="status"></span>
  </form></details><p>API 密钥请在“角色与接入”中通过私密凭据入口填写；官方账号登录请在各软件的官方窗口完成。</p>`;
  main.prepend(panel);
  document.querySelector('#store-setup').addEventListener('submit',async event=>{
    event.preventDefault();
    const out=document.querySelector('#store-setup-status');
    try {
      const data=Object.fromEntries(new FormData(event.target));
      const response=await fetch('/api/store/setup',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:JSON.stringify(data)});
      const value=await response.json();
      if(!response.ok || !value.ok) throw Error(value.error || '保存失败');
      out.textContent='配置已保存，没有调用 AI。请再检查接入和 MCP 状态。';
    } catch(error) {out.textContent=error.message;}
  });

  let currentCapabilities={},exitBusy=false;
  const canCancel=()=>currentCapabilities.task_run_cancellation===true && currentCapabilities.cancellation_semantics==='keep_pipeline_shell';
  document.addEventListener('studio:state',event=>{currentCapabilities=event.detail?.app?.capabilities || {};const b=document.querySelector('#cancel-task-confirm');if(b)b.disabled=!canCancel();const n=document.querySelector('#cancel-task-capability');if(n)n.hidden=canCancel();const e=document.querySelector('#exit-backend-confirm');if(e)e.disabled=exitBusy || currentCapabilities.backend_pause_exit!==true;const x=document.querySelector('#exit-backend-capability');if(x)x.hidden=currentCapabilities.backend_pause_exit===true;});
  const pause=document.querySelector('#pause-job');
  if(pause) {
    const button=document.createElement('button');button.id='cancel-task';button.className='button quiet';button.textContent='取消本次任务';
    const dialog=document.createElement('dialog');dialog.id='cancel-task-dialog';
    dialog.setAttribute('aria-labelledby','cancel-task-title');dialog.setAttribute('aria-describedby','cancel-task-notice');
    dialog.innerHTML='<div class="dialog-heading"><h2 id="cancel-task-title">取消本次任务</h2></div><p id="cancel-task-notice">取消本次任务，已生成内容不可恢复；保留流水线及工位/连线/预算配置；可以先暂停修改；是否确定取消？</p><p id="cancel-task-id" class="muted" data-i18n-raw="true"></p><p class="muted">已经产生的费用、用量和尝试记录保留，不会退款或重置。</p><div class="form-actions end"><button id="cancel-task-no" type="button" class="button quiet" autofocus>否</button><button id="cancel-task-confirm" type="button" class="button">是，取消本次任务</button></div>';
    const capNotice=document.createElement('p');capNotice.id='cancel-task-capability';capNotice.className='notice';capNotice.textContent='当前后台尚未启用保留配置的取消协议。请重新打开软件加载更新后再操作。';dialog.querySelector('.form-actions').before(capNotice);
    document.body.append(dialog);pause.after(button);
    let pendingId=null,busy=false;
    const close=()=>{if(busy)return;pendingId=null;dialog.close();};
    dialog.querySelector('#cancel-task-no').addEventListener('click',close);
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();else pendingId=null;});
    button.addEventListener('click',()=>{
      const id=document.querySelector('#job-title')?.textContent?.trim();
      if(!id || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id))return alert('请先选择一条流水线。');
      pendingId=id;dialog.querySelector('#cancel-task-id').textContent=id;dialog.querySelector('#cancel-task-confirm').disabled=!canCancel();capNotice.hidden=canCancel();dialog.showModal();dialog.querySelector('#cancel-task-no').focus();
    });
    dialog.querySelector('#cancel-task-confirm').addEventListener('click',async()=>{
      if(busy || !pendingId || !canCancel())return;const id=pendingId;busy=true;
      dialog.querySelectorAll('button').forEach(b=>b.disabled=true);
      try {
        const response=await fetch('/api/jobs/cancel',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:JSON.stringify({job_id:id,confirmation:'CANCEL '+id})});
        const value=await response.json();if(!response.ok || !value.ok)throw Error(value.error || '取消失败');
        alert('本次任务已取消。流水线、工位、连线与预算配置保留，可编辑后重新开始。已删除的内容不可恢复。');location.reload();
      }catch(error){alert(error.message);}finally{busy=false;pendingId=null;dialog.close();dialog.querySelectorAll('button').forEach(b=>b.disabled=false);dialog.querySelector('#cancel-task-confirm').disabled=!canCancel();}
    });
  }
  const exitButton=document.createElement('button');exitButton.id='exit-backend';exitButton.className='button quiet';exitButton.textContent='退出后台并暂停任务';
  const exitDialog=document.createElement('dialog');exitDialog.id='exit-backend-dialog';exitDialog.setAttribute('aria-labelledby','exit-backend-title');
  exitDialog.innerHTML='<h2 id="exit-backend-title">退出后台并暂停任务</h2><p>退出后台会先请求暂停并保存活动任务；再次打开后不会自动恢复。只关闭界面时，后台继续运行。</p><div class="form-actions end"><button type="button" id="exit-backend-no" class="button quiet" autofocus>否</button><button type="button" id="exit-backend-confirm" class="button">是，退出后台</button></div>';
  const exitNotice=document.createElement('p');exitNotice.id='exit-backend-capability';exitNotice.className='notice';exitNotice.textContent='当前后台尚未启用退出暂停协议。请重新打开软件加载更新后再操作。';exitDialog.querySelector('.form-actions').before(exitNotice);
  const exitPanel=document.createElement('section');exitPanel.className='panel';exitPanel.append(exitButton);(document.querySelector('main') || document.body).append(exitPanel);document.body.append(exitDialog);
  exitButton.addEventListener('click',()=>{exitDialog.querySelector('#exit-backend-confirm').disabled=currentCapabilities.backend_pause_exit!==true;exitNotice.hidden=currentCapabilities.backend_pause_exit===true;exitDialog.showModal();exitDialog.querySelector('#exit-backend-no').focus();});
  exitDialog.querySelector('#exit-backend-no').addEventListener('click',()=>exitDialog.close());
  exitDialog.querySelector('#exit-backend-confirm').addEventListener('click',async()=>{
    if(exitBusy || currentCapabilities.backend_pause_exit!==true)return;exitBusy=true;const confirm=exitDialog.querySelector('#exit-backend-confirm');confirm.disabled=true;
    try{const response=await fetch('/api/app/exit',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:'{}'});const value=await response.json();if(!response.ok || value.ok===false)throw Error(value.error || '操作失败');alert('已请求暂停并退出后台。请等待任务保存与收尾；重开后需明确恢复。');exitDialog.close();}
    catch(error){alert(error.message);}finally{exitBusy=false;confirm.disabled=currentCapabilities.backend_pause_exit!==true;}
  });

  // Deletion is distinct from cancelling one run and requires a declared backend protocol.
  let deletionState={},deletePending=null,deleteBusy=false;
  const deleteButton=document.createElement('button');deleteButton.id='delete-pipeline';deleteButton.className='button small danger';deleteButton.textContent='删除流水线';deleteButton.disabled=true;
  deleteButton.setAttribute('aria-describedby','delete-pipeline-hint');
  const deleteHint=document.createElement('p');deleteHint.id='delete-pipeline-hint';deleteHint.className='muted small-text';deleteHint.setAttribute('role','status');
  const deleteAnchor=document.querySelector('#cancel-task') || document.querySelector('#pause-job');
  if(deleteAnchor){deleteAnchor.after(deleteButton);deleteAnchor.parentElement.after(deleteHint);}
  const deleteDialog=document.createElement('dialog');deleteDialog.id='delete-pipeline-dialog';deleteDialog.setAttribute('aria-labelledby','delete-pipeline-title');deleteDialog.setAttribute('aria-describedby','delete-pipeline-warning');
  deleteDialog.innerHTML='<h2 id="delete-pipeline-title">删除流水线</h2><p id="delete-pipeline-warning">删除整条流水线，会移除流水线配置及其保存内容，无法恢复。取消本次任务会保留配置；如果只是想调整任务，请先暂停修改。是否确定删除？</p><p id="delete-pipeline-id" class="muted" data-i18n-raw="true"></p><p class="muted">已删除的流水线编号不能再次使用；请为新流水线使用新编号。</p><p class="muted">已发生费用、用量及必要审计记录不会因删除而退款或清零。费用尚未核对时，需先完成核对才能删除。</p><div class="form-actions end"><button id="delete-pipeline-no" class="button quiet" type="button" autofocus>否</button><button id="delete-pipeline-confirm" class="button danger" type="button">是，删除流水线</button></div>';
  document.body.append(deleteDialog);
  const deletionId=value=>typeof value==='string'?value:value?.pipeline_id || value?.job_id || value?.id || '';
  const selectedDeletionId=()=>document.querySelector('#job-title')?.textContent?.trim() || '';
  const deletionBlock=id=>{
    if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id))return '请先选择一条流水线。';
    if(deletionState.app?.capabilities?.pipeline_deletion!==true)return '当前后台尚未启用删除流水线协议。请重新打开软件加载更新后再操作。';
    const row=(deletionState.jobs || []).find(item=>deletionId(item)===id);
    if(!row)return '请先选择一条流水线。';
    const active=(deletionState.running_jobs || []).some(item=>deletionId(item)===id) || deletionId(deletionState.running_job)===id || row.running===true || row.runtime?.running===true || ['RUNNING','STARTING','PAUSING','RESUMING','CANCELLING','DELETING'].includes(row.status);
    return active?'流水线仍在运行或暂停收尾，请先暂停并等待任务进程退出，再删除。':'';
  };
  const updateDelete=()=>{const reason=deletionBlock(selectedDeletionId());deleteButton.disabled=deleteBusy || !!reason;deleteHint.textContent=reason || '取消本次任务保留流水线配置；删除流水线会移除整条配置及保存内容。';if(deletePending)deleteDialog.querySelector('#delete-pipeline-confirm').disabled=deleteBusy || !!deletionBlock(deletePending);};
  document.addEventListener('studio:state',event=>{deletionState=event.detail || {};updateDelete();});
  const deletionTitle=document.querySelector('#job-title');if(deletionTitle)new MutationObserver(updateDelete).observe(deletionTitle,{subtree:true,childList:true,characterData:true});
  deleteButton.addEventListener('click',()=>{const id=selectedDeletionId();if(deletionBlock(id))return;deletePending=id;deleteDialog.querySelector('#delete-pipeline-id').textContent=id;updateDelete();deleteDialog.showModal();deleteDialog.querySelector('#delete-pipeline-no').focus();});
  deleteDialog.querySelector('#delete-pipeline-no').addEventListener('click',()=>{if(!deleteBusy){deletePending=null;deleteDialog.close();}});
  deleteDialog.addEventListener('cancel',event=>{if(deleteBusy)event.preventDefault();else deletePending=null;});
  deleteDialog.querySelector('#delete-pipeline-confirm').addEventListener('click',async()=>{if(deleteBusy || !deletePending || deletionBlock(deletePending))return;const id=deletePending;deleteBusy=true;updateDelete();
    try{const response=await fetch('/api/pipelines/delete',{method:'POST',headers:{'Content-Type':'application/json','X-Studio-Token':window.STUDIO_TOKEN},body:JSON.stringify({job_id:id,confirmation:'DELETE '+id})});const value=await response.json();if(!response.ok || value.ok===false)throw Error(value.error || '删除失败');alert('流水线已删除。已删除内容无法恢复，已发生费用与审计记录不清零。');location.reload();}
    catch(error){alert(error.message);}finally{deleteBusy=false;deletePending=null;deleteDialog.close();updateDelete();}
  });
  updateDelete();
})();
