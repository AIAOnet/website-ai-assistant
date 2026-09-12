"use strict";
const error=document.querySelector("#error");
let csrf="";
async function api(path,options={}){
  const response=await fetch("/api/admin/"+path,{...options,headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,...options.headers}});
  if(response.status===401&&path!=="login"){location.replace("/admin/login");throw Error("Session expired. Please sign in again.")}
  let data;
  try { data=await response.json(); }
  catch { throw Error("The server could not complete the request. Please try again or check diagnostics."); }
  if(!response.ok)throw Error(typeof data.detail==="string"?data.detail:"The request could not be completed.");
  return data;
}
const login=document.querySelector("#login-form");
if(login){login.addEventListener("submit",async event=>{event.preventDefault();const button=login.querySelector("button");button.disabled=true;error.textContent="";try{await api("login",{method:"POST",body:JSON.stringify({username:login.username.value,password:login.password.value})});login.password.value="";location.replace("/admin")}catch(problem){error.textContent=problem.message;login.password.value=""}finally{button.disabled=false}})}
else{
  async function session(){try{const identity=await api("session");csrf=identity.csrf_token;document.documentElement.dataset.adminRole=identity.role;const label=identity.role==="administrator"?"Administrator":identity.role[0].toUpperCase()+identity.role.slice(1);document.querySelector("#identity").textContent=identity.username+" · "+label;const access=document.querySelector("#access-level");if(access){access.textContent=identity.permissions.write?"You can review and edit administration settings.":"Read-only access: changes and operational actions are disabled by the server.";access.dataset.readonly=String(!identity.permissions.write)}const audit=document.querySelector("#security-audit");if(audit)audit.hidden=identity.role!=="administrator";if(!identity.permissions.write)document.querySelectorAll(".panel form input,.panel form select,.panel form textarea,.panel form button").forEach(control=>control.disabled=true)}catch(problem){error.textContent=problem.message}}
  session();
  setInterval(session,60000);
  window.addEventListener("pageshow",()=>session());
  document.querySelector("#logout").addEventListener("click",async()=>{try{await api("logout",{method:"POST"});location.replace("/admin/login")}catch(problem){error.textContent=problem.message}});
  const tabs=[...document.querySelectorAll("[data-tab]")],tablist=document.querySelector("nav[aria-label='Admin sections']");tablist.setAttribute("role","tablist");
  tabs.forEach((button,index)=>{const panel=document.getElementById(button.dataset.tab);button.id="tab-"+button.dataset.tab;button.setAttribute("role","tab");button.setAttribute("aria-controls",panel.id);button.setAttribute("aria-selected",String(index===0));button.tabIndex=index? -1:0;panel.setAttribute("role","tabpanel");panel.setAttribute("aria-labelledby",button.id);button.addEventListener("click",()=>{tabs.forEach(item=>{const active=item===button;item.setAttribute("aria-pressed",String(active));item.setAttribute("aria-selected",String(active));item.tabIndex=active?0:-1});document.querySelectorAll(".panel").forEach(item=>item.hidden=item.id!==button.dataset.tab)});button.addEventListener("keydown",event=>{let target;if(event.key==="ArrowRight")target=tabs[(index+1)%tabs.length];else if(event.key==="ArrowLeft")target=tabs[(index-1+tabs.length)%tabs.length];else if(event.key==="Home")target=tabs[0];else if(event.key==="End")target=tabs[tabs.length-1];if(target){event.preventDefault();target.focus();target.click()}})});
}

// Independent section navigation within the integration and AI settings tabs.
(() => {
  const storageKey = 'admin-settings-location';
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}'); } catch {}
  document.querySelectorAll('.settings-menu').forEach(menu => {
    const buttons = [...menu.querySelectorAll('[data-section]')];
    const parent = menu.closest('.panel');
    const activate = button => {
      buttons.forEach(item => {
        const active = item === button;
        item.setAttribute('aria-selected', String(active));
        item.tabIndex = active ? 0 : -1;
        document.getElementById(item.dataset.section).hidden = !active;
      });
      saved[parent.id] = button.dataset.section;
      try { sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch {}
    };
    buttons.forEach((button, index) => {
      const section = document.getElementById(button.dataset.section);
      section.setAttribute('role', 'tabpanel');
      section.setAttribute('aria-labelledby', button.id);
      button.addEventListener('click', () => activate(button));
      button.addEventListener('keydown', event => {
        let target;
        if (['ArrowDown', 'ArrowRight'].includes(event.key)) target = buttons[(index + 1) % buttons.length];
        if (['ArrowUp', 'ArrowLeft'].includes(event.key)) target = buttons[(index - 1 + buttons.length) % buttons.length];
        if (event.key === 'Home') target = buttons[0];
        if (event.key === 'End') target = buttons[buttons.length - 1];
        if (target) { event.preventDefault(); target.focus(); activate(target); }
      });
    });
    activate(buttons.find(button => button.dataset.section === saved[parent.id]) || buttons[0]);
  });
  document.querySelectorAll('[data-status-source]').forEach(summary => {
    const source = document.getElementById(summary.dataset.statusSource);
    const update = () => { summary.textContent = source.textContent; };
    new MutationObserver(update).observe(source, {childList: true, characterData: true, subtree: true});
    update();
  });
})();
