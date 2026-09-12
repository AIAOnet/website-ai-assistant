'use strict';
(() => {
 const form=document.querySelector('#homepage-build');
 if(!form)return;
 const status=document.querySelector('#homepage-build-status'),jobs=document.querySelector('#homepage-build-jobs'),report=document.querySelector('#homepage-build-report');
 let timer;
 const problem=error=>{status.textContent=error.message||'Website build unavailable.';};
 async function inspect(id){
  const job=await api('ingestion/'+id);report.replaceChildren();
  const summary=document.createElement('p');summary.textContent=job.homepage+' · '+job.state;report.append(summary);
  const failure=job.error||job.report?.error;
  if(failure){
   const reasons={fetch_failed:'The website request failed. Check server connectivity and retry.',incomplete_response:'The website response ended before all content arrived. Retry the build.',request_timeout:'The website request timed out. Retry the build.',robots_denied:'The website robots policy does not allow this crawl.',response_too_large:'The website response exceeds the configured size limit.',discovery_failed:'The build could not finish. Review the server diagnostics and retry.'};
   const note=document.createElement('p');note.textContent='Build failed: '+(reasons[failure]||failure);report.append(note);
  }
  if(job.report){
   const detail=document.createElement('p'),value=job.report;
   detail.textContent=`${value.extraction?.source_count||0} sources · ${value.extraction?.chunk_count||0} passages · ${value.ontology?.entity_count||0} entities · ${value.activation?.state||(job.state==='failed'?'not published':'staged')}`;report.append(detail);
   if(value.activation?.reason){const note=document.createElement('p');note.textContent=value.activation.reason;report.append(note);}
   for(const page of value.pages){const row=document.createElement('p');row.textContent='Saved: '+page.url;report.append(row);}
   for(const skipped of value.skipped.slice(0,50)){const row=document.createElement('p');row.textContent='Skipped: '+skipped.url+' · '+skipped.reason;report.append(row);}
   if(job.state==='complete'&&document.documentElement.dataset.adminRole!=='viewer'){
    const button=document.createElement('button');button.type='button';button.textContent='Publish this build';
    button.onclick=async()=>{if(!confirm('Replace current website knowledge with this build?'))return;try{await api('ingestion/'+id+'/publish',{method:'POST',body:JSON.stringify({confirmed:true})});await refresh();await inspect(id);}catch(error){problem(error);}};report.append(button);
   }
  }
 }
 async function refresh(){
  clearTimeout(timer);const data=await api('ingestion');jobs.replaceChildren();let running=false;
  status.textContent=data.source_count?`${data.source_count} website sources are available to chat.`:'No website knowledge yet. Add a homepage URL to begin.';
  for(const job of data.jobs){
   const active=['queued','crawling'].includes(job.state);running ||= active||job.activation_pending;
   const row=document.createElement('article');row.className='rag-card';row.textContent=`${job.homepage} · ${job.state} · ${job.progress.page_count} pages`;
   const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='View build report';button.onclick=()=>inspect(job.id).catch(problem);row.append(button);
   if(active&&document.documentElement.dataset.adminRole!=='viewer'){const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Cancel build';cancel.onclick=async()=>{try{await api('ingestion/'+job.id+'/cancel',{method:'POST',body:'{}'});await refresh();}catch(error){problem(error);}};row.append(cancel);}jobs.append(row);
  }
  for(const version of data.versions){const row=document.createElement('p');row.textContent=`${version.homepage} · ${version.source_count} sources · ${version.active?'Active':'Previous build'}`;
   if(!version.active&&document.documentElement.dataset.adminRole!=='viewer'){const restore=document.createElement('button');restore.type='button';restore.className='secondary';restore.textContent='Restore '+version.version.slice(0,8);restore.onclick=async()=>{if(!confirm('Restore this website dataset?'))return;try{await api('ingestion/versions/'+version.version+'/restore',{method:'POST',body:JSON.stringify({confirmed:true})});await refresh();}catch(error){problem(error);}};row.append(restore);}jobs.append(row);}
  form.querySelector('button[type=submit]').disabled=running||document.documentElement.dataset.adminRole==='viewer';
  if(running)timer=setTimeout(()=>refresh().catch(problem),2000);
 }
 form.onsubmit=async event=>{event.preventDefault();try{await api('ingestion',{method:'POST',body:JSON.stringify({homepage:form.homepage.value,limits:{max_pages:Number(form.pages.value),max_depth:Number(form.depth.value)}})});await refresh();}catch(error){problem(error);}};
 document.querySelector('#homepage-build-refresh').onclick=()=>refresh().catch(problem);
 document.querySelector('[data-tab=sources]').addEventListener('click',()=>refresh().catch(problem));
 refresh().catch(problem);
})();
