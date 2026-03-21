// app.js – Picolo Web UI (Full-featured)
let agentConfig = {};
let sessionId = null;
let isTyping = false;
let statusStartTime = null;
let statusTimerInterval = null;
let abortController = null;
let logsInterval = null;

// DOM element shortcut
const $ = id => document.getElementById(id);

// Theme management
function applyTheme(theme) {
  document.body.classList.toggle('dark', theme === 'dark');
  const btn = $('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? '🌙' : '🌞';
}
function toggleTheme() {
  const current = document.body.classList.contains('dark') ? 'dark' : 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', next);
  applyTheme(next);
}

// Logs management
function fetchLogs() {
  fetch('/api/logs?limit=7')
    .then(res => res.ok ? res.json() : Promise.reject(res))
    .then(data => {
      $('log-content').textContent = data.logs.join('\n');
    })
    .catch(err => {
      $('log-content').textContent = `Error loading logs: ${err.statusText || err}`;
    });
}
function startLogsPolling() {
  if (logsInterval) clearInterval(logsInterval);
  logsInterval = setInterval(fetchLogs, 60000);
  fetchLogs(); // immediate
}
function stopLogsPolling() {
  if (logsInterval) clearInterval(logsInterval);
  logsInterval = null;
}

// Copy to clipboard
async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const orig = btn.title;
    btn.title = 'Copied!';
    btn.style.color = '#10b981';
    setTimeout(() => { btn.title = orig; btn.style.color = ''; }, 1500);
  } catch (e) { console.error('Copy failed:', e); }
}

// Timestamp formatting
function formatTimestamp(ts) {
  if (!ts) return '';
  // Expected format: "YYYY-MM-DD HH:MM:SS"
  return ts.substring(0, 19); // "YYYY-MM-DD HH:MM"
}

function getCurrentTimestamp() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  const hours = String(now.getHours()).padStart(2, '0');
  const minutes = String(now.getMinutes()).padStart(2, '0');
  const seconds = String(now.getSeconds()).padStart(2, '0');
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

// Render a single message
function renderMessage(msg) {
  const container = $('messages');
  if (msg.role === 'tool') {
    const toolDiv = document.createElement('div');
    toolDiv.className = 'message tool-result';
    const toolName = msg.name || 'tool';

    const header = document.createElement('div');
    header.className = 'tool-header';
    header.textContent = `Tool Call: ${toolName}`;
    header.title = "Click to expand/collapse";
    toolDiv.appendChild(header);

    const body = document.createElement('div');
    body.className = 'tool-body';
    body.textContent = msg.content || '';
    toolDiv.appendChild(body);

    // Smooth expand/collapse via class toggle
    header.addEventListener('click', () => {
      toolDiv.classList.toggle('open');
    });

    container.appendChild(toolDiv);
    return;
  }

  const isChat = msg.role === 'user' || msg.role === 'assistant';
  // Compute message number for chat messages (user/assistant)
  let messageNumber = '';
  if (isChat) {
    const prevChatCount = Array.from(container.children).filter(el => {
      const cls = el.className || '';
      return (cls.includes('user') || cls.includes('assistant')) && !cls.includes('welcome');
    }).length;
    messageNumber = prevChatCount + 1;
  }

  const timeStr = formatTimestamp(msg.timestamp);

  const div = document.createElement('div');
  div.className = `message ${msg.role}`;

  // Header with meta info and copy button
  const header = document.createElement('div');
  header.style.display = 'flex';
  header.style.justifyContent = 'space-between';
  header.style.alignItems = 'center';
  header.style.marginBottom = '4px';

  // Left side: counter and timestamp
  if (isChat) {
    const left = document.createElement('span');
    left.style.fontSize = '0.8rem';
    left.style.color = 'var(--text-light)';
    left.style.opacity = '0.8';
    left.textContent = `#${messageNumber} ${timeStr}`;
    header.appendChild(left);
  } else if (timeStr) {
    // For system or other roles
    const left = document.createElement('span');
    left.style.fontSize = '0.8rem';
    left.style.color = 'var(--text-light)';
    left.style.opacity = '0.7';
    left.textContent = timeStr;
    header.appendChild(left);
  }

  // Right side: copy button
  const copyBtn = document.createElement('button');
  copyBtn.className = 'copy-btn';
  copyBtn.title = 'Copy';
  copyBtn.innerHTML = '📋';
  copyBtn.onclick = () => copyText(msg.content || '', copyBtn);
  header.appendChild(copyBtn);

  div.appendChild(header);

  // Content
  const contentDiv = document.createElement('div');
  contentDiv.className = 'message-content';
  contentDiv.textContent = msg.content || '';
  div.appendChild(contentDiv);

  // Tool call indicator
  if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
    const toolNames = msg.tool_calls.map(tc => tc.function.name).join(', ');
    const toolDiv = document.createElement('div');
    toolDiv.className = 'message tool-call';
    toolDiv.style.fontSize = '0.85rem';
    toolDiv.style.marginTop = '4px';
    toolDiv.style.opacity = '0.9';
    toolDiv.textContent = `⚙️ Using: ${toolNames}`;
    div.appendChild(toolDiv);
  }

  container.appendChild(div);
}

