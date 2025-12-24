# config.py - 自動支援本地開發和 Streamlit Cloud 部署

import streamlit as st

# ==================== Gemini API Key ====================
# 優先使用 Streamlit Secrets，否則使用本地設定
if "GEMINI_API_KEY" in st.secrets:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
else:
    # 本地開發時使用（請替換成您的實際 API Key）
    GEMINI_API_KEY = "your-gemini-api-key-here"

# ==================== Google Cloud Credentials ====================
# 優先使用 Streamlit Secrets，否則使用本地設定
if "gcp_service_account" in st.secrets:
    GCP_CREDENTIALS = dict(st.secrets["gcp_service_account"])
else:
    # 本地開發時使用（請替換成您的實際憑證）
    GCP_CREDENTIALS = {
        "type": "service_account",
        "project_id": "your-project-id",
        "private_key_id": "your-private-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
        "client_email": "your-service-account@your-project.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/..."
    }
