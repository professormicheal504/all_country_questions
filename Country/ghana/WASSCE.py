import os
import json
import asyncio
import aiohttp
import random
import re

# ==========================================
# CONFIGURATION
# ==========================================
BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "ghana", "exam", "WASSCE for School Candidates")
TOPICS_DIR = os.path.join(BASE_DIR, "subject_topic")

API_KEYS = [
    "uE3VwMK76ZnpOIuEGuS4HO8rBk5Y5cuU",
    "Qt8p4oZPlSoLnjra340ocVMoV7v8nmwP",
    "ZJipXwvcpx9qreNQhlXnOwEZFjKhxhsC"
]
MODEL = "mistral-large-latest"

# Automatically load all subjects based on the folders inside BASE_DIR
# If the folder doesn't exist on Google Drive yet, it will create it and use a fallback list.
os.makedirs(TOPICS_DIR, exist_ok=True)

try:
    SUBJECTS = [f.replace(".json", "").replace("_", " ").title() for f in os.listdir(TOPICS_DIR) if f.endswith(".json")]
except FileNotFoundError:
    SUBJECTS = []

if not SUBJECTS:
    raise ValueError(f"\nCRITICAL ERROR: Google Colab cannot find your JSON files inside '{TOPICS_DIR}'!\nColab might be hiding them. Please click the 'Refresh' button in the Colab file explorer on the left, or ensure the files are actually uploaded.")
    
YEARS = [str(y) for y in range(2000, 2027)]

# Generate limits (chunked into 10 questions per API call to avoid token truncation)
TYPES_CONFIG = {
    "Objective": 40,
    "Theory": 10,
    "Practical": 10
}

# ==========================================
# HELPER FUNCTIONS
# ==========================================
git_lock = asyncio.Lock()

