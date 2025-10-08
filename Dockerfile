FROM mcr.microsoft.com/playwright/python:v1.48.0-focal

# Create and switch to the application directory.
WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application source code.
COPY . .

# Ensure Python output is not buffered so logs show up immediately.
ENV PYTHONUNBUFFERED=1

# Allow overriding at runtime; defaults align with config.py.
ENV PORT=5000 \
    FLASK_ENV=production

# Expose the Flask port (configurable via PORT env var).
EXPOSE 5000

# Start the Flask application.
CMD ["python", "final_scrapping_script.py"]
