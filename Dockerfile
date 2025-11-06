# ---- Build stage ----
FROM python:3.12-alpine AS builder

WORKDIR /app

# Copy everything into the image
COPY . .

# Install backend dependencies
RUN pip install --no-cache-dir -r backend/requirements.txt

# ---- Runtime stage ----
FROM python:3.12-alpine

WORKDIR /app
COPY --from=builder /app /app

# Expose backend (8000) and frontend (5173)
EXPOSE 8000 5173

# Run both servers (backend + frontend)
CMD sh -c "cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 & cd ../frontend && python -m http.server 5173"
