"use strict";
(() => {
  const get=id=>document.getElementById(id);
  let connection=null;
  function widgetCode(base){return `<section id="website-widget" aria-label="Website assistant">
  <p>Independent concept demo. Do not enter personal or sensitive information.</p>
  <form><label>Question <input name="question" required maxlength="500"></label>
    <label>Language <select name="language"><option value="en">English</option><option value="sv">Swedish</option></select></label>
    <button type="submit">Ask</button></form>
  <div role="status" aria-live="polite"></div>
</section>
<script>
(() => {
  const root = document.getElementById("website-widget");
  const form = root.querySelector("form"), output = root.querySelector('[role="status"]');
  const conversationId = "widget-" + crypto.randomUUID();
  const apiBase = ${JSON.stringify(base)};
  let visitor = null;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const message = form.elements.question.value.trim();
    if (!message) return;
    const language = form.elements.language.value;
    const button = form.querySelector("button");
    button.disabled = true;
    output.textContent = language === "sv" ? "Laddar…" : "Loading…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90000);
    try {
      if (!visitor || visitor.expires_at * 1000 < Date.now() + 60000) {
        const headers = visitor && visitor.expires_at * 1000 > Date.now()
          ? {Authorization: "Bearer " + visitor.token} : {};
        const session = await fetch(apiBase + "/api/widget/session", {
          method: "POST", credentials: "omit", signal: controller.signal, headers
        });
        if (!session.ok) { visitor = null; throw new Error("Session unavailable"); }
        visitor = await session.json();
      }
      const response = await fetch(apiBase + "/api/widget/chat", {
        method: "POST", credentials: "omit", signal: controller.signal,
        headers: {"Content-Type": "application/json", Authorization: "Bearer " + visitor.token},
        body: JSON.stringify({conversation_id: conversationId, message, language})
      });
      if (!response.ok) { if (response.status === 401) visitor = null; throw new Error("Request failed"); }
      const data = await response.json();
      output.textContent = data.answer;
      const mode = document.createElement("p");
      const labels = language === "sv"
        ? {llm: "LLM-svar", fallback: "Källbaserat reservsvar", deterministic: "Verktygssvar"}
        : {llm: "LLM answer", fallback: "Source-based fallback", deterministic: "Tool response"};
      mode.textContent = labels[data.generation] || "";
      output.append(mode);
      for (const source of data.sources || []) {
        const url = new URL(source.url);
        if (!["https:", "http:"].includes(url.protocol)) continue;
        const link = document.createElement("a");
        link.href = url.href; link.textContent = source.title;
        link.target = "_blank"; link.rel = "noopener noreferrer";
        output.append(link, document.createElement("br"));
      }
    } catch {
      output.textContent = language === "sv"
        ? "Assistenten kunde inte nås. Försök igen."
        : "The assistant could not be reached. Please try again.";
    } finally { clearTimeout(timeout); button.disabled = false; }
  });
})();
</script>`;}
  function render(data){
    connection=data;get("website-base").value=data.settings.api_base_url;
    get("website-origins").value=data.settings.allowed_origins.join("\n");
    get("website-endpoint").value=data.chat_endpoint;
    get("website-code").value=widgetCode(data.effective_api_base_url);
    get("website-warning").textContent=data.warning||"";
    get("website-status").textContent=`Active backend address: ${data.effective_api_base_url} · ${data.settings.allowed_origins.length} additional website origins allowed.`;
    get("website-test").disabled=false;get("website-copy").disabled=false;
    get("website-settings").querySelector("fieldset").disabled=false;
  }
  get("website-settings").addEventListener("submit",async event=>{
    event.preventDefault();error.textContent="";const button=event.currentTarget.querySelector("button");button.disabled=true;
    try{
      await api("website",{method:"PUT",body:JSON.stringify({api_base_url:get("website-base").value.trim(),allowed_origins:get("website-origins").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)})});
      // Reload to apply the CSP connect-src allowlist for the saved test target.
      sessionStorage.setItem("website-settings-saved","true");location.reload();
    }catch(problem){error.textContent=problem.message;button.disabled=false}
  });
  get("website-test").addEventListener("click",async()=>{
    const button=get("website-test"),status=get("website-test-status");button.disabled=true;status.textContent="Testing saved connection…";
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
    try{
      const options={credentials:"omit",signal:controller.signal,redirect:"error"};
      const health=await fetch(connection.health_endpoint,options);
      if(!health.ok||(await health.json()).status!=="healthy")throw Error();
      const session=await fetch(connection.effective_api_base_url+'/api/widget/session',{...options,method:'POST'});
      if(!session.ok)throw Error();
      const visitor=await session.json();
      const probe=await fetch(connection.chat_endpoint,{...options,method:"POST",headers:{"Content-Type":"application/json",Authorization:'Bearer '+visitor.token},body:"{}"});
      if(probe.status!==422||!Array.isArray((await probe.json()).detail))throw Error();
      status.textContent="Connection passed: health and chat validation are reachable from this admin origin. No LLM request was made. Verify the installed widget from its own website origin too.";
    }catch{status.textContent="Connection failed. Check the saved address, HTTPS, network access and the target backend's allowed origins. Unsaved edits are not tested."}
    finally{clearTimeout(timeout);button.disabled=false}
  });
  get("website-copy").addEventListener("click",async()=>{
    try{await navigator.clipboard.writeText(get("website-code").value);get("website-copy-status").textContent="Widget code copied."}
    catch{get("website-code").focus();get("website-code").select();get("website-copy-status").textContent="Select and copy the code manually."}
  });
  (async()=>{try{const identity=await api("session");csrf=identity.csrf_token;render(await api("website"));
    if(sessionStorage.getItem("website-settings-saved")){sessionStorage.removeItem("website-settings-saved");get("website-status").textContent="Website settings saved and active. "+get("website-status").textContent}
  }catch(problem){get("website-status").textContent="Website settings unavailable.";error.textContent=problem.message}})();
})();
