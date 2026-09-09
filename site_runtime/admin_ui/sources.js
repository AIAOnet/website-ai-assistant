"use strict";
(() => {
  const byId=id=>document.getElementById(id), panel=byId("sources"), status=byId("sources-status");
  const node=(tag,text)=>{const element=document.createElement(tag);element.textContent=text;return element};
  let records=[],revision="",editing=null,loaded=false,previewToken=null,importToken=null,documents=[],documentRevision="",documentSourceRevision="";
  const previewDialog=byId("sources-preview");document.body.append(previewDialog);
  panel.querySelector(".notice").textContent="Manual saves, reviewed text imports, and approved web refreshes validate the complete snapshot, check contact and ontology references, rebuild the live index, and keep one recovery snapshot. Imported or fetched content is never activated automatically, and ontology facts are never generated.";
  const importBox=node("div","");importBox.className="notice";
  const importLabel=node("label","Import a document for review");importLabel.htmlFor="sources-import-file";
  const importFile=document.createElement("input");importFile.id="sources-import-file";importFile.type="file";importFile.accept=".txt,.md,.pdf,.docx,.xlsx,.csv,text/plain,text/markdown,text/csv,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  const importButton=node("button","Load file into editor");importButton.id="sources-import";importButton.type="button";importButton.className="secondary";
  const importStatus=node("p","TXT/Markdown/CSV: 100 KB. CSV: 1,000 rows and 50 columns. PDF: 5 MB and 100 pages. DOCX: 5 MB and 100 paragraphs. XLSX: 5 MB, 20 worksheets, and 5,000 cells. Spreadsheet formulas are blocked. Maximum 20,000 extracted characters; scanned PDFs need OCR. Import never activates a source.");importStatus.id="sources-import-status";importStatus.setAttribute("role","status");
  importBox.append(importLabel,importFile,importButton,importStatus);byId("sources-content").before(importBox);
  const documentBox=node("section","");documentBox.id="approved-documents";documentBox.className="rag-card";documentBox.append(node("h3","Approved documents"),node("p","Original files are protected and available only to signed-in administrators."));
  const documentResults=node("div","");documentResults.id="approved-document-results";documentBox.append(documentResults);importBox.before(documentBox);
  async function checksum(){
    const bytes=new TextEncoder().encode(byId("sources-content").value.trim());
    const digest=await crypto.subtle.digest("SHA-256",bytes);
    byId("sources-checksum").textContent="Generated checksum: sha256:"+[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,"0")).join("");
  }
  function inputRecord(){return {source_id:byId("sources-id").value,title:byId("sources-title").value,canonical_url:byId("sources-url").value,category:byId("sources-category").value,language:byId("sources-language").value,retrieved_at:byId("sources-retrieved").value,document_version:byId("sources-version").value,content:byId("sources-content").value}}
  function closeEditor(){editing=null;importToken=null;byId("sources-editor").hidden=true;byId("sources-confirm").checked=false}
  function openEditor(record=null){
    editing=record?.source_id||null;byId("sources-editor-title").textContent=record?`Edit ${record.source_id}`:"Add source";
    for(const [id,key] of [["sources-id","source_id"],["sources-title","title"],["sources-url","canonical_url"],["sources-category","category"],["sources-language","language"],["sources-retrieved","retrieved_at"],["sources-version","document_version"],["sources-content","content"]])byId(id).value=record?.[key]||"";
    byId("sources-id").readOnly=Boolean(record);byId("sources-confirm").checked=false;byId("sources-editor").hidden=false;checksum();byId("sources-id").focus();
  }
  async function change(payload,message){
    error.textContent="";panel.querySelectorAll("button").forEach(button=>button.disabled=true);
    try{render(await api("sources/changes",{method:"POST",body:JSON.stringify({...payload,revision,confirmed:true})}));status.textContent=message;closeEditor()}
    catch(problem){error.textContent=problem.message}finally{panel.querySelectorAll("button").forEach(button=>button.disabled=false)}
  }
  function discardPreview(){previewToken=null;byId("sources-preview-confirm").checked=false;byId("sources-preview-apply").disabled=true;if(previewDialog.open)previewDialog.close()}
  async function fetchPreview(record){
    error.textContent="";panel.querySelectorAll("button").forEach(button=>button.disabled=true);status.textContent=`Fetching ${record.source_id} for review…`;
    try{
      const data=await api("sources/refresh-preview",{method:"POST",body:JSON.stringify({revision,source_id:record.source_id})});
      previewToken=data.preview_token;byId("sources-preview-summary").textContent=`${data.changed?"Content changed":"Content is unchanged"} · current ${data.previous_characters} characters · fetched ${data.fetched_characters} characters · preview expires in ${data.expires_in_seconds/60} minutes · ${data.previous_checksum} → ${data.fetched_checksum}`;
      byId("sources-preview-link").href=data.canonical_url;byId("sources-preview-old").textContent=data.previous_content;byId("sources-preview-new").textContent=data.fetched_content;
      byId("sources-preview-confirm").checked=false;byId("sources-preview-apply").disabled=true;previewDialog.showModal();status.textContent=`Fetched ${record.source_id}. Review and explicitly approve before activation.`;
    }catch(problem){status.textContent="Source refresh preview failed; nothing changed.";error.textContent=problem.message}
    finally{panel.querySelectorAll("button").forEach(button=>button.disabled=false)}
  }
  function renderList(){
    const filter=byId("sources-filter").value.toLowerCase();
    const matches=records.filter(record=>[record.source_id,record.title,record.category,record.language,record.source_status].join(" ").toLowerCase().includes(filter));
    byId("sources-results").replaceChildren(...matches.map(record=>{
      const card=node("article","");card.className="rag-card rag-source";
      const link=node("a",record.title);link.href=record.canonical_url;link.target="_blank";link.rel="noopener noreferrer";
      card.append(link,node("small",`${record.source_id} · ${record.language.toUpperCase()} · ${record.category} · ${record.source_status}`),node("small",`Retrieved ${record.retrieved_at} · ${record.checksum}`),node("p",record.content));
      const edit=node("button","Edit");edit.type="button";edit.className="secondary";edit.onclick=()=>openEditor(record);card.append(edit);
      const refresh=node("button","Fetch page preview");refresh.type="button";refresh.className="secondary";refresh.onclick=()=>fetchPreview(record);card.append(refresh);
      const active=record.source_status==="active",toggle=node("button",active?"Archive":"Reactivate");toggle.type="button";toggle.className="secondary";
      toggle.onclick=()=>{const action=active?"archive":"reactivate";if(confirm(`${action[0].toUpperCase()+action.slice(1)} ${record.source_id}? The complete snapshot and its references will be validated before activation.`))change({operation:active?"archive":"activate",source_id:record.source_id,record:null},`${record.source_id} ${active?"archived":"reactivated"}; live index rebuilt.`)};card.append(toggle);return card;
    }));
  }
  function render(data){
    records=data.records;revision=data.revision;status.textContent=`${data.active_records} active of ${records.length} sources · ${data.approved_documents??"Unknown"} approved documents · Index: ${data.index_status} · Recovery: ${data.recovery_snapshot}`;
    renderList();
  }
  function renderDocuments(data){
    documents=data.documents;documentRevision=data.revision;documentSourceRevision=data.source_revision;
    documentResults.replaceChildren(...documents.map(document=>{
      const card=node("article","");card.className="rag-source";card.append(node("strong",document.display_filename),node("small",`${document.source_id} · ${document.media_type} · ${document.byte_size} bytes · ${document.status}`),node("small",`${document.language.toUpperCase()} · ${document.category} · approved ${document.approved_at}`));
      const download=node("a","Download original");download.href=`/api/admin/documents/${document.document_id}/download`;card.append(download);
      if(document.status==="active"){
        const reindex=node("button","Re-index");reindex.type="button";reindex.className="secondary";reindex.onclick=async()=>{if(!confirm(`Re-index the approved source linked to ${document.display_filename}?`))return;try{renderDocuments(await api(`documents/${document.document_id}/reindex`,{method:"POST",body:JSON.stringify({manifest_revision:documentRevision,confirmed:true})}));status.textContent="Approved document source re-indexed."}catch(problem){error.textContent=problem.message}};card.append(reindex);
        const archive=node("button","Archive document and source");archive.type="button";archive.className="secondary";archive.onclick=async()=>{if(!confirm(`Archive ${document.display_filename} and remove its linked source from live retrieval?`))return;try{await api(`documents/${document.document_id}/archive`,{method:"POST",body:JSON.stringify({manifest_revision:documentRevision,source_revision:documentSourceRevision,confirmed:true})});await load();status.textContent="Approved document and linked source archived."}catch(problem){error.textContent=problem.message}};card.append(archive);
      }return card;
    }));
    if(!documents.length)documentResults.replaceChildren(node("p","No approved uploaded documents yet."));
  }
  async function load(){try{const [sourceData,documentData]=await Promise.all([api("sources"),api("documents")]);render(sourceData);renderDocuments(documentData);loaded=true}catch(problem){status.textContent="Sources unavailable.";error.textContent=problem.message}}
  byId("sources-add").onclick=()=>openEditor();byId("sources-cancel").onclick=closeEditor;
  byId("sources-refresh").onclick=load;byId("sources-filter").oninput=()=>loaded&&renderList();
  byId("sources-content").addEventListener("input",()=>checksum());
  importButton.onclick=async()=>{
    const file=importFile.files?.[0];if(!file){importStatus.textContent="Choose a TXT, Markdown, PDF, DOCX, XLSX, or CSV file first.";return}
    const isPdf=/\.pdf$/i.test(file.name),isDocx=/\.docx$/i.test(file.name),isXlsx=/\.xlsx$/i.test(file.name),limit=isPdf||isDocx||isXlsx?5000000:100000;
    if(file.size>limit){importStatus.textContent=`File exceeds the ${isPdf?"5 MB PDF":isDocx?"5 MB DOCX":isXlsx?"5 MB XLSX":"100 KB text"} import limit.`;return}
    importButton.disabled=true;error.textContent="";importStatus.textContent="Extracting document locally for protected review…";
    try{
      const type=file.type||(isPdf?"application/pdf":isDocx?"application/vnd.openxmlformats-officedocument.wordprocessingml.document":isXlsx?"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":/\.csv$/i.test(file.name)?"text/csv":/\.md$/i.test(file.name)?"text/markdown":"text/plain");
      const data=await api("sources/import-preview",{method:"POST",headers:{"Content-Type":type,"X-Website-Filename":encodeURIComponent(file.name)},body:await file.arrayBuffer()});importToken=data.import_token;
      byId("sources-content").value=data.content;if(!byId("sources-title").value)byId("sources-title").value=data.suggested_title;
      if(!byId("sources-version").value)byId("sources-version").value=data.filename;
      if(!byId("sources-retrieved").value)byId("sources-retrieved").value=new Date().toISOString();
      await checksum();byId("sources-confirm").checked=false;importStatus.textContent=`Loaded ${data.filename} for review${data.pages?` · ${data.pages} page${data.pages===1?"":"s"}`:data.paragraphs?` · ${data.paragraphs} paragraph${data.paragraphs===1?"":"s"}`:data.sheets?` · ${data.sheets} worksheet${data.sheets===1?"":"s"} · ${data.cells} cells`:data.rows?` · ${data.rows} rows · ${data.columns} columns · ${data.delimiter} delimiter`:""} · ${data.characters} characters · security scan ${data.malware_scan.replace("_"," ")} · ${data.checksum}. Complete metadata and confirm to activate.`;
    }catch(problem){importStatus.textContent="Import preview failed; the approved source registry is unchanged.";error.textContent=problem.message}
    finally{importButton.disabled=false}
  };
  byId("sources-preview-confirm").onchange=event=>byId("sources-preview-apply").disabled=!event.target.checked;
  byId("sources-preview-cancel").onclick=discardPreview;previewDialog.addEventListener("close",()=>{previewToken=null});
  byId("sources-preview-apply").onclick=async()=>{
    const token=previewToken;if(!token)return;error.textContent="";byId("sources-preview-apply").disabled=true;
    try{render(await api("sources/refresh-apply",{method:"POST",body:JSON.stringify({preview_token:token,confirmed:true})}));discardPreview();status.textContent="Reviewed web refresh approved; live index rebuilt."}
    catch(problem){error.textContent=problem.message;byId("sources-preview-apply").disabled=!byId("sources-preview-confirm").checked}
  };
  byId("sources-editor").addEventListener("submit",event=>{event.preventDefault();change({operation:editing?"update":"add",source_id:editing,record:inputRecord(),import_token:importToken},`${editing||byId("sources-id").value} saved; live index rebuilt.`)});
  document.querySelector('[data-tab="sources"]').addEventListener("click",()=>{if(!loaded)load()});
})();
