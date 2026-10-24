# Backend image for Railway (doc/deployment.md): `pulse serve` = runs API + weekly scheduler.
# Run data lives on a Railway volume mounted at /app/data.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# CPU-only torch first, so sentence-transformers doesn't pull the multi-GB CUDA wheels.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# Editable install: prompts/ and pulse/template.md.j2 are read relative to the repo layout.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[api]"

# Local models are baked into the image so runs never download them (and nothing leaves the box but Groq calls).
RUN python -m spacy download en_core_web_sm \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2', device='cpu')"

ENV HF_HUB_OFFLINE=1

COPY config.yaml ./
COPY prompts ./prompts

EXPOSE 8000
# Reads $PORT (set by Railway); needs PULSE_API_TOKEN, PULSE_ID_SALT and GROQ_API_KEY in the environment.
CMD ["pulse", "serve"]
