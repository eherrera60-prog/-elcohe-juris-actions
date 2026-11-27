FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY prueba_registro_165.py prueba_persistencia_165.py arranque_persistencia_165.py app.py auth_access.py oauth_actions_165.py registry.py registry_promotion.py juris_163.py juris_164.py tc_indexed_recovery.py foundation_origin_165.py document_continuity_165.py web_client_165.py portal_validation_165.py mcp_server.py decision_number.py ./
ENV PORT=8000
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
