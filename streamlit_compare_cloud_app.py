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

# ==================== 設定載入（支援 Streamlit Cloud 和本地開發） ====================
CONFIG_LOADED = False
GCP_CREDENTIALS = None
GEMINI_API_KEY = None

# 方式 1：優先使用 Streamlit Secrets（適用於雲端部署）
if hasattr(st, 'secrets'):
    try:
        # 讀取 Gemini API Key
        if "GEMINI_API_KEY" in st.secrets:
            GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
        
        # 讀取 Google Cloud 憑證
        if "gcp_service_account" in st.secrets:
            GCP_CREDENTIALS = dict(st.secrets["gcp_service_account"])
        
        # 檢查是否成功載入
        if GEMINI_API_KEY and GCP_CREDENTIALS:
            CONFIG_LOADED = True
    except Exception as e:
        pass  # 如果 Secrets 讀取失敗，繼續嘗試 config.py

# 方式 2：使用本地 config.py（適用於本地開發）
if not CONFIG_LOADED:
    try:
        from config import GCP_CREDENTIALS as LOCAL_GCP, GEMINI_API_KEY as LOCAL_GEMINI
        GCP_CREDENTIALS = LOCAL_GCP
        GEMINI_API_KEY = LOCAL_GEMINI
        CONFIG_LOADED = True
    except ImportError:
        pass

# 檢查是否成功載入設定
if not CONFIG_LOADED or not GCP_CREDENTIALS or not GEMINI_API_KEY:
    st.error("❌ 無法載入 API 設定")
    st.info("""
    請確認以下設定之一已完成：
    
    **雲端部署（Streamlit Cloud）：**
    - 在 Settings → Secrets 中設定 GEMINI_API_KEY 和 gcp_service_account
    
    **本地開發：**
    - 建立 config.py 檔案並設定 GEMINI_API_KEY 和 GCP_CREDENTIALS
    """)
    st.stop()

# ==================== 設定與 UI 初始化 ====================
st.set_page_config(page_title="捷運緊急語音轉譯台", page_icon="🎙️", layout="wide")

st.title("🎙️ 捷運緊急語音轉譯工具 (Web版)")
st.markdown("上傳無線電錄音檔，自動透過 Google STT 與 Gemini 產出逐字稿並合併記錄。")

# 快速開始提示
st.info("💡 **快速開始：** 選擇模式 → 上傳音訊 → 開始轉譯 → 下載結果")

# 捷運專業術語 (保留原本設定)
RAILWAY_PHRASES = [
    "OCC", "行控中心", "呼叫", "軌道", "月台", 
    "Bypass", "VVVF", "異物", "車門", "號車",
    "緊急", "停車", "淨空", "方形鑰匙"
]

# ==================== 側邊欄：設定區 ====================
with st.sidebar:
    st.header("⚙️ 系統設定")
    
    # 顯示憑證狀態
    st.subheader("🔑 憑證狀態")
    
    # 顯示憑證來源
    config_source = "Streamlit Secrets" if hasattr(st, 'secrets') and "GEMINI_API_KEY" in st.secrets else "本地 config.py"
    st.caption(f"來源：{config_source}")
    
    # Google Cloud STT
    if GCP_CREDENTIALS and GCP_CREDENTIALS.get('project_id'):
        st.success(f"✅ Google STT: {GCP_CREDENTIALS.get('project_id')}")
    else:
        st.error("❌ Google STT 憑證未設定")
    
    # Gemini
    if GEMINI_API_KEY and len(GEMINI_API_KEY) > 10:
        st.success(f"✅ Gemini API: {GEMINI_API_KEY[:8]}...")
    else:
        st.error("❌ Gemini API Key 未設定")

    # 模式選擇
    st.markdown("---")
    mode = st.radio("選擇轉譯模式", ["僅 Google STT", "僅 Gemini", "雙模式 (比較)"])
    
    # 進階設定
    st.markdown("---")
    st.subheader("🔧 進階設定")
    chunk_duration = st.slider("音訊切分長度 (秒)", 30, 60, 50, 5, 
                                help="長音訊會自動切分為此長度進行辨識")

