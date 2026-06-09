import json
import os

Q_DIR = "questions"

for filename in os.listdir(Q_DIR):
    if not filename.endswith(".json"):
        continue
    filepath = os.path.join(Q_DIR, filename)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        before = [q["id"] for q in data[:3]]
        data.sort(key=lambda q: q.get("id", 0))
        after = [q["id"] for q in data[:3]]
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"{filename}: {len(data)} questions sorted | before: {before} -> after: {after}")
    except Exception as e:
        print(f"{filename}: ERROR - {e}")

print("\nDone.")
