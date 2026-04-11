FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Stub for coach_briefing module (lives in separate repo)
# If the real module isn't present, create a minimal stub so the server can start.
RUN if [ ! -f coach_briefing.py ] && [ ! -d coach_briefing ]; then \
    printf '%s\n' \
    'import os' \
    'DB_CONFIG = {' \
    '    "host": os.environ.get("DB_HOST", "localhost"),' \
    '    "port": int(os.environ.get("DB_PORT", "3306")),' \
    '    "database": os.environ.get("DB_NAME", "egelloc"),' \
    '    "user": os.environ.get("DB_USER", "dummy"),' \
    '    "password": os.environ.get("DB_PASSWORD", "dummy"),' \
    '}' \
    'ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")' \
    'def get_student_profile(*a, **k): return {}' \
    'def fetch_fathom_transcripts(*a, **k): return []' \
    'def match_fathom_to_student(*a, **k): return []' \
    'def strip_html(s): return s' \
    > coach_briefing.py; \
    fi

ENV HOST=0.0.0.0
EXPOSE 5051

CMD ["python", "server.py"]
