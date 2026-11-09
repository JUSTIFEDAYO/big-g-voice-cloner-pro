"""
BIG G'S VOICE CLONER PRO - Next-Gen Voice AI & Audio Studio
Engineered with:
- Genuine Zero-Shot In-Context Voice Cloning (Fish Audio s2.1-pro-free)
- Curated Ultra-Human Pro Voices (Fish Audio reference_id community & studio catalog)
- 400+ Free Edge-TTS Neural Voices
- Voice Deletion (with clean disk file removal)
- Out-of-the-box Audio FX Studio: Reverb, Radio, Deep Bass, Whisper, Cyberpunk Echo
- Multi-Speaker Dialogue & Conversation Generator
- One-Click High Accuracy Speech-to-Text Auto-Transcription
"""

import os
import json
import uuid
import asyncio
import time
import shutil
import io
import numpy as np
import soundfile as sf
import edge_tts
import httpx
import msgpack
import speech_recognition as sr
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
VOICES_DIR = os.path.join(BASE_DIR, "voices")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")

for d in [STATIC_DIR, UPLOADS_DIR, VOICES_DIR, OUTPUTS_DIR]:
    os.makedirs(d, exist_ok=True)

VOICES_DB_FILE = os.path.join(VOICES_DIR, "cloned_voices.json")
if not os.path.exists(VOICES_DB_FILE):
    with open(VOICES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)

