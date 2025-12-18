"""
MLAI Points Client

HTTP client for the mlai-backend Points System API.
This module is the implementation backing the mlai-points skill.
"""
import httpx
from typing import Optional, List, Dict, Any
from datetime import date


class PointsClient:
    """Client for MLAI Points API."""
    
    def __init__(self, base_url: str, api_key: Optional[str] = None, internal_api_key: Optional[str] = None):
        """
        Initialize the Points client.
        
        Args:
            base_url: Base URL of mlai-backend (e.g., https://api.mlai.au)
            api_key: Optional API key for user authentication
            internal_api_key: Optional secure key for admin operations
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.internal_api_key = internal_api_key
        self._points_base = f"{self.base_url}/api/v1/points"
        
        # Cache admin status to reduce API calls
        self._admin_cache: Dict[str, bool] = {}

    def _clean_slack_id(self, user_id: str) -> str:
        """Clean a Slack ID or mention string to extract the ID."""
        if not user_id:
            return user_id
        # Handle <@U12345> format
        if user_id.startswith("<@") and user_id.endswith(">"):
            parts = user_id[2:-1].split("|")
            return parts[0]
        # Handle @U12345 format
        if user_id.startswith("@"):
            return user_id[1:]
        return user_id
    
    @property
    def headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers
        
    @property
    def admin_headers(self) -> dict:
        """Headers for admin endpoints using internal secure key."""
        headers = {"Content-Type": "application/json"}
        if self.internal_api_key:
            headers["X-API-Key"] = self.internal_api_key
        return headers
    
    # =========================================================================
    # Member Endpoints
    # =========================================================================
    
    async def get_balance(self, slack_user_id: str) -> dict:
        """
        Get points balance for a user.
        
        Returns:
            Dict with balance, lifetime_earned, lifetime_spent
        """
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/users/{slack_user_id}/balance/",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_history(
        self, 
        slack_user_id: str, 
        limit: int = 10
    ) -> List[dict]:
        """Get recent ledger entries for a user."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/ledger/",
                params={"slack_user_id": slack_user_id},
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()[:limit]
    
    async def list_tasks(
        self, 
        status: Optional[str] = "open",
        portfolio: Optional[str] = None
    ) -> List[dict]:
        """List tasks, optionally filtered by status and portfolio."""
        params = {}
        if status:
            params["status"] = status
        if portfolio:
            params["portfolio"] = portfolio
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/tasks/",
                params=params,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_task(self, task_id: int) -> dict:
        """Get a specific task by ID."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/tasks/{task_id}/",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def claim_task(
        self, 
        task_id: int, 
        slack_user_id: str
    ) -> dict:
        """Claim a task for completion."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/claim/",
                json={"slack_user_id": self._clean_slack_id(slack_user_id)},
                headers=self.headers,

                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def submit_task(
        self,
        task_id: int,
        slack_user_id: str,
        submission_text: str,
        submission_url: Optional[str] = None
    ) -> dict:
        """Submit completed work for a task."""
        payload = {
            "slack_user_id": slack_user_id,
            "submission_text": submission_text,
        }
        if submission_url:
            payload["submission_url"] = submission_url
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/submit/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def check_coworking(
        self,
        check_date: Optional[str] = None,
        days: int = 7
    ) -> List[dict]:
        """Check coworking availability."""
        params = {"days": days}
        if check_date:
            params["date"] = check_date
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/coworking/availability/",
                params=params,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def book_coworking(
        self,
        slack_user_id: str,
        booking_date: str,
        slack_channel_id: Optional[str] = None
    ) -> dict:
        """Book a coworking day."""
        payload = {
            "slack_user_id": slack_user_id,
            "date": booking_date,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/coworking/book/",
                json=payload,
                headers=self.headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()
    
    async def cancel_coworking(
        self,
        slack_user_id: str,
        booking_id: Optional[str] = None,
        booking_date: Optional[str] = None
    ) -> dict:
        """Cancel a coworking booking."""
        payload = {"slack_user_id": slack_user_id}
        if booking_id:
            payload["booking_id"] = booking_id
        elif booking_date:
            payload["date"] = booking_date
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/coworking/cancel/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_my_bookings(self, slack_user_id: str) -> List[dict]:
        """Get user's coworking bookings."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/coworking/my-bookings/",
                params={"slack_user_id": slack_user_id},
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_rate_card(self) -> List[dict]:
        """
        Get the automated rate card for point awards.
        
        Returns:
            List of dicts with 'alias', 'name', 'points'.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._points_base}/rate-card/",
                    headers=self.headers,
                    timeout=5.0
                )
                if response.status_code == 404:
                    print("⚠️ Rate card endpoint not found (backend might be outdated).")
                    return []
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"❌ Failed to fetch rate card: {e}")
            return []

    async def list_rewards(self, slack_user_id: Optional[str] = None) -> List[dict]:
        """List available rewards."""
        params = {}
        if slack_user_id:
            params["slack_user_id"] = slack_user_id
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/rewards/",
                params=params,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def request_reward(
        self,
        slack_user_id: str,
        reward_code: str,
        quantity: int = 1,
        notes: Optional[str] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None
    ) -> dict:
        """Request a reward redemption."""
        payload = {
            "slack_user_id": slack_user_id,
            "reward_code": reward_code,
            "quantity": quantity,
        }
        if notes:
            payload["notes"] = notes
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/rewards/request/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    # =========================================================================
    # Admin Endpoints
    # =========================================================================
    
    async def is_admin(self, slack_user_id: str) -> bool:
        """Check if a user is a Points Admin (with caching)."""
        if slack_user_id in self._admin_cache:
            return self._admin_cache[slack_user_id]
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._points_base}/admins/{slack_user_id}/",
                    headers=self.headers,
                    timeout=10.0
                )
                is_admin = response.status_code == 200
                self._admin_cache[slack_user_id] = is_admin
                return is_admin
        except Exception:
            return False
    
    async def create_task(
        self,
        admin_slack_id: str,
        title: str,
        points: int,
        description: str = "",
        portfolio: str = "events",
        due_date: Optional[str] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None
    ) -> dict:
        """Create a new task (admin only)."""
        payload = {
            "title": title,
            "points": points,
            "description": description,
            "portfolio": portfolio,
            "created_by_user_id": admin_slack_id,
            "status": "open",
        }
        if due_date:
            payload["due_date"] = due_date
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/",
                json=payload,
                headers=self.admin_headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def approve_task(
        self,
        task_id: int,
        admin_slack_id: str,
        submission_id: Optional[str] = None
    ) -> dict:
        """Approve a task submission (admin only)."""
        payload = {"slack_user_id": admin_slack_id}
        if submission_id:
            payload["submission_id"] = submission_id
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/approve/",
                json=payload,
                headers=self.admin_headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()
    
    async def reject_task(
        self,
        task_id: int,
        admin_slack_id: str,
        reason: str = "",
        submission_id: Optional[str] = None
    ) -> dict:
        """Reject a task submission (admin only)."""
        payload = {
            "slack_user_id": admin_slack_id,
            "reason": reason,
        }
        if submission_id:
            payload["submission_id"] = submission_id
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/reject/",
                json=payload,
                headers=self.admin_headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def award_task(
        self,
        task_id: int,
        admin_slack_id: str,
        target_slack_id: str
    ) -> dict:
        """Direct award a task (claim + approve) to a user (admin only)."""
        payload = {
            "created_by_user_id": admin_slack_id,
            "assigned_to_user_id": self._clean_slack_id(target_slack_id),
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/award/",
                json=payload,
                headers=self.admin_headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()
    
    async def award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str
    ) -> dict:
        """Manually award or deduct points (admin only)."""
        # 1. Pre-flight Admin Check
        is_admin = await self.is_admin(admin_slack_id)
        if not is_admin:
            raise PermissionError(f"User {admin_slack_id} is not multiple Points Admin.")

        # 2. Pre-flight Self-Award Check
        cleaned_target = self._clean_slack_id(target_slack_id)
        if admin_slack_id == cleaned_target and points > 0:
            raise ValueError("Nice try! You can't award points to yourself. 😉")

        payload = {
            "admin_slack_id": admin_slack_id,
            "target_slack_id": cleaned_target,
            "points": points,
            "reason": reason,
        }
        async with httpx.AsyncClient() as client:
            print(f"🕵️ DEBUG: POST {self._points_base}/admin/award/ | Payload: {payload}")
            response = await client.post(
                f"{self._points_base}/admin/award/",
                json=payload,
                headers=self.admin_headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()
    
    async def approve_reward(
        self,
        admin_slack_id: str,
        redemption_id: str
    ) -> dict:
        """Approve a reward redemption request (admin only)."""
        payload = {
            "slack_user_id": admin_slack_id,
            "redemption_id": redemption_id,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/rewards/approve/",
                json=payload,
                headers=self.admin_headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_pending_redemptions(self, admin_slack_id: str) -> List[dict]:
        """Get pending reward redemption requests (admin only)."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/rewards/pending/",
                params={"slack_user_id": admin_slack_id},
                headers=self.admin_headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
