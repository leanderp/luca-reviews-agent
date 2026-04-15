"""
Approve & Post Responses — Luca Money Review Agent
====================================================
Run this AFTER agent.py sends you the daily email.

It will:
1. Load today's report
2. Show you each review + proposed response one by one
3. Ask: approve / edit / skip
4. Post approved iOS responses to App Store Connect automatically
5. Post approved Android responses to Google Play automatically

Usage:
    python approve.py                              # today's report
    python approve.py reports/report_2025-01-15.json   # specific report
"""

import os
import sys
import json
import time
import datetime
import jwt
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── CONFIG ──────────────────────────────────────────────────────────────────

IOS_ISSUER_ID = os.environ["IOS_ISSUER_ID"]
IOS_KEY_ID    = os.environ["IOS_KEY_ID"]
IOS_APP_ID    = os.environ["IOS_APP_ID"]
IOS_P8_PATH   = os.path.expanduser(os.environ["IOS_P8_PATH"])

ANDROID_PACKAGE      = os.environ.get("ANDROID_PACKAGE", "com.undr.luca")
ANDROID_SERVICE_ACCT = os.path.expanduser(os.environ["ANDROID_SERVICE_ACCT"])

REPORTS_DIR  = Path(__file__).parent / "reports"
FEEDBACK_FILE = Path(__file__).parent / "feedback.json"


# ─── FEEDBACK LEARNING ───────────────────────────────────────────────────────

