"use strict";
(() => {
  const byId=id=>document.getElementById(id);
  const node=(tag,text)=>{const element=document.createElement(tag);element.textContent=text;return element};
  const panel=byId("evaluations"), form=byId("evaluations-form"), suiteForm=byId("evaluations-suite-form"), liveForm=byId("evaluations-live-form"), select=byId("evaluations-case");
  let cases=[], configured=false, loaded=false;
  let definitions=[], revision=0, editingId=null, editorOpen=false, managing=false, canWrite=false;
  const editor=byId("evaluation-editor");
  const lines=id=>byId(id).value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  function modeFields(){
    const retrieval=byId("evaluation-mode").value==="retrieval";
    byId("evaluation-answer-fields").hidden=retrieval;
    byId("evaluation-answer-fields").querySelectorAll("input,select,textarea").forEach(input=>input.disabled=retrieval);
    byId("evaluation-generation").required=!retrieval;
  }
  function renderDefinitions(){
    byId("evaluation-add").disabled=!canWrite||managing||editorOpen;
    byId("evaluation-test-list").replaceChildren(...definitions.map(item=>{
      const row=node("article","");row.className="rag-result";
      row.append(node("strong",item.description),node("p",`${item.case_id} · ${item.language.toUpperCase()} · ${item.execution_mode} · ${item.enabled?"Enabled":"Disabled"}`));
      const actions=node("div","");actions.className="ontology-actions";
      const edit=node("button","Edit"),remove=node("button","Delete");
      edit.type=remove.type="button";edit.className=remove.className="secondary";
      edit.disabled=remove.disabled=!canWrite||managing||editorOpen;
      edit.addEventListener("click",()=>openDefinition(item));
      remove.addEventListener("click",async()=>{
        if(!confirm(`Delete test "${item.description}"?`))return;
        managing=true;renderDefinitions();
        try{await api("evaluations/manage",{method:"POST",body:JSON.stringify({revision,operation:"delete",case_id:item.case_id})});await load()}
        catch(problem){byId("evaluation-manage-status").textContent=problem.message}
        finally{managing=false;renderDefinitions()}
      });
      actions.append(edit,remove);row.append(actions);return row;
    }));
  }
  async function loadDefinitions(){
    if(editorOpen)return;
    const identity=await api("session");canWrite=identity.permissions.write;
    const data=await api("evaluations/manage");definitions=data.cases;revision=data.revision;
    byId("evaluation-manage-status").textContent=definitions.length?`${definitions.length} saved tests${canWrite?"":" · Read-only access"}`:"No tests yet. Add your first test to get started.";
    renderDefinitions();
  }
  function openDefinition(item=null){
    if(!canWrite||managing)return;
    editingId=item?.case_id||null;editorOpen=true;editor.hidden=false;
    byId("evaluation-editor-title").textContent=item?"Edit test":"Add test";
    const values={id:item?.case_id||"",description:item?.description||"",question:item?.question||"",language:item?.language||"en",mode:item?.execution_mode||"retrieval",intent:item?.expected_intent||"COMPANY_INFORMATION",contact:item?.expected_contact_id||"",sources:(item?.expected_source_ids||[]).join("\n"),required:(item?.required_phrases||[]).join("\n"),forbidden:(item?.forbidden_phrases||[]).join("\n"),tags:(item?.tags||["custom"]).join("\n")};
    for(const [key,value] of Object.entries(values))byId("evaluation-"+key).value=value;
    byId("evaluation-id").readOnly=Boolean(item);
    byId("evaluation-enabled").checked=item?.enabled??true;
    for(const option of byId("evaluation-generation").options)option.selected=(item?.expected_generation||["llm","fallback"]).includes(option.value);
    byId("evaluation-editor-error").textContent="";modeFields();renderDefinitions();byId(item?"evaluation-description":"evaluation-id").focus();
  }
  function closeDefinition(){editorOpen=false;editingId=null;editor.hidden=true;renderDefinitions()}
  byId("evaluation-add").addEventListener("click",()=>openDefinition());
  byId("evaluation-cancel").addEventListener("click",()=>{closeDefinition();byId("evaluation-add").focus()});
  byId("evaluation-mode").addEventListener("change",modeFields);
  editor.addEventListener("submit",async event=>{
    event.preventDefault();if(managing||!canWrite)return;
    const retrieval=byId("evaluation-mode").value==="retrieval";
    const item={case_id:byId("evaluation-id").value.trim(),description:byId("evaluation-description").value.trim(),question:byId("evaluation-question").value.trim(),enabled:byId("evaluation-enabled").checked,language:byId("evaluation-language").value,execution_mode:byId("evaluation-mode").value,expected_intent:byId("evaluation-intent").value,expected_generation:retrieval?[]:[...byId("evaluation-generation").selectedOptions].map(x=>x.value),expected_source_ids:lines("evaluation-sources"),expected_contact_id:retrieval?null:byId("evaluation-contact").value.trim()||null,required_phrases:retrieval?[]:lines("evaluation-required"),forbidden_phrases:retrieval?[]:lines("evaluation-forbidden"),tags:lines("evaluation-tags")};
    managing=true;editor.querySelector("fieldset").disabled=true;byId("evaluation-editor-error").textContent="";
    try{await api("evaluations/manage",{method:"POST",body:JSON.stringify({revision,operation:editingId?"update":"create",case_id:item.case_id,case:item})});closeDefinition();await load();byId("evaluation-manage-status").textContent="Test saved in the database."}
    catch(problem){byId("evaluation-editor-error").textContent=problem.message+" Your edits are retained. If tests changed, cancel and refresh before retrying."}
    finally{managing=false;editor.querySelector("fieldset").disabled=false;renderDefinitions()}
  });
  function selected(){return cases.find(item=>item.case_id===select.value)}
  function describe(){
    const item=selected();
    if(!item){byId("evaluations-case-detail").textContent="";return}
    const model=item.execution_mode==="full"
      ? configured?"May call the configured live LLM.":"Live LLM is not configured; full generation will use a safe fallback."
      :"Does not call the LLM.";
    byId("evaluations-case-detail").textContent=`${item.description} ${item.language.toUpperCase()} · ${item.execution_mode} · ${model} Tags: ${item.tags.join(", ")}.`;
  }
  function renderResult(result){
    const card=node("article","");card.className="rag-result evaluation-summary";card.dataset.pass=String(result.passed);
    card.append(node("h3",result.passed?"Evaluation passed":"Evaluation failed"));
    card.append(node("p",`${result.case_id} · suite ${result.suite_version} · ${result.duration_ms} ms · ${result.model_call_count} model call${result.model_call_count===1?"":"s"}`));
    card.append(node("p",`Retrieved sources: ${result.retrieved_source_ids.join(", ")||"none"} · Fallback: ${result.fallback_codes.join(", ")||"none"}`));
    const checks=node("ul","");checks.className="evaluation-checks";
    result.checks.forEach(check=>{const item=node("li",`${check.passed?"Pass":"Fail"} · ${check.code.replaceAll("_"," ")}`);item.dataset.pass=String(check.passed);checks.append(item)});
    card.append(checks);byId("evaluations-result").replaceChildren(card);
  }
  function renderSuite(result){
    const summary=node("article","");summary.className="rag-result evaluation-summary";summary.dataset.pass=String(result.passed);
    const label=result.suite_kind==="full"?"Live-LLM suite":"Non-LLM suite";
    summary.append(node("h3",`${label} ${result.passed?"passed":"failed"}`));
    summary.append(node("p",`Suite ${result.suite_version} · ${result.completed_cases}/${result.total_cases} completed · ${result.passed_cases} passed · ${result.failed_cases} failed · ${result.duration_ms} ms · ${result.model_call_count} model calls`));
    const items=node("div","");items.className="evaluation-suite-results";
    result.results.forEach(item=>{const failures=item.failure_codes.length?` · Failure: ${item.failure_codes.join(", ").replaceAll("_"," ")}`:"";const row=node("p",`${item.passed?"Pass":"Fail"} · ${item.case_id} · ${item.duration_ms} ms · ${item.model_call_count} model calls${failures}`);row.dataset.pass=String(item.passed);items.append(row)});
    summary.append(items);byId("evaluations-result").replaceChildren(summary);
  }
  function disableActions(disabled){
    byId("evaluations-run").disabled=disabled||!cases.length;select.disabled=disabled||!cases.length;
    byId("evaluations-suite-confirm").disabled=disabled;
    byId("evaluations-suite-run").disabled=disabled||!cases.some(c=>c.execution_mode!=="full")||!byId("evaluations-suite-confirm").checked;
    byId("evaluations-live-confirm").disabled=disabled;
    byId("evaluations-live-run").disabled=disabled||!cases.some(c=>c.execution_mode==="full")||!byId("evaluations-live-confirm").checked;
  }
  async function load(){
    const refresh=byId("evaluations-refresh");refresh.disabled=true;byId("evaluations-status").textContent="Loading evaluation cases…";
    try{
      await loadDefinitions();
      const data=await api("evaluations/cases");cases=data.cases;configured=data.assistant_configured;
      select.replaceChildren(...cases.map(item=>{const option=node("option",`${item.description} · ${item.language.toUpperCase()} · ${item.execution_mode}`);option.value=item.case_id;return option}));
      form.querySelector("fieldset").disabled=!cases.length;loaded=true;describe();disableActions(false);
      const offline=cases.filter(item=>item.execution_mode!=="full").length;
      const live=cases.filter(item=>item.execution_mode==="full").length;
      byId("evaluations-suite-run").textContent=`Run ${offline} non-LLM cases`;
      byId("evaluations-live-run").textContent=`Run ${live} live cases`;
      byId("evaluations-status").textContent=`${cases.length} enabled cases · suite ${data.suite_version} · live LLM ${configured?"configured":"not configured"}.`;
    }catch(problem){byId("evaluations-status").textContent="Evaluation cases unavailable: "+problem.message}
    finally{refresh.disabled=false}
  }
  form.addEventListener("submit",async event=>{
    event.preventDefault();const item=selected();if(!item)return;
    error.textContent="";byId("evaluations-result").replaceChildren();disableActions(true);
    byId("evaluations-status").textContent=`Running ${item.case_id}…`;
    try{const result=await api("evaluations/run",{method:"POST",body:JSON.stringify({case_id:item.case_id})});renderResult(result);byId("evaluations-status").textContent=`${item.case_id} completed.`}
    catch(problem){byId("evaluations-status").textContent="Evaluation did not complete.";error.textContent=problem.message}
    finally{disableActions(false)}
  });
  suiteForm.addEventListener("submit",async event=>{
    event.preventDefault();if(!byId("evaluations-suite-confirm").checked)return;
    error.textContent="";byId("evaluations-result").replaceChildren();disableActions(true);
    const total=cases.filter(item=>item.execution_mode!=="full").length;
    byId("evaluations-status").textContent=`Running ${total} non-LLM cases sequentially…`;
    try{const result=await api("evaluations/run-non-llm",{method:"POST"});renderSuite(result);byId("evaluations-status").textContent="Non-LLM suite completed."}
    catch(problem){byId("evaluations-status").textContent="Non-LLM suite did not complete.";error.textContent=problem.message}
    finally{disableActions(false)}
  });
  liveForm.addEventListener("submit",async event=>{
    event.preventDefault();if(!byId("evaluations-live-confirm").checked)return;
    error.textContent="";byId("evaluations-result").replaceChildren();disableActions(true);
    const total=cases.filter(item=>item.execution_mode==="full").length;
    byId("evaluations-status").textContent=`Running ${total} live-LLM cases sequentially…`;
    try{
      const result=await api("evaluations/run-full",{method:"POST",body:JSON.stringify({confirmed_live_model_calls:true,expected_case_count:total})});
      renderSuite(result);byId("evaluations-status").textContent="Live-LLM suite completed.";
    }catch(problem){byId("evaluations-status").textContent="Live-LLM suite did not complete.";error.textContent=problem.message}
    finally{byId("evaluations-live-confirm").checked=false;disableActions(false)}
  });
  byId("evaluations-suite-confirm").addEventListener("change",()=>disableActions(false));
  byId("evaluations-live-confirm").addEventListener("change",()=>disableActions(false));
  select.addEventListener("change",describe);byId("evaluations-refresh").addEventListener("click",load);
  document.querySelector('[data-tab="evaluations"]').addEventListener("click",()=>{if(!loaded)load()});
})();
