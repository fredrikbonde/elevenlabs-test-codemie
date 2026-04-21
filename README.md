# ElevenLabs ↔ CodeMie Bridge

A FastAPI app that connects an ElevenLabs voice agent to EPAM's internal CodeMie LLM platform.

## How it works

```
You (voice)
    ↕
ElevenLabs Agent (voice in/out via WebSocket)
    ↕
This FastAPI app (OpenAI-compatible HTTP endpoint)
    ↕
CodeMie (EPAM internal LLM platform)
```

This app exposes an OpenAI-compatible `/chat/completions` endpoint. ElevenLabs is configured to call it as a custom LLM. The app translates between OpenAI message format and CodeMie's internal API format, streaming responses back in real time.

---

## Project structure

```
/
├── main.py              # FastAPI app
├── requirements.txt     # Python dependencies
├── apprunner.yaml       # AWS App Runner configuration
├── .env                 # Local environment variables (never commit this!)
└── README.md
```

---

## Local setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Create your `.env` file

Copy the following into a `.env` file in the project root and fill in your CodeMie credentials.

> You need a CodeMie account to connect to the platform. Enter your EPAM CodeMie username and password below.

```
CODEMIE_ENDPOINT=https://codemie.lab.epam.com/code-assistant-api/v1/assistants
CODEMIE_ASSISTANT_ID=your-assistant-id
CODEMIE_ASSISTANT_FOLDER=your-assistant-folder-name
CODEMIE_LLM_MODEL=claude-haiku-4-5-20251001

KEYCLOAK_URL=https://auth.codemie.lab.epam.com/realms/codemie-prod/protocol/openid-connect/token
KEYCLOAK_CLIENT=codemie-sdk

CODEMIE_USERNAME=your-codemie-username
CODEMIE_PASSWORD=your-codemie-password
```

### 3. Run the app
```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

### 4. Verify it's working
Open in browser:
```
http://localhost:8080/health   # Basic health check
http://localhost:8080/ping     # Sends a test message to CodeMie and returns the response
```

---

## AWS App Runner deployment

### Environment variables
Non-sensitive variables are set directly in `apprunner.yaml`. The CodeMie credentials (`CODEMIE_USERNAME` and `CODEMIE_PASSWORD`) are sensitive and stored in **AWS SSM Parameter Store** as `SecureString`, referenced in `apprunner.yaml` via ARN.

### Deploying
1. Push code to GitHub
2. App Runner picks up changes automatically (auto-deploy is enabled)
3. Monitor deployment in the App Runner console
4. Check **CloudWatch Logs** for application logs under `/aws/apprunner/...`

### Verifying the deployment
```
https://your-url.eu-central-1.awsapprunner.com/health
https://your-url.eu-central-1.awsapprunner.com/ping
```

---

## ElevenLabs agent configuration

1. Go to https://elevenlabs.io/app/conversational-ai
2. Open your agent
3. Go to **LLM** settings
4. Set LLM to **Custom LLM**
5. Set the URL to:
   ```
   https://your-url.eu-central-1.awsapprunner.com
   ```
   (ElevenLabs appends `/chat/completions` automatically)
6. Save the agent

The system prompt configured in ElevenLabs is passed through to CodeMie automatically.

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Returns service status, configured model, and token state |
| `/ping` | GET | Sends a test message to CodeMie and returns the response |
| `/chat/completions` | POST | OpenAI-compatible chat endpoint (used by ElevenLabs) |
| `/v1/chat/completions` | POST | Same as above, alternate path |

---

## Known limitations

- **No persistent conversation storage** — the conversation store is an in-memory dict. Sufficient for demo; replace with DynamoDB for production.
- **Latency** — there is an extra network hop through this bridge compared to using CodeMie or Claude directly.