def save_feedback(item: dict, original_response: str, edited_response: str):
    """Save an edited response as a learning example for future runs."""
    feedback = []
    if FEEDBACK_FILE.exists():
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            feedback = json.load(f)

    feedback.append({
        "date": datetime.date.today().isoformat(),
        "platform": item.get("platform"),
        "rating": item.get("rating"),
        "review": item.get("body", ""),
        "original_response": original_response,
        "corrected_response": edited_response,
    })

    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(feedback, f, ensure_ascii=False, indent=2)

    print("  📚 Saved as learning example for next run.")


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _make_ios_token():
    with open(IOS_P8_PATH, "r") as f:
        private_key = f.read()
    now = int(time.time())
    payload = {
        "iss": IOS_ISSUER_ID,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": IOS_KEY_ID})


def load_report(path: Path = None) -> tuple[dict, Path]:
    if path:
        report_path = Path(path)
    else:
        today = datetime.date.today().isoformat()
        report_path = REPORTS_DIR / f"report_{today}.json"

    if not report_path.exists():
        # Find latest report
        reports = sorted(REPORTS_DIR.glob("report_*.json"), reverse=True)
        if not reports:
            print("❌ No reports found. Run agent.py first.")
            sys.exit(1)
        report_path = reports[0]
        print(f"ℹ️  Using latest report: {report_path.name}")

    with open(report_path, "r", encoding="utf-8") as f:
        return json.load(f), report_path


def save_report(report: dict, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def post_ios_response(review_id: str, response_body: str) -> bool:
    """Post a developer response to an App Store Connect review."""
    token = _make_ios_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Check if response already exists
    check_url = f"https://api.appstoreconnect.apple.com/v1/customerReviews/{review_id}/response"
    check_resp = requests.get(check_url, headers=headers)

    if check_resp.status_code == 200 and check_resp.json().get("data"):
        # Update existing response
        existing_id = check_resp.json()["data"]["id"]
        url = f"https://api.appstoreconnect.apple.com/v1/customerReviewResponses/{existing_id}"
        method = "PATCH"
        payload = {
            "data": {
                "type": "customerReviewResponses",
                "id": existing_id,
                "attributes": {"responseBody": response_body},
            }
        }
    else:
        # Create new response
        url = "https://api.appstoreconnect.apple.com/v1/customerReviewResponses"
        method = "POST"
        payload = {
            "data": {
                "type": "customerReviewResponses",
                "attributes": {"responseBody": response_body},
                "relationships": {
                    "review": {
                        "data": {"type": "customerReviews", "id": review_id}
                    }
                },
            }
        }

    resp = requests.request(method, url, headers=headers, json=payload)

    if resp.status_code in (200, 201):
        return True
    else:
        print(f"   ❌ API error {resp.status_code}: {resp.text[:200]}")
        return False


def _extract_android_review_id(raw_id: str) -> str:
    """Extract the reviewId from a full Google Play URL or return as-is."""
    if raw_id.startswith("http"):
        parsed = urlparse(raw_id)
        params = parse_qs(parsed.query)
        return params.get("reviewId", [raw_id])[0]
    return raw_id


def post_android_response(review_id: str, response_body: str) -> bool:
    """Post a developer response to a Google Play review."""
    try:
        clean_id = _extract_android_review_id(review_id)
        creds = service_account.Credentials.from_service_account_file(
            ANDROID_SERVICE_ACCT,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        service = build("androidpublisher", "v3", credentials=creds)
        service.reviews().reply(
            packageName=ANDROID_PACKAGE,
            reviewId=clean_id,
            body={"replyText": response_body},
        ).execute()
        return True
    except Exception as e:
        print(f"   ❌ Android API error: {e}")
        return False


def _stars(n):
    return "⭐" * int(n) + "☆" * (5 - int(n))


def _platform_icon(platform):
    return "🍎" if platform == "ios" else "🤖"


# ─── INTERACTIVE APPROVAL ─────────────────────────────────────────────────────

def review_interactively(report: dict, report_path: Path):
    items = report["items"]
    pending = [i for i in items if not i.get("approved") and i.get("proposed_response")]

    if not pending:
        print("✅ All responses in this report have already been approved.")
        return

    print(f"\nFound {len(pending)} response(s) to review.\n")
    print("Commands:  [y] Approve  [e] Edit  [s] Skip  [a] Approve all  [q] Quit\n")
    print("─" * 60)

    approved_ios = []
    approved_android = []

    for idx, item in enumerate(pending, 1):
        platform = item.get("platform", "?")
        print(f"\n[{idx}/{len(pending)}] {_platform_icon(platform)} {platform.upper()}")
        print(f"Author : {item.get('author', 'Anonymous')}")
        print(f"Rating : {_stars(item.get('rating', 0))} ({item.get('rating')}/5)")
        if item.get("title"):
            print(f"Title  : {item['title']}")
        print(f"Review : {item.get('body', '(no text)')}")
        print()
        print(f"📝 Proposed response:")
        print(f"   {item['proposed_response']}")
        print()

        while True:
            choice = input("→ [y/e/s/a/q]: ").strip().lower()
            if choice == "q":
                print("\nQuitting. Progress saved.")
                save_report(report, report_path)
                _post_approved(approved_ios, approved_android)
                return
            elif choice == "a":
                # Approve all remaining (current + rest)
                print("  ✓ Approving all remaining responses...")
                remaining = pending[idx - 1:]  # current item onwards
                for r_item in remaining:
                    for r in report["items"]:
                        if r["id"] == r_item["id"]:
                            r["approved"] = True
                            break
                    if r_item.get("platform") == "ios":
                        if r_item not in approved_ios:
                            approved_ios.append(r_item)
                    else:
                        if r_item not in approved_android:
                            approved_android.append(r_item)
                print(f"  ✓ {len(remaining)} response(s) approved!")
                save_report(report, report_path)
                total_approved = len(approved_ios) + len(approved_android)
                print(f"\n📋 Summary: {total_approved} response(s) ready to post.")
                print(f"   iOS: {len(approved_ios)} | Android: {len(approved_android)}")
                print()
                confirm = input("Should I send the replies? [y/n]: ").strip().lower()
                if confirm == "y":
                    _post_approved(approved_ios, approved_android)
                else:
                    print("\n⏸  Saved but not posted. Run approve.py again to post.")
                return
            elif choice == "s":
                print("  Skipped.")
                break
            elif choice == "y":
                # Mark approved in report
                for r in report["items"]:
                    if r["id"] == item["id"]:
                        r["approved"] = True
                        break
                if platform == "ios":
                    approved_ios.append(item)
                else:
                    approved_android.append(item)
                print("  ✓ Approved!")
                break
            elif choice == "e":
                print("  Enter your edited response (press Enter twice when done):")
                lines = []
                while True:
                    line = input("  ")
                    if line == "" and lines:
                        break
                    lines.append(line)
                new_response = " ".join(lines).strip()
                if new_response:
                    original_response = item["proposed_response"]
                    item["proposed_response"] = new_response
                    for r in report["items"]:
                        if r["id"] == item["id"]:
                            r["proposed_response"] = new_response
                            r["approved"] = True
                            break
                    if platform == "ios":
                        approved_ios.append(item)
                    else:
                        approved_android.append(item)
                    save_feedback(item, original_response, new_response)
                    print("  ✓ Updated and approved!")
                break
            else:
                print("  Please enter y, e, s, or q.")

        print("─" * 60)

    save_report(report, report_path)

    total_approved = len(approved_ios) + len(approved_android)
    if total_approved == 0:
        print("\nNo responses approved. Nothing to post.")
        return

    print(f"\n📋 Summary: {total_approved} response(s) ready to post.")
    print(f"   iOS: {len(approved_ios)} (posts automatically)")
    print(f"   Android: {len(approved_android)} (posts automatically)")
    print()
    confirm = input("Should I send the replies? [y/n]: ").strip().lower()
    if confirm == "y":
        _post_approved(approved_ios, approved_android)
    else:
        print("\n⏸  Responses saved but not posted. Run approve.py again to post them.")


def _load_ios_responded() -> set:
    path = Path(__file__).parent / "ios_responded.json"
    if not path.exists():
        return set()
    with open(path) as f:
        return set(json.load(f))

def _save_ios_responded(ids: set):
    path = Path(__file__).parent / "ios_responded.json"
    with open(path, "w") as f:
        json.dump(list(ids), f)

def _load_android_responded() -> set:
    path = Path(__file__).parent / "android_responded.json"
    if not path.exists():
        return set()
    with open(path) as f:
        return set(json.load(f))

def _save_android_responded(ids: set):
    path = Path(__file__).parent / "android_responded.json"
    with open(path, "w") as f:
        json.dump(list(ids), f)

def _post_approved(approved_ios: list, approved_android: list):
    if approved_ios:
        print(f"\n📤 Posting {len(approved_ios)} iOS response(s)...")
        responded = _load_ios_responded()
        for item in approved_ios:
            print(f"   → {item.get('author', '?')}", end=" ", flush=True)
            ok = post_ios_response(item["id"], item["proposed_response"])
            print("✓ Posted" if ok else "✗ Failed")
            if ok:
                responded.add(item["id"])
        _save_ios_responded(responded)

    if approved_android:
        print(f"\n📤 Posting {len(approved_android)} Android response(s)...")
        android_responded = _load_android_responded()
        for item in approved_android:
            print(f"   → {item.get('author', '?')}", end=" ", flush=True)
            ok = post_android_response(item["id"], item["proposed_response"])
            print("✓ Posted" if ok else "✗ Failed")
            if ok:
                android_responded.add(item["id"])
        _save_android_responded(android_responded)

    total = len(approved_ios) + len(approved_android)
    print(f"\n✅ Done! {total} response(s) posted.")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    report_path_arg = sys.argv[1] if len(sys.argv) > 1 else None
    report, report_path = load_report(report_path_arg)

    print("=" * 60)
    print("  Luca Money — Review Approval")
    print(f"  Report: {report_path.name}")
    print(f"  Total reviews: {report['total_reviews']} "
          f"({report['ios_count']} iOS, {report['android_count']} Android)")
    print("=" * 60)

    review_interactively(report, report_path)


if __name__ == "__main__":
    main()
