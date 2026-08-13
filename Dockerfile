FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY db.py .

# Only quiz images dir needed (sessions are in MongoDB now)
RUN mkdir -p quiz_images

CMD ["python", "bot.py"]
