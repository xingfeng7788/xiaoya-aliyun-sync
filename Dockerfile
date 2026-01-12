# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (aria2)
RUN apt-get update && apt-get install -y aria2 && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Use --no-cache-dir to keep the image size small
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port the app runs on
EXPOSE 5666

# Define environment variable
# These can be overridden by docker-compose or .env
ENV HOST=0.0.0.0
ENV PORT=5666

# Run app.py when the container launches
CMD ["python", "app.py"]