// Scroll to bottom smoothly
function scrollToBottom() {
  const container = $('messages');
  container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
}

// Typing indicator
function setTyping(active) {
  $('typing-indicator').style.display = active ? 'flex' : 'none';
  if (active) scrollToBottom();
}

// Auto-resize textarea
function autoResize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

// Status bar
function startStatus(message = 'Thinking…') {
  $('status-bar').classList.remove('hidden');
  $('status-text').textContent = message;
  statusStartTime = Date.now();
  $('status-timer').textContent = '';
  if (statusTimerInterval) clearInterval(statusTimerInterval);
  statusTimerInterval = setInterval(() => {
    const elapsed = (Date.now() - statusStartTime) / 1000;
    $('status-timer').textContent = `(${elapsed.toFixed(1)}s)`;
  }, 200);
}
function stopStatus(finalMessage = null, duration = null) {
  if (statusTimerInterval) clearInterval(statusTimerInterval);
  statusTimerInterval = null;
  if (finalMessage) {
    $('status-text').textContent = finalMessage;
    if (duration !== null) $('status-timer').textContent = `(${duration.toFixed(1)}s)`;
    setTimeout(() => $('status-bar').classList.add('hidden'), 3000);
  } else {
    $('status-bar').classList.add('hidden');
  }
}
function cancelRequest() {
  if (abortController) {
    abortController.abort();
    abortController = null;
    stopStatus('Cancelled');
    $('status-cancel').disabled = true;
    $('status-cancel').textContent = 'Cancelling…';
  }
}

let PROVIDERS = []; // will be loaded from config API

function getProvidersFromConfig(config) {
  return config.providers || [
    // Minimal fallback if not defined in config
    { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1', models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'] },
    { id: 'anthropic', name: 'Anthropic', base_url: 'https://api.anthropic.com/v1', models: ['claude-3-5-sonnet-latest', 'claude-3-opus-latest', 'claude-3-haiku-latest'] },
    { id: 'nvidia', name: 'NVIDIA', base_url: 'https://integrate.api.nvidia.com/v1', models: ['stepfun-ai/step-3.5-flash', 'stepfun-ai/step-3.5-turbo', 'stepfun-ai/step-3.1-max'] },
    { id: 'google', name: 'Google', base_url: 'https://generativelanguage.googleapis.com/v1beta', models: ['gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash'] },
    { id: 'groq', name: 'Groq', base_url: 'https://api.groq.com/openai/v1', models: ['llama-3.1-8b', 'llama-3.1-70b', 'llama-3.2-11b', 'gemma2-9b', 'mixtral-8x7b'] },
    { id: 'openrouter', name: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', models: ['openai/gpt-4o', 'openai/gpt-4o-mini', 'anthropic/claude-3.5-sonnet', 'meta-llama/llama-3.3-70b-instruct', 'google/gemini-2.0-flash-001'] },
    { id: 'together', name: 'Together', base_url: 'https://api.together.xyz/v1', models: ['meta-llama/Llama-3.3-70B-Instruct-Turbo', 'deepseek-ai/DeepSeek-V3', 'Qwen/Qwen2.5-72B-Instruct-Turbo'] },
    { id: 'custom', name: 'Custom', base_url: '', models: [] }
  ];
}

