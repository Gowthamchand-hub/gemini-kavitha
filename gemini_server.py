#!/usr/bin/env python3
"""
WebSocket bridge server: Exotel <-> Gemini 3.1 Flash Live (Kavitha agent).

Flow:
  Exotel Stream applet opens WS to /stream
  server bridges audio to Gemini Live API
  Gemini responds as Kavitha in real time

Run:
  uvicorn gemini_server:app --host 0.0.0.0 --port 8000
"""

import os
import json
import base64
import asyncio
import logging
import audioop
import struct
import random
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv
import websockets

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

def get_ws_base_url():
    load_dotenv(override=True)
    return os.getenv("SERVER_WS_BASE_URL", "wss://your-server.com")

GEMINI_WS_URL = (
    f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta"
    f".GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

# Gemini Live audio output sample rate
GEMINI_OUTPUT_SAMPLE_RATE = 24000

KAVITHA_SYSTEM_PROMPT = """
You are Kavitha, a recruitment coordinator at Supernan Childcare Solutions, Bangalore.

Your job is to speak with candidates and collect basic details for a nanny position in a structured, step-by-step manner.

You are professional, slightly fast-paced, and efficient. You do not overtalk, do not explain unnecessarily, and do not behave like a chatbot. You sound like a real recruiter doing many calls daily.

-----------------------------------
CORE BEHAVIOR
-----------------------------------

- Ask ONE question at a time
- Keep responses short (1 sentence, max 2)
- Do NOT over-explain
- Do NOT repeat questions unnecessarily
- Do NOT summarize user responses
- Do NOT correct or judge user answers
- Do NOT switch to full English (use simple Hindi / Hinglish)

-----------------------------------
FLOW (STRICT ORDER)
-----------------------------------

You must collect these details in order:

1. Name
2. Area (Bangalore location)
3. Experience with children
4. Languages
5. Preferred child age
6. Timing
7. Salary expectation
8. Reference

Do NOT skip any step
Do NOT go back to previous steps
Do NOT move forward without a valid answer

-----------------------------------
START
-----------------------------------

Always begin with:

"Haan, kuch details leni thi — shuru karein? Aap apna naam bata dijiye."

-----------------------------------
QUESTION STYLE
-----------------------------------

Use natural, short phrasing:

- "Achha, Bangalore mein kahan rehte hain aap?"
- "Theek hai, bacchon ke saath experience hai aapko?"
- "Achha, kaun si languages aati hain aapko?"
- "Theek hai, kaunsi age ke bacche comfortable hain aapko?"
- "Achha, timing kya chahiye aapko?"
- "Theek hai, kitna expect kar rahe hain aap?"
- "Achha, koi reference hai aapke paas?"

-----------------------------------
VALIDATION (VERY IMPORTANT)
-----------------------------------

If the answer is unclear, vague, or irrelevant:
(e.g., "koi bhi", "pata nahi", "bharat", "indian", "kitna bhi")

→ Ask the same question again in a clearer way
→ Do NOT move forward

Examples:
- "Hindi, Kannada, ya English?"
- "Full day ya part-time?"
- "Approx bata dijiye… 15, 20, ya 25 hazaar?"

-----------------------------------
EXCEPTION 1: NO EXPERIENCE
-----------------------------------

If user says they have no experience:

Say:
"Koi baat nahi, hum training dete hain. Achha, kaun si languages aati hain aapko?"

-----------------------------------
EXCEPTION 2: SALARY QUESTION
-----------------------------------

If user asks:
"aap kitna doge?"

Say ONLY:
"Range usually 10 se 30 hazaar hota hai… aap bataiye aapko kitna theek lagega?"

Then wait for answer.

-----------------------------------
USER QUESTIONS (IMPORTANT)
-----------------------------------

If user asks ANY question during flow:

→ Answer briefly (1–2 lines max)
→ Then say:
"Theek hai… kya hum details continue karein?"

→ Then continue from SAME step (do NOT restart)

-----------------------------------
REJECTION HANDLING
-----------------------------------

If user says they are not interested:

First time:
"Achha… koi concern hai kya? Bata sakte hain."

If they share concern:
→ Address briefly
→ Then ask:
"Toh ab baat continue kar sakte hain?"

If they reject again:
"Theek hai. Koi baat nahi. Baad mein mann kare toh isi number pe call karna. Take care."
→ END conversation

-----------------------------------
FINAL STEP
-----------------------------------

After collecting all details:

Ask:
"Theek hai… saari details mil gayi hain. Koi sawaal hai?"

If user asks question:
→ Answer → ask to continue → then finish

If user says NO:
→ Say:
"Theek hai… hamari team aapse jald contact karegi. Thank you, take care."
→ END

-----------------------------------
STRICT RULES
-----------------------------------

- Never say "sorry"
- Never give long explanations
- Never invent details (salary, timing, location, etc.)
- Never ask multiple questions at once
- Never end the call early unless rejection is final
- Never behave like a chatbot

-----------------------------------
GOAL
-----------------------------------

Sound like a real recruiter:
calm, efficient, slightly fast, and human.
"""

app = FastAPI(title="Exotel-Gemini Kavitha Bridge")


# ---------------------------------------------------------------------------
# /answer — Exotel hits this when candidate picks up
# ---------------------------------------------------------------------------

@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    stream_ws_url = f"{get_ws_base_url().rstrip('/')}/stream"
    log.info(f"Call answered — streaming to {stream_ws_url}")

    exoml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_ws_url}" />
    </Connect>
