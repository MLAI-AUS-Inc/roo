"""
Tests for MLAI Points Client

Unit tests for the PointsClient with mocked HTTP responses.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx


# Import the client
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "mlai_points"))
from client import PointsClient


@pytest.fixture
def client():
    """Create a PointsClient instance for testing."""
    return PointsClient(
        base_url="http://test-api.mlai.au", 
        api_key="test-key",
        internal_api_key="secure-key"
    )


class TestMemberEndpoints:
    """Tests for member-facing endpoints."""
    
    @pytest.mark.asyncio
    async def test_get_balance_success(self, client):
        """Test successful balance retrieval."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "slack_user_id": "U123ABC",
            "balance": 15,
            "lifetime_earned": 42,
            "lifetime_spent": 27
        }
        mock_response.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.get_balance("U123ABC")
            
            assert result["balance"] == 15
            assert result["lifetime_earned"] == 42
            mock_client.get.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_list_tasks_with_filters(self, client):
        """Test listing tasks with status and portfolio filters."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"id": 1, "title": "Task 1", "points": 3, "portfolio": "tech"},
            {"id": 2, "title": "Task 2", "points": 5, "portfolio": "tech"},
        ]
        mock_response.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.list_tasks(status="open", portfolio="tech")
            
            assert len(result) == 2
            assert result[0]["points"] == 3
    
    @pytest.mark.asyncio
    async def test_book_coworking_success(self, client):
        """Test successful coworking booking."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "booking-uuid",
            "date": "2025-12-20",
            "status": "booked",
            "points_cost": 1
        }
        mock_response.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.book_coworking("U123ABC", "2025-12-20")
            
            assert result["status"] == "booked"
            assert result["points_cost"] == 1


class TestAdminEndpoints:
    """Tests for admin-only endpoints."""
    
    @pytest.mark.asyncio
    async def test_is_admin_true(self, client):
        """Test admin check returns true for admins."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"slack_user_id": "U123ABC", "role": "admin"}
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.is_admin("U123ABC")
            
            assert result is True
            # Check caching works
            assert client._admin_cache["U123ABC"] is True
    
    @pytest.mark.asyncio
    async def test_is_admin_false(self, client):
        """Test admin check returns false for non-admins."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.is_admin("U999XXX")
            
            assert result is False
    
    @pytest.mark.asyncio
    async def test_award_points_success(self, client):
        """Test successful manual points award."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ledger": {"id": 1, "delta": 5},
            "new_balance": 20
        }
        mock_response.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            # Mock is_admin to return True
            client.is_admin = AsyncMock(return_value=True)
            
            result = await client.award_points(
                admin_slack_id="UADMIN",
                target_slack_id="UTARGET",
                points=5,
                reason="Test award"
            )
            
            assert result["new_balance"] == 20
    
    @pytest.mark.asyncio
    async def test_create_task_success(self, client):
        """Test successful task creation."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": 42,
            "title": "Test Task",
            "points": 3,
            "portfolio": "tech",
            "status": "open"
        }
        mock_response.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            result = await client.create_task(
                admin_slack_id="UADMIN",
                title="Test Task",
                points=3,
                portfolio="tech"
            )
            
            assert result["id"] == 42
            assert result["status"] == "open"


class TestErrorHandling:
    """Tests for error handling."""
    
    @pytest.mark.asyncio
    async def test_permission_denied_raises(self, client):
        """Test that 403 errors are raised properly."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden",
            request=MagicMock(),
            response=mock_response
        )
        
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_client
            
            # Should raise PermissionError because is_admin returns False
            with pytest.raises(PermissionError):
                await client.award_points("UNOTADMIN", "UTARGET", 5, "test")

    @pytest.mark.asyncio
    async def test_self_award_raises_value_error(self, client):
        """Test that self-awarding points raises ValueError."""
        # Mock admin check to return True so we reach the self-award check
        client.is_admin = AsyncMock(return_value=True)
        
        with pytest.raises(ValueError, match="Nice try"):
            await client.award_points("UADMIN", "UADMIN", 5, "Self award")

    @pytest.mark.asyncio
    async def test_admin_access_required(self, client):
        """Test that non-admins get PermissionError."""
        # Mock admin check to return False
        client.is_admin = AsyncMock(return_value=False)
        
        with pytest.raises(PermissionError):
            await client.award_points("UNOTADMIN", "UTARGET", 5, "test")
