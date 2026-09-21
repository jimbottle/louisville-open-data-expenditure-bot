# Throwaway image for the SSE streaming spike (louisville-open-data-4l4).
# Lambda Web Adapter runs as a Lambda extension and proxies the invoke to the
# uvicorn process on $PORT. AWS_LWA_INVOKE_MODE=response_stream makes it
# forward the response as it is produced instead of buffering it.
FROM public.ecr.aws/awsguru/aws-lambda-adapter:1.0.0 AS lwa

FROM python:3.12-slim
COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8000 \
    AWS_LWA_PORT=8000 \
    AWS_LWA_INVOKE_MODE=response_stream \
    AWS_LWA_READINESS_CHECK_PATH=/api/health \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.115.12" "uvicorn==0.34.2"
COPY app.py .

# Plain uvicorn, exactly as the real container runs it. No handler, no Mangum.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
