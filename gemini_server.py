#!/usr/bin/env python3
"""
WebSocket bridge server: Exotel <-> Gemini 3.1 Flash Live (Priya agent).

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
import time
import random
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv
import websockets
import gspread
from google.oauth2.service_account import Credentials

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Sheets setup
# ---------------------------------------------------------------------------

SHEET_ID = "112yETKsk2aaM6knc5Bk8ZUFd-IHK0bD_puMOKrDz7XQ"
SHEET_NAME = "gemini-kavitha-sheets"
SHEET_HEADERS = ["Timestamp", "Name", "Area", "Experience", "Languages", "Age Group", "Timing", "Salary", "Smartphone", "Reference Name", "Reference Number", "Status"]

def get_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        log.warning("GOOGLE_CREDENTIALS_JSON not set — Sheets logging disabled")
        return None
    try:
        creds_dict = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            sheet = spreadsheet.worksheet(SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.sheet1  # fallback to first tab
        return sheet
    except Exception as e:
        log.error(f"Google Sheets init error: {e}")
        return None

def ensure_headers(sheet):
    try:
        first_row = sheet.row_values(1)
        if first_row != SHEET_HEADERS:
            sheet.insert_row(SHEET_HEADERS, 1)
    except Exception as e:
        log.error(f"Header check error: {e}")

async def save_to_sheet(data: dict):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_to_sheet_sync, data)
    except Exception as e:
        log.error(f"save_to_sheet error: {e}")

def _save_to_sheet_sync(data: dict):
    sheet = get_sheet()
    if not sheet:
        return
    ensure_headers(sheet)
    row = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        data.get("name", ""),
        data.get("area", ""),
        data.get("experience", ""),
        data.get("languages", ""),
        data.get("age_group", ""),
        data.get("timing", ""),
        data.get("salary", ""),
        data.get("smartphone", ""),
        data.get("reference_name", ""),
        data.get("reference_number", ""),
        data.get("status", "Completed"),
    ]
    sheet.append_row(row)
    log.info(f"Saved to sheet: {data.get('name', 'Unknown')}")

TRANSCRIPT_SHEET_NAME = "call-transcripts"
TRANSCRIPT_HEADERS = ["Timestamp", "Phone", "Name", "Status", "Transcript"]

async def save_transcript(data: dict, conversation_log: list):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _save_transcript_sync, data, conversation_log)
    except Exception as e:
        log.error(f"save_transcript error: {e}")

def _save_transcript_sync(data: dict, conversation_log: list):
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            return
        creds_dict = json.loads(creds_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        try:
            sheet = spreadsheet.worksheet(TRANSCRIPT_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=TRANSCRIPT_SHEET_NAME, rows=1000, cols=5)
        first_row = sheet.row_values(1)
        if first_row != TRANSCRIPT_HEADERS:
            sheet.insert_row(TRANSCRIPT_HEADERS, 1)
        transcript_text = "\n".join(conversation_log)
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.get("phone", ""),
            data.get("name", ""),
            data.get("status", ""),
            transcript_text,
        ]
        sheet.append_row(row)
        log.info(f"Transcript saved for {data.get('name', 'Unknown')}")
    except Exception as e:
        log.error(f"_save_transcript_sync error: {e}")

# ---------------------------------------------------------------------------

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

_prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kavitha_prompt.txt")
with open(_prompt_path, "r", encoding="utf-8") as _f:
    KAVITHA_SYSTEM_PROMPT = _f.read()

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
    <Stream url="{stream_ws_url}" bidirectional="true" />
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
    log.info(f"WS /stream — headers: {dict(exotel_ws.headers)}")
    await exotel_ws.accept(subprotocol=subprotocol)
    log.info(f"Exotel WebSocket accepted — subprotocol={subprotocol}")

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
                                    "voiceName": "Despina"
                                }
                            }
                        }
                    },
                    "realtimeInputConfig": {
                        # Manual VAD active — built-in VAD disabled
                        # To revert to auto VAD, replace with:
                        # "automaticActivityDetection": {
                        #     "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                        #     "endOfSpeechSensitivity": "END_SENSITIVITY_LOW"
                        # }
                        "automaticActivityDetection": {
                            "disabled": True
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
                                    "description": "End the phone call. Call this function after saying the final goodbye to the candidate.",
                                    "parameters": {"type": "OBJECT", "properties": {}}
                                },
                                {
                                    "name": "save_candidate",
                                    "description": "Save the candidate record before ending the call. Call this ALWAYS before end_call, in every scenario — completed screening, busy/callback, not interested, or disqualified. Pass whatever data was collected and a status string.",
                                    "parameters": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "status":           {"type": "STRING", "description": "One of: Completed, Not Interested, Busy - Callback [time], Disqualified - No Experience, Disqualified - Outside Bangalore, Disqualified - No Smartphone, Disqualified - No Reference"},
                                            "name":             {"type": "STRING", "description": "Candidate's full name (if collected)"},
                                            "area":             {"type": "STRING", "description": "Area in Bangalore (if collected)"},
                                            "experience":       {"type": "STRING", "description": "Years of experience and where (if collected)"},
                                            "languages":        {"type": "STRING", "description": "Languages with assessed proficiency (if collected)"},
                                            "age_group":        {"type": "STRING", "description": "Preferred child age group (if collected)"},
                                            "timing":           {"type": "STRING", "description": "Working hours candidate mentioned (if collected)"},
                                            "salary":           {"type": "STRING", "description": "Salary expectation (if collected)"},
                                            "smartphone":       {"type": "STRING", "description": "Yes or Can Arrange (if collected)"},
                                            "reference_name":   {"type": "STRING", "description": "Reference person name (if collected)"},
                                            "reference_number": {"type": "STRING", "description": "Reference contact number (if collected)"}
                                        },
                                        "required": ["status"]
                                    }
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

            # Wait for Exotel start event to get stream_sid
            stream_sid_holder = []
            async for raw in exotel_ws.iter_text():
                evt = json.loads(raw)
                if evt.get("event") == "connected":
                    continue
                if evt.get("event") == "start":
                    info = evt.get("start", {})
                    stream_sid = info.get("stream_sid") or info.get("streamSid", "")
                    stream_sid_holder.append(stream_sid)
                    caller_phone = info.get("from", "") or info.get("caller", "")
                    log.info(f"Stream started — streamSid: {stream_sid}")
                    break

            candidate_name = os.environ.get("TEST_CANDIDATE_NAME", "").strip()
            name_info = f" The candidate's name is {candidate_name}." if candidate_name else ""
            await gemini_ws.send(json.dumps({"realtimeInput": {"text": f"The call has just connected.{name_info} Begin the conversation now."}}))

            last_audio_ts = [0.0]
            last_speech_ts = [0.0]   # updated only on VAD activityStart/End — used by silence watchdog
            nudge_pending  = [False] # True after watchdog nudge sent — cleared on Kavitha's turnComplete
            resample_state = [None]
            first_turn_done = [False]  # True after Kavitha's first message — ignore candidate audio until then
            session_data = [{"phone": caller_phone}]  # incremental candidate data collected during call
            call_completed = [False]  # True if save_candidate was called (full completion)
            goodbye_spoken = [False]  # True if goodbye phrase detected in Kavitha's speech
            pending_hangup = [False]  # True if end_call was received but goodbye was injected first
            conversation_log = []     # full transcript: ["Kavitha: ...", "Candidate: ...", ...]
            hello_count      = [0]    # resets when candidate speaks — shared with watchdog
            kavitha_speaking = [False]  # True while Gemini audio is streaming — watchdog skips during this
            task1 = asyncio.create_task(_exotel_to_gemini(exotel_ws, gemini_ws, stream_sid_holder, last_audio_ts, last_speech_ts, resample_state, first_turn_done, hello_count))
            task2 = asyncio.create_task(_gemini_to_exotel(gemini_ws, exotel_ws, stream_sid_holder, last_audio_ts, last_speech_ts, nudge_pending, first_turn_done, session_data, call_completed, goodbye_spoken, pending_hangup, conversation_log, kavitha_speaking))
            task3 = asyncio.create_task(_silence_watchdog(gemini_ws, first_turn_done, last_speech_ts, nudge_pending, call_completed, goodbye_spoken, hello_count, kavitha_speaking))

            done, pending = await asyncio.wait([task1, task2, task3], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            # Save partial data if call ended without completing all questions
            if not call_completed[0] and session_data[0]:
                partial = dict(session_data[0])
                partial["status"] = "Incomplete"
                await save_to_sheet(partial)
            # Save transcript
            if conversation_log:
                await save_transcript(session_data[0], conversation_log)
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


async def _exotel_to_gemini(exotel_ws: WebSocket, gemini_ws, stream_sid_holder: list, last_audio_ts: list, last_speech_ts: list, resample_state: list, first_turn_done: list, hello_count: list = None):
    """Candidate's voice -> Gemini with manual VAD.

    State machine:
      silence -> possible_speech -> speech -> possible_silence -> silence
    Short sounds (< SPEECH_START_CHUNKS * ~20ms) never trigger activityStart.
    """
    ENERGY_THRESHOLD   = 150   # RMS level to consider as speech — lowered from 300 to catch soft/low women's voices
    SPEECH_START_CHUNKS = 12   # ~240ms of speech needed before activityStart — filters quick backchannels without blocking real speech
    SPEECH_END_CHUNKS   = 30   # ~600ms of silence needed before activityEnd

    vad_state    = "silence"
    speech_chunks  = 0
    silence_chunks = 0
    audio_buffer   = []  # holds chunks during possible_speech

    try:
        async for raw in exotel_ws.iter_text():
            data  = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                log.info("Exotel stream connected")

            elif event == "start":
                pass  # already handled before tasks started

            elif event == "media":
                audio_b64 = data["media"]["payload"]
                last_audio_ts[0] = time.time()
                raw_audio = base64.b64decode(audio_b64)
                raw_audio, resample_state[0] = audioop.ratecv(raw_audio, 2, 1, 8000, 16000, resample_state[0])

                if not first_turn_done[0]:
                    continue  # block candidate audio until Kavitha's first message finishes

                rms       = audioop.rms(raw_audio, 2)
                is_speech = rms > ENERGY_THRESHOLD
                pcm_b64   = base64.b64encode(raw_audio).decode()

                if vad_state == "silence":
                    if is_speech:
                        vad_state     = "possible_speech"
                        speech_chunks = 1
                        audio_buffer  = [pcm_b64]

                elif vad_state == "possible_speech":
                    if is_speech:
                        speech_chunks += 1
                        audio_buffer.append(pcm_b64)
                        if speech_chunks >= SPEECH_START_CHUNKS:
                            vad_state      = "speech"
                            silence_chunks = 0
                            log.debug(f"VAD: speech started (rms={rms})")
                            last_speech_ts[0] = time.time()
                            if hello_count is not None:
                                hello_count[0] = 0  # candidate spoke — reset hello counter
                            await gemini_ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))
                            for buf in audio_buffer:
                                await gemini_ws.send(json.dumps({
                                    "realtimeInput": {"audio": {"data": buf, "mimeType": "audio/pcm;rate=16000"}}
                                }))
                            audio_buffer = []
                    else:
                        vad_state     = "silence"
                        speech_chunks = 0
                        audio_buffer  = []

                elif vad_state == "speech":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"audio": {"data": pcm_b64, "mimeType": "audio/pcm;rate=16000"}}
                    }))
                    if not is_speech:
                        vad_state      = "possible_silence"
                        silence_chunks = 1
                    else:
                        silence_chunks = 0

                elif vad_state == "possible_silence":
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {"audio": {"data": pcm_b64, "mimeType": "audio/pcm;rate=16000"}}
                    }))
                    if is_speech:
                        vad_state      = "speech"
                        silence_chunks = 0
                    else:
                        silence_chunks += 1
                        if silence_chunks >= SPEECH_END_CHUNKS:
                            vad_state      = "silence"
                            silence_chunks = 0
                            speech_chunks  = 0
                            log.debug("VAD: speech ended")
                            last_speech_ts[0] = time.time()
                            await gemini_ws.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))

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


async def _gemini_to_exotel(gemini_ws, exotel_ws: WebSocket, stream_sid_holder: list, last_audio_ts: list, last_speech_ts: list, nudge_pending: list, first_turn_done: list, session_data: list, call_completed: list, goodbye_spoken: list = None, pending_hangup: list = None, conversation_log: list = None, kavitha_speaking: list = None):
    """Kavitha's voice (Gemini) -> candidate."""
    if goodbye_spoken is None:
        goodbye_spoken = [False]
    if pending_hangup is None:
        pending_hangup = [False]
    if conversation_log is None:
        conversation_log = []
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
                    if kavitha_speaking is not None:
                        kavitha_speaking[0] = True
                    audio_b64 = inline_data["data"]
                    raw_audio = base64.b64decode(audio_b64)

                    # Gemini outputs 24kHz PCM — resample to 8kHz for Exotel
                    raw_audio, _ = audioop.ratecv(raw_audio, 2, 1, 24000, 8000, None)

                    # Subtle line noise
                    samples = list(struct.unpack(f"{len(raw_audio)//2}h", raw_audio))
                    noise_level = 60
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

            # Handle tool calls
            tool_call = data.get("toolCall", {})
            for fn in tool_call.get("functionCalls", []):
                if fn.get("name") == "save_candidate" and not call_completed[0]:
                    args = fn.get("args", {})
                    # status comes from Gemini; fallback to Completed if missing
                    if not args.get("status"):
                        args["status"] = "Completed"
                    session_data[0].update(args)
                    call_completed[0] = True
                    log.info(f"Saving candidate: {args.get('name', 'Unknown')} — {args.get('status')}")
                    await save_to_sheet(args)
                    await gemini_ws.send(json.dumps({
                        "toolResponse": {
                            "functionResponses": [{"id": fn.get("id"), "response": {"result": "saved"}}]
                        }
                    }))

                if fn.get("name") == "end_call":
                    await gemini_ws.send(json.dumps({
                        "toolResponse": {
                            "functionResponses": [{"id": fn.get("id"), "response": {"result": "ok"}}]
                        }
                    }))
                    if not goodbye_spoken[0]:
                        # Gemini skipped the goodbye — force it now
                        log.info("Goodbye not spoken — injecting goodbye before hangup")
                        candidate_name = session_data[0].get("name", "")
                        name_part = f"{candidate_name}ji, " if candidate_name else ""
                        goodbye_text = f"{name_part}Bahut shukriya aapka time dene ke liye. Thank you. Good day."
                        await gemini_ws.send(json.dumps({
                            "clientContent": {
                                "turns": [{"role": "user", "parts": [{"text": f"[SAY THIS OUT LOUD NOW]: {goodbye_text}"}]}],
                                "turnComplete": True
                            }
                        }))
                        pending_hangup[0] = True
                    else:
                        log.info("Kavitha called end_call — hanging up")
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

            if server_content.get("turnComplete") or server_content.get("generationComplete"):
                if kavitha_speaking is not None:
                    kavitha_speaking[0] = False
                first_turn_done[0] = True  # Kavitha finished — candidate audio now live
                last_speech_ts[0] = time.time()  # reset silence timer — give candidate fresh time to respond
                nudge_pending[0] = False  # Kavitha responded to nudge — allow next nudge if needed
                if candidate_buf:
                    text = ''.join(candidate_buf)
                    log.info(f"Candidate: {text}")
                    conversation_log.append(f"Candidate: {text}")
                    candidate_buf.clear()
                if kavitha_buf:
                    full_text = ''.join(kavitha_buf)
                    log.info(f"Kavitha: {full_text}")
                    conversation_log.append(f"Kavitha: {full_text}")
                    kavitha_buf.clear()
                    # Track goodbye, hangup when injected goodbye finishes
                    goodbye_phrases = ["good day", "lekar aayiye", "passport photo", "aapka number hata", "shukriya aapka time", "bahut shukriya"]
                    if any(p in full_text.lower() for p in goodbye_phrases):
                        goodbye_spoken[0] = True
                        if pending_hangup[0]:
                            log.info("Injected goodbye spoken — now hanging up")
                        else:
                            log.info("Goodbye detected — ending call")
                        await asyncio.sleep(1)
                        try:
                            await exotel_ws.close()
                        except Exception:
                            pass
                        return
                first_response = True

    except websockets.exceptions.ConnectionClosedOK:
        log.info("Gemini reader closed")
    except Exception as e:
        log.error(f"Gemini→Exotel error: {e}")
    finally:
        try:
            await exotel_ws.close()
        except Exception:
            pass


