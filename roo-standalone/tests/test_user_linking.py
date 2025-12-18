import sys
from unittest.mock import MagicMock

# MOCK DEPENDENCIES BEFORE IMPORT
sys.modules["sqlalchemy"] = MagicMock()
sys.modules["sqlalchemy.ext.asyncio"] = MagicMock()
sys.modules["httpx"] = MagicMock()
sys.modules["frontmatter"] = MagicMock()
sys.modules["pydantic"] = MagicMock()
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["openai"] = MagicMock()
sys.modules["tenacity"] = MagicMock()
sys.modules["slack_sdk"] = MagicMock()
# Mock entire roo.config because it imports pydantic_settings eagerly
sys.modules["roo.config"] = MagicMock()

import asyncio
import unittest
from unittest.mock import patch, AsyncMock

# Must mock get_settings inside config before importing database/executor if they use it at module level
# (Checking imports... executor imports generic stuff. database imports sqlalchemy)

# We can now import our local modules
# Note: we need to handle the fact that 'roo' package resolution might still fail if not in path
import os
sys.path.append(os.getcwd())

# We need to mock the Database class specifically since we can't import the real one easily
# due to its heavy dependencies we just mocked out.
# Let's just mock the import of roo.database entirely?
# No, we want to test executor logic. Executor imports `get_db` from `..database`.

# Let's verify we can import SkillExecutor
try:
    from roo.skills.executor import SkillExecutor
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)

# Import Database after mocks
try:
    from roo.database import Database
except ImportError:
    # Just mock it if import fails due to other deps
    Database = MagicMock()

# Mock data
MOCK_SLACK_ID = "U12345"
MOCK_EMAIL = "test@example.com"
MOCK_DB_USER_ID = 42

class TestUserLinking(unittest.IsolatedAsyncioTestCase):
    
    async def test_user_linking_logic(self):
        """
        Test that the executor:
        1. Checks for Slack ID (fails)
        2. Fetches Slack info (gets email)
        3. Checks DB by email (succeeds)
        4. Links Slack ID to user
        """
        
        # Mock Database
        mock_db = AsyncMock(spec=Database)
        # 1. get_user_by_slack_id returns None (User not found by ID)
        mock_db.get_user_by_slack_id.return_value = None
        # 3. get_user_by_email returns ID (User found by email)
        mock_db.get_user_by_email.return_value = MOCK_DB_USER_ID
        
        # Mock Slack Client
        with patch("roo.skills.executor.get_db", return_value=mock_db), \
             patch("roo.skills.executor.SkillExecutor._execute_with_llm"), \
             patch("roo.skills.executor.post_message"), \
             patch("roo.slack_client.get_user_info") as mock_get_user_info:
            
            # 2. Setup Slack Mock to return email
            mock_get_user_info.return_value = {"email": MOCK_EMAIL}
            
            # Setup Executor and Client
            executor = SkillExecutor()
            mock_client = AsyncMock()
            mock_client.award_points.return_value = {"new_balance": 100}
            
            params = {
                "action": "award_points", 
                "points": 10, 
                "target_users": [MOCK_SLACK_ID],
                "reason": "Test linking"
            }
            
            mock_skill = MagicMock()
            mock_skill.name = "mlai-points"
            
            # Execute
            await executor._handle_points_action(
                client=mock_client,
                action="award_points",
                params=params,
                text=f"award 10 points to <@{MOCK_SLACK_ID}>",
                user_id="ADMIN_ID",
                channel_id="C1",
                thread_ts="t1",
                skill=mock_skill
            )
            
            # ASSERTIONS
            
            # 1. Verify get_user_by_slack_id was called
            mock_db.get_user_by_slack_id.assert_called_with(MOCK_SLACK_ID)
            
            # 2. Verify Slack email lookup happened
            mock_get_user_info.assert_called_with(MOCK_SLACK_ID)
            
            # 3. Verify get_user_by_email was called with the fetched email
            mock_db.get_user_by_email.assert_called_with(MOCK_EMAIL)
            
            # 4. Verify link_user_slack_id was called to link them!
            mock_db.link_user_slack_id.assert_called_with(MOCK_DB_USER_ID, MOCK_SLACK_ID)
            
            print("\n✅ Test Passed: User logic correctly identified email and linked Slack ID.")

if __name__ == "__main__":
    unittest.main()