// Config
async function initConfig() {
  try {
    const res = await fetch('/api/config');
    if (res.ok) {
      agentConfig = await res.json();
      PROVIDERS = getProvidersFromConfig(agentConfig);
      populateConfigForm();
    }
  } catch (e) { console.error('Config error:', e); }
}

// Session management
function updateSessionDisplay() {
  const display = $('session-id-display');
  if (sessionId) {
    // Show full session ID
    display.textContent = `Session: ${sessionId}`;
  } else {
    display.textContent = '';
  }
}
async function ensureSession() {
  // Try to get sessionId from localStorage first
  let saved = localStorage.getItem('picolo_session_id');
  if (saved) {
    sessionId = saved;
  } else {
    // Get a new UUID from the server
    try {
      const res = await fetch('/api/chat/new', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        sessionId = data.session_id;
        localStorage.setItem('picolo_session_id', sessionId);
      } else {
        sessionId = 'default';
      }
    } catch (e) {
      sessionId = 'default';
    }
  }
  updateSessionDisplay();
}
function populateConfigForm() {
  // Determine which provider should be selected from config or base_url
  let provider = agentConfig.provider;
  if (!provider) {
    provider = guessProviderFromBaseUrl(agentConfig.base_url);
    if (!PROVIDERS.find(p => p.id === provider)) provider = 'custom';
  }
  // Populate provider dropdown and select the current provider
  repopulateProviderDropdown(provider);

  // Populate models for the selected provider
  populateModelOptions(provider, agentConfig.model);

  // Set other fields
  $('system-prompt').value = agentConfig.system_prompt || '';
  $('max-input-tokens').value = agentConfig.max_input_tokens || 200000;
  const email = agentConfig.email || {};
  $('smtp-server').value = email.smtp_server || '';
  $('smtp-port').value = email.smtp_port || 587;
  $('email-username').value = email.username || '';
  $('email-password').value = email.password || '';
  $('imap-server').value = email.imap_server || '';
  $('imap-port').value = email.imap_port || 993;
  $('imap-use-ssl').checked = email.imap_use_ssl !== false; // default true
  // Telegram fields
  $('telegram-token').value = agentConfig.telegram_token || '';
  $('telegram-allowed-users').value = agentConfig.telegram_allowed_users ? agentConfig.telegram_allowed_users.join(', ') : '';
  // Discord fields
  $('discord-token').value = agentConfig.discord_token || '';
  $('discord-allowed-users').value = agentConfig.discord_allowed_users ? agentConfig.discord_allowed_users.join(', ') : '';
  // Initialize provider management UI
  handleProviderChange(provider);
}

function guessProviderFromBaseUrl(url) {
  if (!url) return 'custom';
  for (const p of PROVIDERS) {
    if (p.base_url && url.startsWith(p.base_url)) return p.id;
  }
  return 'custom';
}

function repopulateProviderDropdown(selectedId = null) {
  const providerSelect = $('provider');
  providerSelect.innerHTML = '';
  PROVIDERS.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    providerSelect.appendChild(opt);
  });
  // If selectedId provided and exists, select it
  if (selectedId && PROVIDERS.find(p => p.id === selectedId)) {
    providerSelect.value = selectedId;
  }
}

