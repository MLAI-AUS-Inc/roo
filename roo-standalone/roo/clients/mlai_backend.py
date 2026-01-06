"""
MLAI Backend Client

HTTP client for communicating with the mlai-backend service.
"""
from typing import Optional, Dict, Any, List

import httpx

from ..config import get_settings


class MLAIBackendClient:
    """Client for mlai-backend API."""
    
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, internal_api_key: Optional[str] = None):
        settings = get_settings()
        self.base_url = base_url or settings.MLAI_BACKEND_URL
        self.api_key = api_key or settings.MLAI_API_KEY
        self.internal_api_key = internal_api_key or settings.INTERNAL_API_KEY
        self.base_url = self.base_url.rstrip('/') if self.base_url else ""
        self._points_base = f"{self.base_url}/api/v1/points"
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
        # Prefer the internal key, but fall back to the standard API key so
        # admin endpoints still authenticate when only one key is configured.
        if hasattr(self, 'internal_api_key') and self.internal_api_key:
             headers["X-API-Key"] = self.internal_api_key
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers
    
    async def save_article_generation(
        self,
        slack_user_id: str,
        job_id: str,
        domain: str,
        result: Optional[dict] = None
    ) -> dict:
        """
        Save article generation record to mlai-backend.
        
        Args:
            slack_user_id: Slack user who triggered generation
            job_id: Content Factory job ID
            domain: Target domain
            result: Optional generation result data
        
        Returns:
            Created record data
        """
        if not self.base_url:
            print("⚠️  MLAI_BACKEND_URL not configured, skipping save")
            return {}
        
        payload = {
            "slack_user_id": slack_user_id,
            "job_id": job_id,
            "domain": domain,
        }
        
        if result:
            payload.update({
                "topic": result.get("topic"),
                "title": result.get("title"),
                "slug": result.get("slug"),
                "meta_title": result.get("meta_title"),
                "meta_description": result.get("meta_description"),
                "keywords": result.get("keywords", []),
                "status": "completed"
            })
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/roo/article-generations/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_user_by_slack_id(self, slack_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user information by Slack ID.
        
        Args:
            slack_id: Slack user ID
        
        Returns:
            User data or None if not found
        """
        if not self.base_url:
            return None
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/api/roo/users/slack/{slack_id}/",
                    headers=self.headers,
                    timeout=10.0
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"❌ User lookup failed: {e}")
                return None
    
    async def create_user(
        self,
        slack_id: str,
        name: str,
        email: Optional[str] = None
    ) -> dict:
        """Create a new user in mlai-backend."""
        if not self.base_url:
            return {}
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/roo/users/",
                json={
                    "slack_id": slack_id,
                    "name": name,
                    "email": email
                },
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def trigger_article_generation(
        self,
        slack_user_id: str,
        domain: str,
        topic: str,
        target_keyword: str,
        context: Optional[str] = None
    ) -> dict:
        """
        Trigger article generation via mlai-backend.
        
        Args:
            slack_user_id: Slack ID of requesting user
            domain: Target domain
            topic: Article topic
            target_keyword: Main keyword
            context: Optional conversation context
            
        Returns:
            Dict containing job_id and status
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        payload = {
            "slack_user_id": slack_user_id,
            "domain": domain,
            "topic": topic,
            "target_keyword": target_keyword,
            "context": context
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/content/generate",
                json=payload,
                headers=self.headers,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()

    async def get_content_org_config(self, slack_user_id: str) -> Optional[dict]:
        """
        Check if content factory org config exists for a user.
        
        Args:
            slack_user_id: The Slack ID of the user
            
        Returns:
            Config dict or None if not found
        """
        if not self.base_url:
            return None
        
        # Clean ID here just in case caller didn't
        clean_id = self._clean_slack_id(slack_user_id)
        params = {"slack_user_id": clean_id}
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/api/content-factory/org/config",
                    params=params,
                    headers=self.headers,
                    timeout=5.0
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"Failed to check org config: {e}")
                return None

    async def discover_opportunities(
        self,
        domain: str,
        competitors: list[str],
        seed_keywords: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Discover content opportunities via mlai-backend.
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        payload = {
            "domain": domain,
            "competitors": competitors,
            "seed_keywords": seed_keywords
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/content/discover",
                    json=payload,
                    headers=self.headers,
                    timeout=60.0
                )
                response.raise_for_status()
                data = response.json()
                return data.get("opportunities", [])
            except Exception as e:
                print(f"Discovery failed: {e}")
                raise

    async def check_generation_status(self, job_id: str) -> dict:
        """Check status of a generation job."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/api/v1/content/jobs/{job_id}",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    # =========================================================================
    # Points / Member Endpoints (Merged from mlai_points/client.py)
    # =========================================================================
    
    async def get_balance(self, slack_user_id: str) -> dict:
        """Get points balance for a user."""
        if not self.base_url: return {}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/users/{slack_user_id}/balance/",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_history(self, slack_user_id: str, limit: int = 10) -> List[dict]:
        """Get recent ledger entries for a user."""
        if not self.base_url: return []
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/ledger/",
                params={"slack_user_id": slack_user_id},
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()[:limit]
    
    async def list_tasks(self, status: Optional[str] = "open", portfolio: Optional[str] = None) -> List[dict]:
        """List tasks, optionally filtered by status and portfolio."""
        if not self.base_url: return []
        params = {}
        if status: params["status"] = status
        if portfolio: params["portfolio"] = portfolio
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/tasks/",
                params=params,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def claim_task(self, task_id: int, slack_user_id: str) -> dict:
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

    async def submit_task(self, task_id: int, slack_user_id: str, submission_text: str, submission_url: Optional[str] = None) -> dict:
        """Submit completed work for a task."""
        payload = {
            "slack_user_id": slack_user_id,
            "submission_text": submission_text,
        }
        if submission_url: payload["submission_url"] = submission_url
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/tasks/{task_id}/submit/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def check_coworking(self, check_date: Optional[str] = None, days: int = 7) -> List[dict]:
        """Check coworking availability."""
        params = {"days": days}
        if check_date: params["date"] = check_date
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self._points_base}/coworking/availability/",
                params=params,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def book_coworking(self, slack_user_id: str, booking_date: str, slack_channel_id: Optional[str] = None) -> dict:
        """Book a coworking day."""
        try:
            from datetime import datetime
            current_time = datetime.now().isoformat()
        except Exception:
            current_time = ""
        payload = {
            "slack_user_id": slack_user_id,
            "date": booking_date,
            "current_time": current_time,
        }
        if slack_channel_id: payload["slack_channel_id"] = slack_channel_id
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/coworking/book/",
                json=payload,
                headers=self.headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_github_auth_url(self, slack_user_id: str) -> dict:
        """Get the GitHub OAuth URL for a user from the backend."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/integrations/github/auth-url",
                    params={"slack_user_id": slack_user_id},
                    headers=self.headers,
                    timeout=10.0
                )
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Failed to get GitHub auth URL: {e}")
            return {"error": str(e)}

    async def get_integration(self, slack_user_id: str) -> Optional[dict]:
        """Check if user has a valid GitHub integration."""
        try:
            clean_id = self._clean_slack_id(slack_user_id)
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/integrations/github/{clean_id}/",
                    headers=self.headers,
                    timeout=10.0
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Failed to check integration status: {e}")
            return None

    async def save_pending_intent(self, slack_user_id: str, intent_data: str) -> None:
        """Save a pending intent to resume after auth."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/integrations/pending-intent/",
                json={"slack_user_id": slack_user_id, "intent_data": intent_data},
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()

    async def trigger_repo_scan(self, slack_user_id: str, slack_channel_id: Optional[str] = None, slack_thread_ts: Optional[str] = None) -> dict:
        """Trigger a repository scan for a user via the backend."""
        try:
            clean_id = self._clean_slack_id(slack_user_id)
            payload = {"slack_user_id": clean_id}
            if slack_channel_id:
                payload["slack_channel_id"] = slack_channel_id
            if slack_thread_ts:
                payload["slack_thread_ts"] = slack_thread_ts
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/integrations/github/scan",
                    json=payload,
                    headers=self.headers,
                    timeout=60.0
                )
                if response.status_code == 202:
                    return {"status": "accepted", "message": "Scan queued successfully."}
                if response.status_code == 404:
                    return {"error": "no_integration", "message": "No GitHub integration found for this user."}
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"Failed to trigger repo scan: {e}")
            return {"error": "scan_failed", "message": str(e)}

    async def publish_article(self, job_id: str, slack_user_id: str) -> dict:
        """Request publication of a completed article."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
        
        # Ensure we have a clean ID
        clean_id = self._clean_slack_id(slack_user_id)
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/content/publish/{job_id}",
                json={"slack_user_id": clean_id},
                headers=self.headers,
                timeout=60.0
            )
            response.raise_for_status()
            return response.json()

    # =========================================================================
    # Missing Admin / Points Methods
    # =========================================================================

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

    async def get_rate_card(self) -> List[dict]:
        """Get the automated rate card for point awards."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._points_base}/rate-card/",
                    headers=self.headers,
                    timeout=5.0
                )
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                return response.json()
        except Exception as e:
            print(f"❌ Failed to fetch rate card: {e}")
            return []

    async def is_admin(self, slack_user_id: str) -> bool:
        """Check if a user is a Points Admin (with caching)."""
        if slack_user_id in self._admin_cache:
            return self._admin_cache[slack_user_id]
        
        try:
            details = await self.get_admin_details(slack_user_id)
            is_admin = details is not None
            self._admin_cache[slack_user_id] = is_admin
            return is_admin
        except Exception:
            return False

    async def get_admin_details(self, slack_user_id: str) -> Optional[dict]:
        """Get details for a Points Admin."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._points_base}/admins/{slack_user_id}/",
                    headers=self.headers,
                    timeout=10.0
                )
                if response.status_code == 200:
                    return response.json()
                return None
        except Exception as e:
            print(f"Failed to fetch admin details: {e}")
            return None

    async def get_admin_allowance(self, slack_user_id: str) -> dict:
        """Get the admin's weekly allowance status."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._points_base}/admin/allowance/",
                    params={"slack_id": slack_user_id},
                    headers=self.admin_headers,
                    timeout=10.0
                )
                if response.status_code == 404:
                    return {'error': 'Not a points admin'}
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {'error': 'Not a points admin'}
            raise
        except Exception as e:
            print(f"❌ Failed to fetch admin allowance: {e}")
            return {'error': str(e)}

    async def create_task(
        self,
        admin_slack_id: str,
        title: str,
        points: int,
        description: str = "",
        portfolio: str = "events",
        due_date: Optional[str] = None,
        assigned_to_user_id: Optional[str] = None,
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
        if assigned_to_user_id:
            payload["assigned_to_user_id"] = self._clean_slack_id(assigned_to_user_id)
            payload["status"] = "claimed"
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
            # Handle 403 gracefully
            if response.status_code == 403:
                return {"error": "forbidden", "message": response.json().get("error")}
                
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

    async def award_task(self, task_id: int, admin_slack_id: str, target_slack_id: str) -> dict:
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
            raise PermissionError(f"User {admin_slack_id} is not a Points Admin.")

        # 2. Pre-flight Self-Award Check
        cleaned_target = self._clean_slack_id(target_slack_id)
        if admin_slack_id == cleaned_target and points > 0:
            raise ValueError("Nice try! You can't award points to yourself. 😉")

        # 3. Pre-flight Negative Check
        if points < 0:
            raise ValueError("Point deductions are disabled. Only positive awards are allowed.")

        # 4. Pre-flight Weekly Allowance Check
        if points > 0:
            allowance = await self.get_admin_allowance(admin_slack_id)
            if 'error' in allowance:
                raise PermissionError(allowance['error'])
            remaining = allowance.get('remaining', 0)
            if remaining <= 0:
                raise ValueError(
                    f"You've used your full weekly allowance ({allowance.get('allowance', 0)} pts). It resets on Monday."
                )
            if points > remaining:
                raise ValueError(
                    f"You only have {remaining} pts left this week. Try awarding {remaining} or less."
                )

        payload = {
            "admin_slack_id": admin_slack_id,
            "target_slack_id": cleaned_target,
            "points": points,
            "reason": reason,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/admin/award/",
                json=payload,
                headers=self.admin_headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()

    async def list_rewards(self, slack_user_id: Optional[str] = None) -> List[dict]:
        """List available rewards."""
        params = {}
        if slack_user_id: params["slack_user_id"] = slack_user_id
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
        if notes: payload["notes"] = notes
        if slack_channel_id: payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts: payload["slack_thread_ts"] = slack_thread_ts
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/rewards/request/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def approve_reward(self, admin_slack_id: str, redemption_id: str) -> dict:
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

    async def system_award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str
    ) -> dict:
        """System award points (bypasses client-side admin checks)."""
        payload = {
            "admin_slack_id": admin_slack_id,
            "target_slack_id": self._clean_slack_id(target_slack_id),
            "points": points,
            "reason": reason,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._points_base}/admin/award/",
                json=payload,
                headers=self.admin_headers,
                timeout=15.0
            )
            response.raise_for_status()
            return response.json()

    async def link_slack_user(self, slack_id: str, email: str) -> Optional[int]:
        """Link a Slack ID to an existing user found by email."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/api/v1/users/link-slack/",
                    json={"slack_id": slack_id, "email": email},
                    headers=self.admin_headers,
                    timeout=10.0
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json().get("user_id")
        except Exception as e:
            print(f"Failed to link Slack user: {e}")
            return None

    # End of MLAIBackendClient
