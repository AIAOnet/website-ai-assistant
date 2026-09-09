"use strict";
(() => {
  const byId=id=>document.getElementById(id);
  const node=(tag,text)=>{const element=document.createElement(tag);element.textContent=text;return element};
  let data=null, action="add", selected=null, editing=false, busy=false;

  function controls(){
    byId("ontology-alias-add").disabled=!data||editing||busy;
    byId("ontology-alias-editor").querySelector("fieldset").disabled=busy;
    byId("ontology-alias-results").querySelectorAll("button").forEach(button=>button.disabled=editing||busy);
  }
  function sourceLink(source){
    const url=new URL(source.canonical_url),link=node("a",source.source_id+" · "+source.title);
    if(!["https:","http:"].includes(url.protocol))return null;
    link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";return link;
  }
  function populateSources(preferred=""){
    const language=byId("ontology-alias-language").value;
    const choices=data.sources.filter(source=>source.language===language);
    byId("ontology-alias-source").replaceChildren(...choices.map(source=>new Option(source.source_id+" · "+source.title,source.source_id)));
    if(choices.some(source=>source.source_id===preferred))byId("ontology-alias-source").value=preferred;
    showEvidence();
  }
  function showEvidence(){
    const source=data?.sources.find(item=>item.source_id===byId("ontology-alias-source").value);
    const target=byId("ontology-alias-evidence");target.replaceChildren();
    if(source){const link=sourceLink(source);if(link)target.append(link,document.createElement("br"));target.append(node("small",source.content))}
  }
  function close(){
    editing=false;selected=null;action="add";byId("ontology-alias-editor").hidden=true;
    byId("ontology-alias-confirm").checked=false;controls();
  }
  function open(operation,row=null){
    if(!data||editing||busy)return;
    editing=true;action=operation;selected=row;error.textContent="";
    const deleting=operation==="delete";
    byId("ontology-alias-title").textContent=deleting?"Remove alias #"+row.row:operation==="update"?"Edit alias #"+row.row:"Add alias";
    byId("ontology-alias-note").textContent=deleting?`${row.alias} → ${row.entity}. Confirming removes this alias and keeps one previous snapshot.`:"The alias is only a retrieval label, not factual answer evidence. Compare it with the approved source before confirming.";
    byId("ontology-alias-entity").value=row?.entity||byId("ontology-alias-entity").options[0]?.value||"";
    byId("ontology-alias-value").value=row?.alias||"";
    byId("ontology-alias-language").value=row?.language||"en";
    for(const id of ["ontology-alias-entity","ontology-alias-value","ontology-alias-language","ontology-alias-source"]){byId(id).disabled=deleting}
    populateSources(row?.source_id||"");
    byId("ontology-alias-save").textContent=deleting?"Remove alias":"Save alias";
    byId("ontology-alias-confirm").checked=false;byId("ontology-alias-editor").hidden=false;controls();
    (deleting?byId("ontology-alias-confirm"):byId("ontology-alias-entity")).focus();
  }
  function render(){
    if(!data)return;
    const filter=byId("ontology-filter").value.toLocaleLowerCase();
    const aliases=data.aliases.filter(item=>[item.entity,item.alias,item.language,item.source_id,...item.issues].join(" ").toLocaleLowerCase().includes(filter));
    byId("ontology-alias-status").textContent=`${aliases.length} of ${data.aliases.length} aliases`;
    byId("ontology-alias-results").replaceChildren(...aliases.map(item=>{
      const card=node("details","");card.className="rag-source";
      card.append(node("summary",`#${item.row} · ${item.alias||"Invalid alias"} → ${item.entity||"?"} · ${(item.language||"?").toUpperCase()}`),
                  node("p",item.issues.length?item.issues.join(" "):"Structure, language and source reference pass. Meaning still requires human evidence review."));
      if(item.source){const link=sourceLink(item.source);if(link)card.append(link);card.append(node("p",item.source.content))}
      const actions=node("div","");actions.className="ontology-actions";
      const edit=node("button","Edit"),remove=node("button","Remove");edit.type=remove.type="button";remove.className="secondary";
      edit.addEventListener("click",()=>open("update",item));remove.addEventListener("click",()=>open("delete",item));actions.append(edit,remove);card.append(actions);return card;
    }));
    if(!aliases.length)byId("ontology-alias-results").append(node("p","No matching aliases."));controls();
  }
  async function refresh(){
    if(editing||busy)return;busy=true;controls();
    try{
      data=await api("ontology");
      byId("ontology-alias-entity").replaceChildren(...data.entities.map(entity=>new Option(entity.label,entity.label)));
      populateSources();render();
    }catch(problem){data=null;byId("ontology-alias-status").textContent=problem.message;byId("ontology-alias-results").replaceChildren()}
    finally{busy=false;controls()}
  }
  byId("ontology-alias-editor").addEventListener("submit",async event=>{
    event.preventDefault();if(!data||busy)return;
    const payload={revision:data.revision,operation:action,row:selected?.row??null,confirmed:byId("ontology-alias-confirm").checked,
      alias:action==="delete"?null:{entity:byId("ontology-alias-entity").value,alias:byId("ontology-alias-value").value,language:byId("ontology-alias-language").value,source_id:byId("ontology-alias-source").value}};
    busy=true;controls();error.textContent="";
    try{const message=action==="delete"?"Alias removed":"Alias saved";data=await api("ontology/aliases",{method:"POST",body:JSON.stringify(payload)});close();render();byId("ontology-refresh").click();byId("ontology-alias-status").textContent=message+"; previous snapshot saved."}
    catch(problem){error.textContent=problem.message}finally{busy=false;controls()}
  });
  byId("ontology-alias-add").addEventListener("click",()=>open("add"));
  byId("ontology-alias-cancel").addEventListener("click",()=>{close();byId("ontology-alias-add").focus()});
  byId("ontology-alias-language").addEventListener("change",()=>{byId("ontology-alias-confirm").checked=false;populateSources()});
  byId("ontology-alias-source").addEventListener("change",()=>{byId("ontology-alias-confirm").checked=false;showEvidence()});
  byId("ontology-alias-editor").addEventListener("input",event=>{if(event.target.id!=="ontology-alias-confirm")byId("ontology-alias-confirm").checked=false});
  byId("ontology-filter").addEventListener("input",render);
  document.querySelector('[data-tab="ontology"]').addEventListener("click",refresh);
  document.addEventListener("ontology-updated",refresh);
  controls();
})();
