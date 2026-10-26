import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from roo.slack_client import get_user_info

def check_user(user_id):
    print(f"🕵️ Checking Slack info for {user_id}...")
    try:
        info = get_user_info(user_id)
        print(f"📄 Result: {info}")
        
        email = info.get("email")
        if email:
            print(f"✅ Email found: {email}")
        else:
            print("❌ No email field in response. Check 'users:read.email' scope.")
            
    except Exception as e:
        print(f"💥 Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = "U08D97NRBJS" # One of the failing IDs from logs
    
    check_user(target)
