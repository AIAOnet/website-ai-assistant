"use strict";
(() => {
  const byId=id=>document.getElementById(id), status=byId("rag-status");
  const node=(tag,text)=>{const element=document.createElement(tag);element.textContent=text;return element};
  let sources=[],embeddingPreview=null;
  const providerLabels={available:"Provider connection passed.",not_configured:"Provider is not configured.",usage_budget_exhausted:"AI usage budget exhausted; no provider request was sent.",unauthorized:"Provider rejected the credentials.",rate_limited:"Provider rate limit reached.",timeout:"Provider request timed out.",upstream_gateway:"Provider gateway is unavailable.",request_rejected:"Provider rejected the endpoint, model, or request format.",network_unavailable:"Provider network connection is unavailable.",provider_error:"Provider returned an unavailable or invalid response.",busy:"A provider test is already running."};
  function renderProvider(data){byId("provider-probe-status").textContent=data.configured?"Model provider is configured. Run the confirmed test to check current availability.":providerLabels.not_configured}
  function renderModelConfiguration(data){
    byId("model-configuration-status").textContent=data.configured?"Generation provider is configured.":"Generation provider is incomplete or not configured.";
    byId("model-configuration-source").textContent=data.source.replaceAll("_"," ");
    byId("model-configuration-host").textContent=data.endpoint_host||"Not configured";
    byId("model-configuration-model").textContent=data.model||"Not configured";
    byId("model-configuration-timeout").textContent=`${data.timeout_seconds} seconds`;
    byId("model-configuration-key").textContent=data.api_key_configured?"Configured · value hidden":"Not configured";
    const warning=byId("model-configuration-warning");warning.textContent=data.warning||"";warning.hidden=!data.warning;
    byId("model-configuration-model-input").value=data.model||"";
    byId("model-configuration-timeout-input").value=data.timeout_seconds;
  }
  function renderEmbeddingConfiguration(data){
    byId("embedding-configuration-status").textContent=data.configured?"Embedding provider is configured.":"Embedding provider is incomplete or not configured.";
    byId("embedding-configuration-source").textContent=data.source.replaceAll("_"," ");
    byId("embedding-configuration-host").textContent=data.endpoint_host||"Not configured";
    byId("embedding-configuration-model").textContent=data.model||"Not configured";
    byId("embedding-configuration-timeout").textContent=`${data.timeout_seconds} seconds`;
    byId("embedding-configuration-key").textContent=data.api_key_configured?"Configured · value hidden":"Not configured";
    const warning=byId("embedding-configuration-warning");warning.textContent=data.warning||"";warning.hidden=!data.warning;
    byId("embedding-configuration-model-input").value=data.model||"";
    byId("embedding-configuration-timeout-input").value=data.timeout_seconds;
    byId("embedding-probe-status").textContent=data.configured?"Embedding provider is configured. Run the confirmed test to check current availability.":providerLabels.not_configured;
  }
  async function refreshEmbeddingPreview(){
    embeddingPreview=await api("embeddings/rebuild-preview");
    byId("embedding-rebuild-status").textContent=embeddingPreview.configured?`${embeddingPreview.chunk_count} approved chunks · ${embeddingPreview.matching_vectors} matching cached vectors · ${embeddingPreview.vectors_to_generate} currently missing · Model: ${embeddingPreview.model} · ${embeddingPreview.running?"Rebuild running":"Ready for confirmed full replacement"}`:"Embedding provider is not configured.";
    byId("embedding-rebuild-confirm").checked=false;byId("embedding-rebuild-run").disabled=true;
  }
  function sourceLink(source){const a=node("a",source.title);a.href=source.canonical_url;a.target="_blank";a.rel="noopener noreferrer";return a}
  function renderSources(){
    const filter=byId("rag-source-filter").value.toLowerCase();
    const matches=sources.filter(s=>[s.title,s.language,s.category,s.source_id].join(" ").toLowerCase().includes(filter));
    byId("rag-source-count").textContent=`${matches.length} of ${sources.length} records`;
    byId("rag-sources").replaceChildren(...matches.map(s=>{
      const entry=node("details","");entry.className="rag-source";
      entry.append(node("summary",`${s.title} · ${s.language.toUpperCase()} · ${s.source_status}`),
        node("small",`${s.source_id} · ${s.category} · retrieved ${s.retrieved_at}`),
        node("small",`Checksum: ${s.checksum}`),sourceLink(s),node("p",s.content));
      return entry;
    }));
  }
  function render(data){
    const s=data.settings;
    byId("rag-limit").value=s.result_limit;byId("rag-score").value=s.minimum_score;
    byId("rag-weight").value=s.lexical_weight;byId("rag-semantic").checked=s.use_semantic;
    byId("rag-ontology-depth").value=String(s.ontology_depth);
    byId("rag-semantic-weight").textContent=(1-s.lexical_weight).toFixed(2);
    status.textContent=`${data.index.active_records} active records · Index: ${data.index.status}`+
      (data.index.generated_at?` · Built ${data.index.generated_at}`:"")+
      ` · Embeddings: ${data.embedding.configured?"configured (availability checked during search)":"not configured"}`+
      (data.embedding.model?` · Model: ${data.embedding.model}`:"")+
      ` · Cached vectors: ${data.embedding.vectors_cached}`+
      (data.embedding.dimensions?` × ${data.embedding.dimensions} dimensions`:"")+
      ` · Cache: ${data.embedding.persistent?"persistent":"unavailable"}`;
    byId("rag-warning").textContent=data.settings_warning||"";
    renderProvider(data.provider);
    renderModelConfiguration(data.generation_configuration);
    renderEmbeddingConfiguration(data.embedding_configuration);
    const classifier=data.intent_classification;byId("intent-classification-status").textContent=`Optional schema classifier: ${classifier.enabled?"enabled":"disabled"} · Shared model provider: ${classifier.provider_available?"available":"unavailable"} · Allowed outcomes: product information, service information, company information, or out of scope.`;
    sources=data.sources;renderSources();
    const category=byId("rag-category"),selected=category.value;
    category.replaceChildren(new Option("All categories",""),...[...new Set(sources.map(s=>s.category))].sort().map(c=>new Option(c,c)));
    if([...category.options].some(option=>option.value===selected))category.value=selected;
  }
  async function run(form,action){
    error.textContent="";const buttons=form.querySelectorAll("button");buttons.forEach(b=>b.disabled=true);
    try{await action()}catch(problem){error.textContent=problem.message}finally{buttons.forEach(b=>b.disabled=(b.id==="model-configuration-save"&&!byId("model-configuration-confirm").checked)||(b.id==="embedding-configuration-save"&&!byId("embedding-configuration-confirm").checked)||(b.id==="embedding-rebuild-run"&&(!byId("embedding-rebuild-confirm").checked||!embeddingPreview?.configured||embeddingPreview.running)))}
  }
  byId("rag-weight").addEventListener("input",()=>byId("rag-semantic-weight").textContent=(1-Number(byId("rag-weight").value)).toFixed(2));
  byId("rag-source-filter").addEventListener("input",renderSources);
  byId("rag-settings").addEventListener("submit",event=>{event.preventDefault();run(event.currentTarget,async()=>{
    const data=await api("rag/settings",{method:"PUT",body:JSON.stringify({result_limit:Number(byId("rag-limit").value),minimum_score:Number(byId("rag-score").value),lexical_weight:Number(byId("rag-weight").value),use_semantic:byId("rag-semantic").checked,ontology_depth:Number(byId("rag-ontology-depth").value)})});
    render(data);status.textContent="Settings saved and active for subsequent searches. "+status.textContent;
  })});
  byId("rag-search").addEventListener("submit",event=>{event.preventDefault();run(event.currentTarget,async()=>{
    byId("rag-search-status").textContent="Searching…";byId("rag-results").replaceChildren();
    try{
      const result=await api("rag/search",{method:"POST",body:JSON.stringify({query:byId("rag-query").value,language:byId("rag-language").value,category:byId("rag-category").value||null})});
      const aliases=result.ontology?.matched_aliases?.map(item=>`${item.alias} → ${item.entity}`).join(", ")||"none";
      const paths=result.ontology?.paths?.map(path=>`${path.entities.join(" → ")} [${path.predicates.join(", ")}; rows ${path.relationship_rows.join(", ")}; sources ${path.source_ids.join(", ")}]`).join(" · ")||"none";
      byId("rag-search-status").textContent=`${result.records.length} matches · ${result.search_mode} · Confidence: ${result.confidence}`+(result.fallback_reason?` · ${result.fallback_reason}`:"")+` · Ontology depth ${result.ontology?.max_hops} · Aliases: ${aliases} · Paths: ${paths}`+(result.ontology?.truncated?" · Traversal truncated at safety limits":"")+` · Limit ${result.settings.result_limit}, minimum ${result.settings.minimum_score}, lexical weight ${result.settings.lexical_weight}`;
      byId("rag-results").replaceChildren(...result.records.map(s=>{const item=node("article","");item.className="rag-result";item.append(sourceLink(s),node("small",`${s.source_id}${s.source_location?.label?` · ${s.source_location.label}`:""} · Combined ${s.score} · Lexical ${s.lexical_score} · Semantic ${s.semantic_score} · Ontology ${s.ontology_score || 0}`),node("p",s.content));return item}));
    }catch(problem){byId("rag-search-status").textContent="Search failed.";throw problem}
  })});
  byId("rag-rebuild").addEventListener("submit",event=>{event.preventDefault();run(event.currentTarget,async()=>{
    status.textContent="Validating and rebuilding source index…";
    try{render(await api("rag/rebuild",{method:"POST"}));status.textContent="Index rebuilt and active. "+status.textContent;byId("rag-rebuild-confirm").checked=false}
    catch(problem){status.textContent="Rebuild failed. Previous live index remains active.";throw problem}
  })});
  byId("provider-probe-confirm").addEventListener("change",event=>{byId("provider-probe-run").disabled=!event.target.checked});
  byId("embedding-rebuild-confirm").addEventListener("change",event=>{byId("embedding-rebuild-run").disabled=!event.target.checked||!embeddingPreview?.configured||embeddingPreview.running});
  byId("embedding-rebuild-refresh").addEventListener("click",()=>run(byId("embedding-rebuild"),refreshEmbeddingPreview));
  byId("embedding-rebuild").addEventListener("submit",event=>{event.preventDefault();if(!embeddingPreview)return;run(event.currentTarget,async()=>{
    byId("embedding-rebuild-status").textContent="Generating a complete replacement vector cache…";
    const result=await api("embeddings/rebuild",{method:"POST",body:JSON.stringify({confirmed_external_embedding_calls:true,expected_model:embeddingPreview.model,expected_chunk_count:embeddingPreview.chunk_count})});
    await refreshEmbeddingPreview();byId("embedding-rebuild-status").textContent=`Rebuilt ${result.chunk_count} chunks in ${result.batch_count} batches · ${result.dimensions} dimensions · Model: ${result.model}.`;
  })});
  byId("model-configuration-confirm").addEventListener("change",event=>{byId("model-configuration-save").disabled=!event.target.checked});
  byId("model-configuration-form").addEventListener("submit",event=>{event.preventDefault();run(event.currentTarget,async()=>{
    const key=byId("model-configuration-api-key").value;
    const data=await api("provider/settings",{method:"PUT",body:JSON.stringify({endpoint:byId("model-configuration-endpoint").value,model:byId("model-configuration-model-input").value,timeout_seconds:Number(byId("model-configuration-timeout-input").value),api_key:key||null,confirmed:true})});
    render(data);byId("model-configuration-endpoint").value="";byId("model-configuration-api-key").value="";byId("model-configuration-confirm").checked=false;byId("model-configuration-save").disabled=true;byId("model-configuration-status").textContent="Model configuration saved and active for new requests.";
  })});
  byId("provider-probe").addEventListener("submit",async event=>{
    event.preventDefault();const confirm=byId("provider-probe-confirm"),button=byId("provider-probe-run");if(!confirm.checked)return;
    error.textContent="";confirm.disabled=true;button.disabled=true;byId("provider-probe-status").textContent="Testing provider connection…";
    try{const result=await api("provider/probe",{method:"POST",body:JSON.stringify({confirmed_model_request:true})});byId("provider-probe-status").textContent=`${providerLabels[result.outcome]||providerLabels.provider_error} ${result.duration_ms} ms.`}
    catch(problem){byId("provider-probe-status").textContent="Provider test did not complete.";error.textContent=problem.message}
    finally{confirm.checked=false;confirm.disabled=false;button.disabled=true}
  });
  byId("embedding-configuration-confirm").addEventListener("change",event=>{byId("embedding-configuration-save").disabled=!event.target.checked});
  byId("embedding-configuration-form").addEventListener("submit",event=>{event.preventDefault();run(event.currentTarget,async()=>{
    const key=byId("embedding-configuration-api-key").value;
    const data=await api("embeddings/settings",{method:"PUT",body:JSON.stringify({endpoint:byId("embedding-configuration-endpoint").value,model:byId("embedding-configuration-model-input").value,timeout_seconds:Number(byId("embedding-configuration-timeout-input").value),api_key:key||null,confirmed:true})});
    render(data);await refreshEmbeddingPreview();byId("embedding-configuration-endpoint").value="";byId("embedding-configuration-api-key").value="";byId("embedding-configuration-confirm").checked=false;byId("embedding-configuration-save").disabled=true;byId("embedding-configuration-status").textContent="Embedding configuration saved and active for new retrieval requests.";
  })});
  byId("embedding-probe-confirm").addEventListener("change",event=>{byId("embedding-probe-run").disabled=!event.target.checked});
  byId("embedding-probe").addEventListener("submit",async event=>{event.preventDefault();const confirm=byId("embedding-probe-confirm"),button=byId("embedding-probe-run");if(!confirm.checked)return;error.textContent="";confirm.disabled=true;button.disabled=true;byId("embedding-probe-status").textContent="Testing embedding provider connection…";try{const result=await api("embeddings/probe",{method:"POST",body:JSON.stringify({confirmed_embedding_request:true})});byId("embedding-probe-status").textContent=`${providerLabels[result.outcome]||providerLabels.provider_error} ${result.duration_ms} ms.`}catch(problem){byId("embedding-probe-status").textContent="Embedding provider test did not complete.";error.textContent=problem.message}finally{confirm.checked=false;confirm.disabled=false;button.disabled=true}});
  byId("rag-refresh").addEventListener("click",()=>run(byId("rag"),async()=>render(await api("rag"))));
  (async()=>{try{const identity=await api("session");csrf=identity.csrf_token;render(await api("rag"));await refreshEmbeddingPreview();document.querySelectorAll("#rag fieldset,#configuration fieldset").forEach(f=>f.disabled=false)}catch(problem){status.textContent="RAG or configuration status unavailable.";error.textContent=problem.message}})();
})();
