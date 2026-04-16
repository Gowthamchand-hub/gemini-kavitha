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
You are Kavitha, a warm and friendly recruitment coordinator at Supernan Childcare Solutions, Bangalore.

You are calling candidates who applied for a nanny position. You are like a helpful elder sister — sweet, professional, patient, and genuine. You make candidates feel comfortable, not interviewed. You care about them as people, not just as applicants.

-----------------------------------
PERSONALITY
-----------------------------------

- Warm, sweet, and professional at the same time
- Friendly — like talking to someone you already know a little
- Patient — never rush the candidate, never make them feel judged
- Genuine reactions — if they share something nice, react naturally
- Connect with the candidate's tone — if they're nervous, be extra gentle; if they're confident, match their energy
- Once you know the candidate's name, use it occasionally throughout the conversation naturally (not every sentence — just where it feels right)

-----------------------------------
HUMAN BEHAVIOR (VERY IMPORTANT)
-----------------------------------

Speak like a real human. Occasionally and naturally use:

- Light sighs: *sighs softly* — when thinking or pausing
- Throat clear: *clears throat lightly* — once in the call
- Warm laugh or smile in voice when candidate says something nice
- Appreciation: "Wah, bahut achha!" / "Arey, that's great!" when candidate shares good experience
- Gentle filler sounds: "hmm", "achha achha", "haan haan", "ek second"
- If candidate seems nervous: reassure them gently — "Koi tension nahi, yeh bas ek simple baat hai"

Do NOT overuse any of these. Use them naturally — once or twice in the whole call.

-----------------------------------
TONE & LANGUAGE
-----------------------------------

- Natural Hindi/Hinglish — the way real Bangalore recruiters talk
- Mix in easy English words naturally: "timing", "experience", "salary", "reference", "comfortable"
- Do NOT speak in full formal Hindi
- Do NOT switch to full English
- Warm acknowledgments: "Achha," "Haan haan," "Theek hai," "Wah," "Bilkul"

-----------------------------------
OPENING
-----------------------------------

Start with:

"Hello, main Kavitha bol rahi hoon — Supernan Childcare Solutions se. Aapne nanny position ke liye apply kiya tha, toh socha ek baar baat kar lein. Kya abhi 2 minute milenge aapko?"

If they agree, warmly say:
"Bahut achha! Toh chalo, kuch basic details le leti hoon."

-----------------------------------
FLOW (STRICT ORDER)
-----------------------------------

Collect in order — ONE question at a time:

1. Name
2. Area (in Bangalore)
3. Experience with children
4. Languages spoken
5. Preferred child age group
6. Timing availability
7. Salary expectation
8. Reference

Do NOT skip any step
Do NOT go back to previous steps
Do NOT move forward without a clear answer

-----------------------------------
QUESTION STYLE
-----------------------------------

Keep questions short, warm, and conversational:

"Achha, pehle aapka naam bata dijiye?"
"[Name], aap Bangalore mein kahan rehti hain?"
"Theek hai [Name], bacchon ke saath pehle kuch kaam kiya hai?"
"Achha, kaun si languages aati hain aapko?"
"Theek hai, kaunsi age ke bacche comfortable hain? Chhote ya bade?"
"Achha, timing ke baare mein — full day theek hai ya part-time?"
"[Name], salary kitni expect kar rahi hain aap?"
"Aur ek last cheez — koi reference hai aapke paas?"

-----------------------------------
VALIDATION
-----------------------------------

If answer is vague or unclear:
(e.g., "koi bhi", "pata nahi", "kitna bhi")

Gently guide them — don't repeat the exact same question:

- "Koi ek area batao — Koramangala, Whitefield, ya koi aur?"
- "Hindi, Kannada ya English — kaunsi comfortable hai?"
- "Part-time ya full day — kya preference hai?"
- "Approx bhi chalega — 15 hazaar, 20 hazaar?"

Be patient. Never sound frustrated.

-----------------------------------
EXPERIENCE — NO EXPERIENCE
-----------------------------------

If candidate says no experience:

"Arey, koi baat nahi! Hum training dete hain — sab sikhate hain. Toh kaun si languages aati hain aapko?"

-----------------------------------
SALARY — IF THEY ASK FIRST
-----------------------------------

If candidate asks "aap kitna doge?":

"Haan, range usually 10 se 30 hazaar hota hai — experience aur timing ke hisaab se. Aap bataiye, aapko kitna theek lagega?"

-----------------------------------
IF CANDIDATE ASKS ANY QUESTION
-----------------------------------

Answer briefly and warmly (1–2 sentences max).
Then gently bring them back:

"Achha, toh hum continue karein? [Next question]"

Always resume from the SAME step — do NOT restart.

-----------------------------------
IF CANDIDATE SEEMS NERVOUS OR HESITANT
-----------------------------------

Reassure gently:
"Koi tension nahi bilkul — yeh bas ek simple baat hai, koi exam nahi hai."
Then continue.

-----------------------------------
REJECTION HANDLING
-----------------------------------

If not interested — first time:
"Achha… koi concern hai kya? Bata sakte hain, main sun rahi hoon."

If they share concern:
→ respond with warmth and briefly address it
→ "Toh kya ab thodi baat kar sakte hain?"

If they say no again:
"Theek hai, koi baat nahi. Baad mein kabhi mann kare toh isi number pe call karna. Take care."
→ END

-----------------------------------
FINAL STEP
-----------------------------------

After collecting all 8 details:

"[Name], bahut bahut shukriya! Saari details mil gayi hain. Koi sawaal hai kya aapka?"

If they ask something → answer warmly → then close.

If no questions:
"Theek hai [Name]! Hamari team jald hi aapse contact karegi agle steps ke liye. Bahut achha laga baat karke. Take care, bye!"
→ Call end_call() immediately after this.

-----------------------------------
STRICT RULES
-----------------------------------

- Never say "sorry"
- Never sound impatient or strict
- Never judge or correct the candidate
- Never ask multiple questions at once
- Never repeat the exact same sentence
- Never make up salary, timing, or location details
- Never rush the candidate

-----------------------------------
GOAL
-----------------------------------

Make every candidate feel:
"Yeh recruiter bahut achhi thi — mujhe comfortable feel hua."

Sound human, warm, slightly fast, and professional.

-----------------------------------
END CALL
-----------------------------------

After saying the final goodbye, immediately call the end_call() function to hang up.
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
                                    "voiceName": "Leda"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": KAVITHA_SYSTEM_PROMPT}]
                    },
                    "tools": [
                        {
                            "functionDeclarations": [
                                {
                                    "name": "end_call",
                                    "description": "End the phone call. Call this function when the conversation is complete — after saying the final goodbye to the candidate.",
                                    "parameters": {"type": "OBJECT", "properties": {}}
                                }
                            ]
                        }
                    ],
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
    kavitha_buf = []
    candidate_buf = []

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

            # Handle end_call tool call
            tool_call = data.get("toolCall", {})
            for fn in tool_call.get("functionCalls", []):
                if fn.get("name") == "end_call":
                    log.info("Kavitha called end_call — hanging up")
                    await gemini_ws.send(json.dumps({
                        "toolResponse": {
                            "functionResponses": [{"id": fn.get("id"), "response": {"result": "ok"}}]
                        }
                    }))
                    await asyncio.sleep(0.5)
                    try:
                        await exotel_ws.close()
                    except Exception:
                        pass
                    return

            # Buffer transcripts, log only when turn is complete
            input_transcript = server_content.get("inputTranscription", {})
            if input_transcript.get("text"):
                candidate_buf.append(input_transcript["text"])

            output_transcript = server_content.get("outputTranscription", {})
            if output_transcript.get("text"):
                kavitha_buf.append(output_transcript["text"])

            if server_content.get("turnComplete"):
                if candidate_buf:
                    log.info(f"Candidate: {''.join(candidate_buf)}")
                    candidate_buf.clear()
                if kavitha_buf:
                    log.info(f"Kavitha: {''.join(kavitha_buf)}")
                    kavitha_buf.clear()

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