</Response>"""

    return Response(content=exoml, media_type="application/xml")


# ---------------------------------------------------------------------------
# /stream — WebSocket bridge: Exotel <-> Gemini Live
# ---------------------------------------------------------------------------

@app.websocket("/stream")
async def stream(exotel_ws: WebSocket):
    subprotocol = None
    if "sec-websocket-protocol" in exotel_ws.headers:
        subprotocol = exotel_ws.headers["sec-websocket-protocol"].split(",")[0].strip()
    await exotel_ws.accept(subprotocol=subprotocol)
    log.info("Exotel WebSocket connected")

    try:
        async with websockets.connect(
            GEMINI_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as gemini_ws:
            log.info("Connected to Gemini Live API")

            # Send setup config to Gemini
            setup_msg = {
                "setup": {
                    "model": f"models/{GEMINI_MODEL}",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Aoede"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": KAVITHA_SYSTEM_PROMPT}]
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))

            # Wait for setup complete
            setup_response = await gemini_ws.recv()
            log.info(f"Gemini setup response: {setup_response[:100]}")

            # Trigger Kavitha to start the call
            await gemini_ws.send(json.dumps({
                "realtimeInput": {
                    "text": "The call has just connected. Begin the conversation now."
                }
            }))

            stream_sid_holder = []
            task1 = asyncio.create_task(_exotel_to_gemini(exotel_ws, gemini_ws, stream_sid_holder))
            task2 = asyncio.create_task(_gemini_to_exotel(gemini_ws, exotel_ws, stream_sid_holder))

            done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            try:
                await gemini_ws.close()
            except Exception:
                pass

    except WebSocketDisconnect:
        log.info("Exotel disconnected")
    except websockets.exceptions.ConnectionClosedOK:
        log.info("Gemini connection closed cleanly")
    except Exception as e:
        log.exception(f"Bridge error: {e}")
    finally:
        log.info("Stream session ended")


async def _exotel_to_gemini(exotel_ws: WebSocket, gemini_ws, stream_sid_holder: list):
    """Candidate's voice -> Gemini."""
    try:
        async for raw in exotel_ws.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                log.info("Exotel stream connected")

            elif event == "start":
                info = data.get("start", {})
                stream_sid = info.get("stream_sid") or info.get("streamSid", "")
                stream_sid_holder.append(stream_sid)
                log.info(f"Stream started — callSid: {info.get('call_sid')}, streamSid: {stream_sid}")

            elif event == "media":
                audio_b64 = data["media"]["payload"]
                # Exotel sends 8kHz PCM — Gemini Live accepts it directly
                await gemini_ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": audio_b64,
                            "mimeType": "audio/pcm;rate=8000"
                        }
                    }
                }))

            elif event == "stop":
                log.info("Exotel stream stopped — candidate hung up")
                try:
                    await gemini_ws.close()
                except Exception:
                    pass
                break

    except WebSocketDisconnect:
        log.info("Exotel disconnected — closing Gemini")
        try:
            await gemini_ws.close()
        except Exception:
            pass
    except Exception as e:
        log.error(f"Exotel→Gemini error: {e}")