function populateModelOptions(provider, currentModel) {
  const modelSelect = $('model');
  modelSelect.innerHTML = '';
  const provDef = PROVIDERS.find(p => p.id === provider);
  const models = provDef && provDef.models ? provDef.models : [];

  if (models.length === 0) {
    // Custom provider: use a text input instead of select
    const input = document.createElement('input');
    input.type = 'text';
    input.id = 'model';
    input.name = 'model';
    input.placeholder = 'Enter model name';
    input.value = currentModel || '';
    modelSelect.parentNode.replaceChild(input, modelSelect);
  } else {
    // Ensure we have a <select> element
    if ($('model').tagName !== 'SELECT') {
      const select = document.createElement('select');
      select.id = 'model';
      select.name = 'model';
      modelSelect.parentNode.replaceChild(select, modelSelect);
    }
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === currentModel) opt.selected = true;
      $('model').appendChild(opt);
    });
  }
}

// Provider Management (Simplified Inline)
function handleProviderChange(providerId) {
  const editSection = $('provider-edit-section');
  const addSection = $('provider-add-section');
  const deleteBtn = $('delete-provider-btn');
  const showAddBtn = $('show-add-provider');

  if (providerId === 'custom') {
    editSection.style.display = 'none';
    addSection.style.display = 'block';
    showAddBtn.style.display = 'none';
    // Clear all add fields
    $('provider-name-add').value = '';
    $('provider-id-add').value = '';
    $('provider-base-url-add').value = '';
    $('provider-models-add').value = '';
    $('provider-api-key-add').value = '';
    $('provider-add-status').textContent = '';
  } else {
    addSection.style.display = 'none';
    showAddBtn.style.display = 'block';
    editSection.style.display = 'block';
    const prov = PROVIDERS.find(p => p.id === providerId);
    if (prov) {
      $('provider-name-edit').value = prov.name;
      $('provider-base-url-edit').value = prov.base_url || '';
      $('provider-models-edit').value = prov.models ? prov.models.join(', ') : '';
      $('provider-api-key-edit').value = prov.api_key || '';
    }
    $('provider-edit-status').textContent = '';
  }

  deleteBtn.disabled = PROVIDERS.length <= 1;
}

function setupEditListeners() {
  ['provider-name-edit', 'provider-base-url-edit', 'provider-models-edit', 'provider-api-key-edit'].forEach(elmId => {
    $(elmId).addEventListener('input', () => {
      const selected = $('provider').value;
      if (selected === 'custom') return;
      const prov = PROVIDERS.find(p => p.id === selected);
      if (!prov) return;
      if (elmId === 'provider-name-edit') {
        prov.name = $(elmId).value.trim();
        repopulateProviderDropdown(selected);
      }
      if (elmId === 'provider-base-url-edit') {
        prov.base_url = $(elmId).value.trim();
      }
      if (elmId === 'provider-models-edit') {
        prov.models = $(elmId).value.split(',').map(s => s.trim()).filter(Boolean);
        if (selected === $('provider').value) {
          populateModelOptions(selected, $('model').value);
        }
      }
      if (elmId === 'provider-api-key-edit') {
        prov.api_key = $(elmId).value.trim();
      }
      const statusEl = $('provider-edit-status');
      statusEl.textContent = 'Unsaved changes (will save with Settings)';
      statusEl.className = 'status';
      clearTimeout(window.editStatusTimeout);
      window.editStatusTimeout = setTimeout(() => {
        if (statusEl.textContent.includes('Unsaved')) statusEl.textContent = '';
      }, 2000);
    });
  });
}

