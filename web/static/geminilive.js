/**
 * geminilive.js – Gemini Live WebSocket client for Picolo
 * Model: gemini-3.1-flash-live-preview
 * API:   v1alpha + BidiGenerateContentConstrained (ephemeral token auth)
 */

function parseResponseMessages(data) {
  const responses = [];
  const serverContent = data?.serverContent;
  const parts = serverContent?.modelTurn?.parts;

  try {
    if (data?.setupComplete) {
      responses.push({ type: 'SETUP_COMPLETE', data: '' });
      return responses;
    }
    if (data?.toolCall) {
      responses.push({ type: 'TOOL_CALL', data: data.toolCall });
      return responses;
    }
    if (parts?.length) {
      for (const part of parts) {
        if (part.inlineData) {
          responses.push({ type: 'AUDIO', data: part.inlineData.data });
        } else if (part.text) {
          responses.push({ type: 'TEXT', data: part.text });
        }
      }
    }
    if (serverContent?.inputTranscription) {
      responses.push({
        type: 'INPUT_TRANSCRIPTION',
        data: {
          text: serverContent.inputTranscription.text || '',
          finished: serverContent.inputTranscription.finished || false,
        },
      });
    }
    if (serverContent?.outputTranscription) {
      responses.push({
        type: 'OUTPUT_TRANSCRIPTION',
        data: {
          text: serverContent.outputTranscription.text || '',
          finished: serverContent.outputTranscription.finished || false,
        },
      });
    }
    if (serverContent?.interrupted) {
      responses.push({ type: 'INTERRUPTED', data: '' });
    }
    if (serverContent?.turnComplete) {
      responses.push({ type: 'TURN_COMPLETE', data: '', endOfTurn: true });
    }
  } catch (err) {
    console.error('[GeminiLive] Parse error:', err, data);
  }
  return responses;
}

class GeminiLiveAPI {
  constructor(token, model) {
    this.token = token;
    this.model = model || 'gemini-3.1-flash-live-preview';
    this.modelUri = 'models/' + this.model;

    this.responseModalities = ['AUDIO'];  // AUDIO only — TEXT causes 1007 on native audio
    this.systemInstructions = '';
    this.voiceName = 'Puck';
    this.inputAudioTranscription = true;
    this.outputAudioTranscription = true;
    // Array of Gemini FunctionDeclaration objects: { name, description, parameters }
    this.tools = [];
    this.connected = false;
    this.webSocket = null;

    // v1alpha — matches the ws example and the token creation api_version
    this.serviceUrl =
      'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token=' +
      this.token;

    // Callbacks
    this.onReceiveResponse = () => {};
    this.onOpen  = () => {};
    this.onClose = () => {};
    this.onError = (msg) => console.error('[GeminiLive] Error:', msg);
  }

  connect() {
    console.log('[GeminiLive] Connecting to:', this.serviceUrl.replace(this.token, '<token>'));
    this.webSocket = new WebSocket(this.serviceUrl);

    this.webSocket.onopen = () => {
      console.log('[GeminiLive] WebSocket opened');
      this.connected = true;
      this._sendSetup();
      this.onOpen();
    };

    this.webSocket.onclose = (e) => {
      console.log('[GeminiLive] WebSocket closed:', e.code, e.reason);
      this.connected = false;
      this.onClose(e);
    };

    this.webSocket.onerror = (e) => {
      console.error('[GeminiLive] WebSocket error:', e);
      this.connected = false;
      this.onError('WebSocket connection error. Check browser console for details.');
    };

    this.webSocket.onmessage = this._onMessage.bind(this);
  }

  disconnect() {
    if (this.webSocket) {
      this.webSocket.close();
      this.connected = false;
    }
  }

  _send(obj) {
    if (this.webSocket && this.webSocket.readyState === WebSocket.OPEN) {
      this.webSocket.send(JSON.stringify(obj));
    } else {
      console.warn('[GeminiLive] Cannot send — WebSocket not open');
    }
  }

  _sendSetup() {
    const setup = {
      setup: {
        model: this.modelUri,
        generationConfig: {
          responseModalities: this.responseModalities,
          speechConfig: {
            voiceConfig: {
              prebuiltVoiceConfig: { voiceName: this.voiceName },
            },
          },
        },
        systemInstruction: {
          parts: [{ text: this.systemInstructions }],
        },
      },
    };

    // Inject tool declarations so Gemini Live can call them
    if (this.tools && this.tools.length > 0) {
      setup.setup.tools = [{ functionDeclarations: this.tools }];
    }

    // Transcription fields sit at the top level of setup, NOT inside generationConfig
    if (this.inputAudioTranscription) {
      setup.setup.inputAudioTranscription = {};
    }
    if (this.outputAudioTranscription) {
      setup.setup.outputAudioTranscription = {};
    }

    console.log('[GeminiLive] Sending setup:', JSON.stringify(setup, null, 2));
    this._send(setup);
  }

  /**
   * Send tool call results back to Gemini Live after executing them locally.
   * @param {Array<{id: string, name: string, response: any}>} results
   */
  sendToolResponse(results) {
    const toolResponses = results.map(r => ({
      id: r.id,
      name: r.name,
      response: { result: r.response },
    }));
    this._send({ toolResponse: { functionResponses: toolResponses } });
  }

  async _onMessage(event) {
    let text;
    if (event.data instanceof Blob)        text = await event.data.text();
    else if (event.data instanceof ArrayBuffer) text = new TextDecoder().decode(event.data);
    else                                   text = event.data;

    try {
      const data = JSON.parse(text);
      const responses = parseResponseMessages(data);
      for (const resp of responses) {
        this.onReceiveResponse(resp);
      }
    } catch (e) {
      console.error('[GeminiLive] JSON parse error:', e, text);
    }
  }

  sendAudio(base64PCM) {
    this._send({
      realtimeInput: {
        audio: { mimeType: 'audio/pcm', data: base64PCM },
      },
    });
  }

  sendText(text) {
    this._send({ realtimeInput: { text } });
  }
}
