/* Auto Visão Pré-Laudo — service worker.
   O sistema funciona online: páginas, scripts e API sempre vêm da rede.
   Só a identidade visual (logo, ícones, fontes) fica em cache, e há uma tela para quando não houver conexão. */
const CACHE='auto-visao-v1';
const SHELL=['/offline.html','/assets/brand/logo-auto-visao.png','/assets/brand/logo-auto-visao-branco.png','/assets/brand/favicon.svg','/assets/brand/icon-192.png'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  const req=e.request;if(req.method!=='GET')return;
  const url=new URL(req.url);if(url.origin!==location.origin)return;
  if(url.pathname.startsWith('/api/')||url.pathname.startsWith('/comprovantes/'))return;
  if(req.mode==='navigate'){e.respondWith(fetch(req).catch(()=>caches.match('/offline.html')));return}
  if(url.pathname.startsWith('/assets/')){
    e.respondWith(caches.match(req).then(hit=>hit||fetch(req).then(res=>{if(res.ok){const copy=res.clone();caches.open(CACHE).then(c=>c.put(req,copy))}return res})));
  }
});
