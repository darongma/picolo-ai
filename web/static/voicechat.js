/**
 * voicechat.js – Voice chat integration for Picolo using Gemini Live API
 *
 * Fixes applied vs v1:
 *  1. isListening set BEFORE the worklet message handler runs (was gating all mic audio)
 *  2. Transcription logic inverted: Gemini sends content on !finished chunks,
 *     finished=true is just a "done" sentinel with empty text — match ws example
 *  3. _playAudio is async; wrapped in promise chain so audio always queues correctly
 *  4. Agent transcript streamed live chunk-by-chunk (not buffered until finished)
 *  5. _agentBuffer cleared on INTERRUPTED too
 *  6. Full system prompt (identity + soul + profile + memory) fetched from /api/voice-config
 *  7. Tools wired up: Gemini Live function calls are executed via /api/voice-tool
 */

class PicoloVoiceChat {
  constructor({ onUserTranscript, onAgentTranscript, onAgentAudioStart, onAgentAudioEnd, onStatusChange, onError, voiceName, sessionId, onToolCall, onToolResult }) {
    this.onUserTranscript  = onUserTranscript  || (() => {});
    this.onAgentTranscript = onAgentTranscript || (() => {});
    this.onAgentAudioStart = onAgentAudioStart || (() => {});
    this.onAgentAudioEnd   = onAgentAudioEnd   || (() => {});
    this.onStatusChange    = onStatusChange    || (() => {});
    this.onError           = onError           || ((e) => console.error('[VoiceChat]', e));
    this.onToolCall        = onToolCall        || (() => {});
    this.onToolResult      = onToolResult      || (() => {});

    // Fetched from /api/voice-config on start()
    this.systemPrompt = '';
    this.voiceTools   = [];
    this.voiceName    = voiceName  || 'Puck';
    this.sessionId    = sessionId  || 'default';

    this.gemini      = null;
    this.micContext  = null;
    this.micWorklet  = null;
    this.micStream   = null;
    this.playContext = null;
    this.playWorklet = null;

    this.isConnected = false;
    this.isListening = false;

    this._userBuffer  = '';
    this._agentBuffer = '';
    this._textBuffer  = ''; // For text messages sent via sendText()
  }

  // ── Public ──────────────────────────────────────────────────────────────────