function deleteSelectedProvider() {
  const selected = $('provider').value;
  if (PROVIDERS.length <= 1) {
    alert('You must have at least one provider.');
    return;
  }
  const prov = PROVIDERS.find(p => p.id === selected);
  if (!prov) return;
  if (!confirm(`Delete provider "${prov.name}"?`)) return;
  PROVIDERS = PROVIDERS.filter(p => p.id !== selected);
  const newSelected = PROVIDERS[0].id;
  $('provider').value = newSelected;
  agentConfig.provider = newSelected;
  repopulateProviderDropdown(newSelected);
  populateModelOptions(newSelected, agentConfig.model);
  handleProviderChange(newSelected);
  const statusEl = $('provider-edit-status');
  statusEl.textContent = `Deleted ${prov.name}`;
  statusEl.className = 'status success';
  setTimeout(() => { if (statusEl.textContent.startsWith('Deleted')) statusEl.textContent = ''; }, 3000);
}
// History
async function initChat() {
  if (!sessionId) await ensureSession();
  try {
    const res = await fetch(`/api/chat/history?session_id=${sessionId}`);
    const data = await res.json();
    $('messages').innerHTML = '';
    if (data.history.length === 0) {
      const welcomeContent = "Hello! I'm Picolo, your AI assistant. I can help with office documents, web pages, file operations, and more. How can I assist you today?";
      const welcome = document.createElement('div');
      welcome.className = 'message assistant welcome';

      // Header with timestamp and copy
      const header = document.createElement('div');
      header.style.display = 'flex';
      header.style.justifyContent = 'space-between';
      header.style.alignItems = 'center';
      header.style.marginBottom = '4px';

      // Timestamp (left side)
      const left = document.createElement('span');
      left.style.fontSize = '0.8rem';
      left.style.color = 'var(--text-light)';
      left.style.opacity = '0.8';
      left.textContent = formatTimestamp(getCurrentTimestamp());
      header.appendChild(left);

      // Copy button right
      const copyBtn = document.createElement('button');
      copyBtn.className = 'copy-btn';
      copyBtn.title = 'Copy';
      copyBtn.innerHTML = '📋';
      copyBtn.onclick = () => copyText(welcomeContent, copyBtn);
      header.appendChild(copyBtn);

      welcome.appendChild(header);

      // Content
      const contentDiv = document.createElement('div');
      contentDiv.className = 'message-content';
      contentDiv.textContent = welcomeContent;
      welcome.appendChild(contentDiv);

      $('messages').appendChild(welcome);
    } else {
      data.history.forEach(renderMessage);
    }
    scrollToBottom();
  } catch (e) { console.error('History error:', e); }
}

// Send message
async function sendMessage() {
  const input = $('user-input');
  const sendBtn = $('send-btn');
  const content = input.value.trim();
  if (!content || isTyping) return;

  // Slash command: /new → start a new session
  if (content === '/new') {
    // Treat as "New Chat"
    input.value = '';
    autoResize(input);
    $('clear-chat-btn').click();
    return;
  }

  input.value = '';
  autoResize(input);
  sendBtn.disabled = true;
  setTyping(true);
  document.getElementById("user-input").placeholder="Type a message… ";
  now=new Date().toLocaleString();
  startStatus(agentConfig.provider+" 🤖 "+agentConfig.model+' is working... 📆 '+now+'… 🕒');

  abortController = new AbortController();

  try {
    const container = $('messages');

    // Compute message number and timestamp for user message
    const prevChatCount = Array.from(container.children).filter(el => {
      const cls = el.className || '';
      return (cls.includes('user') || cls.includes('assistant')) && !cls.includes('welcome');
    }).length;
    const messageNumber = prevChatCount + 1;
    const timestamp = getCurrentTimestamp();
    const timeStr = formatTimestamp(timestamp);

    const userDiv = document.createElement('div');
    userDiv.className = 'message user';

    const header = document.createElement('div');
    header.style.display = 'flex';
    header.style.justifyContent = 'space-between';
    header.style.alignItems = 'center';
    header.style.marginBottom = '4px';

    const left = document.createElement('span');
    left.style.fontSize = '0.8rem';
    left.style.color = 'var(--text-light)';
    left.style.opacity = '0.8';
    left.textContent = `#${messageNumber} ${timeStr}`;
    header.appendChild(left);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.title = 'Copy';
    copyBtn.innerHTML = '📋';
    copyBtn.onclick = () => copyText(content, copyBtn);
    header.appendChild(copyBtn);

    userDiv.appendChild(header);

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = content;
    userDiv.appendChild(contentDiv);

    container.appendChild(userDiv);
    scrollToBottom();

    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: content, session_id: sessionId }),
      signal: abortController.signal
    });

    const elapsed = (Date.now() - statusStartTime) / 1000;
    stopStatus('Done', elapsed);
    setTyping(false);
    abortController = null;
    $('status-cancel').disabled = false;
    $('status-cancel').textContent = 'Cancel';

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || 'Unknown error');
    }

    const data = await res.json();
    
    // Use Optional Chaining (?.) to keep the code clean and crash-proof
