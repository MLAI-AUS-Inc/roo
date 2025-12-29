import pytest
import asyncio
from unittest.mock import MagicMock, patch
from roo.agent import RooAgent

@pytest.mark.asyncio
async def test_fast_path_balance():
    agent = RooAgent()
    
    # Mock the skill selection to ensure we don't hit LLM if fast path matches
    agent._select_skill = MagicMock()
    
    # Mock MLAIBackendClient
    mock_client = MagicMock()
    mock_client.get_balance = MagicMock(return_value=asyncio.Future())
    mock_client.get_balance.return_value.set_result({"balance": 100, "lifetime_earned": 200})
    
    # Patch bot ID for cleaning
    with patch('roo.slack_client.get_bot_user_id', return_value="MYBOTID"):
        
        # Patch the skill's get_client_class to return our mock
        with patch('roo.skills.loader.Skill.get_client_class', return_value=MagicMock(return_value=mock_client)):
            # Inject a mock skill into agent.skills so it finds "mlai-points"
            mock_skill = MagicMock()
            mock_skill.name = "mlai-points"
            agent.skills = [mock_skill]
            mock_skill.get_client_class.return_value = MagicMock(return_value=mock_client)
            
            # Test "<@MYBOTID> points"
            result = await agent.handle_mention("<@MYBOTID> points", "U123")
            
            assert result['skill_used'] == "mlai-points (fast)"
            assert "100 points" in result['message']
            mock_client.get_balance.assert_called_with("U123")
            
            # Ensure LLM was NOT called
            agent._select_skill.assert_not_called()

@pytest.mark.asyncio
async def test_fast_path_book_today():
    agent = RooAgent()
    agent._select_skill = MagicMock()
    
    mock_client = MagicMock()
    mock_client.book_coworking = MagicMock(return_value=asyncio.Future())
    mock_client.book_coworking.return_value.set_result({"points_cost": 1})
    
    with patch('roo.slack_client.get_bot_user_id', return_value="MYBOTID"):
        with patch('roo.skills.loader.Skill.get_client_class', return_value=MagicMock(return_value=mock_client)):
            mock_skill = MagicMock()
            mock_skill.name = "mlai-points"
            agent.skills = [mock_skill]
            mock_skill.get_client_class.return_value = MagicMock(return_value=mock_client)
            
            # Test "<@MYBOTID> coworking book today"
            result = await agent.handle_mention("<@MYBOTID> coworking book today", "U123")
            
            assert result['skill_used'] == "mlai-points (fast)"
            assert "Booked you in" in result['message']
            
            # Check if date=today was passed
            from datetime import date
            today = date.today().isoformat()
            mock_client.book_coworking.assert_called_with("U123", today, None)
            
            agent._select_skill.assert_not_called()

@pytest.mark.asyncio
async def test_complex_query_falls_through():
    agent = RooAgent()
    
    # Mock select_skill to return None (triggering general response) to handle the fallthrough
    # In reality it would return a skill, but we just want to prove Fast Path returns None
    agent._select_skill = MagicMock(return_value=asyncio.Future())
    agent._select_skill.return_value.set_result(None)
    agent._general_response = MagicMock(return_value=asyncio.Future())
    agent._general_response.return_value.set_result("General response")
    
    # Test "@Roo can I buy a sticker?"
    await agent.handle_mention("@Roo can I buy a sticker?", "U123")
    
    # Ensure LLM WAS called (select_skill called)
    agent._select_skill.assert_called()