# ==================== 工具函數 ====================

def check_audio_quality(file_path):
    """
    檢查音訊品質並返回警告訊息
    """
    try:
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        
        streams = data.get('streams', [])
        if not streams:
            return None, []
        
        audio_stream = streams[0]
        codec = audio_stream.get('codec_name', '')
        sample_rate = int(audio_stream.get('sample_rate', 0))
        
        warnings = []
        needs_conversion = False
        
        # 檢查編碼格式
        if codec == 'adpcm_ima_wav':
            warnings.append("⚠️ 使用壓縮格式（ADPCM），辨識率可能較低")
            needs_conversion = True
        
        # 檢查取樣率
        if sample_rate < 16000:
            warnings.append(f"⚠️ 取樣率偏低（{sample_rate} Hz），建議 16000 Hz 以上")
            needs_conversion = True
        
        return needs_conversion, warnings
        
    except Exception as e:
        return False, []

def convert_audio_to_standard_format(input_path, output_path, target_format='wav'):
    """
    轉換音訊為標準格式
    
    Parameters:
    - input_path: 輸入音訊路徑
    - output_path: 輸出音訊路徑
    - target_format: 目標格式 ('wav' for STT, 'm4a' for Gemini)
    
    Returns:
    - success: 是否成功
    - message: 訊息
    """
    try:
        if target_format == 'wav':
            # Google STT 最佳格式：PCM 16kHz 單聲道
            cmd = [
                'ffmpeg', '-i', input_path,
                '-ar', '16000',  # 取樣率 16kHz
                '-ac', '1',      # 單聲道
                '-acodec', 'pcm_s16le',  # PCM 編碼
                '-y', output_path
            ]
        elif target_format == 'm4a':
            # Gemini 最佳格式：AAC 16kHz 單聲道
            cmd = [
                'ffmpeg', '-i', input_path,
                '-ar', '16000',
                '-ac', '1',
                '-acodec', 'aac',
                '-b:a', '128k',  # 位元率 128kbps
                '-y', output_path
            ]
        else:
            return False, f"不支援的格式：{target_format}"
        
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=120
        )
        
        if result.returncode == 0:
            return True, f"已轉換為標準 {target_format.upper()} 格式"
        else:
            return False, f"轉換失敗：{result.stderr[:200]}"
            
    except subprocess.TimeoutExpired:
        return False, "轉換超時"
    except Exception as e:
        return False, f"轉換錯誤：{str(e)[:200]}"

def get_audio_info(file_path):
    """取得音訊長度 (秒) - 使用 ffprobe 避免 ADPCM 解碼問題"""
    try:
        # 先檢查檔案是否存在
        if not os.path.exists(file_path):
            st.error(f"檔案不存在: {file_path}")
            return 0
        
        # 檢查檔案大小
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            st.error(f"檔案是空的: {file_path}")
            return 0
        
        # 使用 ffprobe 讀取音訊資訊（避免 pydub 的 ADPCM 問題）
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            st.error(f"無法讀取 {file_path} 的資訊")
            return 0
        
        data = json.loads(result.stdout)
        duration = float(data.get('format', {}).get('duration', 0))
        
        if duration == 0:
            st.warning(f"音訊長度為 0: {file_path}")
        
        return duration
        
    except subprocess.TimeoutExpired:
        st.error(f"讀取音訊超時: {file_path}")
        return 0
    except Exception as e:
        st.error(f"無法讀取音訊資訊: {str(e)}")
        return 0

def format_duration(seconds):
    """格式化時長為 HH:MM:SS"""
    return str(timedelta(seconds=int(seconds)))

def extract_datetime_from_filename(filename):
    """從檔名解析時間，若失敗則回傳現在時間"""
    try:
        name = Path(filename).stem
        parts = name.split('_')
        if len(parts) >= 2:
            date_str = parts[0]
            time_str = parts[1]
            return datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
    except:
        pass
    return datetime.now()

# ==================== 轉譯核心邏輯 ====================

