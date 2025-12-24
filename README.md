# 🎙️ 捷運緊急語音轉譯工具

自動將無線電錄音檔透過 Google STT 與 Gemini AI 轉為逐字稿。

## 🚀 快速部署到 Streamlit Cloud

### 步驟 1：準備檔案

1. 將 `streamlit_app_fixed.py` 改名為 `streamlit_app.py`
2. 將 `config_cloud.py` 改名為 `config.py`
3. 確保有以下檔案：
   - streamlit_app.py
   - config.py
   - requirements.txt
   - packages.txt
   - .gitignore

### 步驟 2：上傳到 GitHub

```bash
git init
git add streamlit_app.py config.py requirements.txt packages.txt .gitignore README.md
git commit -m "Initial commit"
git remote add origin https://github.com/你的帳號/你的專案名稱.git
git push -u origin main
```

**⚠️ 確認 config.py 中沒有真實的 API Key！**

### 步驟 3：部署

1. 前往 https://share.streamlit.io
2. 登入 GitHub 帳號
3. 點擊「New app」
4. 選擇您的 Repository
5. Main file: `streamlit_app.py`
6. 點擊「Deploy」

### 步驟 4：設定 Secrets

在 Streamlit Cloud 後台點擊「Settings」→「Secrets」，貼上：

```toml
GEMINI_API_KEY = "你的-Gemini-API-Key"

[gcp_service_account]
type = "service_account"
project_id = "你的專案ID"
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "..."
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

### 步驟 5：完成！

您的網址：`https://你的app名稱.streamlit.app`

## 📱 使用方式

1. 開啟網址
2. 選擇轉譯模式（Google STT / Gemini / 雙模式）
3. 上傳音訊檔案
4. 點擊「開始轉譯」
5. 下載結果 ZIP

## 💡 功能特色

- ✅ 雙 AI 引擎比較
- ✅ 支援長音訊自動切分
- ✅ 批次處理多檔案
- ✅ 逐字稿時間戳記
- ✅ ZIP 打包下載

## ⚠️ 限制

- Streamlit Cloud 免費版：1GB RAM
- 單檔音訊建議 < 15MB
- 並發用戶數有限

## 🔒 安全提醒

- 不要在 GitHub 上傳真實 API Key
- 使用 Streamlit Secrets 保護敏感資訊
