# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install FFmpeg and other system dependencies
# apt-get update refreshes the package lists
# -y flag auto-confirms the installation
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application's code
COPY . .

# Command to run on container start
CMD ["python", "main.py"]
