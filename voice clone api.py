"""
Voice Clone API — XTTS-v2 (Coqui TTS)
=======================================
Self-hosted voice cloning server. Clone any voice from a short audio sample
(6-30 seconds) and synthesize new speech in that voice.

REQUIREMENTS (install first):
    pip install TTS flask torch torchaudio

NOTE ON HARDWARE:
    - Works on CPU but is SLOW (can take 10-60s per sentence).
    - GPU strongly recommended (CUDA). Rent one on RunPod / Vast.ai if you
      don't have one. Render's free/starter tiers are NOT enough for this.
    - Model download is ~2GB on first run.

NOTE ON KHMER:
    XTTS-v2 does not officially support Khmer. It will still try to speak
    the text using its closest phonetic guess, which may sound off. For
    better Khmer results you'd need to fine-tune on a Khmer voice dataset
    (see the fine_tune_notes.md file for pointers). For now this works best
    with English/French/Chinese/etc. reference clips + those languages.

USAGE:
    1. Run this file:  python voice_clone_api.py
    2. It starts a Flask server on http://0.0.0.0:5000
    3. Call it from your bot (see call_from_bot_example.py)

ENDPOINTS:
    POST /clone
        form-data:
            text        (str, required)   - text to speak
            language    (str, optional)   - default "en"
            speaker_wav (file, required)  - reference voice sample (wav/mp3)
        returns: audio/wav file

    GET /health
        returns: {"status": "ok"}
"""

import os
import io
import tempfile
import logging

from flask import Flask, request, send_file, jsonify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice_clone_api")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Lazy model loading — the model is heavy, so we only load it once, on first
# request, not at import time. This also lets /health respond instantly.
# ---------------------------------------------------------------------------
_tts_model = None


def get_model():
    global _tts_model
    if _tts_model is None:
        logger.info("Loading XTTS-v2 model (first request, this can take a while)...")
        from TTS.api import TTS
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Using device: {device}")

        _tts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
        logger.info("Model loaded.")
    return _tts_model


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/clone", methods=["POST"])
def clone_voice():
    text = request.form.get("text", "").strip()
    language = request.form.get("language", "en").strip()

    if not text:
        return jsonify({"error": "Missing 'text' field"}), 400

    if "speaker_wav" not in request.files:
        return jsonify({"error": "Missing 'speaker_wav' file (reference voice sample)"}), 400

    speaker_file = request.files["speaker_wav"]

    # Save the uploaded reference clip to a temp file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as ref_tmp:
        speaker_file.save(ref_tmp.name)
        ref_path = ref_tmp.name

    out_path = None
    try:
        model = get_model()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out_tmp:
            out_path = out_tmp.name

        logger.info(f"Synthesizing ({language}): {text[:60]}...")
        model.tts_to_file(
            text=text,
            speaker_wav=ref_path,
            language=language,
            file_path=out_path,
        )

        return send_file(out_path, mimetype="audio/wav", as_attachment=True, download_name="cloned_voice.wav")

    except Exception as e:
        logger.exception("Synthesis failed")
        return jsonify({"error": str(e)}), 500

    finally:
        # cleanup temp files
        if os.path.exists(ref_path):
            os.remove(ref_path)
        # NOTE: out_path is streamed by send_file; Flask cleans it up after
        # sending in most setups, but if you notice files piling up on disk,
        # add a scheduled cleanup job for the temp directory.


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