async def _gemini_to_exotel(gemini_ws, exotel_ws: WebSocket, stream_sid_holder: list):
    """Kavitha's voice (Gemini) -> candidate."""
    try:
        async for raw in gemini_ws:
            data = json.loads(raw)

            server_content = data.get("serverContent", {})
            model_turn = server_content.get("modelTurn", {})
            parts = model_turn.get("parts", [])

            for part in parts:
                inline_data = part.get("inlineData", {})
                mime = inline_data.get("mimeType", "")
                if mime.startswith("audio/"):
                    audio_b64 = inline_data["data"]
                    raw_audio = base64.b64decode(audio_b64)

                    # Gemini outputs 24kHz PCM — resample to 8kHz for Exotel
                    raw_audio, _ = audioop.ratecv(raw_audio, 2, 1, 24000, 8000, None)

                    # Subtle line noise
                    samples = list(struct.unpack(f"{len(raw_audio)//2}h", raw_audio))
                    noise_level = 150
                    noise = []
                    for i in range(0, len(samples), 8):
                        n = random.randint(-noise_level, noise_level)
                        noise.extend([n] * min(8, len(samples) - i))
                    samples = [max(-32768, min(32767, s + noise[i])) for i, s in enumerate(samples)]
                    raw_audio = struct.pack(f"{len(samples)}h", *samples)

                    audio_b64 = base64.b64encode(raw_audio).decode()
                    stream_sid = stream_sid_holder[0] if stream_sid_holder else ""
                    try:
                        await exotel_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_b64},
                        }))
                    except Exception:
                        break

            # Log transcripts
            input_transcript = server_content.get("inputTranscription", {})
            if input_transcript.get("text"):
                log.info(f"Candidate: {input_transcript['text']}")

            output_transcript = server_content.get("outputTranscription", {})
            if output_transcript.get("text"):
                log.info(f"Kavitha: {output_transcript['text']}")

    except websockets.exceptions.ConnectionClosedOK:
        log.info("Gemini reader closed")
    except Exception as e:
        log.error(f"Gemini→Exotel error: {e}")
    finally:
        try:
            await exotel_ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /status — Exotel call status callback
# ---------------------------------------------------------------------------

@app.api_route("/status", methods=["GET", "POST"])
async def status(request: Request):
    if request.method == "POST":
        form = await request.form()
        data = dict(form)
    else:
        data = dict(request.query_params)

    log.info(f"Call {data.get('CallSid', 'unknown')} ended — "
             f"status: {data.get('Status', 'unknown')}, "
             f"duration: {data.get('Duration', '0')}s")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# /stream-config — Returns WSS URL for Exotel Stream applet
# ---------------------------------------------------------------------------

@app.api_route("/stream-config", methods=["GET", "POST"])
async def stream_config(request: Request):
    wss_url = f"{get_ws_base_url().rstrip('/')}/stream"
    return JSONResponse({"url": wss_url})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "model": GEMINI_MODEL})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("gemini_server:app", host="0.0.0.0", port=port, reload=False)
