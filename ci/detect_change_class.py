import os
import sys
import subprocess

def get_changed_files():
    try:
        output = subprocess.check_output(['git', 'diff', '--name-only', 'origin/main...HEAD'], encoding='utf-8')
        return [f for f in output.split('\n') if f]
    except Exception:
        return []

def detect():
    pr_title = os.getenv("PR_TITLE", "").lower()
    files = get_changed_files()
    
    # RULE 1: EMERGENCY (Explicit only)
    if "[emergency]" in pr_title:
        return "emergency_override"

    score = {"spec_sync": 0, "infra_change": 0}

    for f in files:
        # Scoring cho spec_sync
        if f.startswith("spec/"):
            score["spec_sync"] += 3
            if f.startswith("spec/core/") or f.startswith("spec/integration/"):
                score["spec_sync"] += 5
        
        # Scoring cho infra_change
        if f.startswith("ci/") or f.startswith(".github/"):
            score["infra_change"] += 3

    # Anti-loop Guard: Kiểm tra xung đột
    if score["spec_sync"] >= 5 and score["infra_change"] >= 3:
        print(f"Ambiguous change: Spec={score['spec_sync']}, Infra={score['infra_change']}", file=sys.stderr)
        sys.exit(1)

    # Phân loại dựa trên điểm số
    if score["spec_sync"] >= 5:
        return "spec_sync"
    if score["infra_change"] >= 3:
        return "infra_change"
    
    return "normal"

if __name__ == "__main__":
    print(detect())
