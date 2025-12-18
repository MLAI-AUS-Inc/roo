import pytest
import asyncio
from unittest.mock import MagicMock, patch
from roo.skills.executor import SkillExecutor, Skill
from roo.skills.loader import Skill

@pytest.mark.asyncio
async def test_smart_award():
    executor = SkillExecutor()
    
    # Mock Skill
    mock_skill = MagicMock(spec=Skill)
    mock_skill.name = "mlai-points"
    
    # Mock PointsClient
    mock_client = MagicMock()
    mock_client.get_rate_card = MagicMock(return_value=asyncio.Future())
    mock_client.get_rate_card.return_value.set_result([
        {"alias": "newsletter", "name": "Weekly Newsletter", "points": 10},
        {"alias": "talk", "name": "Meetup Talk", "points": 50}
    ])
    
    mock_client.award_points = MagicMock(return_value=asyncio.Future())
    mock_client.award_points.return_value.set_result({"new_balance": 100})
    
    mock_skill.get_client_class.return_value = MagicMock(return_value=mock_client)
    
    # Test case: "reward @sam for newsletter" (points missing)
    # We simulate parameters extracted by LLM (or regex fallback, though generic executor relies on LLM extraction mostly)
    # Here we test _execute_mlai_points specifically or just _handle_points_action via the executor if exposed.
    # Ideally we test execute().
    
    # Mock parameter extraction to simulate LLM understanding "newsletter" is the reason
    executor._extract_parameters = MagicMock(return_value=asyncio.Future())
    executor._extract_parameters.return_value.set_result({
        "action": "award_points",
        "target_user": "U12345",
        "reason": "newsletter" 
        # Note: points is missing
    })
    
    # Needs channel_id to avoid crash?
    result = await executor.execute(
        skill=mock_skill,
        text="reward @U12345 for newsletter",
        user_id="ADMIN_ID",
        channel_id="C123"
    )
    
    assert result.success is True
    assert "Awarded 10 points" in result.message
    
    # Verify award_points called with 10 pts
    mock_client.award_points.assert_called_with(
        "ADMIN_ID", "U12345", 10, "newsletter (Weekly Newsletter)"
    )

@pytest.mark.asyncio
async def test_smart_award_no_match():
    executor = SkillExecutor()
    mock_skill = MagicMock(spec=Skill)
    mock_skill.name = "mlai-points"
    
    mock_client = MagicMock()
    mock_client.get_rate_card = MagicMock(return_value=asyncio.Future())
    mock_client.get_rate_card.return_value.set_result([]) # Empty rate card
    
    mock_skill.get_client_class.return_value = MagicMock(return_value=mock_client)
    
    executor._extract_parameters = MagicMock(return_value=asyncio.Future())
    executor._extract_parameters.return_value.set_result({
        "action": "award_points",
        "target_user": "U12345",
        "reason": "unknown thing"
    })
    
    result = await executor.execute(
        skill=mock_skill,
        text="reward @U12345 for unknown thing",
        user_id="ADMIN_ID"
    )
    
    # Should expect failure or ask message
    assert "How many points?" in result.message