async def sync_to_github():
    # Only run if not already syncing to prevent overlap and git index locks
    if git_lock.locked(): return
    async with git_lock:
        try:
            # We use devnull for stdout/stderr so we don't spam the console
            proc1 = await asyncio.create_subprocess_shell('git add .', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc1.communicate()
            
            proc2 = await asyncio.create_subprocess_shell('git commit -m "Auto-save generated questions"', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc2.communicate()
            
            proc3 = await asyncio.create_subprocess_shell('git push', stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc3.communicate()
        except Exception as e:
            print(f"[Auto-Save Error] Git sync failed: {e}")

class KeyRotator:
    def __init__(self, keys):
        self.keys = keys
        self.index = 0
        self.lock = asyncio.Lock()

    async def get_key(self):
        async with self.lock:
            key = self.keys[self.index]
            self.index = (self.index + 1) % len(self.keys)
            return key

key_manager = KeyRotator(API_KEYS)

def clean_json_response(text):
    # Strip markdown blocks if the model wrapped it
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def load_topics(subject):
    path = os.path.join(TOPICS_DIR, f"{subject.lower().replace(' ', '_')}.json")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list): return data
                if isinstance(data, dict): return list(data.keys())
        except Exception as e:
            print(f"Error loading topics for {subject}: {e}")
    return []

# ==========================================
# MISTRAL API CALL
# ==========================================
async def generate_questions_batch(session, subject, exam_type, year, topics, batch_size, start_order_id):
    api_key = await key_manager.get_key()

    topics_str = ", ".join(topics) if topics else "standard WAEC Ghana syllabus topics"

    prompt = f"""
    You are an expert examiner for the West African Examinations Council (WAEC) in Ghana.
    Generate EXACTLY {batch_size} standard CBT {exam_type} questions for {subject} (Year {year}).
    The questions MUST be strictly based on the topics for the Ghana WAEC syllabus, specifically drawn from: {topics_str}.

    REQUIREMENTS:
    1. Output strictly as a JSON object, exactly matching the structure below. Do NOT wrap it in markdown. No introductory text.
    2. Each question MUST be 100% accurate, verified, and follow standard Ghana WAEC difficulty.
    3. The JSON root should be an object where keys are stringified incremental IDs (start at "{random.randint(10000, 99999)}").
    4. For Objective, provide exactly 4 options (a, b, c, d), with exactly ONE having "is_correct": true.
    5. For Theory/Practical, options MUST be an empty array [].
    6. Ensure the "order_id" increments sequentially starting from {start_order_id}.
    7. FORMATTING: You MUST use simple HTML tags for text formatting: <b> for bold, <i> for italic, <u> for underline, and <br> for line breaks.
    8. EQUATIONS: You MUST use inline LaTeX enclosed in single dollar signs (e.g., $x^2 + y = 0$) for ANY math, physics, chemistry equations or symbols.
    9. Apply this formatting (HTML and LaTeX) to the "question_text", the "text" field of "options", and the "explanation".
    10. "question_text" and "explanation" MUST be wrapped in <p> tags.

    JSON STRUCTURE FORMAT:
    {{
        "{random.randint(10000, 99999)}": {{
            "order_id": {start_order_id},
            "exam_type": "WAEC",
            "subject": "{subject}",
            "topic": "Topic Name",
            "question_type": "{exam_type}",
            "exam_year": "{year}",
            "question_text": "<p>Your question text here?</p>",
            "question_image": null,
            "answer_image": null,
            "explanation": "<p>Detailed step-by-step marking scheme/explanation here.</p>",
            "reference_passage": null,
            "novel": null,
            "comments_count": 0,
            "options": [
                {{ "option_id": "{random.randint(10000, 99999)}", "tag": "a", "text": "Option A text", "is_correct": false }},
                {{ "option_id": "{random.randint(10000, 99999)}", "tag": "b", "text": "Option B text", "is_correct": true }},
                {{ "option_id": "{random.randint(10000, 99999)}", "tag": "c", "text": "Option C text", "is_correct": false }},
                {{ "option_id": "{random.randint(10000, 99999)}", "tag": "d", "text": "Option D text", "is_correct": false }}
            ],
            "_page_num": 1,
            "_page_idx": 1
        }}
    }}
    """

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a JSON generating API. You only output valid JSON. You never output conversational text."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2, # Low temperature for factual accuracy
        "response_format": {"type": "json_object"}
    }

    print(f"Requesting {batch_size} {exam_type} questions for {subject} starting at order {start_order_id} using key {api_key[:6]}...")

    for attempt in range(3): # Retry logic
        try:
            async with session.post("https://api.mistral.ai/v1/chat/completions", json=payload, headers=headers, timeout=120) as response:
                if response.status == 200:
                    data = await response.json()
                    content = data["choices"][0]["message"]["content"]
                    clean_content = clean_json_response(content)

                    try:
                        parsed_json = json.loads(clean_content)
                        # Minimal Validation
                        if len(parsed_json) < batch_size - 2: # Give slight leeway if model shorts 1 or 2
                            print(f"Warning: Model generated {len(parsed_json)} instead of {batch_size} questions.")
                        return parsed_json
                    except json.JSONDecodeError as e:
                        print(f"Failed to parse JSON on attempt {attempt+1}: {e}")
                else:
                    err = await response.text()
                    print(f"HTTP {response.status} on attempt {attempt+1}: {err}")
        except Exception as e:
            print(f"Exception on attempt {attempt+1}: {e}")

        await asyncio.sleep(2 ** attempt) # Exponential backoff

    return {} # Return empty if all retries fail

import time

# ==========================================
# MAIN EXECUTION
# ==========================================
def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

async def generate_for_subject_year_type(session, subject, year, exam_type, target_count, topics, stats):
    safe_subject = subject.replace(" ", "_").replace("/", "_")
    target_dir = os.path.join(BASE_DIR, "WAEC", safe_subject, "year", exam_type)
    target_file = os.path.join(target_dir, f"{year}.json")

    existing_data = {}
    if os.path.exists(target_file) and os.path.getsize(target_file) > 10:
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            # Only skip if the file is fully complete! (Allowing a tiny margin of 3 in case the AI missed 1)
            if len(existing_data) >= target_count - 3:
                print(f"\n[Skipped] {exam_type} questions for {subject} {year} already exist (Count: {len(existing_data)}).")
                stats["completed"] += target_count
                return
            else:
                print(f"\n[Resuming] Found incomplete file for {subject} {year} with {len(existing_data)} questions. Resuming...")
                stats["completed"] += len(existing_data)
        except Exception:
            existing_data = {}

    print(f"\n--- Generating {target_count} {exam_type} questions for {subject} {year} ---")

    batch_size = 10
    tasks = []

    start_index = len(existing_data) + 1
    for start_order in range(start_index, target_count + 1, batch_size):
        tasks.append(generate_questions_batch(session, subject, exam_type, year, topics, batch_size, start_order))

    # Process batches as they complete and save instantly
    combined_data = existing_data.copy()
    os.makedirs(target_dir, exist_ok=True)

    for coro in asyncio.as_completed(tasks):
        res = await coro
        stats["completed"] += batch_size # Increment global count
        
        if res:
            combined_data.update(res)

            # Re-sequence order_id just in case the LLM messed up the numbering
            final_data = {}
            order_idx = 1
            for key, val in list(combined_data.items()):
                if not isinstance(val, dict):
                    # Remove invalid items from combined data so they don't persist
                    del combined_data[key]
                    continue
                val["order_id"] = order_idx
                final_data[key] = val
                order_idx += 1

            # Save instantly to Disk
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(final_data, f, indent=4)
                
            # Trigger an async push to GitHub in the background
            asyncio.create_task(sync_to_github())
                
            # Calculate ETA
            elapsed = time.time() - stats["start_time"]
            avg_time = elapsed / max(1, stats["completed"])
            remaining = stats["total"] - stats["completed"]
            eta_seconds = remaining * avg_time
            
            print(f"[Instant Save] Saved {len(final_data)} questions to {target_file}")
            print(f" -> Progress: {stats['completed']}/{stats['total']} | Time Left: {format_time(eta_seconds)}")

    if not combined_data:
        print(f"Failed to generate any valid questions for {exam_type}")
    else:
        print(f"Successfully finished generating {len(combined_data)} questions for {target_file}")

async def main():
    total_questions = len(SUBJECTS) * len(YEARS) * sum(TYPES_CONFIG.values())
    stats = {
        "completed": 0,
        "total": total_questions,
        "start_time": time.time()
    }
    
    print(f"Starting generation... Total Target: {total_questions} questions.")
    
    async with aiohttp.ClientSession() as session:
        for subject in SUBJECTS:
            topics = load_topics(subject)
            if not topics:
                print(f"Notice: No topics found in {TOPICS_DIR} for {subject}. Will use general WAEC syllabus.")

            for year in YEARS:
                for exam_type, count in TYPES_CONFIG.items():
                    await generate_for_subject_year_type(session, subject, year, exam_type, count, topics, stats)
                    
                    # Stop gracefully before GitHub Actions 6-hour limit (stop at 5.5 hours)
                    elapsed_time = time.time() - stats["start_time"]
                    if elapsed_time > (5.5 * 3600):
                        print(f"\n[TIME LIMIT] Script has run for {format_time(elapsed_time)}. Stopping gracefully to allow GitHub Actions to commit.")
                        return

    print("\nGeneration Complete! All files saved successfully.")

if __name__ == "__main__":
    import sys
    if "ipykernel" in sys.modules:
        import nest_asyncio
        nest_asyncio.apply()
    asyncio.run(main())