def transcribe_google_stt(audio_path, filename, max_chunk_duration=50):
    """
    使用 Google STT 進行轉譯
    自動檢測並轉換音訊格式，處理長音訊切分
    
    Parameters:
    - audio_path: 音訊檔案路徑
    - filename: 檔案名稱（用於錯誤訊息）
    - max_chunk_duration: 最大切分長度（秒），預設 50 秒
    """
    try:
        # 步驟 1：檢查音訊品質
        needs_conversion, warnings = check_audio_quality(audio_path)
        
        # 步驟 2：如果需要，自動轉換為標準格式
        working_path = audio_path
        if needs_conversion:
            temp_converted = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
            temp_converted.close()
            
            success, message = convert_audio_to_standard_format(
                audio_path, 
                temp_converted.name, 
                target_format='wav'
            )
            
            if success:
                working_path = temp_converted.name
            else:
                # 轉換失敗，仍嘗試使用原檔案
                working_path = audio_path
        
        # 步驟 3：建立 Google STT 客戶端
        credentials = service_account.Credentials.from_service_account_info(GCP_CREDENTIALS)
        client = speech.SpeechClient(credentials=credentials)
        
        # 步驟 4：讀取音訊並檢查長度和大小
        audio_segment = AudioSegment.from_file(working_path)
        duration_seconds = len(audio_segment) / 1000.0
        file_size_mb = os.path.getsize(working_path) / (1024 * 1024)
        
        # 判斷是否需要切分（保守估計：超過設定長度或 8MB 就切分）
        max_size_mb = 8
        
        # 設定辨識參數（所有模式共用）
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,  # 16-bit PCM 編碼
            sample_rate_hertz=16000,  # 採樣率 16kHz
            language_code="cmn-Hant-TW",  # 台灣繁體中文
            enable_automatic_punctuation=True,  # 自動標點符號
            model="latest_long",  # 使用最新的長音訊模型
            speech_contexts=[speech.SpeechContext(phrases=RAILWAY_PHRASES, boost=15)]  # 捷運專業術語加權
        )
        
        if duration_seconds <= max_chunk_duration and file_size_mb <= max_size_mb:
            # ========== 短音訊：直接辨識 ==========
            with open(working_path, 'rb') as f:
                content = f.read()
            
            audio = speech.RecognitionAudio(content=content)
            response = client.recognize(config=config, audio=audio)
            
            # 組合結果
            transcript = "".join([result.alternatives[0].transcript for result in response.results])
            result = transcript if transcript else "[無法辨識內容]"
        
        else:
            # ========== 長音訊：切分處理 ==========
            chunk_duration_ms = int(max_chunk_duration * 1000)  # 轉換為毫秒
            chunks = []
            transcripts = []
            
            # 切分音訊（每段最多 max_chunk_duration 秒）
            for i in range(0, len(audio_segment), chunk_duration_ms):
                chunk = audio_segment[i:i + chunk_duration_ms]
                chunks.append(chunk)
            
            # 逐段辨識
            for idx, chunk in enumerate(chunks):
                try:
                    # 將切分的音訊轉為 WAV bytes
                    chunk_io = io.BytesIO()
                    chunk.export(
                        chunk_io, 
                        format="wav", 
                        codec="pcm_s16le",
                        parameters=["-ar", "16000", "-ac", "1"]
                    )
                    chunk_bytes = chunk_io.getvalue()
                    
                    # 檢查切片大小（避免單段過大）
                    chunk_size_mb = len(chunk_bytes) / (1024 * 1024)
                    if chunk_size_mb > max_size_mb:
                        transcripts.append(f"[第{idx+1}段過大，跳過]")
                        continue
                    
                    # 辨識該段
                    audio = speech.RecognitionAudio(content=chunk_bytes)
                    response = client.recognize(config=config, audio=audio)
                    
                    # 提取辨識結果
                    chunk_transcript = "".join([result.alternatives[0].transcript for result in response.results])
                    
                    if chunk_transcript:
                        transcripts.append(chunk_transcript)
                    else:
                        transcripts.append("")  # 該段無內容，但不標記為錯誤
                    
                except Exception as chunk_error:
                    # 單段失敗不影響其他段
                    error_msg = str(chunk_error)
                    if "quota" in error_msg.lower():
                        transcripts.append(f"[第{idx+1}段: 配額不足]")
                    else:
                        transcripts.append(f"[第{idx+1}段辨識失敗]")
            
            # 合併所有段落（自動加上連接符號）
            full_transcript = "".join(transcripts)
            
            if not full_transcript or full_transcript.strip() == "":
                result = "[無法辨識內容]"
            else:
                result = full_transcript
        
        # 清理暫存檔
        if needs_conversion and working_path != audio_path:
            try:
                os.unlink(working_path)
            except:
                pass
        
        return result
        
    except Exception as e:
        error_msg = str(e)
        # 提供更友善的錯誤訊息
        if "quota" in error_msg.lower():
            return "[STT 錯誤: API 配額不足，請稍後再試]"
        elif "invalid" in error_msg.lower():
            return "[STT 錯誤: 音訊格式無效]"
        elif "duration limit" in error_msg.lower() or "too long" in error_msg.lower():
            return "[STT 錯誤: 音訊過長，請調整切分設定]"
        else:
            return f"[STT 錯誤: {error_msg[:100]}]"