app = FastAPI(title="BIG G'S VOICE CLONER PRO", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_fish_api_key():
    return os.getenv("FISH_AUDIO_API_KEY", "").strip()

def get_studio_credentials():
    return {
        "username": os.getenv("STUDIO_USERNAME", "bigg").strip(),
        "access_key": os.getenv("STUDIO_ACCESS_KEY", "bigg2026").strip()
    }

class LoginRequest(BaseModel):
    username: str
    access_key: str

@app.post("/api/auth/login")
def auth_login(req: LoginRequest):
    creds = get_studio_credentials()
    if req.username.strip().lower() == creds["username"].lower() and req.access_key.strip() == creds["access_key"]:
        # Return secure session token
        token = f"token_{uuid.uuid4().hex}"
        return JSONResponse({
            "success": True,
            "token": token,
            "username": creds["username"],
            "message": "Welcome back to BIG G'S VOICE CLONER PRO!"
        })
    return JSONResponse({
        "success": False, 
        "message": "Invalid username or studio access key."
    }, status_code=401)

def get_db():
    try:
        with open(VOICES_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_db(data):
    with open(VOICES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# Curated Ultra-Human USA English Pro Voices (Fish Audio reference_id models)
CURATED_HUMAN_VOICES = [
    {
        "id": "fish_7c0a85bc08f3454c9e29dba716eef0ab",
        "fish_id": "7c0a85bc08f3454c9e29dba716eef0ab",
        "name": "Marcus - Deep American Documentary",
        "gender": "Male",
        "locale": "en-US",
        "category": "Cinematic & Narration",
        "description": "Ultra-rich, deep, velvety American narrator voice (Morgan Freeman / Attenborough style).",
        "avatar": "🎙️",
        "is_fish_pro": True
    },
    {
        "id": "fish_933563129e564b19a115bedd57b7406a",
        "fish_id": "933563129e564b19a115bedd57b7406a",
        "name": "Sarah - Natural American Conversational",
        "gender": "Female",
        "locale": "en-US",
        "category": "Podcast & Everyday",
        "description": "Warm, relatable, natural American young adult voice with authentic breath nuances.",
        "avatar": "🎧",
        "is_fish_pro": True
    },
    {
        "id": "fish_802e3bc2b27e49c2995d23ef70e6ac89",
        "fish_id": "802e3bc2b27e49c2995d23ef70e6ac89",
        "name": "Jordan - American Commercial & Tech",
        "gender": "Male",
        "locale": "en-US",
        "category": "Commercial & Ads",
        "description": "Crisp, dynamic, upbeat American corporate & product launch speaker.",
        "avatar": "⚡",
        "is_fish_pro": True
    },
    {
        "id": "fish_15471e9a59e74a90a87f52ec52a9a21e",
        "fish_id": "15471e9a59e74a90a87f52ec52a9a21e",
        "name": "Elena - Warm American Storyteller",
        "gender": "Female",
        "locale": "en-US",
        "category": "Audiobook & Story",
        "description": "Intimate, warm American storytelling voice with smooth expressive inflection.",
        "avatar": "📖",
        "is_fish_pro": True
    },
    {
        "id": "fish_8ec9973300874562962bbcb300f82213",
        "fish_id": "8ec9973300874562962bbcb300f82213",
        "name": "Guyzo - Gritty American Movie Trailer",
        "gender": "Male",
        "locale": "en-US",
        "category": "Trailer & Gaming",
        "description": "Deep raspy cinematic American trailer narrator with commanding low-end punch.",
        "avatar": "🎬",
        "is_fish_pro": True
    },
    {
        "id": "fish_a2346273eb314b6cb82ba83dbc9d9fee",
        "fish_id": "a2346273eb314b6cb82ba83dbc9d9fee",
        "name": "Mark - Dynamic American Creator",
        "gender": "Male",
        "locale": "en-US",
        "category": "Gaming & YouTube",
        "description": "Energetic, engaging, expressive American creator voice with dramatic pitch swings.",
        "avatar": "🎮",
        "is_fish_pro": True
    },
    {
        "id": "fish_6fa730641de144038d185bb35b31787b",
        "fish_id": "6fa730641de144038d185bb35b31787b",
        "name": "Seraphina - Emotional American Cinema",
        "gender": "Female",
        "locale": "en-US",
        "category": "Drama & Cinema",
        "description": "Soft, emotive American narrator with gentle cinematic whisper pauses.",
        "avatar": "✨",
        "is_fish_pro": True
    },
    {
        "id": "fish_c5f56a6cc2ec4fa8920cb4c5889a3fb7",
        "fish_id": "c5f56a6cc2ec4fa8920cb4c5889a3fb7",
        "name": "Slax - Clear American Radio Host",
        "gender": "Male",
        "locale": "en-US",
        "category": "Podcast & Broadcast",
        "description": "Smooth, articulate, balanced American broadcast voice for news and journalism.",
        "avatar": "📻",
        "is_fish_pro": True
    }
]

def analyze_audio_profile(file_path):
    """Extract acoustic characteristics (fundamental pitch, spectral centroid, energy)."""
    try:
        data, samplerate = sf.read(file_path)
        if len(data.shape) > 1:
            data = data.mean(axis=1)
        
        energy = float(np.sqrt(np.mean(data**2)))
        corr = np.correlate(data[:min(len(data), 16000)], data[:min(len(data), 16000)], mode='full')
        corr = corr[len(corr)//2:]
        d = np.diff(corr)
        start = np.where(d > 0)[0]
        if len(start) > 0:
            peak = np.argmax(corr[start[0]:]) + start[0]
            estimated_pitch = float(samplerate / peak) if peak > 0 else 180.0
        else:
            estimated_pitch = 180.0
            
        duration = float(len(data) / samplerate)
        gender = "Female" if estimated_pitch > 165 else "Male"
        pitch_shift_pct = int(np.clip(((estimated_pitch - 160) / 160) * 40, -40, 40))
        
        return {
            "duration": round(duration, 2),
            "samplerate": samplerate,
            "estimated_pitch_hz": round(estimated_pitch, 1),
            "gender_affinity": gender,
            "recommended_pitch_mod": f"{'+' if pitch_shift_pct >= 0 else ''}{pitch_shift_pct}%",
            "energy_db": round(20 * np.log10(energy + 1e-6), 1)
        }
    except Exception:
        return {
            "duration": 5.0,
            "samplerate": 24000,
            "estimated_pitch_hz": 180.0,
            "gender_affinity": "Neutral",
            "recommended_pitch_mod": "+0%",
            "energy_db": -20.0
        }

def apply_dsp_effects(file_path: str, effect: str) -> str:
    """Apply creative studio DSP audio filters."""
    try:
        data, sr = sf.read(file_path)
        if len(data.shape) > 1:
            data = data.mean(axis=1)

        out_name = f"fx_{effect}_{uuid.uuid4().hex[:8]}.wav"
        out_path = os.path.join(OUTPUTS_DIR, out_name)

        if effect == "reverb":
            # Convolve with synthetic decay impulses for concert hall reflection
            decay = np.exp(-np.linspace(0, 5, int(sr * 0.45)))
            ir = np.random.randn(len(decay)) * decay
            convolved = np.convolve(data, ir, mode='full')[:len(data)]
            processed = 0.7 * data + 0.3 * convolved
        elif effect == "telephone":
            # Bandpass filter simulation (rough telephone / walkie-talkie speaker)
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[(freqs < 400) | (freqs > 3400)] *= 0.05
            processed = np.fft.irfft(fft, n=len(data))
            # slight saturation
            processed = np.tanh(processed * 2.2) * 0.8
        elif effect == "radio":
            # Lo-fi AM Radio effect with vintage crunch
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[(freqs < 300) | (freqs > 4500)] *= 0.02
            processed = np.fft.irfft(fft, n=len(data))
            processed = np.clip(processed * 1.5, -0.7, 0.7)
        elif effect == "bass_boost":
            # Warm sub-bass punch
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[freqs < 220] *= 2.2
            processed = np.fft.irfft(fft, n=len(data))
        elif effect == "cyberpunk":
            # Metallic pitch comb-filter
            delay_samples = int(sr * 0.025)
            echo = np.zeros_like(data)
            if len(data) > delay_samples:
                echo[delay_samples:] = data[:-delay_samples] * 0.65
            processed = data + echo
        else:
            processed = data

        # Normalize to prevent clipping
        max_val = np.max(np.abs(processed))
        if max_val > 0:
            processed = (processed / max_val) * 0.95

        sf.write(out_path, processed, sr)
        return out_name
    except Exception as e:
        print("DSP Effect Error:", e)
        return os.path.basename(file_path)

@app.get("/api/config")
def get_config():
    api_key = get_fish_api_key()
    has_fish = bool(api_key and len(api_key) > 10)
    return JSONResponse({
        "fish_audio_configured": has_fish,
        "fish_model": os.getenv("FISH_AUDIO_MODEL", "s2.1-pro-free"),
        "key_preview": f"{api_key[:12]}...{api_key[-4:]}" if has_fish else "Not configured"
    })

VOICES_CACHE_FILE = os.path.join(VOICES_DIR, "us_voices_cache.json")

@app.get("/api/models")
async def list_neural_voices():
    """
    Return exclusively English voices, focusing on USA (en-US) models
    alongside Fish Audio Ultra-Human American voices (served instantly from local cache).
    """
    try:
        if os.path.exists(VOICES_CACHE_FILE):
            with open(VOICES_CACHE_FILE, "r", encoding="utf-8") as f:
                curated = json.load(f)
        else:
            voices = await edge_tts.list_voices()
            curated = []
            for v in voices:
                loc = v.get("Locale", "")
                if loc.startswith("en-"):
                    friendly = v.get("FriendlyName", v.get("ShortName"))
                    name_clean = friendly.replace("Microsoft Server Speech Text to Speech Voice (", "").replace(")", "").replace("Online (Natural)", "").strip()
                    curated.append({
                        "id": v.get("ShortName"),
                        "name": name_clean,
                        "gender": v.get("Gender"),
                        "locale": loc,
                        "suggested": loc == "en-US",
                        "is_fish_pro": False
                    })
            curated.sort(key=lambda x: (x["locale"] != "en-US", x["name"]))
            with open(VOICES_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(curated, f, indent=2)

        return JSONResponse({
            "count": len(curated), 
            "voices": curated, 
            "human_pro_voices": CURATED_HUMAN_VOICES
        })
    except Exception as e:
        return JSONResponse({"count": 0, "voices": [], "human_pro_voices": CURATED_HUMAN_VOICES, "error": str(e)})

@app.get("/api/cloned-voices")
def list_cloned_voices():
    return JSONResponse(get_db())

@app.post("/api/clone")
async def clone_voice(
    name: str = Form(...),
    description: Optional[str] = Form(""),
    reference_text: Optional[str] = Form(""),
    tags: Optional[str] = Form("Fish Audio, Custom Voice"),
    audio_file: UploadFile = File(...)
):
    """
    Clone a voice from an uploaded audio reference sample.
    Stores reference audio & metadata for zero-shot cloning.
    """
    voice_id = str(uuid.uuid4())[:8]
    ext = os.path.splitext(audio_file.filename)[1] or ".mp3"
    filename = f"{voice_id}_ref{ext}"
    save_path = os.path.join(UPLOADS_DIR, filename)
    
    with open(save_path, "wb") as f:
        shutil.copyfileobj(audio_file.file, f)
        
    profile = analyze_audio_profile(save_path)
    base_voice = "en-US-AvaMultilingualNeural" if profile["gender_affinity"] == "Female" else "en-US-AndrewMultilingualNeural"

    voice_entry = {
        "id": voice_id,
        "name": name,
        "description": description or f"Cloned voice ({profile['gender_affinity']}, ~{profile['estimated_pitch_hz']}Hz, duration: {profile['duration']}s)",
        "reference_text": reference_text.strip() if reference_text else "",
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "sample_url": f"/uploads/{filename}",
        "file_path": save_path,
        "base_model": base_voice,
        "engine": "fish_audio",
        "profile": profile,
        "pitch_shift": profile["recommended_pitch_mod"],
        "rate": "+0%",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    db = get_db()
    db.insert(0, voice_entry)
    save_db(db)
    
    return JSONResponse({"success": True, "voice": voice_entry})

class TTSRequest(BaseModel):
    text: str
    voice: str # Cloned Voice ID, Fish Pro ID, or Edge TTS ShortName
    engine: Optional[str] = "auto"
    rate: Optional[str] = "+0%"
    pitch: Optional[str] = "+0Hz"
    volume: Optional[str] = "+0%"
    enhance_audio: Optional[bool] = False
    dsp_effect: Optional[str] = "none" # "none", "reverb", "telephone", "radio", "bass_boost", "cyberpunk"

@app.post("/api/tts")
async def generate_speech(req: TTSRequest):
    """Generate speech using Fish Audio Zero-Shot, Fish Curated Public IDs, or Edge-TTS."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty.")
        
    db = get_db()
    cloned_match = next((v for v in db if v["id"] == req.voice), None)
    pro_match = next((v for v in CURATED_HUMAN_VOICES if v["id"] == req.voice), None)
    api_key = get_fish_api_key()
    
    output_filename = f"gen_{uuid.uuid4().hex[:10]}.mp3"
    output_path = os.path.join(OUTPUTS_DIR, output_filename)
    engine_used = "edge_tts"
    use_fish = False

    # Check 1: Curated Ultra-Human Pro Voice via Fish Audio
    if pro_match and api_key:
        try:
            payload = {
                "text": req.text,
                "reference_id": pro_match["fish_id"],
                "format": "mp3"
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "model": "s2.1-pro-free"
            }
            async with httpx.AsyncClient(timeout=40.0) as client:
                resp = await client.post("https://api.fish.audio/v1/tts", headers=headers, json=payload)
                
            if resp.status_code == 200:
                with open(output_path, "wb") as f:
                    f.write(resp.content)
                engine_used = f"fish_pro_{pro_match['name'].split()[0].lower()}"
                use_fish = True
        except Exception as e:
            print("Fish Pro Voice failed:", e)

    # Check 2: Cloned User Voice via Fish Audio Zero-Shot
    elif cloned_match and api_key and (req.engine in ["auto", "fish_audio"]):
        try:
            ref_path = cloned_match.get("file_path") or os.path.join(UPLOADS_DIR, os.path.basename(cloned_match.get("sample_url", "")))
            if os.path.exists(ref_path):
                with open(ref_path, "rb") as rf:
                    ref_bytes = rf.read()
                
                ref_obj = {"audio": ref_bytes}
                if cloned_match.get("reference_text"):
                    ref_obj["text"] = cloned_match["reference_text"]
                    
                payload = {
                    "text": req.text,
                    "references": [ref_obj],
                    "format": "mp3"
                }
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/msgpack",
                    "model": "s2.1-pro-free"
                }
                async with httpx.AsyncClient(timeout=45.0) as client:
                    resp = await client.post("https://api.fish.audio/v1/tts", headers=headers, content=msgpack.packb(payload))
                    
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    engine_used = "fish_audio_zero_shot"
                    use_fish = True
        except Exception as e:
            print(f"Fish Audio zero-shot call failed: {e}. Falling back to Edge TTS.")

    # Fallback: Edge-TTS Neural Engine
    if not use_fish:
        if cloned_match:
            target_voice = cloned_match["base_model"]
            pitch_val = req.pitch if (req.pitch and req.pitch != "+0Hz" and req.pitch != "+0%") else "+0Hz"
        elif pro_match:
            target_voice = "en-US-AndrewMultilingualNeural" if pro_match["gender"] == "Male" else "en-US-AvaMultilingualNeural"
            pitch_val = "+0Hz"
        else:
            target_voice = req.voice
            pitch_val = req.pitch or "+0Hz"

        clean_pitch = str(pitch_val).strip()
        if clean_pitch.endswith("%"):
            try:
                pct = int(clean_pitch.replace("%", "").replace("+", ""))
                hz_val = int(pct * 1.5)
                clean_pitch = f"{'+' if hz_val >= 0 else ''}{hz_val}Hz"
            except Exception:
                clean_pitch = "+0Hz"
        elif not clean_pitch.endswith("Hz"):
            clean_pitch = f"{clean_pitch}Hz"
            
        if not clean_pitch.startswith("+") and not clean_pitch.startswith("-"):
            clean_pitch = f"+{clean_pitch}"

        rate_str = req.rate or "+0%"
        if not rate_str.endswith("%"):
            rate_str = f"{rate_str}%"
        if not rate_str.startswith("+") and not rate_str.startswith("-"):
            rate_str = f"+{rate_str}"
            
        communicate = edge_tts.Communicate(
            text=req.text,
            voice=target_voice,
            rate=rate_str,
            pitch=clean_pitch,
            volume=req.volume or "+0%"
        )
        await communicate.save(output_path)
        engine_used = "edge_tts_neural"

    # Optional Studio DSP Audio FX (Reverb, Radio, Bass Boost, etc.)
    if req.dsp_effect and req.dsp_effect != "none":
        fx_name = apply_dsp_effects(output_path, req.dsp_effect)
        output_filename = fx_name

    # Optional audio normalization enhancement
    elif req.enhance_audio:
        try:
            data, sr = sf.read(output_path)
            max_val = np.max(np.abs(data))
            if max_val > 0:
                data = (data / max_val) * 0.95
            enhanced_name = f"enhanced_{output_filename}.wav"
            enhanced_path = os.path.join(OUTPUTS_DIR, enhanced_name)
            sf.write(enhanced_path, data, sr)
            output_filename = enhanced_name
        except Exception:
            pass
            
    return JSONResponse({
        "success": True,
        "audio_url": f"/outputs/{output_filename}",
        "filename": output_filename,
        "engine_used": engine_used,
        "voice_name": pro_match["name"] if pro_match else (cloned_match["name"] if cloned_match else req.voice)
    })

# Multi-Speaker Dialogue Generator (Story Studio / Podcast Mode)
class DialogueLine(BaseModel):
    speaker: str # Voice ID
    speaker_name: str
    text: str

class DialogueRequest(BaseModel):
    lines: List[DialogueLine]

@app.post("/api/dialogue")
async def generate_dialogue(req: DialogueRequest):
    """Generate multi-character back-and-forth conversation audio merged into one master track."""
    if not req.lines:
        raise HTTPException(status_code=400, detail="No conversation lines provided.")

    combined_segments = []
    sample_rate = 24000

    for idx, line in enumerate(req.lines):
        line_tts = TTSRequest(text=line.text, voice=line.speaker, engine="auto")
        tts_res = await generate_speech(line_tts)
        line_data = json.loads(tts_res.body.decode("utf-8"))
        
        file_path = os.path.join(OUTPUTS_DIR, line_data["filename"])
        if os.path.exists(file_path):
            audio_data, sr = sf.read(file_path)
            if len(audio_data.shape) > 1:
                audio_data = audio_data.mean(axis=1)
            combined_segments.append(audio_data)
            # Add 0.3s natural breath pause between speakers
            pause = np.zeros(int(sr * 0.35))
            combined_segments.append(pause)
            sample_rate = sr

    if not combined_segments:
        raise HTTPException(status_code=500, detail="Dialogue generation failed.")

    merged_data = np.concatenate(combined_segments)
    dialogue_filename = f"dialogue_{uuid.uuid4().hex[:10]}.wav"
    dialogue_path = os.path.join(OUTPUTS_DIR, dialogue_filename)
    sf.write(dialogue_path, merged_data, sample_rate)

    return JSONResponse({
        "success": True,
        "audio_url": f"/outputs/{dialogue_filename}",
        "filename": dialogue_filename,
        "lines_rendered": len(req.lines)
    })

@app.post("/api/transcribe")
async def transcribe_audio_file(audio_file: UploadFile = File(...)):
    """Auto-transcribe reference audio sample with free high-accuracy neural ASR."""
    try:
        content = await audio_file.read()
        audio_io = io.BytesIO(content)
        data, sample_rate = sf.read(audio_io)
        if len(data.shape) > 1:
            data = data.mean(axis=1)
            
        wav_io = io.BytesIO()
        sf.write(wav_io, data, sample_rate, format='WAV')
        wav_io.seek(0)
        
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            recorded = recognizer.record(source)
            
        transcript = recognizer.recognize_google(recorded)
        return JSONResponse({"success": True, "transcript": transcript})
    except sr.UnknownValueError:
        return JSONResponse({"success": False, "transcript": "", "message": "Speech could not be deciphered clearly."})
    except Exception as e:
        return JSONResponse({"success": False, "transcript": "", "error": str(e)})

@app.delete("/api/cloned-voices/{voice_id}")
def delete_cloned_voice(voice_id: str):
    """Permanently delete a cloned voice and clean up its uploaded reference audio file."""
    db = get_db()
    deleted_voice = next((v for v in db if v["id"] == voice_id), None)
    
    if deleted_voice:
        file_path = deleted_voice.get("file_path")
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print("Could not remove ref audio:", e)
        filtered = [v for v in db if v["id"] != voice_id]
        save_db(filtered)
        return JSONResponse({"success": True, "message": f"Voice '{deleted_voice['name']}' deleted.", "remaining": len(filtered)})
    
    return JSONResponse({"success": False, "message": "Voice not found"}, status_code=404)

# Serve static audio files
app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=True)
