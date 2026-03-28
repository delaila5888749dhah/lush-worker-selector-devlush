import os
import subprocess
import sys


def git_ref_exists(ref):
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def resolve_diff_range():
    base_ref = os.environ.get("GITHUB_BASE_REF")
    head_sha = os.environ.get("GITHUB_HEAD_SHA")
    if base_ref and head_sha:
        if git_ref_exists(base_ref):
            return f"{base_ref}...{head_sha}"
        origin_base = f"origin/{base_ref}"
        if git_ref_exists(origin_base):
            return f"{origin_base}...{head_sha}"
        if git_ref_exists(f"{head_sha}^"):
            return f"{head_sha}^...{head_sha}"
        if git_ref_exists(head_sha):
            return head_sha

    if git_ref_exists("develop"):
        return "develop...HEAD"
    if git_ref_exists("origin/develop"):
        return "origin/develop...HEAD"
    if git_ref_exists("HEAD~1"):
        return "HEAD~1...HEAD"
    return "HEAD"


def get_changed_files():
    diff_range = resolve_diff_range()

    result = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("check_spec_lock: FAIL")
        if result.stderr:
            print(result.stderr.rstrip())
        return None

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main():
    changed_files = get_changed_files()
    if changed_files is None:
        return 1

    spec_files = [path for path in changed_files if path.startswith("spec/")]
    if spec_files:
        print("check_spec_lock: FAIL")
        for path in spec_files:
            print(path)
        print("Spec files cannot be modified in this PR.")
        return 1

    print("check_spec_lock: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
