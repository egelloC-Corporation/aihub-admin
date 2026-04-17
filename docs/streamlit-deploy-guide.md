# Deploying a Streamlit App to AI Hub

This documents every issue encountered deploying a Streamlit dashboard (behind a Flask auth proxy) to the AI Hub platform, and the fixes required. Use this as a reference for any future Streamlit app deployments.

## Architecture

```
Browser → Nginx (TLS) → Flask (auth + proxy, port 8080) → Streamlit (dashboard, port 8501)
                   ↘ WebSocket → Streamlit directly (port 8501)
```

Flask handles authentication and proxies HTTP requests to Streamlit. WebSocket connections bypass Flask and go directly to Streamlit (Flask's dev server can't handle WebSocket upgrades).

---

## Issues & Fixes (in order encountered)

### 1. No Dockerfile

**Error:** `No Dockerfile found in /app/apps/<name>`

**Cause:** The repo didn't have a Dockerfile. The deploy system can auto-generate one for simple Node.js or Python apps, but a Streamlit app with a Flask proxy needs a custom one.

**Fix:** Add a Dockerfile to the repo root (or monorepo subfolder):

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "server.py"]
```

---

### 2. Monorepo subdirectory not detected

**Error:** `No Dockerfile found` even though one exists in the subfolder.

**Cause:** The repo URL was `https://github.com/org/repo.git` but the app lives in a subfolder (e.g. `cohort-analysis/`). The deploy system clones the whole repo and looks for a Dockerfile at the root.

**Fix:** Submit with the full GitHub tree URL instead of the `.git` URL:
```
https://github.com/org/repo/tree/main/cohort-analysis
```
The platform auto-detects the subdirectory from this URL format.

---

### 3. Flask intercepts `/static/` requests

**Error:** All Streamlit static files (JS, CSS) return 404. The Streamlit skeleton loads but nothing renders.

**Cause:** Flask registers a built-in `/static/` route that serves from a local `static/` folder (which doesn't exist). This takes priority over the catch-all `/<path:path>` route that proxies to Streamlit.

**Fix:** Disable Flask's static file serving:

```python
app = Flask(__name__, static_folder=None)
```

---

### 4. Navbar injection fails on gzipped responses

**Error:** The AI Hub app drawer (waffle menu) doesn't appear.

**Cause:** The Flask proxy injects `<script src="/hub-navbar.js">` by finding `</body>` in the response body. But when the browser sends `Accept-Encoding: gzip`, Streamlit returns compressed HTML and the `</body>` string isn't found in the gzipped bytes.

**Fix:** Strip `Accept-Encoding` from forwarded headers so Streamlit returns uncompressed HTML:

```python
HOP_BY_HOP = frozenset({
    "host", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-authenticate", "accept-encoding",  # ← add this
})
```

---

### 5. WebSocket connection fails (`/_stcore/stream` returns 400)

**Error:** `WebSocket connection to 'wss://incubator.egelloc.com/app-name/_stcore/stream' failed`. The Streamlit skeleton loads but data never appears.

**Cause:** Streamlit uses WebSocket via `/_stcore/stream` to push data to the browser. Flask's dev server (werkzeug) cannot handle WebSocket upgrade requests — it returns HTTP 400.

**Fix (app side):** Use `flask-sock` and `websocket-client` to relay WebSocket connections:

```python
from flask_sock import Sock
import websocket as ws_client

sock = Sock(app)

@sock.route("/_stcore/stream")
def ws_proxy(ws):
    qs = request.query_string.decode()
    url = f"ws://127.0.0.1:{STREAMLIT_PORT}/_stcore/stream"
    if qs:
        url += f"?{qs}"

    backend = ws_client.create_connection(url)

    def forward_to_client():
        try:
            while True:
                data = backend.recv()
                if isinstance(data, bytes):
                    ws.send(data)
                else:
                    ws.send(data)
        except:
            pass

    import threading
    t = threading.Thread(target=forward_to_client, daemon=True)
    t.start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            backend.send(msg)
    except:
        pass
    finally:
        backend.close()
```

Add to `requirements.txt`:
```
flask-sock>=0.7
websocket-client>=1.7
```

**Fix (Nginx side):** Route WebSocket directly to Streamlit, bypassing Flask entirely. This requires Streamlit to bind to `0.0.0.0`:

```python
# In server.py start_streamlit():
"--server.address", "0.0.0.0",  # not 127.0.0.1
```

The Nginx config then routes WebSocket to Streamlit's port directly:
```nginx
location /app-name/_stcore/stream {
    proxy_pass http://127.0.0.1:8501/_stcore/stream;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
}
```

The app container must publish both ports (8080 for Flask, 8501 for Streamlit).

---

### 6. Data error in dashboard

**Error:** `ArrowInvalid: Could not convert '—' with type str: tried to convert to int64`

**Cause:** An em-dash character (`—`) in a data column that should be numeric.

**Fix:** Clean the data or use `errors='coerce'` in pandas:
```python
df['Attended'] = pd.to_numeric(df['Attended'], errors='coerce').fillna(0).astype(int)
```

---

### 7. Navbar doesn't render inside Streamlit

**Error:** The AI Hub app drawer (waffle icon) doesn't appear even though `hub-navbar.js` is injected into the HTML.

**Cause:** Streamlit renders inside a React app that replaces the DOM. The injected `<script>` tag runs but the navbar DOM elements get overwritten by React.

**Fix:** Load the navbar as a Streamlit component instead:
```python
import streamlit.components.v1 as components
components.html('<script src="/hub-navbar.js"></script>', height=0)
```

---

## Complete requirements.txt

```
flask>=3.0.0
flask-cors>=4.0.0
flask-sock>=0.7
websocket-client>=1.7
requests>=2.32.0
streamlit>=1.45.0
pandas>=2.0.0
plotly>=5.0.0
# Add your other dependencies
```

## Complete server.py checklist

- [ ] `Flask(__name__, static_folder=None)` — disable Flask static serving
- [ ] `"accept-encoding"` in `HOP_BY_HOP` — prevent gzipped proxy responses
- [ ] `@sock.route("/_stcore/stream")` — WebSocket relay handler
- [ ] `"--server.address", "0.0.0.0"` — Streamlit binds to all interfaces
- [ ] `/health` endpoint — required for deploy health checks
- [ ] `AIHUB_AUTH_URL` read from env — set by deploy system
- [ ] `AIHUB_LOGIN_URL` read from env — redirects to admin panel login
- [ ] Navbar injection in HTML responses — `</body>` replacement
- [ ] Streamlit component for navbar — `components.html('<script src="/hub-navbar.js"></script>', height=0)`
