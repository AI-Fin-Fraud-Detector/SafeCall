# Anti-Fraud Communication Service

This is a GPT-4 powered anti-fraud voice assistant service, specifically designed to engage with scammers through clever prompt engineering to elicit information about their scam schemes and identity.

## Features

- **GPT-4 Powered**: Uses OpenAI's GPT-4 model for natural language conversation
- **Anti-Fraud Prompts**: Specialized prompt engineering designed to guide scammers into revealing their call purpose
- **Voice Support**: Integrated with TTS services for voice interaction
- **Food Recognition**: Retains original food recognition functionality (if needed)

## Environment Setup

### Required Environment Variables

Before starting the service, ensure the following environment variables are set:

```bash
# OpenAI API Key (Required)
export OPENAI_API_KEY="your-openai-api-key-here"

# Other necessary environment variables
export REDIS_URL="redis://localhost:6379"
export DATABASE_URL="postgresql://user:password@localhost/dbname"
```

### Docker環境設置

如果使用Docker，請在docker-compose.yml中添加環境變數：

```yaml
services:
  fraud:
    build: .
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://db:5432/kebbi
```

## Anti-Fraud Conversation Strategy

This service employs the following conversation strategies to guide scammers:

1. **Role-Playing**: Plays a middle-aged woman named "Sarah" who appears slightly nervous but willing to cooperate
2. **Information Gathering**: Uses seemingly naive questions to extract the caller's identity and purpose
3. **Time Delay**: Keeps the conversation going to give law enforcement more tracking time
4. **Avoid Exposure**: Does not show professional knowledge about scams or immediate rejection

### Example Conversation Flow

**Scammer**: "Hello, this is customer service from XX Bank..."
**System Response**: "A bank? I didn't apply for anything... Which bank are you from? I can't hear you clearly..."

**Scammer**: "Your account has suspicious transactions..."
**System Response**: "What kind of suspicious activity? I don't understand these things. Can you explain in detail? Who did you say you were?"

## API Endpoints

### POST /api/fraud/
Anti-fraud conversation endpoint

**Headers:**
- `X-User-Id`: User ID
- `X-Username`: Username  
- `X-Installation-Id`: Device ID

**Request Body:**
```json
{
  "prompt": "User input message"
}
```

**Response:**
- Success: Returns audio file (MP3) with text response in headers
- Failure: Returns JSON format error message

### POST /api/food-recognition/
食物辨識端點（保留原功能）

## 開發與部署

### 本地開發

```bash
# 安裝依賴
pip install -e .

# 啟動服務
python src/main.py
```

### Docker部署

```bash
# 構建映像
docker build -t anti-fraud-service .

# 啟動服務
docker-compose up -d
```

## 安全注意事項

1. **API密鑰保護**: 絕對不要將OpenAI API密鑰硬編碼在代碼中
2. **日誌管控**: 確保敏感對話內容不會被記錄在明文日誌中
3. **訪問控制**: 實施適當的身份驗證和授權機制
4. **監控告警**: 設置異常使用監控，防止API濫用

## 故障排除

### 常見問題

1. **OpenAI API密鑰錯誤**
   - 檢查環境變數是否正確設置
   - 確認API密鑰有效且有足夠額度

2. **TTS服務失敗**
   - 服務會自動降級為文字回應
   - 檢查TTS服務的連接狀態

3. **Redis連接問題**
   - 確認Redis服務正常運行
   - 檢查網絡連接和配置

## 授權協議

此項目僅供合法的反詐騙用途使用。使用者需確保遵守當地法律法規。