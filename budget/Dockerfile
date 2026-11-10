FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    BUDGET_HOME=/data \
    BUDGET_SUGGESTIONS=/data/rules_suggested.txt \
    BUDGET_RULES=/config/rules.txt
WORKDIR /app
COPY budget.py web.py ./
CMD ["python", "web.py"]
