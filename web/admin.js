const status = document.querySelector('#status');
let csrf = '';
let routingRevision = 0;
let editable = false;
let editingRule = false;
let versionRevision = 0;
async function versionRequest(path='',body) {
  const response=await fetch('/api/admin/versions'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Unable to update knowledge version.');
  return data;
}
async function loadVersions() {
  const data=await versionRequest();versionRevision=data.revision;
  document.querySelector('#version-status').textContent=`${data.active_version?'Active version '+data.active_version.slice(0,8):'No website build is active.'} · Automatic activation ${data.auto_activate?'on':'off'}`;
  const list=document.querySelector('#version-list');list.replaceChildren();
  for(const version of data.versions) {
    const row=document.createElement('article');row.textContent=`${version.id.slice(0,8)} · ${new Date(version.created*1000).toLocaleString()}${version.id===data.active_version?' · Active':''}`;
    if(editable&&version.id!==data.active_version) {
      const button=document.createElement('button');button.type='button';button.textContent='Restore version '+version.id.slice(0,8);
      button.addEventListener('click',async()=>{
        if(!window.confirm('Restore this saved knowledge version for new chat requests?')) return;
        button.disabled=true;
        try {await versionRequest('/'+version.id+'/restore',{revision:versionRevision,confirmed:true});await refresh();}
        catch(error) {button.disabled=false;document.querySelector('#version-status').textContent=error.message;}
      });row.append(button);
    }
    list.append(row);
  }
  document.querySelector('#summary').textContent=`${data.summary.sources} sources · ${data.summary.entities} entities · ${data.summary.relationships} relationships`;
}
document.querySelector('#reload-versions').addEventListener('click',()=>loadVersions().catch(jobError));
async function refresh() {
  const response = await fetch('/api/admin/status');
  if (!response.ok) return false;
  const data = await response.json(); csrf = data.csrf;
  document.querySelector('#login').hidden = true;
  document.querySelector('#overview').hidden = false;
  document.querySelector('#summary').textContent = `${data.source_count} sources · ${data.ontology.entities.length} entities · ${data.ontology.relationships.length} relationships`;
  status.textContent = `Signed in as ${data.username}. ${data.stage}.`;
  document.querySelector('#provider-summary').textContent = data.provider.configured
    ? `${data.provider.model} · ${data.provider.endpoint_host} · Answers checked against source sentences.`
    : 'No answer provider configured. Chat uses website excerpts.';
  document.querySelector('#probe').hidden = data.role === 'viewer' || !data.provider.configured;
  editable = data.role !== 'viewer';
  document.querySelector('#discovery-form').hidden = !editable;
  if (!document.querySelector('#homepage').value) document.querySelector('#homepage').value = data.home_url;
  document.querySelector('#rule-editor').hidden = !editable;
  document.querySelector('#route-preview').hidden = !editable;
  await loadRules();
  await loadJobs();
  return true;
}
document.querySelector('#login').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const response = await fetch('/api/admin/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:document.querySelector('#username').value, password:document.querySelector('#password').value})});
    document.querySelector('#password').value = '';
    if (!response.ok) { status.textContent = response.status === 503 ? 'Administrator access is not configured. Follow the setup instructions.' : 'Sign-in failed. Check your details or try again later.'; return; }
    await refresh();
  } catch { status.textContent = 'Sign-in unavailable. Please try again.'; }
});
document.querySelector('#logout').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/admin/logout', {method:'POST', headers:{'X-CSRF-Token':csrf}});
    if (!response.ok) throw new Error();
    location.reload();
  } catch { status.textContent = 'Sign-out failed. Please try again.'; }
});
document.querySelector('#probe').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  const result = document.querySelector('#probe-result');
  result.textContent = 'Testing connection…';
  try {
    const response = await fetch('/api/admin/provider/probe', {method:'POST', headers:{'X-CSRF-Token':csrf}});
    if (!response.ok) throw new Error();
    const data = await response.json();
    result.textContent = data.available ? 'Connection successful.' : 'Connection could not be verified. Check your configuration.';
  } catch { result.textContent = 'Connection test unavailable. Please try again later.'; }
  finally { button.disabled = false; }
});
refresh().catch(() => { status.textContent = 'Unable to load administrator status.'; });