def transcribe_gemini(audio_path):
    """
    使用 Gemini 進行轉譯
    自動檢測並轉換音訊格式為 Gemini 最佳格式（M4A/AAC）
    """
    try:
        # 步驟 1：檢查音訊品質
        needs_conversion, warnings = check_audio_quality(audio_path)
        
        # 步驟 2：自動轉換為 Gemini 最佳格式（M4A）
        # 即使不需要轉換，也統一轉成 M4A 確保相容性
        temp_converted = tempfile.NamedTemporaryFile(delete=False, suffix='.m4a')
        temp_converted.close()
        
        success, message = convert_audio_to_standard_format(
            audio_path, 
            temp_converted.name, 
            target_format='m4a'
        )
        
        if not success:
            return f"[Gemini 錯誤: 音訊轉換失敗 - {message}]"
        
        working_path = temp_converted.name
        
        # 步驟 3：設定 Gemini API
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        # 步驟 4：檢查檔案大小
        file_size_mb = os.path.getsize(working_path) / (1024 * 1024)
        
        # inline 模式的檔案大小限制
        if file_size_mb > 15:
            try:
                os.unlink(working_path)
            except:
                pass
            return f"[Gemini 錯誤: 轉換後檔案過大 ({file_size_mb:.1f}MB，限制 15MB)]"
        
        # 步驟 5：讀取音訊檔案
        with open(working_path, 'rb') as f:
            audio_bytes = f.read()
        
        # 統一使用 audio/mp4 MIME type（M4A 的標準 MIME type）
        mime_type = 'audio/mp4'
        
        # 定義轉譯提示詞
        prompt = """
        請將這段無線電通訊轉為逐字稿。
        規則：
        1. 這是台灣捷運通訊，保留術語(OCC, Bypass, VVVF等)。
        2. 保留數字和英文代號。
        3. 直接輸出文字，不要加引言或說明。
        4. 如果有多段對話，請用句號或換行分隔。
        5. 盡可能完整辨識所有內容。
        """
        
        # 使用 inline 方式傳送音訊
        try:
            response = model.generate_content([
                prompt,
                {
                    "mime_type": mime_type,
                    "data": audio_bytes
                }
            ])
            
            # 清理暫存檔
            try:
                os.unlink(working_path)
            except:
                pass
            
            if not response or not response.text:
                return "[無法辨識內容]"
            
            return response.text.strip()
            
        except Exception as gen_error:
            # 清理暫存檔
            try:
                os.unlink(working_path)
            except:
                pass
            
            error_str = str(gen_error)
            if "quota" in error_str.lower() or "429" in error_str:
                return "[Gemini 錯誤: API 配額不足]"
            elif "unsupported" in error_str.lower() or "invalid" in error_str.lower():
                return f"[Gemini 錯誤: 格式問題 - {error_str[:100]}]"
            elif "safety" in error_str.lower():
                return "[Gemini 錯誤: 內容被安全過濾器阻擋]"
            else:
                return f"[Gemini 生成錯誤: {error_str[:120]}]"
        
    except Exception as e:
        error_msg = str(e)
        # 提供更友善的錯誤訊息
        if "api key" in error_msg.lower() or "api_key" in error_msg.lower():
            return "[Gemini 錯誤: API Key 無效或未設定]"
        elif "quota" in error_msg.lower() or "429" in error_msg:
            return "[Gemini 錯誤: API 配額不足]"
        elif "permission" in error_msg.lower():
            return "[Gemini 錯誤: API 權限不足]"
        else:
            return f"[Gemini 錯誤: {error_msg[:150]}]"