  async start() {
    this._intentionalStop = false;
    this.onStatusChange('Loading agent config…');

    // ── Fetch full system prompt + tool declarations from the backend ──────
    try {
      const cfgResp = await fetch('/api/voice-config');
      if (cfgResp.ok) {
        const cfg = await cfgResp.json();
        this.systemPrompt = cfg.system_prompt || '';
        this.voiceTools   = cfg.tools || [];
        console.log(`[VoiceChat] Loaded system prompt (${this.systemPrompt.length} chars), ${this.voiceTools.length} tools`);
      } else {
        console.warn('[VoiceChat] Could not load voice config, continuing without it');
      }
    } catch (e) {
      console.warn('[VoiceChat] voice-config fetch failed:', e.message);
    }

    this.onStatusChange('Fetching session token…');

    let token;
    try {
      const resp = await fetch('/api/gemini-token', { method: 'POST' });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || resp.statusText);
      }
      ({ token } = await resp.json());
    } catch (e) {
      this.onError('Token error: ' + e.message);
      return;
    }

    this.onStatusChange('Connecting to Gemini Live…');

    this.gemini = new GeminiLiveAPI(token, 'gemini-3.1-flash-live-preview');
    this.gemini.systemInstructions       = this.systemPrompt;
    this.gemini.voiceName                = this.voiceName;
    this.gemini.inputAudioTranscription  = true;
    this.gemini.outputAudioTranscription = true;
    // Wire tools into Gemini Live so it can call them during voice sessions
    if (this.voiceTools && this.voiceTools.length > 0) {
      this.gemini.tools = this.voiceTools;
    }

    this.gemini.onOpen = () => {
      this.isConnected = true;
      this.onStatusChange('Connected — starting mic…');
      this._startMic();
    };

    this.gemini.onClose = (e) => {
      this.isConnected = false;
      this.isListening = false;
      // Code 1000 = normal close; if we didn't intentionally stop, reconnect
      if (this._intentionalStop) {
        this.onStatusChange('Disconnected');
      } else {
        console.warn('[VoiceChat] Unexpected close (code', e.code, ') — reconnecting…');
        this.onStatusChange('Reconnecting…');
        // Small delay then restart the session
        setTimeout(() => {
          if (!this._intentionalStop) this.start();
        }, 1500);
      }
    };

    this.gemini.onError = (msg) => {
      this.onError(msg);
      this.stop();
    };

    this.gemini.onReceiveResponse = (resp) => this._handleResponse(resp);
    this.gemini.connect();
  }

  stop() {
    this._intentionalStop = true;
    this._stopMic();
    this._stopPlayback();
    if (this.gemini) {
      this.gemini.disconnect();
      this.gemini = null;
    }
    this.isConnected = false;
    this.isListening = false;
    this._userBuffer  = '';
    this._agentBuffer = '';
    this.onStatusChange('Idle');
  }

  async sendText(text) {
    if (this.gemini && this.gemini.connected) {
      this.gemini.sendText(text);
      // Store text for later saving when turn completes
      this._textBuffer = text;
    }
  }

  // ── Microphone ──────────────────────────────────────────────────────────────

  async _startMic() {
    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });

      this.micContext = new AudioContext({ sampleRate: 16000 });
      await this.micContext.audioWorklet.addModule('/static/audio-processors/capture.worklet.js');
      this.micWorklet = new AudioWorkletNode(this.micContext, 'audio-capture-processor');

      // FIX 1: set flag BEFORE hooking the handler — no audio drops on first chunk
      this.isListening = true;

      this.micWorklet.port.onmessage = (e) => {
        if (!this.isListening) return;
        if (!this.gemini || !this.gemini.connected) return;
        if (e.data.type === 'audio') {
          const b64 = this._arrayBufferToBase64(this._floatToPCM16(e.data.data));
          this.gemini.sendAudio(b64);
        }
      };

      const src = this.micContext.createMediaStreamSource(this.micStream);
      src.connect(this.micWorklet);

      this.onStatusChange('Listening…');
    } catch (e) {
      this.onError('Mic error: ' + e.message);
    }
  }

  _stopMic() {
    this.isListening = false;
    if (this.micWorklet) { this.micWorklet.disconnect(); this.micWorklet = null; }
    if (this.micContext) { this.micContext.close().catch(() => {}); this.micContext = null; }
    if (this.micStream)  { this.micStream.getTracks().forEach(t => t.stop()); this.micStream = null; }
  }

  // ── Playback ────────────────────────────────────────────────────────────────

  async _ensurePlayContext() {
    if (this.playContext) return;
    this.playContext = new AudioContext({ sampleRate: 24000 });
    await this.playContext.audioWorklet.addModule('/static/audio-processors/playback.worklet.js');
    this.playWorklet = new AudioWorkletNode(this.playContext, 'pcm-processor');
    this.playWorklet.connect(this.playContext.destination);
  }

  // Capture worklet reference locally so a concurrent stop() doesn't null it mid-flight
  _playAudio(base64PCM) {
    this._ensurePlayContext().then(() => {
      const worklet = this.playWorklet;
      if (!worklet) return; // already stopped — discard this chunk
      const binary = atob(base64PCM);
      const bytes  = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const samples = new Float32Array(bytes.byteLength / 2);
      const view    = new DataView(bytes.buffer);
      for (let i = 0; i < samples.length; i++) {
        samples[i] = view.getInt16(i * 2, true) / 32768;
      }
      worklet.port.postMessage(samples);
    }).catch(e => console.error('[VoiceChat] Playback error:', e));
  }

  _stopPlayback() {
    if (this.playWorklet) {
      this.playWorklet.port.postMessage('interrupt');
      this.playWorklet.disconnect();
      this.playWorklet = null;
    }
    if (this.playContext) {
      this.playContext.close().catch(() => {});
      this.playContext = null;
    }
  }

  // ── Response handler ────────────────────────────────────────────────────────

  _handleResponse(resp) {
    switch (resp.type) {

      case 'SETUP_COMPLETE':
        console.log('[VoiceChat] Gemini session ready');
        this.onStatusChange('Listening…');
        break;

      case 'AUDIO':
        this.onAgentAudioStart();
        this._playAudio(resp.data);
        break;

      case 'TOOL_CALL':
        // Gemini Live wants to call one or more tools — execute them via the backend
        this._handleToolCall(resp.data);
        break;

      case 'INPUT_TRANSCRIPTION': {
        const text     = (resp.data && resp.data.text) || '';
        const finished = (resp.data && resp.data.finished) || false;
        if (!finished) {
          this._userBuffer += text;
          if (this._userBuffer.trim()) {
            this.onUserTranscript(this._userBuffer);
          }
        } else {
          this._userBuffer = '';
        }
        break;
      }

      case 'OUTPUT_TRANSCRIPTION': {
        const text     = (resp.data && resp.data.text) || '';
        const finished = (resp.data && resp.data.finished) || false;
        if (!finished) {
          this._agentBuffer += text;
          if (this._agentBuffer.trim()) {
            this.onAgentTranscript(this._agentBuffer);
          }
        } else {
          this._agentBuffer = '';
        }
        break;
      }

      case 'TURN_COMPLETE': {
        const userText  = this._userBuffer.trim() || this._textBuffer;
        const agentText = this._agentBuffer.trim();
        this._saveTurnToHistory(userText, agentText);
        this._agentBuffer = '';
        this._userBuffer  = '';
        this._textBuffer  = '';
        this.onAgentAudioEnd();
        break;
      }

      case 'INTERRUPTED':
        if (this.playWorklet) this.playWorklet.port.postMessage('interrupt');
        this._agentBuffer = '';
        break;
    }
  }

  // ── History persistence ─────────────────────────────────────────────────────

  /**
   * Saves a completed voice turn (user speech + agent response) to the backend DB.
   * Mirrors how agent.chat() persists text-chat messages via memory.add_message().
   */
  async _saveTurnToHistory(userText, agentText) {
    if (!userText && !agentText) {
      console.warn('[VoiceChat] _saveTurnToHistory called with no text — skipping');
      return;
    }
    console.log(`[VoiceChat] Saving turn — user: "${userText?.slice(0,60)}", agent: "${agentText?.slice(0,60)}"`);
    try {
      await fetch('/api/voice-message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id:  this.sessionId,
          user_text:   userText  || '',
          agent_text:  agentText || '',
        }),
      });
    } catch (e) {
      console.warn('[VoiceChat] Failed to save turn to history:', e.message);
    }
  }

  // ── Tool execution ──────────────────────────────────────────────────────────

  /**
   * Called when Gemini Live issues a toolCall message.
   * Executes each function call via /api/voice-tool and returns results to Gemini.
   *
   * @param {object} toolCallData - raw toolCall payload from Gemini Live
   *   { functionCalls: [{ id, name, args }] }
   */
  async _handleToolCall(toolCallData) {
    const calls = toolCallData.functionCalls || [];
    if (calls.length === 0) return;

    console.log('[VoiceChat] Tool calls:', calls.map(c => c.name).join(', '));
    this.onStatusChange(`Running tool${calls.length > 1 ? 's' : ''}: ${calls.map(c => c.name).join(', ')}…`);

    // Emit tool call events for UI rendering
    calls.forEach(call => {
      if (this.onToolCall) {
        this.onToolCall({
          tool: call.name,
          args: JSON.stringify(call.args || {}),
          tool_call_id: call.id
        });
      }
    });

    const results = await Promise.all(calls.map(async (call) => {
      try {
        const resp = await fetch('/api/voice-tool', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ 
            tool: call.name, 
            args: call.args || {}, 
            session_id: this.sessionId,
            tool_call_id: call.id 
          }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          return { id: call.id, name: call.name, response: `Error: ${err.detail || resp.statusText}` };
        }
        const data = await resp.json();
        console.log(`[VoiceChat] Tool "${call.name}" result:`, data.result);
        
        // Emit tool result event for UI rendering
        if (this.onToolResult) {
          this.onToolResult({
            tool: call.name,
            result: data.result,
            tool_call_id: call.id
          });
        }
        
        return { id: call.id, name: call.name, response: data.result };
      } catch (e) {
        console.error(`[VoiceChat] Tool "${call.name}" failed:`, e);
        
        // Emit error as tool result
        if (this.onToolResult) {
          this.onToolResult({
            tool: call.name,
            result: `Tool execution error: ${e.message}`,
            tool_call_id: call.id
          });
        }
        
        return { id: call.id, name: call.name, response: `Tool execution error: ${e.message}` };
      }
    }));

    // Send all results back to Gemini Live so it can continue the conversation
    if (this.gemini && this.gemini.connected) {
      this.gemini.sendToolResponse(results);
    }
    this.onStatusChange('Listening…');
  }

  // ── Utilities ───────────────────────────────────────────────────────────────

  _floatToPCM16(float32Array) {
    const pcm = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Array[i]));
      pcm[i] = s < 0 ? s * 32768 : s * 32767;
    }
    return pcm.buffer;
  }

  _arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let bin = '';
    for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
}
