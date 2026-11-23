/* SPDX-License-Identifier: Apache-2.0 · Copyright 2026 Borislav Vladimirov Petrakiev */
/* KaBor Quant 7.4 — idiomas.
   El panel y el login están escritos en español. Este archivo traduce lo que se ve en pantalla al idioma elegido,
   sin tocar el resto del código: vigila la página y cambia cada texto que esté en el diccionario
   (/i18n/<idioma>.json, con el texto en español como clave). Las frases con números o nombres llevan huecos {0}, {1}…
   Lo que no está en el diccionario se queda en español. También traduce el texto de las gráficas (canvas).
   El idioma se guarda en este navegador (localStorage «kq_lang»). */
(function () {
  "use strict";
  const LANGS = {es: "Español", en: "English", fr: "Français"};
  const LOCALES = {es: "es-ES", en: "en-GB", fr: "fr-FR"};
  let lang = "es";
  try { lang = localStorage.getItem("kq_lang") || ""; } catch (e) { lang = ""; }
  if (!LANGS[lang]) { const nav = (navigator.language || "es").slice(0, 2).toLowerCase(); lang = LANGS[nav] ? nav : "es"; }
  const exact = new Map(), first = new Map(), last = new Map(), misc = [];
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "PRE", "NOSCRIPT"]);
  const ATTRS = ["placeholder", "title", "aria-label", "alt"];
  const norm = s => s.replace(/\s+/g, " ").trim();
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  let ready = lang === "es";

  function compile(dict) {
    for (const [k, v] of Object.entries(dict)) {
      if (!/\{\d+\}/.test(k)) { exact.set(k, v); continue; }
      const order = [];
      // un hueco pegado a una palabra (plural: «línea{1}», «bot{1}») puede quedar vacío; los demás no
      const re = new RegExp("^" + k.split(/(\{\d+\})/).map((p, i, arr) => { const m = /^\{(\d+)\}$/.exec(p); if (m) { order.push(+m[1]); return /[A-Za-zÁÉÍÓÚáéíóúñÑ]$/.test(arr[i - 1] || "") ? "(.*?)" : "(.+?)"; } return esc(p); }).join("") + "$", "s");
      // «débil»: casi todo son huecos («{0} de {1}»); solo vale si los huecos son números o símbolos
      const weak = k.replace(/\{\d+\}/g, "").replace(/[^A-Za-zÁÉÍÓÚáéíóúñÑ]/g, "").length < 5;
      const pat = {re, order, out: v, weak, lit: k.replace(/\{\d+\}/g, "").length};
      const pre = k.split("{")[0], suf = k.slice(k.lastIndexOf("}") + 1);
      if (pre.length >= 3) { const b = pre.slice(0, 3).toLowerCase(); (first.get(b) || first.set(b, []).get(b)).push(pat); }
      else if (suf.length >= 3) { const b = suf.slice(-3).toLowerCase(); (last.get(b) || last.set(b, []).get(b)).push(pat); }
      else misc.push(pat);
    }
  }
  const WORDY = /[A-Za-zÁÉÍÓÚáéíóúñÑ]{3,}/;
  function trCore(core, depth = 0) {
    const e = exact.get(core);
    if (e !== undefined) return e;
    if (!/[A-Za-zÁÉÍÓÚáéíóúñÑ]{2}/.test(core)) return null;
    const cands = (first.get(core.slice(0, 3).toLowerCase()) || []).concat(last.get(core.slice(-3).toLowerCase()) || [], misc);
    cands.sort((x, y) => y.lit - x.lit);   // primero la regla más específica (más texto fijo)
    for (const p of cands) {
      const m = p.re.exec(core);
      if (!m) continue;
      const vals = {}; let ok = true;
      p.order.forEach((n, i) => { const g = m[i + 1], gt = g.trim();
        const t = depth < 2 && gt ? trCore(gt, depth + 1) : null;
        if (t === null && p.weak && WORDY.test(g)) ok = false;   // patrón casi vacío con texto que no es nuestro: no tocar
        if (/[.!?:]\s+[«¿¡A-ZÁÉÍÓÚ]/.test(gt)) ok = false;   // el hueco se ha tragado otra frase: mejor frase a frase
        vals[n] = t !== null ? g.replace(gt, t) : g; });
      if (!ok) continue;
      return p.out.replace(/\{(\d+)\}/g, (_, n) => vals[n] !== undefined ? vals[n] : "");
    }
    if (depth < 2) {
      // frases pegadas en un mismo texto: se traduce cada una por separado
      const parts = core.split(/(?<=[.!?:])\s+(?=[«¿¡A-ZÁÉÍÓÚ0-9])/);
      if (parts.length > 1) {
        let hit = false;
        const out = parts.map(x => { const t = trCore(x, depth + 1); if (t !== null) { hit = true; return t; } return x; });
        if (hit) return out.join(" ");
      }
      if (/[.:]$/.test(core)) { const t = trCore(core.slice(0, -1), depth + 1); if (t !== null) return t + core.slice(-1); }
    }
    return null;
  }
  function tr(s) {
    if (lang === "es" || !ready || s == null) return s;
    s = String(s);
    const core = norm(s);
    if (!core) return s;
    const t = trCore(core);
    if (t === null) return s;
    const lead = /^\s*/.exec(s)[0], trail = /\s*$/.exec(s)[0];
    return (lead ? " " : "") + t + (trail ? " " : "");
  }
  const done = new WeakMap();
  function trText(n) {
    const p = n.parentNode;
    if (!p || SKIP.has(p.nodeName) || (p.closest && p.closest("[data-noi18n],[translate=no]"))) return;
    const v = n.nodeValue;
    if (done.get(n) === v) return;
    const t = tr(v);
    if (t !== v) n.nodeValue = t;
    done.set(n, n.nodeValue);
  }
  function trAttrs(el) {
    if (!el.getAttribute || (el.closest && el.closest("[data-noi18n],[translate=no]"))) return;
    for (const a of ATTRS) { const v = el.getAttribute(a); if (v) { const t = tr(v); if (t !== v) el.setAttribute(a, t); } }
  }
  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { trText(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    if (root.nodeType === 1) { if (SKIP.has(root.nodeName)) return; trAttrs(root); }
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = w.nextNode())) { if (n.nodeType === 3) trText(n); else trAttrs(n); }
  }
  function start() {
    walk(document.documentElement);
    document.title = tr(document.title);
    new MutationObserver(ms => {
      for (const m of ms) {
        if (m.type === "characterData") trText(m.target);
        else if (m.type === "attributes") trAttrs(m.target);
        else m.addedNodes.forEach(walk);
      }
    }).observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS});
    document.documentElement.classList.remove("i18n-espera");
  }
  // gráficas: el texto que se dibuja en un canvas también pasa por el diccionario
  const C = window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
  if (C && lang !== "es") {
    const ft = C.fillText, mt = C.measureText, st = C.strokeText;
    C.fillText = function (t, ...a) { return ft.call(this, tr(t), ...a); };
    C.strokeText = function (t, ...a) { return st.call(this, tr(t), ...a); };
    C.measureText = function (t) { return mt.call(this, tr(t)); };
  }
  document.documentElement.lang = lang;
  if (lang !== "es") {
    document.documentElement.classList.add("i18n-espera");
    setTimeout(() => document.documentElement.classList.remove("i18n-espera"), 1500);
    fetch("/i18n/" + lang + ".json").then(r => r.ok ? r.json() : {}).catch(() => ({})).then(d => {
      compile(d); ready = true;
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
    });
  }
  window.KQI18N = {
    langs: LANGS, get lang() { return lang; }, locale: () => LOCALES[lang] || "es-ES", tr,
    set(l) { if (!LANGS[l]) return; try { localStorage.setItem("kq_lang", l); } catch (e) {} location.reload(); }
  };
})();
