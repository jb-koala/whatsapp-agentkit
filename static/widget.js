// widget.js — Widget de chat embebible para Koala-OS
// Generado por AgentKit
//
// Uso en cualquier página web:
//   <script src="https://TU-SERVIDOR/static/widget.js" async></script>
//
// El widget detecta automáticamente el dominio donde está hosteado y
// dispara los requests a /chat sobre ese mismo origen.

(function () {
  "use strict";

  // ─── Configuración ─────────────────────────────────────────────
  const currentScript = document.currentScript || (function () {
    const scripts = document.getElementsByTagName("script");
    return scripts[scripts.length - 1];
  })();

  const SCRIPT_SRC = currentScript ? currentScript.src : "";
  const API_BASE = SCRIPT_SRC.replace(/\/static\/widget\.js.*$/, "");
  const SESSION_KEY = "koala-os-session-id";
  const HISTORY_KEY = "koala-os-history";

  // Etiqueta y mensajes — vienen del data-* attribute o defaults
  const WIDGET_TITLE = currentScript?.dataset?.title || "Koala-OS";
  const WIDGET_SUBTITLE = currentScript?.dataset?.subtitle || "Tu asistente Koala";
  const WIDGET_GREETING = currentScript?.dataset?.greeting ||
    "¡Hola! ¿En qué te puedo ayudar?";
  const WIDGET_COLOR = currentScript?.dataset?.color || "#2c5530";

  // ─── Identificador de sesión persistente (localStorage) ────────
  function getSessionId() {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) {
      id = (crypto.randomUUID && crypto.randomUUID()) ||
        ("web-" + Math.random().toString(36).slice(2) + Date.now().toString(36));
      localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  }

  function getHistory() {
    try {
      return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    } catch (_) {
      return [];
    }
  }

  function saveHistory(history) {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(-50)));
    } catch (_) { /* localStorage lleno o deshabilitado */ }
  }

  function resetSession() {
    localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(HISTORY_KEY);
  }

  // ─── Estilos inline ────────────────────────────────────────────
  const styles = `
    .koala-widget-root, .koala-widget-root * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .koala-widget-bubble {
      position: fixed; bottom: 24px; right: 24px;
      width: 60px; height: 60px; border-radius: 50%;
      background: ${WIDGET_COLOR}; color: #fff;
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; box-shadow: 0 4px 14px rgba(0,0,0,0.2);
      z-index: 999999; transition: transform 0.2s;
    }
    .koala-widget-bubble:hover { transform: scale(1.08); }
    .koala-widget-bubble svg { width: 28px; height: 28px; fill: #fff; }

    .koala-widget-panel {
      position: fixed; bottom: 100px; right: 24px;
      width: 360px; max-width: calc(100vw - 32px);
      height: 540px; max-height: calc(100vh - 130px);
      background: #fff; border-radius: 12px;
      box-shadow: 0 10px 40px rgba(0,0,0,0.15);
      display: none; flex-direction: column;
      z-index: 999999; overflow: hidden;
    }
    .koala-widget-panel.open { display: flex; }

    .koala-widget-header {
      background: ${WIDGET_COLOR}; color: #fff;
      padding: 16px; display: flex; justify-content: space-between; align-items: center;
    }
    .koala-widget-header h3 { margin: 0; font-size: 16px; font-weight: 600; }
    .koala-widget-header p { margin: 2px 0 0; font-size: 12px; opacity: 0.85; }
    .koala-widget-header button {
      background: transparent; border: none; color: #fff; font-size: 22px;
      cursor: pointer; line-height: 1; padding: 0 4px;
    }

    .koala-widget-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      background: #fafaf8; display: flex; flex-direction: column; gap: 8px;
    }
    .koala-msg { padding: 10px 14px; border-radius: 14px; max-width: 80%; font-size: 14px; line-height: 1.4; word-wrap: break-word; white-space: pre-wrap; }
    .koala-msg-user { align-self: flex-end; background: ${WIDGET_COLOR}; color: #fff; border-bottom-right-radius: 4px; }
    .koala-msg-bot { align-self: flex-start; background: #fff; color: #222; border-bottom-left-radius: 4px; border: 1px solid #eee; }
    .koala-msg-typing { font-style: italic; opacity: 0.6; }

    .koala-widget-input {
      border-top: 1px solid #eee; padding: 12px;
      display: flex; gap: 8px; background: #fff;
    }
    .koala-widget-input textarea {
      flex: 1; border: 1px solid #ddd; border-radius: 8px;
      padding: 10px; resize: none; font-size: 14px; outline: none;
      font-family: inherit; max-height: 100px;
    }
    .koala-widget-input textarea:focus { border-color: ${WIDGET_COLOR}; }
    .koala-widget-input button {
      background: ${WIDGET_COLOR}; color: #fff; border: none;
      border-radius: 8px; padding: 0 16px; cursor: pointer;
      font-size: 14px; font-weight: 600;
    }
    .koala-widget-input button:disabled { opacity: 0.5; cursor: not-allowed; }

    .koala-widget-footer { padding: 6px 12px; font-size: 11px; color: #999; text-align: center; background: #fff; border-top: 1px solid #f0f0f0; }
    .koala-widget-footer a { color: #999; text-decoration: none; }
  `;

  // ─── Construcción del DOM ──────────────────────────────────────
  function buildWidget() {
    const styleTag = document.createElement("style");
    styleTag.textContent = styles;
    document.head.appendChild(styleTag);

    const root = document.createElement("div");
    root.className = "koala-widget-root";
    root.innerHTML = `
      <div class="koala-widget-bubble" id="koala-widget-bubble" title="${WIDGET_TITLE}">
        <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
      </div>
      <div class="koala-widget-panel" id="koala-widget-panel">
        <div class="koala-widget-header">
          <div>
            <h3>${WIDGET_TITLE}</h3>
            <p>${WIDGET_SUBTITLE}</p>
          </div>
          <button id="koala-widget-close" aria-label="Cerrar">×</button>
        </div>
        <div class="koala-widget-messages" id="koala-widget-messages"></div>
        <div class="koala-widget-input">
          <textarea id="koala-widget-input" rows="1" placeholder="Escribí tu mensaje…"></textarea>
          <button id="koala-widget-send">Enviar</button>
        </div>
        <div class="koala-widget-footer">Powered by AgentKit</div>
      </div>
    `;
    document.body.appendChild(root);

    return {
      bubble: root.querySelector("#koala-widget-bubble"),
      panel: root.querySelector("#koala-widget-panel"),
      close: root.querySelector("#koala-widget-close"),
      messages: root.querySelector("#koala-widget-messages"),
      input: root.querySelector("#koala-widget-input"),
      send: root.querySelector("#koala-widget-send"),
    };
  }

  // ─── Renderizado de mensajes ───────────────────────────────────
  function appendMessage(container, role, text, extraClass) {
    const div = document.createElement("div");
    div.className = "koala-msg koala-msg-" + role + (extraClass ? " " + extraClass : "");
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
  }

  function renderHistory(container) {
    container.innerHTML = "";
    const history = getHistory();
    if (history.length === 0) {
      appendMessage(container, "bot", WIDGET_GREETING);
      return;
    }
    history.forEach(msg => appendMessage(container, msg.role === "user" ? "user" : "bot", msg.content));
  }

  // ─── Envío de mensajes ─────────────────────────────────────────
  async function sendMessage(elements, text) {
    if (!text.trim()) return;

    elements.send.disabled = true;
    elements.input.disabled = true;

    appendMessage(elements.messages, "user", text);
    const history = getHistory();
    history.push({ role: "user", content: text });
    saveHistory(history);

    const typingEl = appendMessage(elements.messages, "bot", "Escribiendo…", "koala-msg-typing");

    try {
      const response = await fetch(API_BASE + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: getSessionId(), message: text }),
      });

      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();

      typingEl.classList.remove("koala-msg-typing");
      typingEl.textContent = data.response;

      history.push({ role: "assistant", content: data.response });
      saveHistory(history);
    } catch (err) {
      console.error("Koala widget error:", err);
      typingEl.classList.remove("koala-msg-typing");
      typingEl.textContent = "Disculpá, tuve un problema técnico. Probá de nuevo en un momento.";
    } finally {
      elements.send.disabled = false;
      elements.input.disabled = false;
      elements.input.focus();
    }
  }

  // ─── Inicialización ────────────────────────────────────────────
  function init() {
    if (window.__koalaWidgetLoaded) return;
    window.__koalaWidgetLoaded = true;

    const elements = buildWidget();
    renderHistory(elements.messages);

    elements.bubble.addEventListener("click", () => {
      elements.panel.classList.toggle("open");
      if (elements.panel.classList.contains("open")) {
        setTimeout(() => elements.input.focus(), 100);
      }
    });

    elements.close.addEventListener("click", () => {
      elements.panel.classList.remove("open");
    });

    elements.send.addEventListener("click", () => {
      const text = elements.input.value;
      elements.input.value = "";
      sendMessage(elements, text);
    });

    elements.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        elements.send.click();
      }
    });

    // API pública (para devs que quieran controlar el widget)
    window.KoalaWidget = {
      open: () => elements.panel.classList.add("open"),
      close: () => elements.panel.classList.remove("open"),
      reset: () => { resetSession(); renderHistory(elements.messages); },
      sessionId: getSessionId,
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
