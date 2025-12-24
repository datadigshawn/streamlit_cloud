import streamlit as st
import os
import time
import json
import tempfile
import zipfile
import io
import re
import subprocess
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from pydub import AudioSegment

# Google Cloud & Gemini
from google.cloud import speech
import google.generativeai as genai
from google.oauth2 import service_account

# ==================== 匯入設定檔 ====================
GCP_CREDENTIALS = None
GEMINI_API_KEY = None
CONFIG_LOADED = False

if "gcp_service_account" in st.secrets and "GEMINI_API_KEY" in st.secrets:
    try:
        GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        creds_dict = dict(st.secrets["gcp_service_account"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        GCP_CREDENTIALS = creds_dict
        CONFIG_LOADED = True
    except Exception as e:
        st.error(f"讀取 Secrets 時發生錯誤: {e}")

if not CONFIG_LOADED:
    try:
        from config import GCP_CREDENTIALS as Local_GCP, GEMINI_API_KEY as Local_Gemini
        GCP_CREDENTIALS = Local_GCP
        GEMINI_API_KEY = Local_Gemini
        CONFIG_LOADED = True
    except ImportError:
        pass

if not CONFIG_LOADED:
    st.error("❌ 找不到設定檔！")
    st.stop()

# ==================== 設定與 UI 初始化 ====================
st.set_page_config(page_title="捷運緊急語音轉譯台", page_icon="🎙️", layout="wide")
st.title("🎙️ 捷運緊急語音轉譯工具 (Web版)")

# 捷運專業術語
RAILWAY_PHRASES = [
    "OCC", "行控中心", "呼叫", "軌道", "月台", 
    "Bypass", "VVVF", "異物", "車門", "號車",
    "緊急", "停車", "淨空", "方形鑰匙", "G9", "G7"
]

with st.sidebar:
    st.header("⚙️ 系統設定")
    if GCP_CREDENTIALS: st.success("✅ Google STT 設定完成")
    if GEMINI_API_KEY: st.success("✅ Gemini API 設定完成")
    st.markdown("---")
    mode = st.radio("選擇轉譯模式", ["僅 Google STT", "僅 Gemini", "雙模式 (比較)"])
    st.markdown("---")
    chunk_duration = st.slider("音訊切分長度 (秒)", 30, 58, 50, 5, help="為避免API限制，建議設為50秒")

# ==================== 工具函數 ====================
def format_duration(seconds):
    return str(timedelta(seconds=int(seconds)))

def extract_datetime_from_filename(filename):
    try:
        name = Path(filename).stem
        parts = name.split('_')
        if len(parts) >= 2:
            return datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M%S")
    except:
        pass
    return datetime.now()

# ==================== [核心修正] 轉譯邏輯 ====================

def transcribe_google_stt(audio_path, filename, max_chunk_duration=50):
    """
    修正點：
    1. 切分時輸出 raw (s16le) 格式，而非 wav，以匹配 LINEAR16 設定。
    2. 模型改用 'default'，比 'latest_long' 在短語音和無線電中更穩定。
    """
    try:
        credentials = service_account.Credentials.from_service_account_info(GCP_CREDENTIALS)
        client = speech.SpeechClient(credentials=credentials)
        
        audio_segment = AudioSegment.from_file(audio_path)
        duration_seconds = len(audio_segment) / 1000.0
        
        # 設定辨識參數
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16, # 預期 Raw PCM
            sample_rate_hertz=16000,
            language_code="cmn-Hant-TW",
            enable_automatic_punctuation=True,
            model="default", # 修正：改用標準模型
            use_enhanced=True, # 修正：啟用增強模式
            speech_contexts=[speech.SpeechContext(phrases=RAILWAY_PHRASES, boost=20)]
        )
        
        # 準備切分
        chunk_duration_ms = int(max_chunk_duration * 1000)
        transcripts = []
        
        # 處理單段或多段
        for i in range(0, len(audio_segment), chunk_duration_ms):
            chunk = audio_segment[i:i + chunk_duration_ms]
            
            # [關鍵修正] 轉為 Raw PCM (s16le)，不帶 WAV 檔頭
            chunk_io = io.BytesIO()
            chunk.export(chunk_io, format="s16le", parameters=["-ac", "1", "-ar", "16000"])
            chunk_bytes = chunk_io.getvalue()
            
            try:
                audio = speech.RecognitionAudio(content=chunk_bytes)
                response = client.recognize(config=config, audio=audio)
                
                chunk_text = "".join([result.alternatives[0].transcript for result in response.results])
                transcripts.append(chunk_text)
            except Exception as e:
                print(f"Chunk error: {e}")
                transcripts.append("") # 容錯，忽略該段錯誤

        full_transcript = "".join(transcripts)
        return full_transcript if full_transcript.strip() else "[無法辨識內容]"
        
    except Exception as e:
        return f"[STT 錯誤: {str(e)[:100]}]"

def transcribe_gemini(audio_path):
    """
    修正點：
    1. 直接接收 WAV 檔案，不使用 M4A。
    2. 明確指定 mime_type 為 audio/wav。
    """
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        with open(audio_path, 'rb') as f:
            audio_bytes = f.read()
            
        # 提示詞優化
        prompt = """
        你是一個專業的捷運無線電通訊紀錄員。請將音檔轉錄為逐字稿。
        
        重要規則：
        1. 內容包含台灣捷運術語 (如: OCC, G9, G7, 軌道, 斷路器)。
        2. 這是無線電通話，可能會有雜訊，請根據上下文修正語句。
        3. 輸出格式：純文字，不要任何 markdown 標題或額外說明。
        4. 若有講者代號 (如: 司機員, 行控) 請標示。
        """
        
        response = model.generate_content([
            prompt,
            {
                "mime_type": "audio/wav", # 修正：直接使用 wav
                "data": audio_bytes
            }
        ])
        
        return response.text.strip() if response.text else "[無法辨識內容]"
            
    except Exception as e:
        if "400" in str(e):
            return "[Gemini 錯誤: 格式不支援或檔案損毀]"
        return f"[Gemini 錯誤: {str(e)[:100]}]"

# ==================== 報告生成邏輯 (保持不變) ====================
def generate_merged_content(records):
    lines = []
    lines.append("═" * 60)
    lines.append(f"生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("═" * 60 + "\n")
    for record in records:
        lines.append(f"[{record['filename']}]")
        lines.append(record['transcript'])
        lines.append("-" * 30 + "\n")
    return "\n".join(lines)

def generate_comparison_report(stt_records, gemini_records):
    lines = ["STT vs Gemini 比較報告", "="*40]
    for stt, gem in zip(stt_records, gemini_records):
        lines.append(f"檔案: {stt['filename']}")
        lines.append(f"Google STT: {stt['transcript']}")
        lines.append(f"Gemini    : {gem['transcript']}")
        lines.append("-" * 40)
    return "\n".join(lines)

# ==================== 主程式邏輯 ====================

uploaded_files = st.file_uploader("選擇錄音檔", type=['wav', 'mp3', 'm4a', 'flac'], accept_multiple_files=True)

if st.button("🚀 開始轉譯", type="primary") and uploaded_files:
    stt_records = []
    gemini_records = []
    progress_bar = st.progress(0)
    status_text = st.empty()

    with tempfile.TemporaryDirectory() as temp_dir:
        for i, uploaded_file in enumerate(uploaded_files):
            # 1. 儲存原始檔
            temp_path = os.path.join(temp_dir, uploaded_file.name)
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            # 2. 統一轉檔為 16k WAV (雙模式共用此檔案)
            # 這是最穩定的格式：Google STT (讀 raw data) 和 Gemini (讀 wav file) 都能用
            wav_path = os.path.join(temp_dir, f"converted_{i}.wav")
            
            status_text.text(f"🔄 轉檔中：{uploaded_file.name}...")
            try:
                subprocess.run([
                    'ffmpeg', '-i', temp_path,
                    '-ar', '16000', '-ac', '1', '-acodec', 'pcm_s16le',
                    '-y', wav_path
                ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                st.error(f"轉檔失敗: {uploaded_file.name}")
                continue

            # 取得音訊長度
            try:
                sound = AudioSegment.from_file(wav_path)
                duration_sec = len(sound) / 1000.0
            except:
                duration_sec = 0

            base_dt = extract_datetime_from_filename(uploaded_file.name)

            # 3. 執行辨識
            use_stt = "Google STT" in mode or "雙模式" in mode
            use_gemini = "Gemini" in mode or "雙模式" in mode

            if use_stt:
                status_text.text(f"🎤 STT 辨識中...")
                # 傳入 wav_path，但在函數內部會轉為 raw bytes
                res = transcribe_google_stt(wav_path, uploaded_file.name, chunk_duration)
                stt_records.append({'filename': uploaded_file.name, 'datetime': base_dt, 'duration_sec': duration_sec, 'transcript': res})

            if use_gemini:
                status_text.text(f"🤖 Gemini 辨識中...")
                # 直接傳入 wav_path
                res = transcribe_gemini(wav_path)
                gemini_records.append({'filename': uploaded_file.name, 'datetime': base_dt, 'duration_sec': duration_sec, 'transcript': res})

            progress_bar.progress((i + 1) / len(uploaded_files))

    status_text.success("✨ 處理完成！")

    # ==================== 顯示結果 ====================
    tabs = []
    if use_stt: tabs.append("Google STT")
    if use_gemini: tabs.append("Gemini")
    if use_stt and use_gemini: tabs.append("比較")
    
    tab_objs = st.tabs(tabs)
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        if use_stt:
            with tab_objs[0]:
                txt = generate_merged_content(stt_records)
                st.text_area("STT 結果", txt, height=300)
                zf.writestr("GoogleSTT_Result.txt", txt)
        
        if use_gemini:
            idx = 1 if use_stt else 0
            with tab_objs[idx]:
                txt = generate_merged_content(gemini_records)
                st.text_area("Gemini 結果", txt, height=300)
                zf.writestr("Gemini_Result.txt", txt)

        if use_stt and use_gemini:
            with tab_objs[2]:
                comp_txt = generate_comparison_report(stt_records, gemini_records)
                st.text_area("比較報告", comp_txt, height=300)
                zf.writestr("Comparison_Report.txt", comp_txt)

    st.download_button("📥 下載結果 (ZIP)", zip_buffer.getvalue(), "transcripts.zip", "application/zip")