async function routingRequest(path, body) {
  const response = await fetch('/api/admin/routing' + path, body ? {
    method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body:JSON.stringify(body)
  } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 409 ? 'Rules changed. Reload before saving.' :
    typeof data.detail === 'string' ? data.detail : 'Check the rule fields and try again.');
  return data;
}
function showRoutingError(error) { document.querySelector('#routing-status').textContent = error.message || 'Routing unavailable.'; }
function clearRule() {
  document.querySelector('#rule-editor').reset();
  document.querySelector('#rule-id').readOnly = false;
  document.querySelector('#save-rule').textContent = 'Create rule';
  editingRule = false;
}
async function loadRules() {
  const data = await routingRequest('');
  routingRevision = data.revision;
  const list = document.querySelector('#rule-list'); list.replaceChildren();
  for (const rule of data.rules) {
    const article = document.createElement('article');
    article.textContent = `${rule.name} · ${rule.intent} · ${rule.language} · priority ${rule.priority} · ${rule.enabled ? 'enabled' : 'disabled'}\n${rule.phrases.join(', ')}`;
    if (editable) {
      const edit = document.createElement('button'); edit.textContent = 'Edit'; edit.type = 'button';
      edit.addEventListener('click', () => {
        clearRule(); editingRule = true;
        for (const field of ['id', 'name', 'intent', 'language', 'priority']) document.querySelector('#rule-' + field).value = field === 'id' ? rule.rule_id : rule[field];
        document.querySelector('#rule-phrases').value = rule.phrases.join('\n');
        document.querySelector('#rule-enabled').checked = rule.enabled;
        document.querySelector('#rule-id').readOnly = true;
        document.querySelector('#save-rule').textContent = 'Save rule';
      });
      const remove = document.createElement('button'); remove.textContent = 'Delete'; remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete rule "${rule.name}"?`)) return;
        try { await routingRequest('', {operation:'delete', revision:routingRevision, rule, confirmed:true}); await loadRules(); clearRule(); }
        catch (error) { showRoutingError(error); }
      });
      article.append(edit, remove);
    }
    list.append(article);
  }
  document.querySelector('#routing-status').textContent = `Revision ${data.revision}. ${data.rules.length} rules.`;
}
document.querySelector('#reload-rules').addEventListener('click', () => { clearRule(); loadRules().catch(showRoutingError); });
document.querySelector('#new-rule').addEventListener('click', clearRule);
document.querySelector('#rule-editor').addEventListener('submit', async event => {
  event.preventDefault();
  const rule = {rule_id:document.querySelector('#rule-id').value, name:document.querySelector('#rule-name').value,
    phrases:document.querySelector('#rule-phrases').value.split('\n').map(s => s.trim()).filter(Boolean),
    intent:document.querySelector('#rule-intent').value, language:document.querySelector('#rule-language').value,
    priority:Number(document.querySelector('#rule-priority').value), enabled:document.querySelector('#rule-enabled').checked};
  try { await routingRequest('', {operation:editingRule ? 'update' : 'create', revision:routingRevision, rule, confirmed:document.querySelector('#rule-confirmed').checked}); await loadRules(); clearRule(); }
  catch (error) { showRoutingError(error); }
});
document.querySelector('#route-preview').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const data = await routingRequest('/preview', {phrase:document.querySelector('#preview-phrase').value, language:document.querySelector('#preview-language').value});
    document.querySelector('#routing-status').textContent = `Result: ${data.intent}${data.rule_id ? ' · rule ' + data.rule_id : ''}${data.protected ? ' · protected action rule' : ''}`;
  } catch (error) { showRoutingError(error); }
});

let jobTimer;
async function jobRequest(path = '', body) {
  const response = await fetch('/api/admin/discovery' + path, body !== undefined ? {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify(body)
  } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the URL and limits.');
  return data;
}
function jobError(error) { document.querySelector('#discovery-status').textContent = error.message || 'Discovery unavailable.'; }
async function showJob(id) {
  const job = await jobRequest('/' + id);
  const panel = document.querySelector('#job-report'); panel.replaceChildren();
  const summary = document.createElement('p');
  summary.textContent = `${job.homepage} · ${job.state}${job.report ? ' · coverage: ' + job.report.status : ''}${job.error ? ' · ' + job.error : ''}`;
  panel.append(summary);
  if (job.report) {
    if(job.report.activation) {const result=document.createElement('p');result.textContent=`Build activation: ${job.report.activation.state}${job.report.activation.reason?' · '+job.report.activation.reason:''}. See Knowledge versions for the current active version.`;panel.append(result);}
    if(job.state==='complete'&&editable) {
      const form=document.createElement('form');
      const coverageLabel=document.createElement('label');const coverage=document.createElement('input');coverage.type='checkbox';coverageLabel.append(coverage,document.createTextNode(' Allow reduced source coverage'));
      const siteLabel=document.createElement('label');const site=document.createElement('input');site.type='checkbox';siteLabel.append(site,document.createTextNode(' Replace the current website'));
      const activate=document.createElement('button');activate.textContent='Activate this build';
      form.append(coverageLabel,siteLabel,activate);panel.append(form);
      form.addEventListener('submit',async event=>{
        event.preventDefault();if(!window.confirm('Activate this build with its current review decisions for new chat requests?')) return;
        activate.disabled=true;
        try {await jobRequest('/'+id+'/activate',{revision:versionRevision,confirmed:true,allow_coverage_drop:coverage.checked,replace_site:site.checked});await refresh();}
        catch(error) {activate.disabled=false;jobError(error);}
      });
    }
    if (job.report.ontology) {
      const graph = job.report.ontology;
      const summary = document.createElement('p'); summary.textContent = `Candidate ontology: ${graph.entity_count} entities · ${graph.relationship_count} relationships · ${graph.alias_count} aliases${graph.truncated ? ' · graph limit reached' : ''}`;
      const inspect = document.createElement('button'); inspect.type='button'; inspect.textContent='Inspect ontology';
      inspect.addEventListener('click', () => showOntology(id).catch(jobError));
      panel.append(summary,inspect);
    }
    if (job.report.extraction) {
      const extraction = job.report.extraction;
      const detail = document.createElement('p'); detail.textContent = `Extraction: ${extraction.status} · ${extraction.source_count} sources · ${extraction.chunk_count} passages. Automatically checked; not human-reviewed.`;
      panel.append(detail);
      for (const item of extraction.skipped.slice(0,100)) {
        const line = document.createElement('p'); line.textContent = `Not indexed: ${item.url} · ${item.reason}`; panel.append(line);
      }
    }
    for (const page of job.report.pages) {
      const line = document.createElement('p'); line.textContent = 'Saved: ' + page.url; panel.append(line);
    }
    for (const item of job.report.skipped.slice(0,100)) {
      const line = document.createElement('p'); line.textContent = `Skipped: ${item.url} · ${item.reason}`; panel.append(line);
    }
    if (job.report.skipped.length > 100) {
      const note = document.createElement('p'); note.textContent = 'Showing the first 100 skipped URLs.'; panel.append(note);
    }
  }
}
async function loadJobs() {
  clearTimeout(jobTimer);
  const data = await jobRequest();
  if (!document.querySelector('#homepage').value && data.jobs.length) document.querySelector('#homepage').value = data.jobs[0].homepage;
  const list = document.querySelector('#job-list'); list.replaceChildren();
  let running = false;
  for (const job of data.jobs) {
    const active = ['queued','crawling'].includes(job.state); running ||= active || job.activation_pending;
    const article = document.createElement('article');
    article.textContent = `${job.homepage}\n${job.state}${job.progress.stage ? ' · ' + job.progress.stage : ''}${job.cancel_requested ? ' · cancellation requested' : ''} · ${job.progress.page_count} pages · ${job.progress.requests} requests`;
    const inspect = document.createElement('button'); inspect.type='button'; inspect.textContent='View report';
    inspect.addEventListener('click', () => showJob(job.id).catch(jobError)); article.append(inspect);
    if (active && editable) {
      const cancel = document.createElement('button'); cancel.type='button'; cancel.textContent='Cancel'; cancel.disabled=job.cancel_requested;
      cancel.addEventListener('click', async () => {
        try { await jobRequest('/'+job.id+'/cancel', {}); await loadJobs(); } catch(error) { jobError(error); }
      }); article.append(cancel);
    }
    list.append(article);
  }
  document.querySelector('#start-discovery').disabled = running;
  document.querySelector('#discovery-status').textContent = running ? 'Discovery is running. Progress refreshes automatically.' : data.jobs.length ? 'Saved jobs are available below.' : 'No discovery jobs yet.';
  await loadVersions();
  if (running) jobTimer=setTimeout(() => loadJobs().catch(jobError),2000);
}
document.querySelector('#reload-jobs').addEventListener('click', () => loadJobs().catch(jobError));
document.querySelector('#discovery-form').addEventListener('submit', async event => {
  event.preventDefault(); document.querySelector('#start-discovery').disabled=true;
  try {
    await jobRequest('', {homepage:document.querySelector('#homepage').value,
      limits:{max_pages:Number(document.querySelector('#crawl-pages').value),max_depth:Number(document.querySelector('#crawl-depth').value)}});
    await loadJobs();
  } catch(error) { document.querySelector('#start-discovery').disabled=false; jobError(error); }
});

async function showOntology(id) {
  const graph = await jobRequest('/'+id+'/ontology');
  let review = await jobRequest('/'+id+'/ontology/reviews');
  const panel = document.querySelector('#job-report'); panel.replaceChildren();
  const title = document.createElement('h3'); title.textContent='Candidate ontology';
  const note = document.createElement('p'); note.textContent='Build evidence and review decisions. Review changes affect chat only when this build is activated again; saved versions keep their original decisions.';
  panel.append(title,note);
  if (!graph.entities.length) {
    const empty = document.createElement('p'); empty.textContent='No supported entities were found in this build.'; panel.append(empty); return;
  }
  const entities = new Map(graph.entities.map(entity => [entity.entity_id,entity]));
  const evidence = new Map(graph.evidence.map(item => [item.evidence_id,item]));
  const label = document.createElement('label'); label.textContent='Inspect an entity'; label.htmlFor='ontology-entity';
  const select = document.createElement('select'); select.id='ontology-entity';
  for (const entity of graph.entities) {
    const option=document.createElement('option'); option.value=entity.entity_id;
    option.textContent=`${entity.label} · ${entity.type} · ${evidence.get(entity.evidence_ids[0]).source_url}`;
    select.append(option);
  }
  const detail=document.createElement('div');
  function itemControls(kind,item,row) {
    const itemId=item[kind+'_id'];
    const decision=review.item_reviews.find(value=>value.kind===kind&&value.item_id===itemId);
    const effective=review[kind==='alias'?'effective_alias_ids':'effective_relationship_ids'].includes(itemId);
    const state=document.createElement('p');state.textContent=`Review: ${decision.status} · ${effective?'Included':'Excluded'} from reviewed view`;row.append(state);
    if(!editable) return;
    for(const [action,text] of [['approved','Approve'],['suppressed','Suppress'],['reset','Reset review']]) {
      const button=document.createElement('button');button.type='button';button.textContent=text+' '+kind;
      button.addEventListener('click',async()=>{
        if(!window.confirm(`${text} this ${kind}?`)) return;
        button.disabled=true;
        try {review=await jobRequest('/'+id+'/ontology/item-reviews',{kind,item_id:itemId,revision:decision.revision,decision:action,confirmed:true});render();}
        catch(error) {button.disabled=false;jobError(error);}
      });row.append(button);
    }
  }
  function render() {
    detail.replaceChildren();
    const entity=entities.get(select.value);
    const decision=review.reviews.find(item=>item.entity_id===entity.entity_id);
    const state=document.createElement('p');state.textContent=`Review: ${decision.status} · Display name: ${decision.display_label}`;detail.append(state);
    if (decision.status==='needs_review') {const warning=document.createElement('p');warning.textContent='Source evidence changed. The saved decision is retained but is not applied until reviewed again.';detail.append(warning);}
    if(editable) {
      const nameLabel=document.createElement('label');nameLabel.textContent='Source-backed display name';
      const name=document.createElement('select');
      for(const text of new Set([entity.label,...graph.aliases.filter(a=>a.entity_id===entity.entity_id).map(a=>a.label)])) {
        const option=document.createElement('option');option.value=text;option.textContent=text;name.append(option);
      }
      name.value=decision.display_label;nameLabel.append(name);detail.append(nameLabel);
      for(const [action,text] of [['approved','Approve / save name'],['suppressed','Suppress entity'],['reset','Reset review']]) {
        const button=document.createElement('button');button.type='button';button.textContent=text;
        button.addEventListener('click',async()=>{
          if(!window.confirm(`${text}: ${entity.label}?`)) return;
          button.disabled=true;
          try {review=await jobRequest('/'+id+'/ontology/reviews',{entity_id:entity.entity_id,revision:decision.revision,decision:action,display_label:action==='approved'?name.value:null,confirmed:true});render();}
          catch(error) {button.disabled=false;jobError(error);}
        });detail.append(button);
      }
    }
    const description=document.createElement('p'); description.textContent=`${entity.label} · ${entity.type} · ${entity.language} · ${entity.review_status}`; detail.append(description);
    const aliases=graph.aliases.filter(alias=>alias.entity_id===entity.entity_id);
    for(const alias of aliases) {const row=document.createElement('div');row.textContent='Alias: '+alias.label;itemControls('alias',alias,row);detail.append(row);}
    const proofs=new Set(entity.evidence_ids);
    for(const edge of graph.relationships.filter(edge=>edge.subject_id===entity.entity_id||edge.object_id===entity.entity_id)) {
      const row=document.createElement('div');row.textContent=`${entities.get(edge.subject_id).label} — ${edge.predicate} — ${entities.get(edge.object_id)?.label || evidence.get(edge.evidence_ids[0]).source_url}`;
      itemControls('relationship',edge,row);
      detail.append(row);edge.evidence_ids.forEach(value=>proofs.add(value));
    }
    for(const identifier of proofs) {
      const proof=evidence.get(identifier);const quote=document.createElement('blockquote');quote.textContent=proof.quote;
      const source=document.createElement('a');const url=new URL(proof.source_url);
      if(['https:','http:'].includes(url.protocol)) source.href=url.href;
      source.textContent=proof.source_url;source.rel='noopener noreferrer';detail.append(quote,source);
    }
  }
  select.addEventListener('change',render);panel.append(label,select,detail);render();
}