# ==================== 合併邏輯 ====================

def generate_merged_content(records):
    """產生合併後的文字內容字串"""
    lines = []
    total_sec = sum(r['duration_sec'] for r in records)
    
    # 標題區
    lines.append("═" * 60)
    lines.append("           無線電通訊完整記錄 - 合併轉譯檔")
    lines.append("═" * 60)
    lines.append(f"生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"總時長：{format_duration(total_sec)}")
    lines.append(f"檔案數量：{len(records)} 個")
    lines.append("═" * 60 + "\n")
    lines.append(f"{'日期':<12} {'時間':<12} {'發話內容'}")
    lines.append("─" * 60)

    speaker_toggle = True  # 交替顯示講者
    
    # 依時間排序記錄
    records.sort(key=lambda x: x['datetime'])

    for record in records:
        # 切分對話內容（依據標點符號）
        text = record['transcript']
        if text.startswith('['):  # 錯誤訊息不切分
            dialogues = []
        else:
            dialogues = [s.strip() for s in re.split(r'[。！？\n]+', text) if s.strip()]

        # 若無對話或無法切分，整段視為一句
        if not dialogues:
            dialogues = [text]

        # 計算時間戳記（平均分配到每句對話）
        base_time = record['datetime']
        interval = record['duration_sec'] / max(len(dialogues), 1)
        
        # 輸出每句對話
        for i, dialogue in enumerate(dialogues):
            ts = base_time + timedelta(seconds=int(i * interval))
            spk = "講者A" if speaker_toggle else "講者B"
            lines.append(f"{ts.strftime('%Y-%m-%d'):<12} {ts.strftime('%H:%M:%S'):<12} {spk}: {dialogue}")
            speaker_toggle = not speaker_toggle  # 切換講者
        
        # 來源資訊
        lines.append("\n" + "─" * 60)
        lines.append(f"[來源: {record['filename']} | 長度: {format_duration(record['duration_sec'])}]")
        lines.append("─" * 60 + "\n")

    # 結尾
    lines.append("═" * 60)
    lines.append("                        記錄結束")
    lines.append("═" * 60)
    return "\n".join(lines)

def generate_comparison_report(stt_records, gemini_records):
    """產生雙模式比較報告"""
    lines = []
    
    # 標題區
    lines.append("═" * 80)
    lines.append("           Google STT vs Gemini 2.0 - 轉譯結果比較報告")
    lines.append("═" * 80)
    lines.append(f"生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"檔案數量：{len(stt_records)} 個")
    lines.append("═" * 80 + "\n")
    
    # 防止空列表
    if not stt_records or not gemini_records:
        lines.append("⚠️  沒有可比較的記錄")
        lines.append("=" * 80)
        return "\n".join(lines)
    
    # 統計資訊
    total_stt_chars = sum(len(r['transcript']) for r in stt_records)
    total_gemini_chars = sum(len(r['transcript']) for r in gemini_records)
    
    lines.append("📊 整體統計")
    lines.append("─" * 80)
    lines.append(f"Google STT 總字元數：{total_stt_chars}")
    lines.append(f"Gemini 總字元數：{total_gemini_chars}")
    
    # 計算平均差異（防止除以零）
    if len(stt_records) > 0:
        avg_diff = abs(total_stt_chars - total_gemini_chars) / len(stt_records)
        lines.append(f"平均字元差異：{avg_diff:.1f} 字元/檔")
    
    lines.append("")
    
    # 逐檔比較
    for i, (stt_rec, gemini_rec) in enumerate(zip(stt_records, gemini_records), 1):
        lines.append("=" * 80)
        lines.append(f"檔案 {i}: {stt_rec['filename']}")
        lines.append(f"時長: {format_duration(stt_rec['duration_sec'])}")
        lines.append("=" * 80)
        lines.append("")
        
        # Google STT 結果
        lines.append("【Google STT 結果】")
        lines.append("─" * 80)
        lines.append(stt_rec['transcript'])
        lines.append(f"(字元數: {len(stt_rec['transcript'])})")
        lines.append("")
        
        # Gemini 結果
        lines.append("【Gemini 2.0 結果】")
        lines.append("─" * 80)
        lines.append(gemini_rec['transcript'])
        lines.append(f"(字元數: {len(gemini_rec['transcript'])})")
        lines.append("")
        
        # 簡易相似度分析
        stt_text = stt_rec['transcript']
        gemini_text = gemini_rec['transcript']
        
        # 計算共同字元
        common_chars = set(stt_text) & set(gemini_text)
        similarity_pct = len(common_chars) / max(len(set(stt_text)), len(set(gemini_text)), 1) * 100
        
        lines.append("【差異分析】")
        lines.append("─" * 80)
        lines.append(f"字元數差異: {abs(len(stt_text) - len(gemini_text))} 字元")
        lines.append(f"字元集相似度: {similarity_pct:.1f}%")
        
        # 檢查是否有錯誤
        stt_error = stt_text.startswith('[') and '錯誤' in stt_text
        gemini_error = gemini_text.startswith('[') and '錯誤' in gemini_text
        
        if stt_error and gemini_error:
            lines.append("⚠️  兩者皆辨識失敗")
        elif stt_error:
            lines.append("⚠️  Google STT 辨識失敗，Gemini 成功")
        elif gemini_error:
            lines.append("⚠️  Gemini 辨識失敗，Google STT 成功")
        else:
            lines.append("✅ 兩者皆成功辨識")
        
        lines.append("")
    
    # 結尾
    lines.append("=" * 80)
    lines.append("                        比較報告結束")
    lines.append("=" * 80)
    
    return "\n".join(lines)

# ==================== 主頁面邏輯 ====================

uploaded_files = st.file_uploader(
    "選擇錄音檔 (支援多選)", 
    type=['wav', 'mp3', 'm4a', 'flac'], 
    accept_multiple_files=True,
    help="支援 WAV, MP3, M4A, FLAC 格式，可一次上傳多個檔案"
)

if st.button("🚀 開始轉譯", type="primary"):
    # 檢查是否有上傳檔案
    if not uploaded_files:
        st.error("請先上傳檔案！")
        st.stop()
    
    # 確認使用的轉譯模式
    use_stt = "Google STT" in mode or "雙模式" in mode
    use_gemini = "Gemini" in mode or "雙模式" in mode
    
    # 檢查憑證是否已設定（來自 config.py）
    if use_stt and not GCP_CREDENTIALS.get('project_id'):
        st.error("❌ Google STT 憑證未正確設定，請檢查 config.py")
        st.stop()
    
    if use_gemini and (not GEMINI_API_KEY or len(GEMINI_API_KEY) < 10):
        st.error("❌ Gemini API Key 未正確設定，請檢查 config.py")
        st.stop()

    # 初始化結果容器
    stt_records = []
    gemini_records = []
    
    # 建立進度條和狀態顯示
    progress_bar = st.progress(0)
    status_text = st.empty()

    # 建立臨時目錄來處理檔案轉換
    with tempfile.TemporaryDirectory() as temp_dir:
        
        for i, uploaded_file in enumerate(uploaded_files):
            status_text.text(f"正在處理：{uploaded_file.name} ({i+1}/{len(uploaded_files)})")
            
            # 1. 儲存上傳檔案到臨時目錄
            temp_path = os.path.join(temp_dir, uploaded_file.name)
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            # 1.5 檢查音訊品質並顯示警告
            needs_conversion, quality_warnings = check_audio_quality(temp_path)
            if quality_warnings:
                with st.expander(f"⚠️ {uploaded_file.name} 品質提示", expanded=False):
                    for warning in quality_warnings:
                        st.warning(warning)
                    st.info("系統將自動轉換為最佳格式")
            
            # 2. 取得音訊資訊
            try:
                duration_sec = get_audio_info(temp_path)
                if duration_sec == 0:
                    st.error(f"無法讀取 {uploaded_file.name} 的音訊資訊")
                    continue
                
                status_text.text(f"✅ 已載入：{uploaded_file.name} (長度: {format_duration(duration_sec)})")
                
            except Exception as e:
                st.error(f"檔案 {uploaded_file.name} 處理失敗：{str(e)[:200]}")
                continue

            # 3. 解析檔名中的時間資訊
            base_dt = extract_datetime_from_filename(uploaded_file.name)
            
            # 4. 執行 Google STT（函數內部會自動轉換為 WAV）
            if use_stt:
                status_text.text(f"🎤 Google STT 辨識中：{uploaded_file.name}...")
                res = transcribe_google_stt(temp_path, uploaded_file.name, max_chunk_duration=chunk_duration)
                stt_records.append({
                    'filename': uploaded_file.name, 
                    'datetime': base_dt,
                    'duration_sec': duration_sec, 
                    'transcript': res
                })
                status_text.text(f"✅ Google STT 完成：{uploaded_file.name}")

            # 5. 執行 Gemini（函數內部會自動轉換為 M4A）
            if use_gemini:
                status_text.text(f"🤖 Gemini 辨識中：{uploaded_file.name}...")
                res = transcribe_gemini(temp_path)  # 直接使用原始檔案
                gemini_records.append({
                    'filename': uploaded_file.name, 
                    'datetime': base_dt,
                    'duration_sec': duration_sec, 
                    'transcript': res
                })
                status_text.text(f"✅ Gemini 完成：{uploaded_file.name}")
            
            # 更新進度條
            progress_bar.progress((i + 1) / len(uploaded_files))

    status_text.text("✨ 處理完成！正在生成報表...")

    # ==================== 顯示與下載結果 ====================
    
    # 定義顯示結果的 Tabs
    tabs = []
    if use_stt: tabs.append("Google STT 結果")
    if use_gemini: tabs.append("Gemini 結果")
    if use_stt and use_gemini: tabs.append("🔍 雙模式比較")
    
    tab_objs = st.tabs(tabs)
    
    # 處理下載包 (Zip)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        
        # --- 處理 Google STT 輸出 ---
        if use_stt:
            with tab_objs[0]:
                merged_txt = generate_merged_content(stt_records)
                st.text_area("合併預覽", merged_txt, height=300, key="stt_merged_preview")
                zf.writestr("GoogleSTT_Merged.txt", merged_txt)
                
                # 寫入個別檔案
                for rec in stt_records:
                    txt_content = f"檔案：{rec['filename']}\n內容：{rec['transcript']}"
                    zf.writestr(f"GoogleSTT_Individual/{rec['filename']}.txt", txt_content)

        # --- 處理 Gemini 輸出 ---
        if use_gemini:
            idx = 1 if use_stt else 0
            with tab_objs[idx]:
                merged_txt = generate_merged_content(gemini_records)
                st.text_area("合併預覽", merged_txt, height=300, key="gemini_merged_preview")
                zf.writestr("Gemini_Merged.txt", merged_txt)
                
                # 寫入個別檔案
                for rec in gemini_records:
                    txt_content = f"檔案：{rec['filename']}\n內容：{rec['transcript']}"
                    zf.writestr(f"Gemini_Individual/{rec['filename']}.txt", txt_content)
        
        # --- 處理雙模式比較 ---
        if use_stt and use_gemini:
            with tab_objs[2]:
                st.subheader("📊 逐檔比較結果")
                
                # 檢查是否有記錄
                if not stt_records or not gemini_records:
                    st.warning("⚠️ 沒有可比較的記錄。可能所有檔案處理失敗。")
                else:
                    # 產生比較表格
                    comparison_data = []
                    for i in range(len(stt_records)):
                        stt_rec = stt_records[i]
                        gemini_rec = gemini_records[i]
                        
                        comparison_data.append({
                            "檔案": stt_rec['filename'],
                            "時長": format_duration(stt_rec['duration_sec']),
                            "Google STT": stt_rec['transcript'][:100] + "..." if len(stt_rec['transcript']) > 100 else stt_rec['transcript'],
                            "Gemini": gemini_rec['transcript'][:100] + "..." if len(gemini_rec['transcript']) > 100 else gemini_rec['transcript']
                        })
                    
                    # 顯示表格
                    import pandas as pd
                    df = pd.DataFrame(comparison_data)
                    st.dataframe(df, use_container_width=True, height=400)
                    
                    # 詳細逐檔比較
                    st.markdown("---")
                    st.subheader("📝 詳細逐檔對照")
                    
                    for i, (stt_rec, gemini_rec) in enumerate(zip(stt_records, gemini_records)):
                        with st.expander(f"📄 {stt_rec['filename']} ({format_duration(stt_rec['duration_sec'])})"):
                            col1, col2 = st.columns(2)
                            
                            with col1:
                                st.markdown("**🔵 Google STT**")
                                st.text_area(
                                    "STT 結果", 
                                    stt_rec['transcript'], 
                                    height=200, 
                                    key=f"compare_stt_{i}",
                                    label_visibility="collapsed"
                                )
                                stt_length = len(stt_rec['transcript'])
                                st.caption(f"字數: {stt_length} 字元")
                            
                            with col2:
                                st.markdown("**🟢 Gemini**")
                                st.text_area(
                                    "Gemini 結果", 
                                    gemini_rec['transcript'], 
                                    height=200, 
                                    key=f"compare_gemini_{i}",
                                    label_visibility="collapsed"
                                )
                                gemini_length = len(gemini_rec['transcript'])
                                st.caption(f"字數: {gemini_length} 字元")
                    
                    # 生成比較報告文字檔（在有記錄的情況下）
                    comparison_report = generate_comparison_report(stt_records, gemini_records)
                    zf.writestr("Comparison_Report.txt", comparison_report)

    # 下載按鈕
    st.success("✅ 全部轉譯完成！")
    st.download_button(
        label="📥 下載完整結果 (ZIP)",
        data=zip_buffer.getvalue(),
        file_name=f"transcripts_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
        mime="application/zip"
    )

# ==================== 頁腳與使用說明 ====================
st.markdown("---")
with st.expander("📖 使用說明"):
    st.markdown("""
    ### 使用步驟
    
    1. **選擇轉譯模式**
       - 僅 Google STT：速度較快，適合快速轉譯
       - 僅 Gemini：品質較好，支援較長音訊
       - 雙模式比較：同時使用兩種引擎，可比對結果
    
    2. **上傳音訊檔案**
       - 支援格式：WAV, MP3, M4A, FLAC
       - 可一次上傳多個檔案
       - 建議單檔 < 2 分鐘（更長會自動切分）
    
    3. **調整進階設定**（可選）
       - 音訊切分長度：控制長音訊的切分間隔
    
    4. **開始轉譯**
       - 點擊「開始轉譯」按鈕
       - 等待處理完成（依檔案數量和長度而定）
    
    5. **查看與下載結果**
       - 在不同 Tab 查看各引擎結果
       - 雙模式可查看詳細比較
       - 下載 ZIP 檔案包含所有結果
    
    ### 注意事項
    
    - ⚠️ 轉譯需要時間，請耐心等待
    - ⚠️ 同時多人使用可能較慢
    - ⚠️ 音訊品質會影響辨識準確度
    - ⚠️ 捷運專業術語已優化辨識
    
    ### 技術支援
    
    如有問題請聯繫系統管理員。
    """)

st.markdown("---")
st.caption("🎙️ 捷運緊急語音轉譯工具 | Powered by Google STT & Gemini AI")
