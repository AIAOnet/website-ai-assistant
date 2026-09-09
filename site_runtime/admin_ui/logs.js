"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
  const audit=node("section","");audit.id="security-audit";audit.className="rag-card";audit.hidden=true;
  const traffic=node("section","");traffic.id="traffic-metrics";traffic.className="rag-card";
  const trafficHeading=node("div","");trafficHeading.className="rag-heading";const trafficTitle=node("div","");trafficTitle.append(node("h3","Traffic protection"),node("p","Aggregate public API activity since this application instance started."));
  const trafficButton=node("button","Refresh traffic metrics");trafficButton.id="traffic-metrics-refresh";trafficButton.type="button";trafficButton.className="secondary";trafficHeading.append(trafficTitle,trafficButton);
  const trafficStatus=node("p","Open Logs and refresh to inspect public API protection.");trafficStatus.id="traffic-metrics-status";trafficStatus.setAttribute("role","status");traffic.append(trafficHeading,node("p","Counts contain no messages, form values, conversation IDs, IP addresses, cookies, or credentials."),trafficStatus);byId("logs").append(traffic);
  const monitoring=node("section","");monitoring.id="monitoring";monitoring.className="rag-card";monitoring.append(node("h3","Local monitoring"),node("p","Health thresholds use only bounded aggregate diagnostics. Alerts stay local; no external delivery is configured."));
  const monitoringStatus=node("p","Open Logs to evaluate health.");monitoringStatus.id="monitoring-status";monitoringStatus.setAttribute("role","status");
  const monitoringForm=node("form","");monitoringForm.id="monitoring-thresholds";const fields=document.createElement("fieldset");fields.disabled=true;
  const settings=[["fallback","Fallback rate (%)",30,60,100],["provider","Provider failures",3,10,10000],["api","API errors",10,30,100000],["limited","Rate-limit rejections",10,30,100000]];
  settings.forEach(([key,label,warning,critical,maximum])=>{const row=node("div","");row.className="regex-row";[["warning","Warning",warning],["critical","Critical",critical]].forEach(([level,text,value])=>{const wrap=node("div","");const id=`monitoring-${key}-${level}`;const caption=document.createElement("label");caption.htmlFor=id;caption.textContent=`${label} · ${text}`;const input=document.createElement("input");input.id=id;input.type="number";input.min="1";input.max=String(maximum);input.value=String(value);input.required=true;wrap.append(caption,input);row.append(wrap)});fields.append(row)});
  const confirmation=document.createElement("label");confirmation.className="check-label";const check=document.createElement("input");check.type="checkbox";check.id="monitoring-confirm";confirmation.append(check,document.createTextNode(" I confirm replacing the local health thresholds."));const save=node("button","Save monitoring thresholds");save.type="submit";save.disabled=true;fields.append(confirmation,save);monitoringForm.append(fields);const alerts=node("div","");alerts.id="monitoring-alerts";alerts.setAttribute("aria-live","polite");monitoring.append(monitoringStatus,monitoringForm,alerts);byId("logs").append(monitoring);
  const heading=node("div","");heading.className="rag-heading";const title=node("div","");title.append(node("h3","Security audit"),node("p","Attributable administrator activity with bounded retention and tamper-evident sequencing."));
  const auditButton=node("button","Refresh security audit");auditButton.id="security-audit-refresh";auditButton.type="button";auditButton.className="secondary";heading.append(title,auditButton);
  const notice=node("p","Administrator role required. Credentials, cookies, CSRF values, API keys, prompts, answers and document content are never recorded.");notice.className="notice";
  const auditStatus=node("p","Open Logs and refresh to inspect the security audit.");auditStatus.id="security-audit-status";auditStatus.setAttribute("role","status");
  const auditResults=node("div","");auditResults.id="security-audit-results";auditResults.setAttribute("aria-live","polite");audit.append(heading,notice,auditStatus,auditResults);byId("logs").append(audit);
  async function refresh() {
    const button = byId("logs-refresh");
    button.disabled = true;
    byId("logs-status").textContent = "Loading diagnostics…";
    byId("logs-results").replaceChildren();
    try {
      const data = await api("logs?kind=" + encodeURIComponent(byId("logs-kind").value));
      const storage = data.persistent ? "Persistent local storage" : "Temporary test storage";
      byId("logs-status").textContent = `${data.events.length} shown · ${data.matching_events} matching · ${data.retained_events}/${data.capacity} retained · ${storage} · Newest first`;
      if(data.storage_healthy===false)byId("logs-status").textContent+=" · Diagnostic storage failed; some events were not recorded. Repair storage before relying on these logs.";
      if (!data.events.length) byId("logs-results").append(node("p", "No events yet. Ask the assistant a question or save an admin setting, then refresh."));
      for (const event of data.events) {
        const entry = node("article", ""); entry.className = "rag-result";
        entry.append(node("h3", event.kind === "chat" ? `Chat · ${event.generation}` : `Admin · ${event.action}`));
        entry.append(node("small", `${event.timestamp} · ${event.outcome} · ${event.duration_ms} ms · Event ${event.id}`));
        if (event.kind === "admin") {
          if ((event.action === "evaluation.suite.run" || event.action === "evaluation.live_suite.run") && typeof event.suite_version === "string" &&
              typeof event.passed === "boolean" && Number.isInteger(event.total_cases) &&
              Number.isInteger(event.completed_cases) && Number.isInteger(event.passed_cases) &&
              Number.isInteger(event.failed_cases) && Number.isFinite(event.evaluation_duration_ms) &&
              Number.isInteger(event.model_call_count) && Array.isArray(event.failed_case_codes)) {
            entry.classList.add("evaluation-log");entry.dataset.pass=String(event.passed);
            const suiteLabel=event.action === "evaluation.live_suite.run" ? "Live-LLM suite" : "Non-LLM suite";
            entry.append(node("p", `${event.passed ? "Passed" : "Failed"} · ${suiteLabel} ${event.suite_version} · ${event.completed_cases}/${event.total_cases} completed · ${event.passed_cases} passed · ${event.failed_cases} failed · Evaluation ${event.evaluation_duration_ms} ms · ${event.model_call_count} model calls · HTTP ${event.status}.`));
            event.failed_case_codes.forEach(item=>{
              if(item && typeof item.case_id==="string" && Array.isArray(item.failure_codes) && item.failure_codes.every(code=>typeof code==="string"))
                entry.append(node("small",`Failure · ${item.case_id} · ${item.failure_codes.join(", ").replaceAll("_"," ")||"unclassified"}`));
            });
            entry.append(node("small", "No questions, answers, prompts, evidence, credentials, provider details, or administrator identity recorded."));
          } else if (event.action === "provider.probe" && typeof event.provider_outcome === "string" &&
              Number.isFinite(event.provider_duration_ms)) {
            entry.append(node("p", `Provider test · ${event.provider_outcome.replaceAll("_"," ")} · ${event.provider_duration_ms} ms · HTTP ${event.status}.`));
            entry.append(node("small", "No endpoint, key, model, prompt, response, provider detail, or administrator identity recorded."));
          } else if (event.action === "evaluation.run" && typeof event.case_id === "string" &&
              typeof event.passed === "boolean" && Number.isFinite(event.evaluation_duration_ms) &&
              Number.isInteger(event.model_call_count) && Array.isArray(event.failure_codes)) {
            entry.classList.add("evaluation-log");entry.dataset.pass=String(event.passed);
            entry.append(node("p", `${event.passed ? "Passed" : "Failed"} · Case ${event.case_id} · Evaluation ${event.evaluation_duration_ms} ms · ${event.model_call_count} model call${event.model_call_count === 1 ? "" : "s"} · HTTP ${event.status}.`));
            if(event.failure_codes.length)entry.append(node("small",`Failure · ${event.failure_codes.join(", ").replaceAll("_"," ")}`));
            entry.append(node("small", "No question, answer, prompt, evidence, credentials, provider details, or administrator identity recorded."));
          } else entry.append(node("p", `HTTP ${event.status}. No configuration values or administrator identity recorded.`));
        }
        else {
          entry.append(node("p", `Model calls: ${event.model_calls.length} · Follow-up context: ${event.follow_up_resolved ? "resolved" : "not used"}` + (event.fallback_reasons.length ? ` · Fallback: ${event.fallback_reasons.join(", ")}` : "")));
          const classification=event.intent_classification;
          if(classification && typeof classification.mode==="string" && typeof classification.outcome==="string")entry.append(node("small",`Intent classification: ${classification.mode.replaceAll("_"," ")} · ${classification.outcome.replaceAll("_"," ")}`));
          event.model_calls.forEach((call, index) => entry.append(node("small", `Model call ${index + 1}: ${call.outcome} · ${call.duration_ms} ms (includes grounding review calls)`)));
          const r = event.retrieval;
          if (r) {
            entry.append(node("p", `Retrieved sources: ${r.source_ids.join(", ") || "none"}`));
            entry.append(node("small", `Search: ${r.search_mode} · Embeddings used: ${r.embedding_used} · Retrieval fallback: ${r.embedding_fallback || "none"}`));
            entry.append(node("small", `Ontology: ${r.ontology_match_count} entity matches · Depth: ${r.ontology_depth} · Rows at request time: ${r.ontology_rows.join(", ") || "none"} · Paths: ${r.ontology_paths.map(path=>`depth ${path.depth} rows ${path.relationship_rows.join("→")}`).join(", ") || "none"} · Linked sources: ${r.ontology_source_ids.join(", ") || "none"} · Truncated: ${r.ontology_truncated} · Graph fallback: ${r.ontology_fallback || "none"}`));
          } else entry.append(node("p", "No knowledge retrieval recorded for this request."));
        }
        byId("logs-results").append(entry);
      }
    } catch (problem) { byId("logs-status").textContent = "Diagnostics unavailable: " + problem.message; }
    finally { button.disabled = false; }
  }
  async function refreshAudit() {
    const button=byId("security-audit-refresh");if(!button||document.documentElement.dataset.adminRole!=="administrator")return;
    button.disabled=true;byId("security-audit-status").textContent="Loading security audit…";byId("security-audit-results").replaceChildren();
    try{
      const data=await api("security-audit");
      byId("security-audit-status").textContent=`${data.events.length} shown · ${data.retained_events}/${data.max_records} retained · ${data.retention_days} day retention · Chain ${data.chain_valid?"valid":"verification failed"} · Newest first`;
      if(!data.events.length)byId("security-audit-results").append(node("p","No security events yet."));
      data.events.forEach(event=>{const entry=node("article","");entry.className="rag-result";entry.append(node("h3",`${event.action} · ${event.outcome}`));entry.append(node("p",`${event.actor} · ${event.role} · HTTP ${event.status}`));entry.append(node("small",`${event.timestamp} · Sequence ${event.sequence} · Event ${event.event_id}`));if(event.target_type&&event.target_id)entry.append(node("small",`Target: ${event.target_type} · ${event.target_id}`));byId("security-audit-results").append(entry)});
    }catch(problem){byId("security-audit-status").textContent="Security audit unavailable: "+problem.message}finally{button.disabled=false}
  }
  async function refreshTraffic(){const button=byId("traffic-metrics-refresh");button.disabled=true;byId("traffic-metrics-status").textContent="Loading traffic metrics…";try{const data=await api("traffic-metrics");const chat=data.routes.chat,appointments=data.routes.appointments;byId("traffic-metrics-status").textContent=`Shared persistent limits · Chat: ${chat.allowed} allowed · ${chat.limited} limited · ${chat.failed} failed · ${chat.active_clients} active clients · limit ${chat.limit_per_client}/${data.window_seconds}s. Appointments: ${appointments.allowed} allowed · ${appointments.limited} limited · ${appointments.failed} failed · ${appointments.active_clients} active clients · limit ${appointments.limit_per_client}/${data.window_seconds}s.`}catch(problem){byId("traffic-metrics-status").textContent="Traffic metrics unavailable: "+problem.message}finally{button.disabled=false}}
  function renderMonitoring(data){byId("monitoring-status").textContent=`${data.status.toUpperCase()} · Fallback ${data.metrics.fallback_percent}% · Provider failures ${data.metrics.provider_failures} · API errors ${data.metrics.api_errors} · Limited ${data.metrics.rate_limited} · ${data.alerts.length}/${data.capacity} alert transitions retained.`;const map={fallback:"fallback",provider:"provider_failure",api:"api_error",limited:"rate_limit"};Object.entries(map).forEach(([key,name])=>{byId(`monitoring-${key}-warning`).value=data.thresholds[`${name}_warning${name==="fallback"?"_percent":""}`];byId(`monitoring-${key}-critical`).value=data.thresholds[`${name}_critical${name==="fallback"?"_percent":""}`]});const results=byId("monitoring-alerts");results.replaceChildren();data.alerts.slice(0,10).forEach(alert=>{const item=node("article","");item.className="rag-result";item.append(node("h4",alert.status.toUpperCase()),node("p",alert.reasons.join(", ").replaceAll("_"," ")||"Recovered"),node("small",`${alert.timestamp} · Alert ${alert.alert_id}`));results.append(item)});fields.disabled=false}
  async function refreshMonitoring(){byId("monitoring-status").textContent="Evaluating aggregate health…";try{renderMonitoring(await api("monitoring"))}catch(problem){byId("monitoring-status").textContent="Monitoring unavailable: "+problem.message}}
  function monitoringPayload(){const result={confirmed:true};const map={fallback:"fallback",provider:"provider_failure",api:"api_error",limited:"rate_limit"};Object.entries(map).forEach(([key,name])=>{result[`${name}_warning${name==="fallback"?"_percent":""}`]=Number(byId(`monitoring-${key}-warning`).value);result[`${name}_critical${name==="fallback"?"_percent":""}`]=Number(byId(`monitoring-${key}-critical`).value)});return result}
  byId("logs-refresh").addEventListener("click", refresh);
  byId("security-audit-refresh").addEventListener("click", refreshAudit);
  byId("traffic-metrics-refresh").addEventListener("click", refreshTraffic);
  byId("logs-kind").addEventListener("change", refresh);
  byId("monitoring-confirm").addEventListener("change",event=>{save.disabled=!event.target.checked});monitoringForm.addEventListener("submit",async event=>{event.preventDefault();if(!check.checked)return;save.disabled=true;try{renderMonitoring(await api("monitoring",{method:"PUT",body:JSON.stringify(monitoringPayload())}));check.checked=false}catch(problem){byId("monitoring-status").textContent=problem.message}finally{save.disabled=!check.checked}});
  document.querySelector('[data-tab="logs"]').addEventListener("click",()=>{refresh();refreshTraffic();refreshMonitoring()});
})();