if (data.history?.length > 0 && data.response) {
  const lastEntry = data.history[data.history.length - 1];
  
  // Ensure we are comparing the right field (content vs response)
  if (data.response !== lastEntry.content) {
    const now = Date.now();
    const formatted = new Date(now).toISOString().replace('T', ' ').slice(0, 19);
    data.history.push({
      role: "assistant",
      content: "⛔ " + data.response,
      timestamp: formatted
    });
  }
}
    
    /*
    const beforeCount = container.children.length;
    const newHistory = data.history.slice(beforeCount);
    */

    const newHistory = data.history;
    const usedTools = new Set();
    newHistory.forEach(msg => {
      if (msg.role === 'assistant' && msg.tool_calls) {
        msg.tool_calls.forEach(tc => usedTools.add(tc.function.name));
      }
    });

    now=new Date().toLocaleString();
    document.getElementById("user-input").placeholder="Type a message… \r\n\r\n"+agentConfig.provider+" 🤖 "+agentConfig.model+" completed your last request with 💰 "+data.tokens+" tokens in 🕒 "+Math.round(elapsed)+" seconds on 📆"+now;
    
    let finalMsg = 'Done';
    if (usedTools.size > 0) finalMsg += ` (${Array.from(usedTools).join(', ')})`;
    stopStatus(finalMsg, elapsed);
    newHistory.forEach(renderMessage);
    scrollToBottom();

  } catch (e) {
    setTyping(false);
    stopStatus();
    if (e.name !== 'AbortError') {
      const errDiv = document.createElement('div');
      errDiv.className = 'message system';
      errDiv.textContent = `Error: ${e.message}`;
      $('messages').appendChild(errDiv);
      scrollToBottom();
    }
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// Event listeners
function setupEventListeners() {
  // Theme toggle
  $('theme-toggle').addEventListener('click', toggleTheme);
  // Load saved theme or respect system preference
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme) applyTheme(savedTheme);
  else if (window.matchMedia('(prefers-color-scheme: dark)').matches) applyTheme('dark');

  // Config panel toggle
  $('config-toggle').addEventListener('click', () => {
    $('config-panel').hidden = !$('config-panel').hidden;
  });

  // Logs panel toggle
  $('logs-toggle').addEventListener('click', () => {
    const panel = $('logs-panel');
    panel.hidden = !panel.hidden;
    if (!panel.hidden) {
      startLogsPolling();
    } else {
      stopLogsPolling();
    }
  });

  // New Chat: create a fresh session ID and reload history
  $('clear-chat-btn').addEventListener('click', async () => {
    if (!confirm('Start a new conversation? This will create a new session ID.')) return;
    try {
      const res = await fetch('/api/chat/new', { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        sessionId = data.session_id;
        localStorage.setItem('picolo_session_id', sessionId);
        updateSessionDisplay();
        await initChat(); // loads empty history with welcome
      }
    } catch (e) { console.error('New chat failed:', e); }
  });



  // Textarea auto-resize
  $('user-input').addEventListener('input', () => autoResize($('user-input')));

  // Send with Enter (Shift+Enter inserts newline)
  $('user-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Send button
  $('send-btn').addEventListener('click', sendMessage);

  // Cancel button
  $('status-cancel').addEventListener('click', cancelRequest);

  // Provider change → update model options and management sections
  $('provider').addEventListener('change', () => {
    const providerId = $('provider').value;
    populateModelOptions(providerId, '');
    handleProviderChange(providerId);
  });

  // Edit provider fields → live update in memory
  setupEditListeners();

  // Delete provider button
  $('delete-provider-btn').addEventListener('click', deleteSelectedProvider);

  // Show Add Provider button
  $('show-add-provider').addEventListener('click', () => {
    $('provider').value = 'custom';
    $('provider').dispatchEvent(new Event('change'));
  });

  // Add Provider confirm
  $('add-provider-confirm').addEventListener('click', () => {
    const name = $('provider-name-add').value.trim();
    let id = $('provider-id-add').value.trim().toLowerCase().replace(/\s+/g, '-');
    const baseUrl = $('provider-base-url-add').value.trim();
    const apiKey = $('provider-api-key-add').value.trim();
    const modelsStr = $('provider-models-add').value.trim();
    const statusEl = $('provider-add-status');

    if (!name || !id) {
      statusEl.textContent = 'Name and ID are required';
      statusEl.className = 'status error';
      return;
    }
    id = id.replace(/[^a-z0-9_-]/g, '');
    if (!id) {
      statusEl.textContent = 'Invalid ID (use lowercase letters, numbers, - or _)';
      statusEl.className = 'status error';
      return;
    }
    if (PROVIDERS.find(p => p.id === id)) {
      statusEl.textContent = 'Provider ID already exists';
      statusEl.className = 'status error';
      return;
    }
    const models = modelsStr ? modelsStr.split(',').map(s => s.trim()).filter(Boolean) : [];
    PROVIDERS.push({ id, name, base_url: baseUrl, api_key: apiKey, models });

    statusEl.textContent = `Added ${name}`;
    statusEl.className = 'status success';
    setTimeout(() => { if (statusEl.textContent.includes('Added')) statusEl.textContent = ''; }, 3000);

    // Select the new provider
    $('provider').value = id;
    $('provider').dispatchEvent(new Event('change'));
  });

  // Config form submission
  $('config-form').addEventListener('submit', async e => {
    e.preventDefault();
    // Validate providers: ensure each has a non-empty name and ID
    for (const p of PROVIDERS) {
      if (!p.name || p.name.trim() === '') {
        const statusEl = $('config-status');
        statusEl.textContent = 'All providers must have a name.';
        statusEl.className = 'status error';
        return;
      }
      if (!p.id || p.id.trim() === '') {
        const statusEl = $('config-status');
        statusEl.textContent = 'All providers must have an ID.';
        statusEl.className = 'status error';
        return;
      }
    }
    const provider = $('provider').value;
    const updates = {
      provider: provider === 'custom' ? null : provider, // store only if known
      model: $('model').value,
      max_input_tokens: parseInt($('max-input-tokens').value, 10) || 200000,
      system_prompt: $('system-prompt').value,
      email: {
        smtp_server: $('smtp-server').value,
        smtp_port: parseInt($('smtp-port').value, 10) || 587,
        username: $('email-username').value,
        password: $('email-password').value,
        imap_server: $('imap-server').value,
        imap_port: parseInt($('imap-port').value, 10) || 993,
        imap_use_ssl: $('imap-use-ssl').checked
      },
      telegram_token: $('telegram-token').value.trim(),
      telegram_allowed_users: $('telegram-allowed-users').value.split(',').map(s => s.trim()).filter(Boolean),
      discord_token: $('discord-token').value.trim(),
      discord_allowed_users: $('discord-allowed-users').value.split(',').map(s => s.trim()).filter(Boolean),
      providers: PROVIDERS // persist provider catalog
    };
    const statusEl = $('config-status');
    statusEl.textContent = 'Saving…';
    statusEl.className = 'status';
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
      });
      if (res.ok) {
        statusEl.textContent = '✓ Settings saved.';
        statusEl.className = 'status success';
        setTimeout(() => statusEl.textContent = '', 3000);
      } else {
        const err = await res.json();
        statusEl.textContent = `Error: ${err.detail || 'unknown'}`;
        statusEl.className = 'status error';
      }
    } catch (e) {
      statusEl.textContent = `Network error: ${e}`;
      statusEl.className = 'status error';
    }
  });
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
  initConfig();
  ensureSession().then(() => initChat());
  setupEventListeners();
});
