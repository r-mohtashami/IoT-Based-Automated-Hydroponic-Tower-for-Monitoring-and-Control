FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/tests ./tests
COPY backend/pytest.ini .

# The closed-loop integration test deliberately exercises the real simulator
# state-transition and actuator command code in-process. Keep the production
# backend runtime isolated from simulator control; this copy exists only so the
# packaged test suite can import the simulator contract when pytest is run
# inside the backend container.
COPY simulator/simulator.py /simulator/simulator.py

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