async def _silence_watchdog(gemini_ws, first_turn_done: list, last_speech_ts: list, nudge_pending: list, call_completed: list, goodbye_spoken: list = None, hello_count: list = None, kavitha_speaking: list = None):
    """Say 'hello' every 5s of silence. End call after 4 consecutive unanswered hellos."""
    SILENCE_TIMEOUT = 5   # seconds of silence before saying hello
    CHECK_INTERVAL  = 2   # how often to check
    MAX_HELLOS      = 4   # end call after this many consecutive unanswered hellos

    if hello_count is None:
        hello_count = [0]

    await asyncio.sleep(5)  # give call time to start
    last_speech_ts[0] = time.time()  # reset baseline

    try:
        while not call_completed[0]:
            await asyncio.sleep(CHECK_INTERVAL)
            if not first_turn_done[0]:
                last_speech_ts[0] = time.time()
                continue
            if goodbye_spoken and goodbye_spoken[0]:
                break
            if nudge_pending[0]:
                continue  # wait for Kavitha to finish before next hello
            if kavitha_speaking and kavitha_speaking[0]:
                last_speech_ts[0] = time.time()  # Kavitha is speaking — reset timer
                continue
            elapsed = time.time() - last_speech_ts[0]
            if elapsed >= SILENCE_TIMEOUT:
                hello_count[0] += 1
                log.info(f"Silence watchdog: {elapsed:.1f}s — hello #{hello_count[0]}")
                nudge_pending[0] = True
                if hello_count[0] >= MAX_HELLOS:
                    log.info("No response after 4 hellos — ending call")
                    try:
                        await gemini_ws.send(json.dumps({
                            "realtimeInput": {
                                "text": "The candidate has not responded after several attempts. Say a brief goodbye and end the call now."
                            }
                        }))
                    except Exception:
                        pass
                    break
                try:
                    await gemini_ws.send(json.dumps({
                        "realtimeInput": {
                            "text": "The candidate has been silent. IMPORTANT: if you are currently waiting for them on purpose — for example they said 'wait', 'ek second', 'ruko', or you told them to take their time (like for a reference number) — then stay completely silent and do NOT say anything. Only if the silence is unexpected, say 'Hello?' in the language you are currently speaking."
                        }
                    }))
                except Exception:
                    break
    except asyncio.CancelledError:
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


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "model": GEMINI_MODEL})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("gemini_server:app", host="0.0.0.0", port=port, reload=False)
