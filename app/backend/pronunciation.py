"""AI pronunciation assessment: Azure Speech when configured, local heuristic fallback."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _azure_config() -> tuple[str, str, str]:
    key = os.getenv("AZURE_SPEECH_KEY", "").strip()
    region = os.getenv("AZURE_SPEECH_REGION", "eastasia").strip() or "eastasia"
    language = os.getenv("AZURE_SPEECH_LANGUAGE", "en-US").strip() or "en-US"
    return key, region, language


def _whisper_url() -> str:
    return os.getenv("WHISPER_ASR_URL", "").strip()


def _provider_mode() -> str:
    return (os.getenv("PRONUNCIATION_PROVIDER", "auto") or "auto").strip().lower()


def provider_status() -> dict[str, Any]:
    key, region, language = _azure_config()
    whisper = bool(_whisper_url())
    mode = _provider_mode()
    azure = bool(key)
    if mode == "azure" or (mode == "auto" and azure):
        active = "azure"
        note = "Azure Pronunciation Assessment（音素级）"
    elif mode == "whisper" or (mode == "auto" and whisper):
        active = "whisper+align"
        note = "自建 Whisper ASR + 词级对齐纠音（无需 Azure 密钥）"
    elif mode in {"browser", "browser-asr"} or mode == "auto":
        active = "browser-asr+local"
        note = "浏览器语音识别词级纠音；无识别结果时回退本地启发式"
    else:
        active = "local"
        note = "仅本地启发式评分"
    return {
        "provider": active,
        "mode": mode,
        "azure_configured": azure,
        "whisper_configured": whisper,
        "region": region if azure else None,
        "language": language,
        "note": note,
        "supports_browser_asr": True,
        "ladder": ["azure(phoneme)", "whisper+align", "browser-asr", "local"],
    }


def _tokenize(words: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z']+", words.lower()) if w]


def _clamp_score(value: float) -> int:
    return max(0, min(100, int(round(value))))


def convert_to_wav_16k(src: Path) -> Path:
    """Convert any ffmpeg-readable audio to 16kHz mono PCM WAV."""
    dst = Path(tempfile.mkstemp(suffix=".wav")[1])
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-f", "wav", str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        if dst.exists():
            dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg convert failed: {exc}") from exc
    return dst


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True, timeout=10,
        )
        return max(0.0, float(out.strip() or 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _word_feedback_local(words: list[str], seed: bytes) -> list[dict[str, Any]]:
    feedback = []
    for index, word in enumerate(words):
        digest = hashlib.sha256(seed + word.encode() + str(index).encode()).digest()
        score = 55 + digest[0] % 41  # 55-95
        level = "Excellent" if score >= 90 else "Good" if score >= 75 else "Fair" if score >= 60 else "Poor"
        tip = {
            "Excellent": "发音很棒，继续保持。",
            "Good": "整体清晰，注意元音饱满度。",
            "Fair": "可再放慢语速，把重音读清楚。",
            "Poor": "建议逐词跟读，对照标准音标练习。",
        }[level]
        feedback.append({"word": word, "accuracy_score": score, "error_type": "None" if score >= 75 else "Mispronunciation", "level": level, "tip": tip})
    return feedback


def assess_from_transcript(
    reference_text: str,
    transcript: str,
    duration_seconds: float | None = None,
    audio_path: Path | None = None,
) -> dict[str, Any]:
    """Word-level scoring by comparing browser/ASR transcript to reference text."""
    ref_words = _tokenize(reference_text)
    hyp_words = _tokenize(transcript)
    duration = duration_seconds if duration_seconds and duration_seconds > 0 else (
        probe_duration(audio_path) if audio_path else 0.0
    )
    expected = max(1.2, len(ref_words) * 0.42)
    timing = timing_score(duration, expected) if duration else 70

    hyp_set = set(hyp_words)
    # greedy alignment for feedback
    feedback = []
    matched = 0
    hyp_i = 0
    for word in ref_words:
        hit = False
        error = "Omission"
        score = 35
        # look ahead a few words in hypothesis
        for j in range(hyp_i, min(hyp_i + 3, len(hyp_words))):
            if hyp_words[j] == word:
                hit = True
                error = "None"
                score = 95
                hyp_i = j + 1
                matched += 1
                break
            if hyp_words[j][:2] == word[:2] and abs(len(hyp_words[j]) - len(word)) <= 2:
                hit = True
                error = "Mispronunciation"
                score = 68
                hyp_i = j + 1
                matched += 0.5
                break
        if not hit and word in hyp_set:
            error = "Mispronunciation"
            score = 60
            matched += 0.4
        level = "Excellent" if score >= 90 else "Good" if score >= 75 else "Fair" if score >= 60 else "Poor"
        tip = "发音匹配良好。" if error == "None" else f"参考词 “{word}” 识别为偏差或漏读，请再跟读。"
        feedback.append({"word": word, "accuracy_score": score, "error_type": error, "level": level, "tip": tip})

    total = max(len(ref_words), 1)
    accuracy = _clamp_score(100 * matched / total)
    completeness = _clamp_score(100 * len([w for w in feedback if w["error_type"] != "Omission"]) / total)
    fluency = _clamp_score(0.6 * timing + 0.4 * accuracy)
    overall = _clamp_score(0.5 * accuracy + 0.25 * fluency + 0.25 * completeness)
    tips = "已使用浏览器语音识别做词级纠音。配置 Azure 后可升级为音素级评分。"
    if not hyp_words:
        tips = "未识别到有效英文内容，请靠近麦克风、使用英文朗读后重试。"
        overall = min(overall, 40)
    return {
        "provider": "browser-asr",
        "overall_score": overall,
        "accuracy_score": accuracy,
        "fluency_score": fluency,
        "completeness_score": completeness,
        "prosody_score": _clamp_score((fluency + timing) / 2),
        "recognized_text": transcript.strip(),
        "words": feedback,
        "tips": tips,
        "duration_seconds": round(duration or 0, 2),
    }


def assess_local(audio_path: Path, reference_text: str, duration_seconds: float | None = None) -> dict[str, Any]:
    words = _tokenize(reference_text)
    duration = duration_seconds if duration_seconds and duration_seconds > 0 else probe_duration(audio_path)
    expected = max(1.2, len(words) * 0.42)
    size = audio_path.stat().st_size if audio_path.exists() else 0
    timing = timing_score(duration, expected)
    if size < 800 or duration < 0.4:
        overall = 20
        tips = "录音过短或音量过低，请重新朗读完整句子。"
    else:
        energy = 70 + min(25, size / 4000)
        overall = _clamp_score(0.55 * timing + 0.45 * energy)
        tips = "本地启发式评分（无识别文本）。请允许浏览器语音识别，或配置 Azure/Whisper 获得真实纠音。"

    feedback = _word_feedback_local(words, (str(size) + reference_text).encode())
    if feedback:
        accuracy = _clamp_score(sum(w["accuracy_score"] for w in feedback) / len(feedback))
    else:
        accuracy = overall
    fluency = _clamp_score(overall * 0.96)
    completeness = 100 if duration >= expected * 0.55 else _clamp_score(100 * duration / expected)
    prosody = _clamp_score((fluency + timing) / 2)
    # 本地启发不可作为作业通关依据（防止噪声/空文件刷分）
    overall = min(overall, 49)
    accuracy = min(accuracy, 49)
    fluency = min(fluency, 49)

    return {
        "provider": "local",
        "overall_score": overall,
        "accuracy_score": accuracy,
        "fluency_score": fluency,
        "completeness_score": completeness,
        "prosody_score": prosody,
        "recognized_text": "",
        "words": feedback,
        "tips": tips + " 本地分上限 49，作业需 Azure/Whisper/浏览器识别才能及格。",
        "duration_seconds": round(duration, 2),
        "pass_eligible": False,
    }


def timing_score(duration: float, expected: float) -> float:
    ratio = duration / max(expected, 0.1)
    if 0.7 <= ratio <= 1.5:
        return 92
    if 0.5 <= ratio < 0.7 or 1.5 < ratio <= 2.0:
        return 75
    return 55


def assess_azure(wav_path: Path, reference_text: str) -> dict[str, Any]:
    key, region, language = _azure_config()
    if not key:
        raise RuntimeError("AZURE_SPEECH_KEY not configured")
    params = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Word",
        "Dimension": "Comprehensive",
        "EnableProsodyAssessment": "True",
        "EnableMiscue": "True",
    }
    pron_header = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    query = urllib.parse.urlencode({"language": language, "format": "detailed"})
    url = f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1?{query}"
    data = wav_path.read_bytes()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Ocp-Apim-Subscription-Key", key)
    req.add_header("Content-Type", "audio/wav; codecs=audio/pcm; samplerate=16000")
    req.add_header("Accept", "application/json")
    req.add_header("Pronunciation-Assessment", pron_header)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Azure pronunciation failed ({exc.code}): {body[:300]}") from exc

    if payload.get("RecognitionStatus") != "Success":
        raise RuntimeError(f"Azure recognition status: {payload.get('RecognitionStatus')}")

    nbest = (payload.get("NBest") or [{}])[0]
    words_out = []
    for item in nbest.get("Words") or []:
        score = item.get("AccuracyScore")
        error = item.get("ErrorType") or "None"
        level = "Excellent" if (score or 0) >= 90 else "Good" if (score or 0) >= 75 else "Fair" if (score or 0) >= 60 else "Poor"
        tip = "发音准确。" if error == "None" and (score or 0) >= 75 else f"注意纠正：{item.get('Word')}（{error}）"
        words_out.append({
            "word": item.get("Word"),
            "accuracy_score": _clamp_score(score or 0),
            "error_type": error,
            "level": level,
            "tip": tip,
        })

    overall = _clamp_score(nbest.get("PronScore") or nbest.get("AccuracyScore") or 0)
    accuracy = _clamp_score(nbest.get("AccuracyScore") or overall)
    fluency = _clamp_score(nbest.get("FluencyScore") or overall)
    completeness = _clamp_score(nbest.get("CompletenessScore") or overall)
    prosody = nbest.get("ProsodyScore")
    tips = "Azure 纠音完成。" if overall >= 80 else "建议对照低分单词慢读两遍，注意重音与连读。"
    return {
        "provider": "azure",
        "overall_score": overall,
        "accuracy_score": accuracy,
        "fluency_score": fluency,
        "completeness_score": completeness,
        "prosody_score": _clamp_score(prosody) if prosody is not None else None,
        "recognized_text": payload.get("DisplayText") or nbest.get("Display") or "",
        "words": words_out,
        "tips": tips,
        "raw_status": payload.get("RecognitionStatus"),
    }


def transcribe_whisper(audio_path: Path) -> str:
    """Call self-hosted Whisper-compatible HTTP API. Expect JSON {text: "..."} or {transcript:"..."}."""
    url = _whisper_url()
    if not url:
        raise RuntimeError("WHISPER_ASR_URL not configured")
    wav = convert_to_wav_16k(audio_path)
    try:
        boundary = f"----AiEnglish{uuid_hex()}"
        file_bytes = wav.read_bytes()
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="language"\r\n\r\n',
            b"en\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        token = os.getenv("WHISPER_ASR_TOKEN", "").strip()
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload.get("text") or payload.get("transcript") or payload.get("DisplayText") or ""
        if isinstance(payload.get("segments"), list) and not text:
            text = " ".join(str(s.get("text", "")) for s in payload["segments"])
        if not str(text).strip():
            raise RuntimeError(f"Whisper empty transcript: {payload}")
        return str(text).strip()
    finally:
        if wav.exists():
            wav.unlink(missing_ok=True)


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex


def assess_audio(
    audio_path: Path,
    reference_text: str,
    duration_seconds: float | None = None,
    transcript: str | None = None,
    allow_client_transcript: bool = True,
) -> dict[str, Any]:
    reference_text = (reference_text or "").strip()
    if not reference_text:
        raise ValueError("reference_text is required")
    if not audio_path.exists():
        raise FileNotFoundError(str(audio_path))

    mode = _provider_mode()
    key, _, _ = _azure_config()
    whisper = bool(_whisper_url())
    errors: list[str] = []
    wav_path = None
    # 作业等高风险场景可关闭客户端 transcript，避免粘贴参考文刷分
    effective_transcript = (transcript or "").strip() if allow_client_transcript else ""
    probed = probe_duration(audio_path) or 0.0
    duration_seconds = float(probed or duration_seconds or 0)

    def with_browser_or_local(prefix: str = "") -> dict[str, Any]:
        if effective_transcript:
            result = assess_from_transcript(reference_text, effective_transcript, duration_seconds, audio_path)
            if prefix:
                result["tips"] = f"{prefix}{result.get('tips') or ''}"
            if errors:
                result["fallback_errors"] = errors
            return result
        local = assess_local(audio_path, reference_text, duration_seconds)
        if prefix:
            local["tips"] = f"{prefix}{local.get('tips') or ''}"
        if not allow_client_transcript and (transcript or "").strip():
            local["tips"] = (local.get("tips") or "") + "（已忽略客户端识别文本，防作弊）"
        if errors:
            local["fallback_errors"] = errors
        return local

    try:
        # 1) Azure phoneme (preferred when configured / forced)
        if mode == "azure" or (mode == "auto" and key):
            if not key and mode == "azure":
                errors.append("AZURE_SPEECH_KEY missing")
            else:
                try:
                    wav_path = convert_to_wav_16k(audio_path)
                    result = assess_azure(wav_path, reference_text)
                    result["duration_seconds"] = duration_seconds
                    return result
                except Exception as azure_exc:
                    errors.append(f"azure:{azure_exc}")
                    if mode == "azure":
                        return with_browser_or_local("Azure 失败已回退：")

        # 2) Self-hosted Whisper ASR + alignment (no Azure key needed)
        if mode in {"whisper", "whisper+align"} or (mode == "auto" and whisper):
            if not whisper and mode.startswith("whisper"):
                errors.append("WHISPER_ASR_URL missing")
            else:
                try:
                    hyp = transcribe_whisper(audio_path)
                    result = assess_from_transcript(reference_text, hyp, duration_seconds, audio_path)
                    result["provider"] = "whisper+align"
                    result["tips"] = "已使用自建 Whisper 识别 + 词级对齐纠音（可替代 Azure 密钥方案）。"
                    if errors:
                        result["fallback_errors"] = errors
                    return result
                except Exception as whisper_exc:
                    errors.append(f"whisper:{whisper_exc}")
                    if mode.startswith("whisper"):
                        return with_browser_or_local("Whisper 失败已回退：")

        # 3) Browser transcript / local
        if mode == "local":
            return assess_local(audio_path, reference_text, duration_seconds)
        return with_browser_or_local(("上级引擎失败，已回退：" if errors else ""))
    finally:
        if wav_path and wav_path.exists():
            wav_path.unlink(missing_ok=True)
