import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from roo.skills.executor import SkillExecutor

async def test_executor_error_handling():
    executor = SkillExecutor()
    skill = MagicMock()
    skill.name = "mlai-points"
    skill.get_client_class.return_value = MagicMock()
    
    # Mock settings
    with unittest.mock.patch('roo.skills.executor.get_settings') as mock_settings:
        mock_settings.return_value.MLAI_BACKEND_URL = "http://test"
        mock_settings.return_value.MLAI_API_KEY = "key"
        
        # Test 1: PermissionError
        print("\n--- Test 1: PermissionError ---")
        executor._handle_points_action = AsyncMock(side_effect=PermissionError("Only admins"))
        result = await executor._execute_mlai_points(skill, "text", {}, "user", None, None)
        print(f"Result: {result}")
        assert "Sorry mate, you're not authorized" in result

        # Test 2: ValueError
        print("\n--- Test 2: ValueError ---")
        executor._handle_points_action = AsyncMock(side_effect=ValueError("Nice try!"))
        result = await executor._execute_mlai_points(skill, "text", {}, "user", None, None)
        print(f"Result: {result}")
        assert "Nice try!" in result

        print("\n✅ Verification Successful!")

if __name__ == "__main__":
    import unittest
    asyncio.run(test_executor_error_handling())
