# Use an official Python 3.10 slim image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the file that lists your dependencies
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your other files (like main.py) into the container
COPY . .

# Set the command to run when the container starts
CMD ["python", "main.py"]
