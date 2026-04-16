# ElevenLabs ↔ CodemIE Bridge

A FastAPI app that connects an ElevenLabs voice agent to EPAM's internal CodemIE LLM platform.

## How it works

```
You (voice)
    ↕
ElevenLabs Agent (voice in/out via WebSocket)
    ↕
This FastAPI app (OpenAI-compatible HTTP endpoint)
    ↕
CodemIE (EPAM internal LLM platform)
```

This app exposes an OpenAI-compatible `/chat/completions` endpoint. ElevenLabs is configured to call it as a custom LLM. The app translates between OpenAI message format and CodemIE's internal API format, streaming responses back in real time.

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
```
CODEMIE_ENDPOINT=https://codemie.lab.epam.com/code-assistant-api/v1/assistants
CODEMIE_ASSISTANT_ID=your-assistant-id
CODEMIE_LLM_MODEL=claude-haiku-4-5-20251001
CODEMIE_OAUTH_PROXY_0_A="first _oauth2_proxy_0 cookie value"
CODEMIE_OAUTH_PROXY_0_B="second _oauth2_proxy_0 cookie value"
CODEMIE_OAUTH_PROXY_1="_oauth2_proxy_1 cookie value"
```

#### How to get the cookies
1. Open CodemIE in Firefox and log in
2. Open DevTools → Network tab
3. Make any chat request
4. Find the request, go to Headers → Cookie
5. Copy the values for `_oauth2_proxy_0` (there will be two) and `_oauth2_proxy_1`

> ⚠️ Cookies expire periodically. When the app starts returning 401 errors, repeat the above steps and update your `.env`.

### 3. Run the app
```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

### 4. Verify it's working
Open in browser:
```
http://localhost:8080/health   # Basic health check
http://localhost:8080/ping     # Sends a test message to CodemIE and returns the response
```

---

## AWS App Runner deployment

### Environment variables
The non-sensitive variables are set in `apprunner.yaml`. The three cookie values are sensitive and should be stored in **AWS SSM Parameter Store** as `SecureString` and referenced in `apprunner.yaml` via ARN.

> ⚠️ Note: SSM SecureString has a 4096 character limit. If cookie values exceed this, store them in **AWS Secrets Manager** instead (65536 character limit).

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

The system prompt configured in ElevenLabs is passed through to CodemIE automatically.

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Returns service status and configured model |
| `/ping` | GET | Sends a test message to CodemIE and returns the response |
| `/chat/completions` | POST | OpenAI-compatible chat endpoint (used by ElevenLabs) |
| `/v1/chat/completions` | POST | Same as above, alternate path |

---

## Cookie authentication

CodemIE uses `oauth2_proxy` for authentication, which splits large tokens across multiple cookies with the same name. This is why there are two `_oauth2_proxy_0` cookies. The app sends them as a raw `Cookie` header to preserve duplicates.

The relevant cookies are:
- `_oauth2_proxy_0` (appears twice — store as `CODEMIE_OAUTH_PROXY_0_A` and `CODEMIE_OAUTH_PROXY_0_B`)
- `_oauth2_proxy_1`

All other cookies (`_ga_*`, `__cf_bm`, etc.) are analytics/CDN cookies and are not needed.

---

## Known limitations

- **Cookie expiry** — cookies expire periodically and must be manually refreshed. For a production setup, proper API key authentication should be obtained from the CodemIE platform team.
- **Latency** — there is an extra network hop through this bridge compared to using CodemIE or Claude directly. Initial token latency depends on CodemIE's internal processing time.
- **No persistent conversation storage** — conversation history is maintained by ElevenLabs and passed in each request. The bridge itself is stateless.
