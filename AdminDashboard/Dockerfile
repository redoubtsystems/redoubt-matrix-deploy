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
    nodejs \
    npm \
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
RUN sass scss/admin.scss static/css/admin.css

# Flask Port
EXPOSE 5000

CMD ["python", "app.py"]