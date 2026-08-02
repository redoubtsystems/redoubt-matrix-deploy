FROM python:3.11-slim

# Prevent Python from buffering logs
ENV PYTHONUNBUFFERED=1

# App Setup
WORKDIR /app

# System deps (certs matter!)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    nodejs \
    npm \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

# SCSS Compiler
RUN npm install -g sass

# Copy Source Files
COPY  app.py .
COPY requirements.txt .
COPY redoubt_login_email.html .
COPY static/ static/
COPY scss/ scss/
COPY templates/ templates/

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Compile SCSS -> CSS
RUN mkdir -p static/css && sass scss/admin.scss static/css/admin.css

# Flask Port
EXPOSE 5000

CMD ["python", "app.py"]
