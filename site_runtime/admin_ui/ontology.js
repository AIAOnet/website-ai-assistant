"use strict";
(() => {
  const byId=id=>document.getElementById(id);
  const node=(tag,text)=>{const item=document.createElement(tag);item.textContent=text;return item};
  let report=null, operation="add", selectedRow=null, editing=false, busy=false;

  function sourceLink(source){
    const link=node("a",source.source_id+" · "+source.title),url=new URL(source.canonical_url);
    if(["https:","http:"].includes(url.protocol)){
      link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";return link;
    }
    return null;
  }
  function controls(){
    byId("ontology-add").disabled=!report||editing||busy;
    byId("ontology-refresh").disabled=editing||busy;
    byId("ontology-editor").querySelector("fieldset").disabled=busy;
    byId("ontology-results").querySelectorAll("button").forEach(button=>button.disabled=editing||busy);
  }
  function showEvidence(){
    const source=report?.sources.find(item=>item.source_id===byId("ontology-source").value);
    const evidence=byId("ontology-evidence");evidence.replaceChildren();
    if(source){
      const link=sourceLink(source);if(link)evidence.append(link,document.createElement("br"));
      evidence.append(node("small",source.content));
    }
  }
  function closeEditor(){
    byId("ontology-editor").hidden=true;selectedRow=null;operation="add";editing=false;
    byId("ontology-confirm").checked=false;controls();
  }
  function openEditor(action,row=null){
    if(!report||busy||editing)return;
    operation=action;selectedRow=row;editing=true;error.textContent="";
    const deleting=action==="delete",values=row||{};
    byId("ontology-editor-title").textContent=deleting?"Remove relationship #"+row.row:action==="update"?"Edit relationship #"+row.row:"Add relationship";
    byId("ontology-editor-note").textContent=deleting?(row.subject||"Invalid record")+" → "+(row.predicate||"?")+" → "+(row.object||"?")+". Confirming removes this row and keeps one previous snapshot.":"Choose an approved evidence source and compare its text before confirming. Use an active contact ID for RESPONSIBLE_CONTACT, or an active source ID for DOCUMENTED_BY.";
    for(const [id,key] of [["ontology-subject","subject"],["ontology-object","object"]]){
      const input=byId(id);input.value=values[key]||"";input.disabled=deleting;input.required=!deleting;
    }
    for(const [id,key] of [["ontology-predicate","predicate"],["ontology-source","source_id"]]){
      const select=byId(id);select.value=values[key]||select.options[0]?.value||"";select.disabled=deleting;select.required=!deleting;
    }
    byId("ontology-save").textContent=deleting?"Remove relationship":"Save relationship";
    byId("ontology-confirm").checked=false;byId("ontology-editor").hidden=false;showEvidence();controls();
    (deleting?byId("ontology-confirm"):byId("ontology-subject")).focus();
  }
  function render(){
    if(!report)return;
    const query=byId("ontology-filter").value.toLocaleLowerCase();
    const rows=report.relationships.filter(r=>[r.subject,r.predicate,r.object,r.source_id,...r.issues].join(" ").toLocaleLowerCase().includes(query));
    byId("ontology-entities").textContent=report.entities.filter(e=>e.label.toLocaleLowerCase().includes(query)).map(e=>e.label+" (rows "+e.relationship_rows.join(", ")+")").join(" · ")||"No matching entities.";
    byId("ontology-results").replaceChildren(...rows.map(row=>{
      const card=node("details","");card.className="rag-source";
      card.append(node("summary","#"+row.row+" · "+(row.subject||"Invalid record")+" → "+(row.predicate||"?")+" → "+(row.object||"?")),
        node("p",row.issues.length?row.issues.join(" "):"Structure and references pass. Claim still requires human evidence review."));
      if(row.source){const link=sourceLink(row.source);if(link)card.append(link);card.append(node("small","Retrieved: "+row.source.retrieved_at),node("p",row.source.content))}
      const actions=node("div","");actions.className="ontology-actions";
      const edit=node("button","Edit"),remove=node("button","Remove");
      edit.type=remove.type="button";remove.className="secondary";
      edit.addEventListener("click",()=>openEditor("update",row));remove.addEventListener("click",()=>openEditor("delete",row));
      actions.append(edit,remove);card.append(actions);return card;
    }));
    if(!rows.length)byId("ontology-results").append(node("p","No matching relationships."));
    controls();
  }
  function applyReport(data,message=""){
    report=data;
    const invalid=report.relationships.filter(r=>r.issues.length).length;
    byId("ontology-status").textContent=(message?message+" · ":"")+"Version "+(report.version||"unknown")+" · "+report.entities.length+" entities · "+report.relationships.length+" relationships · "+report.aliases.length+" aliases · "+invalid+" relationship rows with issues";
    byId("ontology-warning").textContent=report.issues.join(" ");
    byId("ontology-predicate").replaceChildren(...report.predicates.map(value=>new Option(value,value)));
    byId("ontology-source").replaceChildren(...report.sources.map(value=>new Option(value.source_id+" · "+value.title,value.source_id)));
    render();
  }
  async function refresh(){
    if(editing||busy)return;
    busy=true;controls();byId("ontology-status").textContent="Checking loaded ontology…";
    try{applyReport(await api("ontology"))}
    catch(problem){report=null;byId("ontology-results").replaceChildren();byId("ontology-entities").textContent="";byId("ontology-status").textContent=problem.message}
    finally{busy=false;controls()}
  }
  byId("ontology-editor").addEventListener("submit",async event=>{
    event.preventDefault();if(!report||busy)return;
    const payload={revision:report.revision,operation,row:selectedRow?.row??null,confirmed:byId("ontology-confirm").checked,
      relationship:operation==="delete"?null:{subject:byId("ontology-subject").value,predicate:byId("ontology-predicate").value,object:byId("ontology-object").value,source_id:byId("ontology-source").value}};
    busy=true;controls();error.textContent="";
    try{applyReport(await api("ontology/relationships",{method:"POST",body:JSON.stringify(payload)}),operation==="delete"?"Relationship removed; previous snapshot saved":"Relationship saved; previous snapshot saved");closeEditor();document.dispatchEvent(new CustomEvent("ontology-updated"))}
    catch(problem){error.textContent=problem.message}
    finally{busy=false;controls()}
  });
  byId("ontology-add").addEventListener("click",()=>openEditor("add"));
  byId("ontology-cancel").addEventListener("click",()=>{closeEditor();byId("ontology-add").focus()});
  byId("ontology-source").addEventListener("change",()=>{byId("ontology-confirm").checked=false;showEvidence()});
  byId("ontology-editor").addEventListener("input",event=>{if(event.target.id!=="ontology-confirm")byId("ontology-confirm").checked=false});
  byId("ontology-refresh").addEventListener("click",refresh);
  byId("ontology-filter").addEventListener("input",render);
  document.querySelector('[data-tab="ontology"]').addEventListener("click",refresh);
  controls();
})();
