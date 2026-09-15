import json

# Path to your JSON file
file_path = "sampl.json"

# Open and read the JSON file
with open(file_path, "r") as f:
    data = json.load(f)

# Print the contents
print(data)

